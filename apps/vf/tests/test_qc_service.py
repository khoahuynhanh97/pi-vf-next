import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image

from video_factory.qc.models import QCStage, QCVerdict
from video_factory.qc.evaluator import QCResponseParseError, QCTechnicalError
from video_factory.qc.service import (
    ProductVideoQCService,
    _recorded_route_cost,
    _file_url_to_path,
    _reference_url_to_path,
)
from video_factory.cli import main
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.budgets import BudgetError, BudgetPolicy, BudgetRepository
from video_factory.workflows.repository import WorkflowRepository


def _seed_image_workflow(tmp_path):
    image_one = tmp_path / "one.png"
    image_two = tmp_path / "two.png"
    Image.new("RGB", (8, 8), color="red").save(image_one)
    Image.new("RGB", (8, 8), color="blue").save(image_two)
    conn = connect(tmp_path / "qc_service.db")
    WorkflowRepository(conn).commit_workflow(
        "wf-qc-service",
        json.dumps({
            "workflow_id": "wf-qc-service",
            "project_id": "proj-wf-qc-service",
            "owner_user_id": "local-owner",
            "state": "AwaitingProductLock",
            "source_context": {"brand": "AULA", "model": "F75"},
            "resource_pack": {
                "resource_pack_id": "rp-qc-service",
                "version": 1,
                "owner_user_id": "local-owner",
                "project_id": "proj-wf-qc-service",
                "brand": "AULA",
                "model": "F75",
                "assets_mapping": {"a1": str(image_one), "a2": str(image_two)},
            },
            "latest_artifact": {
                "schema": "product_resource_pack",
                "resource_pack_id": "rp-qc-service",
                "version": 1,
                "owner_user_id": "local-owner",
                "project_id": "proj-wf-qc-service",
                "assets_mapping": {"a1": str(image_one), "a2": str(image_two)},
            },
        }),
        [],
        expected_version=None,
    )
    return conn


def test_qc_reference_path_allows_configured_exchange_root_only(tmp_path, monkeypatch):
    from video_factory.storage import layout

    exchange = tmp_path / "approved-exchange"
    exchange.mkdir()
    image = exchange / "reference.png"
    image.write_bytes(b"image")
    unrelated = tmp_path / "unrelated.png"
    unrelated.write_bytes(b"image")
    monkeypatch.setattr(layout, "get_trusted_exchange_roots", lambda: (exchange,))
    monkeypatch.setattr(layout, "get_workspace_layout", lambda: layout.ProductVideoWorkspaceLayout(tmp_path / "workspace"))

    assert _reference_url_to_path(image) == image.resolve()
    with pytest.raises(ValueError, match="CONTAINMENT_VIOLATION"):
        _reference_url_to_path(unrelated)
    with pytest.raises(ValueError, match="CONTAINMENT_VIOLATION"):
        _file_url_to_path(image)


def test_trusted_exchange_roots_include_explicit_inbox_environment(tmp_path, monkeypatch):
    from PIL import Image

    from video_factory.storage.layout import ProductVideoWorkspaceLayout, get_trusted_exchange_roots

    inbox = tmp_path / "selected-inbox"
    inbox.mkdir()
    monkeypatch.setenv("HERMES_EXCHANGE_INBOX", str(inbox))
    source = inbox / "reference.png"
    Image.new("RGB", (8, 8), "red").save(source)

    roots = get_trusted_exchange_roots()
    assert inbox.resolve() in roots
    copied, sha, size = ProductVideoWorkspaceLayout(tmp_path / "workspace").ingest_reference(
        "project_env_inbox", source
    )
    assert copied.is_file()
    assert len(sha) == 64
    assert size > 0


def test_configured_qc_runs_without_money_authorization_or_reservation(tmp_path):
    conn = _seed_image_workflow(tmp_path)
    evaluator = MagicMock()
    evaluator.evaluator_name = "configured_test_vision"
    evaluator.evaluator_version = "1"
    evaluator.model = "test/vision"
    evaluator.fake_mode = False
    evaluator.evaluate_image.return_value = {
        **_reference_profile(), "identity_score": 55, "clarity_score": 23,
        "usability_score": 12, "total_score": 90, "verdict": "pass",
        "eligible": True, "hard_failure": False,
        "source_branding_detected": False, "identity_mismatch": False,
        "reason": "clean",
    }
    result = ProductVideoQCService(conn, evaluator=evaluator).run(
        "wf-qc-service", QCStage.IMAGE, provider_mode="configured"
    )
    assert result["status"] == "completed"
    assert result["report"].verdict == QCVerdict.PASS
    assert evaluator.evaluate_image.call_count == 2
    assert conn.execute("SELECT COUNT(*) FROM budget_reservation").fetchone()[0] == 0
    attempt = conn.execute(
        "SELECT actual_cost, cost_status FROM product_video_operation_attempt WHERE workflow_id=?",
        ("wf-qc-service",),
    ).fetchone()
    assert attempt["actual_cost"] is None
    assert attempt["cost_status"] == "unknown"
    conn.close()


def test_keyframe_qc_requires_approved_script_before_provider_call(tmp_path, monkeypatch):
    keyframe = tmp_path / "keyframe-no-script.png"
    reference = tmp_path / "reference-no-script.png"
    Image.new("RGB", (16, 16), color="red").save(keyframe)
    Image.new("RGB", (16, 16), color="blue").save(reference)
    conn = connect(tmp_path / "keyframe-no-script.db")
    WorkflowRepository(conn).commit_workflow(
        "wf-keyframe-no-script",
        json.dumps({
            "workflow_id": "wf-keyframe-no-script",
            "project_id": "proj-keyframe-no-script",
            "owner_user_id": "operator-1",
            "state": "CreatingStoryboard",
            "source_context": {"brand": "Brand", "model": "Model"},
            "resource_pack": {"brand": "Brand", "model": "Model"},
        }),
        [],
        expected_version=None,
    )
    package = {
        "storyboard_id": "sb-keyframe-no-script",
        "version": 1,
        "package_digest": "sha256:" + "e" * 64,
        "owner_user_id": "operator-1",
        "project_id": "proj-keyframe-no-script",
        "script": {"scenes": []},
        "generated_keyframes": [{
            "frame_id": "scene-1-start",
            "scene_id": "scene-1",
            "image_path": str(keyframe),
            "sha256": __import__("hashlib").sha256(keyframe.read_bytes()).hexdigest(),
            "prompt": "Show the product on a table.",
            "reference_image_paths": [str(reference)],
        }],
    }
    evaluator = MagicMock()
    evaluator.evaluator_name = "configured_test_vision"
    evaluator.evaluator_version = "1"
    evaluator.model = "test/vision"
    evaluator.fake_mode = False
    monkeypatch.setattr(
        "video_factory.qc.service._reference_url_to_path",
        lambda value: Path(value),
    )

    result = ProductVideoQCService(conn, evaluator=evaluator).run(
        "wf-keyframe-no-script",
        QCStage.KEYFRAME,
        provider_mode="configured",
        artifact_override=package,
    )

    assert result["status"] == "pending"
    assert result["code"] == "QC_INPUT_UNVERIFIABLE"
    assert evaluator.evaluate_multiple_images.call_count == 0
    attempt = conn.execute(
        "SELECT status,cost_status FROM product_video_operation_attempt WHERE workflow_id=?",
        ("wf-keyframe-no-script",),
    ).fetchone()
    assert tuple(attempt) == ("pre_send_fail", "not_sent")
    conn.close()


