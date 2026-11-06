"""Deterministic product-image matching from text metadata.

This is an extensible matcher architecture. A future VisionProductImageMatcher
may use embeddings; this milestone only implements deterministic text matching.
"""

from __future__ import annotations

import re

from ..models import (
    ImageCandidate,
    MatchStatus,
    ProductIdentityHint,
    ProductMatchResult,
)


def _tokens(value) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", str(value or "").casefold()) if len(token) > 1}


KNOWN_FLAVORS = (
    "sườn nướng", "suon nuong", "tự nhiên", "tu nhien", "tảo biển", "tao bien",
    "mực cay", "muc cay", "phô mai", "pho mai", "bò nướng", "bo nuong",
    "bít tết", "bit tet", "dưa leo", "dua leo", "kem chua hành", "kem chua hanh",
    "gà cay", "ga cay", "tôm chua cay", "tom chua cay", "bò hầm", "bo ham",
    "sườn hầm", "suon ham", "dâu tây", "dau tay", "dưa hấu", "dua hau",
    "bạc hà", "bac ha", "không đường", "khong duong", "có đường", "co duong",
    "vị dâu", "vi dau", "vị đào", "vi dao", "vị chanh", "vi chanh", "mật ong",
)

NON_SKU_PATTERNS = re.compile(
    r"\b(?:cach lam|cách làm|huong dan nau|hướng dẫn nấu|cong thuc|công thức|"
    r"quan an|quán ăn|via he|vỉa hè|tu lam|tự làm|nau tai nha|nấu tại nhà|"
    r"banner|voucher|logo|combo \d+ món khác loại)\b",
    re.IGNORECASE,
)


class DeterministicProductImageMatcher:
    """Matches a candidate to a product identity using textual signals only.

    When a candidate carries no reliable textual metadata (DDG image rows often
    have none), the match is honestly reported as ``unknown`` rather than
    fabricating confidence.
    """

    def __init__(self, review_threshold: float = 0.5):
        self.review_threshold = review_threshold

    def match(self, candidate: ImageCandidate, hint: ProductIdentityHint, title: str = "") -> ProductMatchResult:
        brand = (hint.brand or "").casefold().strip()
        model = (hint.model or "").casefold().strip()
        name_tokens = _tokens(hint.name)
        signals: dict[str, bool] = {}
        # Only independent search-result metadata counts as evidence; the
        # candidate query is derived from the hint itself and would be circular.
        haystack = f"{title} {candidate.source_page_url or ''}".casefold()

        if not haystack.strip():
            return ProductMatchResult(
                candidate_id=candidate.candidate_id,
                match_status=MatchStatus.UNKNOWN,
                reason="no search-result text metadata to match against",
            )

        # Reject recipes, unpackaged dishes, or banners
        if NON_SKU_PATTERNS.search(haystack):
            return ProductMatchResult(
                candidate_id=candidate.candidate_id,
                match_status=MatchStatus.MISMATCH,
                product_match_score=0.0,
                signals={"non_sku_or_recipe_conflict": False},
                reason="non-packaged dish, recipe, or banner detected",
            )

        if brand:
            signals["brand_match"] = brand in haystack or brand.replace(" ", "") in haystack.replace(" ", "")
        if model:
            signals["model_match"] = model in haystack or all(token in haystack for token in list(_tokens(model))[:3])
        if hint.name and name_tokens:
            signals["name_match"] = bool(name_tokens & _tokens(haystack))

        # Check flavor conflict for food products
        target_flavor = ""
        raw_flavor = (hint.flavor or "").casefold().strip()
        for fl in KNOWN_FLAVORS:
            if raw_flavor and fl in raw_flavor:
                target_flavor = fl
                break
            if model and fl in model:
                target_flavor = fl
                break
            if hint.name and fl in hint.name.casefold():
                target_flavor = fl
                break
        if not target_flavor and raw_flavor:
            target_flavor = raw_flavor

        if target_flavor:
            # Check if haystack mentions a conflicting known flavor and NOT the target flavor
            has_target = target_flavor in haystack
            conflicting_flavor = None
            for fl in KNOWN_FLAVORS:
                if fl != target_flavor and len(fl) >= 4 and fl in haystack:
                    if not has_target:
                        conflicting_flavor = fl
                        break
            if conflicting_flavor:
                return ProductMatchResult(
                    candidate_id=candidate.candidate_id,
                    match_status=MatchStatus.MISMATCH,
                    product_match_score=0.0,
                    signals={"flavor_match": False},
                    reason=f"flavor conflict: expected {target_flavor}, found {conflicting_flavor}",
                )
            if has_target:
                signals["flavor_match"] = True

        if not signals:
            return ProductMatchResult(
                candidate_id=candidate.candidate_id,
                match_status=MatchStatus.UNKNOWN,
                reason="product identity has no matchable fields",
            )
        score = len([value for value in signals.values() if value]) / len(signals)
        if score >= self.review_threshold and all(signals.values()):
            status = MatchStatus.MATCHED
        elif score >= self.review_threshold:
            status = MatchStatus.NEEDS_SEMANTIC_REVIEW
        else:
            status = MatchStatus.MISMATCH
        return ProductMatchResult(
            candidate_id=candidate.candidate_id,
            match_status=status,
            product_match_score=round(score, 2),
            signals=signals,
            reason=status.value,
        )
