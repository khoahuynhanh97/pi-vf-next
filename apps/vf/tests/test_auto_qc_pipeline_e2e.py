from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image

from video_factory.handlers import (
    JobContext,
    JobFailed,
    JobSucceeded,
    ProductVideoJobHandlers,
    start_product_video_workflow,
)
from video_factory.handlers.registry import create_product_video_topic_handlers
from video_factory.qc.models import QCReport, QCStage, QCVerdict
from video_factory.qc.normalization import resource_pack_fingerprint
from video_factory.qc.repository import QCReportRepository
from video_factory.qc.retry_policy import (
    plan_targeted_keyframe_recovery,
)
from video_factory.storage_infra.sqlite import connect
from video_factory.workflow import ProductVideoWorkflowState
from video_factory.workflows.engine import WorkflowEngine
from video_factory.workflows.outbox_dispatcher import OutboxDispatcher
from video_factory.workflows.outbox_repository import OutboxRepository
from video_factory.workflows.repository import WorkflowRepository
from video_factory.workflows.envelope import build_envelope, parse_envelope


_img_seq = 0


def _create_unique_image(path: Path, color: tuple[int, int, int] | None = None) -> Path:
    global _img_seq
    _img_seq += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    c = color or ((_img_seq * 37) % 255, (_img_seq * 71) % 255, (_img_seq * 113) % 255)
    im = Image.new("RGB", (100, 100), color=c)
    from PIL import ImageDraw
    draw = ImageDraw.Draw(im)
    x0 = (_img_seq * 17) % 60
    y0 = (_img_seq * 23) % 60
    draw.rectangle([x0, y0, x0 + 35, y0 + 35], fill=((c[0] + 128) % 255, (c[1] + 128) % 255, (c[2] + 128) % 255))
    im.putpixel((0, 0), (_img_seq & 255, (_img_seq >> 8) & 255, (_img_seq >> 16) & 255))
    im.save(path, format="PNG")
    return path


def _qc_authorization(workflow_id: str) -> dict[str, Any]:
    return {
        "authorization_id": f"auth_{workflow_id}",
        "workflow_id": workflow_id,
        "max_cost_usd": 10.0,
        "workflow_max_cost_usd": 10.0,
        "scope": ["image", "product_video"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }


class _PassImageEvaluator:
    def evaluate_image(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "verdict": "pass", "total_score": 90, "identity_score": 55,
            "clarity_score": 23, "usability_score": 12, "hard_failure": False,
            "source_branding_detected": False, "identity_mismatch": False,
            "eligible": True, "reason": "Clear product match", "view_angle": "front",
            "shot_type": "product", "visible_details": [],
            "scene_suitability": ["hero"], "risk_flags": [],
            "text_coverage_percent": 0,
        }


# =====================================================================
# Scenario 1: Exchange image handoff starts directly at the resource-pack job.
# =====================================================================
def test_scenario_1_auto_exchange_two_correct_images_advances_to_product_lock(tmp_path: Path) -> None:
    db_file = tmp_path / "scenario_1.db"
    conn = connect(db_file)
    repo = WorkflowRepository(conn)

    wf_id = "wf_scen_1"
    img1 = _create_unique_image(tmp_path / "img1.png")
    img2 = _create_unique_image(tmp_path / "img2.png")
    assert hashlib.sha256(img1.read_bytes()).hexdigest() != hashlib.sha256(img2.read_bytes()).hexdigest()

    source_context = {
        "source": "hermes_product_video_exchange",
        "auto_admitted": True,
        "product_key": "ugreen_uno_65w",
        "brand": "Ugreen",
        "model": "Uno 65W",
        "images": [
            {"asset_id": "u1", "absolute_path": str(img1), "original_url": "https://img.ugreen/1.png"},
            {"asset_id": "u2", "absolute_path": str(img2), "original_url": "https://img.ugreen/2.png"},
        ],
    }

    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf_id,
        product_name="Ugreen Uno 65W Fast Charger",
        keywords=["ugreen", "uno", "65w"],
        project_id="proj_scen_1",
        owner_user_id="user_auto",
        source_context=source_context,
        qc_authorization=_qc_authorization(wf_id),
        qc_max_cost_usd=10.0,
    )

    mock_pi = MagicMock()
    mock_pi.research.return_value = {
        "snapshot_id": "snap_scen_1",
        "persistence_status": "saved",
        "source_context": source_context,
    }
    mock_pi.build_resource_pack.return_value = {
        "resource_pack_id": "rp_scen_1",
        "version": 1,
        "assets": [
            {"asset_id": "u1", "local_path": str(img1)},
            {"asset_id": "u2", "local_path": str(img2)},
        ],
        "product_references": [
            {"asset_id": "u1", "local_path": str(img1)},
            {"asset_id": "u2", "local_path": str(img2)},
        ],
    }

    handlers_svc = ProductVideoJobHandlers(
        workflow_repository=repo,
        pi_client=mock_pi,
        video_client=MagicMock(),
        provider_mode="configured",
        evaluator=_PassImageEvaluator(),
    )

    topic_handlers = create_product_video_topic_handlers(
        handlers_service=handlers_svc,
        workflow_repository=repo,
        provider_mode="configured",
    )
    dispatcher = OutboxDispatcher(repository=OutboxRepository(conn), topic_handlers=topic_handlers)

    # Exchange already provides local images, so startup dispatches the resource-pack job.
    rep1 = dispatcher.process_batch(workflow_ids=[wf_id])
    assert rep1.succeeded == 1

    # The lock approval is now the next workflow boundary, not another worker job.
    rep2 = dispatcher.process_batch(workflow_ids=[wf_id])
    assert rep2.claimed == 0

    wf_data = repo.get_workflow(wf_id)
    assert wf_data is not None
    agg = wf_data[0]
    # State has advanced to AwaitingProductLock without any manual confirmation flags!
    assert agg["state"] == ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value

    # Check verified PASS report exists in QCReportRepository
    pack = agg["resource_pack"]
    pack_digest = resource_pack_fingerprint(pack)
    qc_repo = QCReportRepository(conn)
    verified_rep = qc_repo.get_latest_report(workflow_id=wf_id, stage=QCStage.IMAGE, expected_artifact_digest=pack_digest)
    assert verified_rep is not None
    assert verified_rep.verdict == QCVerdict.PASS

    # Product Lock Job verifies report and locks the pack cleanly
    # Transition aggregate to LOCKING_RESOURCE_PACK so lock_job state precondition passes
    agg_lock = dict(agg)
    agg_lock["state"] = ProductVideoWorkflowState.LOCKING_RESOURCE_PACK.value
    repo.commit_workflow(workflow_id=wf_id, aggregate_json=json.dumps(agg_lock), events=[], expected_version=wf_data[1])
    mock_pi.lock_resource_pack.return_value = {
        "resource_pack_id": pack.get("resource_pack_id"),
        "status": "locked",
        "pack_status": "locked",
        "owner": "product_intelligence",
        "schema": "product_resource_pack_locked",
        "version": 1,
    }
    lock_ctx = JobContext(workflow_id=wf_id, job_id="job_lock_1", owner_user_id="user_auto", expected_version=wf_data[1] + 1)
    lock_res = handlers_svc.lock_job(lock_ctx, pack_dict=pack, source_context=source_context)
    assert isinstance(lock_res, JobSucceeded)
    assert lock_res.artifact_ref.get("pack_status") in ("ready_to_lock", "locked")


