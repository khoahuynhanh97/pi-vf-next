"""Adversarial tests for production integrity gaps.

Tests the five hardened integrity contracts:
1. Scene/composite digest validation (no backfilling, scoped updates, invalidation)
2. Provider output validation before terminal completion
3. Exact QC provenance (no fallback queries without artifact_id/version)
4. Fail-closed path handling (no unrestricted fallbacks)
5. End-keyframe contract enforcement
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import os
from fractions import Fraction
from pathlib import Path
from typing import Any

import av
import pytest

from video_factory.integrations.scene_validator import (
    validate_scene_output,
)
from video_factory.integrations.scene_renderer import (
    SceneRenderOperationStore,
)
from video_factory.qc.models import QCReport, QCStage, QCVerdict
from video_factory.qc.repository import QCReportRepository
from video_factory.qc.service import ProductVideoQCService
from video_factory.storage.layout import ProductVideoWorkspaceLayout
from video_factory.workflows.repository import WorkflowRepository
from video_factory.integrations.repository import (
    SQLiteVideoFactoryRepository,
)
from video_factory.integrations.domain import RenderBatch, VideoFactoryArtifact


def _write_valid_video(
    path: Path,
    width: int = 64,
    height: int = 96,
    duration_frames: int = 3,
    fps: int = 24,
) -> Path:
    """Write a minimal valid MP4 for testing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    container = av.open(str(path), mode="w", format="mp4")
    stream = container.add_stream("libx264", rate=fps)
    stream.width = width
    stream.height = height
    stream.pix_fmt = "yuv420p"
    for index in range(duration_frames):
        frame = av.VideoFrame(width, height, "yuv420p")
        for plane in frame.planes:
            plane.update(bytes([32 + index]) * plane.buffer_size)
        frame.pts = index
        frame.time_base = Fraction(1, fps)
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    return path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _save_active_composite(
    repository: SQLiteVideoFactoryRepository,
    *,
    workspace: Path,
    video_path: Path,
    owner_user_id: str,
    project_id: str,
    workflow_id: str,
    artifact_id: str,
    version: int = 1,
) -> str:
    digest = _sha256(video_path)
    repository.save_artifact(
        VideoFactoryArtifact(
            artifact_id=artifact_id,
            owner_user_id=owner_user_id,
            workflow_id=workflow_id,
            project_id=project_id,
            kind="composite",
            relative_path=video_path.relative_to(workspace).as_posix(),
            sha256=digest,
            version=version,
            size=video_path.stat().st_size,
            mime_type="video/mp4",
            status="active",
        )
    )
    return digest


def _save_running_batch(
    db_path: Path,
    *,
    batch_id: str,
    project_id: str,
    owner_user_id: str = "owner",
) -> SQLiteVideoFactoryRepository:
    repository = SQLiteVideoFactoryRepository(db_path)
    repository.save_render_batch(
        RenderBatch(
            render_batch_id=batch_id,
            owner_user_id=owner_user_id,
            workflow_id=f"wf-{project_id}",
            project_id=project_id,
            status="running",
            scene_count=1,
        )
    )
    return repository


# ══════════════════════════════════════════════════════════════════════════════
# 1. Scene/Composite Integrity (No backfilling, scoped updates, invalidation)
# ══════════════════════════════════════════════════════════════════════════════


