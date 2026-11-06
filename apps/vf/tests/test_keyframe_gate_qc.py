from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from video_factory.cli import _advance_qc_waiting_workflow, main
from video_factory.handlers import (
    JobSucceeded,
    ProductVideoWorkflowState,
    create_product_video_topic_handlers,
)
from video_factory.qc.models import QCReport, QCStage, QCVerdict
from video_factory.qc.fingerprint import KEYFRAME_POLICY_VERSION, KEYFRAME_RUBRIC_VERSION
from video_factory.qc.repository import QCReportRepository
from video_factory.workflows.approvals import ApprovalService
from video_factory.workflows.envelope import build_envelope, parse_envelope
from video_factory.workflows.repository import WorkflowRepository
from video_factory.workflows.runtime_control import get_pipeline_pause
from video_factory.storage_infra.sqlite import connect


@dataclass
class _StoryboardHandlerService:
    artifact: dict[str, Any]

    def get_handlers_map(self) -> dict[str, Any]:
        return {
            "product_video.storyboard": lambda *_args, **_kwargs: JobSucceeded(
                artifact_ref=self.artifact,
                next_command={"command": "render_video"},
            )
        }


def _storyboard_artifact() -> dict[str, Any]:
    return {
        "schema": "storyboard_package_v2",
        "schema_version": "storyboard-package-v2",
        "storyboard_id": "sb-keyframe-1",
        "version": 2,
        "package_digest": "sha256:keyframe-package",
        "generated_keyframes": [
            {
                "frame_id": "scene-1-start",
                "image_path": "D:/tmp/keyframe-1.png",
                "sha256": "a" * 64,
                "reference_image_paths": ["D:/tmp/reference-1.png"],
            }
        ],
    }


def _dispatch_storyboard(tmp_path, monkeypatch, qc_result: Any, *, max_qc_retries: int | None = None):
    db_path = tmp_path / "keyframe-gate.db"
    connection = connect(db_path)
    repository = WorkflowRepository(connection)
    ApprovalService(connection)
    workflow_id = "pv-keyframe-gate"
    aggregate = {
        "state": ProductVideoWorkflowState.CREATING_STORYBOARD.value,
        "project_id": "proj-keyframe-gate",
        "owner_user_id": "operator-1",
        "scene_plan": {"scene_plan_id": "scenes-1", "scenes": [{"scene_id": "scene-1"}]},
    }
    if max_qc_retries is not None:
        aggregate["max_qc_retries"] = max_qc_retries
    repository.commit_workflow(
        workflow_id=workflow_id,
        aggregate_json=json.dumps(aggregate),
        events=[],
        expected_version=None,
    )
    artifact = _storyboard_artifact()
    service = _StoryboardHandlerService(artifact)

    from video_factory.qc.service import ProductVideoQCService

    def _run_qc(qc_service, target_workflow_id, stage, **kwargs):
        if callable(qc_result):
            return qc_result(qc_service, target_workflow_id, stage, **kwargs)
        return qc_result

    monkeypatch.setattr(ProductVideoQCService, "run", _run_qc)
    handler = create_product_video_topic_handlers(
        workflow_repository=repository,
        handlers_service=service,
        provider_mode="configured",
    )["job.dispatch"]
    envelope = parse_envelope(
        build_envelope(
            workflow_id=workflow_id,
            topic="job.dispatch",
            idempotency_key="storyboard-keyframe-gate",
            payload={
                "job_name": "product_video.storyboard",
                "owner_user_id": "operator-1",
                "project_id": "proj-keyframe-gate",
                "args": [{"scene_plan_id": "scenes-1"}],
                "kwargs": {},
            },
        )
    )
    handler(envelope)
    return connection, repository, workflow_id, artifact, handler, envelope