# =====================================================================
# Scenario 2: Một ảnh sai: bị loại, ảnh ứng viên kế tiếp được QC và thay thế,
# rồi workflow tiếp tục.
# =====================================================================
def test_scenario_2_candidate_substitution_when_one_image_fails_qc(tmp_path: Path) -> None:
    db_file = tmp_path / "scenario_2.db"
    conn = connect(db_file)
    repo = WorkflowRepository(conn)
    engine = WorkflowEngine(repo)

    wf_id = "wf_scen_2"
    bad_img = _create_unique_image(tmp_path / "bad.png")
    good_img_a = _create_unique_image(tmp_path / "good_a.png")
    good_img_b = _create_unique_image(tmp_path / "good_b.png")
    source_context = {
        "source": "hermes_product_video_exchange",
        "auto_admitted": True,
        "brand": "Ugreen", "model": "Robot GaN Charger",
        "images": [
            {"asset_id": "c_bad", "absolute_path": str(bad_img), "original_url": "https://img.example/bad.png"},
            {"asset_id": "c_good_a", "absolute_path": str(good_img_a), "original_url": "https://img.example/good_a.png"},
            {"asset_id": "c_good_b", "absolute_path": str(good_img_b), "original_url": "https://img.example/good_b.png"},
        ],
    }

    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf_id,
        product_name="Robot GaN Charger",
        keywords=["robot", "gan"],
        project_id="proj_scen_2",
        owner_user_id="user_test",
        source_context=source_context,
        qc_authorization=_qc_authorization(wf_id),
        qc_max_cost_usd=10.0,
    )

    mock_pi = MagicMock()
    mock_pi.build_resource_pack.return_value = {
        "resource_pack_id": "rp_scen_2",
        "version": 1,
        "assets": [
            {"asset_id": "c_bad", "local_path": str(bad_img)},
            {"asset_id": "c_good_a", "local_path": str(good_img_a)},
        ],
        "product_references": [
            {"asset_id": "c_bad", "local_path": str(bad_img)},
            {"asset_id": "c_good_a", "local_path": str(good_img_a)},
        ],
    }

    class MockEvaluatorScenario2:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def evaluate_image(self, image_path: str | Path, prompt: str, system_instruction: str | None = None) -> dict[str, Any]:
            p_str = str(image_path)
            self.calls.append(p_str)
            if "bad" in p_str:
                return {
                    "verdict": "fail",
                    "total_score": 30,
                    "identity_score": 15,
                    "clarity_score": 10,
                    "usability_score": 5,
                    "hard_failure": False,
                    "source_branding_detected": False,
                    "identity_mismatch": True,
                    "eligible": False,
                    "reason": "Wrong product appearance",
                    "view_angle": "front",
                    "shot_type": "product",
                    "visible_details": [],
                    "scene_suitability": [],
                    "risk_flags": ["identity_mismatch"],
                    "text_coverage_percent": 0,
                }
            return {
                "verdict": "pass",
                "total_score": 90,
                "identity_score": 55,
                "clarity_score": 23,
                "usability_score": 12,
                "hard_failure": False,
                "source_branding_detected": False,
                "identity_mismatch": False,
                "eligible": True,
                "reason": "Clear product match",
                "view_angle": "front three-quarter",
                "shot_type": "hero reference",
                "visible_details": [],
                "scene_suitability": ["hero", "context"],
                "risk_flags": [],
                "text_coverage_percent": 0,
            }

    evaluator = MockEvaluatorScenario2()
    handlers = ProductVideoJobHandlers(
        workflow_engine=engine,
        workflow_repository=repo,
        pi_client=mock_pi,
        video_client=MagicMock(),
        provider_mode="configured",
        evaluator=evaluator,
    )

    ctx = JobContext(workflow_id=wf_id, job_id="job_pack_2", owner_user_id="user_test", expected_version=1)
    res = handlers.resource_pack_job(
        ctx,
        research_result={"snapshot_id": "snap_scen_2", "persistence_status": "saved"},
        source_context=source_context,
        qc_authorization=_qc_authorization(wf_id),
        qc_max_cost_usd=10.0,
    )

    assert isinstance(res, JobSucceeded)
    final_pack = res.artifact_ref
    final_assets = final_pack.get("assets", [])
    assert len(final_assets) == 1
    # c_bad was rejected; the first usable reference is enough.
    assert {a["asset_id"] for a in final_assets} == {"c_good_a"}
    assert evaluator.calls.count(str(bad_img)) == 1

    # Verified PASS report persisted
    pack_digest = resource_pack_fingerprint(final_pack)
    verified_rep = QCReportRepository(conn).get_latest_report(workflow_id=wf_id, stage=QCStage.IMAGE, expected_artifact_digest=pack_digest)
    assert verified_rep is not None
    assert verified_rep.verdict == QCVerdict.PASS


