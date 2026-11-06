"""Deterministic scoring engine for trend strength, confidence, and freshness."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from product_scout.daily_parser import ParsedCriteria

from .config import TrendDiscoveryConfig
from .models import (
    AggregatedCandidate,
    CandidateObservation,
    PreviousTrendSignal,
    ProductHistory,
    ScoreBreakdown,
    ScoredCandidate,
)
from .tiktok_category import has_complete_tiktok_listing


def recency_points(obs: CandidateObservation, now: datetime) -> float:
    """Calculate recency score points based on observation publication or observation age."""
    if obs.published_at:
        age_days = (now - obs.published_at).total_seconds() / 86400.0
        if age_days <= 1.0:
            return 25.0
        elif age_days <= 3.0:
            return 20.0
        elif age_days <= 7.0:
            return 15.0
        elif age_days <= 14.0:
            return 10.0
        return 0.0
    # Published date missing: fallback to observed_at with lower weight
    obs_age_days = (now - obs.observed_at).total_seconds() / 86400.0
    if obs_age_days <= 1.0:
        return 5.0
    return 0.0


def calculate_trend_score(
    candidate: AggregatedCandidate,
    previous_signal: PreviousTrendSignal | None,
    now: datetime,
) -> tuple[float, ScoreBreakdown, int]:
    """Calculate trend score (0-100) reflecting external evidence strength."""
    unique_hosts = {obs.source_host for obs in candidate.observations if obs.source_host}
    independent_host_count = len(unique_hosts)

    source_diversity = min(30.0, independent_host_count * 10.0)
    recency = max((recency_points(row, now) for row in candidate.observations), default=0.0)

    positive_ranks = [row.source_rank for row in candidate.observations if row.source_rank > 0]
    min_positive_rank = min(positive_ranks) if positive_ranks else 21
    search_rank = max(0.0, 21.0 - min_positive_rank)

    if previous_signal:
        source_count_delta = independent_host_count - previous_signal.independent_source_count
        rank_improvement = (
            (previous_signal.min_source_rank - min_positive_rank)
            if (previous_signal.min_source_rank and positive_ranks)
            else 0
        )
        velocity = min(15.0, max(0.0, source_count_delta * 5.0 + float(rank_improvement)))
    else:
        velocity = 0.0

    commerce = 10.0 if any(row.signal_type == "commerce_listing" for row in candidate.observations) else 0.0

    trend_score = min(100.0, source_diversity + recency + search_rank + velocity + commerce)

    breakdown = ScoreBreakdown(
        source_diversity=source_diversity,
        recency=recency,
        search_rank=search_rank,
        velocity=velocity,
        commerce=commerce,
    )
    return trend_score, breakdown, independent_host_count


def confidence_score(
    candidate: AggregatedCandidate,
    criteria: ParsedCriteria,
) -> tuple[float, ScoreBreakdown]:
    """Measure product identity quality and alignment with request criteria (0-100)."""
    has_brand = bool(candidate.brand.strip())
    has_model = bool(candidate.model.strip())

    # Check for concrete product terms in candidate name (category-aware)
    cand_name_lower = candidate.name.lower()
    is_food = any(
        term in criteria.category.lower()
        for term in (
            "thực phẩm", "thuc pham", "đồ ăn", "do an", "đồ uống", "do uong",
            "ăn vặt", "an vat", "snack", "bánh kẹo", "banh keo", "nước giải khát",
            "nuoc giai khat", "nước ngọt", "nuoc ngot", "trà", "tra", "cà phê",
            "ca phe", "đóng chai", "dong chai", "đóng gói", "dong goi",
        )
    )
    if is_food:
        has_concrete_product = any(
            kw in cand_name_lower
            for kw in (
                "snack", "bánh", "banh", "kẹo", "keo", "trà", "tra", "cà phê", "ca phe",
                "nước ngọt", "nuoc ngot", "nước giải khát", "nuoc giai khat", "xúc xích", "xuc xich",
                "mì gói", "mi goi", "mì", "mi", "khô gà", "kho ga", "khô bò", "kho bo",
                "rong biển", "rong bien", "khoai tây", "khoai tay", "hạt điều", "hat dieu",
                "đậu phộng", "dau phong", "thạch", "thach", "sữa chua", "sua chua", "ngũ cốc",
            )
        )
    else:
        has_concrete_product = any(
            kw in cand_name_lower
            for kw in (
                "tai nghe", "chuot", "chuột", "ban phim", "bàn phím", "cu sac", "củ sạc",
                "day sac", "dây sạc", "cap sac", "cáp sạc", "pin du phong", "pin dự phòng",
                "loa", "smartwatch", "dong ho", "đồng hồ", "robot", "hut bui", "hút bụi",
                "camera", "wifi", "man hinh", "màn hình", "quat", "quạt", "hub", "sac", "sạc",
                "ban chai dien", "bàn chải điện", "may cao rau", "máy cạo râu",
            )
        )

    brand_pts = 25.0 if has_brand else 0.0
    if has_model:
        model_pts = 25.0
    elif has_brand and has_concrete_product:
        model_pts = 15.0
    else:
        model_pts = 0.0

    identity_pts = brand_pts + model_pts
    if not has_brand and not has_model and has_complete_tiktok_listing(candidate):
        identity_pts = 35.0
    evidence_agreement = min(20.0, len(candidate.observations) * 10.0)

    # Price fit
    is_price_unconstrained = (criteria.price_min == 0 and criteria.price_max == 0)
    if is_price_unconstrained:
        price_fit = 15.0
    elif candidate.price_min is not None or candidate.price_max is not None:
        p_min = candidate.price_min if candidate.price_min is not None else 0
        p_max = candidate.price_max if candidate.price_max is not None else 100_000_000
        if p_min <= criteria.price_max and p_max >= criteria.price_min:
            price_fit = 15.0
        else:
            price_fit = 0.0
    else:
        price_fit = 5.0

    # Category fit
    cand_cat = (candidate.category or "").lower()
    if cand_cat == "conflict":
        category_fit = 0.0
    else:
        cat_target = criteria.category.lower()
        if any(term in cand_cat or term in cand_name_lower for term in cat_target.split()) or has_concrete_product:
            category_fit = 10.0
        else:
            category_fit = 5.0

    has_conflict = bool(candidate.identity_conflicts) or (cand_cat == "conflict")
    conflict_free = 5.0 if not has_conflict else 0.0

    total_confidence = min(
        100.0, identity_pts + evidence_agreement + price_fit + category_fit + conflict_free
    )
    breakdown = ScoreBreakdown(
        identity=identity_pts,
        evidence_agreement=evidence_agreement,
        price_fit=price_fit,
        category_fit=category_fit,
        conflict_free=conflict_free,
    )
    return total_confidence, breakdown


def calculate_freshness_score(
    history: ProductHistory | None,
    now: datetime,
    cooldown_days: int = 30,
) -> float:
    """Calculate freshness score (0-100) based on selection history."""
    if history is None or history.last_selected_at is None or history.selection_count == 0:
        return 100.0

    days_since_selected = (now - history.last_selected_at).total_seconds() / 86400.0
    if days_since_selected < cooldown_days:
        return 0.0

    # Outside cooldown: recovers gradually, penalizes high total selection count
    bonus = min(50.0, (days_since_selected - cooldown_days) * 2.0)
    penalty = history.selection_count * 5.0
    return min(100.0, max(0.0, 50.0 + bonus - penalty))


def score_candidate(
    candidate: AggregatedCandidate,
    history: ProductHistory | None,
    previous_signal: PreviousTrendSignal | None,
    *,
    now: datetime,
    criteria: ParsedCriteria,
    config: TrendDiscoveryConfig,
) -> ScoredCandidate:
    """Compute trend, confidence, freshness, and final combined score for a candidate."""
    trend, trend_breakdown, ind_sources = calculate_trend_score(candidate, previous_signal, now)
    conf, conf_breakdown = confidence_score(candidate, criteria)
    freshness = calculate_freshness_score(history, now, config.cooldown_days)

    final = round(
        config.trend_weight * trend
        + config.freshness_weight * freshness
        + config.confidence_weight * conf,
        2,
    )

    combined_breakdown = ScoreBreakdown(
        source_diversity=trend_breakdown.source_diversity,
        recency=trend_breakdown.recency,
        search_rank=trend_breakdown.search_rank,
        velocity=trend_breakdown.velocity,
        commerce=trend_breakdown.commerce,
        identity=conf_breakdown.identity,
        evidence_agreement=conf_breakdown.evidence_agreement,
        price_fit=conf_breakdown.price_fit,
        category_fit=conf_breakdown.category_fit,
        conflict_free=conf_breakdown.conflict_free,
    )

    return ScoredCandidate(
        candidate=candidate,
        trend_score=trend,
        freshness_score=freshness,
        confidence_score=conf,
        final_score=final,
        previous_trend_score=previous_signal.trend_score if previous_signal else None,
        independent_source_count=ind_sources,
        score_breakdown=combined_breakdown,
    )
