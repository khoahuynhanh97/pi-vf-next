from __future__ import annotations

from datetime import datetime, timezone

from product_scout.trend_discovery.config import TrendDiscoveryConfig
from product_scout.trend_discovery.models import (
    AggregatedCandidate,
    ScoreBreakdown,
    ScoredCandidate,
)
from product_scout.trend_discovery.selection import PortfolioSelector

FIXED_NOW = datetime(2026, 9, 18, 7, 0, 0, tzinfo=timezone.utc)
CONFIG = TrendDiscoveryConfig()


def make_scored(
    canonical_key: str,
    trend_score: float = 70.0,
    freshness_score: float = 100.0,
    confidence_score: float = 80.0,
    final_score: float = 75.0,
    independent_sources: int = 2,
    previous_trend_score: float | None = None,
) -> ScoredCandidate:
    candidate = AggregatedCandidate(
        canonical_key=canonical_key,
        name=canonical_key.replace("|", " ").title(),
        brand=canonical_key.split("|")[0].title() if "|" in canonical_key else "",
        model=canonical_key.split("|")[1] if "|" in canonical_key else "",
    )
    return ScoredCandidate(
        candidate=candidate,
        trend_score=trend_score,
        freshness_score=freshness_score,
        confidence_score=confidence_score,
        final_score=final_score,
        previous_trend_score=previous_trend_score,
        independent_source_count=independent_sources,
        score_breakdown=ScoreBreakdown(),
    )


def test_selector_reserves_seven_fresh_and_caps_reentry():
    # 8 fresh products
    rows = [
        make_scored(f"brand|new-{i}", freshness_score=100.0, final_score=70.0 + i)
        for i in range(8)
    ]
    # 5 re-emerging products (fresh=0, trend=90, sources=2)
    rows += [
        make_scored(
            f"brand|old-{i}",
            freshness_score=0.0,
            trend_score=90.0,
            previous_trend_score=60.0,
            independent_sources=2,
            final_score=95.0 - i,
        )
        for i in range(5)
    ]
    result = PortfolioSelector(CONFIG).select(rows, limit=10, now=FIXED_NOW)
    assert len(result.selected) == 10
    statuses = list(result.statuses.values())
    assert statuses.count("new") >= 7
    assert statuses.count("re_emerging") <= 3


def test_selector_returns_short_report_when_fresh_quota_is_missing():
    # Only 5 fresh products
    rows = [
        make_scored(f"brand|new-{i}", freshness_score=100.0, final_score=80.0)
        for i in range(5)
    ]
    # 3 old products
    rows += [
        make_scored(
            f"brand|old-{i}",
            freshness_score=0.0,
            trend_score=95.0,
            previous_trend_score=60.0,
            independent_sources=3,
            final_score=95.0,
        )
        for i in range(3)
    ]
    result = PortfolioSelector(CONFIG).select(rows, limit=10, now=FIXED_NOW)
    # Should not take old products to fill the quota
    assert len(result.selected) == 5
    assert result.warnings == ["insufficient_fresh_candidates"]
    assert all(status == "new" for status in result.statuses.values())


def test_selector_rejects_reentry_with_single_source():
    rows = [
        make_scored(f"brand|new-{i}", freshness_score=100.0, final_score=70.0 + i)
        for i in range(7)
    ]
    # Old product with only 1 source, even though trend=90
    rows.append(
        make_scored(
            "brand|old-single-source",
            freshness_score=0.0,
            trend_score=90.0,
            independent_sources=1,
            final_score=95.0,
        )
    )
    result = PortfolioSelector(CONFIG).select(rows, limit=10, now=FIXED_NOW)
    assert "brand|old-single-source" not in result.statuses


def test_selector_rejects_confidence_below_60():
    rows = [
        make_scored("brand|low-conf", freshness_score=100.0, confidence_score=55.0, final_score=90.0),
        make_scored("brand|good-conf", freshness_score=100.0, confidence_score=65.0, final_score=70.0),
    ]
    result = PortfolioSelector(CONFIG).select(rows, limit=10, now=FIXED_NOW)
    selected_keys = [s.candidate.canonical_key for s in result.selected]
    assert "brand|low-conf" not in selected_keys
    assert "brand|good-conf" in selected_keys


def test_selector_rejects_unusable_product_identities():
    rows = [
        make_scored("huawei|gt-2"),
        make_scored("baseus|150k"),
        make_scored("xiaomi|17"),
    ]
    rows[0].candidate.name = "shopee.vn/Huawei-Watch-GT-2"
    rows[1].candidate.name = "Đèn Baseus Sunshine"
    rows[2].candidate.name = "Sản phẩm công nghệ mới ra mắt tháng 2 và tháng 3"
    result = PortfolioSelector(CONFIG).select(rows, limit=10, now=FIXED_NOW)
    assert result.selected == []
    assert all(
        result.reason_codes[row.candidate.canonical_key] == ["invalid_product_identity"]
        for row in rows
    )


def test_selector_respects_custom_limit():
    # limit = 5 -> fresh_quota = ceil(5 * 0.7) = 4, max re-emerging = 1
    rows = [
        make_scored(f"brand|new-{i}", freshness_score=100.0, final_score=70.0 + i)
        for i in range(4)
    ]
    rows += [
        make_scored(
            f"brand|old-{i}",
            freshness_score=0.0,
            trend_score=85.0,
            independent_sources=2,
            final_score=85.0,
        )
        for i in range(3)
    ]
    result = PortfolioSelector(CONFIG).select(rows, limit=5, now=FIXED_NOW)
    assert len(result.selected) == 5
    statuses = list(result.statuses.values())
    assert statuses.count("new") == 4
    assert statuses.count("re_emerging") == 1


def test_selector_prefers_brand_diversity_when_multiple_brands_available():
    # 5 items from BrandA with slightly higher score, plus 6 items from BrandB..BrandG
    rows = [
        make_scored(f"branda|model-{i}", freshness_score=100.0, final_score=90.0 - i * 0.5)
        for i in range(5)
    ] + [
        make_scored(f"brand{chr(98 + i)}|model-1", freshness_score=100.0, final_score=85.0 - i)
        for i in range(6)
    ]
    result = PortfolioSelector(CONFIG).select(rows, limit=7, now=FIXED_NOW)
    assert len(result.selected) == 7
    brands = [s.candidate.brand.lower() for s in result.selected]
    # BrandA should be capped at 2 since plenty of other eligible fresh brands exist
    assert brands.count("branda") == 2
    assert len(set(brands)) >= 6
