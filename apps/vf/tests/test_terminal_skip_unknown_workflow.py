from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from video_factory.automation.failure_classification import FailureCategory
from video_factory.cli import main
from video_factory.qc.retry_policy import ensure_qc_retry_schema
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.repository import WorkflowRepository
from video_factory.workflows.runtime_control import pause_pipeline
from video_factory.workflows.runtime_control import skip_unresolved_workflow


def _seed_unknown_workflow(connection: sqlite3.Connection, workflow_id: str) -> None:
    repo = WorkflowRepository(connection)
    repo.commit_workflow(
        workflow_id,
        json.dumps({
            "state": "WaitingForOperator",
            "operator_action": "qc_reconciliation",
            "operator_stage": "image",
        }),
        [],
        outbox_message=json.dumps({"workflow_id": workflow_id, "job_name": "product_video.resource_pack"}),
        expected_version=0,
    )
    now = "2026-10-21T09:00:00+00:00"
    connection.execute(
        """INSERT INTO daily_product_video_import (
             product_key,canonical_product_key,source_date,source_batch_id,
             source_product_id,source_path,workflow_id,status,created_at,updated_at
           ) VALUES (?,?,?,?,?,?,?,'started',?,?)""",
        (workflow_id, f"brand:{workflow_id}", "2026-10-21", "batch-1", workflow_id, "manifest.json", workflow_id, now, now),
    )
    ensure_qc_retry_schema(connection)
    connection.execute(
        """INSERT INTO workflow_qc_retries (
             workflow_id,retry_type,item_id,attempt_number,verdict,operation_id,
             status,reason_text,created_at
           ) VALUES (?,'source_image_qc',?,1,'unknown',?,'technical_unknown','response lost after send',?)""",
        (workflow_id, workflow_id, f"op-{workflow_id}", now),
    )
    connection.execute(
        """INSERT INTO product_video_operation_attempt (
             attempt_id,authorization_id,campaign_id,owner_user_id,workflow_id,
             capability_id,attempt_number,input_fingerprint,estimated_cost,
             reservation_id,provider_operation_id,status,cost_status,created_at,updated_at
           ) VALUES (?,?,?,'local-owner',?,'product_video.qc.image',1,'sha256:test',NULL,
                     NULL,NULL,'technical_unknown','unknown',?,?)""",
        (f"op-attempt-{workflow_id}", f"auth-{workflow_id}", f"campaign-{workflow_id}", workflow_id, now, now),
    )
    connection.commit()