def test_storyboard_is_persisted_before_keyframe_qc_waits_for_authorization(
    tmp_path, monkeypatch
) -> None:
    observed_during_qc: list[dict[str, Any]] = []

    def _pending_after_checkpoint(qc_service, workflow_id, stage, **_kwargs):
        aggregate, _ = qc_service.workflow_repository.get_workflow(workflow_id)
        observed_during_qc.append(aggregate)
        assert stage is QCStage.KEYFRAME
        assert aggregate["storyboard"] == _storyboard_artifact()
        assert aggregate["latest_artifact"] == _storyboard_artifact()
        return {
            "status": "pending",
            "stage": QCStage.KEYFRAME.value,
            "reason": "Configured QC requires authorization",
        }

    connection, repository, workflow_id, artifact, _handler, _envelope = _dispatch_storyboard(
        tmp_path,
        monkeypatch,
        _pending_after_checkpoint,
    )

    assert len(observed_during_qc) == 1
    aggregate, _ = repository.get_workflow(workflow_id)
    assert aggregate["state"] == ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
    assert aggregate["storyboard"] == artifact
    assert aggregate["latest_artifact"] == artifact
    assert aggregate["operator_action"] == "qc_authorization"
    assert aggregate["operator_stage"] == QCStage.KEYFRAME.value
    assert connection.execute("SELECT COUNT(*) FROM approval_request").fetchone()[0] == 0


def test_unverifiable_keyframe_qc_input_pauses_checkout_as_contract_broken(
    tmp_path, monkeypatch
) -> None:
    connection, repository, workflow_id, _artifact, _handler, _envelope = _dispatch_storyboard(
        tmp_path,
        monkeypatch,
        {
            "status": "pending",
            "stage": QCStage.KEYFRAME.value,
            "code": "QC_INPUT_UNVERIFIABLE",
            "reason": "Keyframe prompt context is missing from the workflow contract.",
        },
    )

    aggregate, _ = repository.get_workflow(workflow_id)
    pause = get_pipeline_pause(connection)
    assert aggregate["state"] == "contract_broken"
    assert aggregate["failure_category"] == "CONTRACT_BROKEN"
    assert aggregate["failure_code"] == "QC_INPUT_UNVERIFIABLE"
    assert pause is not None and pause["category"] == "CONTRACT_BROKEN"
    assert pause["workflow_id"] == workflow_id


@pytest.mark.parametrize("verdict", ["fail", "unknown"])
def test_keyframe_qc_non_pass_waits_for_retry_without_gate(
    tmp_path, monkeypatch, verdict: str
) -> None:
    connection, repository, workflow_id, artifact, _handler, _envelope = _dispatch_storyboard(
        tmp_path,
        monkeypatch,
        {
            "status": "completed",
            "stage": QCStage.KEYFRAME.value,
            "report_id": f"qc-{verdict}",
            "report": {"verdict": verdict},
        },
    )

    aggregate, _ = repository.get_workflow(workflow_id)
    assert aggregate["state"] == ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
    assert aggregate["storyboard"] == artifact
    expected_action = "qc_provider_retry" if verdict == "fail" else "qc_reconciliation"
    assert aggregate["operator_action"] == expected_action
    assert aggregate["operator_stage"] == QCStage.KEYFRAME.value
    assert aggregate["operator_qc_report_id"] == f"qc-{verdict}"
    assert connection.execute("SELECT COUNT(*) FROM approval_request").fetchone()[0] == 0


