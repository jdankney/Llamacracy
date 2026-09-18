"""HTTP-level smoke tests against the real FastAPI app with a scripted
upstream: the SSE chat contract, the OpenAI-compatible endpoint, and the
enqueue-time limit gate leaving no orphan rows behind."""

from __future__ import annotations

import json

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
    assert [m["id"] for m in r.json()["models"]] == ["small"]


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
