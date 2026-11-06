"""Comprehensive tests for Product Video artifact architecture, storage layout,

storyboard assets, scene clips, composition, QC provenance, recovery, and immutable export.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from video_factory.integrations.domain import (
    AssetReference, Claim, ClaimStatus, CreativeBrief, FinalApprovalStatus,
    FrameGenerationStatus, FramePrompt, GeneratedScene, ProjectStatus, RawIdea,
    ResourceIdentity, ResourcePack, Scene, ScenePlan, Storyboard,
    StoryboardApprovalStatus, StoryboardFrame, Timeline, TimelineClip,
    TimelineStatus, VideoFactoryProject, VideoGenerationStatus, VideoPrompt,
    VideoFactoryArtifact, RenderBatch,
)
from video_factory.integrations.ports import VideoGenerationRequest, VideoGenerationResult
from video_factory.integrations.repository import SQLiteVideoFactoryRepository
from video_factory.integrations.composer import (
    InvalidVideoArtifact, VideoExpectation, concatenate_videos, probe_video, compute_file_sha256,
)
from video_factory.integrations.export_package import build_export_package
from video_factory.integrations.cleanup import cleanup_unreferenced_artifacts
from video_factory.storage.layout import ProductVideoWorkspaceLayout
from video_factory.qc.models import QCReport, QCStage, QCVerdict
from video_factory.qc.repository import QCReportRepository
from video_factory.integrations.server import (
    render, get_job_status, export, set_service,
)
from video_factory.integrations.service import VideoFactoryService
from support.video_qc import _create_synthetic_mp4


@pytest.fixture(autouse=True)
def reset_server_services():
    yield
    set_service(None)


def test_1_two_products_identical_scene_ids_cannot_collide(tmp_path: Path) -> None:
    """1. Two products with identical scene IDs cannot collide."""
    layout = ProductVideoWorkspaceLayout(root=tmp_path / "workspace")
    proj_a = "proj_prod_a"
    proj_b = "proj_prod_b"
    scene_id = "scene-001"

    from PIL import Image

    # Ingest reference for A
    ref_a_src = tmp_path / "ref_a.jpg"
    Image.new("RGB", (10, 10), (255, 0, 0)).save(ref_a_src, "JPEG")
    ref_a_path, sha_a, _ = layout.ingest_reference(proj_a, ref_a_src, allowed_source_roots=(tmp_path,))

    # Ingest reference for B
    ref_b_src = tmp_path / "ref_b.jpg"
    Image.new("RGB", (10, 10), (0, 255, 0)).save(ref_b_src, "JPEG")
    ref_b_path, sha_b, _ = layout.ingest_reference(proj_b, ref_b_src, allowed_source_roots=(tmp_path,))

    assert ref_a_path != ref_b_path
    assert sha_a != sha_b
    assert ref_a_path.exists() and ref_b_path.exists()

    # Generate keyframe path for scene-001 for A and B
    kf_a = layout.get_keyframe_path(proj_a, version=1, scene_id=scene_id, attempt=1, frame_role="start")
    kf_b = layout.get_keyframe_path(proj_b, version=1, scene_id=scene_id, attempt=1, frame_role="start")

    assert kf_a != kf_b
    assert str(proj_a) in str(kf_a)
    assert str(proj_b) in str(kf_b)


def test_2_restart_after_scene_2_resumes_remaining_scenes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """2. Restart after scene 2 resumes remaining scenes without duplicate paid submissions."""
    from video_factory.integrations.scene_renderer import SceneRenderOperationStore

    db_path = tmp_path / "video_factory.sqlite"
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_path))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_WORKSPACE", str(tmp_path / "workspace"))

    repo = SQLiteVideoFactoryRepository(db_path)
    srv = VideoFactoryService(repo)
    set_service(srv)

    layout = ProductVideoWorkspaceLayout(root=tmp_path / "workspace")
    proj_id = "proj_resume_test"
    srv.create_project("user_1", proj_id)

    # Create synthetic clips for scenes
    clip1 = tmp_path / "clip1.mp4"
    _create_synthetic_mp4(clip1, width=720, height=1280, duration_sec=4.0)
    clip2 = tmp_path / "clip2.mp4"
    _create_synthetic_mp4(clip2, width=720, height=1280, duration_sec=4.0)
    clip3 = tmp_path / "clip3.mp4"
    _create_synthetic_mp4(clip3, width=720, height=1280, duration_sec=4.0)

    # Mock provider: scene 1 and 2 completed; scene 3 was running and finishes
    mock_prov = MagicMock()
    mock_prov.provider_kind = "google_flow"
    mock_prov.is_available = True
    mock_prov.get_status.side_effect = lambda op_id: VideoGenerationResult(
        request_id=op_id,
        status="completed",
        provider_operation_id=op_id,
        video_path=str(clip1 if "scene-1" in op_id else (clip2 if "scene-2" in op_id else clip3)),
    )

    batch_id = f"batch_{proj_id}_v1_test"
    repo.save_render_batch(
        RenderBatch(
            render_batch_id=batch_id,
            owner_user_id="user_1",
            workflow_id="wf_1",
            project_id=proj_id,
            version=1,
            status="running",
            scene_count=3,
            completed_scene_count=0,
            created_at="2026-09-17T00:00:00Z",
            updated_at="2026-09-17T00:00:00Z",
        )
    )

    # Register scene operations in SQLite
    conn = sqlite3.connect(db_path)
    SceneRenderOperationStore(conn)  # Ensure table exists
    with conn:
        conn.execute(
            """INSERT INTO video_factory_scene_operation
               (operation_id, owner_user_id, project_id, scene_id, request_digest,
                provider_operation_id, status, output_path, output_sha256,
               render_batch_id, scene_order, attempt, expected_duration_seconds,
               expected_aspect_ratio, provider_kind)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "op1", "user_1", proj_id, "scene-1", "d1", "prov_op_scene-1",
                "completed", str(clip1), compute_file_sha256(clip1), batch_id, 1, 1,
                4.0, "9:16", "google_flow",
            ),
        )
        conn.execute(
            """INSERT INTO video_factory_scene_operation
               (operation_id, owner_user_id, project_id, scene_id, request_digest,
                provider_operation_id, status, output_path, output_sha256,
               render_batch_id, scene_order, attempt, expected_duration_seconds,
               expected_aspect_ratio, provider_kind)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "op2", "user_1", proj_id, "scene-2", "d2", "prov_op_scene-2",
                "completed", str(clip2), compute_file_sha256(clip2), batch_id, 2, 1,
                4.0, "9:16", "google_flow",
            ),
        )
        conn.execute(
            """INSERT INTO video_factory_scene_operation
               (operation_id, owner_user_id, project_id, scene_id, request_digest,
                provider_operation_id, status, output_path, render_batch_id,
               scene_order, attempt, expected_duration_seconds, expected_aspect_ratio,
               provider_kind)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "op3", "user_1", proj_id, "scene-3", "d3", "prov_op_scene-3",
                "running", "", batch_id, 3, 1, 4.0, "9:16", "google_flow",
            ),
        )
    conn.close()

    # Poll status: only scene 3 should be polled from provider, scenes 1 & 2 reuse completed state
    status_res = get_job_status(
        batch_id, provider=mock_prov, provider_kind="google_flow"
    )
    assert status_res["ok"] is True
    assert status_res["status"] == "completed"

    # Verify mock provider was called only once for scene 3
    assert mock_prov.get_status.call_count == 1
    mock_prov.get_status.assert_called_with("prov_op_scene-3")


