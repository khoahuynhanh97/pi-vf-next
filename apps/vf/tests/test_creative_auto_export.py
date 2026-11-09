from dataclasses import replace
import pytest
from video_factory.automation.creative_policy import evaluate_auto_gate
from video_factory.creative.agents.contracts import CreativeReview


def test_technical_pass_content_fail_unknown_or_stale_cannot_export():
    review = CreativeReview('final','PASS','current',(),{'observed':'pixels'},{'model':'fake'})
    artifact = {'sha256':'final-sha','technical_qc':{'sha256':'final-sha','verdict':'PASS'},
                'review_input_digests':{'final':'current'}}
    policy = {'creative_mode':'four_agent','auto_export':True}
    def decision(r):
        return evaluate_auto_gate(state='AwaitingFinalApproval',artifact=artifact,reviews=(r,),policy=policy)
    assert decision(review)['action'] == 'advance'
    assert decision(review)['human_review'] == 'not_performed'
    for bad in (replace(review,verdict='FAIL'),replace(review,verdict='UNKNOWN'),replace(review,input_digest='stale')):
        assert decision(bad)['action'] != 'advance'
    artifact['technical_qc']['sha256'] = 'old-final'
    assert decision(review)['action'] == 'stop'


def test_default_final_gate_advances_on_current_technical_pass_without_content_critic(monkeypatch):
    from video_factory.creative.agents.critic import CreativeCritic

    monkeypatch.setattr(
        CreativeCritic,
        "review",
        lambda *args, **kwargs: pytest.fail("default final gate must not call the content critic"),
    )
    artifact = {
        "sha256": "final-sha",
        "technical_qc": {"sha256": "final-sha", "verdict": "PASS"},
        "content_assessment": "not_performed",
    }
    result = evaluate_auto_gate(
        state="AwaitingFinalApproval",
        artifact=artifact,
        reviews=(),
        policy={"creative_mode": "four_agent", "auto_export": True},
    )

    assert result["action"] == "advance"


def _high_final_evidence():
    review = CreativeReview('final', 'PASS', 'current', (), {'observed': 'pixels'}, {'model': 'fake'})
    artifact = {'sha256': 'final-sha',
        'technical_qc': {'sha256': 'final-sha', 'verdict': 'PASS'},
        'review_context': {'video_sha256': 'final-sha'},
        'review_input_digests': {'final': 'current'}, 'final_review': review.to_dict()}
    return artifact, review


@pytest.mark.parametrize('missing', ['review_input_digests', 'final_review', 'review_context'])
def test_high_final_gate_rejects_missing_content_evidence(missing):
    artifact, review = _high_final_evidence()
    artifact.pop(missing)
    assert evaluate_auto_gate(state='AwaitingFinalApproval', artifact=artifact, reviews=(review,),
        policy={'creative_mode': 'four_agent', 'auto_export': True, 'qc_level': 'high'})['action'] == 'stop'


@pytest.mark.parametrize('change', ['video_sha', 'review_digest', 'receipt_digest', 'technical_sha', 'technical_verdict'])
def test_high_final_gate_rejects_stale_or_failed_evidence(change):
    artifact, review = _high_final_evidence()
    if change == 'video_sha':
        artifact['review_context']['video_sha256'] = 'old-final'
    elif change == 'review_digest':
        artifact['review_input_digests']['final'] = 'old-review'
    elif change == 'receipt_digest':
        artifact['final_review']['input_digest'] = 'old-review'
    elif change == 'technical_sha':
        artifact['technical_qc']['sha256'] = 'old-final'
    else:
        artifact['technical_qc']['verdict'] = 'UNKNOWN'
    assert evaluate_auto_gate(state='AwaitingFinalApproval', artifact=artifact, reviews=(review,),
        policy={'creative_mode': 'four_agent', 'auto_export': True, 'qc_level': 'high'})['action'] == 'stop'


@pytest.mark.parametrize('verdict, action', [('PASS', 'advance'), ('FAIL', 'repair'), ('UNKNOWN', 'stop')])
def test_high_final_gate_preserves_content_verdict_recovery(verdict, action):
    artifact, review = _high_final_evidence()
    review = replace(review, verdict=verdict)
    artifact['final_review'] = review.to_dict()
    assert evaluate_auto_gate(state='AwaitingFinalApproval', artifact=artifact, reviews=(review,),
        policy={'creative_mode': 'four_agent', 'auto_export': True, 'qc_level': 'high'})['action'] == action


