"""Public trend discovery providers conforming to the TrendDiscoveryProvider protocol."""

from __future__ import annotations

import asyncio
import concurrent.futures
import math
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit

from media.source_discovery.models import SourceDiscoveryQuery
from media.source_discovery.providers import DuckDuckGoSourceDiscoveryProvider
from product_scout.daily_parser import ParsedCriteria, is_food_category
from tools.trend_research.trends import TrendCollector

from .identity import extract_product_identity
from .models import CandidateObservation
from .query_planner import QueryPlanner


def _host(url: str) -> str:
    return urlsplit(url).netloc.casefold().removeprefix("www.") or ""


class TrendDiscoveryProvider(Protocol):
    name: str

    def discover(
        self,
        criteria: ParsedCriteria,
        *,
        run_at: datetime,
        limit: int,
    ) -> list[CandidateObservation]:
        """Discover candidate observations matching criteria."""
        raise NotImplementedError


class GoogleTrendsProvider:
    """Discovers trending tech topics from Google Trends VN RSS."""

    name = "google_trends_vn"

    def __init__(
        self,
        *,
        fetcher: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._fetcher = fetcher

    def discover(
        self,
        criteria: ParsedCriteria,
        *,
        run_at: datetime,
        limit: int,
    ) -> list[CandidateObservation]:
        if is_food_category(criteria.category):
            return []
        async_fetcher = None
        if self._fetcher is not None:
            if asyncio.iscoroutinefunction(self._fetcher):
                async_fetcher = self._fetcher
            else:
                async def _wrapped():
                    res = self._fetcher()
                    if asyncio.iscoroutine(res):
                        return await res
                    return res

                async_fetcher = _wrapped

        collector = TrendCollector(fetcher=async_fetcher)
        try:
            items = asyncio.run(collector.collect_items(limit=limit))
        except RuntimeError:
            loop = asyncio.get_event_loop()
            items = loop.run_until_complete(collector.collect_items(limit=limit))

        observations: list[CandidateObservation] = []
        for rank, item in enumerate(items, start=1):
            url = item.url or f"https://trends.google.com/trending?geo=VN&q={item.title}"
            observations.append(
                CandidateObservation(
                    provider=self.name,
                    candidate_name=item.title,
                    source_url=url,
                    source_host=_host(url),
                    source_rank=rank,
                    observed_at=run_at,
                    published_at=item.published_at,
                    signal_type="search_trend",
                    title=item.title,
                )
            )
        return observations


def _execute_queries_batch(
    search_provider: Any,
    queries: list[str],
    *,
    max_results_per_query: int,
    parallel: bool,
    soft_deadline_seconds: float,
) -> list[list[Any]]:
    """Run search queries (in parallel for live providers, sequential for deterministic test doubles)."""
    t_start = time.perf_counter()
    if not parallel:
        batches: list[list[Any]] = []
        for idx, q_text in enumerate(queries):
            if idx > 0 and (time.perf_counter() - t_start) >= soft_deadline_seconds:
                break
            try:
                res = search_provider.search(
                    SourceDiscoveryQuery(query=q_text, max_results=max_results_per_query)
                )
                batches.append(list(res or []))
            except Exception as exc:
                if batches:
                    break
                raise RuntimeError(f"Search query '{q_text}' failed: {exc}") from exc
        return batches

    batches_by_idx: dict[int, list[Any]] = {}
    errors: list[Exception] = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(4, max(1, len(queries))))
    try:
        future_to_idx = {
            pool.submit(
                search_provider.search,
                SourceDiscoveryQuery(query=q_text, max_results=max_results_per_query),
            ): idx
            for idx, q_text in enumerate(queries)
        }
        remaining = max(0.5, soft_deadline_seconds - (time.perf_counter() - t_start))
        done, _ = concurrent.futures.wait(future_to_idx.keys(), timeout=remaining)
        for fut in done:
            idx = future_to_idx[fut]
            try:
                batches_by_idx[idx] = list(fut.result() or [])
            except Exception as exc:
                errors.append(exc)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    if not batches_by_idx and errors:
        raise RuntimeError(f"All search queries failed: {errors[0]}") from errors[0]

    return [batches_by_idx[i] for i in sorted(batches_by_idx.keys())]


