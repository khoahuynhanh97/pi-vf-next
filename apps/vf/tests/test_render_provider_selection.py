from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3
from pathlib import Path
from threading import Barrier

import pytest

from video_factory.integrations import server
from video_factory.integrations.ports import (
    VideoGenerationRequest,
    VideoGenerationResult,
)
from video_factory.integrations.scene_renderer import (
    SceneRenderOperationStore,
    SceneRenderRuntime,
)
from video_factory.integrations.provider_selection import (
    ProviderSelectionError,
    VideoProviderSelector,
)


class _FakeProvider:
    def __init__(self, kind: str, *, available: bool = True) -> None:
        self.kind = kind
        self.provider_kind = "google_flow" if "flow" in kind else "vertex_ai"
        self.is_available = available
        self.submit_calls: list[str] = []
        self.poll_calls: list[str] = []

    def submit(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        self.submit_calls.append(request.request_id)
        return VideoGenerationResult(
            request_id=request.request_id,
            status="submitted",
            provider_operation_id=f"{self.kind}-operation",
        )

    def get_status(self, operation_id: str) -> VideoGenerationResult:
        self.poll_calls.append(operation_id)
        return VideoGenerationResult(
            request_id=operation_id,
            status="running",
            provider_operation_id=operation_id,
        )


def _request() -> VideoGenerationRequest:
    return VideoGenerationRequest(
        request_id="video-scene-provider-selection",
        owner_user_id="operator-1",
        project_id="project-1",
        scene_id="scene-01",
        prompt="Show the approved product in use",
        duration_seconds=4,
    )


def test_server_selects_explicit_vertex_branch_without_switching_existing_flow() -> None:
    flow = _FakeProvider("google-flow")
    vertex = _FakeProvider("vertex")
    providers = {"google_flow": flow, "vertex_ai": vertex}
    assert server._select_render_provider(providers) is flow
    assert server._select_render_provider(providers, "vertex_ai") is vertex
    assert server._provider_for_operation(
        providers, {"provider_kind": "google_flow", "provider_operation_id": "flow-op"}
    )[0] is flow


def test_explicit_vertex_branch_fails_closed_if_unavailable() -> None:
    providers = {
        "google_flow": _FakeProvider("google-flow"),
        "vertex_ai": _FakeProvider("vertex", available=False),
    }
    with pytest.raises(ProviderSelectionError):
        server._select_render_provider(providers, "vertex_ai")


def _runtime(
    db: sqlite3.Connection,
    google_flow: _FakeProvider,
    vertex: _FakeProvider,
) -> SceneRenderRuntime:
    return SceneRenderRuntime(
        store=SceneRenderOperationStore(db),
        provider={"google_flow": google_flow, "vertex_ai": vertex},
        preferred_provider_kinds=("google_flow", "vertex_ai"),
    )


def test_initial_submission_prefers_available_google_flow_and_persists_pin() -> None:
    google_flow = _FakeProvider("google-flow")
    vertex = _FakeProvider("vertex")
    store = SceneRenderOperationStore(sqlite3.connect(":memory:"))

    result = SceneRenderRuntime(
        store=store,
        provider={"google_flow": google_flow, "vertex_ai": vertex},
        preferred_provider_kinds=("google_flow", "vertex_ai"),
    ).run(_request())

    assert result.status == "submitted"
    assert result.metadata["provider_kind"] == "google_flow"
    assert google_flow.submit_calls == [_request().request_id]
    assert vertex.submit_calls == []
    assert store.get(_request().request_id)["provider_kind"] == "google_flow"


def test_initial_submission_uses_vertex_when_it_is_next_official_provider() -> None:
    google_flow = _FakeProvider("google-flow", available=False)
    vertex = _FakeProvider("vertex")
    store = SceneRenderOperationStore(sqlite3.connect(":memory:"))

    result = _runtime(store.connection, google_flow, vertex).run(_request())

    assert result.status == "submitted"
    assert result.metadata["provider_kind"] == "vertex_ai"
    assert google_flow.submit_calls == []
    assert vertex.submit_calls == [_request().request_id]
    assert store.get(_request().request_id)["provider_kind"] == "vertex_ai"


def test_initial_submission_uses_vertex_when_flow_credits_are_insufficient() -> None:
    class CreditLimitedFlow(_FakeProvider):
        def can_submit(self, request: VideoGenerationRequest) -> bool:
            return False

    flow = CreditLimitedFlow("google-flow")
    vertex = _FakeProvider("vertex")
    store = SceneRenderOperationStore(sqlite3.connect(":memory:"))

    result = _runtime(store.connection, flow, vertex).run(_request())

    assert result.status == "submitted"
    assert store.get(_request().request_id)["provider_kind"] == "vertex_ai"
    assert flow.submit_calls == []
    assert vertex.submit_calls == [_request().request_id]


def test_resume_uses_durable_provider_pin_after_process_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "scene-operations.sqlite"
    db = sqlite3.connect(db_path)
    first_google = _FakeProvider("google-flow")
    first_vertex = _FakeProvider("vertex")
    first = _runtime(db, first_google, first_vertex)
    request = _request()

    submitted = first.run(request)
    assert submitted.provider_operation_id == "google-flow-operation"
    db.close()

    second_google = _FakeProvider("google-flow")
    second_vertex = _FakeProvider("vertex")
    resumed_db = sqlite3.connect(db_path)
    resumed = _runtime(resumed_db, second_google, second_vertex).run(request)

    assert resumed.status == "running"
    assert second_google.poll_calls == ["google-flow-operation"]
    assert second_vertex.poll_calls == []
    assert second_google.submit_calls == []
    assert second_vertex.submit_calls == []


def test_missing_pin_refuses_ambiguous_resume_without_polling_or_submit() -> None:
    request = _request()
    db = sqlite3.connect(":memory:")
    store = SceneRenderOperationStore(db)
    store.mark_submitting(request)
    google_flow = _FakeProvider("google-flow")
    vertex = _FakeProvider("vertex")

    result = _runtime(db, google_flow, vertex).run(request)

    assert result.status == "failed"
    assert result.error_code == "RECONCILIATION_REQUIRED"
    assert "no durable provider pin" in result.error_message
    assert google_flow.submit_calls == []
    assert google_flow.poll_calls == []
    assert vertex.submit_calls == []
    assert vertex.poll_calls == []


def test_unavailable_pinned_provider_does_not_fallback_on_resume() -> None:
    request = _request()
    db = sqlite3.connect(":memory:")
    store = SceneRenderOperationStore(db)
    store.mark_planned(request)
    store.bind_provider_kind(request.request_id, "google_flow")
    store.mark_submitting(request)
    google_flow = _FakeProvider("google-flow", available=False)
    vertex = _FakeProvider("vertex")

    result = _runtime(db, google_flow, vertex).run(request)

    assert result.status == "failed"
    assert result.error_code == "RECONCILIATION_REQUIRED"
    assert "provider is unavailable: google_flow" in result.error_message
    assert vertex.poll_calls == []
    assert vertex.submit_calls == []


def test_provider_selection_does_not_fallback_after_a_durable_submission_id() -> None:
    request = _request()
    db = sqlite3.connect(":memory:")
    store = SceneRenderOperationStore(db)
    store.mark_planned(request)
    store.bind_provider_kind(request.request_id, "google_flow")
    store.mark_submitting(request)
    store.save_result(
        request,
        VideoGenerationResult(
            request_id=request.request_id,
            status="submitted",
            provider_operation_id="google-flow-operation",
        ),
    )
    google_flow = _FakeProvider("google-flow", available=False)
    vertex = _FakeProvider("vertex")

    result = _runtime(db, google_flow, vertex).run(request)

    assert result.status == "failed"
    assert result.error_code == "RECONCILIATION_REQUIRED"
    assert google_flow.poll_calls == []
    assert vertex.poll_calls == []


def test_provider_kind_is_immutable() -> None:
    request = _request()
    store = SceneRenderOperationStore(sqlite3.connect(":memory:"))
    store.mark_planned(request)
    store.bind_provider_kind(request.request_id, "google_flow")

    with pytest.raises(ValueError, match="provider_kind mismatch"):
        store.bind_provider_kind(request.request_id, "vertex_ai")


@pytest.mark.parametrize("kind", ("zoom", "slideshow", "local", "ffmpeg"))
def test_durable_store_rejects_non_official_provider_kind(kind: str) -> None:
    request = _request()
    store = SceneRenderOperationStore(sqlite3.connect(":memory:"))
    store.mark_planned(request)

    with pytest.raises(ValueError, match="unsupported Product Video provider_kind"):
        store.claim_provider_for_submission(request, kind)
    with pytest.raises(ValueError, match="unsupported Product Video provider_kind"):
        store.bind_provider_kind(request.request_id, kind)

    assert store.get(request.request_id)["provider_kind"] == ""
    assert store.get(request.request_id)["status"] == "planned"


def test_bind_provider_kind_does_not_report_success_when_cas_loses() -> None:
    request = _request()
    store = SceneRenderOperationStore(sqlite3.connect(":memory:"))
    store.mark_planned(request)
    store.connection.execute(
        "UPDATE video_factory_scene_operation SET status = 'completed' WHERE operation_id = ?",
        (request.request_id,),
    )
    store.connection.commit()

    with pytest.raises(ValueError, match="cannot bind provider_kind"):
        store.bind_provider_kind(request.request_id, "google_flow")


def _claim_from_connection(
    db_path: Path,
    provider_kind: str,
    request: VideoGenerationRequest,
    provider: _FakeProvider | None = None,
    barrier: Barrier | None = None,
):
    connection = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    try:
        store = SceneRenderOperationStore(connection)
        if barrier is not None:
            barrier.wait(timeout=10)
        result = store.claim_provider_for_submission(request, provider_kind)
        if result.can_submit and provider is not None:
            provider.submit(request)
        return result
    finally:
        connection.close()


def test_two_connections_atomic_claim_same_provider_allows_one_submit(tmp_path: Path) -> None:
    db_path = tmp_path / "claim.sqlite"
    request = _request()
    seed = SceneRenderOperationStore(sqlite3.connect(db_path))
    seed.mark_planned(request)
    seed.connection.close()
    provider = _FakeProvider("google-flow")
    barrier = Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: _claim_from_connection(
                    db_path, "google_flow", request, provider, barrier
                ),
                range(2),
            )
        )

    assert sum(result.status == "claimed" for result in results) == 1
    assert sum(result.status == "already_claimed_same_provider" for result in results) == 1
    row = SceneRenderOperationStore(sqlite3.connect(db_path)).get(request.request_id)
    assert row["provider_kind"] == "google_flow"
    assert row["status"] == "submitting"
    assert row["error_code"] == "SUBMISSION_NOT_RECONCILED"
    assert provider.submit_calls == [request.request_id]


