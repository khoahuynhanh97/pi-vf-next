"""Deterministic query generation and source classification."""

from __future__ import annotations

import unicodedata
from urllib.parse import urlsplit

from media.models import ProductIdentityHint

from .models import SourceCandidate, SourceClass


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.split()).strip()


def build_discovery_query(hint: ProductIdentityHint) -> str:
    parts = [part for part in (hint.brand, hint.model) if part]
    if not parts and hint.name:
        parts.append(hint.name)
    parts.extend(hint.keywords[:1])
    return " ".join(parts).strip() or hint.name


_MARKETPLACE_MARKERS = ("shopee", "tiktok", "lazada", "amazon", "tiki", "shop.", "usercontent", "tikicdn", "lazcdn")
_RETAILER_MARKERS = ("dienmayxanh", "thegioididong", "fptshop", "cellphones", "meta.vn", "dmx", "tgdd", "bcd", "mediamart", "retailer")
_REVIEW_MARKERS = ("review", "blog", "forum", "vnreview", "znews", "kenh14", "dantri", "24h.com.vn", "baomoi", "cafebiz")


def classify_source(candidate: SourceCandidate, official_domains: set[str]) -> SourceCandidate:
    host = candidate.host.casefold()
    if host and (host in official_domains or any(host.endswith("." + domain) for domain in official_domains)):
        candidate.source_class = SourceClass.OFFICIAL
    elif any(marker in host for marker in _MARKETPLACE_MARKERS):
        candidate.source_class = SourceClass.MARKETPLACE
    elif any(marker in host for marker in _REVIEW_MARKERS):
        candidate.source_class = SourceClass.REVIEW
    elif any(marker in host for marker in _RETAILER_MARKERS):
        candidate.source_class = SourceClass.RETAILER
    else:
        candidate.source_class = SourceClass.UNKNOWN
    return candidate


def host_of(url: str) -> str:
    return urlsplit(url).netloc.casefold().removeprefix("www.") or ""