class WebSearchProvider:
    """Discovers trending products via diverse AI/combinatorial web search queries."""

    name = "web_search"

    def __init__(
        self,
        *,
        search_provider: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        query_planner: QueryPlanner | None = None,
        history_provider: Callable[[], list[dict[str, str]]] | None = None,
        query_count: int = 6,
        soft_deadline_seconds: float = 22.0,
    ):
        self._is_custom_search = search_provider is not None
        self._search = search_provider or DuckDuckGoSourceDiscoveryProvider(
            max_results=12, backend_timeout=6.0
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._planner = query_planner or QueryPlanner(enable_llm=not self._is_custom_search)
        self._history_provider = history_provider
        self._query_count = max(1, query_count)
        self._soft_deadline_seconds = soft_deadline_seconds

    def discover(
        self,
        criteria: ParsedCriteria,
        *,
        run_at: datetime,
        limit: int,
    ) -> list[CandidateObservation]:
        recent_identities = self._history_provider() if self._history_provider else []
        target_q_count = max(self._query_count, min(8, math.ceil(limit / 2)))
        plan = self._planner.plan_queries(
            criteria,
            run_at=run_at,
            recent_identities=recent_identities,
            web_count=target_q_count,
            commerce_count=target_q_count,
        )
        queries = plan.web_queries
        if not queries:
            return []

        per_query_cap = max(2, math.ceil(limit / len(queries)))
        search_fetch_size = max(per_query_cap + 3, min(limit, 12))

        batches = _execute_queries_batch(
            self._search,
            queries,
            max_results_per_query=search_fetch_size,
            parallel=not self._is_custom_search,
            soft_deadline_seconds=self._soft_deadline_seconds,
        )

        primary_observations: list[CandidateObservation] = []
        overflow_observations: list[CandidateObservation] = []
        seen_urls: set[str] = set()

        for results in batches:
            taken_for_query = 0
            for cand in results:
                url = str(getattr(cand, "url", "")).strip()
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                title = str(getattr(cand, "title", "")).strip()
                snippet = str(getattr(cand, "snippet", "")).strip()
                rank = int(getattr(cand, "rank", taken_for_query + 1))
                brand, model, clean_name = extract_product_identity(title, snippet, category=criteria.category)

                obs = CandidateObservation(
                    provider=self.name,
                    candidate_name=clean_name or title,
                    brand=brand,
                    model=model,
                    source_url=url,
                    source_host=_host(url),
                    source_rank=rank,
                    observed_at=run_at,
                    published_at=None,
                    signal_type="web_mention",
                    title=title,
                    snippet=snippet,
                )
                if taken_for_query < per_query_cap:
                    primary_observations.append(obs)
                    taken_for_query += 1
                else:
                    overflow_observations.append(obs)

        combined = primary_observations + overflow_observations
        return combined[:limit]


class PublicCommerceProvider:
    """Discovers listings and viral signals on public commerce/social marketplaces."""

    name = "public_commerce"
    ALLOWED_HOSTS = ("shopee.vn", "tiktok.com", "lazada.vn")

    def __init__(
        self,
        *,
        search_provider: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        query_planner: QueryPlanner | None = None,
        history_provider: Callable[[], list[dict[str, str]]] | None = None,
        query_count: int = 6,
        soft_deadline_seconds: float = 22.0,
    ):
        self._is_custom_search = search_provider is not None
        self._search = search_provider or DuckDuckGoSourceDiscoveryProvider(
            max_results=12, backend_timeout=6.0
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._planner = query_planner or QueryPlanner(enable_llm=not self._is_custom_search)
        self._history_provider = history_provider
        self._query_count = max(1, query_count)
        self._soft_deadline_seconds = soft_deadline_seconds

    def discover(
        self,
        criteria: ParsedCriteria,
        *,
        run_at: datetime,
        limit: int,
    ) -> list[CandidateObservation]:
        recent_identities = self._history_provider() if self._history_provider else []
        target_q_count = max(self._query_count, min(8, math.ceil(limit / 2)))
        plan = self._planner.plan_queries(
            criteria,
            run_at=run_at,
            recent_identities=recent_identities,
            web_count=target_q_count,
            commerce_count=target_q_count,
        )
        queries = plan.commerce_queries
        if not queries:
            return []

        per_query_cap = max(2, math.ceil(limit / len(queries)))
        search_fetch_size = max(per_query_cap + 3, min(limit, 12))

        batches = _execute_queries_batch(
            self._search,
            queries,
            max_results_per_query=search_fetch_size,
            parallel=not self._is_custom_search,
            soft_deadline_seconds=self._soft_deadline_seconds,
        )

        primary_observations: list[CandidateObservation] = []
        overflow_observations: list[CandidateObservation] = []
        seen_urls: set[str] = set()

        for results in batches:
            taken_for_query = 0
            for cand in results:
                url = str(getattr(cand, "url", "")).strip()
                if not url or url in seen_urls:
                    continue
                host = _host(url)
                if not any(host == ah or host.endswith("." + ah) for ah in self.ALLOWED_HOSTS):
                    continue

                seen_urls.add(url)
                title = str(getattr(cand, "title", "")).strip()
                snippet = str(getattr(cand, "snippet", "")).strip()
                rank = int(getattr(cand, "rank", taken_for_query + 1))
                brand, model, clean_name = extract_product_identity(title, snippet, category=criteria.category)

                obs = CandidateObservation(
                    provider=self.name,
                    candidate_name=clean_name or title,
                    brand=brand,
                    model=model,
                    source_url=url,
                    source_host=host,
                    source_rank=rank,
                    observed_at=run_at,
                    published_at=None,
                    signal_type="commerce_listing",
                    title=title,
                    snippet=snippet,
                )
                if taken_for_query < per_query_cap:
                    primary_observations.append(obs)
                    taken_for_query += 1
                else:
                    overflow_observations.append(obs)

        combined = primary_observations + overflow_observations
        return combined[:limit]
