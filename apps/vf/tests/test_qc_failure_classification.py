from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from video_factory.automation.failure_classification import (
    FailureCategory,
    classify_failure,
)
from video_factory.automation.coordinator import ProductVideoCoordinator
from video_factory.automation.repository import DailyImportRepository
from video_factory.automation.contracts import ExchangeProduct, compute_product_key
from video_factory.handlers import ProductVideoWorkflowState, start_product_video_workflow
from video_factory.job_contracts import JobFailed
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.runtime_control import (
    ensure_pipeline_pause,
    get_pipeline_pause,
    pause_pipeline,
)
from video_factory.workflows.worker import run_worker
from video_factory.workflows.repository import WorkflowRepository
from video_factory.workflows.envelope import build_envelope
from video_factory.workflows.outbox_dispatcher import OutboxDispatcher
from video_factory.workflows.outbox_repository import OutboxRepository
from video_factory.handlers.registry import create_product_video_topic_handlers
from video_factory.qc.rubric import KEYFRAME_QC_SYSTEM_PROMPT, build_keyframe_qc_prompt
from video_factory.qc.evaluator import validate_qc_response
from video_factory.qc.models import QCStage


@pytest.mark.parametrize(
    ("code", "category"),
    [
        ("EXCHANGE_IMAGE_QC_FAILED", FailureCategory.PRODUCT_REJECTED),
        ("PRODUCT_IMAGE_QC_REJECTED", FailureCategory.PRODUCT_REJECTED),
        ("PRODUCT_INPUT_INVALID", FailureCategory.PRODUCT_REJECTED),
        ("PRODUCT_IDENTITY_REQUIRED: Product Lock requires truthful identity", FailureCategory.PRODUCT_REJECTED),
        ("IMAGE_QC_RETRIES_EXHAUSTED", FailureCategory.PRODUCT_REJECTED),
        ("KEYFRAME_QC_RETRIES_EXHAUSTED", FailureCategory.PRODUCT_REJECTED),
        ("VIDEO_QC_RETRIES_EXHAUSTED", FailureCategory.PRODUCT_REJECTED),
        ("VIDEO_QC_UNTARGETED_FAIL", FailureCategory.PRODUCT_REJECTED),
        ("QC_QUOTA_EXHAUSTED", FailureCategory.QC_INFRA_PAUSED),
        ("QC_RETRY_EXHAUSTED", FailureCategory.QC_INFRA_PAUSED),
        ("IMAGE_QC_FAILED", FailureCategory.QC_INFRA_PAUSED),
        ("KEYFRAME_QC_FAILED", FailureCategory.QC_INFRA_PAUSED),
        ("VIDEO_QC_FAILED", FailureCategory.QC_INFRA_PAUSED),
        ("EXCHANGE_INPUT_REVIEW_REQUIRED", FailureCategory.PRODUCT_REJECTED),
        ("INSUFFICIENT_GROUNDING", FailureCategory.PRODUCT_REJECTED),
        ("CREATIVE_RESPONSE_INVALID", None),
        ("INSUFFICIENT_CANDIDATES", FailureCategory.PRODUCT_REJECTED),
        ("QC_OUTCOME_UNCERTAIN", FailureCategory.QC_INFRA_PAUSED),
        ("QC_PROVIDER_UNAVAILABLE", FailureCategory.QC_INFRA_PAUSED),
        ("QC_RESPONSE_INVALID", FailureCategory.QC_INFRA_PAUSED),
        ("QC_SCHEMA_INVALID", FailureCategory.QC_INFRA_PAUSED),
        ("RuntimeError: MISSING_PROVIDER_CONFIG: configured QC route is absent", FailureCategory.QC_INFRA_PAUSED),
        ("QC_INPUT_UNVERIFIABLE", FailureCategory.CONTRACT_BROKEN),
        ("MISSING_INPUT_REF", FailureCategory.CONTRACT_BROKEN),
        ("PREREQUISITE_FAILED", FailureCategory.CONTRACT_BROKEN),
        ("RATE_LIMITED", FailureCategory.QC_INFRA_PAUSED),
        ("HTTP_429", FailureCategory.QC_INFRA_PAUSED),
        ("RESOURCE_EXHAUSTED", FailureCategory.QC_INFRA_PAUSED),
        ("PROVIDER_TIMEOUT", FailureCategory.QC_INFRA_PAUSED),
        ("PROVIDER_NETWORK_ERROR", FailureCategory.QC_INFRA_PAUSED),
        ("INVALID_SCENE_PLAN_DIGEST", FailureCategory.CONTRACT_BROKEN),
        ("WorkflowTransitionError: cannot transition from render to brief", FailureCategory.CONTRACT_BROKEN),
        ("JOB_BINDING_MISMATCH", FailureCategory.CONTRACT_BROKEN),
    ],
)
def test_failure_categories_are_fail_closed(code: str, category: FailureCategory) -> None:
    assert classify_failure(code) is category


def test_unknown_and_unrecognized_errors_are_never_product_rejection() -> None:
    assert classify_failure("TECHNICAL_UNKNOWN") is FailureCategory.QC_INFRA_PAUSED
    assert classify_failure("SOME_NEW_ERROR") is None
    assert classify_failure("ValueError: scene plan digest mismatch") is FailureCategory.CONTRACT_BROKEN


@pytest.mark.parametrize(
    "code",
    [
        "INVALID_INPUT",
        "IMAGE_QC_FAILED",
        "KEYFRAME_QC_FAILED",
        "VIDEO_QC_FAILED",
        "MAX_RETRIES_EXCEEDED",
        "CLEANING_FAILED",
    ],
)
def test_ambiguous_failure_codes_do_not_reject_a_product(code: str) -> None:
    assert classify_failure(code) is not FailureCategory.PRODUCT_REJECTED


def test_explicit_product_qc_and_input_codes_are_rejected() -> None:
    assert classify_failure("PRODUCT_INPUT_INVALID") is FailureCategory.PRODUCT_REJECTED
    assert classify_failure("PRODUCT_IMAGE_QC_REJECTED") is FailureCategory.PRODUCT_REJECTED
    assert classify_failure("IMAGE_QC_RETRIES_EXHAUSTED") is FailureCategory.PRODUCT_REJECTED


def test_legacy_director_parse_error_requires_exhausted_outbox():
    from video_factory.automation.failure_classification import is_exhausted_legacy_creative_response

    message = "Job execution failed [VF_SCENE_FAILED]: invalid Director prompt JSON: parse error"
    assert is_exhausted_legacy_creative_response(message, 5, 5)
    assert not is_exhausted_legacy_creative_response(message, 4, 5)
    assert not is_exhausted_legacy_creative_response("PROVIDER_TIMEOUT", 5, 5)


def test_image_qc_rejection_requires_consumed_semantic_item_evidence() -> None:
    from video_factory.automation.failure_classification import has_consumed_semantic_image_qc_evidence

    assert not has_consumed_semantic_image_qc_evidence({
        "status": "failed", "usage_status": "uncertain",
        "evaluated_items": [{"verdict": "fail"}],
    })
    assert not has_consumed_semantic_image_qc_evidence({
        "status": "failed", "usage_status": "consumed",
        "evaluated_items": [{"verdict": "unknown", "findings": {"schema_invalid": True}}],
    })
    assert not has_consumed_semantic_image_qc_evidence({
        "status": "failed", "usage_status": "consumed",
        "evaluated_items": [
            {"verdict": "pass", "findings": {}},
            {"verdict": "unknown", "findings": {"technical_unknown": True}},
        ],
    })
    assert has_consumed_semantic_image_qc_evidence({
        "status": "failed", "usage_status": "consumed",
        "evaluated_items": [{"verdict": "fail", "findings": {}}],
    })