def test_high_final_gate_requires_matching_review_object():
    artifact, _ = _high_final_evidence()
    assert evaluate_auto_gate(state='AwaitingFinalApproval', artifact=artifact, reviews=(),
        policy={'creative_mode': 'four_agent', 'auto_export': True, 'qc_level': 'high'})['action'] == 'stop'


@pytest.mark.parametrize('field', ['review_input_digests', 'final_review', 'review_context', 'technical_qc'])
def test_high_final_gate_stops_when_evidence_is_explicitly_null(field):
    artifact, review = _high_final_evidence()
    artifact[field] = None
    assert evaluate_auto_gate(state='AwaitingFinalApproval', artifact=artifact, reviews=(review,),
        policy={'creative_mode': 'four_agent', 'auto_export': True, 'qc_level': 'high'})['action'] == 'stop'


def test_high_final_gate_rejects_disagreeing_receipt_and_review_verdict():
    artifact, review = _high_final_evidence()
    artifact['final_review']['verdict'] = 'UNKNOWN'
    assert evaluate_auto_gate(state='AwaitingFinalApproval', artifact=artifact, reviews=(review,),
        policy={'creative_mode': 'four_agent', 'auto_export': True, 'qc_level': 'high'})['action'] == 'stop'


@pytest.mark.parametrize('level', [None, 'low', 'middle'])
def test_existing_policy_levels_retain_technical_only_final_gate(level):
    from video_factory.automation.creative_policy import normalize_qc_level
    artifact, _ = _high_final_evidence()
    for field in ('review_context', 'review_input_digests', 'final_review'):
        artifact.pop(field)
    policy = {'creative_mode': 'four_agent', 'auto_export': True}
    if level:
        policy['qc_level'] = level
    assert normalize_qc_level(None) == 'middle'
    assert evaluate_auto_gate(state='AwaitingFinalApproval', artifact=artifact, reviews=(), policy=policy)['action'] == 'advance'


def test_brief_and_scene_contracts_do_not_require_duplicate_content_reviews():
    policy = {'creative_mode':'four_agent', 'auto_export':True}
    for state in ('AwaitingBriefApproval', 'AwaitingSceneApproval'):
        assert evaluate_auto_gate(state=state, artifact={}, reviews=(), policy=policy)['action'] == 'advance'


def test_storyboard_gate_uses_current_keyframe_qc_receipt_without_duplicate_critic():
    artifact = {'package_digest':'board-digest', 'keyframe_qc_receipt':{
        'stage':'keyframe', 'verdict':'pass', 'artifact_digest':'board-digest', 'score':12,
        'findings':{'items':[{'verdict':'pass','hard_failure':False}]}}}
    result = evaluate_auto_gate(state='AwaitingStoryboardApproval', artifact=artifact,
                                reviews=(), policy={'creative_mode':'four_agent','auto_export':True})
    assert result['action'] == 'advance'


def test_storyboard_gate_rejects_stale_keyframe_qc_receipt():
    artifact = {'package_digest':'new-board', 'keyframe_qc_receipt':{
        'stage':'keyframe', 'verdict':'pass', 'artifact_digest':'old-board', 'score':99,
        'findings':{'items':[{'verdict':'pass','hard_failure':False}]}}}
    assert evaluate_auto_gate(state='AwaitingStoryboardApproval', artifact=artifact, reviews=(),
        policy={'creative_mode':'four_agent','auto_export':True})['action'] == 'stop'


def test_render_preflight_reuses_current_keyframe_qc_without_duplicate_storyboard_review():
    from video_factory.creative.agents.gates import require_storyboard_qc_pass

    package = {'creative_mode':'four_agent', 'package_digest':'board-digest',
        'keyframe_qc_receipt':{'stage':'keyframe', 'verdict':'pass',
            'artifact_digest':'board-digest', 'findings':{'items':[
                {'verdict':'pass', 'hard_failure':False}]}}}
    require_storyboard_qc_pass(package)