# =====================================================================
# Scenario 3: One matching usable image is sufficient for the resource-pack gate.
# =====================================================================
def test_scenario_3_one_usable_image_is_sufficient(tmp_path: Path) -> None:
    db_file = tmp_path / "scenario_3.db"
    conn = connect(db_file)
    repo = WorkflowRepository(conn)

    wf_id = "wf_scen_3"
    img1 = _create_unique_image(tmp_path / "img1.png")
    img2 = _create_unique_image(tmp_path / "img2.png")

    source_context = {
        "source": "hermes_product_video_exchange",
        "auto_admitted": True,
        "brand": "Ugreen", "model": "Single Image Product",
        "images": [
            {"asset_id": "single_img_1", "absolute_path": str(img1)},
            {"asset_id": "single_img_2", "absolute_path": str(img2)},
        ],
    }

    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf_id,
        product_name="Single Image Product",
        keywords=["single"],
        project_id="proj_scen_3",
        owner_user_id="user_test",
        source_context=source_context,
        qc_authorization=_qc_authorization(wf_id),
        qc_max_cost_usd=10.0,
    )

    mock_pi = MagicMock()
    mock_pi.build_resource_pack.return_value = {
        "resource_pack_id": "rp_scen_3",
        "version": 1,
        "assets": [
            {"asset_id": "single_img_1", "local_path": str(img1)},
            {"asset_id": "single_img_2", "local_path": str(img2)},
        ],
        "product_references": [
            {"asset_id": "single_img_1", "local_path": str(img1)},
            {"asset_id": "single_img_2", "local_path": str(img2)},
        ],
    }

    class OnePassEvaluator(_PassImageEvaluator):
        def evaluate_image(self, image_path: str | Path, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            result = super().evaluate_image(image_path)
            if str(image_path) == str(img2):
                result.update(
                    verdict="fail", total_score=40, identity_score=20,
                    clarity_score=15, usability_score=5, eligible=False,
                    identity_mismatch=True,
                )
            return result

    handlers = ProductVideoJobHandlers(
        workflow_repository=repo,
        pi_client=mock_pi,
        video_client=MagicMock(),
        provider_mode="configured",
        evaluator=OnePassEvaluator(),
    )

    ctx = JobContext(workflow_id=wf_id, job_id="job_pack_3", owner_user_id="user_test", expected_version=1)
    res = handlers.resource_pack_job(
        ctx,
        research_result={"snapshot_id": "snap_3", "persistence_status": "saved"},
        source_context=source_context,
        qc_authorization=_qc_authorization(wf_id),
        qc_max_cost_usd=10.0,
    )

    assert isinstance(res, JobSucceeded)
    assert [asset["asset_id"] for asset in res.artifact_ref["assets"]] == ["single_img_1"]
    assert res.artifact_ref["pack_status"] == "ready_to_lock"


# =====================================================================
# Scenario 4: Duplicate or changed images cannot bypass the current QC report gate.
# =====================================================================
def test_scenario_4_unverified_duplicate_or_tampered_pack_fails_product_lock(tmp_path: Path) -> None:
    db_file = tmp_path / "scenario_4.db"
    conn = connect(db_file)
    repo = WorkflowRepository(conn)
    engine = WorkflowEngine(repo)

    wf_id = "wf_scen_4"
    img1 = _create_unique_image(tmp_path / "img1.png")
    # Clone exact bytes to img2 (duplicate bytes)
    img2 = tmp_path / "img2_duplicate.png"
    img2.write_bytes(img1.read_bytes())
    assert hashlib.sha256(img1.read_bytes()).hexdigest() == hashlib.sha256(img2.read_bytes()).hexdigest()

    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf_id,
        product_name="Duplicate Product",
        keywords=["dup"],
        project_id="proj_scen_4",
        owner_user_id="user_test",
    )

    handlers = ProductVideoJobHandlers(
        workflow_engine=engine,
        workflow_repository=repo,
        pi_client=MagicMock(),
        video_client=MagicMock(),
        provider_mode="configured",
    )

    cur_wf = repo.get_workflow(wf_id)
    assert cur_wf is not None
    cur_agg, cur_ver = cur_wf
    cur_agg["state"] = ProductVideoWorkflowState.LOCKING_RESOURCE_PACK.value
    repo.commit_workflow(workflow_id=wf_id, aggregate_json=json.dumps(cur_agg), events=[], expected_version=cur_ver)

    # Part A: Pack with duplicate content hashes rejected at lock_job
    dup_pack = {
        "resource_pack_id": "rp_dup",
        "version": 1,
        "pack_status": "ready_to_lock",
        "product_references": [
            {"asset_id": "a1", "local_path": str(img1), "sha256": hashlib.sha256(img1.read_bytes()).hexdigest()},
            {"asset_id": "a2", "local_path": str(img2), "sha256": hashlib.sha256(img2.read_bytes()).hexdigest()},
        ],
    }
    lock_ctx = JobContext(workflow_id=wf_id, job_id="job_lock_4a", owner_user_id="user_test", expected_version=cur_ver + 1)
    res_a = handlers.lock_job(lock_ctx, pack_dict=dup_pack)
    assert isinstance(res_a, JobFailed)
    assert res_a.code == "APPROVED_IMAGE_MISMATCH"
    assert "No verified QC report" in res_a.message

    # Part B: A pack whose bytes changed without a corresponding current QC report is blocked.
    tampered_img = _create_unique_image(tmp_path / "tampered.png")
    original_hash = hashlib.sha256(tampered_img.read_bytes()).hexdigest()
    good_img = _create_unique_image(tmp_path / "good.png")

    tampered_pack = {
        "resource_pack_id": "rp_tampered",
        "version": 1,
        "pack_status": "ready_to_lock",
        "product_references": [
            {"asset_id": "t1", "local_path": str(tampered_img), "sha256": original_hash},
            {"asset_id": "t2", "local_path": str(good_img), "sha256": hashlib.sha256(good_img.read_bytes()).hexdigest()},
        ],
    }
    # Modify disk bytes of tampered_img
    tampered_img.write_bytes(b"TAMPERED_BYTES_ON_DISK")
    res_b = handlers.lock_job(lock_ctx, pack_dict=tampered_pack)
    assert isinstance(res_b, JobFailed)
    assert res_b.code == "APPROVED_IMAGE_MISMATCH"
    assert "Byte hash mismatch" in res_b.message