def test_qc_service_rejects_missing_keyframe_digest_without_backfilling(
    tmp_path: Path,
) -> None:
    """QC must reject missing package_digest instead of computing from disk."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row

    # Initialize schema
    db.executescript("""
        CREATE TABLE IF NOT EXISTS workflow (
            workflow_id TEXT PRIMARY KEY,
            aggregate_json TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS workflow_event (
            event_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            command_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    """)

    wf_repo = WorkflowRepository(db)

    # Create workflow with keyframe package missing digest
    wf_id = "wf-missing-digest"
    package = {
        "generated_keyframes": [
            {
                "frame_id": "frame-1",
                "image_path": "file://fake.png",
                "status": "completed",
            }
        ],
        # package_digest intentionally missing
    }
    agg = {
        "owner_user_id": "user-1",
        "project_id": "proj-1",
        "storyboard": package,
        "resource_pack": {
            "product_identity_description": "Test Product",
            "product_identity": {
                "brand": "TestBrand",
                "model": "TestModel",
            },
        },
        "source_context": {},
    }
    wf_repo.commit_workflow(wf_id, json.dumps(agg), [])

    svc = ProductVideoQCService(db)
    result = svc.run(wf_id, QCStage.KEYFRAME, provider_mode="fake")

    assert result["status"] == "failed"
    assert "package_digest" in result["reason"].lower()
    assert "missing" in result["reason"].lower()


def test_export_rejects_qc_report_missing_artifact_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Export must fail when QC report lacks exact artifact_id/version provenance."""
    from video_factory.integrations import server

    ws = tmp_path / "workspace"
    db_path = tmp_path / "factory.sqlite"

    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_path))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_WORKSPACE", str(ws))

    # Create project with final approval
    server.video_project_create("owner", "proj-no-provenance")
    server.resource_pack_save(
        "owner",
        "proj-no-provenance",
        {
            "id": "pack-1",
            "product_references": [
                {"asset_id": "asset-1", "uri": "asset://product.png"}
            ],
            "primary_product_asset_id": "asset-1",
            "product_identity_description": "Test Product",
        },
    )

    svc = server._service()
    project = svc.get_project("owner", "proj-no-provenance")
    from video_factory.integrations.domain import (
        FinalApprovalStatus,
        VideoFactoryProject,
    )

    svc.repository.save(
        VideoFactoryProject(**{
            **project.__dict__,
            "final_approval": FinalApprovalStatus.APPROVED,
        })
    )

    # Write valid video and save QC report WITHOUT artifact_id
    video = _write_valid_video(
        ws / "projects" / "proj-no-provenance" / "renders" / "v1" / "composite" / "draft.mp4",
        width=720,
        height=1280,
        duration_frames=72,
    )
    artifact_id = "art_composite_proj-no-provenance_v1"
    video_sha = _save_active_composite(
        svc.repository,
        workspace=ws,
        video_path=video,
        owner_user_id="owner",
        project_id="proj-no-provenance",
        workflow_id="wf-1",
        artifact_id=artifact_id,
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    QCReportRepository(conn).save_report(
        QCReport(
            report_id="qc-no-artifact",
            workflow_id="wf-1",
            stage=QCStage.VIDEO,
            artifact_id="",  # Missing
            artifact_version=0,  # Missing
            artifact_digest=f"sha256:{video_sha}",
            reference_digests=(),
            evaluator_name="eval",
            evaluator_version="1.0",
            rubric_version="1.0",
            created_at="2026-09-18T00:00:00Z",
            score=95,
            verdict=QCVerdict.PASS,
            findings=(),
            owner_user_id="owner",
            project_id="proj-no-provenance",
        )
    )
    conn.close()

    result = server.export(
        render_result={
            "video_url": f"file://{video}",
            "owner_user_id": "owner",
            "project_id": "proj-no-provenance",
            "workflow_id": "wf-1",
            "artifact_id": artifact_id,
            "artifact_version": 1,
            "sha256": video_sha,
        },
        project_id="proj-no-provenance",
        owner_user_id="owner",
    )

    assert result["ok"] is False
    assert result["code"] == "VIDEO_QC_REQUIRED", result
    # Export correctly rejected - QC report exists but lacks artifact_id binding


# ══════════════════════════════════════════════════════════════════════════════
# 2. Provider Output Validation
# ══════════════════════════════════════════════════════════════════════════════


def test_validate_scene_output_rejects_wrong_duration(tmp_path: Path) -> None:
    """Scene validator must reject output with wrong duration."""
    # Write 1-second video
    video = _write_valid_video(tmp_path / "short.mp4", duration_frames=24, fps=24)

    with pytest.raises(ValueError, match="SCENE_OUTPUT_DURATION_MISMATCH"):
        validate_scene_output(
            output_path=video,
            expected_duration=4.0,  # Expected 4 seconds
            duration_tolerance=0.5,
        )


def test_validate_scene_output_rejects_wrong_aspect_ratio(tmp_path: Path) -> None:
    """Scene validator must reject output with wrong aspect ratio."""
    # Write 16:9 video
    video = _write_valid_video(
        tmp_path / "wrong-aspect.mp4", width=160, height=90, duration_frames=96
    )

    with pytest.raises(ValueError, match="SCENE_OUTPUT_ASPECT_MISMATCH"):
        validate_scene_output(
            output_path=video,
            expected_duration=4.0,
            expected_aspect_ratio="9:16",  # Expected portrait
        )


def test_validate_scene_output_rejects_undecodable(tmp_path: Path) -> None:
    """Scene validator must reject broken MP4."""
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(
        b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * 4096
    )

    with pytest.raises(ValueError, match="SCENE_OUTPUT|Invalid data"):
        validate_scene_output(output_path=broken, expected_duration=4.0)


def test_scene_render_store_validates_before_completed(tmp_path: Path) -> None:
    """SceneRenderOperationStore must validate output before marking completed."""
    from video_factory.integrations.ports import (
        VideoGenerationRequest,
        VideoGenerationResult,
    )

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    store = SceneRenderOperationStore(db)

    # Write 1-second video (wrong duration for a 4-second request)
    video = _write_valid_video(
        tmp_path / "wrong-duration.mp4", duration_frames=24, fps=24
    )

    request = VideoGenerationRequest(
        request_id="op-1",
        owner_user_id="owner",
        project_id="proj",
        scene_id="scene-1",
        duration_seconds=4,  # Expected 4 seconds
        aspect_ratio="9:16",
        prompt="test",
        reference_image_paths=(str(tmp_path / "ref.png"),),
    )

    # Set validation contract
    store.mark_planned(request)

    # Provider claims completed
    result = VideoGenerationResult(
        request_id="op-1",
        status="completed",
        provider_operation_id="prov-1",
        video_path=str(video),
    )

    # Save should validate and transition to failed
    store.save_result(request, result)

    saved = store.get("op-1")
    assert saved["status"] == "failed"
    assert saved["error_code"] == "VALIDATION_FAILED"
    assert "duration" in saved["error_message"].lower()


@pytest.mark.parametrize(
    "case", ["missing_path", "missing_file", "empty_file", "undecodable"]
)
def test_finalize_provider_result_never_trusts_invalid_completed_output(
    tmp_path: Path,
    case: str,
) -> None:
    """Every invalid provider completion must durably fail without a trusted digest."""
    from video_factory.integrations.ports import (
        VideoGenerationRequest,
        VideoGenerationResult,
    )

    store = SceneRenderOperationStore(tmp_path / f"{case}.sqlite")
    request = VideoGenerationRequest(
        request_id=f"op-{case}",
        owner_user_id="owner",
        project_id="proj",
        scene_id="scene-1",
        duration_seconds=4,
        aspect_ratio="9:16",
        prompt="test",
        reference_image_paths=(),
    )
    store.mark_planned(request)

    output_path: str | None
    if case == "missing_path":
        output_path = None
    elif case == "missing_file":
        output_path = str(tmp_path / "does-not-exist.mp4")
    elif case == "empty_file":
        empty = tmp_path / "empty.mp4"
        empty.touch()
        output_path = str(empty)
    else:
        broken = tmp_path / "broken.mp4"
        broken.write_bytes(b"not-a-decodable-video")
        output_path = str(broken)

    state = store.finalize_provider_result(
        request.request_id,
        VideoGenerationResult(
            request_id=request.request_id,
            status="completed",
            provider_operation_id=f"provider-{case}",
            video_path=output_path,
        ),
    )

    assert state["status"] == "failed"
    assert state["error_code"] == "VALIDATION_FAILED"
    assert state["output_sha256"] == ""


def test_polling_validates_wrong_duration_via_finalize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_job_status() should validate scene output through finalize_provider_result()."""
    import sqlite3
    from video_factory.integrations import server
    from video_factory.integrations.ports import VideoGenerationResult
    from video_factory.integrations.scene_renderer import (
        SceneRenderOperationStore,
    )

    ws = tmp_path / "workspace"
    db_path = tmp_path / "factory.sqlite"

    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_path))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_WORKSPACE", str(ws))

    # Create a project
    server.video_project_create("owner", "proj-poll")

    # Write a 1-second video (wrong duration for 4-second contract)
    video = _write_valid_video(tmp_path / "short.mp4", duration_frames=24, fps=24)

    # Create a scene operation with validation contract
    op_store = SceneRenderOperationStore(db_path)
    from video_factory.integrations.ports import VideoGenerationRequest

    request = VideoGenerationRequest(
        request_id="op-validate-1",
        owner_user_id="owner",
        project_id="proj-poll",
        scene_id="scene-1",
        duration_seconds=4,  # Expect 4 seconds
        aspect_ratio="9:16",
        prompt="test scene",
        reference_image_paths=(),
    )

    op_store.mark_planned(request, render_batch_id="batch-validate-1", scene_order=1)
    op_store.mark_submitting(request)

    # Call finalize_provider_result directly to test validation
    result = VideoGenerationResult(
        request_id="op-validate-1",
        status="completed",
        provider_operation_id="prov-validate-1",
        video_path=str(video),
    )

    final_state = op_store.finalize_provider_result("op-validate-1", result)

    # Should fail validation due to wrong duration
    assert final_state["status"] == "failed"
    assert "duration" in final_state[
        "error_message"
    ].lower() or "VALIDATION" in final_state.get("error_code", "")


def test_polling_validates_wrong_aspect_ratio_via_finalize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """finalize_provider_result() should reject wrong aspect ratio."""
    from video_factory.integrations.scene_renderer import (
        SceneRenderOperationStore,
    )
    from video_factory.integrations.ports import (
        VideoGenerationRequest,
        VideoGenerationResult,
    )

    db_path = tmp_path / "ops.db"

    # Write a 16:9 video (wrong aspect for 9:16 contract)
    video = _write_valid_video(
        tmp_path / "landscape.mp4", width=160, height=90, duration_frames=96, fps=24
    )

    # Create scene operation with 9:16 contract
    op_store = SceneRenderOperationStore(db_path)
    request = VideoGenerationRequest(
        request_id="op-aspect-1",
        owner_user_id="owner",
        project_id="proj",
        scene_id="scene-1",
        duration_seconds=4,
        aspect_ratio="9:16",  # Expect 9:16
        prompt="test",
        reference_image_paths=(),
    )

    op_store.mark_planned(request, render_batch_id="batch-1", scene_order=1)

    # Try to finalize with wrong aspect ratio video
    result = VideoGenerationResult(
        request_id="op-aspect-1",
        status="completed",
        provider_operation_id="prov-1",
        video_path=str(video),
    )

    final_state = op_store.finalize_provider_result("op-aspect-1", result)

    # Should fail validation due to wrong aspect ratio
    assert final_state["status"] == "failed"
    assert "aspect" in final_state["error_message"].lower()


def test_production_polling_rejects_wrong_aspect_ratio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production polling must reject wrong aspect ratio and mark batch failed."""
    import sqlite3
    from video_factory.integrations import server
    from video_factory.integrations.ports import (
        VideoGenerationRequest,
        VideoGenerationResult,
    )
    from video_factory.integrations.scene_renderer import (
        SceneRenderOperationStore,
    )

    ws = tmp_path / "workspace"
    db_path = tmp_path / "factory.sqlite"
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_path))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_WORKSPACE", str(ws))

    server.video_project_create("owner", "proj-aspect")
    repository = _save_running_batch(
        db_path,
        batch_id="batch_aspect",
        project_id="proj-aspect",
    )

    # Insert scene operation with 9:16 contract
    op_store = SceneRenderOperationStore(db_path)
    request = VideoGenerationRequest(
        request_id="op-aspect-fail",
        owner_user_id="owner",
        project_id="proj-aspect",
        scene_id="scene-1",
        duration_seconds=4,
        aspect_ratio="9:16",
        prompt="test",
        reference_image_paths=(),
    )
    op_store.mark_planned(request, render_batch_id="batch_aspect", scene_order=1)
    op_store.claim_provider_for_submission(request, "google_flow")

    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE video_factory_scene_operation SET provider_operation_id = 'prov-aspect' WHERE operation_id = 'op-aspect-fail'"
    )
    conn.commit()
    conn.close()

    # Write 16:9 video
    video = _write_valid_video(
        tmp_path / "landscape.mp4", width=160, height=90, duration_frames=96
    )

    # Mock provider
    class MockProvider:
        provider_kind = "google_flow"
        is_available = True

        def __init__(self) -> None:
            self.submit_calls = 0
            self.status_calls = 0

        def submit(self, *_args: Any, **_kwargs: Any) -> None:
            self.submit_calls += 1

        def get_status(self, job_id: str) -> VideoGenerationResult:
            self.status_calls += 1
            return VideoGenerationResult(
                request_id="op-aspect-fail",
                status="completed",
                provider_operation_id="prov-aspect",
                video_path=str(video),
            )

    # Call get_job_status
    provider = MockProvider()
    result = server.get_job_status(
        job_id="batch_aspect", provider=provider, provider_kind="google_flow"
    )
    replay_result = server.get_job_status(
        job_id="batch_aspect", provider=provider, provider_kind="google_flow"
    )

    # Assertions
    assert result["status"] == "failed"
    assert replay_result["status"] == "failed"

    # Verify DB state
    op_row = op_store.get("op-aspect-fail")
    batch_row = repository.get_render_batch("batch_aspect")
    composite_path = (
        ProductVideoWorkspaceLayout(ws).get_composite_dir("proj-aspect", 1)
        / "draft.mp4"
    )

    assert op_row["status"] == "failed"
    assert batch_row is not None
    assert batch_row.status == "failed"
    assert not composite_path.exists()
    assert provider.submit_calls == 0
    assert provider.status_calls == 1


