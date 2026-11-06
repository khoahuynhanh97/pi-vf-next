import json

import pytest

from hermes_personal.application.operator_handler import (
    approve_and_advance_workflow,
    revise_brief_and_request_approval,
    revise_scene_plan_and_request_approval,
    resume_storyboard_after_qc_authorization,
)
from video_factory.qc.image_qc import compute_artifact_digest
from video_factory.qc.models import QCReport, QCStage, QCVerdict
from video_factory.qc.repository import QCReportRepository
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.approvals import ApprovalService
from video_factory.workflows.envelope import build_envelope, parse_envelope
from video_factory.workflows.repository import WorkflowRepository


@pytest.fixture
def approval(tmp_path):
    conn = connect(tmp_path / 'operator.db')
    repo = WorkflowRepository(conn)
    svc = ApprovalService(conn)
    artifact = {'brief_id': 'brief-1', 'version': 3, 'project_id': 'project-7'}
    payload = dict(owner_user_id='owner', workflow_id='workflow-2', project_id='project-7',
                   gate='AwaitingBriefApproval', workflow_version=1,
                   artifact_id='brief-1', artifact_version=3, artifact_ref=artifact)
    req = svc.submit_request(payload)
    agg = dict(state='AwaitingBriefApproval', owner_user_id='owner', project_id='project-7',
               latest_artifact=artifact, pending_approval_id=req, pending_approval_gate=payload['gate'])
    repo.commit_workflow('workflow-2', json.dumps(agg), [], expected_version=0)
    yield conn, repo, svc, req
    conn.close()


@pytest.mark.parametrize('field,value', [('owner_user_id', None), ('project_id', 'foreign'),
                                       ('artifact_id', 'foreign'), ('artifact_version', 2)])
def test_operator_rejects_unbound_approval_without_mutation(approval, field, value):
    conn, repo, svc, req = approval
    payload = svc.get_request(req).payload
    payload[field] = value
    with conn:
        conn.execute('UPDATE approval_request SET payload_json=? WHERE request_id=?', (json.dumps(payload), req))
    before = repo.get_workflow('workflow-2')
    with pytest.raises(ValueError):
        approve_and_advance_workflow(conn, req, owner_user_id='owner', expected_version=1)
    assert repo.get_workflow('workflow-2') == before
    assert svc.get_request(req).status.value == 'pending'
    assert conn.execute('SELECT count(*) FROM workflow_outbox').fetchone()[0] == 0


def test_operator_enqueues_receipt_for_vf_consumer(approval):
    conn, _, svc, req = approval
    approve_and_advance_workflow(conn, req, owner_user_id='owner', expected_version=1)
    envelope = parse_envelope(conn.execute('SELECT message FROM workflow_outbox').fetchone()[0])
    assert envelope.payload['approval_request_id'] == req
    assert envelope.payload['project_id'] == 'project-7'
    assert envelope.payload['kwargs']['approval_receipt']['workflow_version'] == 1
    assert svc.get_request(req).status.value == 'approved'


def test_storyboard_gate_persists_explicit_vertex_render_choice(tmp_path):
    from hermes_personal.application.operator_handler import _current_qc_digest

    conn = connect(tmp_path / "vertex-gate.db")
    artifact = {
        "id": "story-vertex", "storyboard_id": "story-vertex", "version": 1,
        "package_digest": "sha256:" + "a" * 64,
        "generated_keyframes": [{"frame_id": "scene-1-start"}],
    }
    repo, svc, req = _pending_gate(
        conn, "AwaitingStoryboardApproval", "AwaitingStoryboardApproval", artifact,
    )
    report = _qc_report(
        "workflow-qc", QCStage.KEYFRAME, "story-vertex", 1,
        _current_qc_digest(QCStage.KEYFRAME, artifact),
    )
    report.findings["items"] = [{
        "frame_id": "scene-1-start", "verdict": "pass", "score": 90, "hard_failure": False,
    }]
    QCReportRepository(conn).save_report(report)

    approve_and_advance_workflow(
        conn, req, owner_user_id="owner", expected_version=1,
        payload={"render_provider_kind": "vertex_ai"},
    )
    aggregate, _ = repo.get_workflow("workflow-qc")
    envelope = parse_envelope(conn.execute("SELECT message FROM workflow_outbox").fetchone()[0])
    assert aggregate["selected_render_provider"] == "vertex_ai"
    assert envelope.payload["kwargs"]["render_params"] == {"provider_kind": "vertex_ai"}
    assert svc.get_request(req).status.value == "approved"
    conn.close()