def test_keyframe_rubric_treats_narration_as_context_and_identity_as_hard_gate() -> None:
    prompt = build_keyframe_qc_prompt(
        brand="Brand",
        model="Model",
        scene_id="scene-1",
        scene_prompt="A clean close-up of the product.",
        approved_script_scene={"narration": "It works quickly.", "visual_objective": "Show the front."},
    )
    assert "Narration is context only" in prompt
    assert "wrong product color" in KEYFRAME_QC_SYSTEM_PROMPT
    assert "Minor style" in KEYFRAME_QC_SYSTEM_PROMPT


def test_keyframe_response_allows_small_style_difference_but_rejects_identity_detail_mismatch() -> None:
    response = {
        "identity_consistency": 43,
        "scene_adherence": 25,
        "visual_cleanliness": 16,
        "total_score": 84,
        "verdict": "pass",
        "hard_failure": False,
        "reason": "Small framing difference; the right product and controls are visible.",
        "prompt_reference_consistent": True,
        "script_adherence": True,
        "important_details_preserved": True,
        "composition_usable": True,
        "animation_suitable": True,
        "recommended_action": "none",
    }
    assert validate_qc_response(response, QCStage.KEYFRAME)["verdict"] == "pass"

    wrong_color_or_port = {
        **response,
        "important_details_preserved": False,
        "reason": "The product color and port layout contradict the approved reference.",
    }
    assert validate_qc_response(wrong_color_or_port, QCStage.KEYFRAME)["verdict"] == "fail"


def test_pause_survives_reopen_and_worker_does_not_claim_or_send(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "pause.db"
    conn = connect(db_path)
    workflow_repo = WorkflowRepository(conn)
    start_product_video_workflow(
        workflow_repository=workflow_repo,
        workflow_id="wf-paused",
        product_name="Brand Model",
        keywords=["Brand Model"],
        project_id="proj-wf-paused",
    )
    pause_pipeline(
        conn,
        category=FailureCategory.QC_INFRA_PAUSED,
        code="QC_PROVIDER_UNAVAILABLE",
        message="All 9Router vision combo members are unavailable.",
        workflow_id="wf-paused",
    )
    conn.close()

    calls: list[str] = []
    report = run_worker(
        db_path=db_path,
        provider_mode="fake",
        once=True,
        batch_size=20,
        topic_handlers={"job.dispatch": lambda envelope: calls.append(envelope.workflow_id)},
    )

    check = connect(db_path)
    pause = get_pipeline_pause(check)
    outbox = check.execute(
        "SELECT state FROM workflow_outbox WHERE workflow_id='wf-paused'"
    ).fetchone()
    assert pause is not None
    assert pause["code"] == "QC_PROVIDER_UNAVAILABLE"
    assert report is not None and report.claimed == 0
    assert calls == []
    assert outbox["state"] == "pending"
    auto_result = ProductVideoCoordinator(check).start_auto(limit=1)
    assert auto_result.started == ()
    assert auto_result.skipped[0].reason.startswith("BATCH_STOPPED:QC_INFRA_PAUSED")
    check.close()

    from video_factory.cli import main

    assert main(["pipeline-status", "--db-path", str(db_path)]) == 2
    assert "QC_PROVIDER_UNAVAILABLE" in capsys.readouterr().out
    assert main([
        "pipeline-resume", "--db-path", str(db_path),
        "--note", "route fixed and provider attempt reconciled",
    ]) == 0
    conn = connect(db_path)
    assert get_pipeline_pause(conn) is None
    conn.close()


def test_pause_read_does_not_write_while_coordinator_holds_sqlite_write_lock(tmp_path: Path) -> None:
    from video_factory.workflows.runtime_control import get_pipeline_pause

    db_path = tmp_path / "pause-read-lock.db"
    coordinator_conn = connect(db_path)
    pause_pipeline(
        coordinator_conn,
        category=FailureCategory.QC_INFRA_PAUSED,
        code="QC_PROVIDER_UNAVAILABLE",
        message="route unavailable",
        workflow_id="wf-lock",
    )
    coordinator_conn.commit()
    worker_conn = connect(db_path)

    coordinator_conn.execute("BEGIN IMMEDIATE")
    coordinator_conn.execute(
        "UPDATE product_video_runtime_control SET message='coordinator transaction' WHERE state_id=1"
    )
    pause = get_pipeline_pause(worker_conn)

    assert pause is not None
    assert pause["code"] == "QC_PROVIDER_UNAVAILABLE"
    coordinator_conn.rollback()
    worker_conn.close()
    coordinator_conn.close()


def test_watcher_stops_before_candidate_scan_when_pipeline_is_paused(tmp_path: Path, monkeypatch, capsys) -> None:
    from types import SimpleNamespace

    from video_factory.cli import cmd_watch

    db_path = tmp_path / "watcher-paused.db"
    conn = connect(db_path)
    pause_pipeline(
        conn,
        category=FailureCategory.QC_INFRA_PAUSED,
        code="QC_PROVIDER_UNAVAILABLE",
        message="QC route is unavailable.",
        workflow_id="wf-paused",
    )
    conn.close()

    def fail_if_scanned(*_args, **_kwargs):
        raise AssertionError("watcher must not scan or start candidates while paused")

    monkeypatch.setattr("video_factory.cli.DailyCandidateService", fail_if_scanned)
    result = cmd_watch(SimpleNamespace(
        db_path=str(db_path),
        inbox_root=None,
        legacy_source_root=None,
        source_root=None,
        catalog_v2_candidates=None,
        interval=0,
        limit=1,
        once=True,
        provider_mode="fake",
        auto_approve=True,
    ))

    assert result == 2
    assert "QC_INFRA_PAUSED/QC_PROVIDER_UNAVAILABLE" in capsys.readouterr().out


def test_quarantined_qc_failure_is_operator_waiting_not_invalid_product(tmp_path: Path, monkeypatch) -> None:
    conn = connect(tmp_path / "candidate-qc-pause.db")
    canonical_key = "brand:model-qc-paused"
    product_key = compute_product_key(canonical_key)
    product = ExchangeProduct(
        priority=1,
        canonical_product_key=canonical_key,
        product_key=product_key,
        name="Brand Model QC-paused",
        brand="Brand",
        model="Model QC-paused",
        source_product_id="qc-paused",
        images=[],
    )
    manifest = SimpleNamespace(
        production_date="2026-10-21",
        batch_id="run-qc-paused",
        products=[product],
    )
    daily = DailyImportRepository(conn)
    claim = daily.claim_product(
        canonical_product_key=canonical_key,
        source_date=manifest.production_date,
        source_batch_id=manifest.batch_id,
        source_product_id=product.source_product_id,
        source_path="manifest.json",
        worker_id="test-worker",
    )
    daily.mark_started(claim.product_key)
    workflow_repo = WorkflowRepository(conn)
    start_product_video_workflow(
        workflow_repository=workflow_repo,
        workflow_id=claim.workflow_id,
        product_name=product.name,
        keywords=[product.model],
        project_id="project-qc-paused",
    )
    aggregate, version = workflow_repo.get_workflow(claim.workflow_id)
    aggregate.update({
        "state": ProductVideoWorkflowState.BUILDING_RESOURCE_PACK.value,
        "failure_category": FailureCategory.QC_INFRA_PAUSED.value,
        "failure_code": "QC_PROVIDER_UNAVAILABLE",
        "operator_reason": "9Router route preflight failed.",
        "operator_stage": "image",
    })
    workflow_repo.commit_workflow(claim.workflow_id, json.dumps(aggregate), [], expected_version=version)
    conn.execute(
        "UPDATE workflow_outbox SET state='quarantined', last_error=? WHERE workflow_id=?",
        ("Job execution failed [QC_PROVIDER_UNAVAILABLE]: route preflight failed", claim.workflow_id),
    )
    conn.commit()

    coordinator = ProductVideoCoordinator(conn)
    monkeypatch.setattr(coordinator.importer, "find_latest_ready_batch", lambda *_args: tmp_path)
    monkeypatch.setattr(coordinator.importer, "load_manifest", lambda _path: manifest)
    monkeypatch.setattr(coordinator.importer, "validate_admission", lambda *_args, **_kwargs: (True, None, []))

    candidate = coordinator.list_candidates(date(2026, 10, 21))[0]
    assert candidate.status == "waiting_for_operator"
    assert candidate.internal_state == FailureCategory.QC_INFRA_PAUSED.value
    assert "route preflight failed" in candidate.rejection_reason
    conn.close()


def test_governed_image_qc_resume_clears_infra_failure_and_reactivates_ledger(tmp_path: Path) -> None:
    from video_factory.cli import _advance_qc_waiting_workflow
    from video_factory.qc.models import QCStage

    conn = connect(tmp_path / "resume-image-qc.db")
    canonical_key = "brand:model-resume"
    daily = DailyImportRepository(conn)
    claim = daily.claim_product(
        canonical_product_key=canonical_key,
        source_date="2026-10-21",
        source_batch_id="run-resume",
        source_product_id="resume",
        source_path="manifest.json",
        worker_id="test-worker",
    )
    daily.mark_started(claim.product_key)
    workflow_repo = WorkflowRepository(conn)
    start_product_video_workflow(
        workflow_repository=workflow_repo,
        workflow_id=claim.workflow_id,
        product_name="Brand Model",
        keywords=["Brand Model"],
        project_id="project-resume",
    )
    aggregate, version = workflow_repo.get_workflow(claim.workflow_id)
    aggregate.update({
        "state": ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value,
        "failure_category": FailureCategory.QC_INFRA_PAUSED.value,
        "failure_code": "QC_PROVIDER_UNAVAILABLE",
        "failure_reason": "route preflight failed",
        "operator_action": "qc_provider_recovery",
        "operator_stage": "image",
        "resource_pack": {"resource_pack_id": "rp-resume", "version": 1},
    })
    workflow_repo.commit_workflow(claim.workflow_id, json.dumps(aggregate), [], expected_version=version)
    daily.mark_error_by_workflow_id(claim.workflow_id, "QC_INFRA_PAUSED:QC_PROVIDER_UNAVAILABLE")

    _advance_qc_waiting_workflow(conn, claim.workflow_id, QCStage.IMAGE)

    resumed, _ = workflow_repo.get_workflow(claim.workflow_id)
    ledger = conn.execute(
        "SELECT status,last_error FROM daily_product_video_import WHERE workflow_id=?",
        (claim.workflow_id,),
    ).fetchone()
    assert resumed["state"] == ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value
    assert "failure_category" not in resumed
    assert "failure_code" not in resumed
    assert ledger["status"] == "started"
    assert ledger["last_error"] is None
    conn.close()


@pytest.mark.parametrize(
    ("status", "code", "reason"),
    [
        (
            "pending",
            "QC_PROVIDER_UNAVAILABLE",
            "9Router local management API could not verify the route",
        ),
        ("failed", "QC_QUOTA_EXHAUSTED", "Image QC quota exhausted after fallback"),
    ],
)
def test_governed_qc_infrastructure_failure_persistently_pauses_checkout(
    tmp_path: Path, status: str, code: str, reason: str
) -> None:
    from video_factory.cli import _pause_pipeline_for_qc_result

    conn = connect(tmp_path / "qc-cli-pause.db")
    workflow_id = "wf-route-failed"
    repo = WorkflowRepository(conn)
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=workflow_id,
        product_name="Brand Model",
        keywords=["Brand Model"],
        project_id="proj-route-failed",
    )
    pause = _pause_pipeline_for_qc_result(
        conn,
        workflow_id,
        {
            "status": status,
            "code": code,
            "stage": "image",
            "reason": reason,
        },
    )

    assert pause == {
        "category": FailureCategory.QC_INFRA_PAUSED.value,
        "code": code,
        "message": reason,
    }
    stored = get_pipeline_pause(conn)
    assert stored is not None
    assert stored["workflow_id"] == workflow_id
    assert stored["code"] == code
    aggregate, _ = repo.get_workflow(workflow_id)
    assert aggregate["failure_category"] == FailureCategory.QC_INFRA_PAUSED.value
    assert aggregate["failure_code"] == code
    assert aggregate["failure_reason"] == reason
    assert aggregate["operator_action"] == "qc_provider_recovery"
    assert aggregate["operator_stage"] == "image"
    conn.close()