def test_two_connections_different_providers_only_winner_can_submit(tmp_path: Path) -> None:
    db_path = tmp_path / "claim-different.sqlite"
    request = _request()
    seed = SceneRenderOperationStore(sqlite3.connect(db_path))
    seed.mark_planned(request)
    seed.connection.close()
    google_flow = _FakeProvider("google-flow")
    vertex = _FakeProvider("vertex")
    barrier = Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda kind: _claim_from_connection(
                    db_path,
                    kind,
                    request,
                    google_flow if kind == "google_flow" else vertex,
                    barrier,
                ),
                ("google_flow", "vertex_ai"),
            )
        )

    assert sum(result.status == "claimed" for result in results) == 1
    assert sum(result.status == "provider_conflict" for result in results) == 1
    row = SceneRenderOperationStore(sqlite3.connect(db_path)).get(request.request_id)
    assert row["provider_kind"] in {"google_flow", "vertex_ai"}
    assert row["status"] == "submitting"
    assert len(google_flow.submit_calls) + len(vertex.submit_calls) == 1
    if row["provider_kind"] == "google_flow":
        assert google_flow.submit_calls == [request.request_id]
        assert vertex.submit_calls == []
    else:
        assert vertex.submit_calls == [request.request_id]
        assert google_flow.submit_calls == []