# =====================================================================
# Scenario 5: QC FAIL/UNKNOWN, lỗi parse/timeout: không có report PASS suy diễn
# và không vào Product Lock.
# =====================================================================
def test_scenario_5_qc_fail_unknown_timeout_never_infers_pass(tmp_path: Path) -> None:
    db_file = tmp_path / "scenario_5.db"
    conn = connect(db_file)
    repo = WorkflowRepository(conn)
    img1 = _create_unique_image(tmp_path / "img1.png")
    img2 = _create_unique_image(tmp_path / "img2.png")

    class NonPassEvaluator(_PassImageEvaluator):
        def __init__(self, outcome: str) -> None:
            self.outcome = outcome
            self.calls = 0

        def evaluate_image(self, image_path: str | Path, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            if self.outcome == "timeout":
                raise TimeoutError("Provider timeout")
            result = super().evaluate_image(image_path)
            result.update(verdict=self.outcome, total_score=90, eligible=False)
            return result

    # Each paid operation has its own workflow and operation ID. Replaying one
    # completed/uncertain operation with a different evaluator would be invalid.
    for outcome in ("fail", "unknown", "timeout"):
        wf_id = f"wf_scen_5_{outcome}"
        source_context = {
            "source": "hermes_product_video_exchange",
            "auto_admitted": True,
            "brand": "Ugreen", "model": "Failing QC Product",
            "images": [
                {"asset_id": "i1", "absolute_path": str(img1)},
                {"asset_id": "i2", "absolute_path": str(img2)},
            ],
        }
        start_product_video_workflow(
            workflow_repository=repo, workflow_id=wf_id,
            product_name="Failing QC Product", keywords=["fail"],
            project_id="proj_scen_5", owner_user_id="user_test",
            source_context=source_context,
            qc_authorization=_qc_authorization(wf_id), qc_max_cost_usd=10.0,
        )
        mock_pi = MagicMock()
        mock_pi.build_resource_pack.return_value = {
            "resource_pack_id": f"rp_scen_5_{outcome}", "version": 1,
            "assets": [
                {"asset_id": "i1", "local_path": str(img1)},
                {"asset_id": "i2", "local_path": str(img2)},
            ],
        }
        evaluator = NonPassEvaluator(outcome)
        handlers = ProductVideoJobHandlers(
            workflow_repository=repo, pi_client=mock_pi,
            video_client=MagicMock(), provider_mode="configured",
            evaluator=evaluator,
        )
        ctx = JobContext(workflow_id=wf_id, job_id=f"job_pack_5_{outcome}", owner_user_id="user_test", expected_version=1)
        result = handlers.resource_pack_job(
            ctx, research_result={"snapshot_id": "snap_5", "persistence_status": "saved"},
            source_context=source_context,
            qc_authorization=_qc_authorization(wf_id), qc_max_cost_usd=10.0,
        )
        assert isinstance(result, JobFailed)
        assert result.code in {"IMAGE_QC_FAILED", "EXCHANGE_IMAGE_QC_FAILED", "QC_OUTCOME_UNCERTAIN"}
        if outcome == "timeout":
            assert result.code == "QC_OUTCOME_UNCERTAIN"
            assert evaluator.calls == 1
            assert conn.execute(
                "SELECT status FROM workflow_qc_retries WHERE workflow_id=?",
                (wf_id,),
            ).fetchone()[0] == "technical_unknown"
        latest = QCReportRepository(conn).get_latest_report(workflow_id=wf_id, stage=QCStage.IMAGE)
        assert latest is None or latest.verdict != QCVerdict.PASS


# =====================================================================
# Scenario 6: A product with no usable reference is skipped; the next batch item continues.
# =====================================================================
def test_scenario_6_exhaustion_skips_one_product_and_batch_continues(tmp_path: Path) -> None:
    db_file = tmp_path / "scenario_6.db"
    conn = connect(db_file)
    repo = WorkflowRepository(conn)

    wf_fail = "wf_scen_6_fail"
    wf_next = "wf_scen_6_next"

    # 5 candidates: 2 initial in pack + 3 distinct recovery candidates = 4 total attempts (1 initial + 3 recoveries)
    b_imgs = [_create_unique_image(tmp_path / f"bad_{i}.png") for i in range(5)]

    source_context_fail = {
        "source": "hermes_product_video_exchange",
        "auto_admitted": True,
        "brand": "Ugreen", "model": "Exhaustion Product",
        "images": [
            {"asset_id": f"b{i}", "absolute_path": str(b_imgs[i])}
            for i in range(5)
        ],
    }

    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf_fail,
        product_name="Exhaustion Product",
        keywords=["fail"],
        project_id="proj_6",
        owner_user_id="user_test",
        source_context=source_context_fail,
        max_qc_retries=3,
        qc_authorization=_qc_authorization(wf_fail),
        qc_max_cost_usd=10.0,
    )

    # Workflow 2 is a healthy independent product in the batch
    img_ok_1 = _create_unique_image(tmp_path / "ok1.png")
    img_ok_2 = _create_unique_image(tmp_path / "ok2.png")
    source_context_next = {
        "source": "hermes_product_video_exchange",
        "auto_admitted": True,
        "brand": "Ugreen", "model": "Healthy Next Product",
        "images": [
            {"asset_id": "ok1", "absolute_path": str(img_ok_1)},
            {"asset_id": "ok2", "absolute_path": str(img_ok_2)},
        ],
    }
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf_next,
        product_name="Healthy Next Product",
        keywords=["ok"],
        project_id="proj_6",
        owner_user_id="user_test",
        source_context=source_context_next,
        qc_authorization=_qc_authorization(wf_next),
        qc_max_cost_usd=10.0,
    )

    mock_pi = MagicMock()
    exchange_images_by_workflow = {
        wf_fail: source_context_fail["images"],
        wf_next: source_context_next["images"],
    }
    mock_pi.research.side_effect = lambda *args, **kwargs: {
        "snapshot_id": f"snap_{kwargs.get('workflow_id')}",
        "persistence_status": "saved",
    }
    mock_pi.build_resource_pack.side_effect = lambda res, **kwargs: {
        "resource_pack_id": f"rp_{kwargs.get('workflow_id')}",
        "version": 1,
        "assets": exchange_images_by_workflow[kwargs.get("workflow_id")][:2],
        "product_references": exchange_images_by_workflow[kwargs.get("workflow_id")][:2],
    }

    class MockEvaluatorScenario6:
        def evaluate_image(self, image_path: str | Path, prompt: str, system_instruction: str | None = None) -> dict[str, Any]:
            p_str = str(image_path)
            if "bad" in p_str or "b5" in p_str:
                return {
                    "verdict": "fail",
                    "total_score": 25,
                    "identity_score": 10,
                    "clarity_score": 10,
                    "usability_score": 5,
                    "hard_failure": False,
                    "source_branding_detected": False,
                    "identity_mismatch": True,
                    "eligible": False,
                    "reason": "Unrecognizable product variant",
                    "view_angle": "front",
                    "shot_type": "product",
                    "visible_details": [],
                    "scene_suitability": [],
                    "risk_flags": ["identity_mismatch"],
                    "text_coverage_percent": 0,
                }
            return {
                "verdict": "pass",
                "total_score": 90,
                "identity_score": 55,
                "clarity_score": 23,
                "usability_score": 12,
                "hard_failure": False,
                "source_branding_detected": False,
                "identity_mismatch": False,
                "eligible": True,
                "reason": "Clear product match",
                "view_angle": "front three-quarter",
                "shot_type": "hero reference",
                "visible_details": [],
                "scene_suitability": ["hero", "context"],
                "risk_flags": [],
                "text_coverage_percent": 0,
            }

    handlers_svc = ProductVideoJobHandlers(
        workflow_repository=repo,
        pi_client=mock_pi,
        video_client=MagicMock(),
        provider_mode="configured",
        evaluator=MockEvaluatorScenario6(),
    )

    topic_handlers = create_product_video_topic_handlers(
        handlers_service=handlers_svc,
        workflow_repository=repo,
        provider_mode="configured",
    )

    dispatcher = OutboxDispatcher(
        repository=OutboxRepository(conn),
        topic_handlers=topic_handlers,
    )

    # Exchange-backed starts dispatch resource-pack work directly.
    rep = dispatcher.process_batch(workflow_ids=[wf_fail, wf_next])
    # The failed product waits for operator review; the independent product advances.
    assert rep.quarantined == 0
    assert rep.succeeded == 2

    # 1. Check the failed job carries an exact, persisted skip reason.
    data_fail = repo.get_workflow(wf_fail)
    assert data_fail is not None
    agg_fail = data_fail[0]
    assert agg_fail["state"] == "skipped"
    assert "Visual Image QC failed" in agg_fail["skip_reason"]
    # 2. Check wf_next successfully continued to AwaitingProductLock!
    data_next = repo.get_workflow(wf_next)
    assert data_next is not None
    agg_next = data_next[0]
    assert agg_next["state"] == ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value