@pytest.mark.parametrize("reference_state", ["missing", "changed"])
def test_keyframe_reference_contract_error_stops_before_provider_and_is_reported(
    tmp_path, monkeypatch, reference_state: str,
):
    import hashlib

    keyframe = tmp_path / f"contract-{reference_state}-keyframe.png"
    reference = tmp_path / f"contract-{reference_state}-reference.png"
    Image.new("RGB", (16, 16), color="red").save(keyframe)
    Image.new("RGB", (16, 16), color="blue").save(reference)
    approved_reference_sha = hashlib.sha256(reference.read_bytes()).hexdigest()
    if reference_state == "changed":
        Image.new("RGB", (16, 16), color="green").save(reference)
    else:
        reference.unlink()

    conn = _seed_image_workflow(tmp_path)
    package = {
        "storyboard_id": f"sb-reference-{reference_state}",
        "version": 1,
        "package_digest": "sha256:" + "9" * 64,
        "owner_user_id": "local-owner",
        "project_id": "proj-wf-qc-service",
        "script": {"scenes": [{"scene_id": "scene-1", "subject_action": "Show the product"}]},
        "generated_keyframes": [{
            "frame_id": "scene-1-start",
            "scene_id": "scene-1",
            "image_path": str(keyframe),
            "sha256": hashlib.sha256(keyframe.read_bytes()).hexdigest(),
            "prompt": "Show the product clearly.",
            "reference_image_paths": [str(reference)],
            "reference_sha256s": [approved_reference_sha],
        }],
    }
    evaluator = MagicMock()
    evaluator.evaluator_name = "configured_test_vision"
    evaluator.evaluator_version = "1"
    evaluator.model = "test/vision"
    evaluator.fake_mode = False
    monkeypatch.setattr(
        "video_factory.qc.service._reference_url_to_path",
        lambda value: Path(value),
    )

    result = ProductVideoQCService(conn, evaluator=evaluator).run(
        "wf-qc-service",
        QCStage.KEYFRAME,
        provider_mode="configured",
        artifact_override=package,
    )

    assert result["status"] == "pending"
    assert result["code"] == "QC_INPUT_UNVERIFIABLE"
    assert evaluator.evaluate_multiple_images.call_count == 0
    attempt = conn.execute(
        "SELECT status,cost_status FROM product_video_operation_attempt WHERE workflow_id=? AND capability_id=?",
        ("wf-qc-service", "product_video.qc.keyframe"),
    ).fetchone()
    assert tuple(attempt) == ("pre_send_fail", "not_sent")
    conn.close()


def test_combo_fallback_members_are_one_router_request_not_qc_retries(tmp_path):
    conn = _seed_image_workflow(tmp_path)
    evaluator = MagicMock()
    evaluator.evaluator_name = "nine_router_vision_qc"
    evaluator.evaluator_version = "1"
    evaluator.model = "Vision-QC-Check"
    evaluator.fake_mode = False
    evaluator.evaluate_image.side_effect = [
        {
            **_reference_profile(), "identity_score": 55, "clarity_score": 23,
            "usability_score": 12, "total_score": 90, "verdict": "pass",
            "eligible": True, "hard_failure": False,
            "source_branding_detected": False, "identity_mismatch": False,
            "reason": "clean",
            "route_receipt": {
                "route": "9router", "attempt_id": "request-1", "fallback_used": True,
                "fallback_members_attempted": 6, "upstream_provider": "gemini",
                "upstream_model": "gemini/member-6", "cost_status": "recorded", "cost_usd": 0.01,
            },
        },
        {
            **_reference_profile(), "identity_score": 55, "clarity_score": 23,
            "usability_score": 12, "total_score": 90, "verdict": "pass",
            "eligible": True, "hard_failure": False,
            "source_branding_detected": False, "identity_mismatch": False,
            "reason": "clean",
            "route_receipt": {
                "route": "9router", "attempt_id": "request-2", "fallback_used": True,
                "fallback_members_attempted": 6, "upstream_provider": "gemini",
                "upstream_model": "gemini/member-6", "cost_status": "recorded", "cost_usd": 0.02,
            },
        },
    ]

    result = ProductVideoQCService(conn, evaluator=evaluator).run(
        "wf-qc-service", QCStage.IMAGE, provider_mode="configured"
    )

    assert result["status"] == "completed"
    assert evaluator.evaluate_image.call_count == 2
    attempts = conn.execute(
        "SELECT attempt_number, status, actual_model, actual_cost_usd FROM product_video_operation_attempt WHERE workflow_id=?",
        ("wf-qc-service",),
    ).fetchall()
    assert len(attempts) == 1
    assert attempts[0]["attempt_number"] == 1
    assert attempts[0]["status"] == "pass"
    assert attempts[0]["actual_model"] == "gemini/member-6"
    assert attempts[0]["actual_cost_usd"] == pytest.approx(0.03)
    conn.close()


def test_legacy_operation_attempt_cost_check_migrates_and_preserves_rows(tmp_path):
    db_path = tmp_path / "legacy-attempt.db"
    raw = sqlite3.connect(db_path)
    raw.execute(
        """CREATE TABLE product_video_operation_attempt (
            attempt_id TEXT PRIMARY KEY,
            authorization_id TEXT NOT NULL,
            campaign_id TEXT NOT NULL,
            owner_user_id TEXT NOT NULL,
            workflow_id TEXT NOT NULL,
            capability_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL CHECK(attempt_number > 0),
            input_fingerprint TEXT NOT NULL,
            estimated_cost INTEGER NOT NULL CHECK(estimated_cost > 0),
            reservation_id TEXT,
            provider_operation_id TEXT,
            status TEXT NOT NULL,
            actual_cost INTEGER,
            cost_status TEXT NOT NULL DEFAULT 'unknown',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(authorization_id, workflow_id, capability_id, attempt_number)
        )"""
    )
    raw.execute(
        "INSERT INTO product_video_operation_attempt VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("old-attempt", "old-auth", "old-campaign", "owner", "wf-old", "render", 1,
         "fingerprint", 25, "reservation", "provider-op", "pass", 25, "recorded", "now", "now"),
    )
    raw.commit()
    raw.close()

    migrated = connect(db_path)
    old = migrated.execute(
        "SELECT estimated_cost, reservation_id, status FROM product_video_operation_attempt WHERE attempt_id='old-attempt'"
    ).fetchone()
    assert (old["estimated_cost"], old["reservation_id"], old["status"]) == (25, "reservation", "pass")
    migrated.execute(
        """INSERT INTO product_video_operation_attempt (
            attempt_id, authorization_id, campaign_id, owner_user_id, workflow_id, capability_id,
            attempt_number, input_fingerprint, estimated_cost, reservation_id, status, created_at, updated_at
        ) VALUES ('new-qc', 'qc-unbudgeted:wf-new', 'qc-unbudgeted', 'owner', 'wf-new', 'product_video.qc.image', 1, 'hash', NULL, NULL, 'claimed', 'now', 'now')"""
    )
    migrated.commit()
    assert migrated.execute(
        "SELECT estimated_cost FROM product_video_operation_attempt WHERE attempt_id='new-qc'"
    ).fetchone()["estimated_cost"] is None
    migrated.close()