def test_pinned_planned_operation_only_matching_provider_can_claim() -> None:
    request = _request()
    store = SceneRenderOperationStore(sqlite3.connect(":memory:"))
    store.mark_planned(request)
    store.bind_provider_kind(request.request_id, "google_flow")

    same = store.claim_provider_for_submission(request, "google_flow")
    other = store.claim_provider_for_submission(request, "vertex_ai")

    assert same.status == "claimed"
    assert other.status == "provider_conflict"


@pytest.mark.parametrize(
    ("status", "expected_claim_status"),
    [
        ("submitting", "already_claimed_same_provider"),
        ("running", "invalid_state"),
        ("completed", "invalid_state"),
    ],
)
def test_nonplanned_operation_cannot_be_claimed_again(
    status: str, expected_claim_status: str
) -> None:
    request = _request()
    store = SceneRenderOperationStore(sqlite3.connect(":memory:"))
    store.mark_planned(request)
    store.bind_provider_kind(request.request_id, "google_flow")
    if status == "submitting":
        store.claim_provider_for_submission(request, "google_flow")
    else:
        store.connection.execute(
            "UPDATE video_factory_scene_operation SET status = ? WHERE operation_id = ?",
            (status, request.request_id),
        )
        store.connection.commit()

    result = store.claim_provider_for_submission(request, "google_flow")

    assert result.status == expected_claim_status


def test_poll_single_provider_must_match_durable_pin() -> None:
    google_flow = _FakeProvider("google-flow")
    vertex = _FakeProvider("vertex")

    selected, error = server._provider_for_operation(
        vertex,
        {"provider_kind": "google_flow", "provider_operation_id": "flow-op"},
        provider_kind="vertex_ai",
    )

    assert selected is None
    assert "does not match" in (error or "")
    assert vertex.poll_calls == []
    assert google_flow.poll_calls == []


def test_pinned_provider_with_registry_change_fails_closed() -> None:
    google_flow = _FakeProvider("google-flow")
    vertex = _FakeProvider("vertex")

    selected, error = server._provider_for_operation(
        {"vertex_ai": vertex},
        {"provider_kind": "google_flow", "provider_operation_id": "flow-op"},
    )

    assert selected is None
    assert "google_flow" in (error or "")
    assert vertex.poll_calls == []


class _ReconcilingProvider(_FakeProvider):
    def __init__(self, *, result: VideoGenerationResult | None) -> None:
        super().__init__("google-flow")
        self.reconciliation_result = result
        self.lookup_calls: list[str] = []

    def lookup_by_request_id(self, request_id: str) -> VideoGenerationResult | None:
        self.lookup_calls.append(request_id)
        return self.reconciliation_result


def test_submitting_without_operation_id_reconciles_without_submit() -> None:
    request = _request()
    db = sqlite3.connect(":memory:")
    store = SceneRenderOperationStore(db)
    store.mark_planned(request)
    store.claim_provider_for_submission(request, "google_flow")
    provider = _ReconcilingProvider(
        result=VideoGenerationResult(
            request_id=request.request_id,
            status="running",
            provider_operation_id="flow-reconciled-op",
        )
    )

    result = SceneRenderRuntime(
        store=store,
        provider={"google_flow": provider, "vertex_ai": _FakeProvider("vertex")},
    ).run(request)

    assert result.status == "running"
    assert result.provider_operation_id == "flow-reconciled-op"
    assert provider.lookup_calls == [request.request_id]
    assert provider.submit_calls == []
    assert store.get(request.request_id)["provider_operation_id"] == "flow-reconciled-op"


