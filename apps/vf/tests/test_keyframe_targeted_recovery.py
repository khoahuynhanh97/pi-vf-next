from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from video_factory.application.operator_handler import enqueue_keyframe_recovery
from video_factory.cli import main as cli_main
from video_factory.handlers import start_product_video_workflow
from video_factory.qc.models import QCReport, QCStage, QCVerdict
from video_factory.qc.repository import QCReportRepository
from video_factory.qc.retry_policy import (
    claim_qc_attempt,
    complete_qc_attempt,
    get_item_remaining_recoveries,
    get_item_retry_count,
    plan_targeted_keyframe_recovery,
)
from video_factory.storage_infra.sqlite import connect
from video_factory.workflow import ProductVideoWorkflowState
from video_factory.workflows.repository import WorkflowRepository


def test_keyframe_recovery_planning_start_end_cascade(tmp_path: Path) -> None:
    """Targeted frame recovery planner logic: START cascades to dependent END; independent frames preserved.
    Note: Tests the planner function directly; does NOT verify full T07 runtime recovery loop.
    """
    # Given 4 frames across 2 scenes:
    # Scene 1: scene_1-start (FAIL), scene_1-end (PASS originally)
    # Scene 2: scene_2-start (PASS), scene_2-end (PASS)
    generated_keyframes = [
        {"frame_id": "scene_1-start", "frame_role": "start", "scene_id": "scene_1", "image_path": "/p/s1_start.png", "sha256": "sha_s1_start"},
        {"frame_id": "scene_1-end", "frame_role": "end", "scene_id": "scene_1", "image_path": "/p/s1_end.png", "sha256": "sha_s1_end"},
        {"frame_id": "scene_2-start", "frame_role": "start", "scene_id": "scene_2", "image_path": "/p/s2_start.png", "sha256": "sha_s2_start"},
        {"frame_id": "scene_2-end", "frame_role": "end", "scene_id": "scene_2", "image_path": "/p/s2_end.png", "sha256": "sha_s2_end"},
    ]

    qc_findings = {
        "items": [
            {"frame_id": "scene_1-start", "verdict": "fail", "reason": "Subject identity shifted", "recommended_action": "Resample angle"},
            {"frame_id": "scene_1-end", "verdict": "pass", "reason": "Acceptable"},
            {"frame_id": "scene_2-start", "verdict": "pass", "reason": "Clear match"},
            {"frame_id": "scene_2-end", "verdict": "pass", "reason": "Clear match"},
        ]
    }

    plan = plan_targeted_keyframe_recovery(
        qc_report={"findings": qc_findings},
        generated_keyframes=generated_keyframes,
    )

    # 1. scene_1-start failed directly
    assert "scene_1-start" in plan["failing_frame_ids"]
    # 2. scene_1-end was cascaded because its conditioning START failed
    assert "scene_1-end" in plan["failing_frame_ids"]
    assert "scene_1-end" in plan["cascade_regenerations"]
    assert "START frame was regenerated" in plan["feedback_by_frame"]["scene_1-end"]

    # 3. Scene 2 frames were completely independent and PASS; must remain preserved
    assert "scene_2-start" in plan["passed_frame_ids"]
    assert "scene_2-end" in plan["passed_frame_ids"]
    assert "scene_2-start" not in plan["failing_frame_ids"]
    assert "scene_2-end" not in plan["failing_frame_ids"]