# =====================================================================
# Scenario 7: Keyframe lỗi được tái tạo và QC lại qua đường worker;
# START→END cascade, frame PASS độc lập được giữ; hết lượt thì chờ người
# và không render.
# =====================================================================
@pytest.mark.parametrize("low_qc_with_plan", [False, True])
def test_scenario_7_keyframe_targeted_recovery_cascade_and_operator_pause(
    tmp_path: Path, low_qc_with_plan: bool,
) -> None:
    # 1. Test targeted keyframe recovery planning logic directly
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

    # 2. Worker path: Storyboard keyframe recovery failure -> WAITING_FOR_OPERATOR, no render
    db_file = tmp_path / "scenario_7.db"
    conn = connect(db_file)
    repo = WorkflowRepository(conn)

    wf_id = "wf_scen_7"
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf_id,
        product_name="Keyframe Test Product",
        keywords=["kf"],
        project_id="proj_7",
        owner_user_id="user_test",
        max_qc_retries=3,
    )

    wf_tuple = repo.get_workflow(wf_id)
    assert wf_tuple is not None
    cur_agg, cur_ver = wf_tuple
    cur_agg["state"] = ProductVideoWorkflowState.CREATING_STORYBOARD.value
    cur_agg["project_id"] = "proj_7"
    if low_qc_with_plan:
        cur_agg["creative_policy"] = {"qc_level": "low"}
        cur_agg["scene_plan"] = {"scene_plan_id": "sp_7"}
    repo.commit_workflow(workflow_id=wf_id, aggregate_json=json.dumps(cur_agg), events=[], expected_version=cur_ver)

    artifact = {
        "schema": "storyboard_package_v2",
        "schema_version": "storyboard-package-v2",
        "storyboard_id": "sb_7",
        "version": 1,
        "package_digest": "sha256:kf_digest_7",
        "generated_keyframes": [
            {"frame_id": "scene_1-start", "image_path": str(tmp_path / "kf0.png"), "sha256": "11" * 32},
            {"frame_id": "scene_1-end", "image_path": str(tmp_path / "kf1.png"), "sha256": "22" * 32},
        ],
    }

    mock_svc = MagicMock()
    mock_svc.get_handlers_map.return_value = {
        "product_video.storyboard": lambda *_args, **_kwargs: JobSucceeded(
            artifact_ref=artifact,
            next_command={"command": "render_video"},
        )
    }

    from video_factory.qc.service import ProductVideoQCService
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        ProductVideoQCService,
        "run",
        lambda *args, **kwargs: {
            "status": "completed",
            "verdict": "fail",
            "score": 40,
            "report_id": "rep_kf_fail_7",
            "reason": "Keyframe QC failed on scene_1-start",
            "findings": {"recovery_plan": {"frames": [{"frame_id": "scene_1-start"}]}}
            if low_qc_with_plan else {},
        },
    )

    th = create_product_video_topic_handlers(
        workflow_repository=repo,
        handlers_service=mock_svc,
        provider_mode="configured",
    )

    envelope = parse_envelope(
        build_envelope(
            workflow_id=wf_id,
            topic="job.dispatch",
            idempotency_key="idemp_sb_7",
            payload={
                "job_name": "product_video.storyboard",
                "owner_user_id": "user_test",
                "project_id": "proj_7",
                "args": [{"scene_plan_id": "sp_7"}],
                "kwargs": {},
            },
        )
    )
    th["job.dispatch"](envelope)

    # Workflow aggregate transitioned cleanly to WAITING_FOR_OPERATOR with exact reason
    data_7 = repo.get_workflow(wf_id)
    assert data_7 is not None
    agg_7 = data_7[0]
    if low_qc_with_plan:
        assert agg_7["state"] == ProductVideoWorkflowState.CREATING_STORYBOARD.value
        assert agg_7["keyframe_recovery_attempt"] == 1
        assert agg_7["keyframe_recovery_plan"]["frames"] == [{"frame_id": "scene_1-start"}]
        monkeypatch.undo()
        return
    assert agg_7["state"] == ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
    assert agg_7["operator_stage"] == "keyframe"
    assert agg_7["operator_action"] == "qc_provider_retry"
    # Does NOT proceed to AwaitingStoryboardApproval or render
    assert agg_7["state"] != ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL.value
    assert "active_render_job_id" not in agg_7
    monkeypatch.undo()


