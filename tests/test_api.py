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
def client(tmp_db_path, tmp_path, monkeypatch, request):
    """A TestClient whose app talks to a temp SQLite file, a scripted upstream
    and an in-code registry. `pytest.mark.env(...)` on the test sets extra
    environment (e.g. a tiny credit cap) before settings are built."""
    monkeypatch.setenv("DEV_MODE", "1")
    monkeypatch.setenv("DEV_EMAIL", "dev@localhost")
    monkeypatch.setenv("ADMIN_EMAILS", "dev@localhost")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
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
        {"content": ["Let me check."], "tool_calls": [("web_search", {"query": "llama release"})]},
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
    assert [t["function"]["name"] for t in first["tools"]] == ["web_search"]
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
    assert ev[-1]["type"] == "done" and ev[-1]["state"] == "done"


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
    assert ev[-1]["state"] == "done"


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
