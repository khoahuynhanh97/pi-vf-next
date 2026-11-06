"""Classify marketplace links without treating search pages as product pages."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit, urlunsplit


def normalize_market_url(value: str) -> str:
    parts = urlsplit(value.strip())
    host = (parts.hostname or "").lower()
    allowed = any(host == domain or host.endswith("." + domain)
                  for domain in ("shopee.vn", "tiktok.com"))
    if (parts.scheme != "https" or not allowed or parts.username or parts.password
            or parts.port not in (None, 443)):
        raise ValueError("Use a public HTTPS Shopee Vietnam or TikTok URL.")
    return urlunsplit(("https", host, parts.path or "/", parts.query, ""))


def link_kind(value: str) -> str:
    try:
        parts = urlsplit(normalize_market_url(value))
    except (ValueError, AttributeError):
        return "unsupported"
    host, path = parts.hostname or "", parts.path.rstrip("/")
    if host.endswith("shopee.vn"):
        if host == "s.shopee.vn" and path:
            return "share"
        if re.fullmatch(r"/product/\d+/\d+", path) or re.search(r"-i\.\d+\.\d+$", path):
            return "direct"
        if path == "/search" or "keyword" in parse_qs(parts.query):
            return "search"
        if "-cat." in path or path.startswith(("/category/", "/mall")):
            return "category"
        return "shop" if path else "unsupported"
    if host in ("vt.tiktok.com", "vm.tiktok.com") and path:
        return "share"
    if path.startswith("/t/"):
        return "share"
    if re.search(r"/(?:pdp|product)/(?:[^/]+/)*\d+$", path):
        return "direct"
    if path.startswith("/search") or "/shop/s/" in path:
        return "search"
    if re.search(r"/(?:[a-z]{2}/)?c/", path):
        return "category"
    if "/store/" in path or "/shop/store" in path:
        return "shop"
    return "unsupported"


def direct_product_url(value: str) -> str:
    result = normalize_market_url(value)
    if link_kind(result) != "direct":
        raise ValueError("Select a direct product detail URL, not a search/shop/category/share link.")
    return result