def test_governed_image_qc_product_failure_marks_terminal_skip(tmp_path: Path) -> None:
    from video_factory.cli import _mark_cli_image_qc_rejected

    conn = connect(tmp_path / "cli-image-reject.db")
    product_key = "brand:model-cli-image-reject"
    daily = DailyImportRepository(conn)
    claim = daily.claim_product(
        canonical_product_key=product_key,
        source_date="2026-10-21",
        source_batch_id="run-cli-reject",
        source_product_id="product-cli-reject",
        source_path="manifest.json",
        worker_id="test-worker",
    )
    workflow_id = claim.workflow_id
    daily.mark_started(claim.product_key)
    repo = WorkflowRepository(conn)
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=workflow_id,
        product_name="Brand Model",
        keywords=["Brand Model"],
        project_id="proj-cli-image-reject",
    )
    aggregate, version = repo.get_workflow(workflow_id)
    aggregate.update({
        "state": ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value,
        "operator_action": "qc_authorization",
        "operator_stage": "image",
        "product_key": claim.product_key,
    })
    repo.commit_workflow(workflow_id, json.dumps(aggregate), [], expected_version=version)
    conn.execute("DELETE FROM workflow_outbox WHERE workflow_id=?", (workflow_id,))
    conn.commit()

    rejected_key = _mark_cli_image_qc_rejected(
        conn,
        workflow_id,
        QCStage.IMAGE,
        {
            "status": "failed",
            "code": "IMAGE_QC_FAILED",
            "reason": "Only one distinct source image passed QC",
            "report_id": "qc-image-fail-1",
            "usage_status": "consumed",
            "evaluated_items": [{"asset_id": "asset-1", "verdict": "pass"}],
        },
    )

    rejected, _ = repo.get_workflow(workflow_id)
    ledger = conn.execute(
        "SELECT status,last_error FROM daily_product_video_import WHERE workflow_id=?",
        (workflow_id,),
    ).fetchone()
    assert rejected_key == claim.product_key
    assert rejected["state"] == "skipped"
    assert rejected["failure_category"] == FailureCategory.PRODUCT_REJECTED.value
    assert rejected["failure_report"]["report_id"] == "qc-image-fail-1"
    assert ledger["status"] == "error"
    assert ledger["last_error"].startswith("PRODUCT_REJECTED:PRODUCT_IMAGE_QC_REJECTED:")
    conn.close()


