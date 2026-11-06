"""Source discovery orchestration: query → candidates → diverse selection.

Bounded and diversity-aware. Search ranking is never treated as authority.
"""

from __future__ import annotations

from dataclasses import dataclass

from media.models import ProductIdentityHint

from .classify import build_discovery_query, classify_source
from .models import SourceCandidate, SourceClass, SourceDiscoveryProvider
from .providers import DuckDuckGoSourceDiscoveryProvider
from .relevance import RelevanceLevel, assess_relevance


@dataclass
class SourceDiscoveryConfig:
    max_sources: int = 8
    per_host_limit: int = 2
    target_usable_sources: int = 3
    max_results: int = 15
    diversity_order: tuple[SourceClass, ...] = (
        SourceClass.OFFICIAL,
        SourceClass.RETAILER,
        SourceClass.MARKETPLACE,
        SourceClass.REVIEW,
        SourceClass.UNKNOWN,
    )


class SourceDiscoveryService:
    def __init__(self, *, provider: SourceDiscoveryProvider | None = None, config: SourceDiscoveryConfig | None = None, official_domains: set[str] | None = None):
        self.provider = provider or DuckDuckGoSourceDiscoveryProvider(max_results=(config or SourceDiscoveryConfig()).max_results)
        self.config = config or SourceDiscoveryConfig()
        self.official_domains = official_domains or self._default_official_domains()

    @staticmethod
    def _default_official_domains() -> set[str]:
        import json
        from pathlib import Path

        try:
            data = json.loads((Path(__file__).parent.parent.parent / "product_scout" / "official_domains.json").read_text(encoding="utf-8"))
            return {item.casefold().removeprefix("www.") for values in data.values() for item in values}
        except (OSError, ValueError):
            return set()

    def discover(self, hint: ProductIdentityHint, *, max_sources: int | None = None, target_usable: int | None = None) -> list[SourceCandidate]:
        if not hint.is_usable:
            return []
        query_text = build_discovery_query(hint)
        try:
            candidates = self.provider.search(self._query(query_text))
        except Exception:  # noqa: BLE001 - discovery failure is non-fatal for research
            return []
        classified = [classify_source(candidate, self.official_domains) for candidate in candidates]
        # Relevance before diversity: annotate then filter LOW.
        for candidate in classified:
            candidate.relevance, candidate.relevance_reason = assess_relevance(candidate, hint)
        eligible = [candidate for candidate in classified if candidate.relevance != RelevanceLevel.LOW]
        # Sort HIGH before MEDIUM, then by discovery rank.
        priority = {RelevanceLevel.HIGH: 0, RelevanceLevel.MEDIUM: 1}
        eligible.sort(key=lambda candidate: (priority.get(candidate.relevance, 2), candidate.rank))
        return self._select(eligible, max_sources=max_sources, target_usable=target_usable)

    def _query(self, text: str):
        from .models import SourceDiscoveryQuery

        return SourceDiscoveryQuery(query=text, max_results=self.config.max_results)

    def _select(self, candidates: list[SourceCandidate], *, max_sources: int | None = None, target_usable: int | None = None) -> list[SourceCandidate]:
        budget = max_sources or self.config.max_sources
        per_host = self.config.per_host_limit

        # Dedup by URL.
        seen_urls: set[str] = set()
        unique = [c for c in candidates if not (c.url in seen_urls or seen_urls.add(c.url))]
        # Rank diversity: iterate diversity_order, take per-host-limited candidates.
        selected: list[SourceCandidate] = []
        host_counts: dict[str, int] = {}
        used_classes: set[SourceClass] = set()
        for source_class in self.config.diversity_order:
            for candidate in unique:
                if candidate.source_class != source_class:
                    continue
                if len(selected) >= budget:
                    break
                if host_counts.get(candidate.host, 0) >= per_host:
                    continue
                host_counts[candidate.host] = host_counts.get(candidate.host, 0) + 1
                selected.append(candidate)
                used_classes.add(source_class)
            if len(selected) >= budget:
                break
            if len(used_classes) >= len(self.config.diversity_order):
                break
        return selected[:budget]

    @property
    def target_usable(self) -> int:
        return self.config.target_usable_sources
