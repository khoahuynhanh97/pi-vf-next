"""Resource Pack contracts.

A Resource Pack is the trusted, locked input for Video Factory. It must
represent exactly ONE intended product identity and (where material) ONE
variant. Ambiguity is never auto-locked.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class PackStatus(StrEnum):
    READY_TO_LOCK = "ready_to_lock"
    NEEDS_REVIEW = "needs_review"
    INCOMPLETE = "incomplete"
    REJECTED = "rejected"


class ClaimStatus(StrEnum):
    VERIFIED_SPEC = "verified_spec"
    SOURCE_CLAIM = "source_claim"
    UNVERIFIED = "unverified"
    RESTRICTED = "restricted"


class PackMediaItem(BaseModel):
    asset_id: str
    local_path: str = ""
    view: str = "unknown"
    product_match_score: float | None = None
    match_status: str = "unknown"
    source_class: str = "unknown"
    usage_policy: str = "reference_only"
    variant_match: str = "unknown"


class PackClaim(BaseModel):
    claim_id: str = ""
    text: str
    status: ClaimStatus = ClaimStatus.SOURCE_CLAIM
    source_evidence_ids: list[str] = Field(default_factory=list)


class ResourcePack(BaseModel):
    resource_pack_id: str
    version: int = 1
    created_at: str = ""
    research_snapshot_id: str = ""
    canonical_product_id: str = ""
    variant_id: str | None = None
    product_identity: dict = Field(default_factory=dict)
    specifications: list[dict] = Field(default_factory=list)
    claims: list[PackClaim] = Field(default_factory=list)
    media: list[PackMediaItem] = Field(default_factory=list)
    restrictions: dict = Field(default_factory=dict)
    provenance: dict = Field(default_factory=dict)
    completeness: dict = Field(default_factory=dict)
    pack_status: PackStatus = PackStatus.NEEDS_REVIEW
    reason: str = ""


class ResourcePackDraft(BaseModel):
    """Builder output before validation/lock decisions."""

    resource_pack_id: str
    canonical_product_id: str
    variant_id: str | None = None
    product_identity: dict = Field(default_factory=dict)
    specifications: list[dict] = Field(default_factory=list)
    claims: list[PackClaim] = Field(default_factory=list)
    media: list[PackMediaItem] = Field(default_factory=list)
    restrictions: dict = Field(default_factory=dict)
    provenance: dict = Field(default_factory=dict)


class ResourcePackLock(BaseModel):
    """Immutable, versioned lock. Never mutated in place."""

    resource_pack_id: str
    version: int = 1
    created_at: str = ""
    research_snapshot_id: str = ""
    canonical_product_id: str = ""
    variant_id: str | None = None
    media_asset_ids: list[str] = Field(default_factory=list)
    resolved_values: dict = Field(default_factory=dict)
    provenance: dict = Field(default_factory=dict)
