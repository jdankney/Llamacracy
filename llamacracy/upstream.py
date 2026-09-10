"""Thin async client for llama-swap. Our app never spawns llama-server -- it
talks OpenAI-compatible HTTP to llama-swap, which owns model loading/swapping.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from .config import get_settings

log = logging.getLogger("llamacracy.upstream")


@dataclass
class StreamChunk:
    raw: dict
    content_delta: str = ""
    reasoning_delta: str = ""
    usage: dict | None = None
    timings: dict | None = None
    finish_reason: str | None = None


class Upstream:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or get_settings().llamaswap_url).rstrip("/")
        # long read timeout: a heavyweight cold start can block the first byte ~25 s
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> bool:
        try:
            r = await self._client.get("/health", timeout=5.0)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def running(self) -> list[dict]:
        """Currently loaded model(s), straight from llama-swap -- the queue's
        source of truth for 'what is resident', never our own guess."""
        try:
            r = await self._client.get("/running", timeout=5.0)
            r.raise_for_status()
            return r.json().get("running", [])
        except httpx.HTTPError as e:
            log.warning("running() failed: %s", e)
            return []

    async def loaded_model(self) -> str | None:
        for m in await self.running():
            if m.get("state") == "ready":
                return m.get("model")
        return None

    async def unload_all(self) -> bool:
        try:
            r = await self._client.post("/api/models/unload", timeout=30.0)
            return r.status_code < 300
        except httpx.HTTPError as e:
            log.warning("unload_all failed: %s", e)
            return False

    async def list_models(self) -> list[str]:
        r = await self._client.get("/v1/models", timeout=5.0)
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", [])]

    async def stream_chat(self, payload: dict) -> AsyncIterator[StreamChunk]:
        """POST /v1/chat/completions with streaming. Always sets
        stream_options.include_usage so the final chunk carries token counts.
        Yields until [DONE] or the caller cancels the surrounding task.
        """
        body = dict(payload)
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}

        async with self._client.stream("POST", "/v1/chat/completions", json=body) as resp:
            if resp.status_code != 200:
                detail = (await resp.aread()).decode("utf-8", "replace")
                raise UpstreamError(resp.status_code, detail)
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                yield _parse_chunk(obj)

    async def completion(self, payload: dict) -> dict:
        """Non-streaming /v1/completions (FIM path via input_prefix/suffix or
        prompt). Used for the infill endpoint."""
        r = await self._client.post("/v1/completions", json={**payload, "stream": False})
        if r.status_code != 200:
            raise UpstreamError(r.status_code, r.text)
        return r.json()

    async def infill(self, payload: dict) -> dict:
        r = await self._client.post("/infill", json={**payload, "stream": False})
        if r.status_code != 200:
            raise UpstreamError(r.status_code, r.text)
        return r.json()


class UpstreamError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"upstream {status}: {detail[:300]}")
        self.status = status
        self.detail = detail


def _parse_chunk(obj: dict) -> StreamChunk:
    choices = obj.get("choices") or []
    delta = choices[0].get("delta", {}) if choices else {}
    return StreamChunk(
        raw=obj,
        content_delta=delta.get("content") or "",
        reasoning_delta=delta.get("reasoning_content") or "",
        usage=obj.get("usage"),
        timings=obj.get("timings"),
        finish_reason=(choices[0].get("finish_reason") if choices else None),
    )


_upstream: Upstream | None = None


def get_upstream() -> Upstream:
    global _upstream
    if _upstream is None:
        _upstream = Upstream()
    return _upstream
