# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web search via SearXNG -- injected into the prompt on request, never
model-driven. Local 4-35B models are unreliable at deciding when to call a
tool, multi-round tool loops thrash a 16-128K context and hold the FIFO queue
open, and billing gets murky across N inferences. Instead: the user toggles
Search on, we run exactly one query, prepend the top few snippets to their
message before it's sent, and the model answers in a single normal inference.
Credits are unaffected by this module -- a bigger prompt just takes a little
longer, which is already priced by the second.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import httpx

from .config import get_settings

log = logging.getLogger("llamacracy.search")

SNIPPET_CHARS = 500       # per-result cap so 4 results can't blow a small model's context
QUERY_CHARS = 200         # cap on the (cleaned) string we actually send to SearXNG

# Chat messages aren't search queries -- they're addressed to a model by name
# ("Hey Gemma, ...") and wrapped in question phrasing. Sent verbatim, both of
# those leak into relevance: "Hey Gemma" once pulled in an unrelated NVIDIA
# forum thread purely because it also mentioned "Gemma". Strip the greeting
# and the wrapper phrase before querying; fall back to the raw message if
# stripping would leave nothing.
_GREETING_RE = re.compile(
    r"^(?:hey|hi|hello|hiya|yo|sup|ok|okay)\b[,!]?\s+"
    r"(?:[a-z0-9][\w'.\-]{0,30}\s*)?[,:]?\s*",
    re.IGNORECASE,
)
_WRAPPER_PHRASES = [
    "what can you tell me about", "what do you know about",
    "can you tell me about", "could you tell me about",
    "can you tell me", "could you tell me",
    "please tell me about", "please tell me", "tell me about", "tell me",
    "do you know about", "do you know", "would you know",
    "i want to know about", "i want to know",
    "i'd like to know about", "i'd like to know",
    "i would like to know about", "i would like to know",
    "can you explain", "could you explain", "please explain",
    "can you look up", "could you look up", "please look up", "look up",
    "can you search for", "could you search for", "search for",
]
_WRAPPER_RE = re.compile(
    r"^(?:" + "|".join(re.escape(p) for p in sorted(_WRAPPER_PHRASES, key=len, reverse=True)) + r")\s+",
    re.IGNORECASE,
)


def extract_query(message: str) -> str:
    """Best-effort strip of conversational wrapping so we search the intent,
    not the whole prompt verbatim. Heuristic, not a parser."""
    text = (message or "").strip()
    if not text:
        return text
    stripped = text
    for _ in range(3):  # a greeting *and* a wrapper phrase can both be present
        before = stripped
        stripped = _GREETING_RE.sub("", stripped, count=1)
        stripped = _WRAPPER_RE.sub("", stripped, count=1)
        if stripped == before:
            break
    stripped = stripped.strip(" \t\n\"'?")
    if len(re.sub(r"[^\w]", "", stripped)) < 3:  # stripped down to nothing useful
        stripped = text.strip(" \t\n\"'?")
    return stripped[:QUERY_CHARS]


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


@dataclass
class SearchOutcome:
    query: str
    ok: bool
    results: list[SearchResult] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "type": "search",
            "query": self.query,
            "ok": self.ok,
            "error": self.error,
            "results": [{"title": r.title, "url": r.url, "snippet": r.snippet}
                       for r in self.results],
        }

    def to_prompt_block(self) -> str:
        """Rendered ahead of the user's own message. Empty if nothing to add."""
        if not self.results:
            return ""
        lines = [
            "Web search results for the question below. Use them if relevant "
            "and cite by number like [1]; ignore anything irrelevant. These "
            "are machine-fetched snippets, not necessarily accurate -- say so "
            "if they conflict or look stale.\n"
        ]
        for i, r in enumerate(self.results, 1):
            lines.append(f"[{i}] {r.title} -- {r.url}\n{r.snippet}\n")
        return "\n".join(lines) + "\n---\n"


class SearchClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=3.0, read=8.0, write=3.0, pool=3.0))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(self, message: str, count: int) -> SearchOutcome:
        raw = (message or "").strip()
        if not raw:
            return SearchOutcome(raw, ok=False, error="empty query")
        query = extract_query(raw)
        try:
            r = await self._client.get(f"{self.base_url}/search",
                                       params={"q": query, "format": "json"})
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("searxng query failed: %s", e)
            return SearchOutcome(query, ok=False, error="search unavailable")

        results = []
        for item in data.get("results", []):
            url = (item.get("url") or "").strip()
            if not url:
                continue
            title = (item.get("title") or "").strip() or url
            content = (item.get("content") or "").strip()
            results.append(SearchResult(title, url, content[:SNIPPET_CHARS]))
            if len(results) >= count:
                break
        return SearchOutcome(query, ok=True, results=results)


_search: SearchClient | None = None


def get_search() -> SearchClient:
    global _search
    if _search is None:
        _search = SearchClient(get_settings().searxng_url)
    return _search