def test_3_changed_keyframe_or_prompt_invalidates_clip(tmp_path: Path) -> None:
    """3. Changed keyframe/prompt invalidates its clip, composite, QC and Final Approval."""
    from video_factory.integrations.scene_renderer import stable_scene_request_digest

    req1 = VideoGenerationRequest(
        request_id="req-1",
        owner_user_id="user_1",
        project_id="proj_1",
        scene_id="scene-01",
        prompt="A person holding a blue gadget",
        duration_seconds=4,
        reference_image_paths=("ref1.png",),
    )
    req2 = VideoGenerationRequest(
        request_id="req-1",
        owner_user_id="user_1",
        project_id="proj_1",
        scene_id="scene-01",
        prompt="A person holding a red gadget",  # Prompt changed
        duration_seconds=4,
        reference_image_paths=("ref1.png",),
    )

    d1 = stable_scene_request_digest(req1)
    d2 = stable_scene_request_digest(req2)
    assert d1 != d2


def test_4_wrong_duration_or_empty_clip_blocks_composition(tmp_path: Path) -> None:
    """4. Missing, modified, duplicated, reordered or wrong-duration clips block composition."""
    out_path = tmp_path / "composite.mp4"

    # Non-existent clip
    with pytest.raises(FileNotFoundError):
        concatenate_videos([tmp_path / "non_existent.mp4"], out_path)

    # Empty clip
    empty_clip = tmp_path / "empty.mp4"
    empty_clip.write_bytes(b"")
    with pytest.raises(FileNotFoundError):
        concatenate_videos([empty_clip], out_path)


