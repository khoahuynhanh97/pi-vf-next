from video_factory.integrations.ports import VideoGenerationRequest
from video_factory.integrations.vertex_provider import VertexVideoProvider
from video_factory.integrations.provider_selection import VideoProviderSelector
from video_factory.handlers.common import _paid_render_send_count
import sqlite3


def test_vertex_post_timeout_is_unknown_and_pinned_for_reconciliation(tmp_path):
    request = VideoGenerationRequest(
        request_id="scene-uncertain",
        owner_user_id="owner",
        project_id="project",
        scene_id="scene-1",
        prompt="approved scene",
        duration_seconds=4,
        aspect_ratio="9:16",
    )

    def timeout(*_args, **_kwargs):
        raise TimeoutError("timed out after POST")

    provider = VertexVideoProvider(
        project_id="project",
        location="us-central1",
        post_fn=timeout,
        credential_resolver=lambda: ("token", "project"),
        output_dir=tmp_path,
    )

    result = provider.submit(request)

    assert result.status == "unknown"
    assert result.error_code == "NETWORK_UNKNOWN_OUTCOME"
    assert result.metadata["reconcile_required"] is True


def test_vertex_poll_timeout_remains_unknown(tmp_path):
    def timeout(*_args, **_kwargs):
        raise TimeoutError("poll timed out")

    provider = VertexVideoProvider(
        project_id="project",
        location="us-central1",
        post_fn=timeout,
        credential_resolver=lambda: ("token", "project"),
        output_dir=tmp_path,
    )

    result = provider.get_status("operations/scene-uncertain")

    assert result.status == "unknown"
    assert result.error_code == "NETWORK_UNKNOWN_OUTCOME"


class _StubProvider:
    def __init__(self, kind, available=True):
        self.provider_kind = kind
        self._available = available

    def is_available(self):
        return self._available

    def submit(self, request):
        raise AssertionError("selection test must not submit")

    def get_status(self, operation_id):
        raise AssertionError("selection test must not poll")


def test_render_selection_is_vertex_first_and_falls_back_only_before_submit():
    selector = VideoProviderSelector(
        {
            "vertex_ai": _StubProvider("vertex_ai"),
            "google_flow": _StubProvider("google_flow"),
        },
        preferred_kinds=("vertex_ai", "google_flow"),
    )
    assert selector.select_initial().kind == "vertex_ai"

    unavailable_vertex = VideoProviderSelector(
        {
            "vertex_ai": _StubProvider("vertex_ai", available=False),
            "google_flow": _StubProvider("google_flow"),
        },
        preferred_kinds=("vertex_ai", "google_flow"),
    )
    assert unavailable_vertex.select_initial().kind == "google_flow"


def test_paid_render_send_count_includes_reserved_unknowns_and_is_workflow_scoped():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE budget_reservation (scope_id TEXT, capability_id TEXT, state TEXT)"
    )
    connection.executemany(
        "INSERT INTO budget_reservation VALUES (?, ?, ?)",
        [("wf-a", "product_video.render", "committed") for _ in range(4)]
        + [("wf-a", "product_video.render", "reserved")]
        + [("wf-a", "product_video.render", "released")]
        + [("wf-b", "product_video.render", "committed")],
    )
    assert _paid_render_send_count(connection, "wf-a") == 5
    assert _paid_render_send_count(connection, "wf-b") == 1
