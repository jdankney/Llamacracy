# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTTP-level smoke tests against the real FastAPI app with a scripted
upstream: the SSE chat contract, the OpenAI-compatible endpoint, and the
enqueue-time limit gate leaving no orphan rows behind."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import llamacracy.app as appmod
import llamacracy.queue as queuemod
from llamacracy.config import get_settings
from llamacracy.db import Database, get_db
from llamacracy.registry import ModelInfo, Registry, get_registry
from llamacracy.upstream import get_upstream
from tests.conftest import FakeUpstream


def _registry() -> Registry:
    return Registry({
        "small": ModelInfo("small", "Small", "chat", "fast", True, "", "off",
                           seed_cold_load_s=1.0, seed_tg_tok_s=40.0),
        # can reason on request (the Think toggle); hidden from the picker so
        # the existing tests that list picker models are unaffected
        "thinker": ModelInfo("thinker", "Thinker", "chat", "daily", True, "", "off",
                             thinking=True, seed_cold_load_s=1.0, seed_tg_tok_s=40.0),
    }, "test")


@pytest.fixture
def client(tmp_db_path, monkeypatch, request):
    """A TestClient whose app talks to a temp SQLite file, a scripted upstream
    and an in-code registry. `pytest.mark.env(...)` on the test sets extra
    environment (e.g. a tiny credit cap) before settings are built."""
    monkeypatch.setenv("DEV_MODE", "1")
    monkeypatch.setenv("DEV_EMAIL", "dev@localhost")
    monkeypatch.setenv("ADMIN_EMAILS", "dev@localhost")
    marker = request.node.get_closest_marker("env")
    for k, v in (marker.kwargs if marker else {}).items():
        monkeypatch.setenv(k, str(v))
    get_settings.cache_clear()

    db = Database(tmp_db_path)
    up = FakeUpstream()
    reg = _registry()
    # lifespan() and the queue reach these by name; routes reach them via Depends
    for mod in (appmod, queuemod):
        monkeypatch.setattr(mod, "get_db", lambda: db)
        monkeypatch.setattr(mod, "get_upstream", lambda: up)
        monkeypatch.setattr(mod, "get_registry", lambda: reg)
    appmod.app.dependency_overrides[get_db] = lambda: db
    appmod.app.dependency_overrides[get_upstream] = lambda: up
    appmod.app.dependency_overrides[get_registry] = lambda: reg

    with TestClient(appmod.app) as c:
        c.fake_upstream = up
        c.db = db
        c.registry = reg
        yield c

    appmod.app.dependency_overrides.clear()
    get_settings.cache_clear()


def _sse_events(response) -> list[dict]:
    events = []
    for line in response.iter_lines():
        if line.startswith("data:"):
            events.append(json.loads(line[5:].strip()))
    return events


def _count(db: Database, table: str) -> int:
    return db._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_models_lists_picker_models(client):
    r = client.get("/api/models")
    assert r.status_code == 200
    assert [m["id"] for m in r.json()["models"]] == ["small", "thinker"]
    assert [m["thinking"] for m in r.json()["models"]] == [False, True]


