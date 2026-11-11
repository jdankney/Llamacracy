# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTTP-level smoke tests against the real FastAPI app with a scripted
upstream: the SSE chat contract, the OpenAI-compatible endpoint, and the
enqueue-time limit gate leaving no orphan rows behind."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import llamacracy.app as appmod
import llamacracy.queue as queuemod
from llamacracy.config import get_settings
from llamacracy.db import Database, get_db
from llamacracy.queue import ANSWER_ROOM_TOKENS, FINAL_NUDGE
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
def client(tmp_db_path, tmp_path, monkeypatch, request):
    """A TestClient whose app talks to a temp SQLite file, a scripted upstream
    and an in-code registry. `pytest.mark.env(...)` on the test sets extra
    environment (e.g. a tiny credit cap) before settings are built."""
    monkeypatch.setenv("DEV_MODE", "1")
    monkeypatch.setenv("DEV_EMAIL", "dev@localhost")
    monkeypatch.setenv("ADMIN_EMAILS", "dev@localhost")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    # no run_python unless a test brings its own sandbox: whether this machine
    # has one built must not change what the other tests see
    monkeypatch.setenv("SANDBOX_PYTHON", str(tmp_path / "no-sandbox" / "python"))
    # chats keep their stand-in title unless a test turns naming on: the extra
    # request would otherwise be the last payload every other test inspects
    monkeypatch.setenv("SMART_TITLES", "0")
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


def test_v1_clamps_max_tokens_and_fills_in_sampling(client):
    """The two things the passthrough is allowed to change about the body."""
    client.fake_upstream.script["small"] = ["ok"]
    reg = client.registry
    reg._models["small"] = replace(reg.require("small"),
                                   sampling={"temperature": 0.25, "top_p": 0.9})
    key = client.post("/api/keys", json={"label": "t"}).json()["key"]
    client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                json={"model": "small", "max_tokens": 999999, "temperature": 0.1,
                      "messages": [{"role": "user", "content": "hi"}]})

    sent = client.fake_upstream.seen_payload
    assert sent["max_tokens"] == get_settings().api_max_tokens_per_request
    assert sent["temperature"] == 0.1        # what the client sent wins...
    assert sent["top_p"] == 0.9              # ...the inventory fills in the rest