def test_production_polling_rejects_empty_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Already completed row with empty digest must fail closed."""
    import sqlite3
    from video_factory.integrations import server

    ws = tmp_path / "workspace"
    db_path = tmp_path / "factory.sqlite"
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_path))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_WORKSPACE", str(ws))

    server.video_project_create("owner", "proj-empty-digest")
    repository = _save_running_batch(
        db_path,
        batch_id="batch_empty",
        project_id="proj-empty-digest",
    )

    # Insert completed scene but with EMPTY digest - MUST FAIL CLOSED
    video = _write_valid_video(tmp_path / "scene.mp4")
    # Initialize store to ensure columns exist
    SceneRenderOperationStore(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO video_factory_scene_operation (
            operation_id, owner_user_id, project_id, scene_id, request_digest,
            status, render_batch_id, scene_order, output_path, output_sha256,
            expected_duration_seconds, expected_aspect_ratio
        ) VALUES ('op-empty', 'owner', 'proj-empty-digest', 'scene-1', 'digest', 'completed', 'batch_empty', 1, ?, '', 4.0, '9:16')
    """,
        (str(video),),
    )
    conn.commit()
    conn.close()

    # Call get_job_status - should detect missing digest and fail
    result = server.get_job_status(job_id="batch_empty")

    assert result["status"] == "failed"
    assert result["code"] == "SCENE_DIGEST_MISSING"
    durable_operation = SceneRenderOperationStore(db_path).get("op-empty")
    durable_batch = repository.get_render_batch("batch_empty")
    assert durable_operation is not None
    assert durable_operation["status"] == "failed"
    assert durable_operation["output_sha256"] == ""
    assert durable_batch is not None
    assert durable_batch.status == "failed"


# ══════════════════════════════════════════════════════════════════════════════
# 3. Path Handling (Fail-closed, no unrestricted fallback)
# ══════════════════════════════════════════════════════════════════════════════


def test_workspace_resolve_path_rejects_outside_workspace(tmp_path: Path) -> None:
    """resolve_path must reject paths outside workspace without fallback."""
    ws = ProductVideoWorkspaceLayout(tmp_path / "workspace")
    outside = tmp_path / "outside" / "file.txt"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("external")

    with pytest.raises(ValueError, match="CONTAINMENT_VIOLATION"):
        ws.resolve_path(outside)


def test_workspace_resolve_path_rejects_traversal(tmp_path: Path) -> None:
    """resolve_path must reject .. traversal."""
    ws = ProductVideoWorkspaceLayout(tmp_path / "workspace")

    with pytest.raises(ValueError, match="traversal"):
        ws.resolve_path("../../../etc/passwd")


def test_qc_service_file_path_resolution_fail_closed(tmp_path: Path) -> None:
    """QC service must not fall back to unrestricted Path.resolve()."""
    from video_factory.qc.service import _file_url_to_path

    # Set workspace
    import os

    os.environ["HERMES_VIDEO_FACTORY_WORKSPACE"] = str(tmp_path / "workspace")

    outside = tmp_path / "outside" / "file.txt"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("external")

    with pytest.raises(ValueError, match="CONTAINMENT_VIOLATION"):
        _file_url_to_path(str(outside))


# ══════════════════════════════════════════════════════════════════════════════
# 4. Composite Integrity (Digest mismatch must invalidate approvals)
# ══════════════════════════════════════════════════════════════════════════════


def test_modified_composite_after_approval_detected(tmp_path: Path) -> None:
    """Modified composite after Final Approval must be detected by SHA mismatch."""
    video = _write_valid_video(
        tmp_path / "original.mp4", width=720, height=1280, duration_frames=96
    )
    original_sha = _sha256(video)

    # Simulate approval bound to original SHA
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    QCReportRepository(db).save_report(
        QCReport(
            report_id="qc-approved",
            workflow_id="wf-1",
            stage=QCStage.VIDEO,
            artifact_id="art_composite_proj_v1",
            artifact_version=1,
            artifact_digest=f"sha256:{original_sha}",
            reference_digests=(),
            evaluator_name="eval",
            evaluator_version="1.0",
            rubric_version="1.0",
            created_at="2026-09-18T00:00:00Z",
            score=95,
            verdict=QCVerdict.PASS,
            findings=(),
            owner_user_id="owner",
            project_id="proj",
        )
    )

    # Modify the video (simulate post-approval change)
    video.write_bytes(video.read_bytes() + b"MODIFIED")
    modified_sha = _sha256(video)

    # Verify SHA changed
    assert modified_sha != original_sha

    # QC lookup should fail because SHA no longer matches
    repo = QCReportRepository(db)
    report = repo.get_verified_pass_report(
        owner_user_id="owner",
        project_id="proj",
        workflow_id="wf-1",
        artifact_id="art_composite_proj_v1",
        artifact_version=1,
        stage=QCStage.VIDEO,
        expected_sha256=modified_sha,
    )

    assert report is None, "Modified composite should not match approved QC report"


# ══════════════════════════════════════════════════════════════════════════════
# 5. Integration: Full pipeline integrity
# ══════════════════════════════════════════════════════════════════════════════