# =====================================================================
# Scenario 8: Restart giữa các attempt không lặp provider call đã hoàn tất
# hoặc vượt quota.
# =====================================================================
def test_scenario_8_restart_recovery_idempotency_and_no_quota_exceed(tmp_path: Path) -> None:
    db_file = tmp_path / "scenario_8.db"
    conn = connect(db_file)
    repo = WorkflowRepository(conn)
    engine = WorkflowEngine(repo)

    wf_id = "wf_scen_8"
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf_id,
        product_name="Idempotency Product",
        keywords=["idem"],
        project_id="proj_8",
        owner_user_id="user_test",
        idempotency_key="idemp_key_123",
    )

    mock_pi = MagicMock()
    mock_pi.research.return_value = {
        "snapshot_id": "snap_idem_1",
        "persistence_status": "saved",
    }

    handlers = ProductVideoJobHandlers(
        workflow_engine=engine,
        workflow_repository=repo,
        pi_client=mock_pi,
        video_client=MagicMock(),
        provider_mode="configured",
    )

    ctx = JobContext(workflow_id=wf_id, job_id="job_idem", owner_user_id="user_test", idempotency_key="idemp_key_123", expected_version=1)

    # Call 1: Provider is called once
    res1 = handlers.research_job(ctx, product_name="Idempotency Product", keywords=["idem"])
    assert isinstance(res1, JobSucceeded)
    assert mock_pi.research.call_count == 1

    # Simulate restart by instantiating new handlers instance with same DB
    handlers_restarted = ProductVideoJobHandlers(
        workflow_engine=engine,
        workflow_repository=repo,
        pi_client=mock_pi,
        video_client=MagicMock(),
        provider_mode="configured",
    )
    # Pre-populate idempotency cache with previously saved result
    handlers_restarted._idempotency_cache[f"{wf_id}:research:v1:idemp_key_123"] = res1

    # Call 2: With same idempotency key, returns cached result without re-calling provider!
    res2 = handlers_restarted.research_job(ctx, product_name="Idempotency Product", keywords=["idem"])
    assert isinstance(res2, JobSucceeded)
    assert mock_pi.research.call_count == 1  # Still 1! Not called again