def test_keyframe_operation_id_and_replay_protection(tmp_path: Path) -> None:
    """Each keyframe recovery attempt claims a distinct operation_id; replay of previous attempt does not re-claim.
    Note: Unit test for claim_qc_attempt idempotency; does NOT verify full T08 unknown paid outcome handler.
    """
    conn = connect(tmp_path / "t08.db")
    wf_id = "wf_t08"
    frame_id = "scene_1-start"

    # Attempt 1: First recovery
    op_id_1 = f"op_kf_{wf_id}_{frame_id}_att1"
    claim_1 = claim_qc_attempt(
        conn,
        workflow_id=wf_id,
        retry_type="keyframe_frame_retry",
        item_id=frame_id,
        operation_id=op_id_1,
        max_recoveries=3,
    )
    assert claim_1["attempt_number"] == 1
    assert claim_1["status"] == "claimed"
    assert claim_1["reused"] is False

    # Simulate provider completion for attempt 1
    complete_qc_attempt(
        conn,
        attempt_id=claim_1["attempt_id"],
        status="completed",
        verdict="fail",
        output_hash="hash_kf_1",
        action_taken="regenerate_keyframe",
        reason_text="Lighting was too dark",
    )

    # Replay of Attempt 1: same operation_id must return existing claim without re-running
    replay_claim_1 = claim_qc_attempt(
        conn,
        workflow_id=wf_id,
        retry_type="keyframe_frame_retry",
        item_id=frame_id,
        operation_id=op_id_1,
        max_recoveries=3,
    )
    assert replay_claim_1["reused"] is True
    assert replay_claim_1["status"] == "completed"
    assert replay_claim_1["attempt_number"] == 1

    # Attempt 2: Next recovery attempt MUST have a different operation_id
    op_id_2 = f"op_kf_{wf_id}_{frame_id}_att2"
    claim_2 = claim_qc_attempt(
        conn,
        workflow_id=wf_id,
        retry_type="keyframe_frame_retry",
        item_id=frame_id,
        operation_id=op_id_2,
        max_recoveries=3,
    )
    assert claim_2["attempt_number"] == 2
    assert claim_2["status"] == "claimed"
    assert claim_2["reused"] is False

    complete_qc_attempt(
        conn,
        attempt_id=claim_2["attempt_id"],
        status="completed",
        verdict="pass",
        output_hash="hash_kf_2",
        action_taken="regenerate_keyframe",
        reason_text="Lighting corrected",
    )

    # Attempt count is now 2
    assert get_item_retry_count(conn, wf_id, "keyframe_frame_retry", frame_id) == 2
    # With max_recoveries=3, remaining recoveries is 1 + 3 - 2 = 2
    assert get_item_remaining_recoveries(conn, workflow_id=wf_id, retry_type="keyframe_frame_retry", item_id=frame_id, max_recoveries=3) == 2


def test_enqueue_keyframe_recovery_operator_handler(tmp_path: Path) -> None:
    """Verify operator handler enqueue_keyframe_recovery sets up aggregate and outbox envelope."""
    conn = connect(tmp_path / "enqueue.db")
    repo = WorkflowRepository(conn)
    wf_id = "wf_enqueue"

    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf_id,
        product_name="Recovery Product",
        keywords=["rec"],
        project_id="proj_enq",
        owner_user_id="user_enq",
        max_qc_retries=3,
    )

    wf_tuple = repo.get_workflow(wf_id)
    assert wf_tuple is not None
    agg, ver = wf_tuple
    agg["state"] = ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
    agg["operator_stage"] = QCStage.KEYFRAME.value
    agg["scene_plan"] = {"scene_plan_id": "sp_enq"}
    repo.commit_workflow(wf_id, json.dumps(agg), [], expected_version=ver)

    recovery_plan = {
        "failing_frame_ids": ["scene_1-start", "scene_1-end"],
        "feedback_by_frame": {"scene_1-start": "Improve contrast", "scene_1-end": "Cascade from start"},
    }
    auth = {"workflow_id": wf_id, "max_cost_usd": 5.0}

    res = enqueue_keyframe_recovery(
        conn,
        workflow_id=wf_id,
        plan=recovery_plan,
        authorization=auth,
        actor="operator_alice",
        expected_version=ver + 1,
    )

    assert res["status"] == "enqueued"
    assert res["recovery_attempt"] == 1

    updated_wf = repo.get_workflow(wf_id)
    assert updated_wf is not None
    updated_agg, updated_ver = updated_wf
    assert updated_agg["state"] == ProductVideoWorkflowState.CREATING_STORYBOARD.value
    assert updated_agg["keyframe_recovery_plan"] == recovery_plan
    assert updated_agg["keyframe_recovery_attempt"] == 1


