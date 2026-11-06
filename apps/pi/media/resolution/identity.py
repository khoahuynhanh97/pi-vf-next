"""Deterministic identity matching between two ProductEvidence objects."""

from __future__ import annotations

from media.evidence.models import ProductEvidence

from .models import IdentityMatchStatus
from .normalize import (
    normalize_brand,
    normalize_gtin,
    normalize_identity,
    normalize_model,
)


class EvidenceIdentityKey:
    """Normalized strong-identity key used for grouping and stable IDs."""

    def __init__(self, brand: str = "", model: str = "", gtin: str = ""):
        self.brand = normalize_brand(brand)
        self.model = normalize_model(model)
        self.gtin = normalize_gtin(gtin)

    @classmethod
    def from_evidence(cls, evidence: ProductEvidence) -> EvidenceIdentityKey:
        identity = evidence.identity
        return cls(
            brand=identity.brand.value if identity.brand else "",
            model=identity.model.value if identity.model else "",
            gtin=identity.gtin.value if identity.gtin else "",
        )

    @property
    def has_model(self) -> bool:
        return bool(self.model)

    @property
    def has_gtin(self) -> bool:
        return bool(self.gtin)

    def describe(self) -> str:
        parts = [part for part in (self.brand, self.model) if part]
        return " ".join(parts)


def match_identity(left: ProductEvidence, right: ProductEvidence) -> IdentityMatchResult:
    left_key = EvidenceIdentityKey.from_evidence(left)
    right_key = EvidenceIdentityKey.from_evidence(right)
    positive: list[str] = []
    negative: list[str] = []
    supporting: list[str] = [left.evidence_id, right.evidence_id]

    # GTIN: strong identifier, conflicts are strong negative.
    if left_key.has_gtin and right_key.has_gtin:
        if left_key.gtin == right_key.gtin:
            positive.append("gtin_match")
        else:
            return IdentityMatchResult(IdentityMatchStatus.DIFFERENT_PRODUCT, positive, ["gtin_conflict"], supporting, "conflicting explicit GTIN")
    if left_key.has_gtin != right_key.has_gtin:
        positive.append("gtin_single_sided")

    # Model: strong identifier, conflicts are strong negative.
    if left_key.has_model and right_key.has_model:
        if left_key.model == right_key.model:
            positive.append("model_match")
        else:
            return IdentityMatchResult(IdentityMatchStatus.DIFFERENT_PRODUCT, positive, ["model_conflict"], supporting, "conflicting explicit model")

    # Brand: presence on both sides helps but a mismatch alone is not decisive.
    if left_key.brand and right_key.brand:
        if left_key.brand == right_key.brand:
            positive.append("brand_match")
        else:
            positive.append("brand_conflict")

    # Name fallback only when both lack a model.
    if not left_key.has_model and not right_key.has_model:
        left_name = normalize_identity(left.identity.name.value if left.identity.name else "")
        right_name = normalize_identity(right.identity.name.value if right.identity.name else "")
        if left_name and right_name and _exact_or_contained(left_name, right_name):
            positive.append("name_match")

    if not positive and not negative:
        return IdentityMatchResult(IdentityMatchStatus.UNRESOLVED, positive, negative, supporting, "no identity signals")

    if negative:
        return IdentityMatchResult(IdentityMatchStatus.DIFFERENT_PRODUCT, positive, negative, supporting, "negative identity signal")

    if "gtin_match" in positive or ("model_match" in positive and "brand_match" in positive):
        status = IdentityMatchStatus.SAME_PRODUCT
    elif "model_match" in positive or "gtin_single_sided" in positive or "brand_match" in positive and "name_match" in positive:
        status = IdentityMatchStatus.LIKELY_SAME_PRODUCT
    else:
        status = IdentityMatchStatus.UNRESOLVED
    return IdentityMatchResult(status, positive, negative, supporting, "compatible identity signals")


def _exact_or_contained(left: str, right: str) -> bool:
    if left == right:
        return True
    shorter = left if len(left) <= len(right) else right
    if (len(shorter) >= 10 and len(shorter.split()) >= 3) and (left in right or right in left):
        return not _has_differentiator(left, right)
    return False


def _has_differentiator(left: str, right: str) -> bool:
    """True when one name is contained in the other but the longer one adds a
    variant/differentiator token (Pro, Plus, Max, ...). Substring containment
    alone must never merge materially different products."""
    left_tokens = set(left.casefold().split())
    right_tokens = set(right.casefold().split())
    shorter_tokens = left_tokens if len(left) <= len(right) else right_tokens
    longer_tokens = right_tokens if len(left) <= len(right) else left_tokens
    extra = longer_tokens - shorter_tokens
    return bool(extra & _DIFFERENTIATOR_TOKENS)


_DIFFERENTIATOR_TOKENS = {
    "pro", "plus", "max", "ultra", "mini", "lite", "air", "se", "x", "s",
    "5g", "4g", "nfc", "2k", "4k", "2026", "2025", "2024", "aio", "turbo",
}


class IdentityMatchResult:
    __slots__ = ("negative_signals", "positive_signals", "reason", "status", "supporting_evidence_ids")

    def __init__(self, status, positive_signals, negative_signals, supporting_evidence_ids, reason):
        self.status = status
        self.positive_signals = positive_signals
        self.negative_signals = negative_signals
        self.supporting_evidence_ids = supporting_evidence_ids
        self.reason = reason
