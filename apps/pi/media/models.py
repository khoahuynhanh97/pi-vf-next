"""Pydantic contracts for Media Intelligence."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from .config import MediaConfig


class MatchStatus(StrEnum):
    MATCHED = "matched"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"
    NEEDS_SEMANTIC_REVIEW = "needs_semantic_review"


class ResearchStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    NO_USABLE_ASSETS = "no_usable_assets"
    ERROR = "error"


class ViewClass(StrEnum):
    HERO = "hero"
    FRONT = "front"
    BACK = "back"
    SIDE = "side"
    DETAIL = "detail"
    CONTROLS = "controls"
    PACKAGING = "packaging"
    LIFESTYLE = "lifestyle"
    FEATURE_GRAPHIC = "feature_graphic"
    TEXT_HEAVY = "text_heavy"
    UNKNOWN = "unknown"


class SourceClass(StrEnum):
    OFFICIAL = "official"
    MARKETPLACE = "marketplace"
    RETAILER = "retailer"
    REVIEW = "review"
    UNKNOWN = "unknown"


class UsagePolicy(StrEnum):
    REFERENCE_ONLY = "reference_only"
    USER_PROVIDED = "user_provided"
    LICENSED = "licensed"
    UNKNOWN = "unknown"


class ImageSearchQuery(BaseModel):
    query: str
    initial_results: int = 5
    max_results: int = 15


class ImageCandidate(BaseModel):
    candidate_id: str
    discovery_provider: str
    query: str
    search_result_rank: int | None = None
    image_url: str
    source_page_url: str | None = None
    host: str | None = None
    title: str | None = None
    discovered_at: str


class ProductIdentityHint(BaseModel):
    name: str = ""
    brand: str | None = None
    model: str | None = None
    variant: str | None = None
    category: str | None = None
    flavor: str | None = None
    spec: str | None = None
    keywords: list[str] = Field(default_factory=list)
    visual_description: str | None = None
    reference_asset_ids: list[str] = Field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        return bool(self.name or self.brand or self.model or self.keywords)


class TechnicalImageResult(BaseModel):
    """Structured outcome of technical image validation for one candidate."""

    candidate_id: str
    image_url: str
    status: Literal["valid", "rejected"]
    reason: str | None = None
    width: int | None = None
    height: int | None = None
    format: str | None = None
    sha256: str | None = None
    phash: str | None = None
    local_path: str | None = None
    payload_bytes: int | None = None


class ProductMatchResult(BaseModel):
    candidate_id: str
    match_status: MatchStatus
    product_match_score: float | None = Field(default=None, ge=0.0, le=1.0)
    signals: dict[str, bool] = Field(default_factory=dict)
    reason: str = ""


class ProductImageAssessment(BaseModel):
    technical_quality: float | None = Field(default=None, ge=0.0, le=1.0)
    product_match_score: float | None = Field(default=None, ge=0.0, le=1.0)
    match_status: MatchStatus = MatchStatus.UNKNOWN
    view: ViewClass = ViewClass.UNKNOWN
    duplicate_group_id: str | None = None
    selection_score: float | None = Field(default=None, ge=0.0, le=1.0)


class MediaAsset(BaseModel):
    asset_id: str
    type: Literal["image"] = "image"
    local_path: str
    sha256: str
    phash: str
    width: int
    height: int
    format: str
    discovery: dict = Field(default_factory=dict)
    source: dict = Field(default_factory=dict)
    assessment: ProductImageAssessment = Field(default_factory=ProductImageAssessment)
    provenance: dict = Field(default_factory=dict)


class Rejection(BaseModel):
    candidate_id: str | None = None
    image_url: str
    reason: str
    detail: str | None = None


class MediaResearchResult(BaseModel):
    status: ResearchStatus
    product: ProductIdentityHint
    discovered_count: int = 0
    downloaded_count: int = 0
    valid_count: int = 0
    selected_assets: list[MediaAsset] = Field(default_factory=list)
    rejections: list[Rejection] = Field(default_factory=list)
    coverage: dict = Field(default_factory=dict)
    config: MediaConfig | None = None
    error: str | None = None