def test_requeue_only_known_presend_9router_quarantine_without_unpausing(tmp_path: Path) -> None:
    from video_factory.cli import _requeue_verified_presend_resource_pack

    conn = connect(tmp_path / "presend-requeue.db")
    repo = WorkflowRepository(conn)
    daily = DailyImportRepository(conn)
    claim = daily.claim_product(
        canonical_product_key="brand:presend-requeue",
        source_date="2026-10-21",
        source_batch_id="run-presend-requeue",
        source_product_id="presend-requeue",
        source_path="manifest.json",
        worker_id="test-worker",
    )
    workflow_id = claim.workflow_id
    daily.mark_error(claim.product_key, "Job execution failed [IMAGE_QC_ERROR]: pre-send route failure")
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=workflow_id,
        product_name="Brand Model",
        keywords=["Brand Model"],
        project_id="proj-presend-requeue",
    )
    aggregate, version = repo.get_workflow(workflow_id)
    aggregate["state"] = ProductVideoWorkflowState.BUILDING_RESOURCE_PACK.value
    repo.commit_workflow(workflow_id, json.dumps(aggregate), [], expected_version=version)
    row = conn.execute(
        "SELECT outbox_id,message FROM workflow_outbox WHERE workflow_id=?", (workflow_id,)
    ).fetchone()
    envelope = json.loads(row["message"])
    envelope["payload"]["job_name"] = "product_video.resource_pack"
    conn.execute(
        "UPDATE workflow_outbox SET state='quarantined', message=?, last_error=? WHERE outbox_id=?",
        (
            json.dumps(envelope),
            "Job execution failed [IMAGE_QC_ERROR]: Image QC service failed with unexpected error: "
            "9Router local management API could not verify the route",
            row["outbox_id"],
        ),
    )
    pause_pipeline(
        conn,
        category=FailureCategory.QC_INFRA_PAUSED,
        code="IMAGE_QC_ERROR",
        message="route verification failed before request",
        workflow_id=workflow_id,
    )

    requeued_id = _requeue_verified_presend_resource_pack(
        conn, workflow_id, route_preflight=lambda: None
    )

    outbox = conn.execute(
        "SELECT state,last_error,attempt_count FROM workflow_outbox WHERE outbox_id=?", (requeued_id,)
    ).fetchone()
    assert outbox["state"] == "pending"
    assert "management API could not verify" in outbox["last_error"]
    ledger = conn.execute(
        "SELECT status,last_error FROM daily_product_video_import WHERE workflow_id=?", (workflow_id,)
    ).fetchone()
    assert ledger["status"] == "started"
    assert ledger["last_error"] is None
    pause = get_pipeline_pause(conn)
    assert pause is not None
    assert "remains paused" in pause["message"]
    attempt_count = outbox["attempt_count"]
    assert _requeue_verified_presend_resource_pack(
        conn, workflow_id, route_preflight=lambda: None
    ) == requeued_id
    assert conn.execute(
        "SELECT attempt_count FROM workflow_outbox WHERE outbox_id=?", (requeued_id,)
    ).fetchone()["attempt_count"] == attempt_count
    conn.close()


def test_requeue_known_presend_quarantine_in_same_paused_checkout_batch(tmp_path: Path) -> None:
    from video_factory.cli import _requeue_verified_presend_resource_pack

    conn = connect(tmp_path / "presend-requeue-batch.db")
    repo = WorkflowRepository(conn)
    owner_id = "wf-presend-pause-owner"
    target_id = "wf-presend-batch-member"
    for workflow_id in (owner_id, target_id):
        start_product_video_workflow(
            workflow_repository=repo,
            workflow_id=workflow_id,
            product_name="Brand Model",
            keywords=["Brand Model"],
            project_id=f"proj-{workflow_id}",
        )
        aggregate, version = repo.get_workflow(workflow_id)
        aggregate["state"] = ProductVideoWorkflowState.BUILDING_RESOURCE_PACK.value
        repo.commit_workflow(workflow_id, json.dumps(aggregate), [], expected_version=version)
        if workflow_id == target_id:
            row = conn.execute(
                "SELECT outbox_id,message FROM workflow_outbox WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
            envelope = json.loads(row["message"])
            envelope["payload"]["job_name"] = "product_video.resource_pack"
            conn.execute(
                "UPDATE workflow_outbox SET state='quarantined',message=?,last_error=? WHERE outbox_id=?",
                (
                    json.dumps(envelope),
                    "Job execution failed [IMAGE_QC_ERROR]: Image QC service failed with unexpected error: "
                    "9Router local management API could not verify the route",
                    row["outbox_id"],
                ),
            )
    pause_pipeline(
        conn,
        category=FailureCategory.QC_INFRA_PAUSED,
        code="IMAGE_QC_ERROR",
        message="route verification failed before request",
        workflow_id=owner_id,
    )

    outbox_id = _requeue_verified_presend_resource_pack(
        conn, target_id, route_preflight=lambda: None
    )

    outbox = conn.execute(
        "SELECT state,last_error FROM workflow_outbox WHERE outbox_id=?", (outbox_id,)
    ).fetchone()
    pause = get_pipeline_pause(conn)
    assert outbox["state"] == "pending"
    assert "management API could not verify" in outbox["last_error"]
    assert pause is not None and pause["workflow_id"] == owner_id
    assert "remains paused" in pause["message"]
    conn.close()


def test_requeue_rejects_uncertain_quarantine_even_if_route_preflight_passes(tmp_path: Path) -> None:
    from video_factory.cli import _requeue_verified_presend_resource_pack

    conn = connect(tmp_path / "uncertain-requeue.db")
    repo = WorkflowRepository(conn)
    workflow_id = "wf-uncertain-requeue"
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=workflow_id,
        product_name="Brand Model",
        keywords=["Brand Model"],
        project_id="proj-uncertain-requeue",
    )
    aggregate, version = repo.get_workflow(workflow_id)
    aggregate["state"] = ProductVideoWorkflowState.BUILDING_RESOURCE_PACK.value
    repo.commit_workflow(workflow_id, json.dumps(aggregate), [], expected_version=version)
    row = conn.execute(
        "SELECT outbox_id,message FROM workflow_outbox WHERE workflow_id=?", (workflow_id,)
    ).fetchone()
    envelope = json.loads(row["message"])
    envelope["payload"]["job_name"] = "product_video.resource_pack"
    conn.execute(
        "UPDATE workflow_outbox SET state='quarantined', message=?, last_error=? WHERE outbox_id=?",
        (
            json.dumps(envelope),
            "Job execution failed [IMAGE_QC_ERROR]: request may have been sent; "
            "reconciliation is required before retry",
            row["outbox_id"],
        ),
    )
    pause_pipeline(
        conn,
        category=FailureCategory.QC_INFRA_PAUSED,
        code="IMAGE_QC_ERROR",
        message="request outcome uncertain",
        workflow_id=workflow_id,
    )

    with pytest.raises(ValueError, match="not the verified pre-send"):
        _requeue_verified_presend_resource_pack(conn, workflow_id, route_preflight=lambda: None)
    assert conn.execute(
        "SELECT state FROM workflow_outbox WHERE outbox_id=?", (row["outbox_id"],)
    ).fetchone()["state"] == "quarantined"
    assert get_pipeline_pause(conn) is not None
    conn.close()


