"""Small local persistence helpers for the Spaiauto flow."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .links import link_kind, normalize_market_url

SPAIAUTO_URL = "https://tools.spaiauto.com/boc-tach-san-pham-shopee-tiktok"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bangkok_date() -> str:
    return datetime.now(timezone(timedelta(hours=7))).date().isoformat()


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".spaiauto-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def product_url(value: str) -> str:
    result = normalize_market_url(value)
    if link_kind(result) not in ("direct", "share"):
        raise ValueError("Search/shop/category URLs must be resolved to a product first; use start --file.")
    return result


def item_key(url: str) -> str:
    return hashlib.sha256(product_url(url).encode("utf-8")).hexdigest()[:16]