def test_submitting_without_operation_id_without_reconcile_is_not_running() -> None:
    request = _request()
    db = sqlite3.connect(":memory:")
    store = SceneRenderOperationStore(db)
    store.mark_planned(request)
    store.claim_provider_for_submission(request, "google_flow")
    provider = _FakeProvider("google-flow")

    result = SceneRenderRuntime(store=store, provider={"google_flow": provider}).run(
        request
    )

    assert result.status == "failed"
    assert result.error_code == "RECONCILIATION_REQUIRED"
    assert provider.submit_calls == []


def test_submitting_reconcile_none_is_not_running_or_fallback() -> None:
    request = _request()
    db = sqlite3.connect(":memory:")
    store = SceneRenderOperationStore(db)
    store.mark_planned(request)
    store.claim_provider_for_submission(request, "google_flow")
    provider = _ReconcilingProvider(result=None)
    vertex = _FakeProvider("vertex")

    result = SceneRenderRuntime(
        store=store,
        provider={"google_flow": provider, "vertex_ai": vertex},
    ).run(request)

    assert result.status == "failed"
    assert result.error_code == "RECONCILIATION_REQUIRED"
    assert provider.submit_calls == []
    assert vertex.submit_calls == []


def test_get_job_status_reconciles_submitting_checkpoint_without_running_forever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "server-reconcile.sqlite"
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_path))
    from video_factory.integrations.domain import RenderBatch
    from video_factory.integrations.repository import (
        SQLiteVideoFactoryRepository,
    )
    from video_factory.integrations.service import VideoFactoryService

    repository = SQLiteVideoFactoryRepository(db_path)
    repository.save_render_batch(
        RenderBatch(
            render_batch_id="batch_reconcile",
            owner_user_id="operator-1",
            workflow_id="workflow-1",
            project_id="project-1",
            version=1,
            status="running",
            scene_count=1,
            completed_scene_count=0,
            created_at="2026-09-20T00:00:00Z",
            updated_at="2026-09-20T00:00:00Z",
        )
    )
    monkeypatch.setattr(server, "_custom_service", VideoFactoryService(repository))
    request = _request()
    store = SceneRenderOperationStore(sqlite3.connect(db_path))
    store.mark_planned(request, render_batch_id="batch_reconcile", scene_order=1)
    store.claim_provider_for_submission(request, "google_flow")
    provider = _ReconcilingProvider(
        result=VideoGenerationResult(
            request_id=request.request_id,
            status="running",
            provider_operation_id="flow-server-reconciled",
        )
    )

    result = server.get_job_status(
        "batch_reconcile",
        provider={"google_flow": provider, "vertex_ai": _FakeProvider("vertex")},
    )

    assert result["ok"] is True
    assert result["status"] == "running"
    assert provider.submit_calls == []
    assert provider.lookup_calls == [request.request_id]
    assert SceneRenderOperationStore(sqlite3.connect(db_path)).get(
        request.request_id
    )["provider_operation_id"] == "flow-server-reconciled"


def test_get_job_status_never_returns_running_for_unreconciled_submitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "server-reconcile-required.sqlite"
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_path))
    from video_factory.integrations.domain import RenderBatch
    from video_factory.integrations.repository import (
        SQLiteVideoFactoryRepository,
    )
    from video_factory.integrations.service import VideoFactoryService

    repository = SQLiteVideoFactoryRepository(db_path)
    repository.save_render_batch(
        RenderBatch(
            render_batch_id="batch_reconcile_required",
            owner_user_id="operator-1",
            workflow_id="workflow-1",
            project_id="project-1",
            version=1,
            status="running",
            scene_count=1,
            completed_scene_count=0,
            created_at="2026-09-20T00:00:00Z",
            updated_at="2026-09-20T00:00:00Z",
        )
    )
    monkeypatch.setattr(server, "_custom_service", VideoFactoryService(repository))
    request = _request()
    store = SceneRenderOperationStore(sqlite3.connect(db_path))
    store.mark_planned(
        request, render_batch_id="batch_reconcile_required", scene_order=1
    )
    store.claim_provider_for_submission(request, "google_flow")
    provider = _FakeProvider("google-flow")

    result = server.get_job_status(
        "batch_reconcile_required", provider={"google_flow": provider}
    )

    assert result["ok"] is False
    assert result["status"] == "reconciliation_required"
    assert result["code"] == "RECONCILIATION_REQUIRED"
    assert provider.submit_calls == []
    assert SceneRenderOperationStore(sqlite3.connect(db_path)).get(
        request.request_id
    )["status"] == "submitting"