@pytest.mark.parametrize('receipt', [
    {'stage':'keyframe', 'verdict':'pass', 'artifact_digest':'old-board',
        'findings':{'items':[{'verdict':'pass', 'hard_failure':False}]}},
    {'stage':'keyframe', 'verdict':'fail', 'artifact_digest':'board-digest',
        'findings':{'items':[{'verdict':'fail', 'hard_failure':True}]}},
    {'stage':'keyframe', 'verdict':'pass', 'artifact_digest':'board-digest',
        'findings':{'items':[{'verdict':'pass', 'hard_failure':True}]}},
])
def test_render_preflight_rejects_stale_or_failed_keyframe_qc(receipt):
    from video_factory.creative.agents.gates import require_storyboard_qc_pass

    with pytest.raises(ValueError, match='current keyframe QC PASS'):
        require_storyboard_qc_pass({'creative_mode':'four_agent',
            'package_digest':'board-digest', 'keyframe_qc_receipt':receipt})


def test_coordinator_auto_final_gate_reaches_export_with_automation_receipt(tmp_path,monkeypatch):
    import json
    from test_creative_resume import config
    from video_factory.storage_infra.sqlite import connect
    from video_factory.automation.coordinator import ProductVideoCoordinator
    from types import SimpleNamespace
    conn=connect(tmp_path/'worker.db')
    coordinator=ProductVideoCoordinator(conn,db_path=tmp_path/'worker.db')
    started=coordinator.start_fresh_creative(run_id='auto-final',query='Synthetic fixture',run_config=config())
    workflow_id=started['workflow_id']
    from video_factory.creative.agents.critic import CreativeCritic
    monkeypatch.setattr(CreativeCritic, 'review', lambda *a,**k:pytest.fail('final critic must not run'))
    artifact={'creative_mode':'four_agent','sha256':'sha','content_assessment':'not_performed',
        'technical_qc':{'sha256':'sha','verdict':'PASS'}}
    aggregate,version=coordinator.workflow_repo.get_workflow(workflow_id)
    aggregate.update(state='AwaitingFinalApproval',latest_artifact=artifact,render_result=artifact)
    coordinator.workflow_repo.commit_workflow(workflow_id,json.dumps(aggregate),[],expected_version=version)
    calls=[]
    def fake_approve(**kwargs):
        calls.append(('approval',kwargs['payload']['automation_actor']))
        agg,version=coordinator.workflow_repo.get_workflow(workflow_id)
        agg['state']='exporting'
        coordinator.workflow_repo.commit_workflow(workflow_id,json.dumps(agg),[],expected_version=version)
    def fake_worker(**kwargs):
        assert kwargs['workflow_ids']==[workflow_id]
        agg,version=coordinator.workflow_repo.get_workflow(workflow_id)
        assert agg['state']=='exporting'
        calls.append(('worker','export'))
        agg['state']='completed'
        coordinator.workflow_repo.commit_workflow(workflow_id,json.dumps(agg),[],expected_version=version)
        return SimpleNamespace(failed=0)
    monkeypatch.setattr('video_factory.automation.coordinator.approve_and_advance_workflow',fake_approve)
    monkeypatch.setattr('video_factory.automation.coordinator.run_worker',fake_worker)
    assert coordinator.advance_creative(workflow_id,provider_mode='fake')['state']=='completed'
    assert calls==[('approval','creative_auto_policy'),('worker','export')]
    assert coordinator.advance_creative(workflow_id,provider_mode='fake')['action']=='stop'
    assert len(calls)==2
    conn.close()


