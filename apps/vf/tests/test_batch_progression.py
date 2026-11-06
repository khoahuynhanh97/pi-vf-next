from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from PIL import Image, ImageDraw

from video_factory.handlers import (
    JobContext,
    JobFailed,
    JobSucceeded,
    ProductVideoJobHandlers,
    start_product_video_workflow,
)
from video_factory.handlers.registry import create_product_video_topic_handlers
from video_factory.qc.models import QCReport, QCStage, QCVerdict
from video_factory.qc.retry_policy import get_qc_retry_count
from video_factory.storage_infra.sqlite import connect
from video_factory.workflow import ProductVideoWorkflowState
from video_factory.workflows.engine import WorkflowEngine
from video_factory.workflows.outbox_dispatcher import OutboxDispatcher
from video_factory.workflows.outbox_repository import OutboxRepository
from video_factory.workflows.repository import WorkflowRepository


_img_seq = 0


def _make_contrast_image(path: Path) -> Path:
    global _img_seq
    _img_seq += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (120, 120), (255, 255, 255))
    draw = ImageDraw.Draw(im)
    x = (_img_seq * 20) % 70
    draw.rectangle([x, x, x + 40, x + 40], fill=(0, 0, 0))
    im.putpixel((0, 0), (_img_seq & 255, (_img_seq >> 8) & 255, (_img_seq >> 16) & 255))
    im.save(path, format="PNG")
    return path