def _qc_report(
    workflow_id: str,
    stage: QCStage,
    artifact_id: str,
    artifact_version: int,
    artifact_digest: str,
    verdict: QCVerdict = QCVerdict.PASS,
) -> QCReport:
    return QCReport(
        report_id=f"qc-{workflow_id}-{stage.value}",
        workflow_id=workflow_id,
        stage=stage,
        artifact_id=artifact_id,
        artifact_version=artifact_version,
        artifact_digest=artifact_digest,
        reference_digests=[],
        evaluator_name="test",
        evaluator_version="1",
        rubric_version="1",
        created_at="2026-09-15T00:00:00+00:00",
        score=90 if verdict == QCVerdict.PASS else 40,
        verdict=verdict,
        findings={},
    )


def _pending_gate(conn, state, gate, artifact, workflow_id="workflow-qc"):
    repo = WorkflowRepository(conn)
    svc = ApprovalService(conn)
    artifact_id = artifact.get("resource_pack_id") or artifact.get("render_id") or artifact.get("id")
    artifact_version = artifact.get("version", 1)
    payload = dict(
        owner_user_id="owner",
        workflow_id=workflow_id,
        project_id="project-qc",
        gate=gate,
        workflow_version=1,
        artifact_id=artifact_id,
        artifact_version=artifact_version,
        artifact_ref=artifact,
    )
    req = svc.submit_request(payload)
    agg = dict(
        state=state,
        owner_user_id="owner",
        project_id="project-qc",
        latest_artifact=artifact,
        pending_approval_id=req,
        pending_approval_gate=gate,
    )
    repo.commit_workflow(workflow_id, json.dumps(agg), [], expected_version=0)
    return repo, svc, req


@pytest.mark.parametrize(
    "state,gate,stage",
    [
        ("AwaitingProductLock", "AwaitingProductLock", QCStage.IMAGE),
        ("AwaitingFinalApproval", "AwaitingFinalApproval", QCStage.VIDEO),
    ],
)
def test_operator_blocks_product_and_final_approval_without_current_pass_qc(tmp_path, state, gate, stage):
    conn = connect(tmp_path / "operator-qc-missing.db")
    artifact = {"resource_pack_id": "pack-1", "version": 1, "assets": []}
    if stage == QCStage.VIDEO:
        video = tmp_path / "final.mp4"
        video.write_bytes(b"not-a-real-mp4-for-hash-only")
        artifact = {"render_id": "render-1", "version": 1, "video_url": f"file://{video}"}
    repo, svc, req = _pending_gate(conn, state, gate, artifact)

    before = repo.get_workflow("workflow-qc")
    with pytest.raises(ValueError, match="QC"):
        approve_and_advance_workflow(conn, req, owner_user_id="owner", expected_version=1)

    assert repo.get_workflow("workflow-qc") == before
    assert svc.get_request(req).status.value == "pending"
    assert conn.execute("SELECT count(*) FROM workflow_outbox").fetchone()[0] == 0


def test_operator_allows_product_lock_with_current_pass_image_qc(tmp_path):
    conn = connect(tmp_path / "operator-qc-pass.db")
    artifact = {"resource_pack_id": "pack-1", "version": 1, "assets": []}
    repo, svc, req = _pending_gate(conn, "AwaitingProductLock", "AwaitingProductLock", artifact)
    QCReportRepository(conn).save_report(
        _qc_report(
            "workflow-qc",
            QCStage.IMAGE,
            "pack-1",
            1,
            compute_artifact_digest(artifact),
        )
    )

    approve_and_advance_workflow(conn, req, owner_user_id="owner", expected_version=1)

    assert svc.get_request(req).status.value == "approved"
    assert repo.get_workflow("workflow-qc")[0]["state"] == "locking_resource_pack"


def test_operator_blocks_stale_image_qc_digest(tmp_path):
    conn = connect(tmp_path / "operator-qc-stale.db")
    artifact = {"resource_pack_id": "pack-1", "version": 1, "assets": [{"asset_id": "new"}]}
    repo, svc, req = _pending_gate(conn, "AwaitingProductLock", "AwaitingProductLock", artifact)
    stale_artifact = {"resource_pack_id": "pack-1", "version": 1, "assets": []}
    QCReportRepository(conn).save_report(
        _qc_report(
            "workflow-qc",
            QCStage.IMAGE,
            "pack-1",
            1,
            compute_artifact_digest(stale_artifact),
        )
    )

    with pytest.raises(ValueError, match="QC"):
        approve_and_advance_workflow(conn, req, owner_user_id="owner", expected_version=1)

    assert svc.get_request(req).status.value == "pending"