def test_coordinator_recovers_current_keyframe_pass_receipt_before_auto_gate(tmp_path, monkeypatch):
    import json
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from test_creative_resume import config
    from video_factory.automation.coordinator import ProductVideoCoordinator
    from video_factory.qc.models import QCReport, QCStage, QCVerdict
    from video_factory.qc.fingerprint import KEYFRAME_POLICY_VERSION, KEYFRAME_RUBRIC_VERSION
    from video_factory.qc.repository import QCReportRepository
    from video_factory.storage_infra.sqlite import connect

    db_path = tmp_path / "worker.db"
    conn = connect(db_path)
    coordinator = ProductVideoCoordinator(conn, db_path=db_path)
    started = coordinator.start_fresh_creative(
        run_id="auto-keyframe-receipt",
        query="Synthetic fixture",
        run_config=config(),
    )
    workflow_id = started["workflow_id"]
    artifact = {
        "storyboard_id": "sb-current",
        "version": 1,
        "package_digest": "sha256:current-package",
        "generated_keyframes": [{"frame_id": "frame-1", "sha256": "a" * 64}],
    }
    aggregate, version = coordinator.workflow_repo.get_workflow(workflow_id)
    aggregate.update(
        state="AwaitingStoryboardApproval",
        storyboard=artifact,
        latest_artifact=artifact,
        creative_policy={"creative_mode": "four_agent", "auto_export": True, "qc_level": "middle"},
        pending_approval_id="approval-current-storyboard",
        pending_approval_gate="AwaitingStoryboardApproval",
        failure_category="QC_INFRA_PAUSED",
        failure_code="QC_OUTCOME_UNCERTAIN",
        failure_reason="superseded by current pass report",
    )
    coordinator.workflow_repo.commit_workflow(
        workflow_id, json.dumps(aggregate), [], expected_version=version
    )
    QCReportRepository(conn).save_report(QCReport(
        report_id="qc-current-keyframe-pass",
        workflow_id=workflow_id,
        stage=QCStage.KEYFRAME,
        artifact_id=artifact["storyboard_id"],
        artifact_version=artifact["version"],
        artifact_digest=artifact["package_digest"],
        reference_digests=[],
        evaluator_name="configured_test_vision",
        evaluator_version="1",
        rubric_version=KEYFRAME_RUBRIC_VERSION,
        created_at=datetime.now(timezone.utc).isoformat(),
        score=86,
        verdict=QCVerdict.PASS,
        findings={"items": [{"frame_id": "frame-1", "verdict": "pass", "hard_failure": False}]},
        provider_mode="configured",
        owner_user_id=aggregate["owner_user_id"],
        project_id=aggregate["project_id"],
        policy_version=KEYFRAME_POLICY_VERSION,
    ))
    approvals = []

    def fake_approve(**kwargs):
        current, current_version = coordinator.workflow_repo.get_workflow(workflow_id)
        receipt = current["latest_artifact"].get("keyframe_qc_receipt")
        approvals.append((kwargs["payload"]["automation_actor"], receipt))
        current["state"] = "Rendering"
        coordinator.workflow_repo.commit_workflow(
            workflow_id, json.dumps(current), [], expected_version=current_version
        )

    def fake_worker(**kwargs):
        assert kwargs["workflow_ids"] == [workflow_id]
        current, current_version = coordinator.workflow_repo.get_workflow(workflow_id)
        current["state"] = "completed"
        coordinator.workflow_repo.commit_workflow(
            workflow_id, json.dumps(current), [], expected_version=current_version
        )
        return SimpleNamespace(failed=0)

    monkeypatch.setattr("video_factory.automation.coordinator.approve_and_advance_workflow", fake_approve)
    monkeypatch.setattr("video_factory.automation.coordinator.run_worker", fake_worker)

    result = coordinator.advance_creative(workflow_id, provider_mode="configured")

    assert result["state"] == "completed", result
    assert approvals[0][0] == "creative_auto_policy"
    assert approvals[0][1]["report_id"] == "qc-current-keyframe-pass"
    assert approvals[0][1]["artifact_digest"] == artifact["package_digest"]
    completed, _ = coordinator.workflow_repo.get_workflow(workflow_id)
    assert "failure_code" not in completed
    assert conn.execute(
        "SELECT COUNT(*) FROM workflow_event WHERE workflow_id=? AND event_type='KeyframeQCPassReceiptRecovered'",
        (workflow_id,),
    ).fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize("preflight_error", [
    "Four-agent render requires an unchanged storyboard package",
    "Continuous scenes require high clip and endpoint QC",
])
def test_coordinator_requeues_only_known_render_preflight_without_resetting_attempts(tmp_path, monkeypatch, preflight_error):
    import json
    from types import SimpleNamespace
    from test_creative_resume import config
    from video_factory.automation.coordinator import ProductVideoCoordinator
    from video_factory.storage_infra.sqlite import connect
    from video_factory.workflows.outbox_repository import OutboxRepository

    db_path = tmp_path / "worker.db"
    conn = connect(db_path)
    coordinator = ProductVideoCoordinator(conn, db_path=db_path)
    workflow_id = coordinator.start_fresh_creative(
        run_id="known-render-preflight-recovery",
        query="Synthetic fixture",
        run_config=config(),
    )["workflow_id"]
    aggregate, version = coordinator.workflow_repo.get_workflow(workflow_id)
    if preflight_error.startswith("Continuous scenes"):
        aggregate["creative_policy"]["qc_level"] = "low"
    aggregate.update(
        state="rendering",
        active_render_job_id=None,
        storyboard={"package_digest": "sha256:current"},
        latest_artifact={"package_digest": "sha256:current"},
    )
    coordinator.workflow_repo.commit_workflow(
        workflow_id, json.dumps(aggregate), [], expected_version=version
    )
    message = json.dumps({"payload": {"job_name": "product_video.render", "workflow_id": workflow_id}})
    outbox = OutboxRepository(conn).enqueue(workflow_id, message, max_attempts=5)
    conn.execute(
        "UPDATE workflow_outbox SET state='quarantined', attempt_count=1, last_error=? WHERE outbox_id=?",
        (f"Job execution failed: {preflight_error}", outbox.outbox_id),
    )
    conn.commit()

    def fake_worker(**kwargs):
        assert kwargs["workflow_ids"] == [workflow_id]
        row = conn.execute(
            "SELECT state, attempt_count FROM workflow_outbox WHERE outbox_id=?", (outbox.outbox_id,)
        ).fetchone()
        assert tuple(row) == ("pending", 1)
        return SimpleNamespace(failed=0)

    monkeypatch.setattr("video_factory.automation.coordinator.run_worker", fake_worker)

    result = coordinator.advance_creative(workflow_id, provider_mode="configured")

    assert result["action"] == "pending"
    event = conn.execute(
        "SELECT payload_json FROM workflow_event WHERE workflow_id=? AND event_type='RenderPreflightFailureRecovered'",
        (workflow_id,),
    ).fetchone()
    assert event is not None
    payload = json.loads(event["payload_json"])
    assert payload["outbox_id"] == outbox.outbox_id
    assert preflight_error in payload["previous_error"]
    assert payload["storyboard_digest"] == "sha256:current"
    conn.close()


