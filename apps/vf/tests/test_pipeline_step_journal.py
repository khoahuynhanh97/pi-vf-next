from __future__ import annotations

from types import SimpleNamespace

from video_factory.handlers.registry import _run_qc_with_step_log
from video_factory.qc.models import QCStage, QCVerdict
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.repository import WorkflowRepository
from video_factory.workflows.steps import record_pipeline_step, read_pipeline_steps
from video_factory.cli import main


def test_pipeline_step_events_survive_sqlite_restart_and_are_visible_in_status(tmp_path, capsys):
    db = tmp_path / "steps.db"
    conn = connect(db)
    WorkflowRepository(conn).commit_workflow(
        "wf-steps", '{"state":"running","product_key":"sku-1"}', [], expected_version=None
    )

    assert read_pipeline_steps(conn, "wf-steps") == []
    record_pipeline_step(
        conn,
        workflow_id="wf-steps",
        product_key="sku-1",
        step_name="IMAGE_QC",
        status="uncertain",
        attempt_number=1,
        operation_id="op-image-1",
        input_digest="sha256:abc",
        provider="9router",
        requested_model="gemini/flash",
        actual_model=None,
        cost_status="unknown",
        cost_usd=None,
        error_code="QC_OUTCOME_UNCERTAIN",
        message="Request may have been sent; reconcile before retry.",
        next_action="qc-reconcile --workflow-id wf-steps --attempt-id op-image-1",
    )
    conn.close()

    restarted = connect(db)
    rows = read_pipeline_steps(restarted, "wf-steps")
    assert len(rows) == 1
    assert rows[0]["step_name"] == "IMAGE_QC"
    assert rows[0]["status"] == "uncertain"
    assert rows[0]["cost_status"] == "unknown"
    assert rows[0]["cost_usd"] is None
    assert rows[0]["operation_id"] == "op-image-1"
    assert main(["--db-path", str(db), "status", "wf-steps"]) == 0
    output = capsys.readouterr().out
    assert "IMAGE_QC" in output
    assert "uncertain" in output
    assert "QC_OUTCOME_UNCERTAIN" in output
    assert "reconcile before retry" in output
    assert "QC cost:" in output
    assert "unknown attempts 1" in output
    restarted.close()


def test_pipeline_step_journal_accepts_proven_not_sent_cost_status(tmp_path):
    conn = connect(tmp_path / "not-sent-step.db")
    WorkflowRepository(conn).commit_workflow(
        "wf-not-sent", '{"state":"running","product_key":"sku-1"}', [], expected_version=None
    )
    record_pipeline_step(
        conn,
        workflow_id="wf-not-sent",
        product_key="sku-1",
        step_name="KEYFRAME_QC",
        status="pending_operator",
        cost_status="not_sent",
    )
    assert read_pipeline_steps(conn, "wf-not-sent")[0]["cost_status"] == "not_sent"
    conn.close()


def test_qc_step_journal_records_pass_cost_and_uncertain_outcome(tmp_path):
    conn = connect(tmp_path / "qc-steps.db")
    WorkflowRepository(conn).commit_workflow(
        "wf-qc-steps", '{"state":"running","product_key":"sku-2"}', [], expected_version=None
    )

    class QCStub:
        connection = conn
        evaluator = SimpleNamespace(model="test/vision")

        def __init__(self, result=None, error=None):
            self.result = result
            self.error = error

        def run(self, *_args, **_kwargs):
            if self.error:
                raise self.error
            return self.result

    report = SimpleNamespace(
        verdict=QCVerdict.PASS,
        findings={"cost_status": "recorded", "actual_model": "gemini/test"},
        cost_usd=0.012,
    )
    result = {"status": "completed", "report": report}
    _run_qc_with_step_log(
        QCStub(result=result), "wf-qc-steps", QCStage.KEYFRAME,
        provider_mode="configured", aggregate={"product_key": "sku-2"}, artifact={"sha256": "abc"},
    )
    passed = read_pipeline_steps(conn, "wf-qc-steps")[-1]
    assert passed["step_name"] == "KEYFRAME_QC"
    assert passed["status"] == "completed"
    assert passed["requested_model"] == "test/vision"
    assert passed["actual_model"] == "gemini/test"
    assert passed["cost_status"] == "recorded"
    assert passed["cost_usd"] == 0.012

    class UnknownSubmission(RuntimeError):
        request_sent = True

    try:
        _run_qc_with_step_log(
            QCStub(error=UnknownSubmission("no response")), "wf-qc-steps", QCStage.VIDEO,
            provider_mode="configured", aggregate={"product_key": "sku-2"}, artifact={"sha256": "def"},
        )
    except UnknownSubmission:
        pass
    else:
        raise AssertionError("uncertain provider submission should propagate")
    unknown = read_pipeline_steps(conn, "wf-qc-steps")[-1]
    assert unknown["step_name"] == "VIDEO_QC"
    assert unknown["status"] == "uncertain"
    assert unknown["cost_status"] == "unknown"
    record_pipeline_step(
        conn,
        workflow_id="wf-qc-steps",
        product_key="sku-2",
        step_name="VIDEO_QC",
        status="failed",
        message="Authorization: Bearer abc123 api_key=secret prompt: private prompt text",
    )
    safe_text = read_pipeline_steps(conn, "wf-qc-steps")[-1]["message"]
    assert "abc123" not in safe_text
    assert "secret" not in safe_text
    assert "private prompt text" not in safe_text
    conn.close()


def test_hoco_unknown_attempt_fixture_stays_operator_blocked_and_counts_unknown_cost(tmp_path, capsys):
    from video_factory.qc.retry_policy import claim_qc_attempt, complete_qc_attempt, ensure_qc_retry_schema

    db = tmp_path / "hoco-unknown.db"
    conn = connect(db)
    WorkflowRepository(conn).commit_workflow(
        "pv-hoco-fixture",
        '{"state":"WaitingForOperator","product_key":"hoco:j101a",'
        '"failure_code":"QC_OUTCOME_UNCERTAIN","operator_action":"qc_retry_authorization",'
        '"operator_reason":"Reconcile exact Image QC attempt before retry."}',
        [],
        expected_version=None,
    )
    ensure_qc_retry_schema(conn)
    for operation_id, status in (
        ("op-hoco-first", "reconciled_consumed"),
        ("op-hoco-second", "technical_unknown"),
    ):
        claimed = claim_qc_attempt(
            conn,
            workflow_id="pv-hoco-fixture",
            retry_type="source_image_qc",
            item_id="hoco:j101a",
            operation_id=operation_id,
        )
        complete_qc_attempt(
            conn,
            attempt_id=claimed["attempt_id"],
            status=status,
            verdict="unknown",
            reason_text="No QC report was recovered.",
        )
    conn.close()

    assert main(["--db-path", str(db), "status", "pv-hoco-fixture"]) == 0
    output = capsys.readouterr().out
    assert "WaitingForOperator" in output
    assert "QC_OUTCOME_UNCERTAIN" in output
    assert "unknown attempts 2" in output