def test_keyframe_qc_failure_persists_controlled_recovery_plan(
    tmp_path, monkeypatch
) -> None:
    recovery_plan = {
        "blocked_from_video": True,
        "frames": [
            {
                "frame_id": "scene-1-start",
                "actions": [
                    "reselect_reference",
                    "rewrite_prompt",
                    "regenerate_keyframe",
                    "rerun_keyframe_qc",
                ],
                "feedback": "Use the clean front reference and preserve the control",
            }
        ],
    }
    connection, repository, workflow_id, _artifact, _handler, _envelope = _dispatch_storyboard(
        tmp_path,
        monkeypatch,
        {
            "status": "completed",
            "stage": QCStage.KEYFRAME.value,
            "report_id": "qc-fidelity-fail",
            "report": {
                "report_id": "qc-fidelity-fail",
                "verdict": "fail",
                "findings": {"recovery_plan": recovery_plan},
            },
        },
    )

    aggregate, _ = repository.get_workflow(workflow_id)
    assert aggregate["state"] == ProductVideoWorkflowState.CREATING_STORYBOARD.value
    assert aggregate["keyframe_recovery_plan"] == recovery_plan
    assert aggregate["keyframe_recovery_attempt"] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM workflow_outbox WHERE workflow_id=?", (workflow_id,)
    ).fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM approval_request").fetchone()[0] == 0


def test_semantic_keyframe_qc_failure_automatically_enqueues_flow_regeneration(
    tmp_path, monkeypatch
) -> None:
    recovery_plan = {
        "blocked_from_video": True,
        "frames": [
            {
                "frame_id": "scene-1-start",
                "scene_id": "scene-1",
                "actions": ["rewrite_prompt", "regenerate_keyframe", "rerun_keyframe_qc"],
                "feedback": "Remove the bicycle and show the product on the picnic blanket.",
            }
        ],
    }
    connection, repository, workflow_id, artifact, _handler, _envelope = _dispatch_storyboard(
        tmp_path,
        monkeypatch,
        {
            "status": "completed",
            "stage": QCStage.KEYFRAME.value,
            "report_id": "qc-fidelity-fail",
            "report": {
                "report_id": "qc-fidelity-fail",
                "verdict": "fail",
                "findings": {"recovery_plan": recovery_plan},
            },
        },
    )

    aggregate, _ = repository.get_workflow(workflow_id)
    assert aggregate["state"] == ProductVideoWorkflowState.CREATING_STORYBOARD.value
    assert aggregate["keyframe_recovery_attempt"] == 1
    assert aggregate["keyframe_recovery_plan"] == recovery_plan
    outbox = connection.execute(
        "SELECT message FROM workflow_outbox WHERE workflow_id=? ORDER BY outbox_id DESC LIMIT 1",
        (workflow_id,),
    ).fetchone()
    payload = json.loads(outbox["message"])["payload"]
    assert payload["job_name"] == "product_video.storyboard"
    assert payload["kwargs"]["recovery_plan"] == recovery_plan
    assert payload["kwargs"]["previous_storyboard"] == artifact
    retry = connection.execute(
        "SELECT attempt_number,verdict,input_hash,qc_report_id FROM workflow_qc_retries "
        "WHERE workflow_id=? AND retry_type='keyframe_qc'",
        (workflow_id,),
    ).fetchone()
    assert tuple(retry) == (1, "fail", artifact["package_digest"], "qc-fidelity-fail")
    assert connection.execute("SELECT COUNT(*) FROM approval_request").fetchone()[0] == 0


def test_unknown_keyframe_qc_waits_for_reconciliation_without_regeneration(
    tmp_path, monkeypatch
) -> None:
    connection, repository, workflow_id, _artifact, _handler, _envelope = _dispatch_storyboard(
        tmp_path,
        monkeypatch,
        {
            "status": "completed",
            "stage": QCStage.KEYFRAME.value,
            "report_id": "qc-technical-unknown",
            "report": {"report_id": "qc-technical-unknown", "verdict": "unknown"},
        },
    )

    aggregate, _ = repository.get_workflow(workflow_id)
    assert aggregate["state"] == ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
    assert aggregate["operator_action"] == "qc_reconciliation"
    assert connection.execute(
        "SELECT COUNT(*) FROM workflow_outbox WHERE workflow_id=?", (workflow_id,)
    ).fetchone()[0] == 0