def test_revising_pending_brief_replaces_gate_two_approval(approval):
    conn, repo, svc, previous_request_id = approval
    revised = {
        "brief_id": "brief-1",
        "version": 4,
        "project_id": "project-7",
        "cta": "Xem them thong tin san pham.",
    }

    result = revise_brief_and_request_approval(
        conn,
        workflow_id="workflow-2",
        owner_user_id="owner",
        revised_brief=revised,
        expected_version=1,
    )

    aggregate, version = repo.get_workflow("workflow-2")
    assert version == 2
    assert aggregate["state"] == "AwaitingBriefApproval"
    assert aggregate["creative_brief"] == revised
    assert aggregate["latest_artifact"] == revised
    assert aggregate["creative_brief_revisions"][0]["artifact"]["brief_id"] == "brief-1"
    assert svc.get_request(previous_request_id).status.value == "rejected"
    replacement = svc.get_request(result["request_id"])
    assert replacement.status.value == "pending"
    assert replacement.payload["workflow_version"] == 2
    assert replacement.payload["supersedes_approval_id"] == previous_request_id


def test_revising_pending_scene_plan_replaces_gate_three_approval(tmp_path):
    conn = connect(tmp_path / "scene-revision.db")
    repo = WorkflowRepository(conn)
    approvals = ApprovalService(conn)
    old = {"scene_plan_id": "scene-1", "version": 1, "project_id": "project-7"}
    previous_id = approvals.submit_request({
        "owner_user_id": "owner", "workflow_id": "workflow-2", "project_id": "project-7",
        "gate": "AwaitingSceneApproval", "workflow_version": 1,
        "artifact_id": "scene-1", "artifact_version": 1, "artifact_ref": old,
    })
    repo.commit_workflow("workflow-2", json.dumps({
        "state": "AwaitingSceneApproval", "owner_user_id": "owner",
        "project_id": "project-7", "scene_plan": old, "latest_artifact": old,
        "pending_approval_id": previous_id, "pending_approval_gate": "AwaitingSceneApproval",
    }), [], expected_version=0)
    revised = {"scene_plan_id": "scene-1", "version": 2, "project_id": "project-7"}

    result = revise_scene_plan_and_request_approval(
        conn, workflow_id="workflow-2", owner_user_id="owner",
        revised_plan=revised, expected_version=1,
    )

    aggregate, version = repo.get_workflow("workflow-2")
    assert version == 2
    assert aggregate["scene_plan"] == revised
    assert aggregate["latest_artifact"] == revised
    assert len(aggregate["scene_plan_revisions"]) == 1
    assert approvals.get_request(previous_id).status.value == "rejected"
    assert approvals.get_request(result["request_id"]).status.value == "pending"
    with pytest.raises(ValueError, match="Stale workflow version"):
        revise_scene_plan_and_request_approval(
            conn, workflow_id="workflow-2", owner_user_id="owner",
            revised_plan=revised, expected_version=1,
        )
    conn.close()


def test_scene_plan_provider_mismatch_reopens_gate_three(tmp_path):
    conn = connect(tmp_path / "scene-provider-recovery.db")
    repo = WorkflowRepository(conn)
    approvals = ApprovalService(conn)
    old = {"scene_plan_id": "scene-1", "version": 2, "project_id": "project-7"}
    previous_id = approvals.submit_request({
        "owner_user_id": "owner", "workflow_id": "workflow-2", "project_id": "project-7",
        "gate": "AwaitingSceneApproval", "workflow_version": 1,
        "artifact_id": "scene-1", "artifact_version": 2, "artifact_ref": old,
    })
    envelope = build_envelope(
        workflow_id="workflow-2", topic="job.dispatch", idempotency_key="storyboard-1",
        payload={"job_name": "product_video.storyboard", "workflow_id": "workflow-2"},
    )
    repo.commit_workflow("workflow-2", json.dumps({
        "state": "creating_storyboard", "owner_user_id": "owner", "project_id": "project-7",
        "scene_plan": old, "latest_artifact": old,
        "latest_approval_receipt": {"gate": "AwaitingSceneApproval", "approval_request_id": previous_id},
    }), [], outbox_message=envelope, expected_version=0)
    with conn:
        conn.execute("UPDATE approval_request SET status='approved' WHERE request_id=?", (previous_id,))
        conn.execute(
            "UPDATE workflow_outbox SET state='quarantined', last_error=? WHERE workflow_id=?",
            ("Job execution failed [PROVIDER_UNAVAILABLE]: END keyframe unsupported", "workflow-2"),
        )

    result = revise_scene_plan_and_request_approval(
        conn, workflow_id="workflow-2", owner_user_id="owner",
        revised_plan={"scene_plan_id": "scene-1", "version": 3, "project_id": "project-7"},
        expected_version=1,
    )

    aggregate, version = repo.get_workflow("workflow-2")
    assert version == 2
    assert aggregate["state"] == "AwaitingSceneApproval"
    assert aggregate["scene_plan"]["version"] == 3
    assert aggregate["pending_approval_id"] == result["request_id"]
    assert approvals.get_request(previous_id).status.value == "approved"
    assert approvals.get_request(result["request_id"]).status.value == "pending"
    conn.close()