def test_official_skip_keeps_unknown_history_and_resume_ignores_only_skipped_workflow(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "hermes.db"
    connection = connect(db_path)
    _seed_unknown_workflow(connection, "wf-old")
    _seed_unknown_workflow(connection, "wf-active")
    pause_pipeline(
        connection,
        category=FailureCategory.QC_INFRA_PAUSED,
        code="IMAGE_QC_ERROR",
        message="QC response needs reconciliation",
        workflow_id="wf-active",
    )
    connection.close()

    assert main([
        "skip-workflow", "wf-old", "--db-path", str(db_path),
        "--reason", "Cannot correlate the old provider attempt; skip without classifying its cost.",
    ]) == 0
    connection = connect(db_path)
    old = WorkflowRepository(connection).get_workflow("wf-old")
    assert old is not None and old[0]["state"] == "skipped"
    assert old[0]["skip_reason"].startswith("Cannot correlate")
    old_attempt = connection.execute(
        "SELECT status,operation_id FROM workflow_qc_retries WHERE workflow_id='wf-old'"
    ).fetchone()
    assert tuple(old_attempt) == ("technical_unknown", "op-wf-old")
    old_operation = connection.execute(
        "SELECT status,cost_status FROM product_video_operation_attempt WHERE workflow_id='wf-old'"
    ).fetchone()
    assert tuple(old_operation) == ("technical_unknown", "unknown")
    old_import = connection.execute(
        "SELECT status,last_error FROM daily_product_video_import WHERE workflow_id='wf-old'"
    ).fetchone()
    assert old_import["status"] == "error"
    assert old_import["last_error"].startswith("OPERATOR_SKIPPED:")
    old_outbox = connection.execute(
        "SELECT state,last_error FROM workflow_outbox WHERE workflow_id='wf-old'"
    ).fetchone()
    assert old_outbox["state"] == "quarantined"
    assert "OPERATOR_SKIPPED" in old_outbox["last_error"]
    connection.close()

    # The active UNKNOWN still blocks; skipping the first workflow did not
    # reinterpret or clear the other attempt.
    assert main([
        "pipeline-resume", "--db-path", str(db_path), "--note", "Route repaired.",
    ]) == 1

    assert main([
        "skip-workflow", "wf-active", "--db-path", str(db_path),
        "--reason", "Cannot correlate the old provider attempt; skip without classifying its cost.",
    ]) == 0
    assert main([
        "pipeline-resume", "--db-path", str(db_path), "--note", "Route repaired; old workflows terminal-skipped.",
    ]) == 0


def test_known_product_failure_can_be_terminally_skipped_without_erasing_history(tmp_path: Path) -> None:
    connection = connect(tmp_path / "known-failure.db")
    repo = WorkflowRepository(connection)
    repo.commit_workflow(
        "wf-known",
        json.dumps({"state":"creating_scene_plan", "failure_code":"PREPRODUCTION_FAIL"}),
        [],
        outbox_message=json.dumps({"workflow_id":"wf-known", "job_name":"product_video.scene_plan"}),
        expected_version=0,
    )
    connection.execute("UPDATE workflow_outbox SET state='quarantined', last_error='PREPRODUCTION_FAIL: repair quota exhausted'")
    connection.commit()
    skip_unresolved_workflow(connection, "wf-known", "PREPRODUCTION_FAIL: repair quota exhausted",
                              known_product_failure=True)
    saved = repo.get_workflow("wf-known")[0]
    assert saved["state"] == "skipped"
    assert saved["skip_reason"].startswith("PREPRODUCTION_FAIL:")
    connection.close()


def test_received_invalid_creative_response_can_be_persisted_as_terminal_skip(tmp_path: Path) -> None:
    connection = connect(tmp_path / "creative-response-failure.db")
    repo = WorkflowRepository(connection)
    repo.commit_workflow(
        "wf-response",
        json.dumps({"state":"WaitingForOperator", "failure_code":"CREATIVE_RESPONSE_INVALID",
                    "operator_action":"creative_review", "operator_stage":"script"}),
        [],
        outbox_message=json.dumps({"workflow_id":"wf-response", "job_name":"product_video.scene_plan"}),
        expected_version=0,
    )
    connection.execute(
        "UPDATE workflow_outbox SET state='quarantined',attempt_count=5,max_attempts=5,"
        "last_error='CREATIVE_RESPONSE_INVALID: received invalid JSON' WHERE workflow_id='wf-response'")
    connection.commit()
    skip_unresolved_workflow(connection, "wf-response", "CREATIVE_RESPONSE_INVALID: retry quota exhausted",
                             known_product_failure=True)
    saved = repo.get_workflow("wf-response")[0]
    assert saved["state"] == "skipped"
    assert saved["skip_reason"].startswith("CREATIVE_RESPONSE_INVALID:")
    assert connection.execute(
        "SELECT 1 FROM workflow_event WHERE workflow_id='wf-response' "
        "AND event_type='OperatorWorkflowSkipped'").fetchone()
    connection.close()


def test_product_identity_failure_is_item_skip_not_reconciliation(tmp_path: Path) -> None:
    connection = connect(tmp_path / "identity-blocked.db")
    repo = WorkflowRepository(connection)
    repo.commit_workflow(
        "wf-identity",
        json.dumps({"state": "creating_brief", "failure_code": "PRODUCT_IDENTITY_REQUIRED"}),
        [],
        outbox_message=json.dumps({"workflow_id": "wf-identity", "job_name": "product_video.creative_brief"}),
        expected_version=0,
    )
    connection.execute(
        "UPDATE workflow_outbox SET state='quarantined',last_error=? WHERE workflow_id='wf-identity'",
        ("PRODUCT_IDENTITY_REQUIRED: use a real product name and variant",),
    )
    connection.commit()
    skip_unresolved_workflow(
        connection, "wf-identity", "PRODUCT_IDENTITY_REQUIRED: use a real product name and variant",
        known_product_failure=True,
    )
    saved = repo.get_workflow("wf-identity")[0]
    assert saved["state"] == "skipped"
    assert saved["skip_reason"].startswith("PRODUCT_IDENTITY_REQUIRED:")
    assert connection.execute(
        "SELECT 1 FROM workflow_event WHERE workflow_id='wf-identity' AND event_type='OperatorWorkflowSkipped'"
    ).fetchone()
    connection.close()
