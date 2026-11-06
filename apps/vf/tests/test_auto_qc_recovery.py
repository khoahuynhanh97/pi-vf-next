from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

from PIL import Image

from video_factory.handlers import (
    JobContext,
    JobFailed,
    JobSucceeded,
    ProductVideoJobHandlers,
    start_product_video_workflow,
)
from video_factory.qc.retry_policy import (
    get_qc_retry_count,
    plan_targeted_keyframe_recovery,
)
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.engine import WorkflowEngine
from video_factory.workflows.repository import WorkflowRepository


def _write_img(path: Path, color: tuple[int, int, int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (100, 100), color=color)
    from PIL import ImageDraw
    draw = ImageDraw.Draw(im)
    draw.rectangle([color[0] % 40, color[1] % 40, 40 + color[0] % 40, 40 + color[1] % 40], fill=((color[0] + 120) % 255, (color[1] + 120) % 255, (color[2] + 120) % 255))
    im.save(path, format="PNG")
    return path


def test_crawl_candidate_replacement_when_first_image_fails_qc(tmp_path: Path) -> None:
    """Requirement 2: Crawl image fails QC -> alternative candidate from pool tried -> 2 PASS -> Lock."""
    conn = connect(tmp_path / "candidate_replace.db")
    repo = WorkflowRepository(conn)
    engine = WorkflowEngine(repo)

    wf_id = "wf_candidate_replace"
    img1 = _write_img(tmp_path / "c1.png", (10, 10, 10))
    img2 = _write_img(tmp_path / "c2.png", (20, 20, 20))
    img3 = _write_img(tmp_path / "c3.png", (30, 30, 30))

    source_context = {
        "source": "hermes_product_video_exchange",
        "auto_admitted": True,
        "brand": "Ugreen", "model": "Charger",
        "images": [
            {"asset_id": "c1", "absolute_path": str(img1)},
            {"asset_id": "c2", "absolute_path": str(img2)},
            {"asset_id": "c3", "absolute_path": str(img3)},
        ],
    }

    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf_id,
        product_name="Ugreen Charger",
        keywords=["ugreen", "charger"],
        project_id="proj_replace",
        owner_user_id="user_test",
        source_context=source_context,
    )

    from datetime import datetime, timezone, timedelta
    auth = {
        "authorization_id": f"auth_{wf_id}",
        "workflow_id": wf_id,
        "max_cost_usd": 10.0,
        "workflow_max_cost_usd": 10.0,
        "scope": ["image"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    agg, ver = repo.get_workflow(wf_id)
    agg["qc_authorization"] = auth
    repo.commit_workflow(wf_id, json.dumps(agg), [], expected_version=ver)

    mock_pi = MagicMock()
    mock_pi.build_resource_pack.return_value = {
        "resource_pack_id": "rp_test",
        "version": 1,
        "assets": [
            {"asset_id": "c1", "local_path": str(img1)},
            {"asset_id": "c2", "local_path": str(img2)},
        ],
        "product_references": [
            {"asset_id": "c1", "local_path": str(img1)},
            {"asset_id": "c2", "local_path": str(img2)},
        ],
    }

    mock_evaluator = MagicMock()
    call_count = 0
    def mock_eval(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # c1 fails
            return {
                "verdict": "fail", "total_score": 40, "identity_score": 20, "clarity_score": 10,
                "usability_score": 10, "hard_failure": False, "eligible": False,
                "reason": "c1 fails", "visible_details": [], "scene_suitability": [], "risk_flags": [],
                "view_angle": "front", "shot_type": "product", "text_coverage_percent": 0,
                "source_branding_detected": False, "identity_mismatch": False,
            }
        elif call_count == 2:
            # c2 passes
            return {
                "verdict": "pass", "total_score": 85, "identity_score": 50, "clarity_score": 20,
                "usability_score": 15, "hard_failure": False, "eligible": True,
                "reason": "c2 passes", "visible_details": [], "scene_suitability": [], "risk_flags": [],
                "view_angle": "front", "shot_type": "product", "text_coverage_percent": 0,
                "source_branding_detected": False, "identity_mismatch": False,
            }
        else:
            # c3 passes
            return {
                "verdict": "pass", "total_score": 90, "identity_score": 55, "clarity_score": 22,
                "usability_score": 13, "hard_failure": False, "eligible": True,
                "reason": "c3 passes", "visible_details": [], "scene_suitability": [], "risk_flags": [],
                "view_angle": "front", "shot_type": "product", "text_coverage_percent": 0,
                "source_branding_detected": False, "identity_mismatch": False,
            }
    mock_evaluator.evaluate_image.side_effect = mock_eval

    handlers = ProductVideoJobHandlers(
        workflow_engine=engine,
        workflow_repository=repo,
        pi_client=mock_pi,
        provider_mode="configured",
        evaluator=mock_evaluator,
    )

    ctx = JobContext(workflow_id=wf_id, job_id="job_pack", owner_user_id="user_test", expected_version=ver + 1)
    res = handlers.resource_pack_job(
        ctx,
        research_result={"snapshot_id": "snap_1", "persistence_status": "saved"},
        source_context=source_context,
    )

    assert isinstance(res, JobSucceeded)
    assert res.next_command["command"] == "lock_resource_pack"
    # One passed image is sufficient; stop before evaluating further candidates.
    assets = res.artifact_ref.get("assets", [])
    assert len(assets) == 1
    assert {a["asset_id"] for a in assets} == {"c2"}
    for a in assets:
        assert a.get("sha256") is not None
        assert len(a["sha256"]) == 64  # valid hex sha256

    # Verify SQLite retry table recorded attempts
    retries = get_qc_retry_count(conn, wf_id, "source_image_qc")
    # One product-level selection attempt stops after the first usable candidate.
    assert retries == 1
    assert mock_evaluator.evaluate_image.call_count == 2


def test_crawl_candidate_pool_exhaustion_fails_closed(tmp_path: Path) -> None:
    """Requirement 2: All candidates fail QC -> fail closed with IMAGE_QC_FAILED without hanging batch."""
    conn = connect(tmp_path / "candidate_exhaust.db")
    repo = WorkflowRepository(conn)
    engine = WorkflowEngine(repo)

    wf_id = "wf_exhaust"
    img1 = _write_img(tmp_path / "c1.png", (10, 10, 10))
    img2 = _write_img(tmp_path / "c2.png", (20, 20, 20))

    source_context = {
        "source": "hermes_product_video_exchange",
        "auto_admitted": True,
        "brand": "Ugreen", "model": "Charger",
        "images": [
            {"asset_id": "c1", "absolute_path": str(img1)},
            {"asset_id": "c2", "absolute_path": str(img2)},
        ],
    }

    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf_id,
        product_name="Ugreen Charger",
        keywords=["ugreen"],
        project_id="proj_exhaust",
        owner_user_id="user_test",
        source_context=source_context,
    )

    from datetime import datetime, timezone, timedelta
    auth = {
        "authorization_id": f"auth_{wf_id}",
        "workflow_id": wf_id,
        "max_cost_usd": 10.0,
        "workflow_max_cost_usd": 10.0,
        "scope": ["image"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    agg, ver = repo.get_workflow(wf_id)
    agg["qc_authorization"] = auth
    repo.commit_workflow(wf_id, json.dumps(agg), [], expected_version=ver)

    mock_pi = MagicMock()
    mock_pi.build_resource_pack.return_value = {
        "resource_pack_id": "rp_test",
        "version": 1,
        "assets": [
            {"asset_id": "c1", "local_path": str(img1)},
            {"asset_id": "c2", "local_path": str(img2)},
        ],
    }

    mock_evaluator = MagicMock()
    mock_evaluator.evaluate_image.return_value = {
        "verdict": "fail", "total_score": 30, "identity_score": 15,
        "clarity_score": 10, "usability_score": 5, "hard_failure": False,
        "eligible": False, "reason": "poor quality", "visible_details": [],
        "scene_suitability": [], "risk_flags": [],
        "source_branding_detected": False, "identity_mismatch": False,
        "view_angle": "front", "shot_type": "product", "text_coverage_percent": 0,
    }

    handlers = ProductVideoJobHandlers(
        workflow_engine=engine,
        workflow_repository=repo,
        pi_client=mock_pi,
        provider_mode="configured",
        evaluator=mock_evaluator,
    )

    ctx = JobContext(workflow_id=wf_id, job_id="job_pack", owner_user_id="user_test", expected_version=ver + 1)
    res = handlers.resource_pack_job(
        ctx,
        research_result={"snapshot_id": "snap_1", "persistence_status": "saved"},
        source_context=source_context,
    )

    assert isinstance(res, JobFailed)
    assert res.retryable is False
    assert res.code == "EXCHANGE_IMAGE_QC_FAILED"
    assert "no usable eligible image reference" in res.message


def test_lock_job_fails_closed_on_physical_byte_mismatch(tmp_path: Path) -> None:
    """Requirement 2: Physical bytes must match QC-passed SHA-256 hash at lock_job."""
    conn = connect(tmp_path / "lock_mismatch.db")
    repo = WorkflowRepository(conn)
    engine = WorkflowEngine(repo)

    wf_id = "wf_lock_mismatch"
    img1 = _write_img(tmp_path / "p1.png", (50, 50, 50))
    img2 = _write_img(tmp_path / "p2.png", (60, 60, 60))

    source_context = {
        "source": "hermes_product_video_exchange",
        "images": [
            {"asset_id": "p1", "absolute_path": str(img1)},
            {"asset_id": "p2", "absolute_path": str(img2)},
        ],
    }

    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf_id,
        product_name="Ugreen Hub",
        keywords=["ugreen"],
        project_id="proj_lock",
        owner_user_id="user_test",
        source_context=source_context,
    )

    agg, ver = repo.get_workflow(wf_id)
    agg["state"] = "building_resource_pack"
    repo.commit_workflow(wf_id, json.dumps(agg), [], expected_version=ver)

    # Pack has recorded sha256, but physical file on disk was modified/tampered
    h1 = hashlib.sha256(img1.read_bytes()).hexdigest()
    # Tamper img1 by writing different bytes
    _write_img(img1, (99, 99, 99))

    pack_dict = {
        "resource_pack_id": "rp_tampered",
        "version": 1,
        "pack_status": "ready_to_lock",
        "assets": [
            {"asset_id": "p1", "local_path": str(img1), "sha256": h1},
            {"asset_id": "p2", "local_path": str(img2), "sha256": hashlib.sha256(img2.read_bytes()).hexdigest()},
        ],
    }

    handlers = ProductVideoJobHandlers(
        workflow_engine=engine,
        workflow_repository=repo,
        pi_client=MagicMock(),
        provider_mode="configured",
    )

    ctx = JobContext(workflow_id=wf_id, job_id="job_lock", owner_user_id="user_test", expected_version=ver + 1)
    res = handlers.lock_job(ctx, pack_dict=pack_dict, source_context=source_context)

    assert isinstance(res, JobFailed)
    assert res.code == "APPROVED_IMAGE_MISMATCH"
    assert "Byte hash mismatch for asset p1" in res.message


def test_keyframe_targeted_recovery_planning() -> None:
    """Requirement 3: START frame failure cascades to END frame; PASS frame is preserved."""
    frames = [
        {"frame_id": "scene_1-start", "frame_role": "start", "url": "https://img.test/f0.png"},
        {"frame_id": "scene_1-end", "frame_role": "end", "url": "https://img.test/f1.png"},
        {"frame_id": "scene_2-broll", "frame_role": "broll", "url": "https://img.test/f2.png"},
    ]
    # START frame failed QC, B-roll passed QC
    qc_items = [
        {"frame_id": "scene_1-start", "verdict": "fail", "recommended_action": "Fix logo", "reason": "Logo is blurry"},
        {"frame_id": "scene_2-broll", "verdict": "pass"},
    ]

    plan = plan_targeted_keyframe_recovery(
        qc_report={"findings": {"items": qc_items}},
        generated_keyframes=frames,
    )

    assert "scene_1-start" in plan["failing_frame_ids"]  # failing START frame
    assert "scene_1-end" in plan["failing_frame_ids"]    # END frame depends on START frame!
    assert "scene_1-end" in plan["cascade_regenerations"]
    assert "scene_2-broll" in plan["passed_frame_ids"]   # B-roll preserved
    assert "Logo is blurry" in plan["feedback_by_frame"]["scene_1-start"]
    assert "START frame was regenerated" in plan["feedback_by_frame"]["scene_1-end"]