def test_end_to_end_integrity_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full pipeline must reject invalid outputs at every gate."""
    from video_factory.integrations import server

    ws = tmp_path / "workspace"
    db_path = tmp_path / "factory.sqlite"

    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_path))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_WORKSPACE", str(ws))

    # Create approved project
    server.video_project_create("owner", "proj-e2e")
    server.resource_pack_save(
        "owner",
        "proj-e2e",
        {
            "id": "pack-e2e",
            "product_references": [
                {"asset_id": "asset-1", "uri": "asset://product.png"}
            ],
            "primary_product_asset_id": "asset-1",
            "product_identity_description": "Test Product E2E",
        },
    )

    svc = server._service()
    project = svc.get_project("owner", "proj-e2e")
    from video_factory.integrations.domain import (
        FinalApprovalStatus,
        VideoFactoryProject,
    )

    svc.repository.save(
        VideoFactoryProject(**{
            **project.__dict__,
            "final_approval": FinalApprovalStatus.APPROVED,
        })
    )

    # Create INVALID video (wrong dimensions, undecodable tail)
    broken = ws / "projects" / "proj-e2e" / "renders" / "v1" / "composite" / "draft.mp4"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(
        b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * 2048
    )
    artifact_id = "art_composite_proj-e2e_v1"
    broken_sha = _save_active_composite(
        svc.repository,
        workspace=ws,
        video_path=broken,
        owner_user_id="owner",
        project_id="proj-e2e",
        workflow_id="wf-e2e",
        artifact_id=artifact_id,
    )

    # Export must reject invalid video
    result = server.export(
        render_result={
            "video_url": f"file://{broken}",
            "owner_user_id": "owner",
            "project_id": "proj-e2e",
            "workflow_id": "wf-e2e",
            "artifact_id": artifact_id,
            "artifact_version": 1,
            "sha256": broken_sha,
        },
        project_id="proj-e2e",
        owner_user_id="owner",
    )

    assert result["ok"] is False
    assert result["code"] == "INVALID_VIDEO_ARTIFACT"
    assert "decode" in result["error"].lower()


def test_scene_validation_contract_persisted(tmp_path: Path) -> None:
    """Scene operation must persist expected_duration and aspect_ratio for post-restart validation."""
    from video_factory.integrations.scene_renderer import (
        SceneRenderOperationStore,
    )
    from video_factory.integrations.ports import VideoGenerationRequest

    db_path = tmp_path / "ops.db"
    store = SceneRenderOperationStore(db_path)

    request = VideoGenerationRequest(
        request_id="op-contract-1",
        owner_user_id="owner",
        project_id="proj",
        scene_id="scene-1",
        duration_seconds=6,
        aspect_ratio="9:16",
        prompt="test scene",
        reference_image_paths=(),
    )

    store.mark_planned(request, render_batch_id="batch-1", scene_order=1)

    # Reopen connection to verify persistence
    store2 = SceneRenderOperationStore(db_path)
    row = store2.get("op-contract-1")

    assert row is not None
    assert row["expected_duration_seconds"] == 6.0
    assert row["expected_aspect_ratio"] == "9:16"


def test_scene_legacy_operation_missing_contract_fails_validation(
    tmp_path: Path,
) -> None:
    """Legacy operation without validation contract must fail closed."""
    from video_factory.integrations.scene_renderer import (
        SceneRenderOperationStore,
    )
    from video_factory.integrations.ports import VideoGenerationResult

    db_path = tmp_path / "ops.db"
    store = SceneRenderOperationStore(db_path)

    # Insert legacy row manually without validation fields
    store.connection.execute(
        """
        INSERT INTO video_factory_scene_operation (
            operation_id, owner_user_id, project_id, scene_id,
            request_digest, status, expected_duration_seconds, expected_aspect_ratio
        ) VALUES (?, ?, ?, ?, ?, 'running', 0, '')
        """,
        ("op-legacy", "owner", "proj", "scene-1", "digest-123"),
    )

    # Write a valid video
    video = _write_valid_video(tmp_path / "scene.mp4", duration_frames=96, fps=24)

    result = VideoGenerationResult(
        request_id="op-legacy",
        status="completed",
        provider_operation_id="prov-1",
        video_path=str(video),
    )

    # Finalization should detect missing contract
    final_state = store.finalize_provider_result("op-legacy", result)

    assert final_state["status"] == "failed"
    assert (
        "VALIDATION_CONTRACT_MISSING" in final_state["error_code"]
        or "contract" in final_state["error_message"].lower()
    )
    assert final_state["output_sha256"] == ""


def test_get_job_status_missing_scene_digest_fails_closed_and_isolates_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Completed scene with missing digest must fail closed without modifying other projects."""
    from video_factory.integrations import server
    from video_factory.integrations.scene_renderer import (
        SceneRenderOperationStore,
    )

    ws = tmp_path / "workspace"
    db_path = tmp_path / "factory.sqlite"
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_path))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_WORKSPACE", str(ws))

    server.video_project_create("owner_a", "proj_a")
    server.video_project_create("owner_b", "proj_b")

    _save_running_batch(
        db_path, batch_id="batch_a", project_id="proj_a", owner_user_id="owner_a"
    )
    _save_running_batch(
        db_path, batch_id="batch_b", project_id="proj_b", owner_user_id="owner_b"
    )

    video = _write_valid_video(tmp_path / "clip.mp4", duration_frames=96)
    store = SceneRenderOperationStore(db_path)

    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            """INSERT INTO video_factory_scene_operation (
                operation_id, owner_user_id, project_id, scene_id, request_digest,
                status, render_batch_id, scene_order, output_path, output_sha256,
                expected_duration_seconds, expected_aspect_ratio
            ) VALUES ('op_a', 'owner_a', 'proj_a', 'scene-1', 'digest_a', 'completed', 'batch_a', 1, ?, '', 4.0, '9:16')""",
            (str(video),),
        )
        conn.execute(
            """INSERT INTO video_factory_scene_operation (
                operation_id, owner_user_id, project_id, scene_id, request_digest,
                status, render_batch_id, scene_order, output_path, output_sha256,
                expected_duration_seconds, expected_aspect_ratio
            ) VALUES ('op_b', 'owner_b', 'proj_b', 'scene-1', 'digest_b', 'planned', 'batch_b', 1, '', '', 4.0, '9:16')""",
        )
    conn.close()

    res = server.get_job_status(job_id="batch_a")
    assert res["status"] == "failed"
    assert res["code"] == "SCENE_DIGEST_MISSING"

    op_a = store.get("op_a")
    assert op_a["status"] == "failed"
    assert op_a["output_sha256"] == ""

    op_b = store.get("op_b")
    assert op_b["status"] == "planned"
    assert op_b["owner_user_id"] == "owner_b"
    assert op_b["project_id"] == "proj_b"


def test_get_job_status_clip_tamper_fails_batch_and_invalidates_final_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tampering with a completed clip file must fail batch and invalidate Final Approval to PENDING."""
    from dataclasses import replace
    from video_factory.integrations import server
    from video_factory.integrations.domain import FinalApprovalStatus
    from video_factory.integrations.scene_renderer import (
        SceneRenderOperationStore,
    )

    ws = tmp_path / "workspace"
    db_path = tmp_path / "factory.sqlite"
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_path))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_WORKSPACE", str(ws))

    server.video_project_create("owner", "proj_tamper")
    srv = server._service()
    proj = srv.get_project("owner", "proj_tamper")
    srv.repository.save(
        replace(
            proj,
            final_approval=FinalApprovalStatus.APPROVED,
            final_approval_notes="Initially approved",
        )
    )

    repo = _save_running_batch(
        db_path,
        batch_id="batch_tamper",
        project_id="proj_tamper",
        owner_user_id="owner",
    )

    clip = _write_valid_video(tmp_path / "clip.mp4", duration_frames=96)
    clip_sha = _sha256(clip)
    store = SceneRenderOperationStore(db_path)

    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            """INSERT INTO video_factory_scene_operation (
                operation_id, owner_user_id, project_id, scene_id, request_digest,
                status, render_batch_id, scene_order, output_path, output_sha256,
                expected_duration_seconds, expected_aspect_ratio
            ) VALUES ('op_tamper_1', 'owner', 'proj_tamper', 'scene-1', 'digest_1', 'completed', 'batch_tamper', 1, ?, ?, 4.0, '9:16')""",
            (str(clip), f"sha256:{clip_sha}"),
        )
    conn.close()

    clip.write_bytes(clip.read_bytes() + b"TAMPERED_BYTES")

    res = server.get_job_status(job_id="batch_tamper")
    assert res["status"] == "failed"
    assert res["code"] == "SCENE_DIGEST_MISMATCH"

    batch = repo.get_render_batch("batch_tamper")
    assert batch.status == "failed"

    proj_after = srv.get_project("owner", "proj_tamper")
    assert proj_after.final_approval == FinalApprovalStatus.PENDING
    assert "Invalidated" in proj_after.final_approval_notes


def test_get_job_status_composite_tamper_forces_rebuild_and_invalidates_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Modifying a composite MP4 into another valid video must force rebuild from trusted clips and invalidate approval."""
    from dataclasses import replace
    from video_factory.integrations import server
    from video_factory.integrations.composer import compute_file_sha256
    from video_factory.integrations.domain import FinalApprovalStatus
    from video_factory.integrations.scene_renderer import (
        SceneRenderOperationStore,
    )

    ws = tmp_path / "workspace"
    db_path = tmp_path / "factory.sqlite"
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_path))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_WORKSPACE", str(ws))

    layout = ProductVideoWorkspaceLayout(ws)
    server.video_project_create("owner", "proj_comp_tamper")
    srv = server._service()

    clip = _write_valid_video(
        tmp_path / "clip.mp4", width=720, height=1280, duration_frames=96
    )
    clip_sha = _sha256(clip)
    SceneRenderOperationStore(db_path)

    batch_id = "batch_comp_tamper"
    _save_running_batch(
        db_path, batch_id=batch_id, project_id="proj_comp_tamper", owner_user_id="owner"
    )

    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            """INSERT INTO video_factory_scene_operation (
                operation_id, owner_user_id, project_id, scene_id, request_digest,
                status, render_batch_id, scene_order, output_path, output_sha256,
                expected_duration_seconds, expected_aspect_ratio
            ) VALUES ('op_ct_1', 'owner', 'proj_comp_tamper', 'scene-1', 'digest_1', 'completed', ?, 1, ?, ?, 4.0, '9:16')""",
            (batch_id, str(clip), f"sha256:{clip_sha}"),
        )
    conn.close()

    res1 = server.get_job_status(job_id=batch_id)
    assert res1["status"] == "completed"
    original_comp_sha = res1["sha256"]

    comp_dir = layout.get_composite_dir("proj_comp_tamper", 1)
    comp_file = comp_dir / "draft.mp4"
    assert comp_file.is_file()
    assert compute_file_sha256(comp_file) == original_comp_sha

    proj = srv.get_project("owner", "proj_comp_tamper")
    srv.repository.save(
        replace(
            proj,
            final_approval=FinalApprovalStatus.APPROVED,
            final_approval_notes="Approved initial composite",
        )
    )

    tampered_video = _write_valid_video(
        tmp_path / "different_valid.mp4", width=720, height=1280, duration_frames=48
    )
    tampered_sha = _sha256(tampered_video)
    comp_file.write_bytes(tampered_video.read_bytes())
    assert compute_file_sha256(comp_file) == tampered_sha

    res2 = server.get_job_status(job_id=batch_id)
    assert res2["status"] == "completed"

    rebuilt_comp_sha = compute_file_sha256(comp_file)
    assert rebuilt_comp_sha != tampered_sha
    assert rebuilt_comp_sha == original_comp_sha

    proj_after = srv.get_project("owner", "proj_comp_tamper")
    assert proj_after.final_approval == FinalApprovalStatus.PENDING
    assert "Invalidated" in proj_after.final_approval_notes