def test_keyframe_qc_does_not_enqueue_recovery_after_retry_quota_is_exhausted(
    tmp_path, monkeypatch
) -> None:
    connection, repository, workflow_id, _artifact, _handler, _envelope = _dispatch_storyboard(
        tmp_path,
        monkeypatch,
        {
            "status": "completed",
            "stage": QCStage.KEYFRAME.value,
            "report_id": "qc-initial-fail-no-retries",
            "report": {
                "report_id": "qc-initial-fail-no-retries",
                "verdict": "fail",
                "findings": {"recovery_plan": {"frames": [{"frame_id": "scene-1-start"}]}},
            },
        },
        max_qc_retries=0,
    )

    aggregate, _ = repository.get_workflow(workflow_id)
    assert aggregate["state"] == "skipped"
    assert aggregate["failure_category"] == "PRODUCT_REJECTED"
    assert aggregate["failure_code"] == "KEYFRAME_QC_RETRIES_EXHAUSTED"
    assert "after 1 evaluations" in aggregate["skip_reason"]
    assert connection.execute(
        "SELECT COUNT(*) FROM workflow_event WHERE workflow_id=? AND event_type='ProductSkipped'",
        (workflow_id,),
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM workflow_outbox WHERE workflow_id=?", (workflow_id,)
    ).fetchone()[0] == 0


def test_keyframe_qc_pass_creates_pending_gate_four_without_approving(
    tmp_path, monkeypatch
) -> None:
    receipt = {
        "report_id": "qc-pass",
        "stage": "keyframe",
        "verdict": "pass",
        "score": 92,
        "artifact_digest": "sha256:keyframe-package",
        "findings": {
            "items": [
                {
                    "frame_id": "scene-1-start",
                    "sha256": "a" * 64,
                    "reference_digests": ["b" * 64],
                    "score": 92,
                    "verdict": "pass",
                    "hard_failure": False,
                }
            ]
        },
    }
    def _persisted_pass(qc_service, target_workflow_id, stage, **_kwargs):
        QCReportRepository(qc_service.workflow_repository.conn).save_report(QCReport(
            report_id="qc-pass", workflow_id=target_workflow_id,
            stage=stage, artifact_id="sb-keyframe-1", artifact_version=2,
            artifact_digest="sha256:keyframe-package",
            reference_digests=["a" * 64], evaluator_name="test-evaluator",
            evaluator_version="1", rubric_version=KEYFRAME_RUBRIC_VERSION,
            policy_version=KEYFRAME_POLICY_VERSION, created_at=datetime.now(timezone.utc).isoformat(),
            score=92, verdict=QCVerdict.PASS, findings=receipt["findings"],
            provider_mode="configured", owner_user_id="operator-1",
            project_id="proj-keyframe-gate",
        ))
        return {
            "status": "completed", "stage": QCStage.KEYFRAME.value,
            "report_id": "qc-pass", "report": receipt,
        }

    connection, repository, workflow_id, artifact, _handler, _envelope = _dispatch_storyboard(
        tmp_path,
        monkeypatch,
        _persisted_pass,
    )

    aggregate, _ = repository.get_workflow(workflow_id)
    assert aggregate["state"] == ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL.value
    expected_artifact = {**artifact, "keyframe_qc_receipt": receipt}
    assert aggregate["storyboard"] == expected_artifact
    approval = ApprovalService(connection).get_request(aggregate["pending_approval_id"])
    assert approval.status.value == "pending"
    assert approval.payload["gate"] == ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL.value
    assert approval.payload["artifact_ref"] == expected_artifact


def test_keyframe_qc_retry_wait_is_idempotent_on_envelope_replay(
    tmp_path, monkeypatch
) -> None:
    connection, repository, workflow_id, artifact, handler, envelope = _dispatch_storyboard(
        tmp_path,
        monkeypatch,
        {
            "status": "completed",
            "stage": QCStage.KEYFRAME.value,
            "report_id": "qc-unknown",
            "report": {"verdict": "unknown"},
        },
    )
    version_before_replay = repository.get_workflow(workflow_id)[1]

    handler(envelope)

    aggregate, version_after_replay = repository.get_workflow(workflow_id)
    assert version_after_replay == version_before_replay
    assert aggregate["state"] == ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
    assert aggregate["storyboard"] == artifact
    assert connection.execute("SELECT COUNT(*) FROM approval_request").fetchone()[0] == 0