def test_batch_status_without_provider_uses_durable_flow_pin_after_restart(tmp_path, monkeypatch):
    import hashlib
    import json
    from dataclasses import replace
    from types import SimpleNamespace

    from video_factory.integrations.domain import RenderBatch
    from video_factory.integrations.repository import SQLiteVideoFactoryRepository
    from video_factory.integrations.service import VideoFactoryService

    job_id = "batch_pinned_flow_without_caller_provider"
    db_path = tmp_path / "pinned-flow.sqlite"
    monkeypatch.setenv("VIDEO_FACTORY_HOME", str(tmp_path / "vf-home"))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_path))
    repository = SQLiteVideoFactoryRepository(db_path)
    repository.save_render_batch(RenderBatch(
        render_batch_id=job_id,
        owner_user_id="operator-1",
        workflow_id="workflow-pinned-flow",
        project_id="project-1",
        status="running",
        scene_count=3,
        created_at="2026-10-25T00:00:00Z",
        updated_at="2026-10-25T00:00:00Z",
    ))
    monkeypatch.setattr(server, "_custom_service", VideoFactoryService(repository))
    workspace = tmp_path / "workspace"
    context_dir = workspace / "creative_batches"
    context_dir.mkdir(parents=True)
    monkeypatch.setattr(server, "_workspace_path", lambda: workspace)
    package = {
        "creative_mode": "four_agent",
        "backend_profile": {"provider_kind": "google_flow"},
    }
    options = {"creative_mode": "four_agent", "creative_package": package}
    first = replace(_request(), provider_options=options)
    second = replace(first, request_id="video-scene-next", scene_id="scene-02")
    third = replace(first, request_id="video-scene-last", scene_id="scene-03")
    context_dir.joinpath(f"{job_id}.json").write_text(json.dumps({
        "workflow_id": "workflow-pinned-flow",
        "project_id": "project-1",
        "creative_mode": "four_agent",
        "backend_profile": {"provider_kind": "google_flow"},
        "_batch_operation_ids": [first.request_id, second.request_id, third.request_id],
    }), encoding="utf-8")
    output_path = tmp_path / "scene-01.mp4"
    output_path.write_bytes(b"mock clip")
    output_sha = hashlib.sha256(output_path.read_bytes()).hexdigest()
    connection = sqlite3.connect(db_path)
    store = SceneRenderOperationStore(connection)
    store.mark_planned(first, render_batch_id=job_id, scene_order=1)
    store.mark_planned(second, render_batch_id=job_id, scene_order=2)
    store.mark_planned(third, render_batch_id=job_id, scene_order=3)
    connection.execute(
        "UPDATE video_factory_scene_operation SET status='completed',provider_kind='google_flow',"
        "provider_operation_id='existing-flow-operation',output_path=?,output_sha256=? WHERE operation_id=?",
        (str(output_path), output_sha, first.request_id),
    )
    vertex_operation_id = (
        "projects/project-pinned/locations/us-central1/publishers/google/models/veo/operations/existing-scene-2"
    )
    connection.execute(
        "UPDATE video_factory_scene_operation SET status='submitted',provider_kind='vertex_ai',"
        "provider_operation_id=? WHERE operation_id=?",
        (vertex_operation_id, second.request_id),
    )
    connection.commit()
    connection.close()

    flow = _FakeProvider("google-flow")

    class VertexCompletesOnSecondPoll(_FakeProvider):
        def __init__(self, output_path: Path) -> None:
            super().__init__("vertex")
            self.output_path = output_path

        def get_status(self, operation_id: str) -> VideoGenerationResult:
            self.poll_calls.append(operation_id)
            if len(self.poll_calls) == 1:
                return VideoGenerationResult(
                    request_id=second.request_id, status="running", provider_operation_id=operation_id
                )
            return VideoGenerationResult(
                request_id=second.request_id, status="completed",
                provider_operation_id=operation_id, video_path=str(self.output_path),
            )

    second_output = tmp_path / "scene-02.mp4"
    second_output.write_bytes(b"mock second clip")
    vertex = VertexCompletesOnSecondPoll(second_output)
    # The global registry order deliberately prefers Vertex; this API call
    # models a restarted status caller that omits the original provider arg.
    monkeypatch.setattr(server, "_video_provider", lambda **_: {
        "vertex_ai": vertex,
        "google_flow": flow,
    })
    monkeypatch.setattr(server, "validate_scene_output", lambda **_: {"ok": True})
    monkeypatch.setattr(
        "video_factory.integrations.scene_renderer.validate_scene_output",
        lambda **_: SimpleNamespace(sha256=hashlib.sha256(second_output.read_bytes()).hexdigest()),
    )

    conflict = server.get_job_status(job_id, provider_kind="vertex_ai")
    assert conflict["code"] == "PROVIDER_BINDING_MISMATCH"
    assert vertex.submit_calls == []
    assert flow.submit_calls == []

    first_result = server.get_job_status(job_id)
    first_store = SceneRenderOperationStore(sqlite3.connect(db_path))
    second_row = first_store.get(second.request_id)
    assert first_result["status"] == "running"
    assert second_row["provider_kind"] == "vertex_ai"
    assert vertex.poll_calls == [vertex_operation_id]
    assert flow.submit_calls == []
    assert vertex.submit_calls == []
    assert second_row["status"] == "submitted"
    assert first_store.get(third.request_id)["status"] == "planned"
    first_store.connection.close()

    # On restart, reconcile Scene 2 by its own durable Vertex pin. Once terminal,
    # the remaining planned scene still uses the batch's Flow pin.
    resumed = server.get_job_status(job_id)
    final_store = SceneRenderOperationStore(sqlite3.connect(db_path))
    assert resumed["status"] == "running", resumed
    assert vertex.poll_calls == [vertex_operation_id, vertex_operation_id]
    assert flow.submit_calls == [third.request_id]
    assert vertex.submit_calls == []
    assert final_store.get(second.request_id)["provider_kind"] == "vertex_ai"
    assert final_store.get(third.request_id)["provider_kind"] == "google_flow"
    final_store.connection.close()

    resumed_again = server.get_job_status(job_id)
    assert resumed_again["status"] == "running"
    assert vertex.poll_calls == [vertex_operation_id, vertex_operation_id]
    assert flow.submit_calls == [third.request_id]
    assert flow.poll_calls == ["google-flow-operation"]