def test_operator_keyframe_retry_enqueues_recovery_payload(tmp_path):
    conn = connect(tmp_path / "operator-keyframe-recovery.db")
    repo = WorkflowRepository(conn)
    ApprovalService(conn)
    workflow_id = "workflow-keyframe-recovery"
    storyboard = {
        "schema_version": "storyboard-package-v2",
        "package_digest": "sha256:old-package",
        "generated_keyframes": [{"frame_id": "scene-1-start"}],
    }
    recovery_plan = {
        "blocked_from_video": True,
        "frames": [{
            "frame_id": "scene-1-start",
            "actions": ["rewrite_prompt", "regenerate_keyframe"],
            "feedback": "Preserve the product controls",
        }],
    }
    repo.commit_workflow(
        workflow_id,
        json.dumps({
            "state": "WaitingForOperator",
            "owner_user_id": "owner",
            "project_id": "project-recovery",
            "scene_plan": {"scene_plan_id": "scene-plan-1", "scenes": []},
            "storyboard": storyboard,
            "latest_artifact": storyboard,
            "operator_action": "qc_retry_authorization",
            "operator_stage": QCStage.KEYFRAME.value,
            "keyframe_recovery_plan": recovery_plan,
        }),
        [],
        expected_version=None,
    )

    result = resume_storyboard_after_qc_authorization(
        conn,
        workflow_id=workflow_id,
        owner_user_id="owner",
        authorization={"run_id": "recovery-run-1", "max_cost_usd": 2.0},
        expected_version=1,
    )

    assert result["recovery_attempt"] == 1
    aggregate, _ = repo.get_workflow(workflow_id)
    assert aggregate["state"] == "creating_storyboard"
    envelope = parse_envelope(conn.execute("SELECT message FROM workflow_outbox").fetchone()[0])
    assert envelope.payload["kwargs"]["recovery_plan"] == recovery_plan
    assert envelope.payload["kwargs"]["previous_storyboard"] == storyboard
    assert envelope.payload["kwargs"]["recovery_attempt"] == 1


