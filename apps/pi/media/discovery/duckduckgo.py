"""DuckDuckGo image discovery provider."""

from __future__ import annotations

from datetime import UTC, datetime

from ..models import ImageCandidate, ImageSearchQuery


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class DuckDuckGoImageDiscoveryProvider:
    """DDG image search behind the provider interface.

    Uses the full-resolution `image` URL when available; thumbnail/proxy URLs
    are intentionally not used when a full-resolution URL exists.
    """

    name = "duckduckgo_images"

    def __init__(self, *, factory=None, max_results: int = 15):
        self.factory = factory
        self.max_results = max_results

    def _ddgs(self):
        if self.factory is not None:
            return self.factory() if callable(self.factory) else self.factory
        from ddgs import DDGS

        return DDGS()

    def search(self, query: ImageSearchQuery) -> list[ImageCandidate]:
        max_r = query.max_results if query.max_results is not None else self.max_results
        rows = []
        last_exc = None
        for attempt in range(2):
            try:
                rows = self._ddgs().images(query.query, max_results=max_r) or []
                break
            except Exception as exc:
                last_exc = exc
                if attempt == 0:
                    import time
                    time.sleep(0.5)
        if last_exc and not rows:
            return []
        candidates = []
        for rank, row in enumerate(rows, start=1):
            image_url = str(row.get("image") or row.get("url") or "").strip()
            if not image_url:
                continue
            candidates.append(
                ImageCandidate(
                    candidate_id=f"ddg_{rank}",
                    discovery_provider=self.name,
                    query=query.query,
                    search_result_rank=rank,
                    image_url=image_url,
                    source_page_url=str(row.get("url") or row.get("page_url") or "").strip() or None,
                    host=_host(image_url),
                    title=str(row.get("title") or "").strip() or None,
                    discovered_at=_now(),
                )
            )
        return candidates


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).netloc.casefold().removeprefix("www.") or None