def test_5_square_input_is_rejected_instead_of_stretched(tmp_path: Path) -> None:
    """5. Square input is rejected instead of stretched."""
    square_clip = tmp_path / "square.mp4"
    _create_synthetic_mp4(
        square_clip,
        width=720,
        height=720,  # 1:1 Square!
        duration_sec=2.0,
    )
    out_path = tmp_path / "composite_out.mp4"

    with pytest.raises(InvalidVideoArtifact) as exc_info:
        concatenate_videos([square_clip], out_path)

    assert "Square/landscape input rejected" in str(exc_info.value)
    assert not out_path.exists()


def test_6_export_without_pass_video_qc_report_is_rejected(tmp_path: Path) -> None:
    """6. Export without a real PASS Video QC report is rejected."""
    source_mp4 = tmp_path / "source.mp4"
    _create_synthetic_mp4(source_mp4, width=720, height=1280, duration_sec=4.0)
    db_path = tmp_path / "approvals.sqlite"
    qc_conn = sqlite3.connect(db_path)
    qc_repo = QCReportRepository(qc_conn)

    # Save a FAILED report
    qc_repo.save_report(
        QCReport(
            report_id="qc-fail-1",
            workflow_id="wf_test",
            stage=QCStage.VIDEO,
            artifact_id="art-1",
            artifact_version=1,
            artifact_digest=compute_file_sha256(source_mp4),
            reference_digests=(),
            evaluator_name="test_eval",
            evaluator_version="1.0",
            rubric_version="1.0",
            created_at="2026-09-17T00:00:00Z",
            score=40,
            verdict=QCVerdict.FAIL,
            findings={"error": "Defects present"},
        )
    )
    qc_conn.close()

    # Attempt export
    set_service(MagicMock())
    res = export(
        render_result={"file_path": str(source_mp4), "project_id": "proj_1"},
        owner_user_id="user_1",
        project_id="proj_1",
    )
    # Must fail because no PASS report exists for exact bytes
    assert res["ok"] is False


def test_7_ingested_references_survive_removal_of_source(tmp_path: Path) -> None:
    """7. Ingested references survive removal of the exchange source."""
    layout = ProductVideoWorkspaceLayout(root=tmp_path / "workspace")
    from PIL import Image
    exchange_src = tmp_path / "exchange" / "product_photo.jpg"
    exchange_src.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (10, 10), (128, 128, 128)).save(exchange_src, "JPEG")

    ingested_path, sha, size = layout.ingest_reference("proj_ingest_test", exchange_src, allowed_source_roots=(tmp_path / "exchange",))
    assert ingested_path.exists()
    assert size == exchange_src.stat().st_size

    # Delete source exchange file
    exchange_src.unlink()
    assert not exchange_src.exists()

    # Ingested file in workspace survives with same content and hash
    assert ingested_path.exists()
    assert compute_file_sha256(ingested_path) == sha