def test_keyframe_retry_authorization_enqueues_regeneration_before_new_qc(tmp_path) -> None:
    connection = connect(tmp_path / "keyframe-retry.db")
    repository = WorkflowRepository(connection)
    ApprovalService(connection)
    workflow_id = "pv-keyframe-retry"
    artifact = _storyboard_artifact()
    repository.commit_workflow(
        workflow_id=workflow_id,
        aggregate_json=json.dumps(
            {
                "state": ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value,
                "project_id": "proj-keyframe-retry",
                "owner_user_id": "operator-1",
                "storyboard": artifact,
                "latest_artifact": artifact,
                "scene_plan": {"scene_plan_id": "scenes-1", "scenes": [{"scene_id": "scene-1"}]},
                "operator_action": "qc_retry_authorization",
                "operator_stage": QCStage.KEYFRAME.value,
                "operator_qc_report_id": "qc-unknown-first-attempt",
                "keyframe_recovery_plan": {
                    "blocked_from_video": True,
                    "frames": [{"frame_id": "scene-1-start", "actions": ["rewrite_prompt"], "feedback": "Preserve the control"}],
                },
            }
        ),
        events=[],
        expected_version=None,
    )

    recovery_id = _advance_qc_waiting_workflow(
        connection,
        workflow_id,
        QCStage.KEYFRAME,
        {
            "workflow_id": workflow_id,
            "run_id": "keyframe-retry-2",
            "scope": [QCStage.KEYFRAME.value],
            "expires_at": "2099-01-01T00:00:00+00:00",
            "max_cost_usd": 2.0,
        },
    )

    aggregate, _ = repository.get_workflow(workflow_id)
    assert recovery_id.startswith("recovery:")
    assert aggregate["state"] == ProductVideoWorkflowState.CREATING_STORYBOARD.value
    assert "operator_qc_report_id" not in aggregate
    assert connection.execute("SELECT COUNT(*) FROM approval_request").fetchone()[0] == 0
    outbox = connection.execute(
        "SELECT message FROM workflow_outbox WHERE workflow_id = ? ORDER BY outbox_id DESC LIMIT 1",
        (workflow_id,),
    ).fetchone()
    payload = json.loads(outbox["message"])["payload"]
    assert payload["job_name"] == "product_video.storyboard"
    assert payload["kwargs"]["recovery_attempt"] == 1
    assert payload["kwargs"]["recovery_plan"]["frames"][0]["feedback"] == "Preserve the control"


