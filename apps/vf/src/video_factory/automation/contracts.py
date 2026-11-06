"""Data contracts, canonical product identity, and exchange manifest schemas."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any


def _slugify(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = raw.encode("ascii", "ignore").decode("ascii").lower()
    return "-".join(re.findall(r"[a-z0-9]+", ascii_value))


def compute_canonical_product_key(brand: str, model: str, variant: str | None = None) -> str:
    """Compute stable domain identity: canonical_product_key.

    Normalizes brand and model, eliminates redundant brand prefixes from model,
    and includes variant if present.
    Example:
      brand="Baseus", model="Bowie WM02" -> "baseus:bowie-wm02"
      brand="Ugreen", model="Uno Robot GaN", variant="30W" -> "ugreen:uno-robot-gan:30w"
    """
    b_slug = _slugify(brand)
    m_slug = _slugify(model)
    if not b_slug or not m_slug:
        raise ValueError(f"Brand and model required for canonical key: brand={brand!r}, model={model!r}")

    # Strip redundant brand prefix from model slug if present (e.g. baseus:baseus-wm02 -> baseus:wm02)
    if m_slug.startswith(f"{b_slug}-"):
        m_slug = m_slug[len(b_slug) + 1 :]

    if variant:
        v_slug = _slugify(variant)
        if v_slug:
            return f"{b_slug}:{m_slug}:{v_slug}"
    return f"{b_slug}:{m_slug}"


def compute_product_key(canonical_product_key: str) -> str:
    """Hash the canonical_product_key with SHA-256 for database primary key & workflow ID."""
    if not canonical_product_key:
        raise ValueError("canonical_product_key cannot be empty")
    return hashlib.sha256(canonical_product_key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExchangeImage:
    asset_id: str
    relative_path: str
    original_url: str | None = None
    match_score: float = 1.0
    width: int | None = None
    height: int | None = None
    format: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExchangeProduct:
    priority: int
    canonical_product_key: str
    product_key: str
    name: str
    brand: str
    model: str
    variant: str | None = None
    category: str = ""
    price_display: str = ""
    price_min: int | None = None
    price_max: int | None = None
    currency: str = "VND"
    hot_reason: str = ""
    key_features: str = ""
    keywords: list[str] = field(default_factory=list)
    images: list[ExchangeImage] = field(default_factory=list)
    source_product_id: str = ""
    source_url: str | None = None
    selection_reason: str | None = None
    # Links explaining why a product trended.  They are display-only leads and
    # must never be promoted to evidence for a product claim.
    trend_evidence: list[dict[str, Any]] = field(default_factory=list)
    identity_mode: str = "sku"
    source_snapshot: dict[str, Any] = field(default_factory=dict)
    creative_input: dict[str, Any] | None = None
    seller_description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExchangeManifest:
    schema_version: str
    batch_id: str
    production_date: str
    created_at: str
    products: list[ExchangeProduct] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CandidateItem:
    priority: int
    source_date: str
    source_batch_id: str
    source_product_id: str
    canonical_product_key: str
    product_key: str
    name: str
    brand: str
    model: str
    variant: str | None
    price_display: str
    price_min: int | None
    price_max: int | None
    hot_reason: str
    key_features: str
    keywords: list[str]
    images: list[dict[str, Any]]
    status: str  # UI: 'unprocessed' | 'invalid' | 'awaiting_product_lock' | 'in_production' | 'completed'
    internal_state: str  # DB/read model: 'discovered' | 'imported' | 'claimed' | 'started' | 'input_review_required' | 'awaiting_product_lock' | 'in_production' | 'completed' | 'invalid'
    rejection_reason: str | None = None
    workflow_id: str | None = None
    approval_id: str | None = None
    gate: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StartedProduct:
    product_key: str
    canonical_product_key: str
    workflow_id: str
    name: str

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SkippedProduct:
    product_key: str
    reason: str
    workflow_id: str | None = None
    error: str | None = None

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BatchStartResult:
    started: tuple[StartedProduct, ...]
    skipped: tuple[SkippedProduct, ...]

    @property
    def has_errors(self) -> bool:
        return any(
            item.reason.startswith(("CLAIM_ERROR", "START_FAILED", "ERROR", "BATCH_STOPPED"))
            for item in self.skipped
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "started": [s.to_dict() if hasattr(s, "to_dict") else dict(s) for s in self.started],
            "skipped": [s.to_dict() if hasattr(s, "to_dict") else dict(s) for s in self.skipped],
            "has_errors": self.has_errors,
        }

