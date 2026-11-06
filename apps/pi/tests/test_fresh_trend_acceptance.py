from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import pytest

from product_scout.daily_parser import ParsedCriteria
from product_scout.trend_discovery.config import TrendDiscoveryConfig
from product_scout.trend_discovery.models import (
    AggregatedCandidate,
    CandidateObservation,
)
from product_scout.trend_discovery.repository import TrendHistoryRepository
from product_scout.trend_discovery.service import TrendDiscoveryService

FIXED_NOW = datetime(2026, 9, 18, 7, 0, 0, tzinfo=timezone.utc)
CRITERIA = ParsedCriteria(
    raw_prompt="tìm 10 sản phẩm ngành công nghệ hot trend tiktok/shopee trong phạm vi giá từ 200k-1tr vnd",
    limit=10,
    category="công nghệ",
    price_min=200_000,
    price_max=1_000_000,
)
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "trend_candidates_30.json"


class FixtureCandidateProvider:
    name = "fixture_provider"

    def __init__(self, fixture_path: Path):
        self.fixture_path = fixture_path

    def discover(
        self,
        criteria: ParsedCriteria,
        *,
        run_at: datetime,
        limit: int,
    ) -> list[CandidateObservation]:
        data = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        res = []
        for row in data:
            pub_at = (
                datetime.fromisoformat(row["published_at"].replace("Z", "+00:00"))
                if row.get("published_at")
                else None
            )
            obs_at = (
                datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00"))
                if row.get("observed_at")
                else run_at
            )
            res.append(
                CandidateObservation(
                    provider=row.get("provider", self.name),
                    candidate_name=row["candidate_name"],
                    brand=row.get("brand", ""),
                    model=row.get("model", ""),
                    source_url=row["source_url"],
                    source_host=row["source_host"],
                    source_rank=row.get("source_rank", 1),
                    observed_at=obs_at,
                    published_at=pub_at,
                    signal_type=row.get("signal_type", "web_mention"),
                    title=row.get("title", row["candidate_name"]),
                    snippet=row.get("snippet", ""),
                )
            )
        return res


def test_daily_selection_acceptance(tmp_path: Path):
    repo = TrendHistoryRepository(tmp_path / "trend.db")

    # Seed history:
    # 1. Seed recent products within 30 days cooldown:
    # 'imou|ranger 2' and 'ezviz|c6n' were selected 10 days ago.
    # Because both have >=2 sources and high trend in fixture, they qualify as valid re-emerging!
    ten_days_ago = date(2026, 9, 8)
    repo.apply_backfill_selection(
        ten_days_ago,
        AggregatedCandidate(
            canonical_key="imou|ranger 2",
            name="Camera an ninh Imou Ranger 2",
            brand="Imou",
            model="Ranger 2",
        ),
    )
    repo.apply_backfill_selection(
        ten_days_ago,
        AggregatedCandidate(
            canonical_key="ezviz|c6n",
            name="Camera Ezviz C6N",
            brand="Ezviz",
            model="C6N",
        ),
    )

    # 2. Another product selected recently but NOT qualifying (e.g. not enough surge):
    # 'moft|z' selected 5 days ago
    five_days_ago = date(2026, 9, 13)
    repo.apply_backfill_selection(
        five_days_ago,
        AggregatedCandidate(
            canonical_key="moft|z",
            name="Giá đỡ laptop Moft Z",
            brand="Moft",
            model="Z",
        ),
    )

    provider = FixtureCandidateProvider(FIXTURE_PATH)
    service = TrendDiscoveryService(
        repository=repo,
        providers=[provider],
        config=TrendDiscoveryConfig(),
        clock=lambda: FIXED_NOW,
    )

    result = service.discover(CRITERIA, run_date=date(2026, 9, 18))
    selected = result.selection.selected
    statuses = result.selection.statuses

    assert len(selected) == 10
    fresh_count = sum(statuses[c.candidate.canonical_key] == "new" for c in selected)
    reemerging_count = sum(statuses[c.candidate.canonical_key] == "re_emerging" for c in selected)

    # Policy checks: >= 7 new, <= 3 re-emerging
    assert fresh_count >= 7
    assert reemerging_count <= 3
    assert fresh_count + reemerging_count == 10

    # Confidence and evidence checks
    assert all(c.confidence_score >= 60 for c in selected)
    assert all(len(c.candidate.observations) > 0 for c in selected)
    assert all(c.score_breakdown is not None for c in selected)

    # Determinism check: running discovery again produces same order
    result2 = service.discover(CRITERIA, run_date=date(2026, 9, 18))
    keys_1 = [c.candidate.canonical_key for c in selected]
    keys_2 = [c.candidate.canonical_key for c in result2.selection.selected]
    assert keys_1 == keys_2


def test_acceptance_does_not_backfill_with_old_products(tmp_path: Path):
    repo = TrendHistoryRepository(tmp_path / "trend.db")

    # Seed 10 products into history as selected yesterday
    yesterday = date(2026, 9, 17)
    for i in range(10):
        repo.apply_backfill_selection(
            yesterday,
            AggregatedCandidate(
                canonical_key=f"brand{i}|model{i}",
                name=f"Brand{i} Model{i}",
                brand=f"Brand{i}",
                model=f"Model{i}",
            ),
        )

    # Provider returns only 5 fresh products and 5 old products
    class FiveFreshProvider:
        name = "five_fresh_provider"

        def discover(self, criteria, *, run_at, limit):
            items = []
            # 5 fresh
            for i in range(5):
                items.append(
                    CandidateObservation(
                        provider=self.name,
                        candidate_name=f"FreshBrand{i} FreshModel{i}",
                        brand=f"FreshBrand{i}",
                        model=f"FreshModel{i}",
                        source_url=f"https://fresh.test/{i}",
                        source_host="fresh.test",
                        source_rank=1,
                        observed_at=run_at,
                        published_at=run_at,
                        signal_type="web_mention",
                    )
                )
            # 5 old (from history)
            for i in range(5):
                items.append(
                    CandidateObservation(
                        provider=self.name,
                        candidate_name=f"Brand{i} Model{i}",
                        brand=f"Brand{i}",
                        model=f"Model{i}",
                        source_url=f"https://old.test/{i}",
                        source_host="old.test",
                        source_rank=1,
                        observed_at=run_at,
                        published_at=run_at,
                        signal_type="web_mention",
                    )
                )
            return items

    service = TrendDiscoveryService(
        repository=repo,
        providers=[FiveFreshProvider()],
        config=TrendDiscoveryConfig(),
        clock=lambda: FIXED_NOW,
    )

    result = service.discover(CRITERIA, run_date=date(2026, 9, 18))
    assert len(result.selection.selected) == 5
    assert result.selection.warnings == ["insufficient_fresh_candidates"]
    assert all(result.selection.statuses[c.candidate.canonical_key] == "new" for c in result.selection.selected)