def test_reconciled_keyframe_qc_advance_attaches_current_pass_receipt(tmp_path) -> None:
    connection = connect(tmp_path / "keyframe-qc-reconcile-advance.db")
    repository = WorkflowRepository(connection)
    ApprovalService(connection)
    workflow_id = "pv-keyframe-qc-reconcile-advance"
    artifact = _storyboard_artifact()
    repository.commit_workflow(
        workflow_id=workflow_id,
        aggregate_json=json.dumps({
            "state": ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value,
            "project_id": "proj-keyframe-gate",
            "owner_user_id": "operator-1",
            "storyboard": artifact,
            "latest_artifact": artifact,
            "operator_action": "qc_reconciliation",
            "operator_stage": QCStage.KEYFRAME.value,
            "failure_category": "QC_INFRA_PAUSED",
            "failure_code": "QC_OUTCOME_UNCERTAIN",
            "failure_reason": "old uncertain result",
        }),
        events=[],
        expected_version=None,
    )
    QCReportRepository(connection).save_report(QCReport(
        report_id="qc-keyframe-reconciled-pass",
        workflow_id=workflow_id,
        stage=QCStage.KEYFRAME,
        artifact_id=artifact["storyboard_id"],
        artifact_version=artifact["version"],
        artifact_digest=artifact["package_digest"],
        reference_digests=[],
        evaluator_name="configured_test_vision",
        evaluator_version="1",
        rubric_version="3.2",
        created_at=datetime.now(timezone.utc).isoformat(),
        score=86,
        verdict=QCVerdict.PASS,
        findings={"items": [{"frame_id": "scene-1-start", "verdict": "pass", "hard_failure": False}]},
        provider_mode="configured",
        owner_user_id="operator-1",
        project_id="proj-keyframe-gate",
    ))

    approval_id = _advance_qc_waiting_workflow(connection, workflow_id, QCStage.KEYFRAME)

    advanced, _ = repository.get_workflow(workflow_id)
    assert advanced["state"] == ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL.value
    assert "failure_code" not in advanced
    receipt = advanced["latest_artifact"]["keyframe_qc_receipt"]
    assert receipt["report_id"] == "qc-keyframe-reconciled-pass"
    assert receipt["artifact_digest"] == artifact["package_digest"]
    assert receipt["verdict"] == "pass"
    assert ApprovalService(connection).get_request(approval_id).payload["artifact_ref"]["keyframe_qc_receipt"] == receipt
    from video_factory.automation.creative_policy import evaluate_auto_gate

    assert evaluate_auto_gate(
        state=advanced["state"],
        artifact=advanced["latest_artifact"],
        reviews=(),
        policy={"creative_mode": "four_agent", "auto_export": True, "qc_level": "middle"},
    )["action"] == "advance"
    connection.close()


def test_qc_cli_authorizes_keyframe_regeneration_without_rechecking_old_bytes(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "keyframe-cli-retry.db"
    connection = connect(db_path)
    repository = WorkflowRepository(connection)
    workflow_id = "pv-keyframe-cli-retry"
    artifact = _storyboard_artifact()
    repository.commit_workflow(
        workflow_id=workflow_id,
        aggregate_json=json.dumps(
            {
                "state": ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value,
                "project_id": "proj-keyframe-cli-retry",
                "owner_user_id": "operator-1",
                "storyboard": artifact,
                "latest_artifact": artifact,
                "scene_plan": {"scene_plan_id": "scenes-1", "scenes": [{"scene_id": "scene-1"}]},
                "operator_action": "qc_retry_authorization",
                "operator_stage": QCStage.KEYFRAME.value,
                "keyframe_recovery_plan": {
                    "blocked_from_video": True,
                    "frames": [{"frame_id": "scene-1-start", "actions": ["rewrite_prompt"], "feedback": "Keep the product whole"}],
                },
            }
        ),
        events=[],
        expected_version=None,
    )
    connection.close()

    from video_factory.qc.service import ProductVideoQCService

    run_qc = MagicMock(side_effect=AssertionError("old keyframe bytes must not be reevaluated"))
    monkeypatch.setattr(ProductVideoQCService, "run", run_qc)

    exit_code = main(
        [
            "--db-path",
            str(db_path),
            "qc",
            workflow_id,
            "--stage",
            QCStage.KEYFRAME.value,
            "--provider-mode",
            "configured",
            "--authorize-cost",
            "--max-cost-usd",
            "2",
            "--authorization-run-id",
            "keyframe-cli-retry-2",
            "--authorization-scope",
            QCStage.KEYFRAME.value,
            "--authorization-expires-at",
            "2099-01-01T00:00:00+00:00",
            "--authorization-max-cost-usd",
            "2",
            "--advance",
        ]
    )

    assert exit_code == 0
    verify_connection = connect(db_path)
    aggregate, _ = WorkflowRepository(verify_connection).get_workflow(workflow_id)
    assert aggregate["state"] == ProductVideoWorkflowState.CREATING_STORYBOARD.value
    assert "pending_approval_id" not in aggregate
    assert run_qc.call_count == 0