def test_v1_routes_images_to_the_vision_entry(client):
    """An image_url part anywhere in the history needs the -vision entry: the
    text-only one has no --mmproj. A model without one refuses up front."""
    reg = client.registry
    reg._models["small-vision"] = replace(reg.require("small"), key="small-vision",
                                          in_picker=False)
    key = client.post("/api/keys", json={"label": "t"}).json()["key"]
    h = {"Authorization": f"Bearer {key}"}
    image_turn = {"role": "user", "content": [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}
    body = {"model": "small", "messages": [image_turn,
                                           {"role": "assistant", "content": "a cat"},
                                           {"role": "user", "content": "sure?"}]}

    assert client.post("/v1/chat/completions", headers=h, json=body).status_code == 400

    reg._models["small"] = replace(reg.require("small"), vision_key="small-vision")
    assert client.post("/v1/chat/completions", headers=h, json=body).status_code == 200
    assert client.fake_upstream.seen_payload["model"] == "small-vision"

    client.post("/v1/chat/completions", headers=h, json={
        "model": "small", "messages": [{"role": "user", "content": "text only"}]})
    assert client.fake_upstream.seen_payload["model"] == "small"


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


def test_the_profile_reaches_the_model_while_it_is_on(client):
    up = client.fake_upstream
    me = client.get("/api/me").json()["prefs"]["profile"]
    assert me == {"enabled": True, "name": "", "about": ""}     # on, but nothing to say yet
    _chat(client, model="small")
    assert up.seen_payload["messages"][0]["role"] == "user"     # so no system message

    prof = {"enabled": True, "name": "  Sam ", "about": "Second-year student. Likes birds."}
    r = client.put("/api/me/prefs", json={"profile": prof})
    assert r.json()["prefs"]["profile"]["name"] == "Sam"
    _chat(client, model="small")
    first = up.seen_payload["messages"][0]
    assert first["role"] == "system"
    assert "Name: Sam" in first["content"] and "Likes birds." in first["content"]

    # saving the appearance alone leaves the profile be
    client.put("/api/me/prefs", json={"appearance": {"preset": "llamacracy", "chat_text": "lg"}})
    assert client.get("/api/me").json()["prefs"]["profile"]["about"] == prof["about"]

    # off: kept, but not sent
    client.put("/api/me/prefs", json={"profile": {**prof, "enabled": False}})
    _chat(client, model="small")
    assert all("Likes birds" not in str(m["content"]) for m in up.seen_payload["messages"])
    assert client.get("/api/me").json()["prefs"]["profile"]["about"] == prof["about"]


def test_the_profile_shares_the_one_system_message(agent_client):
    up = agent_client.fake_upstream
    up.agent_steps = [{"content": ["Hi Sam."]}]
    agent_client.put("/api/me/prefs", json={"profile": {"name": "Sam", "about": ""}})
    _chat(agent_client, model="small", search=True)
    sys = [m for m in up.payloads[0]["messages"] if m["role"] == "system"]
    assert len(sys) == 1 and "Name: Sam" in sys[0]["content"]


def test_the_profile_has_limits(client):
    assert client.put("/api/me/prefs", json={"profile": {"about": "x" * 2001}}).status_code == 422
    assert client.put("/api/me/prefs", json={"profile": {"name": "x" * 61}}).status_code == 422
    assert client.put("/api/me/prefs", json={"profile": {"mood": "great"}}).status_code == 422


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


def _conv(client, conv_id) -> dict:
    return client.get(f"/api/conversations/{conv_id}").json()


def _texts(conv) -> list[str]:
    return [m["content"] for m in conv["messages"]]


def test_editing_a_message_forks_the_conversation(client):
    up = client.fake_upstream
    first = _chat(client, model="small", message="one")
    cid = first[0]["conversation_id"]
    up.script["small"] = ["reply two"]
    _chat(client, model="small", message="two", conversation_id=cid)
    before = _conv(client, cid)
    two = before["messages"][2]
    assert _texts(before) == ["one", "hello world", "two", "reply two"]
    assert two["siblings"] == [two["id"]]

    # edit "two": the model sees the branch up to its parent, never the original
    up.script["small"] = ["reply TWO"]
    events = _chat(client, model="small", message="TWO", conversation_id=cid, edit_of=two["id"])
    assert [m["content"] for m in up.seen_payload["messages"]] == ["one", "hello world", "TWO"]
    after = _conv(client, cid)
    assert _texts(after) == ["one", "hello world", "TWO", "reply TWO"]
    edited = after["messages"][2]
    assert edited["id"] == events[0]["message_id"]
    assert edited["siblings"] == [two["id"], edited["id"]]         # "2 / 2"

    # flip back: the original message AND its own reply come back
    back = client.post(f"/api/conversations/{cid}/switch", json={"message_id": two["id"]}).json()
    assert _texts(back) == ["one", "hello world", "two", "reply two"]
    assert _texts(_conv(client, cid)) == ["one", "hello world", "two", "reply two"]   # remembered

    # and carrying on continues whichever branch is showing
    _chat(client, model="small", message="three", conversation_id=cid)
    assert [m["content"] for m in up.seen_payload["messages"]] == [
        "one", "hello world", "two", "reply two", "three"]


def test_editing_the_first_message(client):
    cid = _chat(client, model="small", message="hi")[0]["conversation_id"]
    root = _conv(client, cid)["messages"][0]
    _chat(client, model="small", message="hello instead", conversation_id=cid, edit_of=root["id"])
    assert client.fake_upstream.seen_payload["messages"] == [
        {"role": "user", "content": "hello instead"}]
    conv = _conv(client, cid)
    assert conv["messages"][0]["content"] == "hello instead"
    assert conv["messages"][0]["siblings"][0] == root["id"]
    back = client.post(f"/api/conversations/{cid}/switch", json={"message_id": root["id"]}).json()
    assert _texts(back) == ["hi", "hello world"]


def test_edit_and_switch_refuse_what_they_should(client):
    cid = _chat(client, model="small")[0]["conversation_id"]
    reply = _conv(client, cid)["messages"][1]
    r = client.post("/api/chat", json={"model": "small", "message": "x",
                                       "conversation_id": cid, "edit_of": reply["id"]})
    assert r.status_code == 404                       # only your own messages can be edited
    r = client.post("/api/chat", json={"model": "small", "message": "x", "edit_of": reply["id"]})
    assert r.status_code == 400                       # an edit needs its conversation
    other = _chat(client, model="small")[0]["conversation_id"]
    other_msg = _conv(client, other)["messages"][0]["id"]
    r = client.post(f"/api/conversations/{cid}/switch", json={"message_id": other_msg})
    assert r.status_code == 404                       # not a message of this conversation


@pytest.mark.env(COMPACT_KEEP_RECENT="2")
def test_compaction_belongs_to_its_branch(client):
    up = client.fake_upstream
    cid = _chat(client, model="small", message="m1")[0]["conversation_id"]
    for m in ("m2", "m3"):
        _chat(client, model="small", message=m, conversation_id=cid)
    msgs = _conv(client, cid)["messages"]              # m1 r m2 r m3 r

    up.script["small"] = ["SUMMARY"]
    r = client.post(f"/api/conversations/{cid}/compact").json()
    assert r["compact_boundary_id"] == msgs[3]["id"]   # everything but the last 2
    up.script["small"] = ["ok"]
    _chat(client, model="small", message="m4", conversation_id=cid)
    sent = up.seen_payload["messages"]
    assert sent[0]["role"] == "system" and "SUMMARY" in sent[0]["content"]
    assert [m["content"] for m in sent[1:]] == ["m3", "hello world", "m4"]

    # a fork from before the cut never gets a summary of messages it doesn't have
    _chat(client, model="small", message="m2 edited", conversation_id=cid, edit_of=msgs[2]["id"])
    assert [m["content"] for m in up.seen_payload["messages"]] == ["m1", "hello world", "m2 edited"]
    assert _conv(client, cid)["conversation"]["context_summary"] is None

    # ...and switching back brings the original branch's summary with it
    back = client.post(f"/api/conversations/{cid}/switch", json={"message_id": msgs[2]["id"]}).json()
    assert back["conversation"]["context_summary"] == "SUMMARY"


# --------------------------------------------------------------------------- #
# documents
# --------------------------------------------------------------------------- #
def _upload_doc(client, name: str, data: bytes) -> dict:
    r = client.post("/api/uploads", files={"file": (name, data, "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


def test_document_upload_describes_itself(client):
    from tests import docfixtures as fx
    d = _upload_doc(client, "paper.pdf", fx.pdf(["Hello page one", "Page two"]))
    assert d["kind"] == "document" and d["filename"] == "paper.pdf"
    assert (d["units"], d["unit"]) == (2, "page") and d["tokens"] > 0

    text = client.get(f"/api/uploads/{d['id']}/text").json()
    assert "[Page 1]\n\nHello page one" in text["text"]
    # the original always downloads, never renders in the page
    r = client.get(f"/api/uploads/{d['id']}")
    assert r.headers["content-type"] == "application/octet-stream"
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_every_format_uploads(client):
    from tests import docfixtures as fx
    for name, data in (("a.docx", fx.docx()), ("a.xlsx", fx.xlsx()), ("a.odt", fx.odt()),
                       ("a.ods", fx.ods()), ("a.pptx", fx.pptx()), ("a.odp", fx.odp())):
        r = client.post("/api/uploads", files={"file": (name, data, "application/octet-stream")})
        assert r.status_code == 200, (name, r.text)


def test_document_uploads_refuse_with_a_reason(client):
    from tests import docfixtures as fx
    r = client.post("/api/uploads", files={"file": ("old.doc", b"\xd0\xcf\x11\xe0junk", "application/msword")})
    assert r.status_code == 400 and "Save it as PDF" in r.json()["detail"]
    r = client.post("/api/uploads", files={"file": ("scan.pdf", fx.blank_pdf(4), "application/pdf")})
    assert r.status_code == 400 and "looks like a scan" in r.json()["detail"]
    r = client.post("/api/uploads", files={"file": ("x.gif", b"GIF89a", "image/gif")})
    assert r.status_code == 400 and "unsupported image type" in r.json()["detail"]


def test_documents_ride_along_with_their_message(client):
    """Sent ahead of the message that brought them, and again on every later
    turn of the branch: a follow-up about the document still has it."""
    d = _upload_doc(client, "notes.md", b"The launch code is PLUM-42.")
    ev = _chat(client, model="small", message="What's the code?", doc_ids=[d["id"]])
    conv = ev[0]["conversation_id"]
    first = client.fake_upstream.seen_payload["messages"][-1]["content"]
    assert first.startswith('<document name="notes.md">\nThe launch code is PLUM-42.\n</document>')
    assert first.endswith("What's the code?")

    _chat(client, model="small", message="And again?", conversation_id=conv)
    sent = client.fake_upstream.seen_payload["messages"]
    assert "PLUM-42" in sent[0]["content"] and sent[-1]["content"] == "And again?"

    detail = client.get(f"/api/conversations/{conv}").json()
    user = detail["messages"][0]
    assert user["content"] == "What's the code?"        # stored clean
    assert [x["filename"] for x in user["documents"]] == ["notes.md"]
    assert "doc_ids_json" not in user


def test_editing_keeps_the_documents(client):
    d = _upload_doc(client, "a.txt", b"alpha")
    ev = _chat(client, model="small", message="first", doc_ids=[d["id"]])
    conv, msg = ev[0]["conversation_id"], ev[0]["message_id"]
    _chat(client, model="small", message="edited", conversation_id=conv, edit_of=msg)
    assert "alpha" in client.fake_upstream.seen_payload["messages"][-1]["content"]


def test_document_too_big_for_the_model_is_refused_up_front(client):
    reg = client.registry
    reg._models["small"] = replace(reg.require("small"), ctx=2048)
    d = _upload_doc(client, "big.txt", b"lorem ipsum " * 2000)
    r = client.post("/api/chat", json={"model": "small", "message": "sum up", "doc_ids": [d["id"]]})
    assert r.status_code == 400 and "can hold at once" in r.json()["detail"]
    assert _count(client.db, "messages") == 0 and _count(client.db, "jobs") == 0


def test_unknown_or_foreign_documents_are_refused(client):
    r = client.post("/api/chat", json={"model": "small", "message": "x", "doc_ids": ["nope"]})
    assert r.status_code == 404
    ids = [_upload_doc(client, f"{i}.txt", b"x")["id"] for i in range(6)]
    r = client.post("/api/chat", json={"model": "small", "message": "x", "doc_ids": ids})
    assert r.status_code == 400


def test_deleting_a_conversation_removes_its_documents(client):
    from pathlib import Path
    d = _upload_doc(client, "a.txt", b"alpha")
    row = client.db._conn.execute("SELECT path, text_path FROM uploads WHERE id = ?", (d["id"],)).fetchone()
    conv = _chat(client, model="small", message="hi", doc_ids=[d["id"]])[0]["conversation_id"]
    assert client.delete(f"/api/conversations/{conv}").status_code == 200
    assert _count(client.db, "uploads") == 0
    assert not Path(row["path"]).exists() and not Path(row["text_path"]).exists()


def test_compaction_names_documents_instead_of_resending_them(client):
    d = _upload_doc(client, "plan.txt", b"secret plan body")
    conv = _chat(client, model="small", message="one", doc_ids=[d["id"]])[0]["conversation_id"]
    for m in ("two", "three", "four", "five"):
        _chat(client, model="small", message=m, conversation_id=conv)
    r = client.post(f"/api/conversations/{conv}/compact")
    assert r.status_code == 200, r.text
    prompt = client.fake_upstream.seen_payload["messages"][-1]["content"]
    assert "[attached: plan.txt] one" in prompt
    assert "secret plan body" not in prompt


# --------------------------------------------------------------------------- #
# agent turns: a tool-capable model with Search on
# --------------------------------------------------------------------------- #
class FakeSearch:
    def __init__(self):
        self.queries: list[str] = []

    async def query(self, q, count):
        from llamacracy.search import SearchOutcome, SearchResult
        self.queries.append(q)
        return SearchOutcome(q, ok=True, results=[
            SearchResult(f"{q} guide", f"https://example.org/{q.replace(' ', '-')}", f"all about {q}"),
            SearchResult("Shared page", "https://example.org/shared", "on every search")])

    async def search(self, message, count):
        return await self.query(message, count)

    async def aclose(self):
        pass


@pytest.fixture
def agent_client(client, monkeypatch):
    reg = client.registry
    reg._models["small"] = replace(reg.require("small"), tools=True)
    client.search = FakeSearch()
    monkeypatch.setattr(appmod, "get_search", lambda: client.search)
    return client


def test_agent_searches_then_answers_with_citations(agent_client):
    c, up = agent_client, agent_client.fake_upstream
    up.agent_steps = [
        {"content": ["Search it.</think>", "Let me check."],
         "tool_calls": [("web_search", {"query": "llama release"})]},
        {"content": ["It shipped in May [1]."]},
    ]
    ev = _chat(c, model="small", message="When did it ship?", search=True)
    types = [e["type"] for e in ev]
    assert types.index("tool_start") < types.index("tool_end") < types.index("done")
    start = next(e for e in ev if e["type"] == "tool_start")
    assert start["label"] == "Searching “llama release”" and start["said"] == "Let me check."
    end = next(e for e in ev if e["type"] == "tool_end")
    assert end["summary"] == "2 results" and [s["n"] for s in end["sources"]] == [1, 2]

    assert c.search.queries == ["llama release"]       # the model's words, not the message
    first, second = up.payloads
    assert first["messages"][0]["role"] == "system" and "Today is" in first["messages"][0]["content"]
    assert [t["function"]["name"] for t in first["tools"]] == ["web_search", "read_page"]
    call, result = second["messages"][-2:]
    assert call["tool_calls"][0]["function"]["arguments"] == '{"query": "llama release"}'
    assert result["role"] == "tool" and "[1] llama release guide" in result["content"]

    conv = ev[0]["conversation_id"]
    reply = client_messages(c, conv)[1]
    assert reply["content"] == "It shipped in May [1]."     # the preamble isn't the answer
    tools = json.loads(reply["tools_json"])
    assert tools["steps"][0] == {"type": "note", "text": "Let me check."}
    assert tools["steps"][1]["label"] == "Searching “llama release”"
    assert tools["sources"][0]["url"] == "https://example.org/llama-release"
    # one queued job for the whole turn, and no injected search on the message
    assert _count(c.db, "jobs") == 1
    assert client_messages(c, conv)[0]["search_json"] is None


def test_agent_numbers_sources_across_searches(agent_client):
    up = agent_client.fake_upstream
    up.agent_steps = [
        {"tool_calls": [("web_search", {"query": "a"}), ("web_search", {"query": "b"})]},
        {"content": ["done"]},
    ]
    ev = _chat(agent_client, model="small", message="q", search=True)
    ends = [e for e in ev if e["type"] == "tool_end"]
    # the shared page keeps [2] the second time and isn't listed as new
    assert [s["n"] for s in ends[0]["sources"]] == [1, 2]
    assert [s["n"] for s in ends[1]["sources"]] == [3]
    assert "[2] Shared page" in up.payloads[1]["messages"][-1]["content"]


def test_agent_must_answer_after_max_steps(agent_client):
    up = agent_client.fake_upstream
    loop = {"content": ["hm"], "tool_calls": [("web_search", {"query": "again"})]}
    up.agent_steps = [dict(loop) for _ in range(20)]
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("AGENT_MAX_STEPS", "2")
        get_settings.cache_clear()
        ev = _chat(agent_client, model="small", message="q", search=True)
    assert [e["type"] for e in ev].count("tool_start") == 2
    assert up.payloads[-1]["tool_choice"] == "none"      # the last request forbids tools
    # ...and says so, since some templates let a model write a call out as text
    assert up.payloads[-1]["messages"][-1] == {"role": "user", "content": FINAL_NUDGE}
    # the step before the last round is told it's the last round
    assert "one more round" in up.payloads[1]["messages"][-1]["content"]
    assert "one more round" not in up.payloads[0]["messages"][-1]["content"]
    assert ev[-1]["type"] == "done" and ev[-1]["state"] == "done"


def test_agent_answers_when_thinking_eats_its_budget(agent_client):
    reg = agent_client.registry
    reg._models["small"] = replace(reg.require("small"), thinking=True)
    up = agent_client.fake_upstream
    up.agent_steps = [
        {"reasoning": ["hmm"] * 5, "finish": "length"},     # thought until cut off
        {"content": ["the answer"]},
    ]
    ev = _chat(agent_client, model="small", message="q", search=True, think=True)
    first, last = up.payloads
    budget = get_settings().thinking_max_tokens
    # a thinking step leaves an answer's worth of the budget unspent...
    assert first["max_tokens"] == budget - min(ANSWER_ROOM_TOKENS, budget // 4)
    # ...and the answer comes from one more request with thinking off
    assert last["tool_choice"] == "none"
    assert last["chat_template_kwargs"]["enable_thinking"] is False
    assert last["messages"][-1]["content"] == FINAL_NUDGE
    assert ev[-1]["state"] == "done"
    assert client_messages(agent_client, ev[0]["conversation_id"])[1]["content"] == "the answer"


def test_agent_survives_bad_arguments_and_unknown_tools(agent_client):
    up = agent_client.fake_upstream
    up.agent_steps = [
        {"tool_calls": [("web_search", "{not json"), ("teleport", {})]},
        {"content": ["ok"]},
    ]
    ev = _chat(agent_client, model="small", message="q", search=True)
    ends = [e for e in ev if e["type"] == "tool_end"]
    assert [e["ok"] for e in ends] == [False, False]
    results = [m["content"] for m in up.payloads[1]["messages"] if m["role"] == "tool"]
    assert "weren't valid JSON" in results[0] and "no tool called 'teleport'" in results[1]
    # llama.cpp re-parses echoed arguments and 500s on bad JSON, so they go back as {}
    calls = next(m for m in up.payloads[1]["messages"] if m.get("tool_calls"))["tool_calls"]
    assert [c["function"]["arguments"] for c in calls] == ["{}", "{}"]
    assert ev[-1]["state"] == "done"


def test_a_raw_newline_in_code_arguments_is_accepted(compute_client):
    # smaller models put real newlines inside the JSON string; that parses
    # here, and goes back to llama.cpp as valid JSON
    up = compute_client.fake_upstream
    up.agent_steps = [{"tool_calls": [("run_python", '{"code": "x = 2\nprint(x * 21)"}')]},
                      {"content": ["It's 42."]}]
    ev = _chat(compute_client, model="small", message="q")
    assert compute_client.ran == ["x = 2\nprint(x * 21)"]
    assert next(e for e in ev if e["type"] == "tool_end")["ok"]
    call = next(m for m in up.payloads[1]["messages"] if m.get("tool_calls"))["tool_calls"][0]
    assert json.loads(call["function"]["arguments"]) == {"code": "x = 2\nprint(x * 21)"}


def test_agent_thinking_time_adds_up_across_steps(agent_client):
    reg = agent_client.registry
    reg._models["small"] = replace(reg.require("small"), thinking=True)
    up = agent_client.fake_upstream
    up.agent_steps = [
        {"reasoning": ["need data"], "tool_calls": [("web_search", {"query": "x"})]},
        {"reasoning": ["now I know"], "content": ["answer"]},
    ]
    ev = _chat(agent_client, model="small", message="q", search=True, think=True)
    assert [e["type"] for e in ev].count("thought") == 2
    reply = client_messages(agent_client, ev[0]["conversation_id"])[1]
    assert reply["reasoning"] == "need datanow I know"
    # the model's first step's reasoning goes back with its tool call
    assert up.payloads[1]["messages"][-2]["reasoning_content"] == "need data"


def test_search_on_a_plain_model_still_injects_one_search(agent_client):
    reg = agent_client.registry
    reg._models["small"] = replace(reg.require("small"), tools=False)
    ev = _chat(agent_client, model="small", message="what is x", search=True)
    assert "tool_start" not in [e["type"] for e in ev]
    assert "tools" not in agent_client.fake_upstream.seen_payload
    assert "Web search results" in agent_client.fake_upstream.seen_payload["messages"][-1]["content"]


def client_messages(c, conv_id) -> list[dict]:
    rows = c.db._conn.execute(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (conv_id,)).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# reading: web pages and long documents
# --------------------------------------------------------------------------- #
def test_agent_reads_a_page_in_parts(agent_client, monkeypatch):
    import llamacracy.fetch as fetchmod
    body = "<html><title>Big page</title><main>" + "".join(
        f"<p>Paragraph {i} " + "word " * 40 + "</p>" for i in range(200)) + "</main></html>"
    fetched = []

    async def fake_fetch(url, *, max_bytes, **kw):
        fetched.append(url)
        return fetchmod.Fetched(url, "text/html", "utf-8", body.encode(), False)
    monkeypatch.setattr(fetchmod, "fetch", fake_fetch)
    up = agent_client.fake_upstream
    url = "https://example.org/big"
    up.agent_steps = [
        {"tool_calls": [("read_page", {"url": url})]},
        {"tool_calls": [("read_page", {"url": url, "start": 9000})]},
        {"content": ["Summary [1]."]},
    ]
    ev = _chat(agent_client, model="small", message="summarise it", search=True)
    ends = [e for e in ev if e["type"] == "tool_end"]
    assert [e["summary"] for e in ends][0].startswith("part 1 of ")
    assert [s["n"] for s in ends[0]["sources"]] == [1] and ends[1]["sources"] == []
    assert fetched == [url]                          # fetched once, read twice
    first_part = up.payloads[1]["messages"][-1]["content"]
    assert first_part.startswith("[1] Big page\nhttps://example.org/big") and "Paragraph 0 " in first_part
    assert "continues: read_page with url" in first_part
    assert ends[0]["label"] == "Reading example.org/big"


def test_big_document_becomes_a_lookup_for_a_tools_model(agent_client):
    """No Search needed: a document too big to send whole is outlined,
    searched and read through tools instead of refusing the send."""
    reg = agent_client.registry
    reg._models["small"] = replace(reg.require("small"), ctx=4096)
    pages = "\n\n".join(f"[Page {i}]\n\n" + ("filler text " * 60) for i in range(1, 30))
    pages = pages.replace("[Page 17]\n\n", "[Page 17]\n\nThe termination fee is 900 dollars. ")
    d = _upload_doc(agent_client, "lease.txt", pages.encode())
    up = agent_client.fake_upstream
    up.agent_steps = [
        {"tool_calls": [("doc_search", {"ref": "d1", "query": "termination fee"})]},
        {"tool_calls": [("doc_read", {"ref": "d1", "page": 17})]},
        {"content": ["It's 900 dollars (page 17)."]},
    ]
    ev = _chat(agent_client, model="small", message="What's the fee?", doc_ids=[d["id"]])
    first = up.payloads[0]
    assert {t["function"]["name"] for t in first["tools"]} == {"doc_outline", "doc_search", "doc_read"}
    user = first["messages"][-1]["content"]
    assert user.startswith('<document ref="d1" name="lease.txt"') and "filler" not in user
    results = [m["content"] for m in up.payloads[2]["messages"] if m["role"] == "tool"]
    assert "@" in results[0] and "(page 17)" in results[0] and "termination fee" in results[0]
    assert results[1].startswith('d1 "lease.txt"') and "900 dollars" in results[1]
    labels = [e["label"] for e in ev if e["type"] == "tool_start"]
    assert labels == ["Looking in lease.txt for “termination fee”", "Reading lease.txt, page 17"]


def test_small_documents_are_still_sent_whole_to_a_tools_model(agent_client):
    d = _upload_doc(agent_client, "note.txt", b"short and sweet")
    _chat(agent_client, model="small", message="read it", doc_ids=[d["id"]])
    sent = agent_client.fake_upstream.seen_payload
    assert "tools" not in sent and "short and sweet" in sent["messages"][-1]["content"]


def test_compacted_documents_stay_reachable_for_a_tools_model(agent_client):
    d = _upload_doc(agent_client, "plan.txt", b"The secret word is marmalade.")
    conv = _chat(agent_client, model="small", message="one", doc_ids=[d["id"]])[0]["conversation_id"]
    for m in ("two", "three", "four", "five"):
        _chat(agent_client, model="small", message=m, conversation_id=conv)
    assert agent_client.post(f"/api/conversations/{conv}/compact").status_code == 200
    up = agent_client.fake_upstream
    up.agent_steps = [{"content": ["ok"]}]
    _chat(agent_client, model="small", message="what was the word?", conversation_id=conv)
    system = up.payloads[-1]["messages"][0]["content"]
    assert "Documents shared earlier" in system and 'ref="d1" name="plan.txt"' in system
    assert "marmalade" not in json.dumps(up.payloads[-1]["messages"])


# --------------------------------------------------------------------------- #
# workspace files
# --------------------------------------------------------------------------- #
def test_model_writes_and_edits_files(agent_client):
    up = agent_client.fake_upstream
    up.agent_steps = [
        {"tool_calls": [("ws_write", {"path": "plan.md", "content": "# Plan\n\n- one\n- two\n"})]},
        {"tool_calls": [("ws_edit", {"path": "plan.md", "old_text": "- two", "new_text": "- two\n- three"})]},
        {"content": ["Saved plan.md."]},
    ]
    ev = _chat(agent_client, model="small", message="make a plan", files=True)
    ends = [e for e in ev if e["type"] == "tool_end"]
    assert [e["summary"] for e in ends] == ["created, 20 chars", "+1 −0 lines"]
    assert ends[0]["label"] == "Writing plan.md" and ends[1]["label"] == "Editing plan.md"
    # the reply's file list sums the turn up: created, whatever happened after
    assert ev[-1]["tools"]["files"] == [{"path": "plan.md", "id": 2, "chars": 28,
                                         "created": True, "added": 5, "removed": 0}]
    conv = ev[0]["conversation_id"]
    files = agent_client.get(f"/api/conversations/{conv}").json()["conversation"]["files"]
    assert [(f["path"], f["chars"]) for f in files] == [("plan.md", 28)]
    got = agent_client.get(f"/api/conversations/{conv}/files/{files[0]['id']}").json()
    assert got["content"] == "# Plan\n\n- one\n- two\n- three\n"
    dl = agent_client.get(f"/api/conversations/{conv}/files/{files[0]['id']}?download=1")
    assert dl.headers["content-type"].startswith("text/plain")
    assert 'attachment; filename="plan.md"' in dl.headers["content-disposition"]


def test_files_stay_on_for_a_conversation_that_has_them(agent_client):
    up = agent_client.fake_upstream
    up.agent_steps = [{"tool_calls": [("ws_write", {"path": "a.txt", "content": "alpha"})]},
                      {"content": ["ok"]}]
    conv = _chat(agent_client, model="small", message="save", files=True)[0]["conversation_id"]
    up.agent_steps = [{"tool_calls": [("ws_read", {"path": "a.txt"})]}, {"content": ["alpha"]}]
    _chat(agent_client, model="small", message="what did you save?", conversation_id=conv)
    system = up.payloads[-2]["messages"][0]["content"]
    assert "Files now: a.txt (5 chars)." in system
    assert "alpha" in up.payloads[-1]["messages"][-1]["content"]


def test_a_fork_keeps_the_files_as_they_were(agent_client):
    up = agent_client.fake_upstream
    up.agent_steps = [{"tool_calls": [("ws_write", {"path": "a.txt", "content": "v1"})]}, {"content": ["ok"]}]
    ev = _chat(agent_client, model="small", message="first", files=True)
    conv = ev[0]["conversation_id"]
    up.agent_steps = [{"tool_calls": [("ws_write", {"path": "a.txt", "content": "v2"})]}, {"content": ["ok"]}]
    second = _chat(agent_client, model="small", message="change it", conversation_id=conv)
    files = lambda: agent_client.get(f"/api/conversations/{conv}").json()["conversation"]["files"]  # noqa: E731
    content = lambda: agent_client.get(f"/api/conversations/{conv}/files/{files()[0]['id']}").json()["content"]  # noqa: E731
    assert content() == "v2"
    # edit the second message: the new branch starts from the files as they were before it
    up.agent_steps = [{"content": ["sure"]}]
    _chat(agent_client, model="small", message="never mind", conversation_id=conv,
          edit_of=second[0]["message_id"])
    assert content() == "v1"
    # and flipping back to the original branch brings its version back
    agent_client.post(f"/api/conversations/{conv}/switch", json={"message_id": second[0]["message_id"]})
    assert content() == "v2"


def test_workspace_refuses_bad_names_and_ambiguous_edits(agent_client):
    up = agent_client.fake_upstream
    up.agent_steps = [
        {"tool_calls": [("ws_write", {"path": "../../etc/passwd", "content": "x"}),
                        ("ws_write", {"path": "a.txt", "content": "dup dup"}),
                        ("ws_edit", {"path": "a.txt", "old_text": "dup", "new_text": "x"})]},
        {"content": ["ok"]},
    ]
    ev = _chat(agent_client, model="small", message="go", files=True)
    ends = [e for e in ev if e["type"] == "tool_end"]
    assert [e["ok"] for e in ends] == [False, True, False]
    results = [m["content"] for m in up.payloads[1]["messages"] if m["role"] == "tool"]
    assert "isn't a usable file name" in results[0] and "appears 2 times" in results[2]


def test_files_toggle_does_nothing_on_a_plain_model(client):
    _chat(client, model="small", message="hi", files=True)
    assert "tools" not in client.fake_upstream.seen_payload


def test_deleting_a_conversation_deletes_its_files(agent_client):
    up = agent_client.fake_upstream
    up.agent_steps = [{"tool_calls": [("ws_write", {"path": "a.txt", "content": "x"})]}, {"content": ["ok"]}]
    conv = _chat(agent_client, model="small", message="go", files=True)[0]["conversation_id"]
    assert _count(agent_client.db, "workspace_files") == 1
    agent_client.delete(f"/api/conversations/{conv}")
    assert _count(agent_client.db, "workspace_files") == 0


# --------------------------------------------------------------------------- #
# pictures in agent turns
# --------------------------------------------------------------------------- #
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


@pytest.fixture
def vision_client(agent_client):
    reg = agent_client.registry
    reg._models["small-vision"] = replace(reg.require("small"), key="small-vision", in_picker=False)
    reg._models["small"] = replace(reg.require("small"), vision_key="small-vision")
    return agent_client


def _photo_turn(c, text="what plant is this?") -> str:
    img = c.post("/api/uploads", files={"file": ("p.png", PNG, "image/png")}).json()["id"]
    c.fake_upstream.agent_steps = [{"content": ["A fern."]}]
    return _chat(c, model="small", message=text, image_id=img)[0]["conversation_id"]


def test_model_can_look_again_at_an_earlier_photo(vision_client):
    c, up = vision_client, vision_client.fake_upstream
    conv = _photo_turn(c)
    up.agent_steps = [{"tool_calls": [("view_image", {"image": "img1"})]},
                      {"content": ["The leaves are fronds."]}]
    ev = _chat(c, model="small", message="and its leaves?", conversation_id=conv)
    first, second = up.payloads[-2:]
    assert first["model"] == "small-vision"                  # a picture turn needs the mmproj
    assert [t["function"]["name"] for t in first["tools"]] == ["view_image"]
    assert 'img1 (sent with "what plant is this?")' in first["messages"][0]["content"]
    shown = second["messages"][-1]
    assert shown["role"] == "user" and shown["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    end = next(e for e in ev if e["type"] == "tool_end")
    assert end["label"] == "Looking at img1" and end["upload_id"]


def test_search_on_a_vision_model_can_find_and_view_web_images(vision_client, monkeypatch):
    import llamacracy.fetch as fetchmod
    c, up = vision_client, vision_client.fake_upstream

    async def images(q, count):
        from llamacracy.search import ImageResult
        return True, [ImageResult("A llama", "https://example.org/llama", "https://img.example.org/l.png", "800 x 600")]
    c.search.images = images

    async def fake_fetch(url, *, max_bytes, **kw):
        body = PNG if url.endswith(".png") else b"<html>not a picture</html>"
        return fetchmod.Fetched(url, "", None, body, False)
    monkeypatch.setattr(fetchmod, "fetch", fake_fetch)
    up.agent_steps = [
        {"tool_calls": [("image_search", {"query": "llama"})]},
        {"tool_calls": [("view_image", {"image": "https://img.example.org/l.png"}),
                        ("view_image", {"image": "https://example.org/page.html"})]},
        {"content": ["Llamas are fluffy [2]."]},
    ]
    ev = _chat(c, model="small", message="what does a llama look like?", search=True)
    names = [t["function"]["name"] for t in up.payloads[0]["tools"]]
    assert up.payloads[0]["model"] == "small-vision" and {"image_search", "view_image"} <= set(names)
    assert "image: https://img.example.org/l.png" in up.payloads[1]["messages"][-1]["content"]
    ends = [e for e in ev if e["type"] == "tool_end"]
    assert [e["ok"] for e in ends] == [True, True, False] and "not a PNG" in ends[2]["summary"]
    shown = up.payloads[2]["messages"][-1]
    assert shown["role"] == "user" and len(shown["content"]) == 2      # the one real picture
    assert ev[-1]["tools"]["sources"][1]["url"] == "https://img.example.org/l.png"


@pytest.mark.env(AGENT_IMAGES="false")
def test_pictures_can_be_turned_off(vision_client):
    vision_client.fake_upstream.agent_steps = [{"content": ["ok"]}]
    _chat(vision_client, model="small", message="q", search=True)
    assert vision_client.fake_upstream.payloads[-1]["model"] == "small"


def test_a_resident_vision_entry_is_reused_for_plain_turns(vision_client):
    c = vision_client
    reg = c.registry
    reg._models["small"] = replace(reg.require("small"), tools=False)
    img = c.post("/api/uploads", files={"file": ("p.png", PNG, "image/png")}).json()["id"]
    conv = _chat(c, model="small", message="look", image_id=img)[0]["conversation_id"]
    assert c.fake_upstream.seen_payload["model"] == "small-vision"
    _chat(c, model="small", message="thanks", conversation_id=conv)
    # no swap back to the text entry just because this turn has no picture
    assert c.fake_upstream.seen_payload["model"] == "small-vision"


# --------------------------------------------------------------------------- #
# run_python: computing in a sandbox, and the answer's number check
# --------------------------------------------------------------------------- #
@pytest.fixture
def compute_client(agent_client, monkeypatch):
    """A tools model with a (fake) sandbox: records the code, returns canned output."""
    from llamacracy import sandbox
    ran, mounted = [], []

    async def fake_run(code, *, python, timeout_s, memory_mb, output_chars, files=()):
        ran.append(code)
        mounted.append(list(files))
        if "raise" in code:
            return sandbox.RunResult("Traceback (most recent call last):\nValueError: nope", ok=False)
        figs = [("area.png", PNG)] if "savefig" in code else []
        return sandbox.RunResult("2.6666666666666665\narea: 12.566370614359172", ok=True, figures=figs)
    monkeypatch.setattr(sandbox, "available", lambda python: None)
    monkeypatch.setattr(sandbox, "run", fake_run)
    agent_client.ran = ran
    agent_client.mounted = mounted
    return agent_client


def test_a_tool_call_cut_off_by_the_budget_is_not_run(compute_client):
    # from a live run: the cap cut a program off mid-string, and the loop ran
    # the half-written call ("bad arguments") and retried it, four times
    up = compute_client.fake_upstream
    up.agent_steps = [{"content": ["Let me draw it."],
                       "tool_calls": [("run_python", '{"code": "import matplotlib\\nfor i in ra')],
                       "finish": "length"},
                      {"content": ["I ran out of room before the code was finished."]}]
    ev = _chat(compute_client, model="small", message="draw yourself")
    assert compute_client.ran == []
    end = next(e for e in ev if e["type"] == "tool_end")
    assert not end["ok"] and end["summary"] == "cut off"
    assert len(up.payloads) == 2 and up.payloads[1]["tool_choice"] == "none"
    assert "cut off" in up.payloads[1]["messages"][-1]["content"]
    reply = client_messages(compute_client, ev[0]["conversation_id"])[1]
    assert reply["content"] == "I ran out of room before the code was finished."


def test_a_turn_that_can_run_code_answers_when_its_budget_is_spent(compute_client):
    from llamacracy.config import get_settings
    budget = get_settings().thinking_max_tokens
    up = compute_client.fake_upstream
    # the first step uses nearly all of the turn's budget
    up.agent_steps = [{"tool_calls": [("run_python", {"code": "print(1)"})],
                       "usage": {"prompt_tokens": 5, "completion_tokens": budget - 100}},
                      {"content": ["Done."]}]
    _chat(compute_client, model="small", message="chart it")
    # a turn that can run code gets the thinking-sized budget...
    assert up.payloads[0]["max_tokens"] == budget
    # ...and with it spent, the next request answers rather than calls
    assert up.payloads[1]["tool_choice"] == "none"


def test_a_tool_call_is_shown_while_it_is_being_written(compute_client):
    up = compute_client.fake_upstream
    code = 'import numpy as np\nprint("π ≈", np.pi)\n'
    up.agent_steps = [{"tool_calls": [("run_python", {"code": code})]}, {"content": ["Done."]}]
    ev = _chat(compute_client, model="small", message="pi?")
    drafts = [e for e in ev if e["type"] == "tool_draft"]
    assert drafts and {e["name"] for e in drafts} == {"run_python"}
    assert "".join(e["delta"] for e in drafts) == code
    # all of it before the call runs
    assert ev.index(drafts[-1]) < ev.index(next(e for e in ev if e["type"] == "tool_start"))


def test_the_chats_photos_are_readable_by_run_python(compute_client):
    c = compute_client
    reg = c.registry
    reg._models["small-vision"] = replace(reg.require("small"), key="small-vision", in_picker=False)
    reg._models["small"] = replace(reg.require("small"), vision_key="small-vision")
    conv = _photo_turn(c, "my garden")
    img = c.post("/api/uploads", files={"file": ("q.png", PNG, "image/png")}).json()["id"]
    c.fake_upstream.agent_steps = [{"tool_calls": [("run_python", {"code": "from PIL import Image"})]},
                                   {"content": ["Cropped."]}]
    _chat(c, model="small", message="crop this one", image_id=img, conversation_id=conv)
    # numbered in order, as view_image names them: the earlier photo, then this one
    assert [inner for inner, _ in c.mounted[-1]] == ["/data/img1.png", "/data/img2.png"]
    system = c.fake_upstream.payloads[-2]["messages"][0]["content"]
    assert '/data/img1.png (a photo sent with "my garden")' in system
    assert "/data/img2.png (a photo sent with this message)" in system


def test_attached_data_files_are_readable_by_run_python(compute_client):
    up = compute_client.fake_upstream
    csv = _upload_doc(compute_client, "Q3 sales (final).csv", b"month,units\nJul,12\nAug,19\n")
    notes = _upload_doc(compute_client, "notes.md", b"# Notes\nnothing to chart")
    up.agent_steps = [{"tool_calls": [("run_python", {"code": "import pandas as pd"})]},
                      {"content": ["Charted."]}]
    _chat(compute_client, model="small", message="Chart it", doc_ids=[csv["id"], notes["id"]])
    # the data file, under a plain name; not the notes
    [(inner, host)] = compute_client.mounted[0]
    assert inner == "/data/Q3_sales_final.csv" and Path(host).read_bytes().startswith(b"month,")
    system = up.payloads[0]["messages"][0]["content"]
    assert "/data/Q3_sales_final.csv" in system


def test_a_tools_model_gets_python_only_with_a_sandbox(agent_client):
    _chat(agent_client, model="small", message="2+2?")
    # no sandbox built here: no tool, and no agent turn just for that
    assert "tools" not in agent_client.fake_upstream.seen_payload


def test_model_computes_and_the_answer_numbers_are_checked(compute_client):
    c, up = compute_client, compute_client.fake_upstream
    up.agent_steps = [
        {"tool_calls": [("run_python", {"code": "# the double integral\ng = 9.81\nprint(8/3)\nplt.savefig('/tmp/out/area.png')"})]},
        {"content": ["The integral is 2.667 and the area about 12.57; the ratio is 0.4219. "
                     "Step 3 used the 5 m side, with g = 9.81."]},
    ]
    ev = _chat(c, model="small", message="integrate x*y over the region, side 5 m")
    first = up.payloads[0]
    assert [t["function"]["name"] for t in first["tools"]] == ["run_python"]
    assert "run_python runs Python 3" in first["messages"][0]["content"]
    assert c.ran == ["# the double integral\ng = 9.81\nprint(8/3)\nplt.savefig('/tmp/out/area.png')"]
    end = next(e for e in ev if e["type"] == "tool_end")
    assert end["label"] == "Computing: the double integral" and end["ok"]
    assert end["output"].startswith("2.666") and len(end["figures"]) == 1
    # the model reads the output, and is told its picture was shown
    result = next(m for m in up.payloads[1]["messages"] if m["role"] == "tool")["content"]
    assert "2.6666666666666665" in result and "shown to the user: area.png" in result

    done = ev[-1]
    # 2.667 and 12.57 came from the code; 5 was given, and 9.81 is a constant
    # the code was written with; 3 is a step number; 0.4219 came from nowhere,
    # and the reply says so
    assert done["tools"]["check"] == {"matched": 2, "unmatched": ["0.4219"]}
    fig = done["tools"]["figures"][0]
    row = c.db._conn.execute("SELECT user_id, mime, path FROM uploads WHERE id = ?", (fig,)).fetchone()
    assert row["mime"] == "image/png" and Path(row["path"]).read_bytes() == PNG
    assert c.get(f"/api/uploads/{fig}").content == PNG      # the user can open it

    # deleting the chat deletes the pictures it drew
    conv = ev[0]["conversation_id"]
    assert c.delete(f"/api/conversations/{conv}").status_code == 200
    assert not Path(row["path"]).exists()
    assert c.db._conn.execute("SELECT 1 FROM uploads WHERE id = ?", (fig,)).fetchone() is None


def test_a_failed_run_shows_the_error(compute_client):
    up = compute_client.fake_upstream
    up.agent_steps = [{"tool_calls": [("run_python", {"code": "raise ValueError('nope')"})]},
                      {"content": ["It failed, so I can't say."]}]
    ev = _chat(compute_client, model="small", message="q")
    end = next(e for e in ev if e["type"] == "tool_end")
    assert end["ok"] is False and end["summary"] == "ValueError" and end["label"] == "Running Python"
    assert "check" not in ev[-1]["tools"]         # no numbers in the answer: nothing to say


def test_a_tool_call_written_out_as_text_is_not_the_answer(agent_client):
    up = agent_client.fake_upstream
    up.agent_steps = [{"content": ["Here you go.\n\n<tool_call>\n<function=web_search>\n"
                                   "<parameter=query>x</parameter>\n</function>\n</tool_call>"]}]
    ev = _chat(agent_client, model="small", message="q", search=True)
    assert client_messages(agent_client, ev[0]["conversation_id"])[1]["content"] == "Here you go."


# --------------------------------------------------------------------------- #
# smart titles
# --------------------------------------------------------------------------- #
def _send(client, **body) -> list[dict]:
    with client.stream("POST", "/api/chat", json={"model": "small", **body}) as r:
        assert r.status_code == 200
        return _sse_events(r)


@pytest.mark.env(SMART_TITLES="1")
def test_a_new_chat_is_named_by_the_model_that_answered(client):
    from llamacracy.queue import TITLE_ASK
    up = client.fake_upstream
    up.script["small"] = ["Hello", " there"]
    up.title_deltas = ['Title: "Saying', ' hello."\nBecause you said hi.']
    events = _send(client, message="hi there, how are you doing today?")
    types = [e["type"] for e in events]
    # named once the answer is complete, so a Stop during naming can't cost the reply
    assert types[-2:] == ["done", "title"]
    conv_id = events[0]["conversation_id"]
    assert events[-1] == {"type": "title", "conversation_id": conv_id, "title": "Saying hello"}
    assert client.get(f"/api/conversations/{conv_id}").json()["conversation"]["title"] == "Saying hello"
    # the question rides on the conversation as it stands, so the prompt cache holds it
    ask = up.payloads[-1]["messages"]
    assert [m["role"] for m in ask] == ["user", "assistant", "user"]
    assert ask[1]["content"] == "Hello there" and ask[2]["content"] == TITLE_ASK
    assert up.payloads[-1]["max_tokens"] <= 32
    # the reply is saved as written, its token counts untouched by the naming
    msgs = client.get(f"/api/conversations/{conv_id}").json()["messages"]
    assert msgs[1]["content"] == "Hello there" and msgs[1]["completion_tokens"] == 3

    n = len(up.payloads)
    more = _send(client, message="and again", conversation_id=conv_id)
    assert "title" not in [e["type"] for e in more]  # named once
    assert len(up.payloads) == n + 1


@pytest.mark.env(SMART_TITLES="1")
def test_naming_never_costs_the_reply(client, monkeypatch):
    up = client.fake_upstream
    up.title_deltas = ["   "]                        # nothing usable
    events = _send(client, message="first message here")
    assert "title" not in [e["type"] for e in events]
    conv_id = events[0]["conversation_id"]
    assert client.get(f"/api/conversations/{conv_id}").json()["conversation"]["title"] == "first message here"

    real = up.stream_chat

    async def broken(payload):
        from llamacracy.queue import TITLE_ASK
        if payload["messages"][-1].get("content") == TITLE_ASK:
            raise RuntimeError("upstream fell over")
        async for c in real(payload):
            yield c

    monkeypatch.setattr(up, "stream_chat", broken)
    events = _send(client, message="second chat")
    assert events[-1]["type"] == "done" and events[-1]["state"] == "done"
    conv_id = events[0]["conversation_id"]
    detail = client.get(f"/api/conversations/{conv_id}").json()
    assert detail["conversation"]["title"] == "second chat"
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]


def test_titles_stay_as_typed_when_naming_is_off(client):
    events = _send(client, message="keep me")
    assert "title" not in [e["type"] for e in events]
    assert len(client.fake_upstream.payloads) == 1


@pytest.mark.parametrize("raw, title", [
    ("Planning a Trip to Kyoto", "Planning a Trip to Kyoto"),
    ('"Fixing the Wi-Fi."', "Fixing the Wi-Fi"),
    ("**Title:** Sourdough starter tips", "Sourdough starter tips"),
    ("<think>hmm</think>\nTax questions for 2026", "Tax questions for 2026"),
    ("Why is the sky blue?", "Why is the sky blue?"),
    ("\n\n  Rust lifetimes  \nThis chat is about...", "Rust lifetimes"),
    ("", ""), ("“ ”", ""),
    ("A very long title that keeps going well past any sensible length for a sidebar entry",
     "A very long title that keeps going well past any sensible…"),
])
def test_clean_title(raw, title):
    from llamacracy.queue import clean_title
    assert clean_title(raw) == title


def test_admin_locks_and_unlocks_a_model(client):
    assert client.post("/api/admin/lock", json={"model": "nope"}).status_code == 400
    r = client.post("/api/admin/lock", json={"model": "small"})
    assert r.status_code == 200 and r.json() == {"locked_model": "small"}
    live = client.get("/api/admin/live").json()
    assert live["locked_model"] == "small"
    assert {"id": "small", "display": "Small"} in live["lockable"]
    assert client.get("/api/queue").json()["locked_model"] == "small"
    assert client.post("/api/admin/unload").json() == {"ok": True}   # releases it too
    assert client.get("/api/admin/live").json()["locked_model"] is None
    client.post("/api/admin/lock", json={"model": "small"})
    assert client.post("/api/admin/lock", json={"model": None}).json() == {"locked_model": None}


def test_deleting_a_chat_mid_reply_leaves_the_queue_working(client):
    import threading

    import llamacracy.queue as q
    up = client.fake_upstream
    up.delay = 0.2
    up.script["small"] = ["a", "b", "c", "d", "e"]
    with client.stream("POST", "/api/chat", json={"model": "small", "message": "hi"}) as r:
        it = r.iter_lines()
        first = next(line for line in it if line.startswith("data:"))
        conv = json.loads(first[5:])["conversation_id"]
        assert client.delete(f"/api/conversations/{conv}").status_code == 200
        tail = [json.loads(line[5:])["type"] for line in it if line.startswith("data:")]
    assert tail[-1] == "done" and tail[-1] != "error"     # the stream ended cleanly
    assert all(not t.done() for t in q._qm._tasks)         # worker and monitors alive
    assert _count(client.db, "messages") == 0              # nothing written back

    up.delay = 0.01
    out: dict = {}

    def again():
        out["events"] = _send(client, message="still there?")
    t = threading.Thread(target=again, daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive() and out["events"][-1]["type"] == "done"


def test_cleanup_that_fails_still_ends_the_stream(client, monkeypatch):
    import llamacracy.queue as q

    async def boom(job):
        raise RuntimeError("disk full")
    monkeypatch.setattr(q._qm, "_persist_assistant_message", boom)
    events = _send(client, message="hi")
    assert events[-1]["type"] == "done"
    monkeypatch.undo()
    assert _send(client, message="next")[-1]["type"] == "done"   # the worker carried on


@pytest.mark.env(SMART_TITLES="1")
def test_naming_an_agent_turn_keeps_its_prompt_prefix(agent_client):
    from llamacracy.queue import TITLE_ASK
    c, up = agent_client, agent_client.fake_upstream
    up.agent_steps = [{"content": ["It shipped in May."]}]
    events = _send(c, message="when did it ship?", search=True)
    assert events[-1]["type"] == "title"
    turn, ask = up.payloads[-2], up.payloads[-1]
    assert ask["messages"][-1]["content"] == TITLE_ASK
    assert ask["tools"] == turn["tools"] and ask["tool_choice"] == "none"
    assert ask["messages"][:len(turn["messages"])] == turn["messages"]   # same start, so cached


@pytest.mark.env(SMART_TITLES="1")
def test_naming_a_photo_turn_leaves_the_picture_out(vision_client):
    c, up = vision_client, vision_client.fake_upstream
    img = c.post("/api/uploads", files={"file": ("p.png", PNG, "image/png")}).json()["id"]
    up.agent_steps = [{"content": ["A fern."]}]
    events = _send(c, message="what plant is this?", image_id=img)
    assert events[-1]["type"] == "title"
    ask = up.payloads[-1]
    assert "image_url" not in json.dumps(ask)
    assert any(m.get("content") == "what plant is this?" for m in ask["messages"])


def test_v1_cap_cant_be_sidestepped_with_n_predict(client):
    key = client.post("/api/keys", json={"label": "t"}).json()["key"]
    client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                json={"model": "small", "max_tokens": 10, "n_predict": -1,
                      "messages": [{"role": "user", "content": "hi"}]})
    sent = client.fake_upstream.seen_payload
    assert "n_predict" not in sent and sent["max_tokens"] == 10


def test_v1_images_must_be_data_urls(client):
    key = client.post("/api/keys", json={"label": "t"}).json()["key"]
    for url in ("http://192.168.1.1/admin.png", "file:///etc/passwd"):
        r = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                        json={"model": "small", "messages": [{"role": "user", "content": [
                            {"type": "image_url", "image_url": {"url": url}},
                            {"type": "text", "text": "what's this?"}]}]})
        assert r.status_code == 400 and "data:" in r.json()["detail"]


def test_queue_impact_counts_the_wait_one_user_caused_another(client):
    import time as _t
    client.get("/api/me")                                    # the admin's own row (user 1)
    db = client.db
    db._conn.execute("INSERT INTO users (id, oidc_sub, email, created_at) VALUES (2, 's2', 'bob@x', 0)")
    t = _t.time() - 3600
    rows = [("a", 1, t, t, t + 10), ("b", 2, t + 2, t + 10, t + 15),   # b waited 8 s behind a
            ("c", 2, t + 100, t + 100, t + 105)]                   # nobody waited for c
    for jid, uid, q, p, f in rows:
        db._conn.execute("INSERT INTO jobs (id, user_id, model_id, state, queued_at, picked_at, finished_at) "
                         "VALUES (?, ?, 'small', 'done', ?, ?, ?)", (jid, uid, q, p, f))
    impact = client.get("/api/admin/queue-impact").json()["impact"]
    assert impact == [{"email": "dev@localhost", "display_name": impact[0]["display_name"],
                       "wait_inflicted_s": 8.0, "jobs_delayed": 1}]


@pytest.mark.env(MAX_JOBS_PER_USER="2")
def test_one_person_can_only_have_a_few_jobs_in_flight(client):
    import llamacracy.queue as q
    qm = q._qm
    key = client.post("/api/keys", json={"label": "t"}).json()["key"]
    me = client.get("/api/me").json()
    uid = me.get("id") or me.get("user_id") or 1
    blockers = [q.Job(user_id=uid, owner_name="me", owner_email="dev@localhost", model_id="small",
                      payload={"messages": []}) for _ in range(2)]
    qm._pending.extend(blockers)                 # two already waiting (the worker is parked)
    try:
        r = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                        json={"model": "small", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 429 and "already have 2" in r.json()["detail"]
        client.db._conn.execute("UPDATE users SET uncapped = 1 WHERE id = ?", (uid,))
        assert qm.jobs_of(uid) == 2
    finally:
        for b in blockers:
            if b in qm._pending:
                qm._pending.remove(b)


# --------------------------------------------------------------------------- #
# managing chats: rename, search, download, and a message left unanswered
# --------------------------------------------------------------------------- #
def test_renaming_a_chat(client):
    cid = _chat(client, model="small", message="hi")[0]["conversation_id"]
    r = client.patch(f"/api/conversations/{cid}", json={"title": "  Sourdough\n notes  "})
    assert r.status_code == 200 and r.json()["title"] == "Sourdough notes"
    assert client.get("/api/conversations").json()["conversations"][0]["title"] == "Sourdough notes"
    assert client.patch(f"/api/conversations/{cid}", json={"title": "   "}).status_code == 422
    assert client.patch(f"/api/conversations/{cid}", json={"title": "x" * 500}).status_code == 422
    assert client.patch("/api/conversations/nope", json={"title": "x"}).status_code == 404


@pytest.mark.env(SMART_TITLES="1")
def test_a_title_you_set_is_not_replaced_by_the_models(client):
    # naming only replaces the stand-in title, so it loses to a rename
    cid = _chat(client, model="small", message="hi")[0]["conversation_id"]
    assert _conv(client, cid)["conversation"]["title"] == "Greeting the user"
    client.patch(f"/api/conversations/{cid}", json={"title": "Mine"})
    _chat(client, model="small", message="more", conversation_id=cid)
    assert _conv(client, cid)["conversation"]["title"] == "Mine"


def test_searching_chats_by_title_and_by_what_was_said(client):
    up = client.fake_upstream
    up.script["small"] = ["Feed the starter twice a day until it doubles."]
    a = _chat(client, model="small", message="my bread is flat")[0]["conversation_id"]
    up.script["small"] = ["Paris."]
    b = _chat(client, model="small", message="capital of France?")[0]["conversation_id"]

    def search(q):
        r = client.get("/api/conversations/search", params={"q": q})
        assert r.status_code == 200
        return r.json()["results"]

    hit = search("STARTER")                      # in a reply, any case
    assert [x["id"] for x in hit] == [a]
    assert "starter" in hit[0]["snippet"] and hit[0]["title"] == "my bread is flat"
    assert [x["id"] for x in search("capital")] == [b]       # in the title (and the message)
    assert search("100%") == [] and search("_") == []        # LIKE wildcards are plain text
    assert search("zebra") == []
    assert client.get("/api/conversations/search", params={"q": " "}).status_code == 422


def test_search_only_finds_your_own_chats(client):
    _chat(client, model="small", message="secret plans")
    client.db._conn.execute("INSERT INTO users (id, oidc_sub, email, created_at) "
                            "VALUES (77, 'someone-else', 'else@x', 0)")
    client.db._conn.execute("UPDATE conversations SET user_id = 77")
    assert client.get("/api/conversations/search", params={"q": "secret"}).json()["results"] == []


def test_downloading_a_chat_as_markdown(client):
    up = client.fake_upstream
    cid = _chat(client, model="small", message="one")[0]["conversation_id"]
    up.script["small"] = ["reply two"]
    _chat(client, model="small", message="two", conversation_id=cid)
    two = _conv(client, cid)["messages"][2]
    up.script["small"] = ["reply TWO"]
    _chat(client, model="small", message="TWO", conversation_id=cid, edit_of=two["id"])
    client.patch(f"/api/conversations/{cid}", json={"title": "Counting: 1/2"})

    r = client.get(f"/api/conversations/{cid}/export.md")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert 'filename="Counting_ 1_2.md"' in r.headers["content-disposition"]
    text = r.text
    assert text.startswith("# Counting: 1/2\n")
    # the branch on screen, in order, each turn under who said it
    assert text.index("## You") < text.index("one") < text.index("## Small") < text.index("hello world")
    assert "TWO" in text and "reply TWO" in text and "reply two" not in text
    assert client.get("/api/conversations/nope/export.md").status_code == 404


def test_a_message_left_unanswered_is_sent_with_the_next_one(client):
    # a reply that was stopped or failed leaves a user message with nothing
    # under it; the next send must not put two user turns in a row
    cid = _chat(client, model="small", message="one")[0]["conversation_id"]
    client.db._conn.execute("DELETE FROM messages WHERE role = 'assistant'")
    client.db._conn.execute("UPDATE messages SET active_child_id = NULL")
    _chat(client, model="small", message="two", conversation_id=cid)
    assert client.fake_upstream.seen_payload["messages"] == [
        {"role": "user", "content": "one\n\ntwo"}]
    assert _texts(_conv(client, cid)) == ["one", "two", "hello world"]
