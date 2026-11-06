"""Extract product gallery images from a TikTok Shop PDP."""

from __future__ import annotations

from datetime import UTC, datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit

from media.models import ImageCandidate


class _GalleryParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.images: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "img":
            return
        values = dict(attrs)
        if "cursor-zoom-in" not in (values.get("class") or ""):
            return
        src = unescape(values.get("src") or "")
        if src.startswith("https://"):
            self.images.append((src, values.get("alt") or ""))


def _fetch_page(url: str) -> str:
    import httpx

    response = httpx.get(url, follow_redirects=True, timeout=12.0)
    response.raise_for_status()
    return response.text


def extract_tiktok_gallery_candidates(
    pdp_url: str,
    product_name: str,
    *,
    fetcher=None,
) -> list[ImageCandidate]:
    """Return gallery images only; review photos and related items are excluded."""
    parts = urlsplit(pdp_url)
    if parts.scheme != "https" or parts.hostname != "shop.tiktok.com" or "/vn/pdp/" not in parts.path:
        raise ValueError("Expected a TikTok Shop Vietnam PDP URL")
    html = (fetcher or _fetch_page)(pdp_url)
    parser = _GalleryParser()
    parser.feed(html)
    seen: set[str] = set()
    candidates: list[ImageCandidate] = []
    for image_url, alt in parser.images:
        if image_url in seen:
            continue
        seen.add(image_url)
        candidates.append(ImageCandidate(
            candidate_id=f"tiktok_gallery_{len(candidates) + 1}",
            discovery_provider="tiktok_pdp_gallery",
            query=product_name,
            search_result_rank=len(candidates) + 1,
            image_url=image_url,
            source_page_url=pdp_url,
            host=urlsplit(image_url).hostname,
            title=alt or product_name,
            discovered_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        ))
        if len(candidates) >= 10:
            break
    return candidates
