"""Deterministic source-candidate relevance assessment.

Uses only title/snippet/URL evidence against the product identity hint. No
LLMs, no embeddings. Relevance is evaluated BEFORE diversity selection so an
irrelevant source never outranks a relevant one just for host diversity.
"""

from __future__ import annotations

import re
import unicodedata

from media.models import ProductIdentityHint

from .models import SourceCandidate

# Tokens that mark a different model/edition when they follow a model token.
# Local copy of a generic differentiator safety utility; SourceDiscovery does
# not depend on ProductResolver internals.
_MODEL_DIFFERENTIATORS = {
    "pro", "plus", "max", "ultra", "mini", "lite", "air", "se", "x", "s",
    "5g", "4g", "nfc", "2k", "4k", "2026", "2025", "2024", "aio", "turbo",
}


class RelevanceLevel:
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.split()).strip()


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", _normalize(value)) if len(token) > 1}


def _model_is_exact(model: str, text_tokens: set[str], raw_text: str) -> bool:
    """Model token present AND not immediately followed by a differentiator."""
    if model not in text_tokens:
        return False
    normalized = _normalize(raw_text)
    pattern = re.compile(rf"\b{re.escape(model)}\s+({'|'.join(sorted(_MODEL_DIFFERENTIATORS))})\b")
    return not pattern.search(normalized)


def _model_present(model: str, text_tokens: set[str]) -> bool:
    return model in text_tokens


def assess_relevance(candidate: SourceCandidate, hint: ProductIdentityHint) -> tuple[str, str]:
    """Return (level, reason). HIGH/MEDIUM are eligible; LOW is filtered out."""
    brand = _normalize(hint.brand or "")
    model = _normalize(hint.model or "")
    name = _normalize(hint.name or "")

    text = f"{candidate.title} {candidate.snippet} {candidate.url}"
    tokens = _tokens(text)
    brand_token = _tokens(brand)
    model_token = _tokens(model)

    brand_present = bool(brand) and bool(brand_token & tokens)
    model_exact = bool(model) and _model_is_exact(model, tokens, text)
    model_present = bool(model) and _model_present(model, tokens)

    # Brand + exact model → strong relevance.
    if brand_present and model_exact:
        return RelevanceLevel.HIGH, "brand and exact model present"
    # Exact model alone → strong relevance.
    if model_exact:
        return RelevanceLevel.HIGH, "exact model present"
    # An explicit different model token (e.g. FA53 when target is FA55) is low:
    # it is a different product, not an acceptable weak fallback.
    if brand_present and _different_model_like_token(tokens, model_token):
        return RelevanceLevel.LOW, "explicit different model token present"
    # Brand + a model-family token (possibly a different edition like FA55 Pro)
    # is a moderate, non-exact signal.
    if brand_present and model_present:
        return RelevanceLevel.MEDIUM, "brand with model-family token (non-exact)"
    name_tokens = _tokens(name)
    # Brand + sufficiently specific normalized name.
    if brand_present and name_tokens:
        overlap = name_tokens - brand_token - model_token
        if len(overlap & tokens) >= 2:
            return RelevanceLevel.MEDIUM, "brand and name token overlap"
    # Brand-only weak result stays a fallback candidate.
    if brand_present:
        return RelevanceLevel.MEDIUM, "brand-only (weak)"
    # Name-only identity (no brand/model in the hint): match on the name tokens
    # themselves. Two+ overlapping tokens means the candidate names the product.
    if not brand and not model and name_tokens:
        overlap = name_tokens & tokens
        if len(overlap) >= 2:
            return RelevanceLevel.MEDIUM, "name token overlap (no brand/model)"
    return RelevanceLevel.LOW, "no target identity evidence"


_MODEL_LIKE_RE = re.compile(r"^[a-z]{2,}\d+[a-z0-9]*$")


def _different_model_like_token(tokens: set[str], model_token: set[str]) -> bool:
    for token in tokens:
        if token in model_token:
            continue
        if _MODEL_LIKE_RE.match(token):
            return True
    return False
