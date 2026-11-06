"""DDG web-text source discovery provider with engine fallback."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

from .models import SourceCandidate, SourceDiscoveryQuery

# Ordered text engines tried in sequence when the previous one yields no rows.
# `auto` shuffles ddgs' engine list and returns rows keyed by href; the pinned
# engines commonly return "No results found" under rate limits, so auto runs
# first and the rest are explicit fallbacks to widen coverage.
TEXT_BACKENDS = ("auto", "google", "mojeek", "startpage", "yandex")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _host(url: str) -> str:
    return urlsplit(url).netloc.casefold().removeprefix("www.") or ""


class DuckDuckGoSourceDiscoveryProvider:
    """DDG text search behind the source discovery interface.

    Tries TEXT_BACKENDS in order; returns [] only when every engine yields no
    rows. Each engine call is bounded by a thread-pool timeout so a stuck
    backend cannot block the research loop forever.
    """

    name = "duckduckgo_web"

    def __init__(self, *, factory=None, max_results: int = 15, backend_timeout: float = 20.0):
        self.factory = factory
        self.max_results = max_results
        self.backend_timeout = backend_timeout

    def _ddgs(self):
        if self.factory is not None:
            return self.factory() if callable(self.factory) else self.factory
        from ddgs import DDGS

        return DDGS()

    def _text(self, backend: str, query: str, max_results: int) -> list[dict]:
        # ddgs runs its own async/thread machinery internally; bound the whole
        # call so a dead backend cannot hang research indefinitely. shutdown is
        # non-blocking so a genuinely stuck backend cannot block past the timeout.
        import concurrent.futures

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(self._ddgs().text, query, region="vn-vi", backend=backend, max_results=max_results)
            try:
                return future.result(timeout=self.backend_timeout) or []
            except Exception as exc:  # noqa: BLE001 - backend failure is non-fatal
                raise RuntimeError(f"ddg web search failed (backend={backend}): {exc}") from exc
        finally:
            pool.shutdown(wait=False)

    def search(self, query: SourceDiscoveryQuery) -> list[SourceCandidate]:
        max_results = min(query.max_results, self.max_results)
        rows = self._search_all(query.query, max_results)
        candidates = []
        for rank, row in enumerate(rows, start=1):
            url = str(row.get("url") or row.get("href") or "").strip()
            if not url or not url.startswith(("http://", "https://")):
                continue
            candidates.append(
                SourceCandidate(
                    candidate_id=f"src_{rank}",
                    url=url,
                    title=str(row.get("title") or "").strip(),
                    snippet=str(row.get("body") or row.get("snippet") or "").strip(),
                    host=_host(url),
                    query=query.query,
                    discovery_provider=self.name,
                    rank=rank,
                    discovered_at=_now(),
                )
            )
        return candidates

    def _search_all(self, query_text: str, max_results: int) -> list[dict]:
        # factory-backed (test) path: single call, no backend fallback.
        if self.factory is not None:
            try:
                return self._text("auto", query_text, max_results)
            except RuntimeError as exc:
                raise exc
        for backend in TEXT_BACKENDS:
            try:
                rows = self._text(backend, query_text, max_results)
            except RuntimeError:
                continue
            if rows:
                return rows
        return []
