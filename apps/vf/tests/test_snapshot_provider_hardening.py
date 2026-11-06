"""Regression coverage for snapshot-only Product Video execution boundaries."""

from __future__ import annotations

import pytest

from hermes_personal.integrations.video_factory import server
from video_factory.integrations.google_flow_provider import (
    GoogleFlowPlaywrightProvider,
)
from video_factory.integrations.ports import (
    EndKeyframeInput,
    VideoGenerationRequest,
    VideoGenerationResult,
    validate_product_video_render_provider,
)
from video_factory.integrations.vertex_provider import (
    VertexVideoProvider,
)
from video_factory.integrations.scene_renderer import (
    SceneRenderOperationStore,
    SceneRenderRuntime,
)
from video_factory.handlers import MCPVideoFactoryAdapter


class _Registry:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def dispatch(self, name: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(name)
        return {"brief_id": "brief-1"}


class _Provider:
    provider_kind = "google_flow"

    def submit(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        return VideoGenerationResult(
            request_id=request.request_id,
            status="submitted",
            provider_operation_id="flow-1",
        )

    def get_status(self, operation_id: str) -> VideoGenerationResult:
        return VideoGenerationResult(
            request_id=operation_id,
            status="running",
            provider_operation_id=operation_id,
        )


def _request() -> VideoGenerationRequest:
    return VideoGenerationRequest(
        request_id="req-1",
        owner_user_id="owner-1",
        project_id="project-1",
        scene_id="scene-1",
        prompt="approved prompt",
        duration_seconds=4,
    )


def test_execution_stage_never_uses_hardcoded_brief_wire_outside_snapshot() -> None:
    registry = _Registry()
    adapter = MCPVideoFactoryAdapter(registry, wire_tools={})

    with pytest.raises(RuntimeError, match="video.brief.create"):
        adapter.create_creative_brief("product-1")

    assert registry.calls == []


@pytest.mark.parametrize("kind", ("google_flow", "vertex_ai"))
def test_render_provider_accepts_only_official_ai_video_identities(kind: str) -> None:
    provider = _Provider()
    provider.provider_kind = kind  # type: ignore[attr-defined]

    assert validate_product_video_render_provider(provider) is provider


@pytest.mark.parametrize("kind", ("", None, "zoom", "slideshow", "local"))
def test_render_provider_rejects_non_official_or_missing_identity(
    kind: str | None,
) -> None:
    provider = _Provider()
    provider.provider_kind = kind  # type: ignore[attr-defined]

    with pytest.raises(ValueError, match="provider_kind"):
        validate_product_video_render_provider(provider)


def test_scene_runtime_uses_explicit_google_flow_provider() -> None:
    store = SceneRenderOperationStore(":memory:")
    provider = _Provider()

    runtime = SceneRenderRuntime(
        store=store,
        provider=provider,
        preferred_provider_kind="google_flow",
    )

    assert runtime.provider_kind == "google_flow"
    assert store.get(_request().request_id) is None


@pytest.mark.parametrize(
    ("configured_kind", "expected_type"),
    (
        ("vertex_ai", VertexVideoProvider),
    ),
)
def test_server_constructs_only_the_configured_official_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    configured_kind: str,
    expected_type: type,
) -> None:
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"video_factory": {"render_provider": configured_kind}},
    )

    provider = server._video_provider(output_dir=tmp_path)

    assert isinstance(provider, expected_type)
    assert provider.provider_kind == configured_kind


def test_configured_flow_without_authenticated_runtime_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"video_factory": {"render_provider": "google_flow"}},
    )

    with pytest.raises(server.RenderProviderConfigurationError) as exc_info:
        server._video_provider(output_dir=tmp_path)

    assert exc_info.value.code == "GOOGLE_FLOW_UNAVAILABLE"


def test_dual_provider_config_keeps_both_edges_and_selects_before_submission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"video_factory": {
            "render_providers": ["google_flow", "vertex_ai"],
            "vertex_ai": {"location": "us-central1", "model": "veo-3.1-generate-001"},
        }},
    )
    providers = server._video_provider(output_dir=tmp_path)
    assert list(providers) == ["google_flow", "vertex_ai"]
    assert isinstance(providers["vertex_ai"], VertexVideoProvider)
    assert providers["vertex_ai"].location == "us-central1"
    monkeypatch.setattr(providers["google_flow"], "is_available", lambda: False)
    monkeypatch.setattr(providers["vertex_ai"], "is_available", lambda: True)
    assert server.VideoProviderSelector(providers).select_initial().kind == "vertex_ai"


def test_vertex_veo3_payload_sets_explicit_audio_choice(tmp_path) -> None:
    provider = VertexVideoProvider(output_dir=tmp_path)

    body = provider._build_submit_body(_request())
    assert body["parameters"]["generateAudio"] is False

    request_with_audio = VideoGenerationRequest(
        request_id="req-audio",
        owner_user_id="owner-1",
        project_id="project-1",
        scene_id="scene-1",
        prompt="approved prompt",
        duration_seconds=4,
        provider_options={"generateAudio": True},
    )
    assert provider._build_submit_body(request_with_audio)["parameters"]["generateAudio"] is True

    approved_narration_request = VideoGenerationRequest(
        request_id="req-approved-narration",
        owner_user_id="owner-1",
        project_id="project-1",
        scene_id="scene-1",
        prompt="approved visual prompt and Vietnamese narration",
        duration_seconds=4,
        request_audio=True,
    )
    assert provider._build_submit_body(approved_narration_request)["parameters"]["generateAudio"] is True


def test_vertex_veo3_payload_preserves_qc_end_keyframe(tmp_path) -> None:
    start = tmp_path / "start.png"
    end = tmp_path / "end.png"
    start.write_bytes(b"start")
    end.write_bytes(b"end")
    provider = VertexVideoProvider(output_dir=tmp_path)
    request = VideoGenerationRequest(
        request_id="req-frames",
        owner_user_id="owner-1",
        project_id="project-1",
        scene_id="scene-1",
        prompt="approved prompt",
        duration_seconds=4,
        reference_image_paths=(str(start),),
        end_keyframe=EndKeyframeInput(
            frame_id="end-1", image_path=str(end), sha256="digest",
            package_digest="package", qc_report_id="qc-1",
        ),
    )

    assert provider.supports_end_keyframe is True
    assert provider._validate_request(request) is None
    body = provider._build_submit_body(request)
    assert body["instances"][0]["image"]["bytesBase64Encoded"] == "c3RhcnQ="
    assert body["instances"][0]["lastFrame"]["bytesBase64Encoded"] == "ZW5k"


def test_flow_without_browser_never_invents_a_paid_operation(tmp_path) -> None:
    provider = GoogleFlowPlaywrightProvider(output_dir=tmp_path)

    assert provider.is_available() is False
    result = provider.submit(_request())
    assert result.status == "failed"
    assert result.provider_operation_id is None
    assert result.metadata["code"] == "GOOGLE_FLOW_UNAVAILABLE"
    assert provider.lookup_by_request_id(_request().request_id) is None
    unknown = provider.get_status("gflow_op_req-1")
    assert unknown.status == "failed"


def test_server_rejects_synthetic_zoom_provider_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"video_factory": {"render_provider": "zoom"}},
    )

    with pytest.raises(server.RenderProviderConfigurationError) as exc_info:
        server._video_provider()

    assert exc_info.value.code == "UNSUPPORTED_RENDER_PROVIDER"
