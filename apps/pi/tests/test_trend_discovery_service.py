from __future__ import annotations

import time
import pytest
from datetime import date, datetime, timezone
from pathlib import Path

from product_scout.daily_parser import ParsedCriteria
from product_scout.trend_discovery.config import TrendDiscoveryConfig
from product_scout.trend_discovery.models import CandidateObservation
from product_scout.trend_discovery.repository import TrendHistoryRepository
from product_scout.trend_discovery.service import TrendDiscoveryService


@pytest.fixture(autouse=True)
def enable_legacy_discovery_for_regression_tests(monkeypatch):
    monkeypatch.setattr("product_scout.workflow_policy.PRODUCT_DISCOVERY_PAUSED", False)

FIXED_NOW = datetime(2026, 9, 18, 7, 0, 0, tzinfo=timezone.utc)
FIXED_DATE = date(2026, 9, 18)
CRITERIA = ParsedCriteria(
    raw_prompt="tìm 10 sản phẩm công nghệ hot trend",
    limit=10,
    category="công nghệ",
    price_min=200_000,
    price_max=1_000_000,
)


class FailingProvider:
    name = "failing_provider"

    def discover(self, criteria: ParsedCriteria, *, run_at: datetime, limit: int) -> list[CandidateObservation]:
        raise RuntimeError("Provider network outage")


class EmptyProvider:
    name = "empty_provider"

    def discover(self, criteria: ParsedCriteria, *, run_at: datetime, limit: int) -> list[CandidateObservation]:
        return []


class SevenFreshProvider:
    name = "seven_fresh_provider"

    def discover(self, criteria: ParsedCriteria, *, run_at: datetime, limit: int) -> list[CandidateObservation]:
        res = []
        for i in range(7):
            res.append(
                CandidateObservation(
                    provider=self.name,
                    candidate_name=f"Brand{i} Model{i}",
                    brand=f"Brand{i}",
                    model=f"Model{i}",
                    source_url=f"https://source{i}.test/item",
                    source_host=f"source{i}.test",
                    source_rank=1,
                    observed_at=run_at,
                    published_at=run_at,
                    signal_type="web_mention",
                )
            )
        return res


class ProviderAForFA55:
    name = "provider_a"

    def discover(self, criteria: ParsedCriteria, *, run_at: datetime, limit: int) -> list[CandidateObservation]:
        return [
            CandidateObservation(
                provider=self.name,
                candidate_name="Jisulife FA55 handheld fan",
                brand="Jisulife",
                model="FA55",
                source_url="https://site-a.test/fa55",
                source_host="site-a.test",
                source_rank=1,
                observed_at=run_at,
                published_at=run_at,
                signal_type="web_mention",
            )
        ]


class ProviderBForFA55:
    name = "provider_b"

    def discover(self, criteria: ParsedCriteria, *, run_at: datetime, limit: int) -> list[CandidateObservation]:
        return [
            CandidateObservation(
                provider=self.name,
                candidate_name="Quạt mini Jisulife FA55",
                brand="Jisulife",
                model="FA55",
                source_url="https://site-b.test/fa55",
                source_host="site-b.test",
                source_rank=2,
                observed_at=run_at,
                published_at=run_at,
                signal_type="commerce_listing",
            )
        ]


class InvalidIdentityBeforeValidProvider:
    name = "identity_gate_provider"

    def discover(self, criteria: ParsedCriteria, *, run_at: datetime, limit: int) -> list[CandidateObservation]:
        return [
            CandidateObservation(
                provider=self.name,
                candidate_name="shopee.vn/Baseus-Bowie-WM02",
                brand="Baseus",
                model="Bowie WM02",
                source_url="https://shopee.vn/invalid",
                source_host="shopee.vn",
                source_rank=1,
                observed_at=run_at,
                published_at=run_at,
                signal_type="commerce_listing",
            ),
            CandidateObservation(
                provider=self.name,
                candidate_name="Jisulife FA55 handheld fan",
                brand="Jisulife",
                model="FA55",
                source_url="https://reviews.test/fa55",
                source_host="reviews.test",
                source_rank=2,
                observed_at=run_at,
                published_at=run_at,
                signal_type="web_mention",
            ),
        ]


def test_invalid_identity_does_not_consume_enrichment_slot(tmp_path: Path):
    repo = TrendHistoryRepository(tmp_path / "trend.db")
    service = TrendDiscoveryService(
        repository=repo,
        providers=[InvalidIdentityBeforeValidProvider()],
        config=TrendDiscoveryConfig(enrichment_limit=1),
        clock=lambda: FIXED_NOW,
    )
    result = service.discover(CRITERIA, run_date=FIXED_DATE)
    assert [candidate.canonical_key for candidate in result.candidates] == ["jisulife|fa55"]