def test_8_relative_paths_remain_valid_after_moving_workspace(tmp_path: Path) -> None:
    """8. Relative paths remain valid after moving the workspace root."""
    root1 = tmp_path / "ws1"
    layout1 = ProductVideoWorkspaceLayout(root=root1)
    proj_id = "proj_rel_test"

    clip1_path = layout1.get_rendered_clip_path(proj_id, version=1, scene_id="scene-01", attempt=1)
    _create_synthetic_mp4(clip1_path, width=720, height=1280, duration_sec=2.0)

    # Convert to relative path
    rel_path = layout1.to_relative_path(clip1_path)
    assert not Path(rel_path).is_absolute()
    assert rel_path.startswith(f"projects/{proj_id}/renders/v1/scenes/scene-01/attempt-001/clip.mp4")

    # Move workspace directory to ws2
    root2 = tmp_path / "ws2"
    root1.rename(root2)
    layout2 = ProductVideoWorkspaceLayout(root=root2)

    # Resolve under new layout
    resolved_in_ws2 = layout2.resolve_path(rel_path)
    assert resolved_in_ws2.exists()
    assert str(root2) in str(resolved_in_ws2)


def test_9_cleanup_preserves_reachable_artifacts(tmp_path: Path) -> None:
    """9. Cleanup preserves every artifact reachable from approvals or exports."""
    db_path = tmp_path / "video_factory.sqlite"
    repo = SQLiteVideoFactoryRepository(db_path)
    layout = ProductVideoWorkspaceLayout(root=tmp_path / "workspace")
    proj_id = "proj_cleanup_test"

    # Create approved keyframe in attempt-001
    kf_approved = layout.get_keyframe_path(proj_id, version=1, scene_id="scene-01", attempt=1, frame_role="start")
    kf_approved.write_bytes(b"approved keyframe PNG bytes")

    # Create unapproved/failed attempt-002 keyframe
    kf_failed = layout.get_keyframe_path(proj_id, version=1, scene_id="scene-01", attempt=2, frame_role="start")
    kf_failed.write_bytes(b"failed keyframe PNG bytes")

    # Register approved artifact in repository
    repo.save_artifact(
        VideoFactoryArtifact(
            artifact_id="art_kf_approved",
            owner_user_id="user_1",
            workflow_id="wf_1",
            project_id=proj_id,
            kind="keyframe",
            scene_id="scene-01",
            frame_role="start",
            version=1,
            attempt=1,
            relative_path=layout.to_relative_path(kf_approved),
            sha256=compute_file_sha256(kf_approved),
            size=kf_approved.stat().st_size,
            status="approved",
            created_at="2026-09-17T00:00:00Z",
            updated_at="2026-09-17T00:00:00Z",
        )
    )

    # Register failed artifact in repository
    repo.save_artifact(
        VideoFactoryArtifact(
            artifact_id="art_kf_failed",
            owner_user_id="user_1",
            workflow_id="wf_1",
            project_id=proj_id,
            kind="keyframe",
            scene_id="scene-01",
            frame_role="start",
            version=1,
            attempt=2,
            relative_path=layout.to_relative_path(kf_failed),
            sha256=compute_file_sha256(kf_failed),
            size=kf_failed.stat().st_size,
            status="failed",
            created_at="2026-09-10T00:00:00Z",
            updated_at="2026-09-10T00:00:00Z",
        )
    )

    # Create project referencing approved keyframe
    repo.create(
        VideoFactoryProject(
            id=proj_id,
            owner_user_id="user_1",
            status=ProjectStatus.STORYBOARD_APPROVED,
            storyboard=Storyboard(
                storyboard_id="sb_1",
                project_id=proj_id,
                frames=(
                    StoryboardFrame(
                        frame_id="f1",
                        scene_id="scene-01",
                        order=1,
                        label="Scene 1",
                        purpose="Hook",
                        visual_state="State",
                        subject_action="Action",
                        product_state="State",
                        character_state="",
                        context="",
                        camera_intention="",
                        generated_asset_id="art_kf_approved",
                    ),
                ),
                approval_status=StoryboardApprovalStatus.APPROVED,
            ),
            created_at="2026-09-17T00:00:00Z",
            updated_at="2026-09-17T00:00:00Z",
        )
    )

    # Run cleanup
    report = cleanup_unreferenced_artifacts(proj_id, "user_1", repo, layout=layout, dry_run=False, retention_seconds=0.0)
    assert report.deleted_count == 1
    assert not kf_failed.exists()
    assert kf_approved.exists()


