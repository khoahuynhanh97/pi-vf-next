"""Configuration parameters and thresholds for Fresh Trending Product Discovery."""

from __future__ import annotations

from pydantic import BaseModel


class TrendDiscoveryConfig(BaseModel):
    raw_candidate_limit: int = 80
    enrichment_limit: int = 40
    cooldown_days: int = 30
    fresh_ratio: float = 0.70
    confidence_threshold: float = 60.0
    reentry_trend_threshold: float = 80.0
    reentry_delta_threshold: float = 20.0
    minimum_reentry_sources: int = 2
    trend_weight: float = 0.55
    freshness_weight: float = 0.25
    confidence_weight: float = 0.20
    config_version: str = "trend-selection-v1"
    provider_timeout_seconds: float = 30.0
