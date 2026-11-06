"""Trend keyword collection for Vietnamese tech products."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from media.source_discovery.models import SourceDiscoveryQuery
from media.source_discovery.providers import DuckDuckGoSourceDiscoveryProvider

logger = logging.getLogger(__name__)

TREND_RSS_URL = "https://trends.google.com/trending/rss?geo=VN"

TECH_LEXICON = frozenset({
    "robot", "camera", "wifi", "sạc", "pin", "loa", "đèn", "máy", "thông minh",
    "tai nghe", "ổ cứng", "router", "smart", "sạc nhanh", "4g", "5g", "bluetooth",
    "điện thoại", "laptop", "màn hình", "smartphone", "củ sạc", "xịt", "quạt",
})

DEFAULT_FALLBACK_QUERIES = [
    "tai nghe bluetooth", "robot hút bụi", "camera wifi", "củ sạc nhanh",
    "loa bluetooth", "màn hình điện thoại",
]

Fetcher = Callable[[], Awaitable[bytes]]


@dataclass
class TrendItem:
    title: str
    url: str = ""
    published_at: datetime | None = None


def filter_tech_titles(titles: list[str]) -> list[str]:
    seen: set[str] = set()
    kept: list[str] = []
    for title in titles:
        folded = title.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        if any(term in folded for term in TECH_LEXICON):
            kept.append(title)
    return kept


def filter_tech_items(items: list[TrendItem]) -> list[TrendItem]:
    seen: set[str] = set()
    kept: list[TrendItem] = []
    for item in items:
        folded = item.title.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        if any(term in folded for term in TECH_LEXICON):
            kept.append(item)
    return kept


class TrendCollector:
    """Collect trending Vietnamese tech keywords, RSS-first with DDG fallback."""

    def __init__(
        self,
        *,
        fetcher: Fetcher | None = None,
        provider=None,
        fallback_queries: list[str] | None = None,
    ):
        self._fetcher = fetcher or self._fetch_rss
        self._provider = provider or DuckDuckGoSourceDiscoveryProvider(max_results=15)
        self._fallback_queries = fallback_queries or DEFAULT_FALLBACK_QUERIES

    async def _fetch_rss(self) -> bytes:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.get(TREND_RSS_URL)
            response.raise_for_status()
            return response.content

    async def collect_items(self, limit: int = 5) -> list[TrendItem]:
        try:
            content = await self._fetcher()
            items = self._parse_rss_items(content)
            items = filter_tech_items(items)
            if items:
                return items[:limit]
        except Exception as exc:  # noqa: BLE001 - RSS failure is non-fatal
            logger.warning("trend_rss_failed reason=%s", exc)

        items = self._fallback_items()
        return filter_tech_items(items)[:limit]

    async def collect(self, limit: int = 5) -> list[str]:
        items = await self.collect_items(limit=limit)
        return [item.title for item in items]

    def _parse_rss(self, content: bytes) -> list[str]:
        items = self._parse_rss_items(content)
        return [item.title for item in items]

    def _parse_rss_items(self, content: bytes) -> list[TrendItem]:
        root = ET.fromstring(content)
        items: list[TrendItem] = []
        for elem in root.iter("item"):
            title = elem.findtext("title")
            if not title:
                continue
            title = title.strip()

            # Try ht:news_item_url first
            news_url_elem = elem.find(".//{https://trends.google.com/trends/trendingsearches/daily}news_item_url")
            url = ""
            if news_url_elem is not None and news_url_elem.text:
                url = news_url_elem.text.strip()
            if not url:
                url = elem.findtext("link") or elem.findtext("guid") or f"https://trends.google.com/trending?geo=VN&q={title}"

            pub_dt = None
            pub_date_str = elem.findtext("pubDate")
            if pub_date_str:
                try:
                    pub_dt = parsedate_to_datetime(pub_date_str)
                except Exception:
                    pub_dt = None

            items.append(TrendItem(title=title, url=url.strip(), published_at=pub_dt))
        return items

    def _fallback(self) -> list[str]:
        items = self._fallback_items()
        return [item.title for item in items]

    def _fallback_items(self) -> list[TrendItem]:
        items: list[TrendItem] = []
        for query in self._fallback_queries:
            try:
                results = self._provider.search(SourceDiscoveryQuery(query=query, max_results=10))
            except Exception as exc:  # noqa: BLE001 - provider failure is non-fatal
                logger.warning("trend_fallback_query_failed query=%s reason=%s", query, exc)
                continue
            for candidate in results:
                title = getattr(candidate, "title", None)
                url = getattr(candidate, "url", "")
                if title:
                    items.append(TrendItem(title=str(title).strip(), url=url))
        return items