def test_get_job_status_unregistered_composite_rebuilds_and_invalidates_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing draft.mp4 with NO composite artifact row must NOT be trusted; must rebuild from verified clips and invalidate approval."""
    from dataclasses import replace
    from video_factory.integrations import server
    from video_factory.integrations.composer import compute_file_sha256
    from video_factory.integrations.domain import FinalApprovalStatus
    from video_factory.integrations.scene_renderer import (
        SceneRenderOperationStore,
    )

    ws = tmp_path / "workspace"
    db_path = tmp_path / "factory.sqlite"
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_path))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_WORKSPACE", str(ws))

    layout = ProductVideoWorkspaceLayout(ws)
    server.video_project_create("owner", "proj_unreg")
    srv = server._service()

    proj = srv.get_project("owner", "proj_unreg")
    srv.repository.save(
        replace(
            proj,
            final_approval=FinalApprovalStatus.APPROVED,
            final_approval_notes="Approved initial",
        )
    )

    clip = _write_valid_video(
        tmp_path / "clip.mp4", width=720, height=1280, duration_frames=96
    )
    clip_sha = _sha256(clip)
    SceneRenderOperationStore(db_path)

    batch_id = "batch_unreg"
    _save_running_batch(
        db_path, batch_id=batch_id, project_id="proj_unreg", owner_user_id="owner"
    )

    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            """INSERT INTO video_factory_scene_operation (
                operation_id, owner_user_id, project_id, scene_id, request_digest,
                status, render_batch_id, scene_order, output_path, output_sha256,
                expected_duration_seconds, expected_aspect_ratio
            ) VALUES ('op_unreg_1', 'owner', 'proj_unreg', 'scene-1', 'digest_1', 'completed', ?, 1, ?, ?, 4.0, '9:16')""",
            (batch_id, str(clip), f"sha256:{clip_sha}"),
        )
    conn.close()

    # Place a different, valid but untrusted video at draft.mp4
    comp_dir = layout.get_composite_dir("proj_unreg", 1)
    comp_dir.mkdir(parents=True, exist_ok=True)
    comp_file = comp_dir / "draft.mp4"
    tampered_video = _write_valid_video(
        tmp_path / "tampered.mp4", width=720, height=1280, duration_frames=48
    )
    tampered_sha = _sha256(tampered_video)
    comp_file.write_bytes(tampered_video.read_bytes())

    # Ensure NO composite artifact row exists
    comp_art_id = "art_composite_proj_unreg_v1"
    assert srv.repository.get_artifact(comp_art_id, owner_user_id="owner") is None

    res = server.get_job_status(job_id=batch_id)
    assert res["status"] == "completed"

    # Must have rebuilt from verified clip, not trusted the unregistered draft.mp4
    rebuilt_sha = compute_file_sha256(comp_file)
    assert rebuilt_sha != tampered_sha
    assert res["sha256"] == rebuilt_sha

    # Approval must be invalidated to PENDING
    proj_after = srv.get_project("owner", "proj_unreg")
    assert proj_after.final_approval == FinalApprovalStatus.PENDING
    assert "Invalidated" in proj_after.final_approval_notes

    # Artifact provenance must be complete
    saved_art = srv.repository.get_artifact(comp_art_id, owner_user_id="owner")
    assert saved_art is not None
    assert saved_art.sha256 == rebuilt_sha
    prov = (saved_art.provider_metadata or {}).get("provenance") or {}
    assert prov.get("scene_ids") == ["scene-1"]
    assert prov.get("request_digests") == ["digest_1"]
    assert prov.get("clip_digests") == [f"sha256:{clip_sha}"]


def test_get_job_status_incomplete_provenance_composite_rebuilds_and_invalidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A composite artifact with missing/empty provenance fields must trigger rebuild and invalidation."""
    from dataclasses import replace
    from video_factory.integrations import server
    from video_factory.integrations.composer import compute_file_sha256
    from video_factory.integrations.domain import (
        FinalApprovalStatus,
        VideoFactoryArtifact,
    )
    from video_factory.integrations.scene_renderer import (
        SceneRenderOperationStore,
    )

    ws = tmp_path / "workspace"
    db_path = tmp_path / "factory.sqlite"
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_path))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_WORKSPACE", str(ws))

    layout = ProductVideoWorkspaceLayout(ws)
    server.video_project_create("owner", "proj_inc_prov")
    srv = server._service()

    proj = srv.get_project("owner", "proj_inc_prov")
    srv.repository.save(
        replace(
            proj,
            final_approval=FinalApprovalStatus.APPROVED,
            final_approval_notes="Approved initial",
        )
    )

    clip = _write_valid_video(
        tmp_path / "clip.mp4", width=720, height=1280, duration_frames=96
    )
    clip_sha = _sha256(clip)
    SceneRenderOperationStore(db_path)

    batch_id = "batch_inc_prov"
    _save_running_batch(
        db_path, batch_id=batch_id, project_id="proj_inc_prov", owner_user_id="owner"
    )

    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            """INSERT INTO video_factory_scene_operation (
                operation_id, owner_user_id, project_id, scene_id, request_digest,
                status, render_batch_id, scene_order, output_path, output_sha256,
                expected_duration_seconds, expected_aspect_ratio
            ) VALUES ('op_inc_1', 'owner', 'proj_inc_prov', 'scene-1', 'digest_1', 'completed', ?, 1, ?, ?, 4.0, '9:16')""",
            (batch_id, str(clip), f"sha256:{clip_sha}"),
        )
    conn.close()

    comp_dir = layout.get_composite_dir("proj_inc_prov", 1)
    comp_dir.mkdir(parents=True, exist_ok=True)
    comp_file = comp_dir / "draft.mp4"
    tampered_video = _write_valid_video(
        tmp_path / "tampered.mp4", width=720, height=1280, duration_frames=48
    )
    tampered_sha = _sha256(tampered_video)
    comp_file.write_bytes(tampered_video.read_bytes())

    # Save artifact with INCOMPLETE provenance (missing request_digests and clip_digests)
    comp_art_id = "art_composite_proj_inc_prov_v1"
    srv.repository.save_artifact(
        VideoFactoryArtifact(
            artifact_id=comp_art_id,
            owner_user_id="owner",
            workflow_id="",
            project_id="proj_inc_prov",
            kind="composite",
            version=1,
            attempt=1,
            relative_path=layout.to_relative_path(comp_file),
            sha256=tampered_sha,
            size=comp_file.stat().st_size,
            mime_type="video/mp4",
            status="active",
            provider_metadata={"provenance": {"scene_ids": ["scene-1"]}},
            created_at="2026-09-18T00:00:00Z",
            updated_at="2026-09-18T00:00:00Z",
        )
    )

    res = server.get_job_status(job_id=batch_id)
    assert res["status"] == "completed"

    rebuilt_sha = compute_file_sha256(comp_file)
    assert rebuilt_sha != tampered_sha
    assert res["sha256"] == rebuilt_sha

    proj_after = srv.get_project("owner", "proj_inc_prov")
    assert proj_after.final_approval == FinalApprovalStatus.PENDING
    assert "Invalidated" in proj_after.final_approval_notes

    saved_art = srv.repository.get_artifact(comp_art_id, owner_user_id="owner")
    assert saved_art.sha256 == rebuilt_sha
    prov = (saved_art.provider_metadata or {}).get("provenance") or {}
    assert prov.get("scene_ids") == ["scene-1"]
    assert prov.get("request_digests") == ["digest_1"]
    assert prov.get("clip_digests") == [f"sha256:{clip_sha}"]


