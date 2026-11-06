"""Portfolio selection policy enforcing freshness ratio (>=70% new) and re-entry gates."""

from __future__ import annotations

import math
from datetime import datetime

from .config import TrendDiscoveryConfig
from .identity import is_usable_product_identity
from .models import ScoredCandidate, SelectionResult
from .tiktok_category import has_complete_tiktok_listing


class PortfolioSelector:
    """Selects a balanced portfolio of fresh and re-emerging trending products."""

    def __init__(self, config: TrendDiscoveryConfig | None = None):
        self.config = config or TrendDiscoveryConfig()

    def select(
        self,
        candidates: list[ScoredCandidate],
        *,
        limit: int = 10,
        now: datetime,
    ) -> SelectionResult:
        fresh_quota = math.ceil(limit * self.config.fresh_ratio)
        max_reemerging = limit - fresh_quota

        reason_codes: dict[str, list[str]] = {}
        statuses: dict[str, str] = {}
        warnings: list[str] = []

        eligible_fresh: list[ScoredCandidate] = []
        eligible_reemerging: list[ScoredCandidate] = []

        # 1. Gate check: Confidence threshold and classify into fresh vs re-emerging
        for c in candidates:
            ck = c.candidate.canonical_key
            if not is_usable_product_identity(
                c.candidate.name, c.candidate.brand, c.candidate.model
            ) and not has_complete_tiktok_listing(c.candidate):
                reason_codes[ck] = ["invalid_product_identity"]
                continue
            if c.confidence_score < self.config.confidence_threshold:
                reason_codes[ck] = ["confidence_below_threshold"]
                continue

            if c.freshness_score > 0.0:
                eligible_fresh.append(c)
            else:
                # Re-entry criteria check
                reasons = []
                if c.independent_source_count < self.config.minimum_reentry_sources:
                    reasons.append("insufficient_reentry_sources")

                has_high_trend = c.trend_score >= self.config.reentry_trend_threshold
                has_trend_surge = (
                    c.previous_trend_score is not None
                    and (c.trend_score - c.previous_trend_score) >= self.config.reentry_delta_threshold
                )

                if not (has_high_trend or has_trend_surge):
                    reasons.append("insufficient_trend_surge")

                if reasons:
                    reason_codes[ck] = reasons
                else:
                    eligible_reemerging.append(c)

        # Sort order: (-final_score, -trend_score, canonical_key)
        def sort_key(c: ScoredCandidate) -> tuple[float, float, str]:
            return (-c.final_score, -c.trend_score, c.candidate.canonical_key)

        def diversify_by_brand(items: list[ScoredCandidate], max_per_brand: int = 2) -> list[ScoredCandidate]:
            primary: list[ScoredCandidate] = []
            overflow: list[ScoredCandidate] = []
            brand_counts: dict[str, int] = {}
            for item in items:
                b = item.candidate.brand.strip().casefold() or item.candidate.canonical_key
                cnt = brand_counts.get(b, 0)
                if cnt < max_per_brand:
                    primary.append(item)
                    brand_counts[b] = cnt + 1
                else:
                    overflow.append(item)
            return primary + overflow

        eligible_fresh.sort(key=sort_key)
        eligible_fresh = diversify_by_brand(eligible_fresh, max_per_brand=2)
        eligible_reemerging.sort(key=sort_key)
        eligible_reemerging = diversify_by_brand(eligible_reemerging, max_per_brand=2)

        selected: list[ScoredCandidate] = []

        total_eligible = len(eligible_fresh) + len(eligible_reemerging)

        # 2. Check fresh quota availability
        if len(eligible_fresh) < fresh_quota:
            # Under quota: do not backfill with old products! Short report with warning.
            warnings.append("insufficient_fresh_candidates")
            selected = eligible_fresh[:limit]
            for c in selected:
                ck = c.candidate.canonical_key
                statuses[ck] = "new"
                reason_codes[ck] = ["fresh_selected", "fresh_quota_shortfall"]
            return SelectionResult(
                selected=selected,
                statuses=statuses,
                reason_codes=reason_codes,
                warnings=warnings,
                eligible_count=total_eligible,
            )

        # 3. Reserve fresh quota
        reserved_fresh = eligible_fresh[:fresh_quota]
        leftover_fresh = eligible_fresh[fresh_quota:]

        for c in reserved_fresh:
            ck = c.candidate.canonical_key
            statuses[ck] = "new"
            reason_codes[ck] = ["fresh_quota_reserved"]

        selected.extend(reserved_fresh)

        # 4. Fill remaining positions up to limit (max re-emerging capped, brand diversity preserved)
        selected_brand_counts: dict[str, int] = {}
        for c in selected:
            b = c.candidate.brand.strip().casefold() or c.candidate.canonical_key
            selected_brand_counts[b] = selected_brand_counts.get(b, 0) + 1

        raw_pool = leftover_fresh + eligible_reemerging
        raw_pool.sort(key=sort_key)
        primary_pool: list[ScoredCandidate] = []
        overflow_pool: list[ScoredCandidate] = []
        for item in raw_pool:
            b = item.candidate.brand.strip().casefold() or item.candidate.canonical_key
            cnt = selected_brand_counts.get(b, 0)
            if cnt < 2:
                primary_pool.append(item)
                selected_brand_counts[b] = cnt + 1
            else:
                overflow_pool.append(item)
        pool = primary_pool + overflow_pool

        reemerging_chosen = 0
        for c in pool:
            if len(selected) >= limit:
                break
            ck = c.candidate.canonical_key
            is_old = c.freshness_score == 0.0

            if is_old:
                if reemerging_chosen >= max_reemerging:
                    continue
                reemerging_chosen += 1
                statuses[ck] = "re_emerging"
                surge_type = (
                    "trend_above_80"
                    if c.trend_score >= self.config.reentry_trend_threshold
                    else "trend_delta_surge"
                )
                reason_codes[ck] = ["reemerging_admitted", surge_type]
            else:
                statuses[ck] = "new"
                reason_codes[ck] = ["fresh_competitive_fill"]

            selected.append(c)

        return SelectionResult(
            selected=selected,
            statuses=statuses,
            reason_codes=reason_codes,
            warnings=warnings,
            eligible_count=total_eligible,
        )