def test_configured_image_selector_runs_without_authorization_or_budget_reservation(tmp_path):
    conn = _seed_image_workflow(tmp_path)
    evaluator = MagicMock()
    evaluator.evaluator_name = "configured_test_vision"
    evaluator.evaluator_version = "1"
    evaluator.model = "test/vision"
    evaluator.fake_mode = False
    evaluator.evaluate_image.return_value = {
        **_reference_profile(), "identity_score": 55, "clarity_score": 23,
        "usability_score": 12, "total_score": 90, "verdict": "pass",
        "eligible": True, "hard_failure": False,
        "source_branding_detected": False, "identity_mismatch": False,
        "reason": "clean",
    }
    aggregate = WorkflowRepository(conn).get_workflow("wf-qc-service")[0]
    result = ProductVideoQCService(conn, evaluator=evaluator).select_image_references(
        workflow_id="wf-qc-service",
        resource_pack=aggregate["resource_pack"],
        source_context=aggregate["source_context"],
        provider_mode="configured",
        max_recoveries=3,
    )
    assert result["status"] == "completed"
    assert result["selected_count"] == 1
    assert evaluator.evaluate_image.call_count == 1
    assert conn.execute("SELECT COUNT(*) FROM budget_reservation").fetchone()[0] == 0
    assert result["report"].cost_usd is None
    assert result["report"].findings["cost_status"] == "unknown"
    conn.close()


def test_image_qc_quota_rejection_does_not_create_money_reservation(tmp_path):
    from video_factory.qc.retry_policy import claim_qc_attempt

    conn = _seed_image_workflow(tmp_path)
    for index in range(4):
        claim_qc_attempt(
            conn,
            workflow_id="wf-qc-service",
            retry_type="source_image_qc",
            item_id="aula:f75",
            input_hash=f"previous-{index}",
            operation_id=f"previous-operation-{index}",
            max_recoveries=3,
        )
    evaluator = MagicMock()
    evaluator.evaluator_name = "offline_configured_vision"
    evaluator.evaluator_version = "1"
    evaluator.model = "test/vision"
    evaluator.fake_mode = False
    pack = WorkflowRepository(conn).get_workflow("wf-qc-service")[0]["resource_pack"]
    result = ProductVideoQCService(conn, evaluator=evaluator).select_image_references(
        "wf-qc-service", resource_pack=pack,
        source_context={"brand": "AULA", "model": "F75"},
        provider_mode="configured", max_recoveries=3,
    )

    assert result["code"] == "QC_QUOTA_EXHAUSTED"
    evaluator.evaluate_image.assert_not_called()
    assert conn.execute("SELECT COUNT(*) FROM budget_reservation").fetchone()[0] == 0
    conn.close()


