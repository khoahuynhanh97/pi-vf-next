"""Trend discovery service orchestrating providers, aggregation, scoring, and selection."""

from __future__ import annotations

import concurrent.futures
import logging
import time
from collections.abc import Callable
from datetime import date, datetime, timezone

from product_scout.daily_parser import ParsedCriteria

from .config import TrendDiscoveryConfig
from .identity import (
    canonical_key,
    check_category_compatibility,
    extract_prices_from_text,
    extract_product_identity,
    is_usable_product_identity,
)
from .models import (
    AggregatedCandidate,
    CandidateObservation,
    DiscoveryResult,
    ProviderRunSummary,
    ScoredCandidate,
    SelectionResult,
)
from .providers import (
    GoogleTrendsProvider,
    PublicCommerceProvider,
    TrendDiscoveryProvider,
    WebSearchProvider,
)
from .query_planner import QueryPlanner
from .repository import TrendHistoryRepository
from .scoring import score_candidate
from .selection import PortfolioSelector
from .tiktok_category import TikTokSnacksCategoryProvider, has_complete_tiktok_listing

logger = logging.getLogger(__name__)


def build_default_trend_discovery_service(
    repository: TrendHistoryRepository,
    config: TrendDiscoveryConfig | None = None,
) -> TrendDiscoveryService:
    cfg = config or TrendDiscoveryConfig()
    planner = QueryPlanner()
    providers: list[TrendDiscoveryProvider] = [
        GoogleTrendsProvider(),
        WebSearchProvider(
            query_planner=planner,
            history_provider=repository.get_recent_selected_identities,
        ),
        PublicCommerceProvider(
            query_planner=planner,
            history_provider=repository.get_recent_selected_identities,
        ),
        TikTokSnacksCategoryProvider(),
    ]
    return TrendDiscoveryService(repository=repository, providers=providers, config=cfg)


