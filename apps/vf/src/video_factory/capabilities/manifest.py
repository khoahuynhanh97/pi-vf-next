from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from video_factory.capabilities.contracts import CostClass, RiskLevel


@dataclass(frozen=True)
class CapabilitySpec:
    capability_id: str
    owner: str
    input_schema_version: str
    output_schema_version: str
    allowed_skills: tuple[str, ...]
    allowed_workflows: tuple[str, ...]
    approval_policy: str
    risk: RiskLevel
    cost_class: CostClass

    def __post_init__(self) -> None:
        for field_name in (
            "capability_id",
            "owner",
            "input_schema_version",
            "output_schema_version",
            "approval_policy",
        ):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be blank")


_MANIFEST_ENTRIES: list[CapabilitySpec] = [
    CapabilitySpec(
        capability_id="product.research",
        owner="product_intelligence",
        input_schema_version="1.0",
        output_schema_version="1.0",
        allowed_skills=(
            "hermes-personal-product-research",
            "hermes-personal-product-video-production",
        ),
        allowed_workflows=("product_research", "product_to_video"),
        approval_policy="none",
        risk="low",
        cost_class="free",
    ),
    CapabilitySpec(
        capability_id="product.resource_pack.build",
        owner="product_intelligence",
        input_schema_version="1.0",
        output_schema_version="1.0",
        allowed_skills=(
            "hermes-personal-product-research",
            "hermes-personal-product-video-production",
        ),
        allowed_workflows=("product_research", "product_to_video"),
        approval_policy="none",
        risk="medium",
        cost_class="metered",
    ),
    CapabilitySpec(
        capability_id="product.resource_pack.lock",
        owner="product_intelligence",
        input_schema_version="1.0",
        output_schema_version="1.0",
        allowed_skills=(
            "hermes-personal-product-research",
            "hermes-personal-product-video-production",
        ),
        allowed_workflows=("product_research", "product_to_video"),
        approval_policy="hitl:product_lock",
        risk="high",
        cost_class="free",
    ),
    CapabilitySpec(
        capability_id="video.brief.create",
        owner="video_factory",
        input_schema_version="1.0",
        output_schema_version="1.0",
        allowed_skills=("hermes-personal-product-video-production",),
        allowed_workflows=("product_to_video",),
        approval_policy="precondition:product_locked",
        risk="medium",
        cost_class="metered",
    ),
    CapabilitySpec(
        capability_id="video.scene_plan.create",
        owner="video_factory",
        input_schema_version="1.0",
        output_schema_version="1.0",
        allowed_skills=("hermes-personal-product-video-production",),
        allowed_workflows=("product_to_video",),
        approval_policy="precondition:brief_approved",
        risk="medium",
        cost_class="metered",
    ),
    CapabilitySpec(
        capability_id="video.storyboard.create",
        owner="video_factory",
        input_schema_version="1.0",
        output_schema_version="1.0",
        allowed_skills=("hermes-personal-product-video-production",),
        allowed_workflows=("product_to_video",),
        approval_policy="precondition:scene_approved",
        risk="medium",
        cost_class="paid",
    ),
    CapabilitySpec(
        capability_id="video.render",
        owner="video_factory",
        input_schema_version="1.0",
        output_schema_version="1.0",
        allowed_skills=("hermes-personal-product-video-production",),
        allowed_workflows=("product_to_video",),
        approval_policy="hitl:storyboard_and_budget",
        risk="high",
        cost_class="paid",
    ),
    CapabilitySpec(
        capability_id="video.export",
        owner="video_factory",
        input_schema_version="1.0",
        output_schema_version="1.0",
        allowed_skills=("hermes-personal-product-video-production",),
        allowed_workflows=("product_to_video",),
        approval_policy="hitl:final",
        risk="high",
        cost_class="paid",
    ),
    CapabilitySpec(
        capability_id="knowledge.candidate.create",
        owner="learning",
        input_schema_version="1.0",
        output_schema_version="1.0",
        allowed_skills=("hermes-personal-knowledge-learning",),
        allowed_workflows=("knowledge_learning",),
        approval_policy="review_required",
        risk="medium",
        cost_class="free",
    ),
    CapabilitySpec(
        capability_id="knowledge.retrieve",
        owner="learning",
        input_schema_version="1.0",
        output_schema_version="1.0",
        allowed_skills=(
            "hermes-personal-product-research",
            "hermes-personal-product-video-production",
            "hermes-personal-knowledge-learning",
        ),
        allowed_workflows=(
            "product_research",
            "product_to_video",
            "knowledge_learning",
        ),
        approval_policy="approved_only",
        risk="low",
        cost_class="free",
    ),
]


def _build_initial_capabilities(
    entries: list[CapabilitySpec],
) -> Mapping[str, CapabilitySpec]:
    mapping: dict[str, CapabilitySpec] = {}
    for spec in entries:
        if spec.capability_id in mapping:
            raise ValueError(
                f"duplicate capability_id in manifest entries: {spec.capability_id}"
            )
        mapping[spec.capability_id] = spec
    return MappingProxyType(mapping)


INITIAL_CAPABILITIES: Mapping[str, CapabilitySpec] = _build_initial_capabilities(
    _MANIFEST_ENTRIES
)
