from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from PIL import Image

from video_factory.qc.models import QCStage
from video_factory.qc.service import ProductVideoQCService
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.repository import WorkflowRepository


def _passing_response() -> dict[str, object]:
    return {
        "identity_score": 55,
        "clarity_score": 23,
        "usability_score": 12,
        "total_score": 90,
        "verdict": "pass",
        "hard_failure": False,
        "source_branding_detected": False,
        "identity_mismatch": False,
        "eligible": True,
        "reason": "one usable source image",
        "view_angle": "front",
        "shot_type": "product",
        "visible_details": ["logo"],
        "scene_suitability": ["hero"],
        "risk_flags": [],
        "text_coverage_percent": 0,
    }


def test_one_usable_image_is_enough_and_records_usage_without_reservation(tmp_path):
    image = tmp_path / "only-reference.png"
    Image.new("RGB", (8, 8), color="white").save(image)
    conn = connect(tmp_path / "qc-accounting.db")
    evaluator = MagicMock(
        evaluator_name="test-vision",
        evaluator_version="1",
        model="test/vision",
    )
    evaluator.evaluate_image.return_value = {
        **_passing_response(),
        "route_receipt": {
            "attempt_id": "router-qc-image-1",
            "cost_status": "recorded",
            "cost_usd": 0.04,
        },
    }
    service = ProductVideoQCService(conn, evaluator=evaluator)
    resource_pack = {
        "resource_pack_id": "rp-one-image",
        "version": 1,
        "owner_user_id": "owner",
        "project_id": "project",
        "brand": "Ugreen",
        "model": "65827",
        "assets": [{"asset_id": "only", "local_path": str(image)}],
    }
    authorization = {
        "workflow_id": "wf-one-image",
        "authorization_id": "grant-one-image",
        "scope": ["image"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "max_cost_usd": 0.20,
        "campaign_max_cost_usd": 1.00,
    }

    result = service.select_image_references(
        "wf-one-image",
        resource_pack=resource_pack,
        source_context={"brand": "Ugreen", "model": "65827"},
        provider_mode="configured",
        authorization=authorization,
        max_cost_usd=0.10,
    )

    assert result["status"] == "completed"
    assert result["selected_count"] == 1
    assert evaluator.evaluate_image.call_count == 1
    assert conn.execute("SELECT COUNT(*) FROM budget_reservation").fetchone()[0] == 0
    retry = conn.execute(
        "SELECT cost_usd FROM workflow_qc_retries WHERE workflow_id = ?",
        ("wf-one-image",),
    ).fetchone()
    assert retry["cost_usd"] == 0.04
    conn.close()


def test_semantic_unknown_records_unknown_cost_without_money_reservation(tmp_path):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    Image.new("RGB", (8, 8), color="white").save(first)
    Image.new("RGB", (8, 8), color="black").save(second)
    conn = connect(tmp_path / "qc-semantic-unknown.db")
    WorkflowRepository(conn).commit_workflow(
        "wf-semantic-unknown",
        '{"workflow_id":"wf-semantic-unknown","project_id":"project","owner_user_id":"owner","state":"AwaitingProductLock","source_context":{"brand":"Ugreen","model":"65827"},"resource_pack":{"resource_pack_id":"rp-unknown","version":1,"owner_user_id":"owner","project_id":"project","brand":"Ugreen","model":"65827","assets_mapping":{"a1":"' + str(first).replace("\\", "\\\\") + '","a2":"' + str(second).replace("\\", "\\\\") + '"}}}',
        [],
        expected_version=None,
    )
    evaluator = MagicMock(evaluator_name="test-vision", evaluator_version="1", model="test/vision")
    evaluator.evaluate_image.return_value = {
        **_passing_response(), "verdict": "unknown", "eligible": False, "reason": "model cannot determine identity",
    }
    authorization = {
        "workflow_id": "wf-semantic-unknown", "authorization_id": "grant-semantic-unknown",
        "scope": ["image"], "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "max_cost_usd": 0.20,
    }

    result = ProductVideoQCService(conn, evaluator=evaluator).run(
        "wf-semantic-unknown", stage=QCStage.IMAGE, provider_mode="configured", authorize_cost=True,
        max_cost_usd=0.10, authorization=authorization,
    )

    assert result["report"].verdict.value == "unknown"
    assert result["report"].findings["usage_status"] == "consumed"
    assert conn.execute("SELECT COUNT(*) FROM budget_reservation").fetchone()[0] == 0
    attempt = conn.execute(
        "SELECT status,cost_status,actual_cost,reservation_id FROM product_video_operation_attempt WHERE workflow_id=?",
        ("wf-semantic-unknown",),
    ).fetchone()
    assert attempt["status"] == "semantic_unknown"
    assert attempt["cost_status"] == "unknown"
    assert attempt["actual_cost"] is None
    assert attempt["reservation_id"] is None
    conn.close()
