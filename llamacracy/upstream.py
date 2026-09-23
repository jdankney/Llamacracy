# SPDX-License-Identifier: AGPL-3.0-or-later
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

    async def stream_raw(self, payload: dict) -> AsyncIterator[bytes]:
        """POST /v1/chat/completions and yield the response body byte for byte.

        The /v1 passthrough uses this instead of stream_chat: nothing in the
        wire format is parsed, reshaped or re-serialised on the way out, so
        tool calls, logprobs, multiple choices and whatever llama.cpp grows
        next reach the client exactly as llama-swap sent them. Billing reads
        what it needs from a separate sniffer fed the same bytes.
        """
        async with self._client.stream("POST", "/v1/chat/completions", json=payload) as resp:
            if resp.status_code != 200:
                detail = (await resp.aread()).decode("utf-8", "replace")
                raise UpstreamError(resp.status_code, detail)
            async for raw in resp.aiter_bytes():
                if raw:
                    yield raw

    async def post_raw(self, payload: dict) -> bytes:
        """Non-streaming sibling of stream_raw: the whole JSON body, untouched."""
        r = await self._client.post("/v1/chat/completions", json=payload)
        if r.status_code != 200:
            raise UpstreamError(r.status_code, r.text)
        return r.content


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


class UsageSniffer:
    """Reads billing facts out of a passthrough response without touching it.

    Fed the same bytes that go to the client, it watches for the first sign of
    real generation (so the queue can stop the cold-load clock) and keeps the
    last `usage` / `timings` block it sees. Anything it fails to parse is
    simply ignored -- a sniffer that cannot read a chunk must never be able to
    break the stream that chunk belongs to.
    """

    def __init__(self) -> None:
        self._buf = b""
        self.usage: dict | None = None
        self.timings: dict | None = None
        self.saw_output = False

    def feed(self, raw: bytes) -> None:
        """Accumulate SSE bytes; whole `data:` lines are parsed as they complete."""
        self._buf += raw
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if line.startswith(b"data:"):
                self._feed_json(line[5:].strip())

    def feed_body(self, raw: bytes) -> None:
        """Parse one complete non-streaming JSON body."""
        self._feed_json(raw)

    def _feed_json(self, data: bytes) -> None:
        if not data or data == b"[DONE]":
            return
        try:
            obj = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(obj, dict):
            return
        if isinstance(obj.get("usage"), dict):
            self.usage = obj["usage"]
        if isinstance(obj.get("timings"), dict):
            self.timings = obj["timings"]
        for choice in obj.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            # streaming carries `delta`, non-streaming `message`; either one
            # holding content, reasoning or a tool call means the model is
            # producing, not still loading
            part = choice.get("delta") or choice.get("message") or {}
            if isinstance(part, dict) and any(
                part.get(k) for k in ("content", "reasoning_content", "tool_calls")
            ):
                self.saw_output = True


_upstream: Upstream | None = None


def get_upstream() -> Upstream:
    global _upstream
    if _upstream is None:
        _upstream = Upstream()
    return _upstream
