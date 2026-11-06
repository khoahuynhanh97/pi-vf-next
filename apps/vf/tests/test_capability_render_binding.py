"""Configured Product Video render must use one active capability binding."""

from __future__ import annotations

from dataclasses import replace

from hermes_personal.capabilities.bindings import ImplementationBinding
from hermes_personal.capabilities.snapshots import CatalogSnapshot
from video_factory.handlers import ProductVideoJobHandlers
from video_factory.integrations.ports import (
    VideoGenerationRequest,
    VideoGenerationResult,
)
from video_factory.integrations.scene_renderer import (
    SceneRenderOperationStore,
    SceneRenderRuntime,
)


_CAPABILITIES = {
    "product.research": "mcp__product_intelligence__research_product",
    "product.resource_pack.build": "mcp__product_intelligence__build_resource_pack",
    "product.resource_pack.lock": "mcp__product_intelligence__lock_resource_pack",
    "video.brief.create": "mcp__video_factory__create_brief",
    "video.scene_plan.create": "mcp__video_factory__create_scene_plan",
    "video.storyboard.create": "mcp__video_factory__create_storyboard",
    "video.render": "mcp__video_factory__render",
    "video.export": "mcp__video_factory__export",
}


def _snapshot(render_wire: str = "mcp__video_factory__render") -> CatalogSnapshot:
    bindings = tuple(
        ImplementationBinding(
            capability_id=capability_id,
            wire_name=render_wire if capability_id == "video.render" else wire_name,
            source="hermes_mcp",
            provider="video_factory" if capability_id.startswith("video.") else "product_intelligence",
            implementation_version="1",
            input_schema_version="1.0",
            output_schema_version="1.0",
            schema_digest="sha256:" + "a" * 64,
            health="healthy",
        )
        for capability_id, wire_name in _CAPABILITIES.items()
    )
    return CatalogSnapshot(
        snapshot_id="snap-test",
        digest="sha256:" + "b" * 64,
        created_at="2026-09-20T00:00:00+00:00",
        bindings=bindings,
    )


class _Registry:
    def __init__(self) -> None:
        self.called: list[str] = []

    def dispatch(self, tool_name: str, payload: dict[str, object]) -> dict[str, object]:
        self.called.append(tool_name)
        return {"status": "failed"}


def test_configured_render_uses_active_snapshot_binding_not_registered_alias() -> None:
    registry = _Registry()
    handlers = ProductVideoJobHandlers(
        provider_mode="configured",
        tool_registry=registry,
        capability_snapshot=_snapshot(),
    )

    assert handlers._missing_live_provider is False
    assert handlers.video_client.wire_tools["render"] == "mcp__video_factory__render"
    assert "video_generate" not in handlers.video_client.wire_tools.values()


def test_generic_video_generate_cannot_be_the_product_video_render_binding() -> None:
    handlers = ProductVideoJobHandlers(
        provider_mode="configured",
        tool_registry=_Registry(),
        capability_snapshot=_snapshot("video_generate"),
    )

    assert handlers._missing_live_provider is False
    assert "render" not in handlers.video_client.wire_tools


def test_ambiguous_render_bindings_fail_closed() -> None:
    snapshot = _snapshot()
    duplicate_render = replace(
        next(binding for binding in snapshot.bindings if binding.capability_id == "video.render"),
        wire_name="video_generate",
    )
    ambiguous_snapshot = replace(snapshot, bindings=snapshot.bindings + (duplicate_render,))
    handlers = ProductVideoJobHandlers(
        provider_mode="configured",
        tool_registry=_Registry(),
        capability_snapshot=ambiguous_snapshot,
    )

    assert handlers._missing_live_provider is False
    assert "render" not in handlers.video_client.wire_tools


def test_missing_render_binding_fails_closed_without_provider_call() -> None:
    snapshot = _snapshot()
    no_render_snapshot = replace(
        snapshot,
        bindings=tuple(
            binding for binding in snapshot.bindings if binding.capability_id != "video.render"
        ),
    )
    registry = _Registry()
    handlers = ProductVideoJobHandlers(
        provider_mode="configured",
        tool_registry=registry,
        capability_snapshot=no_render_snapshot,
    )

    assert handlers._missing_live_provider is False
    assert "render" not in handlers.video_client.wire_tools
    assert registry.called == []


class _UnavailableFlow:
    provider_kind = "google_flow"
    is_available = False

    def submit(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        raise AssertionError("unavailable Flow provider must not submit")

    def get_status(self, operation_id: str) -> VideoGenerationResult:
        raise AssertionError("unavailable Flow provider must not poll")


def _request() -> VideoGenerationRequest:
    return VideoGenerationRequest(
        request_id="flow-request-1",
        owner_user_id="owner",
        project_id="project",
        scene_id="scene-1",
        duration_seconds=4,
        aspect_ratio="9:16",
        prompt="approved prompt",
        reference_image_paths=(),
    )


def test_google_flow_unavailable_never_falls_back_or_submits() -> None:
    store = SceneRenderOperationStore(":memory:")
    result = SceneRenderRuntime(store=store, provider=_UnavailableFlow()).run(_request())

    assert result.status == "failed"
    assert result.error_code == "PROVIDER_SELECTION_FAILED"
    assert "no configured video provider" in (result.error_message or "")
    assert store.get(_request().request_id)["provider_kind"] == ""


def test_vertex_pinned_operation_requires_vertex_provider_and_is_not_polled_by_flow() -> None:
    store = SceneRenderOperationStore(":memory:")
    store.mark_planned(_request())
    store.claim_provider_for_submission(_request(), "vertex_ai")

    result = SceneRenderRuntime(store=store, provider=_UnavailableFlow()).run(_request())

    assert result.status == "failed"
    assert result.error_code == "RECONCILIATION_REQUIRED"
    assert "pinned to unavailable provider: vertex_ai" in (result.error_message or "")