def test_get_job_status_composition_failure_fails_durable_batch_and_invalidates_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure during video concatenation must fail batch in SQLite, invalidate approval, clean staging, and not promote artifact."""
    from dataclasses import replace
    from video_factory.integrations import server
    from video_factory.integrations.domain import FinalApprovalStatus
    from video_factory.integrations.scene_renderer import (
        SceneRenderOperationStore,
    )

    ws = tmp_path / "workspace"
    db_path = tmp_path / "factory.sqlite"
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_path))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_WORKSPACE", str(ws))

    layout = ProductVideoWorkspaceLayout(ws)
    server.video_project_create("owner", "proj_concat_fail")
    srv = server._service()

    proj = srv.get_project("owner", "proj_concat_fail")
    srv.repository.save(
        replace(
            proj,
            final_approval=FinalApprovalStatus.APPROVED,
            final_approval_notes="Approved initial",
        )
    )

    clip = _write_valid_video(
        tmp_path / "clip.mp4", width=720, height=1280, duration_frames=96
    )
    clip_sha = _sha256(clip)
    SceneRenderOperationStore(db_path)

    batch_id = "batch_concat_fail"
    repo = _save_running_batch(
        db_path, batch_id=batch_id, project_id="proj_concat_fail", owner_user_id="owner"
    )

    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            """INSERT INTO video_factory_scene_operation (
                operation_id, owner_user_id, project_id, scene_id, request_digest,
                status, render_batch_id, scene_order, output_path, output_sha256,
                expected_duration_seconds, expected_aspect_ratio
            ) VALUES ('op_cf_1', 'owner', 'proj_concat_fail', 'scene-1', 'digest_1', 'completed', ?, 1, ?, ?, 4.0, '9:16')""",
            (batch_id, str(clip), f"sha256:{clip_sha}"),
        )
    conn.close()

    def _mock_concat_fail(*args, **kwargs):
        # Create staging file then fail
        staging = layout.get_composite_dir("proj_concat_fail", 1) / ".draft_staging.mp4"
        staging.write_bytes(b"staging_junk")
        raise RuntimeError("FFmpeg concatenation crashed")

    monkeypatch.setattr(server, "concatenate_videos", _mock_concat_fail)

    res = server.get_job_status(job_id=batch_id)
    assert res["ok"] is False
    assert res["status"] == "failed"
    assert res["code"] == "COMPOSITION_FAILED"

    # Render batch must be durably failed in repository
    batch = repo.get_render_batch(batch_id)
    assert batch.status == "failed"

    # Final approval must be invalidated to PENDING
    proj_after = srv.get_project("owner", "proj_concat_fail")
    assert proj_after.final_approval == FinalApprovalStatus.PENDING

    # Staging composite must be cleaned up
    staging = layout.get_composite_dir("proj_concat_fail", 1) / ".draft_staging.mp4"
    assert not staging.exists()

    # Draft composite should not exist
    draft = layout.get_composite_dir("proj_concat_fail", 1) / "draft.mp4"
    assert not draft.exists()


def test_get_job_status_invalidation_persistence_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Final Approval invalidation cannot be persisted, get_job_status must fail closed and NOT return completed."""
    from dataclasses import replace
    from video_factory.integrations import server
    from video_factory.integrations.domain import (
        FinalApprovalStatus,
        VideoFactoryProject,
    )
    from video_factory.integrations.scene_renderer import (
        SceneRenderOperationStore,
    )

    ws = tmp_path / "workspace"
    db_path = tmp_path / "factory.sqlite"
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_path))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_WORKSPACE", str(ws))

    server.video_project_create("owner", "proj_inv_fail")
    srv = server._service()

    proj = srv.get_project("owner", "proj_inv_fail")
    srv.repository.save(
        replace(
            proj,
            final_approval=FinalApprovalStatus.APPROVED,
            final_approval_notes="Approved initial",
        )
    )

    clip = _write_valid_video(
        tmp_path / "clip.mp4", width=720, height=1280, duration_frames=96
    )
    clip_sha = _sha256(clip)
    SceneRenderOperationStore(db_path)

    batch_id = "batch_inv_fail"
    _save_running_batch(
        db_path, batch_id=batch_id, project_id="proj_inv_fail", owner_user_id="owner"
    )

    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            """INSERT INTO video_factory_scene_operation (
                operation_id, owner_user_id, project_id, scene_id, request_digest,
                status, render_batch_id, scene_order, output_path, output_sha256,
                expected_duration_seconds, expected_aspect_ratio
            ) VALUES ('op_inv_1', 'owner', 'proj_inv_fail', 'scene-1', 'digest_1', 'completed', ?, 1, ?, ?, 4.0, '9:16')""",
            (batch_id, str(clip), f"sha256:{clip_sha}"),
        )
    from video_factory.integrations.repository import (
        SQLiteVideoFactoryRepository,
    )

    real_save = SQLiteVideoFactoryRepository.save

    def _failing_save(self, entity, *args, **kwargs):
        if (
            isinstance(entity, VideoFactoryProject)
            and entity.final_approval == FinalApprovalStatus.PENDING
        ):
            raise sqlite3.OperationalError(
                "Simulated disk error during approval invalidation"
            )
        return real_save(self, entity, *args, **kwargs)

    monkeypatch.setattr(SQLiteVideoFactoryRepository, "save", _failing_save)

    res = server.get_job_status(job_id=batch_id)
    assert res["ok"] is False
    assert res["status"] == "failed"
    assert (
        "FINAL_APPROVAL_INVALIDATION_FAILED" in (res.get("code") or "")
        or "invalidation" in res.get("error", "").lower()
    )

    # No composite artifact should have been registered
    comp_art = srv.repository.get_artifact(
        "art_composite_proj_inv_fail_v1", owner_user_id="owner"
    )
    assert comp_art is None


# ══════════════════════════════════════════════════════════════════════════════
# 4. Lineage Propagation And QC Binding (Task 4)
# ══════════════════════════════════════════════════════════════════════════════