def test_candidate_quota_race_keeps_cost_of_images_already_evaluated(tmp_path, monkeypatch):
    from video_factory.qc.retry_policy import QCQuotaExhaustedError
    import video_factory.qc.selection as selection

    conn = _seed_image_workflow(tmp_path)
    candidate = tmp_path / "candidate.png"
    Image.new("RGB", (8, 8), color="green").save(candidate)
    evaluator = MagicMock()
    evaluator.evaluator_name = "offline_configured_vision"
    evaluator.evaluator_version = "1"
    evaluator.model = "test/vision"
    evaluator.fake_mode = False
    evaluator.evaluate_image.return_value = {
        **_reference_profile(), "identity_score": 5, "clarity_score": 10,
        "usability_score": 5, "total_score": 20, "verdict": "fail",
        "eligible": False, "hard_failure": True,
        "source_branding_detected": False, "identity_mismatch": True,
        "reason": "wrong product",
    }

    def quota_race(*args, **kwargs):
        raise QCQuotaExhaustedError(
            "quota race", workflow_id="wf-qc-service",
            retry_type="source_image_qc", item_id="aula:f75",
            attempts_used=4, max_allowed=4,
        )

    monkeypatch.setattr(selection, "claim_qc_attempt", quota_race)
    authorization = {
        "workflow_id": "wf-qc-service", "run_id": "candidate-quota-grant",
        "scope": ["image"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "max_cost_usd": 1.0,
    }
    pack = WorkflowRepository(conn).get_workflow("wf-qc-service")[0]["resource_pack"]
    result = ProductVideoQCService(conn, evaluator=evaluator).select_image_references(
        "wf-qc-service", resource_pack=pack,
        source_context={
            "brand": "AULA", "model": "F75",
            "images": [{"asset_id": "candidate", "absolute_path": str(candidate)}],
        },
        provider_mode="configured", authorization=authorization,
        max_cost_usd=0.50,
    )

    assert result["code"] == "IMAGE_QC_FAILED"
    assert evaluator.evaluate_image.call_count == 2
    assert conn.execute("SELECT COUNT(*) FROM budget_reservation").fetchone()[0] == 0
    conn.close()


def test_fake_qc_service_runs_real_image_qc_entrypoint_and_persists_report(tmp_path):
    conn = _seed_image_workflow(tmp_path)
    evaluator = MagicMock()
    evaluator.evaluator_name = "offline_test_vision"
    evaluator.evaluator_version = "1.0"
    evaluator.evaluate_image.return_value = {
        **_reference_profile(),
        "identity_score": 55,
        "clarity_score": 23,
        "usability_score": 12,
        "total_score": 90,
        "verdict": "pass",
        "eligible": True,
        "hard_failure": False,
        "source_branding_detected": False,
        "identity_mismatch": False,
        "reason": "clean",
    }
    result = ProductVideoQCService(conn, evaluator=evaluator).run(
        "wf-qc-service", QCStage.IMAGE, provider_mode="fake"
    )
    assert result["status"] == "completed"
    assert result["report"].verdict == QCVerdict.PASS
    assert result["eligible_count"] == 2
    assert result["report_id"]
    assert evaluator.evaluate_image.call_count == 2
    conn.close()


def test_cli_qc_uses_service_and_does_not_approve_gate(tmp_path, capsys):
    conn = _seed_image_workflow(tmp_path)
    conn.close()
    assert main([
        "--db-path", str(tmp_path / "qc_service.db"),
        "qc", "wf-qc-service", "--stage", "image", "--provider-mode", "fake",
    ]) == 0
    out = capsys.readouterr().out
    assert "QC status:" in out
    assert "QC report ID:" in out
    assert "Eligible references: 2" in out


def test_cli_qc_exchange_filters_to_operator_approved_sha_without_start(tmp_path, capsys):
    import hashlib

    conn = _seed_image_workflow(tmp_path)
    first = tmp_path / "one.png"
    second = tmp_path / "two.png"
    extras = [tmp_path / "extra-a.png", tmp_path / "extra-b.png"]
    for path, color in zip(extras, ("green", "yellow")):
        Image.new("RGB", (8, 8), color=color).save(path)

    approved = [first, second]
    aggregate, version = WorkflowRepository(conn).get_workflow("wf-qc-service")
    aggregate["source_context"] = {
        "source": "hermes_product_video_exchange",
        "brand": "AULA",
        "model": "F75",
        "input_review": {
            "confirmed_image_asset_ids": [
                hashlib.sha256(path.read_bytes()).hexdigest() for path in approved
            ],
        },
        "images": [
            {
                "asset_id": hashlib.sha256(path.read_bytes()).hexdigest(),
                "absolute_path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in approved
        ],
    }
    aggregate["resource_pack"]["assets_mapping"] = [
        {
            "asset_id": hashlib.sha256(path.read_bytes()).hexdigest(),
            "local_path": str(path),
        }
        for path in [*approved, *extras]
    ]
    WorkflowRepository(conn).commit_workflow(
        "wf-qc-service", json.dumps(aggregate), [], expected_version=version,
    )
    before_workflows = conn.execute("SELECT COUNT(*) FROM workflow").fetchone()[0]
    conn.close()

    assert main([
        "--db-path", str(tmp_path / "qc_service.db"),
        "qc", "wf-qc-service", "--stage", "image", "--provider-mode", "fake",
    ]) == 0
    output = capsys.readouterr().out
    assert "Eligible references: 2" in output

    conn = connect(tmp_path / "qc_service.db")
    assert conn.execute("SELECT COUNT(*) FROM workflow").fetchone()[0] == before_workflows
    stored = WorkflowRepository(conn).get_workflow("wf-qc-service")[0]["resource_pack"]
    assert {item["asset_id"] for item in stored["assets"]} == {
        hashlib.sha256(path.read_bytes()).hexdigest() for path in approved
    }
    assert len(stored["assets"]) == 2
    conn.close()


def test_configured_qc_requires_scoped_expiring_authorization_and_reuses_claim(tmp_path):
    conn = _seed_image_workflow(tmp_path)
    evaluator = MagicMock()
    evaluator.evaluator_name = "configured_test_vision"
    evaluator.evaluator_version = "2"
    evaluator.model = "test/vision"
    evaluator.fake_mode = False
    evaluator.evaluate_image.return_value = {
        **_reference_profile(),
        "identity_score": 55,
        "clarity_score": 23,
        "usability_score": 12,
        "total_score": 90,
        "verdict": "pass",
        "eligible": True,
        "hard_failure": False,
        "source_branding_detected": False,
        "identity_mismatch": False,
        "reason": "clean",
    }
    auth = {
        "workflow_id": "wf-qc-service",
        "run_id": "run-qc-1",
        "scope": ["image"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "max_cost_usd": 0.30,
    }
    service = ProductVideoQCService(
        conn,
        evaluator=evaluator,
        budget_policy=BudgetPolicy(max_total=100, per_channel={"video_factory": 100}),
    )
    result = service.run(
        "wf-qc-service",
        QCStage.IMAGE,
        provider_mode="configured",
        authorize_cost=True,
        max_cost_usd=0.10,
        authorization=auth,
        attempt_id="run-qc-1:image:1",
    )
    assert result["status"] == "completed"
    assert evaluator.evaluate_image.call_count == 2
    again = service.run(
        "wf-qc-service",
        QCStage.IMAGE,
        provider_mode="configured",
        authorize_cost=True,
        max_cost_usd=0.10,
        authorization=auth,
        attempt_id="run-qc-1:image:1",
    )
    assert again["report_id"] == result["report_id"]
    assert evaluator.evaluate_image.call_count == 2
    conn.close()


def test_invalid_schema_is_consumed_and_can_retry_within_durable_limit(tmp_path):
    conn = _seed_image_workflow(tmp_path)
    evaluator = MagicMock()
    evaluator.evaluator_name = "configured_test_vision"
    evaluator.evaluator_version = "2"
    evaluator.model = "test/vision"
    evaluator.fake_mode = False
    passing = {
        **_reference_profile(),
        "identity_score": 55,
        "clarity_score": 23,
        "usability_score": 12,
        "total_score": 90,
        "verdict": "pass",
        "eligible": True,
        "hard_failure": False,
        "source_branding_detected": False,
        "identity_mismatch": False,
        "reason": "clean",
    }
    evaluator.evaluate_image.side_effect = [
        QCResponseParseError('{"identity_score": 55,', "length", "truncated"),
        QCResponseParseError('{"identity_score": 55,', "length", "truncated"),
        passing,
        passing,
    ]
    service = ProductVideoQCService(
        conn,
        evaluator=evaluator,
        budget_policy=BudgetPolicy(max_total=100, per_channel={"video_factory": 100}),
    )
    authorization = {
        "workflow_id": "wf-qc-service",
        "run_id": "run-qc-grant",
        "scope": ["image"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "max_cost_usd": 0.30,
    }
    first = service.run(
        "wf-qc-service", QCStage.IMAGE, provider_mode="configured",
        authorize_cost=True, max_cost_usd=0.10, authorization=authorization,
    )
    assert first["status"] == "pending"
    assert first["code"] == "QC_RESPONSE_INVALID"
    assert first["report"].verdict == QCVerdict.UNKNOWN

    retried = service.run(
        "wf-qc-service", QCStage.IMAGE, provider_mode="configured",
        authorize_cost=True, max_cost_usd=0.10, authorization=authorization,
    )
    assert retried["status"] == "pending"
    assert retried["code"] == "QC_RESPONSE_INVALID"
    assert retried["report"].findings["usage_status"] == "consumed"
    assert evaluator.evaluate_image.call_count == 2
    attempts = conn.execute(
        "SELECT attempt_number, status FROM product_video_operation_attempt ORDER BY attempt_number"
    ).fetchall()
    assert [(row["attempt_number"], row["status"]) for row in attempts] == [(1, "response_invalid"), (2, "response_invalid")]
    conn.close()


def test_legacy_unlimited_flag_is_accepted_but_qc_needs_no_money_cap(tmp_path):
    conn = _seed_image_workflow(tmp_path)
    evaluator = MagicMock()
    evaluator.evaluator_name = "configured_test_vision"
    evaluator.evaluator_version = "2"
    evaluator.model = "test/vision"
    evaluator.fake_mode = False
    evaluator.evaluate_image.return_value = {
        **_reference_profile(), "identity_score": 55, "clarity_score": 23,
        "usability_score": 12, "total_score": 90, "verdict": "pass",
        "eligible": True, "hard_failure": False,
        "source_branding_detected": False, "identity_mismatch": False,
        "reason": "clean",
    }
    service = ProductVideoQCService(
        conn, evaluator=evaluator,
        budget_policy=BudgetPolicy(max_total=1, per_channel={"video_factory": 1}),
    )
    authorization = {
        "workflow_id": "wf-qc-service", "run_id": "unlimited-grant",
        "scope": ["image"], "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "qc_budget_mode": "unlimited",
    }

    result = service.run(
        "wf-qc-service", QCStage.IMAGE, provider_mode="configured",
        authorize_cost=True, max_cost_usd=None, authorization=authorization,
    )

    assert result["status"] == "completed"
    assert result["report"].verdict == QCVerdict.PASS
    assert evaluator.evaluate_image.call_count == 2
    attempt = conn.execute(
        "SELECT status, actual_cost, authorization_id FROM product_video_operation_attempt WHERE workflow_id='wf-qc-service'"
    ).fetchone()
    assert attempt["status"] == "pass"
    assert attempt["actual_cost"] is None
    assert attempt["authorization_id"] == "qc-unbudgeted:wf-qc-service"
    assert conn.execute("SELECT COUNT(*) FROM budget_reservation").fetchone()[0] == 0
    assert result["report"].findings["cost_status"] == "unknown"
    conn.close()


def test_legacy_qc_authorization_scope_does_not_gate_configured_qc(tmp_path):
    conn = _seed_image_workflow(tmp_path)
    evaluator = MagicMock()
    evaluator.evaluator_name = "configured_test_vision"
    evaluator.evaluator_version = "2"
    evaluator.model = "test/vision"
    evaluator.fake_mode = False
    evaluator.evaluate_image.return_value = {
        **_reference_profile(), "identity_score": 55, "clarity_score": 23,
        "usability_score": 12, "total_score": 90, "verdict": "pass",
        "eligible": True, "hard_failure": False,
        "source_branding_detected": False, "identity_mismatch": False,
        "reason": "clean",
    }
    service = ProductVideoQCService(conn, evaluator=evaluator)
    auth = {
        "workflow_id": "wf-qc-service", "run_id": "wrong-scope",
        "scope": ["video"], "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "qc_budget_mode": "unlimited",
    }

    result = service.run(
        "wf-qc-service", QCStage.IMAGE, provider_mode="configured",
        authorize_cost=True, authorization=auth,
    )

    assert result["status"] == "completed"
    assert result["report"].verdict == QCVerdict.PASS
    assert evaluator.evaluate_image.call_count == 2
    assert conn.execute("SELECT COUNT(*) FROM budget_reservation").fetchone()[0] == 0
    conn.close()


def test_image_selector_cost_uses_only_deduplicated_recorded_receipts():
    cost, status = _recorded_route_cost([
        {"asset_id": "image-a", "findings": {"route_receipt": {
            "attempt_id": "provider-call-1", "cost_status": "recorded", "cost_usd": 0.017,
        }}},
        {"asset_id": "image-b", "findings": {"route_receipt": {
            "attempt_id": "provider-call-1", "cost_status": "recorded", "cost_usd": 0.017,
        }}},
        {"asset_id": "image-c", "findings": {"route_receipt": {
            "attempt_id": "provider-call-2", "cost_status": "uncertain", "cost_usd": 9.0,
        }}},
    ])
    assert status == "recorded"
    assert cost == 0.017

    unknown_cost, unknown_status = _recorded_route_cost([
        {"asset_id": "image-d", "findings": {"route_receipt": {
            "attempt_id": "provider-call-3", "cost_status": "uncertain",
        }}}
    ])
    assert unknown_status == "unknown"
    assert unknown_cost is None


def test_unreconciled_unknown_blocks_rerun_even_with_a_new_authorization(tmp_path):
    conn = _seed_image_workflow(tmp_path)
    evaluator = MagicMock()
    evaluator.evaluator_name = "configured_test_vision"
    evaluator.evaluator_version = "2"
    evaluator.model = "test/vision"
    evaluator.fake_mode = False
    evaluator.evaluate_image.side_effect = QCTechnicalError("connection outcome unknown after send")
    service = ProductVideoQCService(conn, evaluator=evaluator)

    def authorization(run_id: str) -> dict[str, object]:
        return {
            "workflow_id": "wf-qc-service",
            "run_id": run_id,
            "scope": ["image"],
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "max_cost_usd": 1.0,
        }

    first = service.run(
        "wf-qc-service", QCStage.IMAGE, provider_mode="configured",
        authorize_cost=True, max_cost_usd=0.10, authorization=authorization("unknown-grant-1"),
    )
    calls_after_unknown = evaluator.evaluate_image.call_count
    assert first["report"].verdict == QCVerdict.UNKNOWN
    assert calls_after_unknown > 0

    retried = service.run(
        "wf-qc-service", QCStage.IMAGE, provider_mode="configured",
        authorize_cost=True, max_cost_usd=0.10, authorization=authorization("unknown-grant-2"),
    )

    assert retried["status"] == "pending"
    assert retried["code"] == "QC_OUTCOME_UNCERTAIN"
    assert evaluator.evaluate_image.call_count == calls_after_unknown
    conn.close()


def test_operator_qc_reconciliation_unblocks_only_after_evidence(tmp_path):
    conn = _seed_image_workflow(tmp_path)
    evaluator = MagicMock()
    evaluator.evaluator_name = "configured_test_vision"
    evaluator.evaluator_version = "2"
    evaluator.model = "test/vision"
    evaluator.fake_mode = False
    passing = {
        **_reference_profile(), "identity_score": 55, "clarity_score": 23,
        "usability_score": 12, "total_score": 90, "verdict": "pass",
        "eligible": True, "hard_failure": False, "source_branding_detected": False,
        "identity_mismatch": False, "reason": "clean",
    }
    evaluator.evaluate_image.side_effect = [QCTechnicalError("connection outcome unknown after send"), passing, passing]
    service = ProductVideoQCService(conn, evaluator=evaluator)
    auth = {
        "workflow_id": "wf-qc-service", "run_id": "reconcile-grant-1", "scope": ["image"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "max_cost_usd": 1.0,
    }
    first = service.run(
        "wf-qc-service", QCStage.IMAGE, provider_mode="configured",
        authorize_cost=True, max_cost_usd=0.10, authorization=auth,
    )
    attempt = conn.execute(
        "SELECT attempt_id, reservation_id FROM product_video_operation_attempt WHERE workflow_id = ?",
        ("wf-qc-service",),
    ).fetchone()
    assert first["report"].verdict == QCVerdict.UNKNOWN
    assert attempt is not None
    with pytest.raises(ValueError, match="evidence reference"):
        service.reconcile_uncertain_attempt(
            "wf-qc-service", attempt["attempt_id"], outcome="not_sent", evidence_reference="", note="checked",
        )
    assert attempt["reservation_id"] is None
    assert conn.execute("SELECT COUNT(*) FROM budget_reservation").fetchone()[0] == 0

    reconciled = service.reconcile_uncertain_attempt(
        "wf-qc-service",
        attempt["attempt_id"],
        outcome="not_sent",
        evidence_reference="router-usage-check:2026-10-19T10:14Z",
        note="Operator verified the request is absent from the provider ledger.",
    )

    assert reconciled["attempt_status"] == "reconciled_not_sent"
    assert reconciled["reservation_status"] == "none"
    aggregate, _ = service.workflow_repository.get_workflow("wf-qc-service")
    assert aggregate["qc_reconciliations"][-1]["attempt_id"] == attempt["attempt_id"]
    assert conn.execute("SELECT COUNT(*) FROM budget_reservation").fetchone()[0] == 0
    assert conn.execute(
        "SELECT event_type FROM workflow_event WHERE event_type='QCOutcomeReconciled'"
    ).fetchone()["event_type"] == "QCOutcomeReconciled"

    retry = service.run(
        "wf-qc-service", QCStage.IMAGE, provider_mode="configured",
    )
    assert retry["status"] == "completed"
    assert evaluator.evaluate_image.call_count == 3
    second_attempt = conn.execute(
        "SELECT attempt_id, reservation_id, status FROM product_video_operation_attempt "
        "WHERE workflow_id = ? AND attempt_number = 2",
        ("wf-qc-service",),
    ).fetchone()
    assert second_attempt["status"] == "pass"
    assert second_attempt["reservation_id"] is None
    assert conn.execute("SELECT COUNT(*) FROM budget_reservation").fetchone()[0] == 0
    conn.close()


def test_qc_stops_after_first_unreconciled_technical_unknown(tmp_path):
    conn = _seed_image_workflow(tmp_path)
    evaluator = MagicMock()
    evaluator.evaluator_name = "configured_test_vision"
    evaluator.evaluator_version = "2"
    evaluator.model = "test/vision"
    evaluator.fake_mode = False
    evaluator.evaluate_image.side_effect = QCTechnicalError("connection outcome unknown after send")
    auth = {
        "workflow_id": "wf-qc-service", "run_id": "four-attempt-grant", "scope": ["image"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "max_cost_usd": 1.0,
    }
    service = ProductVideoQCService(conn, evaluator=evaluator)
    result = service.run("wf-qc-service", QCStage.IMAGE, provider_mode="configured",
                         authorize_cost=True, max_cost_usd=0.10, authorization=auth)
    assert result["status"] == "pending"
    assert result["report"].verdict == QCVerdict.UNKNOWN
    blocked = service.run("wf-qc-service", QCStage.IMAGE, provider_mode="configured",
                          authorize_cost=True, max_cost_usd=0.10, authorization=auth)
    assert blocked["status"] == "pending"
    assert blocked["code"] == "QC_OUTCOME_UNCERTAIN"
    assert evaluator.evaluate_image.call_count == 1
    operation = conn.execute(
        "SELECT status,attempt_id FROM product_video_operation_attempt WHERE workflow_id=? "
        "AND capability_id='product_video.qc.image' ORDER BY attempt_number DESC LIMIT 1",
        ("wf-qc-service",),
    ).fetchone()
    assert operation["status"] == "technical_unknown"
    assert operation["attempt_id"]
    conn.close()


def test_hierarchical_reservation_counts_campaign_and_workflow_totals(tmp_path):
    conn = connect(tmp_path / "budget.db")
    repo = BudgetRepository(conn)
    workflow_policy = BudgetPolicy(max_total=1_000, per_channel={"video_factory": 1_000})
    first = repo.reserve_budget(
        workflow_policy, "video_factory", 900, owner_user_id="owner", scope_id="wf-1",
        idempotency_key="a1", campaign_id="campaign", campaign_max=1_000,
        authorization_id="grant", attempt_id="attempt-a1",
    )
    assert first.amount == 900
    with pytest.raises(BudgetError, match="campaign budget"):
        repo.reserve_budget(
            workflow_policy, "video_factory", 200, owner_user_id="owner", scope_id="wf-2",
            idempotency_key="a2", campaign_id="campaign", campaign_max=1_000,
            authorization_id="grant", attempt_id="attempt-a2",
        )
    with pytest.raises(BudgetError, match="exceeds its limit"):
        repo.reserve_budget(
            BudgetPolicy(max_total=1_000, per_channel={"video_factory": 1_000}),
            "video_factory", 200, owner_user_id="owner", scope_id="wf-1", idempotency_key="a3",
            campaign_id="other", campaign_max=10_000, authorization_id="grant", attempt_id="attempt-a3",
        )
    replay = repo.reserve_budget(
        workflow_policy, "video_factory", 900, owner_user_id="owner", scope_id="wf-1",
        idempotency_key="a1", campaign_id="campaign", campaign_max=1_000,
        authorization_id="grant", attempt_id="attempt-a1",
    )
    assert replay.reservation_id == first.reservation_id
    conn.close()


def test_configured_provider_429_is_retryable_technical_unknown(tmp_path):
    conn = _seed_image_workflow(tmp_path)
    evaluator = MagicMock()
    evaluator.evaluator_name = "configured_test_vision"
    evaluator.evaluator_version = "2"
    evaluator.model = "test/vision"
    evaluator.fake_mode = False
    evaluator.evaluate_image.side_effect = [
        RuntimeError("MultimodalEvaluator HTTP 429: RESOURCE_EXHAUSTED"),
        RuntimeError("MultimodalEvaluator HTTP 429: RESOURCE_EXHAUSTED"),
    ]
    service = ProductVideoQCService(
        conn,
        evaluator=evaluator,
        budget_policy=BudgetPolicy(max_total=100, per_channel={"video_factory": 100}),
    )
    authorization = {
        "workflow_id": "wf-qc-service",
        "run_id": "run-qc-429",
        "scope": ["image"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "max_cost_usd": 0.30,
    }

    first = service.run(
        "wf-qc-service",
        QCStage.IMAGE,
        provider_mode="configured",
        authorize_cost=True,
        max_cost_usd=0.10,
        authorization=authorization,
    )
    assert first["report"].verdict == QCVerdict.UNKNOWN
    attempt = conn.execute(
        "SELECT status, reservation_id, actual_cost_usd, cost_status FROM product_video_operation_attempt WHERE workflow_id = ?",
        ("wf-qc-service",),
    ).fetchone()
    status = attempt["status"]
    assert status == "technical_unknown"
    assert attempt["reservation_id"] is None
    assert attempt["actual_cost_usd"] is None
    assert attempt["cost_status"] == "unknown"
    assert conn.execute("SELECT COUNT(*) FROM budget_reservation").fetchone()[0] == 0
    assert evaluator.evaluate_image.call_count == 1
    conn.close()


def test_configured_provider_failure_keeps_uncertain_status_without_money_reservation(tmp_path):
    conn = _seed_image_workflow(tmp_path)
    evaluator = MagicMock()
    evaluator.evaluator_name = "configured_test_vision"
    evaluator.evaluator_version = "2"
    evaluator.model = "test/vision"
    evaluator.fake_mode = False
    evaluator.evaluate_image.side_effect = RuntimeError(
        'MultimodalEvaluator HTTP 403: {"reason": "SERVICE_DISABLED"}'
    )
    service = ProductVideoQCService(conn, evaluator=evaluator)
    authorization = {
        "workflow_id": "wf-qc-service",
        "run_id": "disabled-service-grant",
        "scope": ["image"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "max_cost_usd": 0.30,
    }

    result = service.run(
        "wf-qc-service", QCStage.IMAGE, provider_mode="configured",
        authorize_cost=True, max_cost_usd=0.10, authorization=authorization,
    )

    assert result["status"] == "pending"
    assert result["report"].verdict == QCVerdict.UNKNOWN
    # The evaluator was invoked, so submission cannot be proven absent and a
    # second image must not be sent while the first request is unreconciled.
    assert evaluator.evaluate_image.call_count == 1
    reservation_count = conn.execute("SELECT COUNT(*) FROM budget_reservation").fetchone()[0]
    attempt = conn.execute(
        "SELECT status, reservation_id, cost_status, actual_cost_usd FROM product_video_operation_attempt WHERE workflow_id = ?",
        ("wf-qc-service",),
    ).fetchone()
    assert reservation_count == 0
    assert attempt["status"] == "technical_unknown"
    assert attempt["reservation_id"] is None
    assert attempt["cost_status"] == "unknown"
    assert attempt["actual_cost_usd"] is None
    conn.close()


def test_operator_reconciliation_supports_durable_image_qc_retry_attempt(tmp_path):
    from video_factory.qc.retry_policy import claim_qc_attempt, complete_qc_attempt

    conn = _seed_image_workflow(tmp_path)
    reservation = BudgetRepository(conn).reserve_budget(
        BudgetPolicy(max_total=100, per_channel={"video_factory": 100}),
        "video_factory",
        50,
        owner_user_id="local-owner",
        scope_id="wf-qc-service",
        idempotency_key="image-qc-durable-attempt",
        capability_id="product_video.qc.image",
        operation_input_digest="input-hash",
        authorization_id="pilot-grant",
        attempt_id="img_claim_wf-qc-service_v1",
    )
    claim = claim_qc_attempt(
        conn,
        workflow_id="wf-qc-service",
        retry_type="source_image_qc",
        item_id="aula:f75",
        input_hash="input-hash",
        operation_id="op_init_img_wf-qc-service_v1",
    )
    complete_qc_attempt(
        conn,
        attempt_id=claim["attempt_id"],
        status="technical_unknown",
        verdict="unknown",
        budget_reference=reservation.reservation_id,
        reason_text="request may have been sent",
    )

    result = ProductVideoQCService(conn).reconcile_uncertain_attempt(
        "wf-qc-service",
        "op_init_img_wf-qc-service_v1",
        outcome="consumed",
        evidence_reference="9router-usage:attempt-123",
        note="Router ledger confirms one consumed request.",
    )

    assert result["reservation_status"] == "committed"
    assert conn.execute(
        "SELECT state FROM budget_reservation WHERE reservation_id = ?",
        (reservation.reservation_id,),
    ).fetchone()["state"] == "committed"
    assert conn.execute(
        "SELECT status FROM workflow_qc_retries WHERE operation_id = ?",
        ("op_init_img_wf-qc-service_v1",),
    ).fetchone()["status"] == "reconciled_consumed"
    conn.close()


def test_cli_reconciles_only_proven_presend_quota_reservation(tmp_path, capsys):
    conn = _seed_image_workflow(tmp_path)
    reservation = BudgetRepository(conn).reserve_budget(
        BudgetPolicy(max_total=100, per_channel={"video_factory": 100}),
        "video_factory", 50,
        owner_user_id="local-owner", scope_id="wf-qc-service",
        idempotency_key="presend-quota-orphan",
        capability_id="product_video.qc.image",
        operation_input_digest="a" * 64,
        authorization_id="quota-grant",
        attempt_id="img_claim_wf-qc-service_v1_abc123",
    )
    aggregate, version = WorkflowRepository(conn).get_workflow("wf-qc-service")
    WorkflowRepository(conn).commit_workflow(
        "wf-qc-service", json.dumps(aggregate), [
            {
                "event_id": "evt-quota-presend",
                "command_id": "cmd-quota-presend",
                "event_type": "WorkflowWaitingForOperator",
                "timestamp": reservation.created_at,
                "payload": {"code": "QC_QUOTA_EXHAUSTED", "message": "4/4 attempts used"},
            },
        ], expected_version=version,
    )
    conn.close()

    assert main([
        "--db-path", str(tmp_path / "qc_service.db"),
        "qc-reconcile-presend", "wf-qc-service",
        "--reservation-id", reservation.reservation_id,
    ]) == 0
    assert "released" in capsys.readouterr().out
    conn = connect(tmp_path / "qc_service.db")
    assert conn.execute(
        "SELECT state FROM budget_reservation WHERE reservation_id=?",
        (reservation.reservation_id,),
    ).fetchone()["state"] == "released"
    assert conn.execute(
        "SELECT COUNT(*) FROM workflow_event WHERE workflow_id=? AND event_type='QCPreSendReservationReleased'",
        ("wf-qc-service",),
    ).fetchone()[0] == 1
    conn.close()


def test_presend_reconciliation_rejects_missing_quota_event_or_existing_attempt(tmp_path):
    from video_factory.qc.retry_policy import claim_qc_attempt

    conn = _seed_image_workflow(tmp_path)
    reservation = BudgetRepository(conn).reserve_budget(
        BudgetPolicy(max_total=100, per_channel={"video_factory": 100}),
        "video_factory", 50,
        owner_user_id="local-owner", scope_id="wf-qc-service",
        idempotency_key="not-proven-presend",
        capability_id="product_video.qc.image",
        operation_input_digest="b" * 64,
        authorization_id="quota-grant",
        attempt_id="img_claim_wf-qc-service_v1_xyz123",
    )
    service = ProductVideoQCService(conn)
    with pytest.raises(ValueError, match="quota event"):
        service.reconcile_presend_reservation("wf-qc-service", reservation.reservation_id)
    aggregate, version = WorkflowRepository(conn).get_workflow("wf-qc-service")
    WorkflowRepository(conn).commit_workflow(
        "wf-qc-service", json.dumps(aggregate), [
            {
                "event_id": "evt-quota-with-attempt",
                "command_id": "cmd-quota-with-attempt",
                "event_type": "WorkflowWaitingForOperator",
                "timestamp": reservation.created_at,
                "payload": {"code": "QC_QUOTA_EXHAUSTED"},
            },
        ], expected_version=version,
    )
    claim_qc_attempt(
        conn, workflow_id="wf-qc-service", retry_type="source_image_qc",
        item_id="aula:f75", input_hash="b" * 64,
        operation_id="op_init_img_wf-qc-service_v1_xyz123",
    )
    with pytest.raises(ValueError, match="attempt"):
        service.reconcile_presend_reservation("wf-qc-service", reservation.reservation_id)
    assert conn.execute(
        "SELECT state FROM budget_reservation WHERE reservation_id=?",
        (reservation.reservation_id,),
    ).fetchone()["state"] == "reserved"
    conn.close()


def test_new_qc_authorization_starts_a_fresh_attempt_after_reconciliation(tmp_path):
    conn = _seed_image_workflow(tmp_path)
    evaluator = MagicMock()
    evaluator.evaluator_name = "configured_test_vision"
    evaluator.evaluator_version = "2"
    evaluator.model = "test/vision"
    evaluator.fake_mode = False
    passing = {
        **_reference_profile(),
        "identity_score": 55,
        "clarity_score": 23,
        "usability_score": 12,
        "total_score": 90,
        "verdict": "pass",
        "eligible": True,
        "hard_failure": False,
        "source_branding_detected": False,
        "identity_mismatch": False,
        "reason": "clean",
    }
    evaluator.evaluate_image.side_effect = [
        __import__("video_factory.integrations.nine_router", fromlist=["NineRouterSubmissionUncertain"]).NineRouterSubmissionUncertain(
            "sent response could not be parsed",
            route_receipt={"attempt_id": "router-attempt-1", "cost_status": "charged"},
        ),
        passing,
        passing,
    ]
    service = ProductVideoQCService(
        conn,
        evaluator=evaluator,
        budget_policy=BudgetPolicy(max_total=200, per_channel={"video_factory": 200}),
    )
    pack = {
        "resource_pack_id": "rp-reconcile-retry",
        "version": 1,
        "owner_user_id": "local-owner",
        "project_id": "proj-wf-qc-service",
        "brand": "AULA",
        "model": "F75",
        "assets": [
            {"asset_id": "a1", "local_path": str(tmp_path / "one.png")},
            {"asset_id": "a2", "local_path": str(tmp_path / "two.png")},
        ],
    }

    def authorization(run_id):
        return {
            "workflow_id": "wf-qc-service",
            "run_id": run_id,
            "scope": ["image"],
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "max_cost_usd": 1.0,
        }

    first = service.select_image_references(
        "wf-qc-service", resource_pack=pack, provider_mode="configured",
        authorization=authorization("grant-one"), max_cost_usd=0.50,
    )
    assert first["code"] in {"QC_OUTCOME_UNCERTAIN", "IMAGE_QC_FAILED"}
    attempt = conn.execute(
        "SELECT operation_id FROM workflow_qc_retries WHERE workflow_id=? AND status='technical_unknown'",
        ("wf-qc-service",),
    ).fetchone()
    service.reconcile_uncertain_attempt(
        "wf-qc-service", attempt["operation_id"], outcome="consumed",
        evidence_reference="router-usage:attempt-1", note="Request consumption confirmed.",
    )

    second = service.select_image_references(
        "wf-qc-service", resource_pack=pack, provider_mode="configured",
        authorization=authorization("grant-two"), max_cost_usd=0.50,
    )

    assert second["status"] == "completed"
    assert evaluator.evaluate_image.call_count == 2
    conn.close()


def test_exchange_asset_mapping_is_filtered_to_confirmed_images_without_double_claims(tmp_path):
    from video_factory.integrations.nine_router import NineRouterSubmissionUncertain

    conn = _seed_image_workflow(tmp_path)
    extra = tmp_path / "extra.png"
    Image.new("RGB", (8, 8), color="green").save(extra)
    first_path = tmp_path / "one.png"
    second_path = tmp_path / "two.png"
    import hashlib

    first_sha = hashlib.sha256(first_path.read_bytes()).hexdigest()
    second_sha = hashlib.sha256(second_path.read_bytes()).hexdigest()
    extra_sha = hashlib.sha256(extra.read_bytes()).hexdigest()
    evaluator = MagicMock()
    evaluator.evaluator_name = "configured_test_vision"
    evaluator.evaluator_version = "2"
    evaluator.model = "test/vision"
    evaluator.fake_mode = False
    evaluator.evaluate_image.side_effect = NineRouterSubmissionUncertain(
        "sent response had invalid JSON",
        route_receipt={"attempt_id": "router-attempt-1", "cost_status": "charged"},
    )
    service = ProductVideoQCService(
        conn,
        evaluator=evaluator,
        budget_policy=BudgetPolicy(max_total=100, per_channel={"video_factory": 100}),
    )
    pack = {
        "resource_pack_id": "rp-exchange-filter",
        "version": 1,
        "owner_user_id": "local-owner",
        "project_id": "proj-wf-qc-service",
        "brand": "AULA",
        "model": "F75",
        "assets_mapping": [
            {"asset_id": first_sha, "local_path": str(first_path)},
            {"asset_id": second_sha, "local_path": str(second_path)},
            {"asset_id": extra_sha, "local_path": str(extra)},
        ],
    }
    source_context = {
        "source": "hermes_product_video_exchange",
        "brand": "AULA",
        "model": "F75",
        "input_review": {"confirmed_image_asset_ids": [first_sha, second_sha]},
        "images": [
            {"asset_id": first_sha, "absolute_path": str(first_path), "sha256": first_sha},
            {"asset_id": second_sha, "absolute_path": str(second_path), "sha256": second_sha},
        ],
    }
    auth = {
        "workflow_id": "wf-qc-service", "run_id": "exchange-filter-grant",
        "scope": ["image"], "max_cost_usd": 1.0,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    }

    result = service.select_image_references(
        "wf-qc-service", resource_pack=pack, source_context=source_context,
        provider_mode="configured", authorization=auth, max_cost_usd=0.50,
    )

    assert result["code"] == "QC_OUTCOME_UNCERTAIN"
    assert evaluator.evaluate_image.call_count == 1
    assert evaluator.evaluate_image.call_args.kwargs["image_path"] == str(first_path)
    assert conn.execute(
        "SELECT COUNT(*) FROM workflow_qc_retries WHERE workflow_id=? AND status='technical_unknown'",
        ("wf-qc-service",),
    ).fetchone()[0] == 1
    conn.close()


def test_exchange_pack_sha_admission_runs_before_evaluator_for_auto_admitted_images(tmp_path):
    conn = _seed_image_workflow(tmp_path)
    first = tmp_path / "one.png"
    second = tmp_path / "two.png"
    wrong_one = tmp_path / "wrong-one.png"
    wrong_two = tmp_path / "wrong-two.png"
    Image.new("RGB", (8, 8), color="green").save(wrong_one)
    Image.new("RGB", (8, 8), color="yellow").save(wrong_two)
    import hashlib

    images = [
        {"asset_id": "exchange-1", "absolute_path": str(first), "sha256": hashlib.sha256(first.read_bytes()).hexdigest(), "identity_review_reason": "ADMITTED_FOR_VISUAL_QC"},
        {"asset_id": "exchange-2", "absolute_path": str(second), "sha256": hashlib.sha256(second.read_bytes()).hexdigest(), "identity_review_reason": "ADMITTED_FOR_VISUAL_QC"},
    ]
    evaluator = MagicMock()
    service = ProductVideoQCService(conn, evaluator=evaluator)
    result = service.select_image_references(
        "wf-qc-service",
        resource_pack={
            "resource_pack_id": "wrong-pack", "version": 1,
            "assets": [
                {"asset_id": "other-1", "local_path": str(wrong_one)},
                {"asset_id": "other-2", "local_path": str(wrong_two)},
            ],
        },
        source_context={
            "source": "hermes_product_video_exchange", "auto_admitted": True,
            "brand": "AULA", "model": "F75", "images": images,
        },
        provider_mode="fake",
    )

    assert result["code"] == "APPROVED_IMAGE_MISMATCH"
    assert evaluator.evaluate_image.call_count == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='workflow_qc_retries'"
    ).fetchone()[0] == 0
    conn.close()


def test_governed_image_qc_gate_rejects_exchange_pack_before_evaluator(tmp_path):
    conn = _seed_image_workflow(tmp_path)
    exchange_one = tmp_path / "exchange-one.png"
    exchange_two = tmp_path / "exchange-two.png"
    Image.new("RGB", (8, 8), color="green").save(exchange_one)
    Image.new("RGB", (8, 8), color="yellow").save(exchange_two)
    import hashlib

    workflow = WorkflowRepository(conn).get_workflow("wf-qc-service")
    aggregate, version = workflow
    aggregate["source_context"] = {
        "source": "hermes_product_video_exchange", "auto_admitted": True,
        "brand": "AULA", "model": "F75",
        "images": [
            {"asset_id": "exchange-1", "absolute_path": str(exchange_one), "sha256": hashlib.sha256(exchange_one.read_bytes()).hexdigest()},
            {"asset_id": "exchange-2", "absolute_path": str(exchange_two), "sha256": hashlib.sha256(exchange_two.read_bytes()).hexdigest()},
        ],
    }
    WorkflowRepository(conn).commit_workflow(
        "wf-qc-service", json.dumps(aggregate), [], expected_version=version,
    )
    evaluator = MagicMock()
    result = ProductVideoQCService(conn, evaluator=evaluator).run(
        "wf-qc-service", QCStage.IMAGE, provider_mode="fake",
        artifact_override={
            "resource_pack_id": "wrong-pack", "version": 1,
            "assets": [
                {"asset_id": "wrong-1", "local_path": str(tmp_path / "one.png")},
                {"asset_id": "wrong-2", "local_path": str(tmp_path / "two.png")},
            ],
        },
    )

    assert result["code"] == "APPROVED_IMAGE_MISMATCH"
    assert evaluator.evaluate_image.call_count == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM product_video_qc_reports WHERE workflow_id=?", ("wf-qc-service",)
    ).fetchone()[0] == 0
    conn.close()


def test_exchange_sha_admission_runs_image_qc_once_and_persists_report(tmp_path):
    conn = _seed_image_workflow(tmp_path)
    first = tmp_path / "one.png"
    second = tmp_path / "two.png"
    wrong = tmp_path / "wrong.png"
    Image.new("RGB", (8, 8), color="yellow").save(wrong)
    import hashlib

    aggregate, version = WorkflowRepository(conn).get_workflow("wf-qc-service")
    aggregate["source_context"] = {
        "source": "hermes_product_video_exchange", "auto_admitted": True,
        "brand": "AULA", "model": "F75",
        "images": [
            {"asset_id": "exchange-1", "absolute_path": str(first), "sha256": hashlib.sha256(first.read_bytes()).hexdigest()},
            {"asset_id": "exchange-2", "absolute_path": str(second), "sha256": hashlib.sha256(second.read_bytes()).hexdigest()},
        ],
    }
    aggregate["resource_pack"]["assets"] = [
        {"asset_id": "exchange-1", "local_path": str(first)},
        {"asset_id": "exchange-2", "local_path": str(second)},
        {"asset_id": "not-exchange", "local_path": str(wrong)},
    ]
    WorkflowRepository(conn).commit_workflow("wf-qc-service", json.dumps(aggregate), [], expected_version=version)
    evaluator = MagicMock()
    evaluator.evaluate_image.return_value = {
        **_reference_profile(), "identity_score": 55, "clarity_score": 23,
        "usability_score": 12, "total_score": 90, "verdict": "pass",
        "eligible": True, "hard_failure": False,
        "source_branding_detected": False, "identity_mismatch": False,
        "reason": "clean",
    }

    result = ProductVideoQCService(conn, evaluator=evaluator).run(
        "wf-qc-service", QCStage.IMAGE, provider_mode="fake"
    )

    assert result["status"] == "completed"
    assert evaluator.evaluate_image.call_count == 2
    assert ProductVideoQCService(conn).repository.get_report(result["report_id"]) is not None
    stored_pack = WorkflowRepository(conn).get_workflow("wf-qc-service")[0]["resource_pack"]
    assert len(stored_pack["assets"]) == 2
    assert {item["asset_id"] for item in stored_pack["assets"]} == {"exchange-1", "exchange-2"}
    conn.close()

def _reference_profile() -> dict[str, object]:
    return {
        "view_angle": "front three-quarter",
        "shot_type": "clean product",
        "visible_details": ["product body", "controls"],
        "scene_suitability": ["hero", "detail"],
        "risk_flags": [],
        "text_coverage_percent": 0,
    }
