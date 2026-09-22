from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import AsyncIterator

import pytest

os.environ.setdefault("DEV_MODE", "1")


@pytest.fixture
def tmp_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    for p in (path, path + "-wal", path + "-shm"):
        try:
            os.remove(p)
        except OSError:
            pass


class FakeUpstream:
    """Scriptable stand-in for llama-swap. `script` maps model_id -> list of
    content deltas; `loaded` is what /running reports."""

    def __init__(self):
        self.loaded: str | None = None
        self.script: dict[str, list[str]] = {}
        self.delay = 0.01
        self.usage = {"prompt_tokens": 5, "completion_tokens": 3}
        self.timings = {"prompt_ms": 20.0, "predicted_ms": 30.0}
        self.unload_calls = 0
        self.tool_calls: list[dict] | None = None
        self.seen_payload: dict | None = None

    async def health(self):
        return True

    async def aclose(self):
        pass

    async def loaded_model(self):
        return self.loaded

    async def running(self):
        return [{"model": self.loaded, "state": "ready"}] if self.loaded else []

    async def unload_all(self):
        self.unload_calls += 1
        self.loaded = None
        return True

    async def stream_chat(self, payload) -> AsyncIterator:
        from llamacracy.upstream import StreamChunk

        model = payload.get("model")
        deltas = self.script.get(model, ["hello", " world"])
        self.loaded = model
        for d in deltas:
            await asyncio.sleep(self.delay)
            yield StreamChunk(raw={}, content_delta=d)
        yield StreamChunk(raw={}, usage=dict(self.usage), timings=dict(self.timings))

    # -- raw passthrough (/v1) ------------------------------------------------
    # These return real OpenAI wire bytes rather than StreamChunks, because the
    # passthrough's whole contract is that the bytes survive the trip untouched.
    # `seen_payload` records what the app actually forwarded, which is where
    # dropped fields (tools, tool_choice) would show up.

    def _raw_chunks(self, model: str) -> list[dict]:
        out = [{"id": "chatcmpl-fake", "object": "chat.completion.chunk", "model": model,
                "choices": [{"index": 0, "delta": {"content": d}, "finish_reason": None}]}
               for d in self.script.get(model, ["hello", " world"])]
        if self.tool_calls:
            out.append({"id": "chatcmpl-fake", "object": "chat.completion.chunk",
                        "model": model,
                        "choices": [{"index": 0, "delta": {"tool_calls": self.tool_calls},
                                     "finish_reason": "tool_calls"}]})
        out.append({"id": "chatcmpl-fake", "object": "chat.completion.chunk", "model": model,
                    "choices": [], "usage": dict(self.usage), "timings": dict(self.timings)})
        return out

    async def stream_raw(self, payload) -> AsyncIterator[bytes]:
        self.seen_payload = dict(payload)
        model = payload.get("model")
        self.loaded = model
        for obj in self._raw_chunks(model):
            await asyncio.sleep(self.delay)
            yield f"data: {json.dumps(obj)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    async def post_raw(self, payload) -> bytes:
        self.seen_payload = dict(payload)
        model = payload.get("model")
        self.loaded = model
        await asyncio.sleep(self.delay)
        msg: dict = {"role": "assistant",
                     "content": "".join(self.script.get(model, ["hello", " world"]))}
        if self.tool_calls:
            msg["content"] = None
            msg["tool_calls"] = self.tool_calls
        return json.dumps({
            "id": "chatcmpl-fake", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": msg,
                         "finish_reason": "tool_calls" if self.tool_calls else "stop"}],
            "usage": dict(self.usage), "timings": dict(self.timings),
        }).encode()