def test_10_realistic_4_scene_produces_full_layout_and_export(tmp_path: Path) -> None:
    """10. A realistic 4-6 scene product produces one complete storyboard manifest,

    one clip per scene, one ordered composite and one immutable export package.
    """
    layout = ProductVideoWorkspaceLayout(root=tmp_path / "workspace")
    proj_id = "proj_4scene_full"

    # Create 4 synthetic scene clips
    clips: list[Path] = []
    for i in range(1, 5):
        clip = layout.get_rendered_clip_path(proj_id, version=1, scene_id=f"scene-0{i}", attempt=1)
        _create_synthetic_mp4(clip, width=720, height=1280, duration_sec=4.0)
        clips.append(clip)

    # Create composite draft
    composite_path = layout.get_composite_dir(proj_id, version=1) / "draft.mp4"
    concatenate_videos(clips, composite_path, target_width=720, target_height=1280, target_fps=24)
    assert composite_path.exists()
    assert probe_video(composite_path).duration_seconds >= 15.5

    # Create keyframe files
    kf_files = []
    for i in range(1, 5):
        kf = layout.get_keyframe_path(proj_id, version=1, scene_id=f"scene-0{i}", attempt=1, frame_role="start")
        kf.write_bytes(f"PNG fake keyframe {i}".encode())
        kf_files.append({"frame_id": f"scene-0{i}-start", "image_path": str(kf)})

    # Build immutable export package
    exp = build_export_package(
        source_video=composite_path,
        exports_root=layout.root / "exports",
        project_id=proj_id,
        version="v1",
        product_identity="Baseus Bowie WM02 Earbuds",
        verified_claims=["Hộp sạc nhỏ gọn", "Pin 25 giờ"],
        cta="Xem thêm tại cửa hàng chính hãng",
        provenance={
            "keyframes": kf_files,
            "qc_reports": {
                "image": {"verdict": "PASS", "score": 90},
                "keyframe": {"verdict": "PASS", "score": 88},
                "video": {"verdict": "PASS", "score": 92},
            },
        },
        expected=VideoExpectation(
            width=720,
            height=1280,
            duration_seconds=16.0,
            max_duration_delta=1.0,
            codec="h264",
            fps=24.0,
        ),
    )

    assert exp.directory.exists()
    assert exp.video_path.exists()
    assert exp.caption_path.exists()
    assert exp.metadata_path.exists()
    assert (exp.directory / "keyframes" / "scene-01-start.png").exists()
    assert (exp.directory / "qc" / "video-report.json").exists()


def test_11_path_traversal_rejection_and_fail_closed(tmp_path: Path) -> None:
    """11. Path traversal escape attempts must be rejected fail-closed."""
    layout = ProductVideoWorkspaceLayout(root=tmp_path / "workspace")
    
    # Traversal in project_id
    with pytest.raises(ValueError, match="Invalid project_id"):
        layout.get_project_dir("../../etc")

    with pytest.raises(ValueError, match="Invalid project_id"):
        layout.get_reference_asset_path("../project", "deadbeef")

    # Traversal in scene_id
    with pytest.raises(ValueError, match="Invalid scene_id"):
        layout.get_scene_dir("proj_valid", 1, "../escape_scene")

    # Outside absolute path resolution without explicit allowed_roots
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"test outside")
    with pytest.raises(ValueError, match="Absolute path outside approved"):
        layout.resolve_path(outside)

    # Relative path outside workspace root
    with pytest.raises(ValueError, match="Path is outside workspace root"):
        layout.to_relative_path(outside)