# =====================================================================
# Scenario 9: get_verified_pass_report() trả None cho FAIL/UNKNOWN dù score >=70/80/100;
# endpoint xuất video từ chối report đó.
# =====================================================================
def test_scenario_9_get_verified_pass_report_rejects_fail_unknown_with_high_score(tmp_path: Path) -> None:
    db_file = tmp_path / "scenario_9.db"
    conn = connect(db_file)
    qc_repo = QCReportRepository(conn)

    owner = "owner_test"
    proj = "proj_test"
    wf_id = "wf_scen_9"
    art_id = "video_composite_1"
    art_ver = 1
    src_sha = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"

    # 1. Report with score=95, but verdict = FAIL
    fail_report = QCReport(
        report_id="rep_fail_high_score",
        workflow_id=wf_id,
        stage=QCStage.VIDEO,
        artifact_id=art_id,
        artifact_version=art_ver,
        artifact_digest=src_sha,
        reference_digests=[],
        evaluator_name="eval_video",
        evaluator_version="1.0",
        rubric_version="1.0",
        policy_version="1.0",
        created_at=datetime.now(timezone.utc).isoformat(),
        score=95,
        verdict=QCVerdict.FAIL,
        findings={"reason": "Watermark violation despite high visual fidelity"},
        owner_user_id=owner,
        project_id=proj,
    )
    qc_repo.save_report(fail_report)

    # get_verified_pass_report must return None despite score=95!
    verified_fail = qc_repo.get_verified_pass_report(
        owner_user_id=owner,
        project_id=proj,
        workflow_id=wf_id,
        artifact_id=art_id,
        artifact_version=art_ver,
        stage=QCStage.VIDEO,
        expected_sha256=src_sha,
    )
    assert verified_fail is None

    # 2. Report with score=85, but verdict = UNKNOWN
    unknown_report = QCReport(
        report_id="rep_unknown_high_score",
        workflow_id=wf_id,
        stage=QCStage.VIDEO,
        artifact_id=art_id,
        artifact_version=art_ver,
        artifact_digest=src_sha,
        reference_digests=[],
        evaluator_name="eval_video",
        evaluator_version="1.0",
        rubric_version="1.0",
        policy_version="1.0",
        created_at=datetime.now(timezone.utc).isoformat(),
        score=85,
        verdict=QCVerdict.UNKNOWN,
        findings={"reason": "Ambiguous brand audio snippet"},
        owner_user_id=owner,
        project_id=proj,
    )
    qc_repo.save_report(unknown_report)

    verified_unknown = qc_repo.get_verified_pass_report(
        owner_user_id=owner,
        project_id=proj,
        workflow_id=wf_id,
        artifact_id=art_id,
        artifact_version=art_ver,
        stage=QCStage.VIDEO,
        expected_sha256=src_sha,
    )
    assert verified_unknown is None

    # 3. Simulate Video Export verification check
    # When get_verified_pass_report is None, export endpoint refuses with VIDEO_QC_REQUIRED
    matching_report = qc_repo.get_verified_pass_report(
        owner_user_id=owner,
        project_id=proj,
        workflow_id=wf_id,
        artifact_id=art_id,
        artifact_version=art_ver,
        stage=QCStage.VIDEO,
        expected_sha256=src_sha,
    )
    export_result = (
        {"ok": True, "status": "exported"}
        if matching_report is not None
        else {
            "ok": False,
            "status": "failed",
            "error": f"VIDEO_QC_REQUIRED: No verified PASS Video QC report bound to exact composite lineage (owner={owner}, project={proj}, workflow={wf_id}, artifact={art_id}, version={art_ver}, sha={src_sha})",
            "code": "VIDEO_QC_REQUIRED",
        }
    )
    assert export_result["ok"] is False
    assert export_result["code"] == "VIDEO_QC_REQUIRED"

    # 4. Save genuine PASS report -> now verification passes!
    pass_report = QCReport(
        report_id="rep_pass_genuine",
        workflow_id=wf_id,
        stage=QCStage.VIDEO,
        artifact_id=art_id,
        artifact_version=art_ver,
        artifact_digest=src_sha,
        reference_digests=[],
        evaluator_name="eval_video",
        evaluator_version="1.0",
        rubric_version="1.0",
        policy_version="1.0",
        created_at=datetime.now(timezone.utc).isoformat(),
        score=92,
        verdict=QCVerdict.PASS,
        findings={"compliant": True},
        owner_user_id=owner,
        project_id=proj,
    )
    qc_repo.save_report(pass_report)

    verified_pass = qc_repo.get_verified_pass_report(
        owner_user_id=owner,
        project_id=proj,
        workflow_id=wf_id,
        artifact_id=art_id,
        artifact_version=art_ver,
        stage=QCStage.VIDEO,
        expected_sha256=src_sha,
    )
    assert verified_pass is not None
    assert verified_pass.verdict == QCVerdict.PASS