def test_four_agent_batch_without_durable_provider_pin_fails_before_send(tmp_path, monkeypatch):
    import json
    from dataclasses import replace

    from video_factory.integrations.domain import RenderBatch
    from video_factory.integrations.repository import SQLiteVideoFactoryRepository
    from video_factory.integrations.service import VideoFactoryService

    job_id = "batch_missing_provider_pin"
    db_path = tmp_path / "missing-provider-pin.sqlite"
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_path))
    repository = SQLiteVideoFactoryRepository(db_path)
    repository.save_render_batch(RenderBatch(
        render_batch_id=job_id,
        owner_user_id="operator-1",
        workflow_id="workflow-missing-pin",
        project_id="project-1",
        status="running",
        scene_count=1,
        created_at="2026-10-25T00:00:00Z",
        updated_at="2026-10-25T00:00:00Z",
    ))
    monkeypatch.setattr(server, "_custom_service", VideoFactoryService(repository))
    workspace = tmp_path / "workspace"
    context_dir = workspace / "creative_batches"
    context_dir.mkdir(parents=True)
    monkeypatch.setattr(server, "_workspace_path", lambda: workspace)
    request = replace(_request(), provider_options={"creative_mode": "four_agent"})
    context_dir.joinpath(f"{job_id}.json").write_text(json.dumps({
        "workflow_id": "workflow-missing-pin",
        "project_id": "project-1",
        "creative_mode": "four_agent",
        "backend_profile": {},
        "_batch_operation_ids": [request.request_id],
    }), encoding="utf-8")
    store = SceneRenderOperationStore(sqlite3.connect(db_path))
    store.mark_planned(request, render_batch_id=job_id, scene_order=1)
    store.connection.close()
    vertex = _FakeProvider("vertex")
    flow = _FakeProvider("google-flow")
    monkeypatch.setattr(server, "_video_provider", lambda **_: {
        "vertex_ai": vertex,
        "google_flow": flow,
    })

    result = server.get_job_status(job_id)

    assert result["status"] == "failed"
    assert result["code"] == "PROVIDER_BINDING_MISSING"
    assert vertex.submit_calls == []
    assert flow.submit_calls == []
    final_store = SceneRenderOperationStore(sqlite3.connect(db_path))
    assert final_store.get(request.request_id)["status"] == "planned"
    final_store.connection.close()


def test_metadata_provider_kind_cannot_override_durable_pin() -> None:
    request = _request()
    store = SceneRenderOperationStore(sqlite3.connect(":memory:"))
    provider = _FakeProvider("google-flow")
    store.mark_planned(request)

    result = SceneRenderRuntime(
        store=store,
        provider={"google_flow": provider},
    ).run(request)

    assert result.metadata["provider_kind"] == "google_flow"


def test_pre_submit_fallback_policy_can_be_disabled() -> None:
    google_flow = _FakeProvider("google-flow", available=False)
    vertex = _FakeProvider("vertex")
    selector = VideoProviderSelector(
        {"google_flow": google_flow, "vertex_ai": vertex},
        preferred_provider_kind="google_flow",
        fallback_provider_kind="vertex_ai",
        allow_pre_submit_fallback=False,
    )

    with pytest.raises(ProviderSelectionError, match="no configured"):
        selector.select_initial()
    assert google_flow.submit_calls == []
    assert vertex.submit_calls == []


def test_explicit_vertex_selection_never_uses_available_flow() -> None:
    vertex = _FakeProvider("vertex", available=False)
    flow = _FakeProvider("google-flow")
    request = _request()
    store = SceneRenderOperationStore(sqlite3.connect(":memory:"))

    result = SceneRenderRuntime(
        store=store,
        provider={"google_flow": flow, "vertex_ai": vertex},
        preferred_provider_kind="vertex_ai",
        allow_pre_submit_fallback=False,
    ).run(request)

    assert result.status == "failed"
    assert result.error_code == "PROVIDER_SELECTION_FAILED"
    assert flow.submit_calls == []
    assert vertex.submit_calls == []


def test_pre_submit_fallback_policy_only_falls_back_before_claim() -> None:
    google_flow = _FakeProvider("google-flow", available=False)
    vertex = _FakeProvider("vertex")
    selector = VideoProviderSelector(
        {"google_flow": google_flow, "vertex_ai": vertex},
        preferred_provider_kind="google_flow",
        fallback_provider_kind="vertex_ai",
        allow_pre_submit_fallback=True,
    )

    selected = selector.select_initial()

    assert selected.kind == "vertex_ai"


