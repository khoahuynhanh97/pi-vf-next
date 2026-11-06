"""Product Research orchestrator contracts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from media.evidence.models import EvidenceSource, ProductEvidence
from media.models import MediaAsset, ProductIdentityHint
from media.resolution.models import (
    CanonicalProduct,
    ProductListing,
    ProductVariant,
    ResolutionConflict,
)
from media.source_discovery.models import SourceCandidate


class ResearchStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    NO_PRODUCT_EVIDENCE = "no_product_evidence"
    AMBIGUOUS = "ambiguous"
    ERROR = "error"


class ResearchProductRequest(BaseModel):
    """Input for ProductResearchService. At least one usable seed is required."""

    url: str | None = None
    additional_urls: list[str] = Field(default_factory=list)
    product_identity_hint: ProductIdentityHint | None = None
    query: str | None = None
    discover_sources: bool = False
    target_assets: int | None = None
    browser_fallback: bool = True

    @property
    def is_usable(self) -> bool:
        if self.url or self.query:
            return True
        if self.additional_urls:
            return True
        if self.product_identity_hint is not None:
            return self.product_identity_hint.is_usable
        return False


class SourceResearchResult(BaseModel):
    url: str
    source_class: str = "unknown"
    acquisition_status: str = ""
    acquisition_method: str = ""
    evidence_status: str = ""
    evidence_id: str | None = None
    host: str | None = None
    observed_at: str = ""
    signals: list[str] = Field(default_factory=list)
    reason: str | None = None
    unresolved: bool = False
    usable: bool = False


def is_usable_source_result(source: SourceResearchResult) -> bool:
    """A source counts as usable for adaptive stopping only when it produced
    meaningful product evidence.

    BLOCKED / CHALLENGED / FAILED / NO_PRODUCT_EVIDENCE are never usable.
    Usability requires a surviving ProductEvidence (evidence_id present) and a
    non-failed acquisition — HTTP 200 alone is not sufficient.
    """
    if not source.evidence_id:
        return False
    return source.acquisition_status not in ("blocked", "challenged", "failed")


class ResearchSummary(BaseModel):
    sources: int = 0
    discovered_sources: int = 0
    usable_evidence: int = 0
    usable_sources: int = 0
    attempted_sources: int = 0
    stop_reason: str = ""
    unresolved_evidence: int = 0
    canonical_products: int = 0
    variants: int = 0
    listings: int = 0
    media_assets: int = 0
    media_status: str = "skipped"
    research_identity: dict = Field(default_factory=dict)


class ProductResearchResult(BaseModel):
    status: ResearchStatus
    request: ResearchProductRequest
    source_results: list[SourceResearchResult] = Field(default_factory=list)
    product_evidence: list[ProductEvidence] = Field(default_factory=list)
    canonical_products: list[CanonicalProduct] = Field(default_factory=list)
    variants: list[ProductVariant] = Field(default_factory=list)
    listings: list[ProductListing] = Field(default_factory=list)
    media_assets: list[MediaAsset] = Field(default_factory=list)
    unresolved_evidence_ids: list[str] = Field(default_factory=list)
    conflicts: list[ResolutionConflict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    summary: ResearchSummary = Field(default_factory=ResearchSummary)
    evidence_sources: list[EvidenceSource] = Field(default_factory=list)
    discovered_sources: list[SourceCandidate] = Field(default_factory=list)
    snapshot_id: str | None = None
    research_id: str | None = None
    persistence_status: str | None = None
    error: str | None = None
