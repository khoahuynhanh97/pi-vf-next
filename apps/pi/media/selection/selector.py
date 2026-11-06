"""Selection of the most useful product assets."""

from __future__ import annotations

from ..models import MatchStatus, MediaAsset


class ProductAssetSelector:
    """Picks distinct, well-matched, technically solid assets.

    Prefers high product match, then technical quality, then distinct
    duplicate groups and distinct views. Never reaches a quota with low-
    confidence images; short coverage is reported as incomplete instead.
    """

    def __init__(self, target_assets: int = 4, minimum_match: float | None = None, allow_unknown: bool = True):
        self.target_assets = target_assets
        self.minimum_match = minimum_match
        self.allow_unknown = allow_unknown

    def select(self, assets: list[MediaAsset]) -> tuple[list[MediaAsset], dict]:
        scored = []
        for asset in assets:
            assessment = asset.assessment
            match_status = assessment.match_status

            # Disqualify MISMATCH completely
            if match_status == MatchStatus.MISMATCH:
                continue

            # UNKNOWN images cannot fill quota unless explicitly allowed as reference
            if match_status == MatchStatus.UNKNOWN and not self.allow_unknown:
                continue

            match_score = assessment.product_match_score
            if self.minimum_match is not None and match_score is not None and match_score < self.minimum_match:
                continue
            selection_score = _selection_score(assessment.product_match_score, assessment.technical_quality, assessment.match_status)
            asset.assessment.selection_score = round(selection_score, 3)
            scored.append((selection_score, asset))
        scored.sort(key=lambda item: (-item[0], item[1].asset_id))

        selected: list[MediaAsset] = []
        seen_groups: set[str] = set()
        matched_count = 0
        for _, asset in scored:
            group = asset.assessment.duplicate_group_id
            if group is not None and group in seen_groups:
                continue
            seen_groups.add(group or asset.asset_id)
            selected.append(asset)
            if asset.assessment.match_status in (MatchStatus.MATCHED, MatchStatus.NEEDS_SEMANTIC_REVIEW):
                matched_count += 1
            if len(selected) >= self.target_assets:
                break

        is_complete = (matched_count >= self.target_assets)
        coverage = {
            "target": self.target_assets,
            "selected": len(selected),
            "matched_count": matched_count,
            "complete": is_complete,
            "distinct_groups": len(seen_groups),
        }
        if not is_complete:
            if matched_count == 0:
                coverage["shortfall_reason"] = "no_matched_identity_assets"
            else:
                coverage["shortfall_reason"] = f"insufficient_matched_assets: {matched_count} < {self.target_assets}"

        return selected, coverage


def _selection_score(match_score: float | None, technical_quality: float | None, match_status: MatchStatus) -> float:
    quality = technical_quality if technical_quality is not None else 0.5
    match = match_score if match_score is not None else 0.0
    if match_status == MatchStatus.UNKNOWN:
        match = min(match, 0.4)  # uncertain matches must not outrank known ones
    return round(0.6 * match + 0.4 * quality, 3)
