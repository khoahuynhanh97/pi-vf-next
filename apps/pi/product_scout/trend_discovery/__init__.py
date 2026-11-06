"""Fresh Trending Product Discovery package."""

from .backfill import backfill_daily_runs
from .config import TrendDiscoveryConfig
from .identity import canonical_key, normalize_identity_text
from .models import (
    AggregatedCandidate,
    BackfillSummary,
    CandidateObservation,
    DiscoveryResult,
    PreviousTrendSignal,
    ProductHistory,
    ProviderRunSummary,
    ScoreBreakdown,
    ScoredCandidate,
    SelectionResult,
)
from .providers import (
    GoogleTrendsProvider,
    PublicCommerceProvider,
    TrendDiscoveryProvider,
    WebSearchProvider,
)
from .repository import TrendHistoryRepository
from .scoring import confidence_score, score_candidate
from .selection import PortfolioSelector
from .service import TrendDiscoveryService, build_default_trend_discovery_service
from .tiktok_category import TikTokSnacksCategoryProvider

__all__ = [
    "AggregatedCandidate",
    "BackfillSummary",
    "CandidateObservation",
    "DiscoveryResult",
    "GoogleTrendsProvider",
    "PortfolioSelector",
    "PreviousTrendSignal",
    "ProductHistory",
    "ProviderRunSummary",
    "PublicCommerceProvider",
    "ScoreBreakdown",
    "ScoredCandidate",
    "SelectionResult",
    "TrendDiscoveryConfig",
    "TrendDiscoveryProvider",
    "TrendDiscoveryService",
    "TikTokSnacksCategoryProvider",
    "TrendHistoryRepository",
    "WebSearchProvider",
    "backfill_daily_runs",
    "build_default_trend_discovery_service",
    "canonical_key",
    "confidence_score",
    "normalize_identity_text",
    "score_candidate",
]