def test_cli_authorizes_one_audited_frame_recovery_without_resetting_history(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "policy-recovery.db"
    conn = connect(db_path)
    repo = WorkflowRepository(conn)
    wf_id = "wf_policy_recovery"
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf_id,
        product_name="Recovery Product",
        keywords=["rec"],
        project_id="proj_policy_recovery",
        owner_user_id="local-owner",
        max_qc_retries=3,
    )
    package = {
        "schema_version": "storyboard-package-v2",
        "storyboard_id": "sb_policy_recovery",
        "version": 1,
        "generated_keyframes": [
            {"frame_id": "scene_02-start", "scene_id": "scene_02", "frame_role": "start", "image_path": str(tmp_path / "start.png"), "sha256": "old-start"},
            {"frame_id": "scene_02-end", "scene_id": "scene_02", "frame_role": "end", "image_path": str(tmp_path / "end.png"), "sha256": "old-end"},
        ],
    }
    package["package_digest"] = "sha256:old-package"
    failing_findings = {"items": [
        {"frame_id": "scene_02-start", "verdict": "fail", "score": 40, "reason": "black bars"},
        {"frame_id": "scene_02-end", "verdict": "fail", "score": 64, "reason": "scene continuity mismatch"},
    ]}
    qc_repo = QCReportRepository(conn)
    for index in range(1, 5):
        qc_repo.save_report(QCReport(
            report_id=f"old-report-{index}", workflow_id=wf_id, stage=QCStage.KEYFRAME,
            artifact_id="sb_policy_recovery", artifact_version=1,
            artifact_digest="sha256:old-package" if index == 4 else f"old-{index}",
            reference_digests=[], evaluator_name="nine_router_vision_qc", evaluator_version="1",
            rubric_version="2.0", created_at=f"2026-10-22T08:0{index}:00+00:00", score=70,
            verdict=QCVerdict.FAIL, findings=failing_findings if index == 4 else {"items": []},
            policy_version="product-video-keyframe-policy-2", owner_user_id="local-owner",
            project_id="proj_policy_recovery",
        ))
    for index in range(1, 5):
        claim = claim_qc_attempt(
            conn, workflow_id=wf_id, retry_type="keyframe_qc", item_id="storyboard",
            operation_id=f"old-op-{index}", max_recoveries=3,
        )
        complete_qc_attempt(
            conn, attempt_id=claim["attempt_id"], status="completed", verdict="fail",
            qc_report_id=f"old-report-{index}",
        )
    loaded = repo.get_workflow(wf_id)
    assert loaded is not None
    aggregate, version = loaded
    aggregate.update({
        "state": "skipped", "failure_code": "KEYFRAME_QC_RETRIES_EXHAUSTED",
        "failure_report": {"report_id": "old-report-4", "verdict": "fail"},
        "storyboard": package, "latest_artifact": package,
        "scene_plan": {"scene_plan_id": "approved-scene-plan"},
        "keyframe_recovery_attempt": 7,
    })
    repo.commit_workflow(wf_id, json.dumps(aggregate), [], expected_version=version)
    with conn:
        conn.execute("UPDATE workflow_outbox SET state='quarantined', last_error='Previous keyframe QC job completed' WHERE workflow_id=?", (wf_id,))
    conn.close()

    result = cli_main([
        "recover-skipped-keyframe-qc", wf_id, "--frame-id", "scene_02-start",
        "--reason", "Replace the square Flow output; VF no longer pads it.",
        "--db-path", str(db_path),
    ])
    assert result == 0
    cli_output = capsys.readouterr().out
    assert '"recovery_number": 1' in cli_output

    check = connect(db_path)
    updated, _ = WorkflowRepository(check).get_workflow(wf_id)
    assert updated["state"] == ProductVideoWorkflowState.CREATING_STORYBOARD.value
    assert updated["keyframe_policy_recovery_count"] == 1
    assert updated["max_qc_retries"] == 3
    assert updated["keyframe_recovery_attempt"] == 8
    assert updated["keyframe_recovery_plan"]["operator_stepwise"] is True
    assert [item["frame_id"] for item in updated["keyframe_recovery_plan"]["frames"]] == ["scene_02-start"]
    assert check.execute("SELECT COUNT(*) FROM product_video_qc_reports WHERE workflow_id=?", (wf_id,)).fetchone()[0] == 4
    assert check.execute("SELECT COUNT(*) FROM workflow_qc_retries WHERE workflow_id=?", (wf_id,)).fetchone()[0] == 4
    event = check.execute("SELECT payload_json FROM workflow_event WHERE workflow_id=? AND event_type='OperatorKeyframePolicyRecoveryAuthorized'", (wf_id,)).fetchone()
    assert json.loads(event[0])["previous_qc_history_preserved"] is True
    outbox = check.execute("SELECT message FROM workflow_outbox WHERE workflow_id=? AND state='pending'", (wf_id,)).fetchone()
    payload = json.loads(outbox[0])["payload"]
    assert payload["kwargs"]["recovery_plan"]["operator_stepwise"] is True
    check.close()