@pytest.mark.parametrize("attempt_status", ["reserved", "sent", "technical_unknown"])
def test_requeue_and_resume_refuse_unresolved_qc_retry_ledger(
    tmp_path: Path, capsys, attempt_status: str
) -> None:
    from video_factory.cli import _requeue_verified_presend_resource_pack
    from video_factory.qc.retry_policy import ensure_qc_retry_schema

    conn = connect(tmp_path / "ledger-unknown-requeue.db")
    repo = WorkflowRepository(conn)
    workflow_id = "wf-ledger-unknown-requeue"
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=workflow_id,
        product_name="Brand Model",
        keywords=["Brand Model"],
        project_id="proj-ledger-unknown-requeue",
    )
    aggregate, version = repo.get_workflow(workflow_id)
    aggregate["state"] = ProductVideoWorkflowState.BUILDING_RESOURCE_PACK.value
    repo.commit_workflow(workflow_id, json.dumps(aggregate), [], expected_version=version)
    row = conn.execute(
        "SELECT outbox_id,message FROM workflow_outbox WHERE workflow_id=?", (workflow_id,)
    ).fetchone()
    envelope = json.loads(row["message"])
    envelope["payload"]["job_name"] = "product_video.resource_pack"
    conn.execute(
        "UPDATE workflow_outbox SET state='quarantined', message=?, last_error=? WHERE outbox_id=?",
        (
            json.dumps(envelope),
            "Job execution failed [IMAGE_QC_ERROR]: Image QC service failed with unexpected error: "
            "9Router local management API could not verify the route",
            row["outbox_id"],
        ),
    )
    ensure_qc_retry_schema(conn)
    conn.execute(
        """INSERT INTO workflow_qc_retries
           (workflow_id,retry_type,item_id,attempt_number,verdict,operation_id,status,created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (workflow_id, "source_image_qc", "sku", 1, "unknown", "op-unknown", attempt_status, "now"),
    )
    pause_pipeline(
        conn,
        category=FailureCategory.QC_INFRA_PAUSED,
        code="IMAGE_QC_ERROR",
        message="QC outcome requires reconciliation",
        workflow_id=workflow_id,
    )

    with pytest.raises(ValueError, match="retry ledger contains an unresolved attempt"):
        _requeue_verified_presend_resource_pack(conn, workflow_id, route_preflight=lambda: None)
    assert conn.execute(
        "SELECT state FROM workflow_outbox WHERE outbox_id=?", (row["outbox_id"],)
    ).fetchone()["state"] == "quarantined"
    assert get_pipeline_pause(conn) is not None
    conn.close()

    from video_factory.cli import main

    assert main([
        "pipeline-resume", "--db-path", str(tmp_path / "ledger-unknown-requeue.db"),
        "--note", "route appears repaired",
    ]) == 1
    assert f"status={attempt_status}" in capsys.readouterr().err
    check = connect(tmp_path / "ledger-unknown-requeue.db")
    assert get_pipeline_pause(check) is not None
    check.close()


def test_pipeline_resume_refuses_sent_qc_operation_attempt(tmp_path: Path, capsys) -> None:
    from video_factory.cli import main

    db_path = tmp_path / "operation-sent-resume.db"
    conn = connect(db_path)
    pause_pipeline(
        conn,
        category=FailureCategory.QC_INFRA_PAUSED,
        code="QC_OUTCOME_UNCERTAIN",
        message="provider operation needs reconciliation",
        workflow_id="wf-operation-sent",
    )
    conn.execute(
        """INSERT INTO product_video_operation_attempt
           (attempt_id,authorization_id,campaign_id,owner_user_id,workflow_id,capability_id,
            attempt_number,input_fingerprint,status,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "attempt-sent", "auth-1", "campaign-1", "local-owner", "wf-operation-sent",
            "product_video.qc.image", 1, "sha256:input", "sent", "now", "now",
        ),
    )
    conn.commit()
    conn.close()

    assert main([
        "pipeline-resume", "--db-path", str(db_path), "--note", "route is available again",
    ]) == 1
    assert "provider operation ledger" in capsys.readouterr().err
    check = connect(db_path)
    assert get_pipeline_pause(check) is not None
    check.close()


