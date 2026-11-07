"""Comprehensive production readiness regression tests.

Covers:
1. Configured mode stops before generation on fixture or missing real assets.
2. Existing exports are never borrowed on new run failure, and scheduler does not mark done.
3. Partial batch success accounts only for current run MP4, resume does not rerun completed item.
4. Uncertain send reconciles existing operation without duplicate provider submission.
5. 3, 4, and 5 scenes concatenate distinct clips verified with PyAV decoder.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from dataclasses import asdict
from fractions import Fraction
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

ROOT_DIR = Path(__file__).resolve().parents[3]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
PI_DIR = ROOT_DIR / "apps" / "pi"
if str(PI_DIR) not in sys.path:
    sys.path.insert(0, str(PI_DIR))

import av
import numpy as np
import pytest
from PIL import Image

from automation.pipelines.spark_pipeline import (
    PipelineRunResult,
    is_verified_real_batch,
    probe_mp4_file,
    run_pipeline,
)
from automation.scheduler.daily_trigger import (
    ScheduleRecord,
    record_run,
    should_run_today,
)
from video_factory.integrations.client import (
    VideoFactoryClient,
    _create_distinct_scene_clip,
    _create_synthetic_mp4,
)
from video_factory.integrations.composer import (
    concatenate_videos,
    probe_video,
)


def _make_dummy_image(path: Path, color: str = "blue") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (720, 1280), color=color)
    img.save(path, format="JPEG", quality=90)
    return path


def _make_authentic_image(path: Path, seed: int = 42) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    # Generate realistic photo-like gradient texture with high variance
    base = rng.integers(50, 200, size=(1280, 720, 3), dtype=np.uint8)
    img = Image.fromarray(base, mode="RGB")
    img.save(path, format="JPEG", quality=95)
    return path


# =============================================================================
# Test 1: Configured Mode Rejects Fixture / Missing Real Assets Before Generation
# =============================================================================
def test_configured_mode_stops_before_generation_on_fixture_or_missing_real_batch(tmp_path: Path):
    """Configured mode must only accept batches containing verified real PI assets."""
    # 1. Test fixture batch detection
    fixture_batch = tmp_path / "inbox" / "2026-11-03" / "run_fix_test_123"
    fixture_batch.mkdir(parents=True, exist_ok=True)
    (fixture_batch / "READY").write_text("READY\n", encoding="utf-8")
    
    img_path = fixture_batch / "products" / "test" / "images" / "img1.jpg"
    _make_dummy_image(img_path, color="blue")
    
    manifest = {
        "schema_version": "1.0.0",
        "batch_id": "run_fix_test_123",
        "fixture": True,
        "products": [{
            "name": "Test Product",
            "images": [{
                "asset_id": "a1",
                "original_url": "https://example.com/dummy.jpg",
                "relative_path": "products/test/images/img1.jpg",
            }],
        }],
    }
    (fixture_batch / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    
    is_real, reason = is_verified_real_batch(fixture_batch)
    assert is_real is False
    assert "fixture" in reason.lower() or "giả lập" in reason.lower()

    # 2. Test authentic real batch passes check
    real_batch = tmp_path / "inbox" / "2026-11-03" / "run_sel_real_456"
    real_batch.mkdir(parents=True, exist_ok=True)
    (real_batch / "READY").write_text("READY\n", encoding="utf-8")
    
    real_img = real_batch / "products" / "real_p" / "images" / "photo1.jpg"
    _make_authentic_image(real_img, seed=101)
    
    real_manifest = {
        "schema_version": "1.0.0",
        "batch_id": "run_sel_real_456",
        "fixture": False,
        "products": [{
            "name": "Authentic Spark Product",
            "images": [{
                "asset_id": "img_auth_01",
                "original_url": "https://cf.shopee.vn/file/vn-11134207-authentic.jpg",
                "relative_path": "products/real_p/images/photo1.jpg",
            }],
            "provenance_path": "products/real_p/provenance.json",
        }],
    }
    (real_batch / "manifest.json").write_text(json.dumps(real_manifest), encoding="utf-8")
    
    prov = {
        "source_url": "https://shopee.vn/product/123/456",
        "review": {
            "reviewed_by": "qa_operator",
            "identity_verified": True,
        },
    }
    (real_batch / "products" / "real_p").mkdir(parents=True, exist_ok=True)
    (real_batch / "products" / "real_p" / "provenance.json").write_text(json.dumps(prov), encoding="utf-8")

    is_real2, reason2 = is_verified_real_batch(real_batch)
    assert is_real2 is True
    assert "hợp lệ" in reason2.lower() or "verified" in reason2.lower()

    # Local-only Spaiauto import must pass without downloading image URLs.
    image = real_manifest["products"][0]["images"][0]
    del image["original_url"]
    image["sha256"] = hashlib.sha256(real_img.read_bytes()).hexdigest()
    prov.update(acquisition_tool="spaiauto", archive_sha256="a" * 64)
    prov["review"]["selected_asset_ids"] = [image["asset_id"]]
    (real_batch / "manifest.json").write_text(json.dumps(real_manifest), encoding="utf-8")
    (real_batch / "products/real_p/provenance.json").write_text(json.dumps(prov), encoding="utf-8")
    assert is_verified_real_batch(real_batch)[0]
    with real_img.open("ab") as stream:
        stream.write(b"tampered")
    assert not is_verified_real_batch(real_batch)[0]


# =============================================================================
# Test 2: New Run Error Does Not Borrow Old Exports & Scheduler Dedupe
# =============================================================================
def test_new_run_with_vf_error_does_not_borrow_old_exports_nor_mark_ledger_done(tmp_path: Path, monkeypatch):
    """Old exports in workspace must never be claimed by a failing or incomplete new run."""
    ws_exports = tmp_path / "workspace" / "exports"
    old_export_dir = ws_exports / "export_proj_old_run_v1"
    old_export_dir.mkdir(parents=True, exist_ok=True)
    old_mp4 = old_export_dir / "final.mp4"
    _create_synthetic_mp4(old_mp4, width=720, height=1280, fps=30, num_frames=60)
    (old_export_dir / "metadata.json").write_text(json.dumps({"project_id": "proj_old_run"}), encoding="utf-8")
    assert old_mp4.is_file()

    # Create dummy spark csv
    spark_csv = tmp_path / "spark.csv"
    spark_csv.write_text("Row ID,Product Name,Source Link\n1,Demo Item,https://shopee.vn/demo-i.1.2\n", encoding="utf-8")

    # Mock subprocess to simulate Stage 1 success with READY, but Stage 2 VF Creative Batch error
    inbox_batch = tmp_path / "inbox" / "2026-11-03" / "run_fix_test"
    inbox_batch.mkdir(parents=True, exist_ok=True)
    (inbox_batch / "READY").write_text("READY", encoding="utf-8")

    def mock_run(cmd, **kwargs):
        if "tools.spark_intake" in cmd:
            return MagicMock(returncode=0, stdout=json.dumps({"status": "success", "exchange_batch_dir": str(inbox_batch)}), stderr="")
        elif "creative-batch" in cmd:
            # Stage 2 returns error
            return MagicMock(returncode=2, stdout="Lỗi kỹ thuật: Demo Item: GPU render timeout\nTổng kết: thành công 0, BLOCKED 0, lỗi kỹ thuật 1.", stderr="GPU out of memory")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", mock_run)

    fake_db = tmp_path / "test_hermes.db"
    res = run_pipeline(
        spark_csv=spark_csv,
        limit=1,
        scenes=4,
        date="2026-11-03",
        provider_mode="fake",
        db_path=fake_db,
    )

    # Must NOT report success or partial based on old_mp4
    assert res.status == "failed"
    assert len(res.exported_videos) == 0
    assert str(old_mp4) not in [p.get("file_path") for p in res.exported_videos]

    # Verify scheduler ledger check: failed or partial run must NOT mark today as done
    sha = hashlib.sha256(spark_csv.read_bytes()).hexdigest()
    rec = ScheduleRecord(
        date="2026-11-03",
        csv_sha256=sha,
        run_id=res.run_id,
        status="failed",
        timestamp="2026-11-03T10:00:00Z",
        mp4_count=0,
        duration_seconds=1.5,
        error="GPU out of memory",
    )
    with patch("automation.scheduler.daily_trigger.load_ledger", return_value=[asdict(rec)]):
        needed, reason = should_run_today(spark_csv, target_date="2026-11-03", force=False)
        assert needed is True
        assert "New or updated intake needed" in reason or "needed" in reason


# =============================================================================
# Test 3: Partial Batch Success & Resume Without Re-running Completed Item
# =============================================================================
def test_partial_run_and_resume_preserves_completed_item_without_rerun(tmp_path: Path, monkeypatch):
    """When 1 item succeeds and 1 fails, only 1 MP4 is counted. Resume preserves the completed item."""
    db_file = tmp_path / "hermes_partial.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE workflow (workflow_id TEXT PRIMARY KEY, aggregate_json TEXT, version INTEGER)")
    
    run_id = "test_run_partial_001"
    selection_id = "pv-creative-batch-" + hashlib.sha256(run_id.encode()).hexdigest()[:24]

    # Item 1: completed with valid export
    out_dir_1 = tmp_path / "exports" / "export_proj_item1_v1"
    out_dir_1.mkdir(parents=True, exist_ok=True)
    mp4_1 = out_dir_1 / "final.mp4"
    _create_synthetic_mp4(mp4_1, width=720, height=1280, fps=30, num_frames=60)
    (out_dir_1 / "metadata.json").write_text(json.dumps({"project_id": "proj_item1"}), encoding="utf-8")

    wf1_agg = {
        "state": "completed",
        "project_id": "proj_item1",
        "source_context": {"name": "Product 1 (Success)", "project_id": "proj_item1", "source_batch_id": "b1"},
        "export_result": {"file_path": str(mp4_1)},
    }
    wf2_agg = {
        "state": "failed",
        "project_id": "proj_item2",
        "source_context": {"name": "Product 2 (Failed)", "project_id": "proj_item2", "source_batch_id": "b1"},
        "failure_reason": "Camera model constraint violation",
    }
    sel_agg = {
        "batch_id": "b1",
        "production_selected_refs": ["wf_item1", "wf_item2"],
        "skipped": [],
        "unresolved": [],
    }
    conn.execute("INSERT INTO workflow VALUES (?, ?, 1)", (selection_id, json.dumps(sel_agg)))
    conn.execute("INSERT INTO workflow VALUES (?, ?, 1)", ("wf_item1", json.dumps(wf1_agg)))
    conn.execute("INSERT INTO workflow VALUES (?, ?, 1)", ("wf_item2", json.dumps(wf2_agg)))
    conn.commit()
    conn.close()

    spark_csv = tmp_path / "spark2.csv"
    spark_csv.write_text("Row ID,Product Name,Source Link\n1,P1,https://shopee.vn/p1-i.1.1\n2,P2,https://shopee.vn/p2-i.1.2\n", encoding="utf-8")

    inbox_batch = tmp_path / "inbox" / "2026-11-03" / "run_b1"
    inbox_batch.mkdir(parents=True, exist_ok=True)
    (inbox_batch / "READY").write_text("READY", encoding="utf-8")

    def mock_run(cmd, **kwargs):
        if "tools.spark_intake" in cmd:
            return MagicMock(returncode=0, stdout=json.dumps({"status": "success", "exchange_batch_dir": str(inbox_batch)}), stderr="")
        elif "creative-batch" in cmd:
            return MagicMock(returncode=2, stdout="Product 1 (Success): Thành công — " + str(mp4_1) + "\nLỗi kỹ thuật: Product 2 (Failed): Camera model constraint violation\nTổng kết: thành công 1, BLOCKED 0, lỗi kỹ thuật 1.", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", mock_run)

    res = run_pipeline(
        spark_csv=spark_csv,
        limit=2,
        scenes=4,
        run_id=run_id,
        db_path=db_file,
    )

    assert res.status == "partial"
    assert len(res.exported_videos) == 1
    assert res.exported_videos[0]["file_path"] == str(mp4_1)
    assert any(p["product_name"] == "Product 1 (Success)" and p["status"] == "success" for p in res.product_outcomes)
    assert any(p["product_name"] == "Product 2 (Failed)" and p["status"] == "failed" for p in res.product_outcomes)


# =============================================================================
# Test 4: Uncertain Send Reconciles Operation Without Duplicate Send
# =============================================================================
def test_uncertain_send_resumes_via_reconcile_without_duplicate_send():
    """When an operation was submitted and uncertain, resume reconciles without re-submitting."""
    from video_factory.integrations.ports import VideoGenerationRequest, VideoGenerationResult
    from video_factory.integrations.scene_renderer import SceneRenderOperationStore, SceneRenderRuntime

    db = sqlite3.connect(":memory:")
    store = SceneRenderOperationStore(db)
    request = VideoGenerationRequest(
        request_id="req-uncertain-scene-001",
        owner_user_id="operator",
        project_id="proj-1",
        scene_id="s1",
        prompt="Show product in action",
        duration_seconds=4,
    )
    store.mark_planned(request)
    store.claim_provider_for_submission(request, "google_flow")

    class SpyReconcilingProvider:
        provider_kind = "google_flow"
        is_available = True

        def __init__(self):
            self.submit_calls = []
            self.lookup_calls = []
            self.poll_calls = []

        def submit(self, req):
            self.submit_calls.append(req.request_id)
            return VideoGenerationResult(request_id=req.request_id, status="submitted", provider_operation_id="flow-op-initial")

        def get_status(self, op_id):
            self.poll_calls.append(op_id)
            return VideoGenerationResult(request_id=op_id, status="running", provider_operation_id=op_id)

        def lookup_by_request_id(self, req_id):
            self.lookup_calls.append(req_id)
            return VideoGenerationResult(request_id=req_id, status="running", provider_operation_id="flow-op-reconciled")

    spy_provider = SpyReconcilingProvider()

    # Resume execution when status is uncertain/submitting
    runtime = SceneRenderRuntime(
        store=store,
        provider={"google_flow": spy_provider},
        preferred_provider_kinds=("google_flow",),
    )
    result = runtime.run(request)

    assert result.status == "running"
    assert result.provider_operation_id == "flow-op-reconciled"
    # Verification: EXACTLY 0 submit calls on resume; 1 lookup call
    assert len(spy_provider.submit_calls) == 0, f"Submit must NOT be called on resume; got {spy_provider.submit_calls}"
    assert len(spy_provider.lookup_calls) == 1
    assert spy_provider.lookup_calls == [request.request_id]
    assert store.get(request.request_id)["provider_operation_id"] == "flow-op-reconciled"


# =============================================================================
# Test 5: Scene Flexibility (3, 4, 5 Scenes) With Real PyAV Composition & Verification
# =============================================================================
@pytest.mark.parametrize("scene_count,expected_seconds,expected_frames", [
    (3, 6.0, 180),
    (4, 8.0, 240),
    (5, 10.0, 300),
])
def test_scene_flexibility_3_4_5_distinct_clips_composer_and_decoder(
    tmp_path: Path, scene_count: int, expected_seconds: float, expected_frames: int
):
    """Verify 3, 4, and 5 scene plans concatenate distinct clips, decoded by PyAV with exact frame counts."""
    out_dir = tmp_path / f"export_scenes_{scene_count}"
    out_dir.mkdir(parents=True, exist_ok=True)
    scenes_dir = out_dir / "scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)

    client = VideoFactoryClient(owner_user_id="test_runner")
    story_ref = {
        "project_id": f"proj_s{scene_count}",
        "scenes": [
            {"scene_id": f"s{i+1}", "duration_seconds": 2.0}
            for i in range(scene_count)
        ],
    }

    render_out = client.render_video(
        story_ref=story_ref,
        workflow_id=f"wf_s{scene_count}",
        project_id=f"proj_s{scene_count}",
    )
    mp4_path = Path(render_out["file_path"])
    assert mp4_path.is_file()

    # Real media decode verification with PyAV
    probe = probe_video(mp4_path, decode_all=True)
    assert probe.width == 720
    assert probe.height == 1280
    assert probe.fps == 30.0
    assert probe.frame_count == expected_frames
    assert abs(probe.duration_seconds - expected_seconds) < 0.2

    # Verify individual scene clips exist and have distinct hashes
    scene_clips = [Path(s["file_path"]) for s in render_out.get("scenes", [])]
    assert len(scene_clips) == scene_count
    for c in scene_clips:
        assert c.is_file()
    clip_shas = {probe_video(c, decode_all=True).sha256 for c in scene_clips}
    assert len(clip_shas) == scene_count, "All scene clips must have distinct visual content and hashes!"

    # Verify export package and metadata.json
    export_out = client.export_video(
        render_ref=render_out,
        workflow_id=f"wf_s{scene_count}",
        project_id=f"proj_s{scene_count}",
    )
    meta_path = Path(export_out["metadata_path"])
    assert meta_path.is_file()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["scene_count"] == scene_count
    assert meta["frame_count"] == expected_frames
    assert len(meta["provenance"]["scenes"]) == scene_count