class _WrongMetadataProvider(_FakeProvider):
    def submit(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        result = super().submit(request)
        return VideoGenerationResult(
            request_id=result.request_id,
            status=result.status,
            provider_operation_id=result.provider_operation_id,
            metadata={"provider_kind": "vertex_ai"},
        )


def test_provider_result_metadata_cannot_override_durable_selected_kind() -> None:
    request = _request()
    db = sqlite3.connect(":memory:")
    store = SceneRenderOperationStore(db)
    provider = _WrongMetadataProvider("google-flow")

    with pytest.raises(ProviderSelectionError, match="does not match"):
        SceneRenderRuntime(store=store, provider={"google_flow": provider}).run(
            request
        )

    row = store.get(request.request_id)
    assert row["provider_kind"] == "google_flow"
    assert row["status"] == "submitting"
    assert row["provider_operation_id"] is None


def test_runtime_explicit_policy_disabled_is_not_overridden_by_mapping_defaults() -> None:
    request = _request()
    store = SceneRenderOperationStore(sqlite3.connect(":memory:"))
    google_flow = _FakeProvider("google-flow", available=False)
    vertex = _FakeProvider("vertex")

    runtime = SceneRenderRuntime(
        store=store,
        provider={"google_flow": google_flow, "vertex_ai": vertex},
        preferred_provider_kind="google_flow",
        fallback_provider_kind="vertex_ai",
        allow_pre_submit_fallback=False,
    )
    result = runtime.run(request)

    assert result.status == "failed"
    assert result.error_code == "PROVIDER_SELECTION_FAILED"
    assert store.get(request.request_id)["provider_kind"] == ""
    assert google_flow.submit_calls == []
    assert vertex.submit_calls == []


def test_runtime_falls_back_only_to_the_configured_official_provider_before_claim() -> None:
    request = _request()
    store = SceneRenderOperationStore(sqlite3.connect(":memory:"))
    google_flow = _FakeProvider("google-flow", available=False)
    vertex = _FakeProvider("vertex")

    result = SceneRenderRuntime(
        store=store,
        provider={"google_flow": google_flow, "vertex_ai": vertex},
        preferred_provider_kind="google_flow",
        fallback_provider_kind="vertex_ai",
        allow_pre_submit_fallback=True,
    ).run(request)

    assert result.status == "submitted"
    assert result.metadata["provider_kind"] == "vertex_ai"
    assert store.get(request.request_id)["provider_kind"] == "vertex_ai"
    assert google_flow.submit_calls == []
    assert vertex.submit_calls == [request.request_id]


def test_runtime_explicit_policy_prefers_available_google_flow() -> None:
    request = _request()
    store = SceneRenderOperationStore(sqlite3.connect(":memory:"))
    google_flow = _FakeProvider("google-flow")
    vertex = _FakeProvider("vertex")

    result = SceneRenderRuntime(
        store=store,
        provider={"google_flow": google_flow, "vertex_ai": vertex},
        preferred_provider_kind="google_flow",
        fallback_provider_kind="vertex_ai",
        allow_pre_submit_fallback=True,
    ).run(request)

    assert result.status == "submitted"
    assert store.get(request.request_id)["provider_kind"] == "google_flow"
    assert google_flow.submit_calls == [request.request_id]
    assert vertex.submit_calls == []


def test_runtime_reconciliation_rejects_result_for_another_request() -> None:
    request = _request()
    store = SceneRenderOperationStore(sqlite3.connect(":memory:"))
    store.mark_planned(request)
    store.claim_provider_for_submission(request, "google_flow")
    provider = _ReconcilingProvider(
        result=VideoGenerationResult(
            request_id="another-request",
            status="running",
            provider_operation_id="wrong-operation",
        )
    )
    vertex = _FakeProvider("vertex")

    result = SceneRenderRuntime(
        store=store,
        provider={"google_flow": provider, "vertex_ai": vertex},
    ).run(request)

    row = store.get(request.request_id)
    assert result.error_code == "RECONCILIATION_REQUIRED"
    assert row["status"] == "submitting"
    assert row["provider_operation_id"] is None
    assert provider.submit_calls == []
    assert provider.poll_calls == []
    assert vertex.submit_calls == []


def test_runtime_reconciliation_rejects_result_with_wrong_provider_metadata() -> None:
    request = _request()
    store = SceneRenderOperationStore(sqlite3.connect(":memory:"))
    store.mark_planned(request)
    store.claim_provider_for_submission(request, "google_flow")

    class WrongProviderMetadata(_ReconcilingProvider):
        def lookup_by_request_id(self, request_id: str) -> VideoGenerationResult:
            return VideoGenerationResult(
                request_id=request_id,
                status="running",
                provider_operation_id="flow-operation",
                metadata={"provider_kind": "vertex_ai"},
            )

    provider = WrongProviderMetadata(result=None)
    result = SceneRenderRuntime(
        store=store,
        provider={"google_flow": provider, "vertex_ai": _FakeProvider("vertex")},
    ).run(request)

    row = store.get(request.request_id)
    assert result.error_code == "RECONCILIATION_REQUIRED"
    assert row["status"] == "submitting"
    assert row["provider_operation_id"] is None


def _seed_server_reconciliation_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    job_id: str,
    result: VideoGenerationResult | None,
):
    from video_factory.integrations.domain import RenderBatch
    from video_factory.integrations.repository import (
        SQLiteVideoFactoryRepository,
    )
    from video_factory.integrations.service import VideoFactoryService

    db_path = tmp_path / f"{job_id}.sqlite"
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_path))
    repository = SQLiteVideoFactoryRepository(db_path)
    repository.save_render_batch(
        RenderBatch(
            render_batch_id=job_id,
            owner_user_id="operator-1",
            workflow_id="workflow-1",
            project_id="project-1",
            version=1,
            status="running",
            scene_count=1,
            completed_scene_count=0,
            created_at="2026-09-20T00:00:00Z",
            updated_at="2026-09-20T00:00:00Z",
        )
    )
    monkeypatch.setattr(server, "_custom_service", VideoFactoryService(repository))
    request = _request()
    store = SceneRenderOperationStore(sqlite3.connect(db_path))
    store.mark_planned(request, render_batch_id=job_id, scene_order=1)
    store.claim_provider_for_submission(request, "google_flow")
    provider = _ReconcilingProvider(result=result)
    return db_path, request, provider