def test_service_isolates_provider_failure(tmp_path: Path):
    repo = TrendHistoryRepository(tmp_path / "trend.db")
    service = TrendDiscoveryService(
        repository=repo,
        providers=[FailingProvider(), SevenFreshProvider()],
        config=TrendDiscoveryConfig(),
        clock=lambda: FIXED_NOW,
    )
    result = service.discover(CRITERIA, run_date=FIXED_DATE)
    assert result.status == "completed"
    assert len(result.selection.selected) == 7
    # Failing provider is recorded in provider_summary with status failed
    failing_summary = [p for p in result.provider_summary if p.provider == "failing_provider"][0]
    assert failing_summary.status == "failed"
    assert failing_summary.error_code is not None


def test_service_fails_without_evidence(tmp_path: Path):
    repo = TrendHistoryRepository(tmp_path / "trend.db")
    service = TrendDiscoveryService(
        repository=repo,
        providers=[EmptyProvider(), FailingProvider()],
        config=TrendDiscoveryConfig(),
        clock=lambda: FIXED_NOW,
    )
    result = service.discover(CRITERIA, run_date=FIXED_DATE)
    assert result.status == "failed"
    assert result.failure_code == "no_discovery_evidence"


def test_service_merges_same_identity_across_providers(tmp_path: Path):
    repo = TrendHistoryRepository(tmp_path / "trend.db")
    service = TrendDiscoveryService(
        repository=repo,
        providers=[ProviderAForFA55(), ProviderBForFA55()],
        config=TrendDiscoveryConfig(),
        clock=lambda: FIXED_NOW,
    )
    result = service.discover(CRITERIA, run_date=FIXED_DATE)
    matches = [row for row in result.candidates if row.canonical_key == "jisulife|fa55"]
    assert len(matches) == 1
    assert len(matches[0].observations) == 2


class SlowSleepProvider:
    name = "slow_provider"

    def discover(self, criteria: ParsedCriteria, *, run_at: datetime, limit: int) -> list[CandidateObservation]:
        time.sleep(1.0)
        return []


def test_service_isolates_slow_provider_timeout(tmp_path: Path):
    repo = TrendHistoryRepository(tmp_path / "trend.db")
    config = TrendDiscoveryConfig(provider_timeout_seconds=0.1)
    service = TrendDiscoveryService(
        repository=repo,
        providers=[SlowSleepProvider(), SevenFreshProvider()],
        config=config,
        clock=lambda: FIXED_NOW,
    )
    t0 = time.perf_counter()
    result = service.discover(CRITERIA, run_date=FIXED_DATE)
    duration = time.perf_counter() - t0

    assert duration < 0.8  # Must not block waiting 1.0s for the slow provider
    assert result.status == "completed"
    slow_sum = [p for p in result.provider_summary if p.provider == "slow_provider"][0]
    assert slow_sum.status == "failed"
    assert slow_sum.error_code == "TimeoutError"
    assert len(result.selection.selected) == 7


class RawLiveProvider:
    name = "raw_live_provider"

    def discover(self, criteria: ParsedCriteria, *, run_at: datetime, limit: int) -> list[CandidateObservation]:
        return [
            CandidateObservation(
                provider=self.name,
                candidate_name="Tai nghe Bluetooth Baseus Bowie M2+ chống ồn ANC",
                brand="",  # Raw live provider did not set brand
                model="",  # Raw live provider did not set model
                source_url="https://shopee.vn/baseus-m2",
                source_host="shopee.vn",
                source_rank=1,
                observed_at=run_at,
                published_at=run_at,
                signal_type="commerce_listing",
                title="Tai nghe Bluetooth Baseus Bowie M2+ chống ồn ANC giá 450k",
                snippet="Tai nghe Baseus Bowie M2+ chính hãng giá 450k",
            ),
            CandidateObservation(
                provider=self.name,
                candidate_name="Chuột không dây Logitech MX Master 3S cao cấp",
                brand="",
                model="",
                source_url="https://shopee.vn/logitech-mx-master-3s",
                source_host="shopee.vn",
                source_rank=2,
                observed_at=run_at,
                published_at=run_at,
                signal_type="commerce_listing",
                title="Chuột không dây Logitech MX Master 3S cao cấp giá 2.500.000đ",
                snippet="Chuột Logitech MX Master 3S giá 2.500.000đ",
            ),
        ]


def test_service_extracts_identity_and_prices_from_raw_live_observations(tmp_path: Path):
    repo = TrendHistoryRepository(tmp_path / "trend.db")
    service = TrendDiscoveryService(
        repository=repo,
        providers=[RawLiveProvider()],
        config=TrendDiscoveryConfig(),
        clock=lambda: FIXED_NOW,
    )
    result = service.discover(CRITERIA, run_date=FIXED_DATE)
    assert result.status == "completed"

    baseus_cand = [c for c in result.candidates if "baseus" in c.canonical_key][0]
    assert baseus_cand.brand.lower() == "baseus"
    assert "bowie m2" in baseus_cand.model.lower()
    assert baseus_cand.price_min == 450000

    logi_cand = [c for c in result.candidates if "logitech" in c.canonical_key][0]
    assert logi_cand.brand.lower() == "logitech"
    assert "mx master 3s" in logi_cand.model.lower()
    assert logi_cand.price_min == 2500000