# =====================================================================
# Scenario 10: Manual/legacy workflow vẫn hoạt động theo gate hợp lệ, không tự sinh PASS.
# =====================================================================
def test_scenario_10_manual_legacy_workflow_respects_gates_without_fake_pass(tmp_path: Path) -> None:
    db_file = tmp_path / "scenario_10.db"
    conn = connect(db_file)
    repo = WorkflowRepository(conn)
    engine = WorkflowEngine(repo)

    wf_id = "wf_scen_10"
    img1 = _create_unique_image(tmp_path / "legacy_1.png")
    img2 = _create_unique_image(tmp_path / "legacy_2.png")

    # Legacy exchange manifest: requires manual operator confirmation of images
    source_context_legacy = {
        "source": "hermes_product_video_exchange",
        "auto_admitted": False,
        "brand": "LegacyBrand",
        "model": "OldModel",
        "images": [
            {"asset_id": "leg_1", "absolute_path": str(img1)},
            {"asset_id": "leg_2", "absolute_path": str(img2)},
        ],
    }

    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf_id,
        product_name="Legacy Product",
        keywords=["legacy"],
        project_id="proj_10",
        owner_user_id="user_operator",
        source_context=source_context_legacy,
    )

    mock_pi = MagicMock()
    wrong_img = _create_unique_image(tmp_path / "wrong.png")
    mismatched_pack = {
        "resource_pack_id": "rp_leg_mismatch",
        "version": 1,
        "assets": [
            {"asset_id": "leg_1", "local_path": str(wrong_img)},  # wrong bytes!
            {"asset_id": "leg_2", "local_path": str(img2)},
        ],
    }
    mock_pi.build_resource_pack.return_value = mismatched_pack

    handlers = ProductVideoJobHandlers(
        workflow_engine=engine,
        workflow_repository=repo,
        pi_client=mock_pi,
        video_client=MagicMock(),
        provider_mode="configured",
    )

    # 1. If pack does not match approved images, fails closed with APPROVED_IMAGE_MISMATCH
    ctx = JobContext(workflow_id=wf_id, job_id="job_pack_10", owner_user_id="user_operator", expected_version=1)
    res_mismatch = handlers.resource_pack_job(
        ctx,
        research_result={"snapshot_id": "snap_10", "persistence_status": "saved"},
        source_context=source_context_legacy,
    )
    assert isinstance(res_mismatch, JobFailed)
    assert res_mismatch.code == "EXCHANGE_IMAGE_ADMISSION_BLOCKED"
    assert res_mismatch.message == "At least one Exchange image must be selected"

    # 2. Operator confirmation selects input images, but does NOT create fake PASS report
    # The QC repository must NOT have an inferred PASS report!
    qc_repo = QCReportRepository(conn)
    assert qc_repo.get_latest_report(workflow_id=wf_id, stage=QCStage.IMAGE) is None
