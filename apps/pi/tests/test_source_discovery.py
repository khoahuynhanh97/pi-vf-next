"""Offline tests for source discovery. No network."""

from __future__ import annotations

from datetime import UTC, datetime

from media.models import ProductIdentityHint
from media.source_discovery import (
    DuckDuckGoSourceDiscoveryProvider,
    SourceCandidate,
    SourceClass,
    SourceDiscoveryService,
    TEXT_BACKENDS,
    build_discovery_query,
    classify_source,
)


def _stamp():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _row(rank, url, title="", snippet=""):
    return {"url": url, "title": title, "body": snippet, "href": url}


def _candidate(url, host, source_class=None, rank=1, title="t", snippet="s"):
    return SourceCandidate(
        candidate_id=f"c{rank}",
        url=url,
        title=title,
        snippet=snippet,
        host=host,
        query="q",
        discovery_provider="test",
        rank=rank,
        source_class=source_class or SourceClass.UNKNOWN,
        discovered_at=_stamp(),
    )


def test_query_generation():
    hint = ProductIdentityHint(name="Jisulife FA55 Turbo Fan", brand="Jisulife", model="FA55")
    assert build_discovery_query(hint) == "Jisulife FA55"


def test_classify_source_official_marketplace_retailer():
    official_domains = {"mi.com", "ugreen.com"}
    assert classify_source(_candidate("https://mi.com/p", "mi.com"), official_domains).source_class == SourceClass.OFFICIAL
    assert classify_source(_candidate("https://shopee.vn/p", "shopee.vn"), official_domains).source_class == SourceClass.MARKETPLACE
    assert classify_source(_candidate("https://dienmayxanh.com/p", "dienmayxanh.com"), official_domains).source_class == SourceClass.RETAILER
    assert classify_source(_candidate("https://vnreview.vn/p", "vnreview.vn"), official_domains).source_class == SourceClass.REVIEW
    assert classify_source(_candidate("https://random-site.example/p", "random-site.example"), official_domains).source_class == SourceClass.UNKNOWN


def test_ddg_provider_parses_rows():
    rows = [_row(1, "https://shopee.vn/a"), _row(2, "https://dienmayxanh.com/b"), _row(3, "not-a-url")]
    provider = DuckDuckGoSourceDiscoveryProvider(factory=lambda: type("DDGS", (), {"text": lambda *a, **k: rows})())
    candidates = provider.search(type("Q", (), {"query": "Jisulife FA55", "max_results": 15})())
    assert len(candidates) == 2
    assert candidates[0].host == "shopee.vn"
    assert candidates[0].discovery_provider == "duckduckgo_web"
    assert candidates[0].rank == 1


def test_ddg_fallback_engine_used_when_primary_empty():
    calls = []
    provider = DuckDuckGoSourceDiscoveryProvider()

    def fake_text(backend, query, max_results):
        calls.append(backend)
        return [] if backend == TEXT_BACKENDS[0] else [_row(1, "https://shopee.vn/a")]

    provider._text = fake_text
    rows = provider._search_all("Jisulife FA55", 15)
    assert len(rows) == 1
    assert calls == [TEXT_BACKENDS[0], TEXT_BACKENDS[1]]  # primary empty -> next backend


def test_ddg_fallback_all_engines_empty_returns_truthfully():
    provider = DuckDuckGoSourceDiscoveryProvider()
    provider._text = lambda backend, query, max_results: []
    assert provider._search_all("q", 15) == []


def test_ddg_backend_failure_falls_through_engines():
    calls = []
    provider = DuckDuckGoSourceDiscoveryProvider()

    def fake_text(backend, query, max_results):
        calls.append(backend)
        raise RuntimeError("backend down")

    provider._text = fake_text
    assert provider._search_all("q", 15) == []
    assert calls == list(TEXT_BACKENDS)


def test_ddg_search_uses_fallback_when_primary_empty():
    provider = DuckDuckGoSourceDiscoveryProvider()

    def fake_text(backend, query, max_results):
        return [] if backend == TEXT_BACKENDS[0] else [_row(1, "https://shopee.vn/a")]

    provider._text = fake_text
    candidates = provider.search(type("Q", (), {"query": "Jisulife FA55", "max_results": 15})())
    assert len(candidates) == 1
    assert candidates[0].host == "shopee.vn"


