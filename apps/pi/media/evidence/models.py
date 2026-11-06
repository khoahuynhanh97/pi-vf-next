"""ProductEvidence contracts.

A ProductEvidence is what ONE source says about one product/listing. It is
explicitly not canonical truth; multi-source resolution is a later milestone.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class EvidenceExtractionStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    NO_PRODUCT_EVIDENCE = "no_product_evidence"
    ERROR = "error"


class SourceKind(StrEnum):
    JSON_LD = "json_ld"
    OPEN_GRAPH = "open_graph"
    META = "meta"
    VISIBLE_TEXT = "visible_text"
    PAGE_TITLE = "page_title"
    DERIVED = "derived"
    UNKNOWN = "unknown"


class Availability(StrEnum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    PREORDER = "preorder"
    LIMITED = "limited"
    UNKNOWN = "unknown"


class EvidenceValue(BaseModel, Generic[T]):
    value: T
    source_kind: SourceKind = SourceKind.UNKNOWN
    source_path: str | None = None
    raw_value: str | None = None
    normalization_notes: str | None = None


class EvidenceSource(BaseModel):
    requested_url: str
    final_url: str | None = None
    canonical_url: str | None = None
    host: str | None = None
    acquisition_method: str = ""
    acquisition_status: str = ""
    observed_at: str = ""
    page_title: str | None = None


class SpecificationEvidence(BaseModel):
    name: EvidenceValue[str]
    value: EvidenceValue[str]


class ClaimEvidence(BaseModel):
    text: EvidenceValue[str]


class MediaEvidence(BaseModel):
    page_image_urls: list[str] = Field(default_factory=list)
    og_image_url: str | None = None
    image_candidate_ids: list[str] = Field(default_factory=list)


class IdentityEvidence(BaseModel):
    name: EvidenceValue[str] | None = None
    brand: EvidenceValue[str] | None = None
    model: EvidenceValue[str] | None = None
    sku: EvidenceValue[str] | None = None
    mpn: EvidenceValue[str] | None = None
    gtin: EvidenceValue[str] | None = None
    variant: EvidenceValue[str] | None = None
    category: EvidenceValue[str] | None = None


class OfferEvidence(BaseModel):
    price: EvidenceValue[float | int] | None = None
    original_price: EvidenceValue[float | int] | None = None
    currency: EvidenceValue[str] | None = None
    availability: EvidenceValue[Availability] | None = None
    availability_raw: EvidenceValue[str] | None = None
    condition: EvidenceValue[str] | None = None
    url: EvidenceValue[str] | None = None
    seller_name: EvidenceValue[str] | None = None
    low_price: EvidenceValue[float | int] | None = None
    high_price: EvidenceValue[float | int] | None = None
    offer_count: EvidenceValue[int] | None = None


class ReputationEvidence(BaseModel):
    rating_value: EvidenceValue[float] | None = None
    rating_count: EvidenceValue[int] | None = None
    review_count: EvidenceValue[int] | None = None
    sold_count: EvidenceValue[int] | None = None


class ProductEvidence(BaseModel):
    evidence_id: str
    source: EvidenceSource
    identity: IdentityEvidence = Field(default_factory=IdentityEvidence)
    offer: OfferEvidence | None = None
    reputation: ReputationEvidence | None = None
    description: EvidenceValue[str] | None = None
    specifications: list[SpecificationEvidence] = Field(default_factory=list)
    claims: list[ClaimEvidence] = Field(default_factory=list)
    media: MediaEvidence = Field(default_factory=MediaEvidence)


class EvidenceConflict(BaseModel):
    field: str
    values: list[EvidenceValue] = Field(default_factory=list)


class ProductEvidenceExtractionResult(BaseModel):
    status: EvidenceExtractionStatus
    source: EvidenceSource
    evidence: ProductEvidence | None = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    conflicts: list[EvidenceConflict] = Field(default_factory=list)