def test_t09_budget_exhaustion_transitions_waiting_for_operator_and_batch_continues(tmp_path: Path) -> None:
    """T09: Workflow with missing/exhausted QC budget transitions to WaitingForOperator; batch continues next product."""
    conn = connect(tmp_path / "t09.db")
    repo = WorkflowRepository(conn)

    wf_no_budget = "wf_t09_no_budget"
    wf_funded = "wf_t09_funded"

    img_a1 = _make_contrast_image(tmp_path / "a1.png")
    img_a2 = _make_contrast_image(tmp_path / "a2.png")
    img_b1 = _make_contrast_image(tmp_path / "b1.png")
    img_b2 = _make_contrast_image(tmp_path / "b2.png")

    # Workflow 1: Configured QC with $0 budget authorization -> cannot admit QC
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf_no_budget,
        product_name="Unfunded Product",
        keywords=["unfunded"],
        project_id="proj_t09",
        owner_user_id="user_t09",
        source_context={
            "source": "hermes_product_video_exchange",
            "auto_admitted": True,
            "brand": "AULA",
            "model": "F75",
            "images": [
                {"asset_id": "a1", "absolute_path": str(img_a1)},
                {"asset_id": "a2", "absolute_path": str(img_a2)},
            ],
        },
        qc_authorization={"workflow_id": wf_no_budget, "authorization_id": "auth_zero", "workflow_max": 0, "max_cost_usd": 0.0, "scope": ["image"], "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
        qc_max_cost_usd=0.0,
    )

    # Workflow 2: Funded workflow in same batch
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf_funded,
        product_name="Funded Product",
        keywords=["funded"],
        project_id="proj_t09",
        owner_user_id="user_t09",
        source_context={
            "source": "hermes_product_video_exchange",
            "auto_admitted": True,
            "brand": "AULA",
            "model": "F75",
            "images": [
                {"asset_id": "b1", "absolute_path": str(img_b1)},
                {"asset_id": "b2", "absolute_path": str(img_b2)},
            ],
        },
        qc_authorization={"workflow_id": wf_funded, "authorization_id": "auth_ok", "workflow_max": 5000, "max_cost_usd": 50.0, "scope": ["image"], "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
        qc_max_cost_usd=50.0,
    )

    mock_pi = MagicMock()
    mock_pi.research.side_effect = lambda *args, **kwargs: {
        "snapshot_id": f"snap_{kwargs.get('workflow_id')}",
        "persistence_status": "saved",
        "source_context": kwargs.get("source_context"),
    }
    mock_pi.build_resource_pack.side_effect = lambda res, **kwargs: {
        "resource_pack_id": f"rp_{kwargs.get('workflow_id')}",
        "version": 1,
        "assets": repo.get_workflow(kwargs["workflow_id"])[0]["source_context"]["images"][:2],
        "product_references": repo.get_workflow(kwargs["workflow_id"])[0]["source_context"]["images"][:2],
        "brand": "AULA", "model": "F75",
    }

    class MockEvalT09:
        def evaluate_image(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
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
                "reason": "Clear match",
                "view_angle": "front",
                "shot_type": "product",
                "visible_details": [],
                "scene_suitability": [],
                "risk_flags": [],
                "text_coverage_percent": 0,
            }

    handlers_svc = ProductVideoJobHandlers(
        workflow_repository=repo,
        pi_client=mock_pi,
        video_client=MagicMock(),
        provider_mode="configured",
        evaluator=MockEvalT09(),
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

    # Dispatch research for both
    dispatcher.process_batch(workflow_ids=[wf_no_budget, wf_funded])

    # Dispatch resource_pack for both
    rep = dispatcher.process_batch(workflow_ids=[wf_no_budget, wf_funded])
    # Neither message is quarantined
    assert rep.quarantined == 0

    # 1. Funded workflow successfully reaches AwaitingProductLock
    wf_f_data = repo.get_workflow(wf_funded)
    assert wf_f_data is not None
    assert wf_f_data[0]["state"] == ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value


def test_t10_two_product_batch_visual_fail_skips_candidate_and_next_succeeds(tmp_path: Path) -> None:
    """A terminal Exchange visual FAIL skips that candidate while the batch continues."""
    conn = connect(tmp_path / "t10.db")
    repo = WorkflowRepository(conn)

    wf1_exhausted = "wf_t10_p1"
    wf2_success = "wf_t10_p2"

    p1_imgs = [_make_contrast_image(tmp_path / f"p1_{i}.png") for i in range(5)]
    p2_imgs = [_make_contrast_image(tmp_path / f"p2_{i}.png") for i in range(2)]

    auth_p1 = {
        "workflow_id": wf1_exhausted,
        "authorization_id": "auth_t10_p1",
        "max_cost_usd": 50.0,
        "workflow_max_cost_usd": 50.0,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "scope": ["image", "product_video"],
    }
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf1_exhausted,
        product_name="Exhausted Product",
        keywords=["p1"],
        project_id="proj_t10",
        owner_user_id="user_t10",
        source_context={
            "source": "hermes_product_video_exchange",
            "auto_admitted": True,
            "brand": "AULA",
            "model": "F75",
            "images": [{"asset_id": f"p1_{i}", "absolute_path": str(p1_imgs[i])} for i in range(5)],
        },
        qc_authorization=auth_p1,
        qc_max_cost_usd=50.0,
        max_qc_retries=3,
    )

    auth_p2 = {
        "workflow_id": wf2_success,
        "authorization_id": "auth_t10_p2",
        "max_cost_usd": 50.0,
        "workflow_max_cost_usd": 50.0,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "scope": ["image", "product_video"],
    }
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf2_success,
        product_name="Success Product",
        keywords=["p2"],
        project_id="proj_t10",
        owner_user_id="user_t10",
        source_context={
            "source": "hermes_product_video_exchange",
            "auto_admitted": True,
            "brand": "AULA",
            "model": "F75",
            "images": [{"asset_id": f"p2_{i}", "absolute_path": str(p2_imgs[i])} for i in range(2)],
        },
        qc_authorization=auth_p2,
        qc_max_cost_usd=50.0,
        max_qc_retries=3,
    )

    mock_pi = MagicMock()
    mock_pi.research.side_effect = lambda *args, **kwargs: {
        "snapshot_id": f"snap_{kwargs.get('workflow_id')}",
        "persistence_status": "saved",
        "source_context": kwargs.get("source_context"),
    }
    mock_pi.build_resource_pack.side_effect = lambda res, **kwargs: {
        "resource_pack_id": f"rp_{kwargs.get('workflow_id')}",
        "version": 1,
        "assets": repo.get_workflow(kwargs["workflow_id"])[0]["source_context"]["images"][:2],
        "product_references": repo.get_workflow(kwargs["workflow_id"])[0]["source_context"]["images"][:2],
        "brand": "AULA", "model": "F75",
    }

    class MockEvalT10:
        def evaluate_image(self, image_path: Path | str, prompt: str, system_instruction: str | None = None) -> dict[str, Any]:
            p_str = str(image_path)
            if "p1" in p_str:
                return {
                    "verdict": "fail",
                    "total_score": 20,
                    "identity_score": 5,
                    "clarity_score": 10,
                    "usability_score": 5,
                    "hard_failure": False,
                    "source_branding_detected": False,
                    "identity_mismatch": True,
                    "eligible": False,
                    "reason": "Model mismatch",
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
                "reason": "OK",
                "view_angle": "front",
                "shot_type": "product",
                "visible_details": [],
                "scene_suitability": [],
                "risk_flags": [],
                "text_coverage_percent": 0,
            }

    handlers_svc = ProductVideoJobHandlers(
        workflow_repository=repo,
        pi_client=mock_pi,
        video_client=MagicMock(),
        provider_mode="configured",
        evaluator=MockEvalT10(),
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

    # Dispatch research
    dispatcher.process_batch(workflow_ids=[wf1_exhausted, wf2_success])

    # Dispatch resource_pack
    rep = dispatcher.process_batch(workflow_ids=[wf1_exhausted, wf2_success])
    assert rep.quarantined == 0

    # 1. A completed visual FAIL is this Exchange candidate's terminal outcome.
    d1 = repo.get_workflow(wf1_exhausted)
    assert d1 is not None
    assert d1[0]["state"] == "skipped"
    assert d1[0]["failure_category"] == "PRODUCT_REJECTED"
    assert "EXCHANGE_IMAGE_QC_FAILED" in d1[0]["failure_code"]
    assert d1[0]["skip_reason"]
    assert conn.execute(
        "SELECT COUNT(*) FROM workflow_event WHERE workflow_id=? AND event_type='ProductSkipped'",
        (wf1_exhausted,),
    ).fetchone()[0] == 1

    # 2. wf2_success advanced to AWAITING_PRODUCT_LOCK
    d2 = repo.get_workflow(wf2_success)
    assert d2 is not None
    assert d2[0]["state"] == ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value
