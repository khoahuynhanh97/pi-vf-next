"""Stable deterministic identifier generation.

Canonical product IDs hash normalized strong identity (brand + model + gtin).
Variant IDs hash canonical id + normalized explicit variant dimensions.
Listing IDs hash the canonical source URL; price is never part of a listing ID.
"""

from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit

from media.evidence.models import ProductEvidence

from .normalize import (
    normalize_brand,
    normalize_gtin,
    normalize_model,
    normalize_variant_value,
)


def _digest(value: str, prefix: str, length: int = 12) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:length]}"


def canonical_product_id(evidence: ProductEvidence) -> str | None:
    identity = evidence.identity
    brand = normalize_brand(identity.brand.value if identity.brand else "")
    model = normalize_model(identity.model.value if identity.model else "")
    gtin = normalize_gtin(identity.gtin.value if identity.gtin else "")
    if model:
        return _digest(f"{brand}|{model}|{gtin}", "product")
    if gtin:
        return _digest(f"gtin:{gtin}", "product")
    return None


def variant_dimensions(evidence: ProductEvidence) -> dict[str, str]:
    identity = evidence.identity
    dimensions: dict[str, str] = {}
    variant = identity.variant.value if identity.variant else ""
    if variant:
        dimensions["variant"] = normalize_variant_value(variant)
    for key in ("color", "size", "capacity", "memory", "configuration", "region", "material", "edition"):
        # ProductEvidence does not carry these explicitly yet; the extractor
        # maps them into identity.variant. Kept here for forward compatibility.
        pass
    return dimensions


def variant_id(canonical_product_id: str, dimensions: dict[str, str]) -> str | None:
    if not dimensions:
        return None
    payload = json.dumps({key: dimensions[key] for key in sorted(dimensions)}, sort_keys=True)
    return _digest(f"{canonical_product_id}|{payload}", "variant")


def listing_id(evidence: ProductEvidence) -> str:
    url = (evidence.source.canonical_url or evidence.source.final_url or evidence.source.requested_url or "").strip()
    return _digest(url, "listing")


def host_of(url: str) -> str | None:
    return urlsplit(url).netloc.casefold().removeprefix("www.") or None