def test_render_polling_returns_canonical_lineage_and_preserves_version_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Render polling must return all 6 canonical lineage fields and support version 2."""
    from video_factory.integrations import server
    from video_factory.integrations.domain import RenderBatch
    from video_factory.integrations.repository import SQLiteVideoFactoryRepository
    from video_factory.integrations.scene_renderer import SceneRenderOperationStore

    ws = tmp_path / "workspace"
    db_path = tmp_path / "factory.sqlite"
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_path))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_WORKSPACE", str(ws))

    owner_id = "user_lineage_1"
    proj_id = "proj_lineage_1"
    wf_id = "wf_lineage_1"
    batch_id = "batch_lineage_v2"

    server.video_project_create(owner_id, proj_id)
    repo = SQLiteVideoFactoryRepository(db_path)
    repo.save_render_batch(
        RenderBatch(
            render_batch_id=batch_id,
            owner_user_id=owner_id,
            workflow_id=wf_id,
            project_id=proj_id,
            status="running",
            scene_count=1,
            version=2,
        )
    )

    clip = _write_valid_video(
        tmp_path / "clip_v2.mp4", width=720, height=1280, duration_frames=96
    )
    clip_sha = _sha256(clip)
    SceneRenderOperationStore(db_path)

    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            """INSERT INTO video_factory_scene_operation (
                operation_id, owner_user_id, project_id, scene_id, request_digest,
                status, render_batch_id, scene_order, output_path, output_sha256,
                expected_duration_seconds, expected_aspect_ratio
            ) VALUES ('op_lin_1', ?, ?, 'scene-1', 'digest_lin_1', 'completed', ?, 1, ?, ?, 4.0, '9:16')""",
            (owner_id, proj_id, batch_id, str(clip), f"sha256:{clip_sha}"),
        )

    # 1. Newly composed branch
    res = server.get_job_status(job_id=batch_id)
    assert res["ok"] is True
    assert res["status"] == "completed"
    assert res["owner_user_id"] == owner_id
    assert res["project_id"] == proj_id
    assert res["workflow_id"] == wf_id
    assert res["artifact_id"] == f"art_composite_{proj_id}_v2"
    assert res["artifact_version"] == 2
    assert res["version"] == 2
    assert "sha256" in res and len(res["sha256"]) == 64

    # 2. Fast reuse branch replay
    replay_res = server.get_job_status(job_id=batch_id)
    assert replay_res["ok"] is True
    assert replay_res["status"] == "completed"
    assert replay_res["owner_user_id"] == owner_id
    assert replay_res["project_id"] == proj_id
    assert replay_res["workflow_id"] == wf_id
    assert replay_res["artifact_id"] == f"art_composite_{proj_id}_v2"
    assert replay_res["artifact_version"] == 2
    assert replay_res["version"] == 2
    assert replay_res["sha256"] == res["sha256"]


def test_mcp_adapter_render_video_preserves_canonical_lineage_and_version_two() -> None:
    """MCPVideoFactoryAdapter must not hardcode version 1 and must preserve all 6 lineage fields."""
    from video_factory.handlers import MCPVideoFactoryAdapter

    class MockRegistry:
        def dispatch(self, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
            if "render" in tool_name:
                return {"status": "pending", "job_id": "job_v2"}
            if "get_job_status" in tool_name:
                return {
                    "ok": True,
                    "status": "completed",
                    "job_id": "job_v2",
                    "video_url": "file:///workspace/proj_v2/composite/v2/draft.mp4",
                    "owner_user_id": "owner_v2",
                    "project_id": "proj_v2",
                    "workflow_id": "wf_v2",
                    "artifact_id": "art_composite_proj_v2_v2",
                    "artifact_version": 2,
                    "version": 2,
                    "sha256": "a" * 64,
                }
            return {}

        def get_all_entries(self):
            return [
                type("Entry", (), {"name": "mcp__video_factory__render"})(),
                type("Entry", (), {"name": "mcp__video_factory__get_job_status"})(),
            ]

    adapter = MCPVideoFactoryAdapter(
        registry=MockRegistry(),
        wire_tools={
            "render": "mcp__video_factory__render",
            "get_job_status": "mcp__video_factory__get_job_status",
        },
    )

    result = adapter.render_video(
        storyboard_result={"storyboard_id": "sb_1"},
        workflow_id="wf_v2",
        owner_user_id="owner_v2",
        project_id="proj_v2",
    )

    assert result["version"] == 2
    assert result["artifact_version"] == 2
    assert result["artifact_id"] == "art_composite_proj_v2_v2"
    assert result["owner_user_id"] == "owner_v2"
    assert result["project_id"] == "proj_v2"
    assert result["workflow_id"] == "wf_v2"
    assert result["sha256"] == "a" * 64


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda value: value.pop("owner_user_id"), "RENDER_LINEAGE_INVALID"),
        (lambda value: value.pop("project_id"), "RENDER_LINEAGE_INVALID"),
        (lambda value: value.pop("workflow_id"), "RENDER_LINEAGE_INVALID"),
        (lambda value: value.pop("artifact_id"), "RENDER_LINEAGE_INVALID"),
        (lambda value: value.pop("artifact_version"), "RENDER_LINEAGE_INVALID"),
        (lambda value: value.pop("sha256"), "RENDER_LINEAGE_INVALID"),
        (lambda value: value.update(artifact_version=0), "RENDER_LINEAGE_INVALID"),
        (lambda value: value.update(artifact_version=-1), "RENDER_LINEAGE_INVALID"),
        (lambda value: value.update(artifact_version="two"), "RENDER_LINEAGE_INVALID"),
        (lambda value: value.update(owner_user_id="other-owner"), "RENDER_LINEAGE_MISMATCH"),
        (lambda value: value.update(project_id="other-project"), "RENDER_LINEAGE_MISMATCH"),
        (lambda value: value.update(workflow_id="other-workflow"), "RENDER_LINEAGE_MISMATCH"),
        (lambda value: value.update(version=3), "RENDER_LINEAGE_MISMATCH"),
    ],
    ids=[
        "missing-owner",
        "missing-project",
        "missing-workflow",
        "missing-artifact-id",
        "missing-artifact-version",
        "missing-sha256",
        "zero-version",
        "negative-version",
        "non-integer-version",
        "mismatched-owner",
        "mismatched-project",
        "mismatched-workflow",
        "mismatched-version-alias",
    ],
)
def test_mcp_adapter_render_video_rejects_invalid_canonical_lineage(
    mutation: Any,
    expected_code: str,
) -> None:
    """A completed poll response may not borrow or invent canonical lineage."""
    from video_factory.handlers import (
        MCPVideoFactoryAdapter,
        TerminalJobError,
    )

    completed = {
        "ok": True,
        "status": "completed",
        "job_id": "job-lineage",
        "video_url": "file:///workspace/proj-lineage/composite/v2/draft.mp4",
        "owner_user_id": "owner-lineage",
        "project_id": "proj-lineage",
        "workflow_id": "wf-lineage",
        "artifact_id": "art-composite-v2",
        "artifact_version": 2,
        "version": 2,
        "sha256": "b" * 64,
    }
    mutation(completed)

    class Registry:
        def dispatch(self, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
            if "get_job_status" in tool_name:
                return dict(completed)
            return {"status": "pending", "job_id": "job-lineage"}

        def get_all_entries(self):
            return [
                type("Entry", (), {"name": "mcp__video_factory__render"})(),
                type("Entry", (), {"name": "mcp__video_factory__get_job_status"})(),
            ]

    adapter = MCPVideoFactoryAdapter(
        registry=Registry(),
        wire_tools={
            "render": "mcp__video_factory__render",
            "get_job_status": "mcp__video_factory__get_job_status",
        },
    )

    with pytest.raises(TerminalJobError) as exc_info:
        adapter.render_video(
            storyboard_result={"storyboard_id": "sb-lineage"},
            workflow_id="wf-lineage",
            owner_user_id="owner-lineage",
            project_id="proj-lineage",
        )

    assert exc_info.value.code == expected_code


def test_export_rejects_missing_or_mismatched_lineage_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """server.export() must reject absent workflow_id and any mismatch in the 6-tuple lineage."""
    from video_factory.integrations import server
    from video_factory.integrations.domain import FinalApprovalStatus
    from video_factory.qc.models import QCReport, QCStage, QCVerdict
    from video_factory.qc.repository import QCReportRepository

    ws = tmp_path / "workspace"
    db_path = tmp_path / "factory.sqlite"
    appr_db_path = tmp_path / "approval.sqlite"
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_path))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_WORKSPACE", str(ws))
    monkeypatch.setenv("HERMES_APPROVAL_DB_PATH", str(appr_db_path))

    owner_id = "user_exp_lin"
    proj_id = "proj_exp_lin"
    wf_id = "wf_exp_lin"

    server.video_project_create(owner_id, proj_id)
    srv = server._service()
    proj = srv.get_project(owner_id, proj_id)
    from dataclasses import replace
    from video_factory.integrations.domain import ResourcePack, AssetReference
    ref_asset = AssetReference(asset_id="asset_1", uri=f"file://{ws}/ref.png", metadata={"sha256": "abc"})
    srv.repository.save(
        replace(
            proj,
            final_approval=FinalApprovalStatus.APPROVED,
            final_approval_notes="Approved for export",
            resource_pack=ResourcePack(
                id="rp_1",
                owner_user_id=owner_id,
                product_references=(ref_asset,),
                primary_product_asset_id="asset_1",
                product_identity_description="Grounded Smart Lamp",
            ),
        )
    )

    video_file = _write_valid_video(
        ws / proj_id / "composite" / "v1" / "draft.mp4",
        width=720,
        height=1280,
        duration_frames=96,
    )
    video_sha = _sha256(video_file)
    from video_factory.integrations.domain import VideoFactoryArtifact

    srv.repository.save_artifact(
        VideoFactoryArtifact(
            artifact_id=f"art_composite_{proj_id}_v2",
            owner_user_id=owner_id,
            workflow_id=wf_id,
            project_id=proj_id,
            kind="composite",
            relative_path=video_file.relative_to(ws).as_posix(),
            sha256=video_sha,
            version=2,
            size=video_file.stat().st_size,
            mime_type="video/mp4",
            status="active",
        )
    )

    # Save a valid PASS report in approval DB
    conn = sqlite3.connect(appr_db_path)
    qc_repo = QCReportRepository(conn)
    report = QCReport(
        report_id="qc_vid_pass_1",
        workflow_id=wf_id,
        stage=QCStage.VIDEO,
        artifact_id=f"art_composite_{proj_id}_v2",
        artifact_version=2,
        artifact_digest=f"sha256:{video_sha}",
        reference_digests=[],
        evaluator_name="eval_test",
        evaluator_version="1",
        rubric_version="1",
        created_at="2026-09-18T12:00:00Z",
        score=95,
        verdict=QCVerdict.PASS,
        findings={},
        owner_user_id=owner_id,
        project_id=proj_id,
    )
    qc_repo.save_report(report)
    conn.close()

    render_result = {
        "video_url": f"file://{video_file.resolve()}",
        "owner_user_id": owner_id,
        "project_id": proj_id,
        "workflow_id": wf_id,
        "artifact_id": f"art_composite_{proj_id}_v2",
        "artifact_version": 2,
        "version": 2,
        "sha256": video_sha,
    }

    cases: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for field in (
        "owner_user_id",
        "project_id",
        "workflow_id",
        "artifact_id",
        "artifact_version",
        "sha256",
    ):
        candidate = dict(render_result)
        candidate.pop(field)
        cases.append((f"missing-{field}", candidate, {}))
    cases.extend(
        [
            ("mismatched-owner", dict(render_result, owner_user_id="other-owner"), {}),
            ("mismatched-project", dict(render_result, project_id="other-project"), {}),
            ("mismatched-workflow", dict(render_result, workflow_id="other-workflow"), {}),
            ("mismatched-artifact-id", dict(render_result, artifact_id="other-artifact"), {}),
            ("mismatched-artifact-version", dict(render_result, artifact_version=3, version=3), {}),
            ("mismatched-sha256", dict(render_result, sha256="f" * 64), {}),
            ("zero-version", dict(render_result, artifact_version=0), {}),
            ("negative-version", dict(render_result, artifact_version=-1), {}),
            ("non-integer-version", dict(render_result, artifact_version="two"), {}),
        ]
    )

    for case_name, candidate, extra_kwargs in cases:
        result = server.export(
            render_result=candidate,
            owner_user_id=owner_id,
            project_id=proj_id,
            workflow_id=wf_id,
            **extra_kwargs,
        )
        assert result["ok"] is False, case_name

    # Matching version-2 six-tuple lineage succeeds.
    res_ok = server.export(
        render_result=render_result,
        owner_user_id=owner_id,
        project_id=proj_id,
        workflow_id=wf_id,
    )
    assert res_ok["ok"] is True


def test_video_qc_stores_canonical_lineage_and_fails_on_missing_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ProductVideoQCService and run_video_qc must fail closed if owner/project is missing and record exact version/artifact_id."""
    from video_factory.qc.service import ProductVideoQCService
    from video_factory.qc.models import QCStage, QCVerdict, QCReport
    from video_factory.qc.repository import QCReportRepository
    from video_factory.qc.image_qc import compute_artifact_digest
    from video_factory.workflows.repository import WorkflowRepository

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS workflow (
            workflow_id TEXT PRIMARY KEY,
            aggregate_json TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS workflow_event (
            event_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            command_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    """)

    wf_repo = WorkflowRepository(db)
    qc_repo = QCReportRepository(db)

    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_WORKSPACE", str(ws))

    video_file = _write_valid_video(
        ws / "comp_v2.mp4", width=720, height=1280, duration_frames=96
    )
    video_sha = _sha256(video_file)

    ref1 = _write_valid_video(ws / "ref1.mp4", width=64, height=64, duration_frames=3)
    ref2 = _write_valid_video(ws / "ref2.mp4", width=64, height=64, duration_frames=3)

    rp_data = {
        "id": "rp_1",
        "version": 1,
        "product_identity": {"brand": "B", "model": "M"},
    }
    img_digest = compute_artifact_digest(rp_data)

    # Pre-seed Image QC PASS
    qc_repo.save_report(
        QCReport(
            report_id="qc_img_1",
            workflow_id="wf_qc_lin_2",
            stage=QCStage.IMAGE,
            artifact_id="rp_1",
            artifact_version=1,
            artifact_digest=img_digest,
            reference_digests=[],
            evaluator_name="test",
            evaluator_version="1",
            rubric_version="1",
            created_at="2026-09-18T12:00:00Z",
            score=95,
            verdict=QCVerdict.PASS,
            findings={
                "items": [
                    {"uri": f"file://{ref1.resolve()}", "eligible": True},
                    {"uri": f"file://{ref2.resolve()}", "eligible": True},
                ]
            },
            owner_user_id="user_qc_2",
            project_id="proj_qc_2",
        )
    )

    agg = {
        "owner_user_id": "user_qc_2",
        "project_id": "proj_qc_2",
        "resource_pack": rp_data,
        "storyboard": {
            "storyboard_id": "sb_1",
            "scenes": [
                {"scene_id": "s1", "duration_seconds": 4.0, "visual_objective": "intro"},
            ],
        },
        "render_result": {
            "file_path": str(video_file.resolve()),
            "owner_user_id": "user_qc_2",
            "project_id": "proj_qc_2",
            "workflow_id": "wf_qc_lin_2",
            "artifact_id": "art_composite_proj_qc_2_v2",
            "artifact_version": 2,
            "version": 2,
            "sha256": video_sha,
        },
    }
    wf_repo.commit_workflow("wf_qc_lin_2", json.dumps(agg), [])

    svc = ProductVideoQCService(db)
    res = svc.run("wf_qc_lin_2", QCStage.VIDEO, provider_mode="fake")

    assert res["status"] == "completed"
    assert res["report"].artifact_id == "art_composite_proj_qc_2_v2"
    assert res["report"].artifact_version == 2
    assert res["report"].owner_user_id == "user_qc_2"
    assert res["report"].project_id == "proj_qc_2"
    assert res["report"].workflow_id == "wf_qc_lin_2"

    class NeverCalledEvaluator:
        evaluator_name = "never-called"
        evaluator_version = "1"
        model = "local"

        def __init__(self) -> None:
            self.calls = 0

        def evaluate_multiple_images(self, **_kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            raise AssertionError("invalid lineage reached the evaluator")

    invalid_cases: list[tuple[str, dict[str, Any]]] = []
    valid_render = dict(agg["render_result"])
    for field in (
        "owner_user_id",
        "project_id",
        "workflow_id",
        "artifact_id",
        "artifact_version",
        "sha256",
    ):
        candidate = dict(valid_render)
        candidate.pop(field)
        invalid_cases.append((f"missing-{field}", candidate))
    invalid_cases.extend(
        [
            ("mismatched-owner", dict(valid_render, owner_user_id="other-owner")),
            ("mismatched-project", dict(valid_render, project_id="other-project")),
            ("mismatched-workflow", dict(valid_render, workflow_id="other-workflow")),
            ("mismatched-version-alias", dict(valid_render, version=3)),
            ("mismatched-sha", dict(valid_render, sha256="f" * 64)),
            ("zero-version", dict(valid_render, artifact_version=0)),
            ("negative-version", dict(valid_render, artifact_version=-1)),
            ("non-integer-version", dict(valid_render, artifact_version="two")),
        ]
    )

    report_count = db.execute(
        "SELECT COUNT(*) FROM product_video_qc_reports"
    ).fetchone()[0]
    evaluator = NeverCalledEvaluator()
    strict_svc = ProductVideoQCService(db, evaluator=evaluator)
    for case_name, candidate in invalid_cases:
        failed = strict_svc.run(
            "wf_qc_lin_2",
            QCStage.VIDEO,
            provider_mode="fake",
            artifact_override=candidate,
        )
        assert failed["status"] == "failed", case_name
        assert evaluator.calls == 0, case_name
        assert (
            db.execute("SELECT COUNT(*) FROM product_video_qc_reports").fetchone()[0]
            == report_count
        ), case_name