@pytest.mark.parametrize("attempt_status", ["reserved", "sent"])
def test_start_auto_stops_for_unresolved_qc_retry_ledger(
    tmp_path: Path, attempt_status: str
) -> None:
    from video_factory.qc.retry_policy import ensure_qc_retry_schema

    conn = connect(tmp_path / f"start-auto-{attempt_status}.db")
    ensure_qc_retry_schema(conn)
    conn.execute(
        """INSERT INTO workflow_qc_retries
           (workflow_id,retry_type,item_id,attempt_number,verdict,operation_id,status,created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            "wf-active-qc", "source_image_qc", "brand:model", 1, "unknown",
            f"op-{attempt_status}", attempt_status, "now",
        ),
    )
    conn.commit()

    result = ProductVideoCoordinator(conn).start_auto(
        limit=1, target_date=date(2026, 10, 21)
    )

    assert result.started == ()
    assert result.skipped
    assert result.skipped[0].reason.startswith("BATCH_STOPPED:QC_OUTCOME_UNCERTAIN")
    assert f"status={attempt_status}" in result.skipped[0].reason
    conn.close()


def test_pipeline_status_shows_requeued_job_waiting_behind_pause(tmp_path: Path, capsys) -> None:
    from video_factory.cli import main

    db_path = tmp_path / "paused-pending-job.db"
    conn = connect(db_path)
    repo = WorkflowRepository(conn)
    workflow_id = "wf-paused-pending-job"
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=workflow_id,
        product_name="Brand Model",
        keywords=["Brand Model"],
        project_id="proj-paused-pending-job",
    )
    row = conn.execute(
        "SELECT outbox_id,message FROM workflow_outbox WHERE workflow_id=?", (workflow_id,)
    ).fetchone()
    envelope = json.loads(row["message"])
    envelope["payload"]["job_name"] = "product_video.resource_pack"
    conn.execute(
        "UPDATE workflow_outbox SET message=?,state='pending' WHERE outbox_id=?",
        (json.dumps(envelope), row["outbox_id"]),
    )
    pause_pipeline(
        conn,
        category=FailureCategory.QC_INFRA_PAUSED,
        code="QC_PROVIDER_UNAVAILABLE",
        message="route repaired; operator has not resumed",
        workflow_id=workflow_id,
    )
    conn.close()

    assert main(["pipeline-status", "--db-path", str(db_path)]) == 2
    output = capsys.readouterr().out
    assert "Pending job:" in output
    assert "job=product_video.resource_pack" in output
    assert "worker will not claim it while the pause is active" in output
    assert "pipeline-resume with a note" in output


def test_legacy_quarantined_qc_error_restores_pause_before_worker_claim(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "legacy-quarantine.db"
    conn = connect(db_path)
    repo = WorkflowRepository(conn)
    workflow_id = "wf-legacy-route-failure"
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=workflow_id,
        product_name="Brand Model",
        keywords=["Brand Model"],
        project_id="proj-legacy-route-failure",
    )
    conn.execute(
        "UPDATE workflow_outbox SET state='quarantined', last_error=? WHERE workflow_id=?",
        (
            "Job execution failed [IMAGE_QC_ERROR]: Image QC service failed with unexpected error: "
            "9Router local management API could not verify the route",
            workflow_id,
        ),
    )
    conn.commit()
    conn.close()

    sends: list[str] = []
    report = run_worker(
        db_path=db_path,
        provider_mode="fake",
        once=True,
        topic_handlers={"job.dispatch": lambda envelope: sends.append(envelope.workflow_id)},
    )
    check = connect(db_path)
    pause = ensure_pipeline_pause(check)
    assert report is not None and report.claimed == 0
    assert sends == []
    assert pause is not None
    assert pause["category"] == "QC_INFRA_PAUSED"
    assert pause["code"] == "IMAGE_QC_ERROR"
    assert pause["workflow_id"] == workflow_id
    assert check.execute(
        "SELECT state FROM workflow_outbox WHERE workflow_id=?", (workflow_id,)
    ).fetchone()["state"] == "quarantined"
    check.close()

    from video_factory.cli import main

    assert main(["pipeline-status", "--db-path", str(db_path)]) == 2
    status_output = capsys.readouterr().out
    assert "9Router local management API could not verify the route" in status_output
    assert f"pipeline-requeue-preflight {workflow_id}" in status_output
    from video_factory.workflows.runtime_control import resume_pipeline

    check = connect(db_path)
    assert resume_pipeline(check, note="route repaired and attempt reconciled") is True
    assert ensure_pipeline_pause(check) is None
    check.close()


def test_scene_plan_digest_error_quarantines_and_pauses_as_contract_broken(tmp_path: Path) -> None:
    conn = connect(tmp_path / "contract.db")
    repo = WorkflowRepository(conn)
    workflow_id = "wf-contract-broken"
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=workflow_id,
        product_name="Brand Model",
        keywords=["Brand Model"],
        project_id="proj-contract-broken",
    )
    conn.execute("DELETE FROM workflow_outbox WHERE workflow_id=?", (workflow_id,))
    aggregate, version = repo.get_workflow(workflow_id)
    aggregate["state"] = "creating_scene_plan"
    repo.commit_workflow(
        workflow_id=workflow_id,
        aggregate_json=json.dumps(aggregate),
        events=[],
        expected_version=version,
    )

    envelope = build_envelope(
        workflow_id=workflow_id,
        topic="job.dispatch",
        idempotency_key="scene-plan-digest-test",
        payload={"job_name": "product_video.scene_plan", "workflow_id": workflow_id, "args": [], "kwargs": {}},
    )
    OutboxRepository(conn).enqueue(workflow_id, envelope)

    class BrokenScenePlanHandler:
        def get_handlers_map(self):
            def fail(*_args, **_kwargs):
                raise ValueError("scene plan digest mismatch")

            return {"product_video.scene_plan": fail}

    handlers = create_product_video_topic_handlers(
        workflow_repository=repo,
        handlers_service=BrokenScenePlanHandler(),
        provider_mode="fake",
    )
    report = OutboxDispatcher(
        repository=OutboxRepository(conn), topic_handlers=handlers,
    ).process_batch(batch_size=1, workflow_ids=[workflow_id])

    pause = get_pipeline_pause(conn)
    updated, _ = repo.get_workflow(workflow_id)
    assert report.quarantined == 1
    assert pause is not None and pause["category"] == "CONTRACT_BROKEN"
    assert pause["code"] == "SCENE_PLAN_DIGEST_MISMATCH"
    assert updated["state"] == "contract_broken"
    assert updated["failure_category"] == "CONTRACT_BROKEN"
    conn.close()


@pytest.mark.parametrize("code", ["MISSING_INPUT_REF", "PREREQUISITE_FAILED"])
def test_missing_workflow_artifact_records_and_pauses_as_contract_broken(
    tmp_path: Path, code: str,
) -> None:
    conn = connect(tmp_path / f"{code.lower()}.db")
    repo = WorkflowRepository(conn)
    workflow_id = f"wf-{code.lower()}"
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=workflow_id,
        product_name="Brand Model",
        keywords=["Brand Model"],
        project_id=f"proj-{workflow_id}",
    )
    conn.execute("DELETE FROM workflow_outbox WHERE workflow_id=?", (workflow_id,))
    envelope = build_envelope(
        workflow_id=workflow_id,
        topic="job.dispatch",
        idempotency_key=f"{code.lower()}-test",
        payload={
            "job_name": "product_video.scene_plan",
            "workflow_id": workflow_id,
            "args": [],
            "kwargs": {},
        },
    )
    OutboxRepository(conn).enqueue(workflow_id, envelope)

    class BrokenInputHandler:
        def get_handlers_map(self):
            return {
                "product_video.scene_plan": lambda *_args, **_kwargs: JobFailed(
                    retryable=False,
                    code=code,
                    message="Required workflow artifact is missing.",
                )
            }

    handlers = create_product_video_topic_handlers(
        workflow_repository=repo,
        handlers_service=BrokenInputHandler(),
        provider_mode="fake",
    )
    report = OutboxDispatcher(
        repository=OutboxRepository(conn), topic_handlers=handlers,
    ).process_batch(batch_size=1, workflow_ids=[workflow_id])

    pause = get_pipeline_pause(conn)
    aggregate, _ = repo.get_workflow(workflow_id)
    assert report.succeeded == 1 and report.quarantined == 0
    assert pause is not None and pause["category"] == "CONTRACT_BROKEN"
    assert pause["code"] == code
    assert aggregate["state"] == "contract_broken"
    assert aggregate["failure_category"] == "CONTRACT_BROKEN"
    conn.close()


@pytest.mark.parametrize("failure_code", [
    "EXCHANGE_IMAGE_QC_FAILED", "CREATIVE_RESPONSE_INVALID", "PRODUCT_IDENTITY_REQUIRED",
])
def test_start_auto_selects_next_catalog_product_after_terminal_product_rejection(
    tmp_path: Path, failure_code: str,
) -> None:
    inbox = tmp_path / "inbox"
    batch_dir = inbox / "2026-10-21" / "run_batch_skip"
    catalog = tmp_path / "catalog.csv"
    db_path = tmp_path / "batch-skip.db"
    batch_dir.mkdir(parents=True)

    products = []
    catalog_rows = ["canonical_product_key,identity_status,pipeline_status"]
    for priority, (slug, brand, model) in enumerate(
        [("alpha", "Brand", "Model A"), ("beta", "Brand", "Model B")],
        start=1,
    ):
        canonical = f"brand:model-{slug}"
        images = []
        for index, color in enumerate(("white", "black"), start=1):
            relative = f"products/{slug}/image-{index}.png"
            image_path = batch_dir / relative
            image_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (24, 24), color=color).save(image_path, format="PNG")
            images.append({
                "asset_id": f"{slug}-image-{index}",
                "relative_path": relative,
                "match_score": 1.0,
            })
        products.append({
            "priority": priority,
            "source_product_id": slug,
            "canonical_product_key": canonical,
            "product_key": compute_product_key(canonical),
            "name": f"Brand {model}",
            "brand": brand,
            "model": model,
            "images": images,
        })
        catalog_rows.append(f"{canonical},VALID,READY_FOR_IMAGE_QC")

    catalog.write_text("\n".join(catalog_rows) + "\n", encoding="utf-8")
    (batch_dir / "manifest.json").write_text(json.dumps({
        "schema_version": "1.0.0",
        "batch_id": "run_batch_skip",
        "production_date": "2026-10-21",
        "products": products,
    }), encoding="utf-8")
    (batch_dir / "READY").write_text("READY\n", encoding="utf-8")

    conn = connect(db_path)
    coordinator = ProductVideoCoordinator(
        conn,
        inbox_root=inbox,
        db_path=db_path,
        catalog_candidates_path=catalog,
    )
    first = coordinator.start_auto(limit=1, target_date=date(2026, 10, 21))
    assert len(first.started) == 1
    assert first.started[0].canonical_product_key == "brand:model-alpha"
    rejected_workflow = first.started[0].workflow_id

    conn.execute("DELETE FROM workflow_outbox WHERE workflow_id=?", (rejected_workflow,))
    failed_envelope = build_envelope(
        workflow_id=rejected_workflow,
        topic="job.dispatch",
        idempotency_key="semantic-image-fail-alpha",
        payload={
            "job_name": "product_video.resource_pack",
            "workflow_id": rejected_workflow,
            "args": [],
            "kwargs": {},
        },
    )
    OutboxRepository(conn).enqueue(rejected_workflow, failed_envelope)

    class SemanticFailHandler:
        def get_handlers_map(self):
            return {
                "product_video.resource_pack": lambda *_args, **_kwargs: JobFailed(
                    retryable=False,
                    code=failure_code,
                    message=(
                        "Creative brief remained invalid after one repair request."
                        if failure_code == "CREATIVE_RESPONSE_INVALID"
                        else "PRODUCT_IDENTITY_REQUIRED: Add a verified product name, brand, and variant."
                        if failure_code == "PRODUCT_IDENTITY_REQUIRED"
                        else "Valid Image QC report rejected both source images for Model A."
                    ),
                    artifact_ref={"report_id": "qc-alpha-fail"},
                )
            }

    handlers = create_product_video_topic_handlers(
        workflow_repository=WorkflowRepository(conn),
        handlers_service=SemanticFailHandler(),
        provider_mode="fake",
    )
    dispatch = OutboxDispatcher(
        repository=OutboxRepository(conn), topic_handlers=handlers,
    ).process_batch(batch_size=1, workflow_ids=[rejected_workflow])
    assert dispatch.succeeded == 1 and dispatch.quarantined == 0
    rejected, _ = WorkflowRepository(conn).get_workflow(rejected_workflow)
    if failure_code == "CREATIVE_RESPONSE_INVALID":
        assert rejected["state"] == ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
        assert rejected["operator_action"] == "creative_review"
    else:
        assert rejected["failure_category"] == "PRODUCT_REJECTED"
        assert rejected["state"] == "skipped"
        if failure_code == "PRODUCT_IDENTITY_REQUIRED":
            assert "verified product name, brand, and variant" in rejected["skip_reason"]

    second = coordinator.start_auto(limit=1, target_date=date(2026, 10, 21))
    assert len(second.started) == 1
    assert second.started[0].canonical_product_key == "brand:model-beta"
    assert second.started[0].workflow_id != rejected_workflow
    if failure_code == "CREATIVE_RESPONSE_INVALID":
        assert any(item.reason.startswith("PRODUCT_RECONCILIATION_REQUIRED:CREATIVE_RESPONSE_INVALID")
                   for item in second.skipped)
    conn.close()


@pytest.mark.parametrize(
    "code",
    [
        "QC_PROVIDER_UNAVAILABLE",
        "MISSING_PROVIDER_CONFIG",
        "QC_RESPONSE_INVALID",
        "QC_OUTCOME_UNCERTAIN",
        "QC_QUOTA_EXHAUSTED",
        "QC_RETRY_EXHAUSTED",
        "RATE_LIMITED",
        "HTTP_429",
        "PROVIDER_TIMEOUT",
    ],
)
def test_qc_provider_failure_pauses_without_skipping_product(tmp_path: Path, code: str) -> None:
    db_path = tmp_path / f"{code}.db"
    conn = connect(db_path)
    repo = WorkflowRepository(conn)
    canonical_key = f"brand:{code.lower()}"
    daily = DailyImportRepository(conn)
    claim = daily.claim_product(
        canonical_product_key=canonical_key,
        source_date="2026-10-21",
        source_batch_id="run-qc-failure",
        source_product_id=code.lower(),
        source_path="manifest.json",
        worker_id="test-worker",
    )
    workflow_id = claim.workflow_id
    daily.mark_started(claim.product_key)
    failed_pack = {"schema": "resource_pack", "resource_pack_id": f"rp-{code.lower()}", "assets": []}
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=workflow_id,
        product_name="Brand Model",
        keywords=["Brand Model"],
        project_id=f"proj-{workflow_id}",
    )
    conn.execute("DELETE FROM workflow_outbox WHERE workflow_id=?", (workflow_id,))
    envelope = build_envelope(
        workflow_id=workflow_id,
        topic="job.dispatch",
        idempotency_key=f"qc-failure-{code}",
        payload={"job_name": "product_video.resource_pack", "workflow_id": workflow_id, "args": [], "kwargs": {}},
    )
    OutboxRepository(conn).enqueue(workflow_id, envelope)

    class FailedQCHandler:
        def get_handlers_map(self):
            return {
                "product_video.resource_pack": lambda *_args, **_kwargs: JobFailed(
                    retryable=False,
                    code=code,
                    message=f"simulated governed QC failure {code}",
                    artifact_ref=failed_pack,
                )
            }

    handlers = create_product_video_topic_handlers(
        workflow_repository=repo,
        handlers_service=FailedQCHandler(),
        provider_mode="fake",
    )
    report = OutboxDispatcher(
        repository=OutboxRepository(conn), topic_handlers=handlers,
    ).process_batch(batch_size=1, workflow_ids=[workflow_id])

    pause = get_pipeline_pause(conn)
    aggregate, _ = repo.get_workflow(workflow_id)
    assert report.succeeded == 1
    assert aggregate["state"] == "WaitingForOperator"
    assert aggregate["failure_category"] == "QC_INFRA_PAUSED"
    assert aggregate.get("state") != "skipped"
    assert aggregate["resource_pack"] == failed_pack
    if code in {"QC_RESPONSE_INVALID", "QC_SCHEMA_INVALID"}:
        assert pause is None
    else:
        assert pause is not None and pause["category"] == "QC_INFRA_PAUSED"
        assert pause["code"] == code
    ledger_row = conn.execute(
        "SELECT status,last_error FROM daily_product_video_import WHERE workflow_id=?",
        (workflow_id,),
    ).fetchone()
    assert ledger_row["status"] == "started"
    assert ledger_row["last_error"] is None
    conn.close()

    sends: list[str] = []
    restarted = run_worker(
        db_path=db_path,
        provider_mode="fake",
        once=True,
        topic_handlers={"job.dispatch": lambda envelope: sends.append(envelope.workflow_id)},
    )
    assert restarted is not None and restarted.claimed == 0
    assert sends == []


def test_pause_scope_distinguishes_per_product_from_shared_infra(tmp_path: Path) -> None:
    db_path = tmp_path / "scope.db"
    conn = connect(db_path)
    workflow_repo = WorkflowRepository(conn)
    start_product_video_workflow(
        workflow_repository=workflow_repo,
        workflow_id="wf-A",
        product_name="Product A",
        keywords=["Product A"],
        project_id="proj-wf-A",
    )
    start_product_video_workflow(
        workflow_repository=workflow_repo,
        workflow_id="wf-B",
        product_name="Product B",
        keywords=["Product B"],
        project_id="proj-wf-B",
    )

    # 1. Blocker rieng cua Item A (KEYFRAME_CONTENT_UNKNOWN) KHONG chan Item B
    pause_pipeline(
        conn,
        category=FailureCategory.QC_INFRA_PAUSED,
        code="KEYFRAME_CONTENT_UNKNOWN",
        message="Keyframe content requires reconciliation for wf-A",
        workflow_id="wf-A",
    )
    conn.close()

    calls_b: list[str] = []
    report_b = run_worker(
        db_path=db_path,
        provider_mode="fake",
        once=True,
        workflow_ids=["wf-B"],
        topic_handlers={"job.dispatch": lambda envelope: calls_b.append(envelope.workflow_id)},
    )
    assert report_b is not None and report_b.claimed == 1
    assert calls_b == ["wf-B"]

    # 2. Pause tai nguyen dung chung (PROVIDER_CAPACITY_WAIT) VAN CHAN Item B du goi rieng workflow_ids=['wf-B']
    conn = connect(db_path)
    pause_pipeline(
        conn,
        category=FailureCategory.QC_INFRA_PAUSED,
        code="PROVIDER_CAPACITY_WAIT",
        message="Google Flow capacity wait across all operations",
        workflow_id="wf-A",
    )
    conn.close()

    calls_shared: list[str] = []
    report_shared = run_worker(
        db_path=db_path,
        provider_mode="fake",
        once=True,
        workflow_ids=["wf-B"],
        topic_handlers={"job.dispatch": lambda envelope: calls_shared.append(envelope.workflow_id)},
    )
    assert report_shared is not None and report_shared.claimed == 0
    assert calls_shared == []


def test_capacity_wait_blocks_new_submissions_without_deadlocking_active_operation(tmp_path: Path) -> None:
    db_path = tmp_path / "deadlock_prevention.db"
    conn = connect(db_path)
    workflow_repo = WorkflowRepository(conn)
    start_product_video_workflow(
        workflow_repository=workflow_repo,
        workflow_id="wf-active-A",
        product_name="Product A",
        keywords=["Product A"],
        project_id="proj-wf-A",
    )
    start_product_video_workflow(
        workflow_repository=workflow_repo,
        workflow_id="wf-waiting-B",
        product_name="Product B",
        keywords=["Product B"],
        project_id="proj-wf-B",
    )

    # Simulating wf-active-A currently holding an in-flight operation
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS video_factory_keyframe_operation (
            workflow_id TEXT, frame_id TEXT, operation_id TEXT, status TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO video_factory_keyframe_operation VALUES ('wf-active-A', 'f1', 'op-A-running', 'running')"
    )

    # wf-waiting-B encountered capacity limit, causing pause
    pause_pipeline(
        conn,
        category=FailureCategory.QC_INFRA_PAUSED,
        code="PROVIDER_CAPACITY_WAIT",
        message="Google Flow capacity wait: 1 operation slot in use",
        workflow_id="wf-waiting-B",
    )
    conn.close()

    # 1. New submissions from waiting workflow B are BLOCKED
    calls_b: list[str] = []
    report_b = run_worker(
        db_path=db_path,
        provider_mode="fake",
        once=True,
        workflow_ids=["wf-waiting-B"],
        topic_handlers={"job.dispatch": lambda env: calls_b.append(env.workflow_id)},
    )
    assert report_b is not None and report_b.claimed == 0
    assert calls_b == []

    # 2. But active workflow A with in-flight operation is NOT deadlocked!
    # It can run its worker tick to poll, reconcile, and finalize
    calls_a: list[str] = []
    report_a = run_worker(
        db_path=db_path,
        provider_mode="fake",
        once=True,
        workflow_ids=["wf-active-A"],
        topic_handlers={"job.dispatch": lambda env: calls_a.append(env.workflow_id)},
    )
    assert report_a is not None and report_a.claimed == 1
    assert calls_a == ["wf-active-A"]


@pytest.mark.parametrize("workflow_ids", [None, ["wf-active-A", "wf-waiting-B"]])
def test_capacity_wait_dispatches_only_active_operations_when_unfiltered_or_mixed(
    tmp_path: Path, workflow_ids: list[str] | None,
) -> None:
    db_path = tmp_path / f"capacity-{bool(workflow_ids)}.db"
    conn = connect(db_path)
    repo = WorkflowRepository(conn)
    start_product_video_workflow(repo, workflow_id="wf-active-A", product_name="Product A",
        keywords=["A"], project_id="project-A")
    start_product_video_workflow(repo, workflow_id="wf-waiting-B", product_name="Product B",
        keywords=["B"], project_id="project-B")
    conn.execute("CREATE TABLE video_factory_keyframe_operation (workflow_id TEXT, frame_id TEXT, operation_id TEXT, status TEXT)")
    conn.execute("INSERT INTO video_factory_keyframe_operation VALUES ('wf-active-A','frame-1','op-A','running')")
    pause_pipeline(conn, category=FailureCategory.QC_INFRA_PAUSED, code="PROVIDER_CAPACITY_WAIT",
        message="capacity full", workflow_id="wf-waiting-B")
    conn.close()

    dispatched: list[str] = []
    report = run_worker(db_path=db_path, provider_mode="fake", once=True,
        workflow_ids=workflow_ids, topic_handlers={"job.dispatch":lambda env:dispatched.append(env.workflow_id)})

    assert report is not None and report.claimed == 1
    assert dispatched == ["wf-active-A"]


def test_background_worker_preserves_workflow_filter(tmp_path: Path) -> None:
    import threading

    db_path = tmp_path / "background-scope.db"
    conn = connect(db_path)
    repo = WorkflowRepository(conn)
    start_product_video_workflow(repo, workflow_id="wf-A", product_name="Product A",
        keywords=["A"], project_id="project-A")
    start_product_video_workflow(repo, workflow_id="wf-B", product_name="Product B",
        keywords=["B"], project_id="project-B")
    conn.close()

    stop = threading.Event()
    dispatched: list[str] = []
    def deliver(envelope):
        dispatched.append(envelope.workflow_id)
        stop.set()

    run_worker(db_path=db_path, provider_mode="fake", once=False, stop_event=stop,
        poll_interval=0.001, workflow_ids=["wf-B"], workflow_id_prefix="wf-B",
        topic_handlers={"job.dispatch":deliver})

    assert dispatched == ["wf-B"]


def test_product_scoped_pause_blocks_only_its_selected_identity(tmp_path: Path) -> None:
    db_path = tmp_path / "selection-pause.db"
    conn = connect(db_path)
    repo = WorkflowRepository(conn)
    start_product_video_workflow(repo, workflow_id="wf-A", product_name="Product A",
        keywords=["A"], project_id="project-A")
    aggregate, version = repo.get_workflow("wf-A")
    aggregate["source_context"] = {"canonical_product_key":"sku-A"}
    repo.commit_workflow("wf-A", json.dumps(aggregate), [], expected_version=version)
    pause_pipeline(conn, category=FailureCategory.QC_INFRA_PAUSED,
        code="KEYFRAME_CONTENT_UNKNOWN", message="wf-A needs reconciliation", workflow_id="wf-A")

    coordinator = ProductVideoCoordinator(conn)
    pause = get_pipeline_pause(conn)
    assert coordinator._pause_blocks_selection(pause, {"sku-B"}) is False
    assert coordinator._pause_blocks_selection(pause, {"sku-A"}) is True
    conn.close()


@pytest.mark.parametrize("workflow_ids", [None, ["wf-A", "wf-B"]])
def test_worker_product_pause_excludes_only_paused_workflow(tmp_path: Path,
        workflow_ids: list[str] | None) -> None:
    db_path = tmp_path / f"product-pause-worker-{bool(workflow_ids)}.db"
    conn = connect(db_path)
    repo = WorkflowRepository(conn)
    start_product_video_workflow(repo, workflow_id="wf-A", product_name="Product A",
        keywords=["A"], project_id="project-A")
    start_product_video_workflow(repo, workflow_id="wf-B", product_name="Product B",
        keywords=["B"], project_id="project-B")
    pause_pipeline(conn, category=FailureCategory.QC_INFRA_PAUSED, code="KEYFRAME_CONTENT_UNKNOWN",
        message="wf-A requires reconciliation", workflow_id="wf-A")
    conn.close()

    dispatched: list[str] = []
    report = run_worker(db_path=db_path, provider_mode="fake", once=True,
        workflow_ids=workflow_ids, topic_handlers={"job.dispatch":lambda env:dispatched.append(env.workflow_id)})

    assert report is not None and report.claimed == 1
    assert dispatched == ["wf-B"]