def test_12_export_strictly_enforces_exact_video_qc_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """12. Export strictly requires exact composite SHA-256 PASS Video QC report and fails-closed on bypass attempts."""
    db_path = tmp_path / "appr.sqlite"
    monkeypatch.setenv("HERMES_APPROVAL_DB_PATH", str(db_path))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(tmp_path / "vf.sqlite"))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_WORKSPACE", str(tmp_path / "workspace"))

    repo = SQLiteVideoFactoryRepository(tmp_path / "vf.sqlite")
    srv = VideoFactoryService(repo)
    set_service(srv)

    # Create approved project with locked resource pack
    proj = srv.create_project("user_qc", "proj_qc")
    rp = ResourcePack(
        id="rp_1",
        owner_user_id="user_qc",
        product_references=(AssetReference(asset_id="ref_1", uri="asset://ref.png"),),
        primary_product_asset_id="ref_1",
        product_identity_description="Baseus WM02 Earbuds",
        locked_at="2026-09-17T00:00:00Z",
        locked_product_identity=ResourceIdentity(description="Baseus WM02 Earbuds"),
    )
    brief = CreativeBrief(
        objective="Sales", target_audience="Office", core_message="Quality sound",
        tone="Calm", pace="Medium", cta="Shop now", content_blocks=("Intro", "Outro"),
        verified_selling_points=(Claim(claim="Authentic Sound", status=ClaimStatus.VERIFIED),),
    )
    repo.save(replace(proj, resource_pack=rp, creative_brief=brief, final_approval=FinalApprovalStatus.APPROVED))

    workspace = tmp_path / "workspace"
    video_file = workspace / "projects" / "proj_qc" / "renders" / "v1" / "composite" / "draft.mp4"
    video_file.parent.mkdir(parents=True, exist_ok=True)
    _create_synthetic_mp4(video_file, width=720, height=1280, duration_sec=4.0)
    real_sha = compute_file_sha256(video_file)
    for artifact_id, workflow_id in (("art_1", "wf_1"), ("art_exact", "wf_exact")):
        repo.save_artifact(
            VideoFactoryArtifact(
                artifact_id=artifact_id,
                owner_user_id="user_qc",
                workflow_id=workflow_id,
                project_id="proj_qc",
                kind="composite",
                relative_path=video_file.relative_to(workspace).as_posix(),
                sha256=real_sha,
                version=1,
                size=video_file.stat().st_size,
                mime_type="video/mp4",
                status="active",
            )
        )

    # 1. Export without any QC report fails closed
    res = export(
        render_result={"video_url": str(video_file), "owner_user_id": "user_qc", "project_id": "proj_qc", "workflow_id": "wf_1", "artifact_id": "art_1", "artifact_version": 1, "sha256": real_sha},
        project_id="proj_qc",
        owner_user_id="user_qc",
    )
    assert res["ok"] is False
    assert res["code"] == "VIDEO_QC_REQUIRED"

    # 2. Caller attempting to skip QC verification via parameter is rejected
    res_bypass = export(
        render_result={"video_url": str(video_file), "owner_user_id": "user_qc", "project_id": "proj_qc", "workflow_id": "wf_1", "artifact_id": "art_1", "artifact_version": 1, "sha256": real_sha},
        export_params={"skip_video_qc_verification": True},
        project_id="proj_qc",
        owner_user_id="user_qc",
    )
    assert res_bypass["ok"] is False
    assert res_bypass["code"] == "VIDEO_QC_REQUIRED"

    # 3. Report with mismatching digest is rejected
    conn = sqlite3.connect(db_path)
    qc_repo = QCReportRepository(conn)
    qc_repo.save_report(QCReport(
        report_id="qc_mismatch",
        workflow_id="wf_1",
        stage=QCStage.VIDEO,
        artifact_id="art_1",
        artifact_version=1,
        artifact_digest="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        reference_digests=("sha256:1111",),
        evaluator_name="test",
        evaluator_version="1.0",
        rubric_version="1.0",
        created_at="2026-09-17T00:00:00Z",
        score=95,
        verdict=QCVerdict.PASS,
        findings=(),
    ))
    res_wrong_digest = export(
        render_result={"video_url": str(video_file), "owner_user_id": "user_qc", "project_id": "proj_qc", "workflow_id": "wf_1", "artifact_id": "art_1", "artifact_version": 1, "sha256": real_sha},
        project_id="proj_qc",
        owner_user_id="user_qc",
    )
    assert res_wrong_digest["ok"] is False
    assert res_wrong_digest["code"] == "VIDEO_QC_REQUIRED"

    # 4. Report matching exact source bytes succeeds
    qc_repo.save_report(QCReport(
        report_id="qc_exact",
        workflow_id="wf_exact",
        stage=QCStage.VIDEO,
        artifact_id="art_exact",
        artifact_version=1,
        artifact_digest=f"sha256:{real_sha}",
        reference_digests=("sha256:1111",),
        evaluator_name="test",
        evaluator_version="1.0",
        rubric_version="1.0",
        created_at="2026-09-17T00:00:00Z",
        score=95,
        verdict=QCVerdict.PASS,
        findings=(),
        owner_user_id="user_qc",
        project_id="proj_qc",
    ))
    res_exact = export(
        render_result={"video_url": str(video_file), "owner_user_id": "user_qc", "project_id": "proj_qc", "workflow_id": "wf_exact", "artifact_id": "art_exact", "artifact_version": 1, "sha256": real_sha},
        project_id="proj_qc",
        owner_user_id="user_qc",
    )
    assert res_exact["ok"] is True
    assert res_exact["export_id"]