@pytest.mark.parametrize("last_error", [
    "unrelated render failure",
    "Four-agent render requires an unchanged storyboard package",
])
def test_coordinator_does_not_requeue_render_preflight_without_exact_safe_conditions(tmp_path, monkeypatch, last_error):
    import json
    from types import SimpleNamespace
    from test_creative_resume import config
    from video_factory.automation.coordinator import ProductVideoCoordinator
    from video_factory.storage_infra.sqlite import connect
    from video_factory.workflows.outbox_repository import OutboxRepository

    db_path = tmp_path / "worker.db"
    conn = connect(db_path)
    coordinator = ProductVideoCoordinator(conn, db_path=db_path)
    workflow_id = coordinator.start_fresh_creative(
        run_id="unsafe-render-preflight-recovery",
        query="Synthetic fixture",
        run_config=config(),
    )["workflow_id"]
    aggregate, version = coordinator.workflow_repo.get_workflow(workflow_id)
    aggregate.update(state="rendering", active_render_job_id=None)
    coordinator.workflow_repo.commit_workflow(
        workflow_id, json.dumps(aggregate), [], expected_version=version
    )
    outbox = OutboxRepository(conn).enqueue(
        workflow_id,
        json.dumps({"payload": {"job_name": "product_video.render", "workflow_id": workflow_id}}),
    )
    conn.execute(
        "UPDATE workflow_outbox SET state='quarantined', attempt_count=1, last_error=? WHERE outbox_id=?",
        (last_error, outbox.outbox_id),
    )
    if "unchanged storyboard package" in last_error:
        conn.execute(
            "INSERT INTO video_factory_render_batches(render_batch_id,owner_user_id,workflow_id,project_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("existing-render", aggregate["owner_user_id"], workflow_id, aggregate["project_id"],
             "running", "2026-10-25T00:00:00+00:00", "2026-10-25T00:00:00+00:00"),
        )
    conn.commit()

    def fake_worker(**kwargs):
        assert conn.execute(
            "SELECT state FROM workflow_outbox WHERE outbox_id=?", (outbox.outbox_id,)
        ).fetchone()[0] == "quarantined"
        return SimpleNamespace(failed=0)

    monkeypatch.setattr("video_factory.automation.coordinator.run_worker", fake_worker)

    result = coordinator.advance_creative(workflow_id, provider_mode="configured")

    assert result["action"] == "quarantined"
    assert conn.execute(
        "SELECT COUNT(*) FROM workflow_event WHERE workflow_id=? AND event_type='RenderPreflightFailureRecovered'",
        (workflow_id,),
    ).fetchone()[0] == 0
    conn.close()


