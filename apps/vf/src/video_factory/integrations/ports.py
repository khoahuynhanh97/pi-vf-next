"""Video Factory domain ports and abstract interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from video_factory.integrations.domain import VideoFactoryProject, VideoFactoryArtifact, RenderBatch


OFFICIAL_PRODUCT_VIDEO_PROVIDER_KINDS = frozenset({"google_flow", "vertex_ai", "muse"})


class VideoFactoryRepository(ABC):
    @abstractmethod
    def create(self, project: VideoFactoryProject) -> VideoFactoryProject:
        raise NotImplementedError

    @abstractmethod
    def get_owned(self, project_id: str, owner_user_id: str) -> VideoFactoryProject | None:
        raise NotImplementedError

    @abstractmethod
    def list_owned(self, owner_user_id: str) -> list[VideoFactoryProject]:
        raise NotImplementedError

    @abstractmethod
    def save(self, project: VideoFactoryProject) -> VideoFactoryProject:
        raise NotImplementedError



    @abstractmethod
    def save_artifact(self, artifact: VideoFactoryArtifact) -> VideoFactoryArtifact:
        raise NotImplementedError

    @abstractmethod
    def get_artifact(self, artifact_id: str, owner_user_id: str) -> VideoFactoryArtifact | None:
        raise NotImplementedError

    @abstractmethod
    def list_artifacts(self, project_id: str, owner_user_id: str, kind: str | None = None) -> list[VideoFactoryArtifact]:
        raise NotImplementedError

    @abstractmethod
    def save_render_batch(self, batch: RenderBatch) -> RenderBatch:
        raise NotImplementedError

    @abstractmethod
    def get_render_batch(self, render_batch_id: str) -> RenderBatch | None:
        raise NotImplementedError

    @abstractmethod
    def list_render_batches(self, project_id: str) -> list[RenderBatch]:
        raise NotImplementedError


@dataclass(frozen=True)
class EndKeyframeInput:
    frame_id: str
    image_path: str
    sha256: str
    package_digest: str
    qc_report_id: str


@dataclass(frozen=True)
class VideoGenerationRequest:
    """Normalized request for video generation."""

    request_id: str
    owner_user_id: str
    project_id: str
    scene_id: str
    prompt: str
    duration_seconds: int
    reference_image_paths: tuple[str, ...] = ()
    aspect_ratio: str = "9:16"
    provider_options: dict[str, Any] = field(default_factory=dict)
    end_keyframe: EndKeyframeInput | None = None
    request_audio: bool = False


@dataclass(frozen=True)
class VideoGenerationResult:
    """Normalized result from video generation."""

    request_id: str
    status: Literal["submitted", "running", "completed", "failed", "unknown"] = "failed"
    provider_operation_id: str | None = None
    video_path: str | None = None
    error_code: str = ""
    error_message: str = ""
    cost_usd: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.status in ("submitted", "running", "completed")


class VideoGenerationPort(Protocol):
    """Specialized video generation capability boundary."""

    provider_kind: str

    def submit(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        """Submit video generation request and return result or operation ID."""
        ...

    def get_status(self, operation_id: str) -> VideoGenerationResult:
        """Check status of async generation operation."""
        ...


def validate_product_video_render_provider(provider: Any) -> VideoGenerationPort:
    """Validate an official Product Video scene-render boundary.

    Product Video never infers identity from a registry key, class name, or
    operation ID. A concrete provider must explicitly declare either the
    Vertex AI API or Google Flow Playwright identity before a durable provider
    pin or external submission is possible.
    """
    if provider is None:
        raise ValueError("render provider is required")

    kind = getattr(provider, "provider_kind", None)
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError("render provider provider_kind must be a non-empty string")
    normalized_kind = kind.strip()
    if normalized_kind=='muse' and getattr(provider,'four_agent_execution',False) is not True:
        raise ValueError('Muse requires an explicit four_agent execution adapter')
    if normalized_kind not in OFFICIAL_PRODUCT_VIDEO_PROVIDER_KINDS:
        allowed = ", ".join(sorted(OFFICIAL_PRODUCT_VIDEO_PROVIDER_KINDS))
        raise ValueError(
            f"render provider provider_kind must be one of: {allowed}"
        )
    for method in ("submit", "get_status"):
        if not callable(getattr(provider, method, None)):
            raise ValueError(f"render provider must implement callable {method}")
    availability = getattr(provider, "is_available", None)
    if availability is not None and not isinstance(availability, bool) and not callable(availability):
        raise ValueError("render provider is_available must be a bool or callable")
    return provider


class VideoGenerationReconciliationPort(Protocol):
    """Optional crash-window reconciliation capability.

    ``lookup_by_request_id`` must return a result whose ``request_id`` is
    exactly the requested client request ID, whose
    ``provider_operation_id`` is the durable external operation identity, and
    whose optional ``metadata['provider_kind']`` is either absent or the same
    stable adapter kind as the durable pin. Providers that cannot guarantee
    those identities must omit this capability; callers then return
    ``RECONCILIATION_REQUIRED`` and never submit a duplicate paid operation.
    """

    def lookup_by_request_id(
        self, request_id: str
    ) -> VideoGenerationResult | None:
        ...


@dataclass(frozen=True)
class KeyframeGenerationRequest:
    """Normalized product-grounded image generation request."""

    operation_id: str
    workflow_id: str
    project_id: str
    frame_id: str
    prompt: str
    product_description: str
    reference_image_paths: tuple[str, ...]
    aspect_ratio: str = "9:16"
    negative_prompt: str = ""
    frame_role: str = "start"
    safety_reason: str = ""
    provider_options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KeyframeGenerationResult:
    operation_id: str
    status: Literal["submitted", "running", "completed", "failed", "cancelled", "canceled", "unknown"]
    provider_operation_id: str | None = None
    image_path: str | None = None
    error_code: str = ""
    error_message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class KeyframeGenerationPort(Protocol):
    def submit(self, request: KeyframeGenerationRequest) -> KeyframeGenerationResult:
        ...

    def get_status(self, operation_id: str) -> KeyframeGenerationResult:
        ...