def test_server_reconciliation_rejects_result_for_another_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = "batch_server_cross_request"
    db_path, request, provider = _seed_server_reconciliation_case(
        tmp_path,
        monkeypatch,
        job_id=job_id,
        result=VideoGenerationResult(
            request_id="another-request",
            status="running",
            provider_operation_id="wrong-operation",
        ),
    )

    result = server.get_job_status(job_id, provider={"google_flow": provider})

    row = SceneRenderOperationStore(sqlite3.connect(db_path)).get(request.request_id)
    assert result["code"] == "RECONCILIATION_REQUIRED"
    assert row["status"] == "submitting"
    assert row["provider_operation_id"] is None
    assert provider.submit_calls == []
    assert provider.poll_calls == []


def test_server_reconciliation_rejects_wrong_provider_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = "batch_server_cross_provider"
    db_path, request, _ = _seed_server_reconciliation_case(
        tmp_path,
        monkeypatch,
        job_id=job_id,
        result=VideoGenerationResult(
            request_id="unused",
            status="running",
            provider_operation_id="wrong-provider-operation",
            metadata={"provider_kind": "vertex_ai"},
        ),
    )

    # Use the durable request ID while still declaring the wrong provider kind.
    provider = _ReconcilingProvider(
        result=VideoGenerationResult(
            request_id="video-scene-provider-selection",
            status="running",
            provider_operation_id="wrong-provider-operation",
            metadata={"provider_kind": "vertex_ai"},
        )
    )
    result = server.get_job_status(job_id, provider={"google_flow": provider})

    row = SceneRenderOperationStore(sqlite3.connect(db_path)).get(request.request_id)
    assert result["code"] == "RECONCILIATION_REQUIRED"
    assert row["status"] == "submitting"
    assert row["provider_operation_id"] is None


def test_server_poll_session_loss_preserves_submitted_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from video_factory.integrations.capacity import ProviderCapacityStore

    job_id = "batch_server_session_loss"
    db_path, request, _ = _seed_server_reconciliation_case(
        tmp_path, monkeypatch, job_id=job_id, result=None,
    )
    store = SceneRenderOperationStore(sqlite3.connect(db_path))
    store.save_result(request, VideoGenerationResult(
        request_id=request.request_id, status="submitted",
        provider_operation_id="accepted-flow-operation",
    ))
    store.connection.close()

    class SessionLostProvider(_FakeProvider):
        def get_status(self, operation_id: str) -> VideoGenerationResult:
            assert operation_id == "accepted-flow-operation"
            return VideoGenerationResult(
                request_id=request.request_id, status="failed",
                provider_operation_id=operation_id,
                error_code="RECONCILIATION_REQUIRED",
                error_message="Flow session expired",
            )

    result = server.get_job_status(
        job_id, provider={"google_flow": SessionLostProvider("google-flow")}
    )
    connection = sqlite3.connect(db_path)
    row = SceneRenderOperationStore(connection).get(request.request_id)
    capacity = ProviderCapacityStore(connection)
    assert result["code"] == "RECONCILIATION_REQUIRED"
    assert row["status"] == "submitted"
    assert row["provider_operation_id"] == "accepted-flow-operation"
    assert len(capacity.active("google_flow")) == 1
    connection.close()


def _bind_operation_id_from_connection(
    db_path: Path, operation_id: str, provider_operation_id: str, barrier: Barrier
):
    connection = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    try:
        store = SceneRenderOperationStore(connection)
        barrier.wait(timeout=10)
        return store.bind_provider_operation_id(operation_id, provider_operation_id)
    finally:
        connection.close()


def test_concurrent_reconciliation_same_operation_id_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "reconcile-same.sqlite"
    request = _request()
    store = SceneRenderOperationStore(sqlite3.connect(db_path))
    store.mark_planned(request)
    store.claim_provider_for_submission(request, "google_flow")
    store.connection.close()
    barrier = Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: _bind_operation_id_from_connection(
                    db_path, request.request_id, "flow-operation", barrier
                ),
                range(2),
            )
        )

    assert all(result["provider_operation_id"] == "flow-operation" for result in results)
    assert SceneRenderOperationStore(sqlite3.connect(db_path)).get(
        request.request_id
    )["provider_operation_id"] == "flow-operation"


def test_concurrent_reconciliation_different_operation_ids_never_overwrite(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "reconcile-different.sqlite"
    request = _request()
    store = SceneRenderOperationStore(sqlite3.connect(db_path))
    store.mark_planned(request)
    store.claim_provider_for_submission(request, "google_flow")
    store.connection.close()
    barrier = Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _bind_operation_id_from_connection,
                db_path,
                request.request_id,
                operation_id,
                barrier,
            )
            for operation_id in ("flow-operation-a", "flow-operation-b")
        ]
        outcomes = [future.exception() or future.result() for future in futures]

    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, ValueError) for outcome in outcomes) == 1
    row = SceneRenderOperationStore(sqlite3.connect(db_path)).get(request.request_id)
    assert row["provider_operation_id"] in {"flow-operation-a", "flow-operation-b"}