def test_chat_streams_and_persists(client):
    client.fake_upstream.script["small"] = ["Hello", " there"]
    with client.stream("POST", "/api/chat", json={"model": "small", "message": "hi"}) as r:
        assert r.status_code == 200
        events = _sse_events(r)

    types = [e["type"] for e in events]
    assert types[0] == "accepted"
    assert types[-1] == "done"
    assert "".join(e["text"] for e in events if e["type"] == "token") == "Hello there"
    assert events[-1]["completion_tokens"] == 3      # from the scripted usage block

    conv_id = events[0]["conversation_id"]
    detail = client.get(f"/api/conversations/{conv_id}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][1]["content"] == "Hello there"
    assert detail["conversation"]["title"] == "hi"


def test_openai_endpoint_with_api_key(client):
    key = client.post("/api/keys", json={"label": "test"}).json()["key"]
    assert key.startswith("llk_")

    assert client.get("/v1/models").status_code == 401           # no bearer -> 401
    headers = {"Authorization": f"Bearer {key}"}
    assert client.get("/v1/models", headers=headers).json()["data"][0]["id"] == "small"

    r = client.post("/v1/chat/completions", headers=headers, json={
        "model": "small", "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "hello world"
    assert body["usage"]["completion_tokens"] == 3
    # API traffic is billed (a jobs row) but never persisted as chat history
    assert _count(client.db, "jobs") == 1
    assert _count(client.db, "conversations") == 0


@pytest.mark.env(SESSION_CREDIT_LIMIT="0.001")
def test_limit_rejection_leaves_no_orphans(client):
    with client.stream("POST", "/api/chat", json={"model": "small", "message": "first"}) as r:
        events = _sse_events(r)
    assert events[-1]["type"] == "done"
    assert _count(client.db, "messages") == 2
    assert _count(client.db, "conversations") == 1

    # the first job spent more than 0.001 credits -> the session is now over cap
    r = client.post("/api/chat", json={"model": "small", "message": "second"})
    assert r.status_code == 429
    assert r.json()["detail"]["limit"] == "session"
    # nothing about the rejected send was written
    assert _count(client.db, "messages") == 2
    assert _count(client.db, "conversations") == 1
    states = [row[0] for row in client.db._conn.execute("SELECT state FROM jobs ORDER BY queued_at")]
    assert states == ["done", "limit_exceeded"]


TOOLS = [{"type": "function", "function": {
    "name": "read_file",
    "description": "Read a file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
}}]

TOOL_CALLS = [{"index": 0, "id": "call_1", "type": "function",
               "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}]


def test_v1_forwards_tools_and_unknown_fields(client):
    """The regression this endpoint was rewritten for: a modelled request body
    silently ate `tools`, so llama.cpp never rendered a tool section and models
    described the call in prose instead of making it."""
    key = client.post("/api/keys", json={"label": "t"}).json()["key"]
    client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, json={
        "model": "small",
        "messages": [{"role": "user", "content": "read a.py"}],
        "tools": TOOLS,
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "response_format": {"type": "text"},
    })

    sent = client.fake_upstream.seen_payload
    assert sent["tools"] == TOOLS
    assert sent["tool_choice"] == "auto"
    assert sent["parallel_tool_calls"] is False
    assert sent["response_format"] == {"type": "text"}
    assert sent["model"] == "small"          # the queue sets this from job.model_id


def test_v1_accepts_tool_result_turns(client):
    """An assistant turn carrying tool_calls has `content: null`, and the tool
    result that follows carries `tool_call_id`. Both used to be rejected."""
    key = client.post("/api/keys", json={"label": "t"}).json()["key"]
    messages = [
        {"role": "user", "content": "read a.py"},
        {"role": "assistant", "content": None, "tool_calls": TOOL_CALLS},
        {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "x = 1"},
    ]
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"model": "small", "messages": messages})
    assert r.status_code == 200
    assert client.fake_upstream.seen_payload["messages"] == messages


def test_v1_streams_tool_calls_verbatim(client):
    """Upstream's bytes reach the client unaltered, tool-call deltas included,
    and the job row still gets the real usage numbers off the same stream."""
    client.fake_upstream.tool_calls = TOOL_CALLS
    key = client.post("/api/keys", json={"label": "t"}).json()["key"]
    with client.stream("POST", "/v1/chat/completions",
                       headers={"Authorization": f"Bearer {key}"},
                       json={"model": "small", "stream": True,
                             "messages": [{"role": "user", "content": "read a.py"}],
                             "tools": TOOLS}) as r:
        assert r.status_code == 200
        raw = r.read().decode()

    chunks = [json.loads(ln[5:]) for ln in raw.splitlines()
              if ln.startswith("data:") and ln[5:].strip() != "[DONE]"]
    assert raw.endswith("data: [DONE]\n\n")

    calls = [c for c in chunks if c["choices"] and c["choices"][0]["delta"].get("tool_calls")]
    assert len(calls) == 1
    assert calls[0]["choices"][0]["delta"]["tool_calls"] == TOOL_CALLS
    assert calls[0]["choices"][0]["finish_reason"] == "tool_calls"

    # include_usage is forced on so credits come off measured tokens, not an estimate
    assert client.fake_upstream.seen_payload["stream_options"]["include_usage"] is True
    row = client.db._conn.execute(
        "SELECT completion_tokens, usage_estimated, state FROM jobs").fetchone()
    assert row[0] == 3 and row[1] == 0 and row[2] == "done"


def test_v1_clamps_max_tokens_and_applies_sampling(client):
    """The two things the passthrough is allowed to change about the body."""
    client.fake_upstream.script["small"] = ["ok"]
    reg = client.registry
    reg._models["small"] = replace(reg.require("small"), sampling={"temperature": 0.25})
    key = client.post("/api/keys", json={"label": "t"}).json()["key"]
    client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                json={"model": "small", "max_tokens": 999999, "temperature": 1.9,
                      "messages": [{"role": "user", "content": "hi"}]})

    sent = client.fake_upstream.seen_payload
    assert sent["max_tokens"] == get_settings().max_tokens_per_request
    assert sent["temperature"] == 0.25       # inventory sampling wins over the client


def test_v1_rejects_bad_requests(client):
    key = client.post("/api/keys", json={"label": "t"}).json()["key"]
    h = {"Authorization": f"Bearer {key}"}
    assert client.post("/v1/chat/completions", headers=h,
                       json={"model": "nope", "messages": [{"role": "user", "content": "x"}]}
                       ).status_code == 400
    assert client.post("/v1/chat/completions", headers=h,
                       json={"model": "small", "messages": []}).status_code == 400
    assert client.post("/v1/chat/completions", headers=h,
                       content=b"not json").status_code == 400


def test_v1_keepalive_is_an_ignorable_comment(client, monkeypatch):
    """A queued job produces no bytes until its turn, and a cold start can be
    25s of silence. The keepalive that holds the connection open must be an SSE
    comment, which every conformant parser drops, and not something the client
    has to understand."""
    monkeypatch.setattr(appmod, "_V1_KEEPALIVE_S", 0.01)
    client.fake_upstream.delay = 0.05
    key = client.post("/api/keys", json={"label": "t"}).json()["key"]
    with client.stream("POST", "/v1/chat/completions",
                       headers={"Authorization": f"Bearer {key}"},
                       json={"model": "small", "stream": True,
                             "messages": [{"role": "user", "content": "hi"}]}) as r:
        raw = r.read().decode()

    assert ": llamacracy queued\n\n" in raw
    # strip comments the way an SSE parser does; the payload is untouched
    data = [ln for ln in raw.splitlines() if ln.startswith("data:")]
    assert data[-1] == "data: [DONE]"
    text = "".join(json.loads(ln[5:])["choices"][0]["delta"].get("content", "")
                   for ln in data[:-1] if json.loads(ln[5:])["choices"])
    assert text == "hello world"


def test_appearance_prefs_round_trip(client):
    me = client.get("/api/me").json()
    assert me["prefs"]["appearance"]["preset"] == "llamacracy"     # default for a new user

    look = {"preset": "custom", "bg": "#1c1216", "surface": "#2a1b22",
            "text": "#F4E4EB", "accent": "#f59ac0", "chat_text": "lg"}
    r = client.put("/api/me/prefs", json={"appearance": look})
    assert r.status_code == 200
    assert client.get("/api/me").json()["prefs"]["appearance"] == look


@pytest.mark.parametrize("bad", [
    {"appearance": {"preset": "custom", "bg": "red"}},
    {"appearance": {"preset": "custom", "bg": "#fff"}},                    # shorthand isn't allowed
    {"appearance": {"preset": "custom", "text": "#000000;background:url(x)"}},
    {"appearance": {"preset": "Nope Nope"}},
    {"appearance": {"chat_text": "huge"}},
    {"appearance": {"font": "Comic Sans"}},                                # unknown field
])
def test_appearance_rejects_anything_but_plain_values(client, bad):
    """Colours are written into CSS custom properties, so only #rrggbb gets in."""
    assert client.put("/api/me/prefs", json=bad).status_code == 422
    assert client.get("/api/me").json()["prefs"]["appearance"]["preset"] == "llamacracy"


def _chat(client, **body) -> list[dict]:
    with client.stream("POST", "/api/chat", json={"message": "hi", **body}) as r:
        assert r.status_code == 200
        return _sse_events(r)


def test_thinking_off_by_default_and_on_when_asked(client):
    up = client.fake_upstream
    _chat(client, model="thinker")
    assert up.seen_payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert up.seen_payload["max_tokens"] == get_settings().max_tokens_per_request

    up.thinking["thinker"] = ["Seventeen", " times twenty-three..."]
    events = _chat(client, model="thinker", think=True)
    assert up.seen_payload["chat_template_kwargs"] == {"enable_thinking": True}
    # reasoning and answer share one budget, so a thinking turn gets the big one
    assert up.seen_payload["max_tokens"] == get_settings().thinking_max_tokens

    types = [e["type"] for e in events]
    assert types.index("reasoning") < types.index("thought") < types.index("token")
    assert events[-1]["thinking_seconds"] is not None

    conv = client.get(f"/api/conversations/{events[0]['conversation_id']}").json()
    reply = conv["messages"][-1]
    assert reply["reasoning"] == "Seventeen times twenty-three..."
    assert reply["thinking_seconds"] >= 0
    assert reply["content"] == "hello world"


def test_think_is_ignored_for_models_that_cannot(client):
    _chat(client, model="small", think=True)
    sent = client.fake_upstream.seen_payload
    assert "chat_template_kwargs" not in sent
    assert sent["max_tokens"] == get_settings().max_tokens_per_request


def test_reasoning_is_not_resent_as_history(client):
    events = _chat(client, model="thinker", think=True)
    _chat(client, model="thinker", conversation_id=events[0]["conversation_id"])
    history = client.fake_upstream.seen_payload["messages"]
    assert [m["role"] for m in history] == ["user", "assistant", "user"]
    assert history[1]["content"] == "hello world"          # the answer only, no thinking


def test_a_reply_that_only_thought_is_still_saved(client):
    """If thinking uses the whole budget there's no answer, but the user paid
    for the reasoning and should be able to read it."""
    client.fake_upstream.script["thinker"] = []
    events = _chat(client, model="thinker", think=True)
    conv = client.get(f"/api/conversations/{events[0]['conversation_id']}").json()
    assert conv["messages"][-1]["role"] == "assistant"
    assert conv["messages"][-1]["content"] == ""
    assert conv["messages"][-1]["reasoning"] == "Let me think."


def test_v1_thinking_defaults_off_and_client_can_turn_it_on(client):
    key = client.post("/api/keys", json={"label": "t"}).json()["key"]
    h = {"Authorization": f"Bearer {key}"}
    msgs = [{"role": "user", "content": "hi"}]

    client.post("/v1/chat/completions", headers=h, json={"model": "thinker", "messages": msgs})
    # nothing said -> the queue's safety net switches thinking off
    assert client.fake_upstream.seen_payload["chat_template_kwargs"] == {"enable_thinking": False}

    client.post("/v1/chat/completions", headers=h, json={
        "model": "thinker", "messages": msgs, "max_tokens": 999999,
        "chat_template_kwargs": {"enable_thinking": True}})
    sent = client.fake_upstream.seen_payload
    assert sent["chat_template_kwargs"] == {"enable_thinking": True}
    assert sent["max_tokens"] == get_settings().thinking_max_tokens
