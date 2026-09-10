from __future__ import annotations

import asyncio
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

    async def health(self):
        return True

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
        for i, d in enumerate(deltas):
            await asyncio.sleep(self.delay)
            yield StreamChunk(raw={}, content_delta=d)
        yield StreamChunk(raw={}, usage=dict(self.usage), timings=dict(self.timings))
