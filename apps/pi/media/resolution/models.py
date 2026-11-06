"""Multi-source product resolution contracts.

Separates CanonicalProduct (intrinsic identity) from ProductVariant
(intrinsic configurable difference) and ProductListing (seller/source
commercial offer). Every resolved value keeps evidence lineage.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from media.evidence.models import ClaimEvidence, SpecificationEvidence


class ResolutionStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    AMBIGUOUS = "ambiguous"
    NO_RESOLUTION = "no_resolution"
    ERROR = "error"


class IdentityMatchStatus(StrEnum):
    SAME_PRODUCT = "same_product"
    LIKELY_SAME_PRODUCT = "likely_same_product"
    DIFFERENT_PRODUCT = "different_product"
    UNRESOLVED = "unresolved"


class ConflictStatus(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    REQUIRES_REVIEW = "requires_review"


class ResolvedField(BaseModel):
    value: object | None = None
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    source_values: list = Field(default_factory=list)
    decision: str = ""
    conflict_status: ConflictStatus = ConflictStatus.RESOLVED


class ResolutionConflict(BaseModel):
    field: str
    candidate_values: list = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    severity: str = "warning"
    status: ConflictStatus = ConflictStatus.UNRESOLVED


class ResolutionDecision(BaseModel):
    decision_type: str
    field: str = ""
    selected_value: object | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str = ""

    def __init__(self, decision_type, field="", selected_value=None, evidence_ids=None, reason=""):
        super().__init__(decision_type=decision_type, field=field, selected_value=selected_value, evidence_ids=list(evidence_ids or []), reason=reason)


class ProductVariant(BaseModel):
    variant_id: str
    canonical_product_id: str
    dimensions: dict[str, str] = Field(default_factory=dict)
    supporting_evidence_ids: list[str] = Field(default_factory=list)


class ProductListing(BaseModel):
    listing_id: str
    canonical_product_id: str
    variant_id: str | None = None
    source_url: str = ""
    host: str | None = None
    seller: str | None = None
    price: float | int | None = None
    currency: str | None = None
    availability: str | None = None
    rating: float | None = None
    review_count: int | None = None
    sold_count: int | None = None
    source_evidence_ids: list[str] = Field(default_factory=list)
    observed_at: str = ""
    low_price: float | int | None = None
    high_price: float | int | None = None
    offer_count: int | None = None


class CanonicalProduct(BaseModel):
    canonical_product_id: str
    name: ResolvedField | None = None
    brand: ResolvedField | None = None
    model: ResolvedField | None = None
    category: ResolvedField | None = None
    description: ResolvedField | None = None
    specifications: list[ResolvedField] = Field(default_factory=list)
    specification_evidence: list[SpecificationEvidence] = Field(default_factory=list)
    claims: list[ClaimEvidence] = Field(default_factory=list)
    media_refs: list[str] = Field(default_factory=list)
    variants: list[ProductVariant] = Field(default_factory=list)
    listings: list[ProductListing] = Field(default_factory=list)
    variant_ids: list[str] = Field(default_factory=list)
    listing_ids: list[str] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)


class ProductResolutionResult(BaseModel):
    status: ResolutionStatus
    canonical_products: list[CanonicalProduct] = Field(default_factory=list)
    unresolved_evidence_ids: list[str] = Field(default_factory=list)
    conflicts: list[ResolutionConflict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    decisions: list[ResolutionDecision] = Field(default_factory=list)