def test_storyboard_data_planning_helpers() -> None:
    from video_factory.creative.storyboard import (
        plan_initial_storyboard_recovery,
        cascade_keyframe_qc_failures,
        build_keyframe_recovery_request,
    )
    from video_factory.integrations.ports import KeyframeGenerationRequest

    # 1. plan_initial_storyboard_recovery cascades START to END
    recovery_plan = {
        "frames": [
            {"frame_id": "scene_1-start", "feedback": "Fix lighting"},
            {"frame_id": "scene_2-end", "feedback": "Adjust angle"},
        ]
    }
    rec_ids, feedback = plan_initial_storyboard_recovery(recovery_plan)
    assert "scene_1-start" in rec_ids
    assert "scene_1-end" in rec_ids
    assert "scene_2-end" in rec_ids
    assert "START frame was regenerated" in feedback["scene_1-end"]
    stepwise_ids, stepwise_feedback = plan_initial_storyboard_recovery({
        "operator_stepwise": True,
        "frames": [{"frame_id": "scene_1-start", "feedback": "Fix native portrait aspect"}],
    })
    assert stepwise_ids == {"scene_1-start"}
    assert "scene_1-end" not in stepwise_feedback

    # 2. cascade_keyframe_qc_failures
    cascaded_ids, cascaded_fb = cascade_keyframe_qc_failures(["scene_3-start"], {"scene_3-start": "blur"})
    assert "scene_3-start" in cascaded_ids
    assert "scene_3-end" in cascaded_ids
    assert "START frame was regenerated" in cascaded_fb["scene_3-end"]

    # 3. build_keyframe_recovery_request
    orig_req = KeyframeGenerationRequest(
        operation_id="orig_op",
        workflow_id="wf_1",
        project_id="p_1",
        frame_id="scene_1-start",
        prompt="A photo of shoes",
        product_description="Shoes",
        reference_image_paths=("/path/1.png",),
    )
    rev_req = build_keyframe_recovery_request(
        orig_req,
        msg="Increase contrast",
        start_cond_p="/path/start.png",
        attempt_num=2,
    )
    assert rev_req.operation_id.startswith("keyframe-")
    assert "Increase contrast" in rev_req.prompt
    assert rev_req.reference_image_paths[0] == "/path/start.png"


