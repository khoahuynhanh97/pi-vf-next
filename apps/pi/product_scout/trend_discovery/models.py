"""Domain models for Fresh Trending Product Discovery."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class CandidateObservation(BaseModel):
    provider: str
    candidate_name: str
    brand: str = ""
    model: str = ""
    source_url: str
    source_host: str
    source_rank: int = 0
    observed_at: datetime
    published_at: datetime | None = None
    signal_type: str
    signal_value: float | None = None
    title: str = ""
    snippet: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class AggregatedCandidate(BaseModel):
    canonical_key: str
    name: str
    brand: str = ""
    model: str = ""
    category: str = ""
    price_min: int | None = None
    price_max: int | None = None
    currency: str = "VND"
    keywords: list[str] = Field(default_factory=list)
    observations: list[CandidateObservation] = Field(default_factory=list)
    identity_conflicts: list[str] = Field(default_factory=list)


class ScoreBreakdown(BaseModel):
    source_diversity: float = 0
    recency: float = 0
    search_rank: float = 0
    velocity: float = 0
    commerce: float = 0
    identity: float = 0
    evidence_agreement: float = 0
    price_fit: float = 0
    category_fit: float = 0
    conflict_free: float = 0


class ScoredCandidate(BaseModel):
    candidate: AggregatedCandidate
    trend_score: float
    freshness_score: float
    confidence_score: float
    final_score: float
    previous_trend_score: float | None = None
    independent_source_count: int = 0
    score_breakdown: ScoreBreakdown


class SelectionResult(BaseModel):
    selected: list[ScoredCandidate] = Field(default_factory=list)
    statuses: dict[str, str] = Field(default_factory=dict)
    reason_codes: dict[str, list[str]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    eligible_count: int = 0


class ProviderRunSummary(BaseModel):
    provider: str
    status: str
    observation_count: int = 0
    latency_ms: int = 0
    error_code: str | None = None


class ProductHistory(BaseModel):
    canonical_key: str
    canonical_name: str
    brand: str = ""
    model: str = ""
    first_seen_at: datetime
    last_seen_at: datetime
    last_selected_at: datetime | None = None
    selection_count: int = 0


class PreviousTrendSignal(BaseModel):
    canonical_key: str
    trend_score: float
    recorded_at: datetime
    independent_source_count: int = 0
    min_source_rank: int | None = None


class DiscoveryResult(BaseModel):
    run_id: str
    status: str
    candidates: list[AggregatedCandidate] = Field(default_factory=list)
    selection: SelectionResult
    provider_summary: list[ProviderRunSummary] = Field(default_factory=list)
    raw_candidate_count: int = 0
    deduplicated_candidate_count: int = 0
    enriched_candidate_count: int = 0
    eligible_candidate_count: int = 0
    failure_code: str | None = None
    failure_message: str | None = None


class BackfillSummary(BaseModel):
    files_read: int = 0
    files_skipped: int = 0
    selections_imported: int = 0
    warnings: list[str] = Field(default_factory=list)
