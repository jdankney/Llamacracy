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
from dataclasses import dataclass, field

import httpx

from .config import get_settings

log = logging.getLogger("llamacracy.search")

SNIPPET_CHARS = 500       # per-result cap so 4 results can't blow a small model's context


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

    async def search(self, query: str, count: int) -> SearchOutcome:
        query = (query or "").strip()
        if not query:
            return SearchOutcome(query, ok=False, error="empty query")
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