def test_storyboard_defers_keyframe_qc_to_governed_dispatcher(tmp_path: Path) -> None:
    """Storyboard generation must not run duplicate inline QC or quality recovery."""
    from types import SimpleNamespace
    from unittest.mock import patch
    from video_factory.handlers import ProductVideoJobHandlers
    from video_factory.job_contracts import JobContext, JobSucceeded
    from video_factory.workflow import ProductVideoWorkflowState

    mock_engine = MagicMock()
    mock_engine.repo.get_workflow.return_value = None
    mock_engine.get_workflow_state.return_value = (
        ProductVideoWorkflowState.CREATING_STORYBOARD.value,
        1,
    )

    heartbeat_calls = []
    def mock_heartbeat():
        heartbeat_calls.append(True)
        # Return True for initial keyframe generation check, then False during recovery
        if len(heartbeat_calls) == 1:
            return True
        return False

    ctx = JobContext(
        workflow_id="wf_hb_fail",
        job_id="j_hb_fail",
        heartbeat_fn=mock_heartbeat,
    )

    dummy_img = tmp_path / "img.png"
    dummy_img.write_bytes(b"dummy_image_data")

    handlers = ProductVideoJobHandlers(workflow_engine=mock_engine)
    handlers.provider_mode = "configured"

    mock_dossier = MagicMock()
    mock_dossier.eligible_assets = []
    mock_script = MagicMock()
    mock_script.scenes = []

    handlers.creative_generator = MagicMock(dossier=mock_dossier, script=mock_script)
    handlers.video_client = MagicMock()
    handlers.video_client.create_storyboard.return_value = {"status": "created"}

    mock_runtime = MagicMock()
    mock_runtime.run.return_value = MagicMock(
        status="completed",
        image_path=str(dummy_img),
        error_code=None,
    )
    handlers.keyframe_runtime_factory = MagicMock(return_value=mock_runtime)

    mock_failing_report = MagicMock()
    mock_failing_report.verdict = QCVerdict.FAIL
    mock_failing_report.report_id = "rep_fail_1"

    handlers.qc_service = MagicMock()
    handlers.qc_service.run.return_value = {
        "status": "completed",
        "report": mock_failing_report,
    }

    with patch("video_factory.handlers._base.reserve_budget", return_value=SimpleNamespace(reservation_id="mock_alloc")):
        with patch("video_factory.handlers._base.commit_budget"):
            with patch("video_factory.handlers._base.release_budget"):
                with patch("video_factory.qc.keyframe_qc.run_keyframe_qc", return_value=mock_failing_report) as inline_qc:
                    with patch("video_factory.qc.retry_policy.get_qc_retry_count", return_value=0):
                        with patch("video_factory.qc.retry_policy.plan_targeted_keyframe_recovery") as mock_plan:
                            mock_plan.return_value = {
                                "failing_frame_ids": ["scene_1-start"],
                                "feedback_by_frame": {"scene_1-start": "Fix lighting"},
                            }
                            with patch("video_factory.handlers._base._confirm_vf_storyboard_write"):
                                with patch("video_factory.creative.storyboard.compile_keyframe_requests") as mock_compile:
                                    mock_req = MagicMock(
                                        frame_id="scene_1-start",
                                        frame_role="start",
                                        prompt="prompt",
                                        negative_prompt="neg",
                                        reference_image_paths=(),
                                    )
                                    mock_compile.return_value = [mock_req]
                                    with patch("video_factory.qc.retry_policy.ensure_qc_retry_schema"):
                                        with patch("video_factory.qc.retry_policy.record_qc_retry"):
                                            res = handlers.storyboard_job(
                                                ctx,
                                                scene_ref={"scenes": []},
                                                max_qc_retries=3,
                                            )

    assert isinstance(res, JobSucceeded)
    assert len(heartbeat_calls) == 1
    assert res.artifact_ref["generated_keyframes"]
    inline_qc.assert_not_called()
    handlers.qc_service.run.assert_not_called()