class TrendDiscoveryService:
    """Orchestrates multi-source discovery, identity resolution, scoring, and selection."""

    def __init__(
        self,
        repository: TrendHistoryRepository,
        providers: list[TrendDiscoveryProvider] | None = None,
        config: TrendDiscoveryConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.repository = repository
        self.config = config or TrendDiscoveryConfig()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if providers is not None:
            self.providers = providers
        else:
            planner = QueryPlanner()
            self.providers = [
                GoogleTrendsProvider(),
                WebSearchProvider(
                    query_planner=planner,
                    history_provider=repository.get_recent_selected_identities,
                ),
                PublicCommerceProvider(
                    query_planner=planner,
                    history_provider=repository.get_recent_selected_identities,
                ),
                TikTokSnacksCategoryProvider(),
            ]

    def discover(
        self,
        criteria: ParsedCriteria,
        *,
        run_date: date,
    ) -> DiscoveryResult:
        from product_scout.workflow_policy import require_product_discovery

        require_product_discovery()
        now = self.clock()
        criteria_dict = (
            criteria.__dict__ if hasattr(criteria, "__dict__") else criteria.model_dump()
        )
        run_id = self.repository.begin_run(run_date, criteria_dict)

        provider_summaries: list[ProviderRunSummary] = []
        collected_observations: list[CandidateObservation] = []

        # Pre-warm any shared QueryPlanner before starting per-provider timeout clocks
        warmed_planners: set[int] = set()
        for provider in self.providers:
            planner = getattr(provider, "_planner", None)
            hist_fn = getattr(provider, "_history_provider", None)
            q_cnt = getattr(provider, "_query_count", 4)
            if planner is not None and id(planner) not in warmed_planners:
                warmed_planners.add(id(planner))
                try:
                    recent_ids = hist_fn() if callable(hist_fn) else []
                    planner.plan_queries(
                        criteria,
                        run_at=now,
                        recent_identities=recent_ids,
                        web_count=q_cnt,
                        commerce_count=q_cnt,
                    )
                except Exception as exc:
                    logger.debug("QueryPlanner pre-warm skipped: %s", exc)

        # 1. Run providers concurrently with true timeout isolation
        provider_futures = {}
        provider_start_times = {}
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(self.providers)))
        try:
            for provider in self.providers:
                t0 = time.perf_counter()
                provider_start_times[provider.name] = t0
                fut = executor.submit(
                    provider.discover,
                    criteria,
                    run_at=now,
                    limit=self.config.raw_candidate_limit,
                )
                provider_futures[fut] = provider

            for future, provider in provider_futures.items():
                t0 = provider_start_times[provider.name]
                elapsed_so_far = time.perf_counter() - t0
                remaining = max(0.01, self.config.provider_timeout_seconds - elapsed_so_far)
                try:
                    rows = future.result(timeout=remaining)
                    elapsed_ms = int((time.perf_counter() - t0) * 1000)
                    provider_summaries.append(
                        ProviderRunSummary(
                            provider=provider.name,
                            status="success",
                            observation_count=len(rows),
                            latency_ms=elapsed_ms,
                        )
                    )
                    collected_observations.extend(rows)
                except TimeoutError:
                    elapsed_ms = int((time.perf_counter() - t0) * 1000)
                    logger.warning("Provider %s timed out after %sms", provider.name, elapsed_ms)
                    provider_summaries.append(
                        ProviderRunSummary(
                            provider=provider.name,
                            status="failed",
                            observation_count=0,
                            latency_ms=elapsed_ms,
                            error_code="TimeoutError",
                        )
                    )
                except Exception as exc:
                    elapsed_ms = int((time.perf_counter() - t0) * 1000)
                    err_code = type(exc).__name__
                    logger.warning("Provider %s failed: %s", provider.name, exc)
                    provider_summaries.append(
                        ProviderRunSummary(
                            provider=provider.name,
                            status="failed",
                            observation_count=0,
                            latency_ms=elapsed_ms,
                            error_code=err_code,
                        )
                    )
        finally:
            # Do NOT block waiting for stuck threads!
            executor.shutdown(wait=False, cancel_futures=True)

        raw_count = len(collected_observations)

        # 2. Check for empty observations -> Fail run
        if raw_count == 0:
            self.repository.fail_run(
                run_id,
                "no_discovery_evidence",
                "No observations collected across providers",
            )
            return DiscoveryResult(
                run_id=run_id,
                status="failed",
                failure_code="no_discovery_evidence",
                failure_message="No observations collected across providers",
                provider_summary=provider_summaries,
                selection=SelectionResult(),
                raw_candidate_count=0,
                deduplicated_candidate_count=0,
                enriched_candidate_count=0,
                eligible_candidate_count=0,
            )

        # 3. Deduplicate and group by canonical key
        grouped: dict[str, list[CandidateObservation]] = {}
        candidate_meta: dict[str, dict[str, str]] = {}
        observation_rows: list[tuple[str, CandidateObservation]] = []

        for obs in collected_observations:
            # Extract brand, model, clean name if missing
            brand = obs.brand
            model = obs.model
            name = obs.candidate_name
            if not brand or not model:
                ext_b, ext_m, ext_n = extract_product_identity(obs.title or name, obs.snippet or "", category=criteria.category)
                if not brand and ext_b:
                    brand = ext_b
                if not model and ext_m:
                    model = ext_m
                if ext_n and (len(ext_n) < len(name) or name == obs.title):
                    name = ext_n

            ck = canonical_key(brand, model, name)
            if not ck:
                continue
            if ck not in grouped:
                grouped[ck] = []
                candidate_meta[ck] = {
                    "name": name,
                    "brand": brand,
                    "model": model,
                }
            else:
                # Update brand/model if previously missing
                if not candidate_meta[ck]["brand"] and brand:
                    candidate_meta[ck]["brand"] = brand
                if not candidate_meta[ck]["model"] and model:
                    candidate_meta[ck]["model"] = model

            grouped[ck].append(obs)
            observation_rows.append((ck, obs))

        # Save observations to repository
        self.repository.save_observations(run_id, observation_rows)

        dedup_count = len(grouped)

        # 4. Build AggregatedCandidates with extracted price and verified category
        aggregated_candidates: list[AggregatedCandidate] = []
        for ck, obs_list in grouped.items():
            meta = candidate_meta[ck]

            # Extract observed prices across observations
            all_prices: list[int] = []
            for obs in obs_list:
                text_to_scan = f"{obs.title} {obs.snippet} {obs.candidate_name}"
                all_prices.extend(extract_prices_from_text(text_to_scan))

            cand_price_min = min(all_prices) if all_prices else None
            cand_price_max = max(all_prices) if all_prices else None

            # Check category compatibility
            all_obs_text = " ".join(f"{obs.title} {obs.snippet}" for obs in obs_list)
            is_match, is_conflict = check_category_compatibility(f"{meta['name']} {all_obs_text}", criteria.category)

            if is_conflict:
                cand_category = "conflict"
            elif is_match:
                cand_category = criteria.category
            else:
                cand_category = ""

            candidate = AggregatedCandidate(
                canonical_key=ck,
                name=meta["name"],
                brand=meta["brand"],
                model=meta["model"],
                category=cand_category,
                price_min=cand_price_min,
                price_max=cand_price_max,
                keywords=[meta["name"]],
                observations=obs_list,
            )
            if not is_usable_product_identity(meta["name"], meta["brand"], meta["model"]) and not has_complete_tiktok_listing(candidate):
                continue
            aggregated_candidates.append(candidate)

        # Sort preliminary by identity quality, observation count, and best search rank
        def _preliminary_sort_key(c: AggregatedCandidate):
            has_brand = 1 if c.brand else 0
            has_model = 1 if c.model else 0
            has_prod = 1 if any(
                kw in c.name.lower()
                for kw in (
                    "tai nghe", "chuot", "chuột", "ban phim", "bàn phím", "sac", "sạc",
                    "loa", "smartwatch", "dong ho", "đồng hồ", "robot", "camera", "wifi",
                    "quat", "quạt", "củ", "dây", "cáp", "pin", "hub"
                )
            ) else 0
            ident_score = (has_brand * 3) + (has_model * 2) + has_prod
            obs_count = len(c.observations)
            min_rank = min((obs.source_rank for obs in c.observations if obs.source_rank > 0), default=20)
            return (-ident_score, -obs_count, min_rank)

        aggregated_candidates.sort(key=_preliminary_sort_key)
        primary_cands: list[AggregatedCandidate] = []
        overflow_cands: list[AggregatedCandidate] = []
        prelim_brand_counts: dict[str, int] = {}
        for c in aggregated_candidates:
            b_key = c.brand.strip().casefold() or c.canonical_key
            cnt = prelim_brand_counts.get(b_key, 0)
            if cnt < 2:
                primary_cands.append(c)
                prelim_brand_counts[b_key] = cnt + 1
            else:
                overflow_cands.append(c)
        diversified_candidates = primary_cands + overflow_cands
        top_candidates = diversified_candidates[: self.config.enrichment_limit]
        enriched_count = len(top_candidates)

        # 5. Score candidates
        scored_candidates: list[ScoredCandidate] = []
        for cand in top_candidates:
            history = self.repository.get_product_history(cand.canonical_key)
            prev_signal = self.repository.get_previous_signal(cand.canonical_key, before=now)
            scored = score_candidate(
                cand,
                history,
                prev_signal,
                now=now,
                criteria=criteria,
                config=self.config,
            )
            scored_candidates.append(scored)

        # 6. Portfolio selection
        selector = PortfolioSelector(self.config)
        selection = selector.select(scored_candidates, limit=criteria.limit, now=now)

        return DiscoveryResult(
            run_id=run_id,
            status="completed",
            candidates=top_candidates,
            selection=selection,
            provider_summary=provider_summaries,
            raw_candidate_count=raw_count,
            deduplicated_candidate_count=dedup_count,
            enriched_candidate_count=enriched_count,
            eligible_candidate_count=selection.eligible_count,
        )