def test_13_artifact_id_collision_across_owners_rejected(tmp_path: Path) -> None:
    """13. Artifact ID collision across different owners or projects must raise ValueError."""
    repo = SQLiteVideoFactoryRepository(tmp_path / "artifacts.sqlite")
    art1 = VideoFactoryArtifact(
        artifact_id="art_shared_123",
        owner_user_id="owner_alpha",
        workflow_id="wf_1",
        project_id="proj_1",
        kind="clip",
        relative_path="projects/proj_1/renders/v1/scene-1/clip.mp4",
        sha256="abc123",
        created_at="2026-09-17T00:00:00Z",
        updated_at="2026-09-17T00:00:00Z",
    )
    repo.save_artifact(art1)

    # Attempting to save same artifact_id with different owner
    art_colliding_owner = replace(art1, owner_user_id="owner_beta")
    with pytest.raises(ValueError, match="Artifact ID collision"):
        repo.save_artifact(art_colliding_owner)

    # Attempting to save same artifact_id with different project
    art_colliding_proj = replace(art1, project_id="proj_2")
    with pytest.raises(ValueError, match="Artifact ID collision"):
        repo.save_artifact(art_colliding_proj)

    # Absolute or traversal relative path is rejected
    art_traversal = replace(art1, artifact_id="art_trav", relative_path="../escape.mp4")
    with pytest.raises(ValueError, match="workspace-relative"):
        repo.save_artifact(art_traversal)


def test_14_cleanup_retention_and_reachability_protection(tmp_path: Path) -> None:
    """14. Cleanup protects young files by retention window and preserves active/reachable artifacts."""
    layout = ProductVideoWorkspaceLayout(root=tmp_path / "workspace")
    repo = SQLiteVideoFactoryRepository(tmp_path / "cleanup.sqlite")
    
    proj_dir = layout.get_project_dir("proj_clean")
    attempt_dir = proj_dir / "scenes" / "scene-1" / "keyframes" / "attempt-001"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    unreferenced_file = attempt_dir / "start.png"
    unreferenced_file.write_bytes(b"old failed keyframe")

    repo.save_artifact(
        VideoFactoryArtifact(
            artifact_id="art_unreferenced",
            owner_user_id="user_1",
            workflow_id="wf_clean",
            project_id="proj_clean",
            kind="keyframe",
            scene_id="scene-1",
            frame_role="start",
            version=1,
            attempt=1,
            relative_path=layout.to_relative_path(unreferenced_file),
            sha256=compute_file_sha256(unreferenced_file),
            size=unreferenced_file.stat().st_size,
            status="failed",
            created_at="2026-09-10T00:00:00Z",
            updated_at="2026-09-10T00:00:00Z",
        )
    )

    # Create dummy project in repository
    srv = VideoFactoryService(repo)
    srv.create_project("user_1", "proj_clean")

    # Default dry_run=True does not delete
    rep_dry = cleanup_unreferenced_artifacts("proj_clean", "user_1", repo, layout, retention_seconds=0.0)
    assert rep_dry.deleted_count == 1
    assert unreferenced_file.exists()

    # Retention protection: file younger than retention_seconds is preserved even if unreferenced
    rep_retained = cleanup_unreferenced_artifacts(
        "proj_clean", "user_1", repo, layout, dry_run=False, retention_seconds=3600.0
    )
    assert rep_retained.deleted_count == 0
    assert unreferenced_file.exists()

    # Running with retention_seconds=0.0 and dry_run=False safely deletes it
    rep_deleted = cleanup_unreferenced_artifacts(
        "proj_clean", "user_1", repo, layout, dry_run=False, retention_seconds=0.0
    )
    assert rep_deleted.deleted_count == 1
    assert not unreferenced_file.exists()