def test_coordinator_resumes_checkpointed_render_reconciliation_without_new_batch(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from test_creative_resume import config
    from video_factory.automation.coordinator import ProductVideoCoordinator
    from video_factory.storage_infra.sqlite import connect
    from video_factory.workflows.outbox_repository import OutboxRepository

    db_path = tmp_path / "worker.db"
    conn = connect(db_path)
    coordinator = ProductVideoCoordinator(conn, db_path=db_path)
    workflow_id = coordinator.start_fresh_creative(
        run_id="checkpointed-render-reconciliation",
        query="Synthetic fixture",
        run_config=config(),
    )["workflow_id"]
    aggregate, version = coordinator.workflow_repo.get_workflow(workflow_id)
    batch_id = f"batch_proj-{workflow_id}_v1_existing"
    aggregate.update(state="rendering", active_render_job_id=batch_id)
    coordinator.workflow_repo.commit_workflow(
        workflow_id, json.dumps(aggregate), [], expected_version=version
    )
    outbox = OutboxRepository(conn).enqueue(
        workflow_id,
        json.dumps({"payload": {"job_name": "product_video.render", "workflow_id": workflow_id}}),
        max_attempts=5,
    )
    conn.execute(
        "UPDATE workflow_outbox SET state='quarantined', attempt_count=2, last_error=? WHERE outbox_id=?",
        ("Job execution failed [RECONCILIATION_REQUIRED]: provider poll is uncertain", outbox.outbox_id),
    )
    conn.commit()

    worker_attempt = 0

    def fake_worker(**kwargs):
        nonlocal worker_attempt
        worker_attempt += 1
        assert kwargs["workflow_ids"] == [workflow_id]
        assert kwargs["batch_size"] == 1
        row = conn.execute(
            "SELECT state,attempt_count FROM workflow_outbox WHERE outbox_id=?", (outbox.outbox_id,)
        ).fetchone()
        assert tuple(row) == ("pending", 1 + worker_attempt)
        return SimpleNamespace(failed=0)

    monkeypatch.setattr("video_factory.automation.coordinator.run_worker", fake_worker)

    result = coordinator.advance_creative(workflow_id, provider_mode="configured")

    assert result["action"] == "pending"
    audit = conn.execute(
        "SELECT payload_json FROM workflow_event WHERE workflow_id=? AND event_type='RenderReconciliationResumed'",
        (workflow_id,),
    ).fetchone()
    assert audit is not None
    payload = json.loads(audit["payload_json"])
    assert payload["batch_id"] == batch_id
    assert "no new batch" in payload["reason"].lower()

    # A later quarantine of the same checkpoint must be auditable and resumable
    # again without colliding with the previous recovery command ID.
    conn.execute(
        "UPDATE workflow_outbox SET state='quarantined', attempt_count=3, last_error=? WHERE outbox_id=?",
        ("Job execution failed [RECONCILIATION_REQUIRED]: provider still uncertain", outbox.outbox_id),
    )
    conn.commit()
    result = coordinator.advance_creative(workflow_id, provider_mode="configured")

    assert result["action"] == "pending"
    audits = conn.execute(
        "SELECT event_id,payload_json FROM workflow_event "
        "WHERE workflow_id=? AND event_type='RenderReconciliationResumed' ORDER BY timestamp",
        (workflow_id,),
    ).fetchall()
    assert len(audits) == 2
    assert audits[0]["event_id"] != audits[1]["event_id"]
    assert conn.execute(
        "SELECT COUNT(DISTINCT command_id) FROM processed_command "
        "WHERE workflow_id=? AND command_id LIKE 'cmd_renderreconciliationresumed_%'",
        (workflow_id,),
    ).fetchone()[0] == 2
    conn.close()


def test_exhausted_outbox_can_resume_only_verified_completed_render_for_export(tmp_path, monkeypatch):
    import hashlib
    import json
    import sqlite3
    from types import SimpleNamespace
    from test_creative_resume import config
    from video_factory.automation.coordinator import ProductVideoCoordinator
    from video_factory.storage_infra.sqlite import connect
    from video_factory.workflows.outbox_repository import OutboxRepository

    workflow_db = tmp_path / "workflow.db"
    render_db = tmp_path / "render.db"
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(render_db))
    conn = connect(workflow_db)
    coordinator = ProductVideoCoordinator(conn, db_path=workflow_db)
    workflow_id = coordinator.start_fresh_creative(
        run_id="completed-render-finalize", query="Fixture", run_config=config(),
    )["workflow_id"]
    aggregate, version = coordinator.workflow_repo.get_workflow(workflow_id)
    batch_id = f"batch_proj-{workflow_id}_v1_existing"
    aggregate.update(
        state="rendering", project_id=f"proj-{workflow_id}", active_render_job_id=batch_id,
        latest_artifact={"creative_mode": "four_agent", "backend_profile": {"provider_kind": "google_flow"}},
    )
    coordinator.workflow_repo.commit_workflow(
        workflow_id, json.dumps(aggregate), [], expected_version=version,
    )
    outbox = OutboxRepository(conn).enqueue(
        workflow_id,
        json.dumps({"payload": {"job_name": "product_video.render", "workflow_id": workflow_id}}),
        max_attempts=2,
    )
    conn.execute(
        "UPDATE workflow_outbox SET state='quarantined',attempt_count=3,last_error='max_attempts_exceeded_on_claim' WHERE outbox_id=?",
        (outbox.outbox_id,),
    )
    conn.commit()

    output = tmp_path / "scene.mp4"
    output.write_bytes(b"verified scene bytes")
    sha = hashlib.sha256(output.read_bytes()).hexdigest()
    render = sqlite3.connect(render_db)
    from video_factory.integrations.scene_renderer import SceneRenderOperationStore
    SceneRenderOperationStore(render)
    render.execute(
        "CREATE TABLE video_factory_render_batches (render_batch_id TEXT,workflow_id TEXT,project_id TEXT,scene_count INTEGER)"
    )
    render.execute(
        "INSERT INTO video_factory_render_batches VALUES (?,?,?,?)",
        (batch_id, workflow_id, aggregate["project_id"], 2),
    )
    render.execute(
        """INSERT INTO video_factory_scene_operation
        (operation_id,owner_user_id,project_id,scene_id,request_digest,provider_operation_id,status,
         output_path,output_sha256,render_batch_id,scene_order,attempt,provider_kind)
        VALUES ('scene-op','local-owner',?,'scene_1','digest','https://flow.google.com/project/00000000-0000-0000-0000-000000000001',
                'completed',?,?,?,1,1,'google_flow')""",
        (aggregate["project_id"], str(output), sha, batch_id),
    )
    render.execute(
        """INSERT INTO video_factory_scene_operation
        (operation_id,owner_user_id,project_id,scene_id,request_digest,provider_operation_id,status,
         output_path,output_sha256,render_batch_id,scene_order,attempt,provider_kind)
        VALUES ('scene-op-2','local-owner',?,'scene_2','digest-2','https://flow.google.com/project/00000000-0000-0000-0000-000000000002',
                'submitted',NULL,'',?,2,1,'google_flow')""",
        (aggregate["project_id"], batch_id),
    )
    render.commit()
    render.close()

    def fake_worker(**kwargs):
        row = conn.execute(
            "SELECT state,attempt_count,max_attempts FROM workflow_outbox WHERE outbox_id=?",
            (outbox.outbox_id,),
        ).fetchone()
        assert tuple(row) == ("pending", 3, 4)
        return SimpleNamespace(failed=0)

    monkeypatch.setattr("video_factory.automation.coordinator.run_worker", fake_worker)
    result = coordinator.advance_creative(workflow_id, provider_mode="configured")
    assert result["action"] == "pending"
    audit = conn.execute(
        "SELECT payload_json FROM workflow_event WHERE workflow_id=? AND event_type='RenderFinalizationResumed'",
        (workflow_id,),
    ).fetchone()
    assert audit is not None
    evidence = json.loads(audit[0])
    assert evidence["scene_operations"][0]["sha256"] == sha
    assert "without submitting a scene" in evidence["reason"]

    conn.execute(
        "UPDATE workflow_outbox SET state='quarantined',attempt_count=4,max_attempts=4,last_error=? WHERE outbox_id=?",
        ("Job execution failed [RECONCILIATION_REQUIRED]: final poll still uncertain", outbox.outbox_id),
    )
    conn.commit()

    def refuse_second_finalization_delivery(**kwargs):
        state = conn.execute(
            "SELECT state FROM workflow_outbox WHERE outbox_id=?", (outbox.outbox_id,)
        ).fetchone()[0]
        assert state == "quarantined"
        return SimpleNamespace(failed=0)

    monkeypatch.setattr(
        "video_factory.automation.coordinator.run_worker", refuse_second_finalization_delivery,
    )
    second = coordinator.advance_creative(workflow_id, provider_mode="configured")
    assert second["action"] == "quarantined"
    conn.close()
