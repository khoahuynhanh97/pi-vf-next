"""Domain contracts for the F1-F5 Video Factory."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


class ClaimStatus(str, Enum):
    VERIFIED = "verified"
    USER_PROVIDED_UNVERIFIED = "user_provided_unverified"
    UNSUPPORTED = "unsupported"
    RESTRICTED = "restricted"


class ProjectStatus(str, Enum):
    DRAFT = "draft"
    RESOURCE_READY = "resource_ready"
    BRIEF_READY = "brief_ready"
    SCENE_PLAN_READY = "scene_plan_ready"
    READY_FOR_STORYBOARD = "ready_for_storyboard"
    STORYBOARD_READY = "storyboard_ready"
    STORYBOARD_APPROVED = "storyboard_approved"
    SCENES_GENERATED = "scenes_generated"
    TIMELINE_READY = "timeline_ready"
    DRAFT_VIDEO_READY = "draft_video_ready"
    READY_TO_PUBLISH = "ready_to_publish"
    AB_VARIANTS_READY = "ab_variants_ready"


class FrameGenerationStatus(str, Enum):
    PLANNED = "planned"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"


class StoryboardApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REVISION_REQUIRED = "revision_required"


class VideoGenerationStatus(str, Enum):
    PENDING = "pending"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"


class TimelineStatus(str, Enum):
    DRAFT = "draft"
    RENDERING = "rendering"
    COMPLETED = "completed"
    FAILED = "failed"


class FinalApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REVISION_REQUIRED = "revision_required"


@dataclass(frozen=True)
class AssetReference:
    asset_id: str
    uri: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.asset_id.strip() or not self.uri.strip():
            raise ValueError("asset_id and uri are required")


@dataclass(frozen=True)
class ResourceIdentity:
    description: str
    shape: str = ""
    color: str = ""
    materials: str = ""
    logo_placement: str = ""
    distinctive_features: tuple[str, ...] = ()
    silhouette: str = ""
    main_proportions: str = ""
    product_count: str = ""
    control_placement: str = ""
    port_count: str = ""
    port_placement: str = ""
    display_shape: str = ""

    def __post_init__(self) -> None:
        if not self.description.strip():
            raise ValueError("identity description is required")


@dataclass(frozen=True)
class ResourcePack:
    id: str
    owner_user_id: str
    product_references: tuple[AssetReference, ...]
    primary_product_asset_id: str
    product_identity_description: str
    locked_product_identity: ResourceIdentity | None = None
    character_references: tuple[AssetReference, ...] = ()
    primary_character_asset_id: str | None = None
    character_identity_description: str = ""
    locked_character_identity: ResourceIdentity | None = None
    default_outfit: str = ""
    context: str = ""
    visual_style: str = ""
    locked_at: str | None = None
    version: int = 1

    def __post_init__(self) -> None:
        if not self.owner_user_id.strip() or not self.product_references:
            raise ValueError("owner_user_id and product_references are required")
        product_ids = {item.asset_id for item in self.product_references}
        if self.primary_product_asset_id not in product_ids:
            raise ValueError("primary_product_asset_id must reference a product image")
        if self.primary_character_asset_id and self.primary_character_asset_id not in {
            item.asset_id for item in self.character_references
        }:
            raise ValueError("primary_character_asset_id must reference a character image")
        if self.locked_product_identity and not self.locked_at:
            raise ValueError("locked product identity requires locked_at")


@dataclass(frozen=True)
class RawIdea:
    text: str
    required_elements: tuple[str, ...] = ()
    required_cta: str = ""
    target_duration_seconds: int | None = None
    platform: str = ""
    aspect_ratio: str = ""

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("raw idea text is required")
        if self.target_duration_seconds is not None and self.target_duration_seconds <= 0:
            raise ValueError("target_duration_seconds must be positive")


@dataclass(frozen=True)
class Claim:
    claim: str
    status: ClaimStatus
    evidence_refs: tuple[str, ...] = ()
    restriction_reason: str = ""

    def __post_init__(self) -> None:
        if not self.claim.strip():
            raise ValueError("claim is required")
        if self.status in (ClaimStatus.UNSUPPORTED, ClaimStatus.RESTRICTED) and not self.restriction_reason.strip():
            raise ValueError("unsupported or restricted claims require a reason")


@dataclass(frozen=True)
class CreativeBrief:
    objective: str
    target_audience: str
    core_message: str
    tone: str
    pace: str
    cta: str
    content_blocks: tuple[str, ...]
    verified_selling_points: tuple[Claim, ...] = ()
    restrictions: tuple[str, ...] = ()
    required_content: tuple[str, ...] = ()
    platform: str = ""
    aspect_ratio: str = ""
    target_duration_seconds: int | None = None

    def __post_init__(self) -> None:
        for name in ("objective", "target_audience", "core_message"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} is required")
        if not self.content_blocks:
            raise ValueError("content_blocks must not be empty")
        if self.target_duration_seconds is not None and self.target_duration_seconds <= 0:
            raise ValueError("target_duration_seconds must be positive")


@dataclass(frozen=True)
class Scene:
    scene_id: str
    order: int
    title: str
    objective: str
    content: str
    main_action: str
    duration_seconds: int
    context: str = ""
    camera_intention: str = ""
    start_state: str = ""
    end_state: str = ""
    required_resources: tuple[str, ...] = ()
    notes: str = ""
    status: str = "planned"

    def __post_init__(self) -> None:
        if self.order < 1 or self.duration_seconds <= 0:
            raise ValueError("scene order and duration_seconds must be positive")
        if not self.scene_id.strip() or not self.title.strip():
            raise ValueError("scene_id and title are required")


@dataclass(frozen=True)
class ScenePlan:
    scenes: tuple[Scene, ...]

    def __post_init__(self) -> None:
        if not self.scenes:
            raise ValueError("scene plan must contain at least one scene")
        orders = [scene.order for scene in self.scenes]
        if len(orders) != len(set(orders)) or sorted(orders) != list(range(1, len(orders) + 1)):
            raise ValueError("scene orders must be unique and contiguous")

    @property
    def total_duration_seconds(self) -> int:
        return sum(scene.duration_seconds for scene in self.scenes)


@dataclass(frozen=True)
class FramePrompt:
    positive_prompt: str
    negative_constraints: str = ""
    product_identity_constraints: str = ""
    character_identity_constraints: str = ""
    composition: str = ""
    camera: str = ""
    lighting: str = ""
    environment: str = ""
    action: str = ""
    reference_asset_ids: tuple[str, ...] = ()
    aspect_ratio: str = ""
    provider_options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.positive_prompt.strip():
            raise ValueError("positive_prompt is required")


@dataclass(frozen=True)
class StoryboardFrame:
    frame_id: str
    scene_id: str
    order: int
    label: str
    purpose: str
    visual_state: str
    subject_action: str
    product_state: str
    character_state: str
    context: str
    camera_intention: str
    required_resource_ids: tuple[str, ...] = ()
    prompt: FramePrompt | None = None
    generation_status: FrameGenerationStatus = FrameGenerationStatus.PLANNED
    generated_asset_id: str | None = None
    generation_job_id: str | None = None
    review_notes: str = ""
    version: int = 1
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.frame_id.strip() or not self.scene_id.strip():
            raise ValueError("frame_id and scene_id are required")
        if self.order < 1:
            raise ValueError("order must be positive")


@dataclass(frozen=True)
class Storyboard:
    storyboard_id: str
    project_id: str
    frames: tuple[StoryboardFrame, ...]
    approval_status: StoryboardApprovalStatus = StoryboardApprovalStatus.PENDING
    approval_notes: str = ""
    version: int = 1
    created_at: str = ""
    updated_at: str = ""
    source_scene_version: int = 0

    def __post_init__(self) -> None:
        if not self.storyboard_id.strip() or not self.project_id.strip():
            raise ValueError("storyboard_id and project_id are required")
        if not self.frames:
            raise ValueError("storyboard must contain at least one frame")


@dataclass(frozen=True)
class VideoPrompt:
    scene_id: str
    duration_seconds: int
    start_visual_state: str = ""
    end_visual_state: str = ""
    subject_action: str = ""
    product_action: str = ""
    camera_movement: str = ""
    camera_framing: str = ""
    environment_motion: str = ""
    motion_constraints: str = ""
    identity_constraints: str = ""
    reference_frame_ids: tuple[str, ...] = ()
    dialogue_or_vo: str = ""
    negative_constraints: str = ""
    provider_options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.scene_id.strip():
            raise ValueError("scene_id is required")
        if self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")


@dataclass(frozen=True)
class GeneratedScene:
    scene_id: str
    video_prompt: VideoPrompt
    generation_status: VideoGenerationStatus = VideoGenerationStatus.PENDING
    generated_asset_id: str | None = None
    generation_job_id: str | None = None
    provider_operation_id: str | None = None
    review_notes: str = ""
    version: int = 1
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.scene_id.strip():
            raise ValueError("scene_id is required")


@dataclass(frozen=True)
class TimelineClip:
    clip_id: str
    order: int
    source_asset_id: str
    trim_start_seconds: float = 0.0
    trim_end_seconds: float | None = None
    duration_seconds: float = 0.0
    transition: str = ""
    audio_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.clip_id.strip() or not self.source_asset_id.strip():
            raise ValueError("clip_id and source_asset_id are required")
        if self.order < 1:
            raise ValueError("order must be positive")


@dataclass(frozen=True)
class Timeline:
    timeline_id: str
    project_id: str
    clips: tuple[TimelineClip, ...]
    audio_track_asset_id: str | None = None
    music_asset_id: str | None = None
    status: TimelineStatus = TimelineStatus.DRAFT
    version: int = 1
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.timeline_id.strip() or not self.project_id.strip():
            raise ValueError("timeline_id and project_id are required")
        if not self.clips:
            raise ValueError("timeline must contain at least one clip")
        orders = [clip.order for clip in self.clips]
        if len(orders) != len(set(orders)) or sorted(orders) != list(range(1, len(orders) + 1)):
            raise ValueError("clip orders must be unique and contiguous")


@dataclass(frozen=True)
class HookVariant:
    """A single A/B hook variant for a video."""
    variant_id: str
    variant_label: str
    hook_angle: str
    creative_brief: CreativeBrief
    scene_plan: ScenePlan
    storyboard: Storyboard | None = None
    generated_scenes: tuple[GeneratedScene, ...] = ()
    timeline: Timeline | None = None
    final_asset_id: str = ""
    export_status: str = "pending"

    def __post_init__(self) -> None:
        if not self.variant_id.strip() or not self.variant_label.strip():
            raise ValueError("variant_id and variant_label are required")


@dataclass(frozen=True)
class ABVariantSet:
    """Collection of A/B variants for a project."""
    variants: tuple[HookVariant, ...] = ()
    selected_variant_id: str = ""


@dataclass(frozen=True)
class VideoFactoryProject:
    id: str
    owner_user_id: str
    status: ProjectStatus = ProjectStatus.DRAFT
    resource_pack: ResourcePack | None = None
    raw_idea: RawIdea | None = None
    creative_brief: CreativeBrief | None = None
    brief_approval: str = "pending"
    scene_plan: ScenePlan | None = None
    scene_plan_approval: str = "pending"
    storyboard: Storyboard | None = None
    generated_scenes: tuple[GeneratedScene, ...] = ()
    timeline: Timeline | None = None
    draft_video_asset_id: str | None = None
    final_video_asset_id: str | None = None
    final_approval: FinalApprovalStatus = FinalApprovalStatus.PENDING
    final_approval_notes: str = ""
    ab_variants: ABVariantSet | None = None
    resource_version: int = 0
    idea_version: int = 0
    brief_version: int = 0
    scene_version: int = 0
    storyboard_version: int = 0
    video_generation_version: int = 0
    timeline_version: int = 0
    revision: int = 0
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.owner_user_id.strip():
            raise ValueError("project id and owner_user_id are required")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VideoFactoryArtifact:
    """Tracked artifact in the canonical project inventory."""
    artifact_id: str
    owner_user_id: str
    workflow_id: str
    project_id: str
    kind: str
    relative_path: str
    sha256: str
    scene_id: str = ""
    frame_role: str = ""
    version: int = 1
    attempt: int = 1
    size: int = 0
    mime_type: str = ""
    status: str = "active"
    parent_artifact_ids: tuple[str, ...] = ()
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.artifact_id.strip() or not self.project_id.strip():
            raise ValueError("artifact_id and project_id are required")


@dataclass(frozen=True)
class RenderBatch:
    """Durable render batch for multi-scene video generation."""
    render_batch_id: str
    owner_user_id: str
    workflow_id: str
    project_id: str
    version: int = 1
    status: str = "pending"
    scene_count: int = 0
    completed_scene_count: int = 0
    timeline_artifact_id: str | None = None
    composite_artifact_id: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.render_batch_id.strip() or not self.project_id.strip():
            raise ValueError("render_batch_id and project_id are required")