def test_recover_unknown_keyframe_submission_on_initial_attempt_without_previous_storyboard(tmp_path, monkeypatch):
    from datetime import datetime, timezone, timedelta
    from hermes_personal.application.operator_handler import recover_unknown_keyframe_submission
    from video_factory.integrations.keyframe_provider import KeyframeOperationStore, VertexKeyframeProvider
    from video_factory.integrations.ports import KeyframeGenerationResult
    from video_factory.creative.contracts import (
        ResearchDossier,
        ScriptScenePlanV2,
        ProductIdentityV2,
        EligibleAsset,
        ScriptSceneV2,
    )
    from video_factory.workflows.envelope import build_envelope

    conn = connect(tmp_path / "unknown-keyframe-recovery.db")
    repo = WorkflowRepository(conn)
    workflow_id = "wf-unknown-recovery"
    project_id = "proj-unknown-recovery"

    import hashlib
    ref_img = tmp_path / "ref.png"
    ref_img.write_bytes(b"dummy image content 1")
    ref_sha = hashlib.sha256(ref_img.read_bytes()).hexdigest()

    ref_img2 = tmp_path / "ref2.png"
    ref_img2.write_bytes(b"dummy image content 2")
    ref_sha2 = hashlib.sha256(ref_img2.read_bytes()).hexdigest()

    dossier = ResearchDossier.create(
        workflow_id=workflow_id,
        product_identity=ProductIdentityV2("Baseus", "Bowie WM02", "Baseus Bowie WM02"),
        verified_claims=(),
        eligible_assets=(
            EligibleAsset("asset-1", str(ref_img), ref_sha, "qc-1"),
            EligibleAsset("asset-2", str(ref_img2), ref_sha2, "qc-1"),
        ),
    )

    script = ScriptScenePlanV2.create(
        target_duration_seconds=18,
        brief_digest="sha256:" + "a" * 64,
                scenes=(
            ScriptSceneV2(
                scene_id="scene-1", order=1, story_role="demo", duration_seconds=6,
                narration="Cất tai nghe", overlay_text="Cất gọn",
                visual_objective="Thao tác cất tai nghe",
                environment="Bàn làm việc", subject_action="Đặt tai nghe vào hộp sạc",
                product_interaction="Tai nghe chạm đúng khe sạc", start_state="Hộp mở và trống",
                end_state="Tai nghe nằm trong hộp", visual_change="Từ ngoài vào trong",
                camera_framing="Cận cảnh", camera_movement="Máy cố định",
                transition_out="Cắt", claim_refs=(),
                reference_asset_ids=("asset-1",),
            ),
        ),
    )
    scene_plan_dict = script.to_dict()
    scene_plan_dict["research_dossier"] = dossier.to_dict()

    # Initial storyboard attempt: NO "storyboard" in aggregate yet!
    repo.commit_workflow(
        workflow_id,
        json.dumps({
            "state": "creating_storyboard",
            "owner_user_id": "owner",
            "project_id": project_id,
            "scene_plan": scene_plan_dict,
            "latest_artifact": scene_plan_dict,
        }),
        [],
        expected_version=None,
    )

    # Quarantined outbox entry with NETWORK_UNKNOWN_OUTCOME
    outbox_msg = build_envelope(
        workflow_id=workflow_id,
        topic="job.dispatch",
        idempotency_key="idemp-storyboard-1",
        payload={"job_name": "product_video.storyboard", "workflow_id": workflow_id},
    )
    with conn:
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO workflow_outbox (workflow_id, message, state, last_error, created_at, updated_at)
            VALUES (?, ?, 'quarantined', 'NETWORK_UNKNOWN_OUTCOME: Connection timed out', ?, ?)""",
            (workflow_id, outbox_msg, now_iso, now_iso),
        )

    # Compile expected request to get operation_id
    from video_factory.creative.storyboard import compile_keyframe_requests
    requests = compile_keyframe_requests(
        workflow_id=workflow_id,
        project_id=project_id,
        dossier=dossier,
        script=script,
    )
    req = requests[0]

    # Insert unknown operation in KeyframeOperationStore
    store = KeyframeOperationStore(conn)
    store.claim_first_submission(req)
    with conn:
        conn.execute(
            "UPDATE video_factory_keyframe_operation SET status='unknown', provider_operation_id='op-unknown-1' WHERE operation_id=?",
            (req.operation_id,),
        )

    # Mock provider to report operation as missing (not completed)
    monkeypatch.setattr(
        VertexKeyframeProvider,
        "get_status",
        lambda self, op_id: KeyframeGenerationResult(
            operation_id=req.operation_id,
            provider_operation_id=op_id,
            status="failed",
            error_code="PROVIDER_TIMEOUT",
        ),
    )
    from hermes_personal.integrations.video_factory import keyframe_provider_factory

    monkeypatch.setattr(
        keyframe_provider_factory,
        "resolve_keyframe_provider",
        lambda output_dir: (VertexKeyframeProvider(output_dir=output_dir), "vertex_keyframe"),
    )

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    result = recover_unknown_keyframe_submission(
        conn,
        workflow_id=workflow_id,
        owner_user_id="owner",
        authorization={
            "workflow_id": workflow_id,
            "scope": ["keyframe"],
            "run_id": "recovery-auth-1",
            "max_cost_usd": 5.0,
            "expires_at": future,
        },
    )

    assert result["reconciled"] == 0
    assert result["replaced"] == 1
    assert result["recovery_attempt"] == 1
    assert result["state"] == "creating_storyboard"

    # Verify old operation is retired / failed
    old_op = store.get(req.operation_id)
    assert old_op["status"] == "failed"

    # Verify new outbox message enqueued for storyboard retry
    new_outbox = conn.execute(
        "SELECT message FROM workflow_outbox WHERE workflow_id = ? AND state = 'pending' ORDER BY outbox_id DESC LIMIT 1",
        (workflow_id,),
    ).fetchone()
    assert new_outbox is not None
    env = parse_envelope(new_outbox[0])
    assert env.payload["job_name"] == "product_video.storyboard"
    assert env.payload["kwargs"]["recovery_attempt"] == 1
    assert env.payload["kwargs"]["previous_storyboard"] is None
    assert len(env.payload["kwargs"]["recovery_plan"]["frames"]) == 1
