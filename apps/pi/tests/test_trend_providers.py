from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import pytest

from product_scout.daily_parser import ParsedCriteria
from product_scout.trend_discovery.models import CandidateObservation
from product_scout.trend_discovery.providers import (
    GoogleTrendsProvider,
    PublicCommerceProvider,
    WebSearchProvider,
)

FIXED_NOW = datetime(2026, 9, 18, 7, 0, 0, tzinfo=timezone.utc)
CRITERIA = ParsedCriteria(
    raw_prompt="tìm sản phẩm công nghệ hot trend",
    limit=10,
    category="công nghệ",
    price_min=200_000,
    price_max=1_000_000,
)
FIXTURE_RSS = Path(__file__).parent / "fixtures" / "google_trends_vn.xml"


def test_google_trends_preserves_provenance():
    rss_bytes = FIXTURE_RSS.read_bytes()
    provider = GoogleTrendsProvider(fetcher=lambda: rss_bytes, clock=lambda: FIXED_NOW)
    rows = provider.discover(CRITERIA, run_at=FIXED_NOW, limit=10)
    assert len(rows) > 0
    assert rows[0].provider == "google_trends_vn"
    assert rows[0].signal_type == "search_trend"
    assert rows[0].source_url.startswith("https://")


def test_web_search_preserves_rank():
    class FakeSearch:
        def search(self, query):
            return [
                SimpleNamespace(
                    title="Tai nghe Baseus Bowie WM02",
                    url="https://example.vn/tai-nghe-baseus",
                    snippet="Tai nghe bluetooth giá rẻ",
                    rank=1,
                )
            ]

    provider = WebSearchProvider(search_provider=FakeSearch(), clock=lambda: FIXED_NOW)
    rows = provider.discover(CRITERIA, run_at=FIXED_NOW, limit=5)
    assert len(rows) > 0
    assert rows[0].source_rank == 1
    assert rows[0].source_host == "example.vn"
    assert rows[0].provider == "web_search"


def test_commerce_provider_rejects_non_marketplace_hosts():
    class FakeCommerceSearch:
        def search(self, query):
            return [
                SimpleNamespace(
                    title="Củ sạc Ugreen Uno Shopee",
                    url="https://shopee.vn/cu-sac-ugreen-uno-i.123",
                    snippet="Củ sạc chính hãng",
                    rank=1,
                ),
                SimpleNamespace(
                    title="Review củ sạc Blog",
                    url="https://blogcongnghe.com/cu-sac",
                    snippet="Review bài viết",
                    rank=2,
                ),
                SimpleNamespace(
                    title="Tai nghe TikTok Shop",
                    url="https://www.tiktok.com/@shop/video/123",
                    snippet="Mua tai nghe",
                    rank=3,
                ),
            ]

    provider = PublicCommerceProvider(search_provider=FakeCommerceSearch(), clock=lambda: FIXED_NOW)
    rows = provider.discover(CRITERIA, run_at=FIXED_NOW, limit=10)
    assert len(rows) == 2
    assert all(row.source_host.endswith(("shopee.vn", "tiktok.com", "lazada.vn")) for row in rows)
    assert all(row.signal_type == "commerce_listing" for row in rows)


def test_web_search_provider_distributes_budget_across_diverse_query_variants():
    queried_strings: list[str] = []

    class MultiResultFakeSearch:
        def search(self, query):
            queried_strings.append(query.query)
            # Return 10 results per query; provider with limit=12 and 6 queries
            # must NOT stop after query #2; it should sample across all 6 queries!
            q_idx = len(queried_strings)
            return [
                SimpleNamespace(
                    title=f"Tai nghe SoundPEATS Air{q_idx} Pro item {i}",
                    url=f"https://review{q_idx}.vn/item-{i}",
                    snippet="Tai nghe chống ồn giá 500k",
                    rank=i,
                )
                for i in range(1, 11)
            ]

    provider = WebSearchProvider(
        search_provider=MultiResultFakeSearch(),
        clock=lambda: FIXED_NOW,
    )
    rows = provider.discover(CRITERIA, run_at=FIXED_NOW, limit=12)
    assert len(rows) == 12
    # Must have executed at least 4 distinct query variations rather than exhausting limit on query 1-2
    assert len(queried_strings) >= 4
    assert len(set(queried_strings)) == len(queried_strings)


def test_providers_rotate_queries_on_different_days():
    day1_queries: list[str] = []
    day2_queries: list[str] = []

    class RecordingSearch:
        def __init__(self, sink: list[str]):
            self.sink = sink

        def search(self, query):
            self.sink.append(query.query)
            return []

    p1 = WebSearchProvider(search_provider=RecordingSearch(day1_queries))
    p2 = WebSearchProvider(search_provider=RecordingSearch(day2_queries))

    p1.discover(CRITERIA, run_at=datetime(2026, 10, 13, 7, 0, tzinfo=timezone.utc), limit=12)
    p2.discover(CRITERIA, run_at=datetime(2026, 10, 14, 7, 0, tzinfo=timezone.utc), limit=12)

    assert len(day1_queries) >= 4
    assert day1_queries != day2_queries
