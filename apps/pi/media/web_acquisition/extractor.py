"""Generic metadata extraction from HTML.

Extracts structured and stable evidence: JSON-LD, Open Graph/meta, canonical
URL, visible text, image URLs. No marketplace-specific selectors.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser

from .models import ExtractedPageContent

_JSONLD_RE = re.compile(r'<script[^>]*type=["\']application/(?:ld\+json)["\'][^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL)
_IMG_RE = re.compile(r"<img[^>]+(?:src|data-src)=[\"']([^\"']+)[\"']", re.IGNORECASE)
_SRCSET_RE = re.compile(r"srcset=[\"']([^\"']+)[\"']", re.IGNORECASE)
_MAX_TEXT = 8000
_MAX_IMAGES = 60


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip_depth = 0
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip_depth += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data):
        if not self.skip_depth:
            stripped = " ".join(data.split())
            if stripped:
                self.parts.append(stripped)


def extract_page_content(html: str, *, url: str, capture_text: bool = True, discover_images: bool = True) -> ExtractedPageContent:
    content = ExtractedPageContent()
    _extract_json_ld(html, content)
    _extract_meta(html, content)
    canonical = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)', html, re.IGNORECASE)
    if canonical:
        content.canonical_url = canonical.group(1)
    if capture_text:
        parser = _TextExtractor()
        try:
            parser.feed(html)
        except Exception:  # noqa: BLE001,S110 - tolerate malformed HTML, keep partial text
            pass
        # Keep source text-node boundaries so evidence extraction can cite an
        # exact product fact instead of a merged navigation/SEO sentence.
        text = "\n".join(parser.parts)[: _MAX_TEXT]
        if text:
            content.visible_text = text
    if discover_images:
        content.image_urls = _image_urls(html, url)
    return content


def _extract_json_ld(html: str, content: ExtractedPageContent) -> None:
    for match in _JSONLD_RE.findall(html):
        try:
            payload = json.loads(match.strip())
        except (ValueError, TypeError):
            continue
        for item in payload if isinstance(payload, list) else [payload]:
            if isinstance(item, dict) and item:
                content.json_ld.append(item)
        if len(content.json_ld) >= 10:
            break


def _extract_meta(html: str, content: ExtractedPageContent) -> None:
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if title:
        content.title = " ".join(title.group(1).split())[:300]
    description = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']', html, re.IGNORECASE)
    if description:
        content.meta_description = description.group(1)[:500]
    og_image = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if og_image:
        content.og["image"] = og_image.group(1)
    og_title = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if og_title:
        content.og["title"] = og_title.group(1)[:300]
    og_description = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']*)["\']', html, re.IGNORECASE)
    if og_description:
        content.og["description"] = og_description.group(1)[:500]
    for key, pattern in (
        ("product:price:amount", r'<meta[^>]+property=["\']product:price:amount["\'][^>]+content=["\']([^"\']+)["\']'),
        ("product:price:currency", r'<meta[^>]+property=["\']product:price:currency["\'][^>]+content=["\']([^"\']+)["\']'),
    ):
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            content.og[key] = match.group(1)[:64]


def _image_urls(html: str, base_url: str) -> list[str]:

    urls: list[str] = []
    for match in _IMG_RE.findall(html):
        url = match.strip()
        if url.startswith(("http://", "https://")):
            urls.append(url)
    for match in _SRCSET_RE.findall(html):
        for part in match.split(","):
            candidate = part.strip().split(" ")[0]
            if candidate.startswith(("http://", "https://")):
                urls.append(candidate)
    seen: dict[str, str] = {}
    for url in urls:
        if url in seen:
            continue
        seen[url] = url
        if len(seen) >= _MAX_IMAGES:
            break
    return list(seen)