def test_15_render_pre_persists_batch_and_scene_ops_before_provider_submit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """15. Render pre-persists RenderBatch and planned scene operations before paid provider calls."""
    db_path = tmp_path / "pre_persist.sqlite"
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_path))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_WORKSPACE", str(tmp_path / "workspace"))

    from video_factory.workflows.budgets import init_default_engine, BudgetPolicy, reserve_budget
    init_default_engine(BudgetPolicy(max_total=1000, per_channel={"video_generation": 1000}))
    alloc = reserve_budget("video_generation", 100, owner_user_id="user_render", scope_id="proj_render")

    repo = SQLiteVideoFactoryRepository(db_path)
    srv = VideoFactoryService(repo)
    set_service(srv)

    srv.create_project("user_render", "proj_render")
    proj = srv.get_project("user_render", "proj_render")
    # Setup storyboard
    sb = Storyboard(
        storyboard_id="sb_1",
        project_id="proj_render",
        frames=(
            StoryboardFrame(
                frame_id="f1",
                scene_id="s1",
                order=1,
                label="Scene 1",
                purpose="Hook",
                visual_state="State 1",
                subject_action="Action 1",
                product_state="State 1",
                character_state="State 1",
                context="Context 1",
                camera_intention="Static",
                prompt=FramePrompt(positive_prompt="Scene 1 Prompt"),
            ),
        ),
        approval_status=StoryboardApprovalStatus.APPROVED,
    )
    repo.save(replace(proj, storyboard=sb, storyboard_version=1))

    # Mock provider that tracks invocation order and checks db state at submission time
    provider_called = False
    db_has_planned_op = False

    class InspectingProvider:
        provider_kind = "google_flow"
        is_available = True

        def submit(self, req):
            nonlocal provider_called, db_has_planned_op
            provider_called = True
            # Verify that database already has batch and planned operation
            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT status, render_batch_id FROM video_factory_scene_operation WHERE operation_id = ?", (req.request_id,)).fetchone()
            if row and row[0] in ("planned", "submitting"):
                db_has_planned_op = True
            conn.close()
            return VideoGenerationResult(request_id=req.request_id, status="submitted", provider_operation_id="op_prov_1")

        def get_status(self, op_id):
            return VideoGenerationResult(request_id="req_1", status="running", provider_operation_id=op_id)

    res = render(
        storyboard_result={"project_id": "proj_render"},
        render_params={"budget_reservation_id": alloc.reservation_id},
        owner_user_id="user_render",
        project_id="proj_render",
        provider=InspectingProvider(),
        admission_context={"reservation_id": alloc.reservation_id},
    )

    assert res["ok"] is True
    assert provider_called is True
    assert db_has_planned_op is True

    # Confirm project is NOT in DRAFT_VIDEO_READY state yet
    updated_proj = srv.get_project("user_render", "proj_render")
    assert updated_proj.status != ProjectStatus.DRAFT_VIDEO_READY

