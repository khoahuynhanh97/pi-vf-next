from datetime import datetime, timezone

import pytest

from product_scout.daily_parser import ParsedCriteria
from product_scout.trend_discovery.tiktok_category import (
    TikTokCategoryChallenge,
    TikTokSnacksCategoryProvider,
)
from product_scout.trend_discovery.repository import TrendHistoryRepository
from product_scout.trend_discovery.config import TrendDiscoveryConfig
from product_scout.trend_discovery.service import (
    TrendDiscoveryService,
    build_default_trend_discovery_service,
)


NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)
SNACKS = ParsedCriteria(
    raw_prompt="tìm snack TikTok từ 20k đến 100k",
    category="đồ ăn vặt",
    price_min=20_000,
    price_max=100_000,
    platforms=["tiktok"],
)


def test_category_cards_feed_product_candidates_with_price_and_deduplicated_pdp():
    page = """
    <title>Snacks - TikTok Shop</title>
    <a href="/vn/pdp/oreo-banh-quy/1736093592588027649?source=category">
      <img alt="Bánh quy Oreo vị chocolate 133g"><span>₫43.145</span><span>19.9K sold</span>
    </a>
    <a href="/vn/pdp/1736093592588027649"><span>Bánh quy Oreo vị chocolate 133g</span></a>
    <a href="https://shop.tiktok.com/vn/pdp/snack-oishi/1736093592588027650">
      <span>Snack Oishi tôm cay 40g</span><span>28.000₫</span>
    </a>
    <a href="https://evil.example/vn/pdp/fake/1736093592588027651">Fake 25.000₫</a>
    <a href="/vn/pdp/pringles/1736093592588027652">Snack Pringles 165g 150.000₫</a>
    """
    provider = TikTokSnacksCategoryProvider(fetcher=lambda url: page)

    rows = provider.discover(SNACKS, run_at=NOW, limit=10)

    assert [row.source_url for row in rows] == [
        "https://shop.tiktok.com/vn/pdp/1736093592588027649",
        "https://shop.tiktok.com/vn/pdp/1736093592588027650",
    ]
    assert rows[0].candidate_name == "Bánh quy Oreo vị chocolate 133g"
    assert rows[0].brand == "Orion" or rows[0].brand == "Oreo"
    assert rows[0].metadata["price_vnd"] == 43_145
    assert rows[0].source_rank == 1


def test_category_provider_only_runs_for_tiktok_food_requests():
    calls = []
    provider = TikTokSnacksCategoryProvider(fetcher=lambda url: calls.append(url) or "")
    tech = ParsedCriteria(raw_prompt="tai nghe", category="công nghệ", platforms=["tiktok"])
    shopee = ParsedCriteria(raw_prompt="snack", category="đồ ăn vặt", platforms=["shopee"])

    assert provider.discover(tech, run_at=NOW, limit=10) == []
    assert provider.discover(shopee, run_at=NOW, limit=10) == []
    assert calls == []


def test_category_challenge_is_reported_instead_of_becoming_a_product():
    provider = TikTokSnacksCategoryProvider(
        fetcher=lambda url: "<title>Security Check</title>Drag the puzzle piece into place",
        renderer=lambda url: "<title>Security Check</title>Drag the puzzle piece into place",
    )

    with pytest.raises(TikTokCategoryChallenge):
        provider.discover(SNACKS, run_at=NOW, limit=10)


def test_category_renders_when_http_page_has_no_product_cards():
    rendered = '<a href="/vn/pdp/1736093592588027649">Bánh quy Oreo 133g ₫43.145</a>'
    provider = TikTokSnacksCategoryProvider(
        fetcher=lambda url: "<title>Snacks</title><div id='app'></div>",
        renderer=lambda url: rendered,
    )

    rows = provider.discover(SNACKS, run_at=NOW, limit=10)

    assert len(rows) == 1
    assert rows[0].metadata["acquisition_method"] == "browser"


def test_default_discovery_runs_category_source_alongside_existing_sources(tmp_path):
    repository = TrendHistoryRepository(tmp_path / "history.db")

    built = build_default_trend_discovery_service(repository)
    direct = TrendDiscoveryService(repository)

    assert "tiktok_snacks_category" in [provider.name for provider in built.providers]
    assert "tiktok_snacks_category" in [provider.name for provider in direct.providers]
    assert "public_commerce" in [provider.name for provider in built.providers]


def test_category_products_reach_existing_portfolio_selection(tmp_path):
    page = """
    <a href="/vn/pdp/1736093592588027649"><img alt="Bánh quy Oreo vị chocolate 133g">₫43.145</a>
    <a href="/vn/pdp/1736093592588027650"><img alt="Snack Oishi tôm cay 40g">₫28.000</a>
    <a href="/vn/pdp/1736093592588027653"><img alt="Bánh Gấu Nhân Kem - Thơm Ngon Giòn Rụm - Ngon Mêly">₫43.145</a>
    """
    repository = TrendHistoryRepository(tmp_path / "history.db")
    provider = TikTokSnacksCategoryProvider(fetcher=lambda url: page)
    service = TrendDiscoveryService(
        repository,
        providers=[provider],
        config=TrendDiscoveryConfig(confidence_threshold=60),
        clock=lambda: NOW,
    )

    result = service.discover(SNACKS, run_date=NOW.date())

    assert result.status == "completed"
    assert {item.candidate.name for item in result.selection.selected} == {
        "Bánh quy Oreo vị chocolate 133g",
        "Snack Oishi tôm cay 40g",
        "Bánh Gấu Nhân Kem - Thơm Ngon Giòn Rụm - Ngon Mêly",
    }
    assert all(item.candidate.observations[0].source_url.startswith("https://shop.tiktok.com/vn/pdp/") for item in result.selection.selected)


def test_login_navigation_does_not_mark_valid_category_as_challenge():
    page = '<nav>Log in</nav><a href="/vn/pdp/1736093592588027649">Bánh quy Oreo 133g ₫43.145</a>'
    provider = TikTokSnacksCategoryProvider(fetcher=lambda url: page)

    assert len(provider.discover(SNACKS, run_at=NOW, limit=10)) == 1
