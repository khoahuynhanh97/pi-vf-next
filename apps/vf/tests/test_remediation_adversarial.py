"""Adversarial regression test suite for hardened Product Video architecture.

Verifies:
1. Paid-submission state machine (no duplicate submit, no regression).
2. Trusted exchange root boundary and decodability validation.
3. Tamper-evident composite verification and rebuild via draft staging.
4. Video QC provenance binding (no cross-project / unauthenticated authorization).
5. Status-driven cleanup (never infer failure from dirname, recursive parent preservation).
6. End-keyframe modeling and clean policy serialization.
7. Owner-scoped artifact access and collision prevention.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from unittest.mock import MagicMock

import av
import pytest
from PIL import Image

from hermes_personal.integrations.video_factory import server
from video_factory.integrations.cleanup import cleanup_unreferenced_artifacts
from video_factory.integrations.composer import VideoExpectation, compute_file_sha256, probe_video
from video_factory.integrations.domain import (
    FinalApprovalStatus,
    GeneratedScene,
    ProjectStatus,
    RenderBatch,
    Storyboard,
    StoryboardFrame,
    VideoFactoryArtifact,
    VideoGenerationStatus,
    VideoPrompt,
)
from video_factory.integrations.ports import (
    KeyframeGenerationRequest,
    VideoGenerationRequest,
    VideoGenerationResult,
)
from video_factory.integrations.repository import SQLiteVideoFactoryRepository
from video_factory.integrations.scene_renderer import (
    SceneRenderOperationStore,
    SceneRenderRuntime,
)
from video_factory.creative.contracts import (
    EligibleAsset,
    EvidenceClaim,
    ProductIdentityV2,
    ResearchDossier,
    ScriptScenePlanV2,
    ScriptSceneV2,
)
from video_factory.creative.storyboard import compile_keyframe_requests
from video_factory.qc.models import QCReport, QCStage, QCVerdict
from video_factory.qc.repository import QCReportRepository
from video_factory.storage.layout import ProductVideoWorkspaceLayout


def _create_valid_png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (64, 64), color=(255, 0, 0))
    img.save(path, format="PNG")
    return path


def _write_valid_mp4(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    container = av.open(str(path), mode="w", format="mp4")
    stream = container.add_stream("libx264", rate=24)
    stream.width = 72
    stream.height = 128
    stream.pix_fmt = "yuv420p"
    for index in range(25):
        frame = av.VideoFrame(72, 128, "yuv420p")
        for plane in frame.planes:
            plane.update(bytes([(48 + index) % 256]) * plane.buffer_size)
        frame.pts = index
        frame.time_base = Fraction(1, 24)
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    return path


# ---------------------------------------------------------------------------
# 1. PAID-SUBMISSION STATE MACHINE
# ---------------------------------------------------------------------------


def test_paid_submission_prevents_duplicate_submit_and_status_regression(tmp_path):
    db_path = tmp_path / "scene_ops.sqlite"
    conn = sqlite3.connect(db_path)
    store = SceneRenderOperationStore(conn)
    
    req = VideoGenerationRequest(
        request_id="req-paid-001",
        owner_user_id="user_1",
        project_id="proj_1",
        scene_id="scene_1",
        prompt="A photo of a lamp",
        duration_seconds=4,
    )

    # 1. Mark planned
    store.mark_planned(req, render_batch_id="batch_1", scene_order=1)
    op = store.get(req.request_id)
    assert op is not None
    assert op["status"] == "planned"

    # 2. Transition to submitting
    ok = store.mark_submitting(req)
    assert ok is True
    op = store.get(req.request_id)
    assert op["status"] == "submitting"

    # 3. mark_planned cannot regress status once submitting
    store.mark_planned(req, render_batch_id="batch_1", scene_order=1)
    op = store.get(req.request_id)
    assert op["status"] == "submitting"

    # 4. In submitting state, runtime probes provider; does NOT call provider.submit again
    mock_provider = MagicMock()
    mock_provider.provider_kind = "google_flow"
    mock_provider.is_available = True
    mock_provider.get_by_request_id = MagicMock(return_value=None)
    mock_provider.lookup_by_request_id = MagicMock(return_value=None)
    runtime = SceneRenderRuntime(store=store, provider=mock_provider)

    res = runtime.run(req)
    assert res.status == "failed"
    assert "RECONCILIATION_REQUIRED" in res.error_code
    assert mock_provider.submit.call_count == 0  # CRUCIAL: No duplicate paid submit!

    conn.close()


# ---------------------------------------------------------------------------
# 2. TRUSTED EXCHANGE ROOT BOUNDARY
# ---------------------------------------------------------------------------


def test_trusted_exchange_roots_reject_untrusted_sources_and_survive_deletion(tmp_path):
    ws_dir = tmp_path / "ws"
    ws = ProductVideoWorkspaceLayout(ws_dir)
    exchange_dir = tmp_path / "allowed_exchange"
    exchange_dir.mkdir(parents=True, exist_ok=True)
    untrusted_dir = tmp_path / "untrusted"
    untrusted_dir.mkdir(parents=True, exist_ok=True)

    # Allowed exchange source
    trusted_file = _create_valid_png(exchange_dir / "valid_product.png")
    # Untrusted source
    untrusted_file = _create_valid_png(untrusted_dir / "sensitive_photo.png")
    # Corrupt non-image
    corrupt_file = exchange_dir / "corrupt.png"
    corrupt_file.write_bytes(b"NOT_A_REAL_IMAGE")

    # 1. Untrusted root rejected
    with pytest.raises(ValueError, match="UNTRUSTED_REFERENCE_SOURCE"):
        ws.ingest_reference("proj_1", untrusted_file, allowed_source_roots=(exchange_dir,))

    # 2. Corrupt image rejected
    with pytest.raises(ValueError, match="INVALID_IMAGE_ARTIFACT"):
        ws.ingest_reference("proj_1", corrupt_file, allowed_source_roots=(exchange_dir,))

    # 3. Trusted root ingested successfully
    dest, sha, size = ws.ingest_reference("proj_1", trusted_file, allowed_source_roots=(exchange_dir,))
    assert dest.is_file()
    assert ws.root in dest.parents

    # 4. Survival: deleting original external file leaves workspace file intact
    trusted_file.unlink()
    assert dest.is_file()


# ---------------------------------------------------------------------------
# 3. COMPOSITE INTEGRITY AND REBUILD ON TAMPER
# ---------------------------------------------------------------------------


def test_storyboard_order_validation_and_tampered_composite_detection(tmp_path, monkeypatch):
    db_file = tmp_path / "vf_test.sqlite"
    ws_dir = tmp_path / "ws_test"
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_file))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_WORKSPACE", str(ws_dir))

    repo = SQLiteVideoFactoryRepository(db_file)

    # Create project with duplicate frame orders
    server.video_project_create("user_test", "proj_order_dup")
    srv = server._service()
    proj = srv.get_project("user_test", "proj_order_dup")

    dup_frames = (
        StoryboardFrame(
            frame_id="f1", scene_id="s1", order=1, label="Scene 1", purpose="intro",
            visual_state="start", subject_action="hold", product_state="intact",
            character_state="none", context="desk", camera_intention="static",
        ),
        StoryboardFrame(
            frame_id="f2", scene_id="s2", order=1, label="Scene 2", purpose="detail",
            visual_state="mid", subject_action="turn", product_state="intact",
            character_state="none", context="desk", camera_intention="static",
        ),
    )
    from video_factory.integrations.domain import StoryboardApprovalStatus
    repo.save(replace(proj, storyboard=Storyboard(storyboard_id="sb_1", project_id="proj_order_dup", frames=dup_frames, approval_status=StoryboardApprovalStatus.APPROVED)))

    from video_factory.workflows.budgets import init_default_engine, BudgetPolicy, reserve_budget
    init_default_engine(BudgetPolicy(max_total=1000, per_channel={"video_generation": 1000}))
    alloc = reserve_budget("video_generation", 100, owner_user_id="user_test", scope_id="proj_order_dup")

    # render() must reject duplicate frame orders
    mock_provider = MagicMock(model="mock-model")
    mock_provider.provider_kind = "google_flow"
    mock_provider.is_available = True
    res = server.render(
        project_id="proj_order_dup",
        owner_user_id="user_test",
        admission_context={"reservation_id": alloc.reservation_id},
        provider=mock_provider,
    )
    assert res["ok"] is False
    assert res["code"] == "INVALID_SCENE_ORDER"


# ---------------------------------------------------------------------------
# 4. VIDEO QC PROVENANCE BINDING (CROSS-PROJECT / UNOWNED REJECTION)
# ---------------------------------------------------------------------------


def test_export_rejects_qc_report_from_another_project_or_owner(tmp_path, monkeypatch):
    db_file = tmp_path / "qc_export.sqlite"
    ws_dir = tmp_path / "ws_export"
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_file))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_WORKSPACE", str(ws_dir))

    server.video_project_create("alice", "proj_alice")
    server.resource_pack_save("alice", "proj_alice", {
        "id": "rp_alice",
        "product_references": [{"asset_id": "a1", "uri": "asset://lamp.png"}],
        "primary_product_asset_id": "a1",
        "product_identity_description": "Xiaomi Mijia Desk Lamp Lite",
    })
    srv = server._service()
    proj = srv.get_project("alice", "proj_alice")
    srv.repository.save(replace(proj, final_approval=FinalApprovalStatus.APPROVED))

    video_file = _write_valid_mp4(
        ws_dir / "projects" / "proj_alice" / "renders" / "v1" / "composite" / "draft.mp4"
    )
    v_sha = compute_file_sha256(video_file)
    artifact_id = "art_alice"
    srv.repository.save_artifact(
        VideoFactoryArtifact(
            artifact_id=artifact_id,
            owner_user_id="alice",
            workflow_id="wf_alice",
            project_id="proj_alice",
            kind="composite",
            relative_path=video_file.relative_to(ws_dir).as_posix(),
            sha256=v_sha,
            version=1,
            size=video_file.stat().st_size,
            mime_type="video/mp4",
            status="active",
        )
    )

    # Mock video expectations
    monkeypatch.setattr(
        server,
        "_EXPORT_VIDEO_EXPECTATION",
        VideoExpectation(
            width=72,
            height=128,
            duration_seconds=probe_video(video_file).duration_seconds,
            codec="h264",
            fps=24.0,
            min_frames=1,
            max_duration_delta=0.5,
        ),
    )

    # Save a PASS QC report belonging to BOB and proj_bob with matching bytes!
    qc_conn = sqlite3.connect(db_file)
    qc_repo = QCReportRepository(qc_conn)
    qc_repo.save_report(
        QCReport(
            report_id="qc-bob-pass",
            workflow_id="wf_bob",
            stage=QCStage.VIDEO,
            artifact_id="art_bob",
            artifact_version=1,
            artifact_digest=f"sha256:{v_sha}",
            reference_digests=(),
            evaluator_name="evaluator",
            evaluator_version="1.0",
            rubric_version="1.0",
            created_at="2026-09-18T00:00:00Z",
            score=95,
            verdict=QCVerdict.PASS,
            findings={},
            owner_user_id="bob",         # WRONG OWNER!
            project_id="proj_bob",       # WRONG PROJECT!
        )
    )
    qc_conn.close()

    rend_result = {
        "status": "completed",
        "video_url": f"file://{video_file.resolve()}",
        "render_id": "r1",
        "project_id": "proj_alice",
        "owner_user_id": "alice",
        "workflow_id": "wf_alice",
        "artifact_id": artifact_id,
        "artifact_version": 1,
        "sha256": v_sha,
    }

    # Alice's export must fail because the QC report is bound to Bob's project
    out = server.export(render_result=rend_result, project_id="proj_alice", owner_user_id="alice")
    assert out["ok"] is False
    assert out["code"] == "VIDEO_QC_REQUIRED"
    assert "VIDEO_QC_REQUIRED" in out["error"]


# ---------------------------------------------------------------------------
# 5. STATUS-DRIVEN CLEANUP
# ---------------------------------------------------------------------------


def test_cleanup_preserves_unmanaged_files_and_only_deletes_failed_superseded(tmp_path):
    ws_dir = tmp_path / "ws_cleanup"
    ws = ProductVideoWorkspaceLayout(ws_dir)
    db_file = tmp_path / "cleanup.sqlite"
    repo = SQLiteVideoFactoryRepository(db_file)

    proj_dir = ws.get_project_dir("proj_clean")
    proj_dir.mkdir(parents=True, exist_ok=True)

    # 1. Create active artifact
    active_file = proj_dir / "active_clip.mp4"
    active_file.write_bytes(b"ACTIVE_BYTES")
    repo.save_artifact(
        VideoFactoryArtifact(
            artifact_id="art_active",
            owner_user_id="user_1",
            workflow_id="wf_1",
            project_id="proj_clean",
            kind="clip",
            relative_path=ws.to_relative_path(active_file),
            sha256=compute_file_sha256(active_file),
            size=active_file.stat().st_size,
            status="active",
        )
    )

    # 2. Create failed artifact
    failed_file = proj_dir / "failed_clip.mp4"
    failed_file.write_bytes(b"FAILED_BYTES")
    repo.save_artifact(
        VideoFactoryArtifact(
            artifact_id="art_failed",
            owner_user_id="user_1",
            workflow_id="wf_1",
            project_id="proj_clean",
            kind="clip",
            relative_path=ws.to_relative_path(failed_file),
            sha256=compute_file_sha256(failed_file),
            size=failed_file.stat().st_size,
            status="failed",
        )
    )

    # 3. Create unmanaged file in attempt directory (NOT registered in DB)
    attempt_dir = proj_dir / "attempt-1"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    unmanaged_file = attempt_dir / "unmanaged.mp4"
    unmanaged_file.write_bytes(b"UNMANAGED_ATTEMPT_BYTES")

    # Set mtimes in the past to exceed retention
    past_time = time.time() - 100_000
    os.utime(active_file, (past_time, past_time))
    os.utime(failed_file, (past_time, past_time))
    os.utime(unmanaged_file, (past_time, past_time))

    # Also register the project
    from video_factory.integrations.domain import VideoFactoryProject
    repo.create(VideoFactoryProject(id="proj_clean", owner_user_id="user_1"))

    # Run cleanup with retention_seconds=10 (older files eligible)
    rep = cleanup_unreferenced_artifacts(
        "proj_clean",
        "user_1",
        repository=repo,
        layout=ws,
        dry_run=False,
        retention_seconds=10.0,
    )

    # Assertions:
    # - Only art_failed should be deleted
    # - active_file must be preserved
    # - unmanaged_file (despite being in attempt-1) must be PRESERVED because it's not marked failed/superseded!
    assert rep.deleted_count == 1
    assert not failed_file.exists()
    assert active_file.exists()
    assert unmanaged_file.exists()


# ---------------------------------------------------------------------------
# 6. END-KEYFRAME MODELING
# ---------------------------------------------------------------------------


def test_end_keyframe_policy_modeling_and_compilation(tmp_path):
    img_path = _create_valid_png(tmp_path / "ref_kf.png")
    img_sha = compute_file_sha256(img_path)

    dossier = ResearchDossier(
        workflow_id="wf_1",
        product_identity=ProductIdentityV2(brand="Xiaomi", model="Desk Lamp", display_name="Xiaomi Desk Lamp Lite"),
        verified_claims=(EvidenceClaim(claim_id="c1", text="Great light", evidence_refs=("ref_1",)),),
        eligible_assets=(
            EligibleAsset(asset_id="a1", path=str(img_path), sha256=img_sha, qc_report_id="qc1"),
            EligibleAsset(asset_id="a2", path=str(img_path), sha256=img_sha, qc_report_id="qc2"),
        ),
    )

    scene_omitted = ScriptSceneV2(
        scene_id="s_omitted",
        order=1,
        story_role="hook",
        duration_seconds=4,
        narration="Hello",
        overlay_text="",
        visual_objective="Show lamp",
        environment="Desk",
        subject_action="Turns on",
        product_interaction="User presses switch",
        start_state="Off",
        end_state="On",
        visual_change="Light turns on",
        camera_framing="Close up",
        camera_movement="Static",
        transition_out="Cut",
        # end_keyframe_policy defaults to "omitted" with default reason
    )
    assert scene_omitted.end_keyframe_policy == "omitted"
    assert scene_omitted.end_keyframe_omission_reason == "continuous_motion_single_shot"

    # Empty omission reason on omitted policy raises ValueError
    with pytest.raises(ValueError, match="end_keyframe_omission_reason is empty"):
        replace(scene_omitted, end_keyframe_omission_reason="")

    scene_generated = replace(
        scene_omitted,
        scene_id="s_generated",
        order=2,
        end_keyframe_policy="generated",
    )

    script = ScriptScenePlanV2(
        target_duration_seconds=8,
        brief_digest="sha256:brief123",
        scenes=(scene_omitted, scene_generated),
    )

    reqs = compile_keyframe_requests(
        workflow_id="wf_1",
        project_id="proj_1",
        dossier=dossier,
        script=script,
    )

    # s_omitted has 1 request (start)
    # s_generated has 2 requests (start and end)
    # Total = 3
    assert len(reqs) == 3
    frame_roles = [r.frame_role for r in reqs]
    assert frame_roles == ["start", "start", "end"]
    assert reqs[2].frame_id == "s_generated-end"


# ---------------------------------------------------------------------------
# 7. OWNER-SCOPED ARTIFACT ACCESS
# ---------------------------------------------------------------------------


def test_artifact_repository_requires_owner_and_prevents_reassignment(tmp_path):
    db_file = tmp_path / "artifacts.sqlite"
    repo = SQLiteVideoFactoryRepository(db_file)

    art = VideoFactoryArtifact(
        artifact_id="art_secure_1",
        owner_user_id="user_alice",
        workflow_id="wf_1",
        project_id="proj_alice",
        kind="clip",
        relative_path="clips/clip1.mp4",
        sha256="abc",
        size=100,
        status="active",
    )
    repo.save_artifact(art)

    # 1. get_artifact requires owner_user_id
    with pytest.raises(ValueError, match="owner_user_id is required"):
        repo.get_artifact("art_secure_1", owner_user_id="")

    # 2. get_artifact for wrong owner returns None
    assert repo.get_artifact("art_secure_1", owner_user_id="user_bob") is None

    # 3. get_artifact for right owner returns artifact
    fetched = repo.get_artifact("art_secure_1", owner_user_id="user_alice")
    assert fetched is not None
    assert fetched.artifact_id == "art_secure_1"

    # 4. list_artifacts requires owner_user_id
    with pytest.raises(ValueError, match="owner_user_id is required"):
        repo.list_artifacts("proj_alice", owner_user_id="")

    # 5. Collision: attempting to save artifact with same ID but different owner raises ValueError
    tampered_art = replace(art, owner_user_id="user_bob")
    with pytest.raises(ValueError, match="Artifact ID collision"):
        repo.save_artifact(tampered_art)