def test_ddg_backend_timeout_is_bounded_and_raises():
    import time

    class SlowDDGS:
        def text(self, *a, **k):
            time.sleep(5)
            return [_row(1, "https://shopee.vn/a")]

    provider = DuckDuckGoSourceDiscoveryProvider(factory=SlowDDGS, backend_timeout=0.1)
    import pytest

    with pytest.raises(RuntimeError, match="ddg web search failed"):
        provider.search(type("Q", (), {"query": "q", "max_results": 15})())


def test_selection_respects_per_host_limit_and_budget():
    candidates = []
    for i in range(6):
        candidates.append(_candidate(f"https://shopee.vn/{i}", "shopee.vn", SourceClass.MARKETPLACE, i + 1))
    for i in range(3):
        candidates.append(_candidate(f"https://dienmayxanh.com/{i}", "dienmayxanh.com", SourceClass.RETAILER, 10 + i))
    service = SourceDiscoveryService(provider=object())
    selected = service._select(candidates, max_sources=8, target_usable=3)
    assert len(selected) <= 8
    shopee = [c for c in selected if c.host == "shopee.vn"]
    assert len(shopee) <= 2  # per_host_limit


def test_selection_favors_diversity():
    candidates = [
        _candidate("https://shopee.vn/1", "shopee.vn", SourceClass.MARKETPLACE, 1),
        _candidate("https://shopee.vn/2", "shopee.vn", SourceClass.MARKETPLACE, 2),
        _candidate("https://mi.com/x", "mi.com", SourceClass.OFFICIAL, 3),
        _candidate("https://dienmayxanh.com/y", "dienmayxanh.com", SourceClass.RETAILER, 4),
        _candidate("https://vnreview.vn/z", "vnreview.vn", SourceClass.REVIEW, 5),
    ]
    service = SourceDiscoveryService(provider=object())
    selected = service._select(candidates, max_sources=3, target_usable=3)
    classes = [c.source_class for c in selected]
    # Diversity order puts official/retailer ahead of marketplace.
    assert SourceClass.OFFICIAL in classes
    assert SourceClass.RETAILER in classes
    assert len({c.host for c in selected}) == len(selected)


def test_discovery_dedups_urls():
    candidates = [
        _candidate("https://x.test/p", "x.test", SourceClass.UNKNOWN, 1),
        _candidate("https://x.test/p", "x.test", SourceClass.UNKNOWN, 2),
    ]
    service = SourceDiscoveryService(provider=object())
    assert len(service._select(candidates, max_sources=8, target_usable=3)) == 1


def test_discovery_returns_empty_for_unusable_hint():
    service = SourceDiscoveryService(provider=object())
    assert service.discover(ProductIdentityHint()) == []


class FakeProvider:
    name = "fake"

    def __init__(self, candidates):
        self.candidates = candidates

    def search(self, query):
        return self.candidates


def test_discover_integrates_classification_and_selection():
    provider = FakeProvider([
        _candidate("https://mi.com/x", "mi.com", SourceClass.UNKNOWN, 1, title="Jisulife FA55 original"),
        _candidate("https://shopee.vn/y", "shopee.vn", SourceClass.UNKNOWN, 2, title="Jisulife FA55 shop"),
        _candidate("https://dienmayxanh.com/z", "dienmayxanh.com", SourceClass.UNKNOWN, 3, title="Jisulife FA55 retailer"),
    ])
    service = SourceDiscoveryService(provider=provider)
    results = service.discover(ProductIdentityHint(name="Jisulife FA55", brand="Jisulife", model="FA55"))
    assert len(results) == 3
    classes = {c.source_class for c in results}
    assert SourceClass.OFFICIAL in classes  # mi.com classified official
    assert SourceClass.MARKETPLACE in classes
    assert SourceClass.RETAILER in classes
    assert all(c.relevance == "high" for c in results)
