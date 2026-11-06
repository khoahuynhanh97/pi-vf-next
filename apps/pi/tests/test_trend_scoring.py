from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from product_scout.daily_parser import ParsedCriteria
from product_scout.trend_discovery.config import TrendDiscoveryConfig
from product_scout.trend_discovery.models import (
    AggregatedCandidate,
    CandidateObservation,
    PreviousTrendSignal,
    ProductHistory,
)
from product_scout.trend_discovery.scoring import confidence_score, score_candidate

FIXED_NOW = datetime(2026, 9, 18, 7, 0, 0, tzinfo=timezone.utc)
CONFIG = TrendDiscoveryConfig()
CRITERIA = ParsedCriteria(
    raw_prompt="test",
    limit=10,
    category="công nghệ",
    price_min=200_000,
    price_max=1_000_000,
)


def make_candidate(
    name: str = "Test Product",
    brand: str = "TestBrand",
    model: str = "ModelX",
    category: str = "công nghệ",
    sources: list[str] | None = None,
    ranks: list[int] | None = None,
    published_days_ago: int | None = None,
    has_commerce: bool = False,
) -> AggregatedCandidate:
    sources = sources or ["https://vnexpress.net/prod1"]
    ranks = ranks or [1] * len(sources)
    obs_list = []
    pub_at = (
        FIXED_NOW - timedelta(days=published_days_ago)
        if published_days_ago is not None
        else None
    )
    for idx, url in enumerate(sources):
        host = url.split("/")[2]
        signal = "commerce_listing" if has_commerce and idx == 0 else "web_mention"
        obs_list.append(
            CandidateObservation(
                provider="web_search",
                candidate_name=name,
                brand=brand,
                model=model,
                source_url=url,
                source_host=host,
                source_rank=ranks[idx] if idx < len(ranks) else 1,
                observed_at=FIXED_NOW,
                published_at=pub_at,
                signal_type=signal,
            )
        )
    return AggregatedCandidate(
        canonical_key=f"{brand.lower()}|{model.lower()}",
        name=name,
        brand=brand,
        model=model,
        category=category,
        price_min=300_000,
        price_max=500_000,
        observations=obs_list,
    )


def test_final_score_uses_approved_weights():
    cand = make_candidate(sources=["https://a.test/1", "https://b.test/2", "https://c.test/3"])
    history = None  # fresh -> 100
    scored = score_candidate(
        cand, history, None, now=FIXED_NOW, criteria=CRITERIA, config=CONFIG
    )
    # final_score = 0.55 * trend + 0.25 * freshness + 0.20 * confidence
    expected = round(0.55 * scored.trend_score + 0.25 * 100.0 + 0.20 * scored.confidence_score, 2)
    assert scored.final_score == expected


def test_recent_selection_has_zero_freshness():
    cand = make_candidate()
    history = ProductHistory(
        canonical_key=cand.canonical_key,
        canonical_name=cand.name,
        brand=cand.brand,
        model=cand.model,
        first_seen_at=FIXED_NOW - timedelta(days=60),
        last_seen_at=FIXED_NOW - timedelta(days=29),
        last_selected_at=FIXED_NOW - timedelta(days=29),
        selection_count=1,
    )
    scored = score_candidate(
        cand, history, None, now=FIXED_NOW, criteria=CRITERIA, config=CONFIG
    )
    assert scored.freshness_score == 0.0


def test_cooldown_boundary_days():
    cand = make_candidate()
    # 31 days ago -> outside cooldown
    history_31 = ProductHistory(
        canonical_key=cand.canonical_key,
        canonical_name=cand.name,
        brand=cand.brand,
        model=cand.model,
        first_seen_at=FIXED_NOW - timedelta(days=60),
        last_seen_at=FIXED_NOW - timedelta(days=31),
        last_selected_at=FIXED_NOW - timedelta(days=31),
        selection_count=1,
    )
    scored_31 = score_candidate(
        cand, history_31, None, now=FIXED_NOW, criteria=CRITERIA, config=CONFIG
    )
    assert scored_31.freshness_score > 0.0


def test_confidence_boundary_is_eligible():
    cand = make_candidate(brand="Anker", model="Nano", sources=["https://a.test/1", "https://b.test/2"])
    conf, breakdown = confidence_score(cand, CRITERIA)
    assert conf >= 60.0
    assert breakdown.identity == 50.0  # 25 brand + 25 model


def test_confidence_price_out_of_budget_penalized():
    cand = make_candidate(brand="Apple", model="MacBook Pro")
    cand.price_min = 30_000_000
    cand.price_max = 45_000_000
    conf, breakdown = confidence_score(cand, CRITERIA)
    assert breakdown.price_fit == 0.0


def test_confidence_category_conflict_penalized():
    cand = make_candidate(name="Bánh thuẫn thơm ngon đậm vị Tết", brand="", model="", category="conflict")
    conf, breakdown = confidence_score(cand, CRITERIA)
    assert breakdown.category_fit == 0.0
    assert conf < 60.0


def test_confidence_generic_article_rejected():
    cand = make_candidate(name="12 video trends shaping 2026: social media video", brand="", model="", category="")
    conf, breakdown = confidence_score(cand, CRITERIA)
    assert breakdown.identity == 0.0
    assert conf < 60.0
