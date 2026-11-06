import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from video_factory.application import candidate_api, studio_commands
from video_factory.application.app import create_application
from video_factory.integrations.keyframe_provider import KeyframeOperationStore
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.envelope import build_envelope
from video_factory.workflows.repository import WorkflowRepository


@pytest.fixture
def studio(tmp_path, monkeypatch):
    conn = connect(tmp_path / "commands.db")
    repo = WorkflowRepository(conn)
    repo.commit_workflow("wf", json.dumps({
        "owner_user_id": "local-owner", "project_id": "project", "state": "WaitingForOperator",
        "operator_stage": "direction", "operator_action": "creative_review", "failure_reason": "Old failure",
        "locked_pack": {"resource_pack_id": "pack-1", "version": 1},
        "source_context": {"product_key": "product", "source_date": "2026-10-26", "source_batch_id": "batch"},
    }), [], expected_version=0)
    runs = []
    monkeypatch.setattr(studio_commands, "prepare_runner", lambda *_: {"fixture": True})

    def fake_runner(**kwargs):
        runs.append(kwargs)
        candidate_api._studio_batch_lock.release()

    monkeypatch.setattr(candidate_api, "_run_studio_batch_background", fake_runner)
    with TestClient(create_application(kanban_connection=conn)) as client:
        yield client, conn, runs
    conn.close()


URL = "/api/operator/workflow/wf/actions"
ORIGIN = {"origin": "http://testserver"}


def command(action="recover-creative", version=1, **changes):
    return {"action_id": action, "expected_version": version, "request_id": str(uuid4()), "note": "Chạy lại từ Studio", **changes}


def update(conn, **changes):
    agg, version = WorkflowRepository(conn).get_workflow("wf")
    agg.update(changes)
    WorkflowRepository(conn).commit_workflow("wf", json.dumps(agg), [], expected_version=version)
    return version + 1


def quarantine(conn, name, error="COMPOSITION_FAILED", attempt=1, maximum=5):
    message = build_envelope(workflow_id="wf", topic="job.dispatch", idempotency_key="fixture-job", payload={"job_name": name, "workflow_id": "wf"})
    conn.execute("INSERT INTO workflow_outbox(workflow_id,message,state,last_error,attempt_count,max_attempts,created_at) VALUES(?,?,'quarantined',?,?,?,CURRENT_TIMESTAMP)", ("wf", message, error, attempt, maximum))
    conn.commit()


def test_real_creative_command_enqueues_once_and_uuid_replay_uses_receipt(studio, monkeypatch):
    client, conn, runs = studio
    options = client.get(URL).json()
    assert options["options"][0]["command"] == "recover-creative"
    body = command()
    response = client.post(URL, json=body, headers=ORIGIN)
    assert response.status_code == 202, response.text
    assert response.json()["execution"] == "studio_selected_batch"
    aggregate, version = WorkflowRepository(conn).get_workflow("wf")
    assert aggregate["state"] == "creating_brief" and version == 2
    message = json.loads(conn.execute("SELECT message FROM workflow_outbox").fetchone()[0])
    assert message["payload"]["job_name"] == "product_video.creative_brief"
    assert len(runs) == 1
    status = client.get(URL + '/' + body["request_id"]).json()
    assert status["state"] == "accepted"
    monkeypatch.setattr(studio_commands, "prepare_runner", lambda *_: pytest.fail("Replay must not prepare a second run"))
    replay = client.post(URL, json=body, headers=ORIGIN)
    assert replay.status_code == 200 and replay.json()["reused"] is True
    assert len(runs) == 1
    assert conn.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0] == 1
    mismatch = client.post(URL, json={**body, "note": "Different payload"}, headers=ORIGIN)
    assert mismatch.status_code == 409


def test_stale_snapshot_cannot_run_or_create_an_intent(studio):
    client, conn, runs = studio
    response = client.post(URL, json=command(version=9), headers=ORIGIN)
    assert response.status_code == 409
    assert conn.execute("SELECT COUNT(*) FROM workflow_event").fetchone()[0] == 0
    assert not runs


@pytest.mark.parametrize("state", ["pending", "leased"])
def test_active_outbox_blocks_recovery_without_stealing_lease(studio, state):
    client, conn, runs = studio
    conn.execute("INSERT INTO workflow_outbox(workflow_id,message,state,lease_token,created_at) VALUES('wf','{}',?,'preserve-token',CURRENT_TIMESTAMP)", (state,))
    conn.commit()
    before = WorkflowRepository(conn).get_workflow("wf")
    response = client.post(URL, json=command(), headers=ORIGIN)
    assert response.status_code == 409
    assert WorkflowRepository(conn).get_workflow("wf") == before
    assert conn.execute("SELECT lease_token FROM workflow_outbox").fetchone()[0] == "preserve-token"
    assert not runs


def test_ambiguous_keyframe_blocks_resend(studio):
    client, conn, runs = studio
    KeyframeOperationStore(conn)
    conn.execute("INSERT INTO video_factory_keyframe_operation(operation_id,workflow_id,project_id,frame_id,request_digest,status) VALUES('op','wf','project','scene1','digest','unknown')")
    conn.commit()
    options = client.get(URL).json()["options"]
    assert options and all(not option["enabled"] for option in options)
    assert client.post(URL, json=command(), headers=ORIGIN).status_code == 409
    assert not runs


@pytest.mark.parametrize("paused_workflow,code,enabled", [
    ("other", "QC_OUTCOME_UNCERTAIN", True),
    ("wf", "QC_OUTCOME_UNCERTAIN", False),
    ("other", "QC_PROVIDER_UNAVAILABLE", False),
    ("", "QC_OUTCOME_UNCERTAIN", False),
])
def test_pending_action_respects_pause_scope(studio, paused_workflow, code, enabled):
    from video_factory.automation.failure_classification import FailureCategory
    from video_factory.workflows.runtime_control import get_pipeline_pause, pause_pipeline

    client, conn, runs = studio
    conn.execute("INSERT INTO workflow_outbox(workflow_id,message,state,created_at) VALUES('wf',?,'pending',CURRENT_TIMESTAMP)",
                 (json.dumps({"payload": {"job_name": "product_video.research"}}),))
    conn.commit()
    pause_pipeline(conn, category=FailureCategory.QC_INFRA_PAUSED, code=code,
                   message="Reconcile existing request", workflow_id=paused_workflow)
    before = get_pipeline_pause(conn)
    option = next(o for o in client.get(URL).json()["options"] if o["id"].startswith("run-pending:"))
    assert option["enabled"] is enabled
    if not enabled:
        assert code in option["reason"]
    assert get_pipeline_pause(conn) == before
    assert not runs


def test_busy_studio_runner_does_not_create_a_parallel_worker(studio):
    client, conn, runs = studio
    assert candidate_api._studio_batch_lock.acquire(blocking=False)
    try:
        assert all(not o["enabled"] for o in client.get(URL).json()["options"])
        assert client.post(URL, json=command(), headers=ORIGIN).status_code == 409
        assert not runs
        assert conn.execute("SELECT COUNT(*) FROM workflow_event").fetchone()[0] == 0
    finally:
        candidate_api._studio_batch_lock.release()


def test_origin_owner_and_payload_are_strict(studio):
    client, conn, runs = studio
    assert client.post(URL, json=command()).status_code == 403
    assert client.post(URL, json=command(), headers={"origin": "http://evil.example"}).status_code == 403
    assert client.post(URL, json={**command(), "render_provider_kind": "other"}, headers=ORIGIN).status_code == 422
    assert client.post(URL, json=command(action="cmd.exe /c reset --all-blocked"), headers=ORIGIN).status_code == 409
    update(conn, owner_user_id="other-owner")
    assert client.get(URL).status_code == 403
    assert client.post(URL, json=command(version=2), headers=ORIGIN).status_code == 403
    assert not runs


def test_retry_exact_export_job_preserves_attempt_count_and_other_workflows(studio):
    client, conn, runs = studio
    version = update(conn, state="exporting", operator_stage=None, operator_action=None)
    quarantine(conn, "product_video.export", attempt=2)
    conn.execute("INSERT INTO workflow_outbox(workflow_id,message,state,attempt_count,created_at) VALUES('other','{}','leased',3,CURRENT_TIMESTAMP)")
    conn.commit()
    response = client.post(URL, json=command("retry-job:1:2", version), headers=ORIGIN)
    assert response.status_code == 202, response.text
    rows = conn.execute("SELECT workflow_id,state,attempt_count FROM workflow_outbox ORDER BY outbox_id").fetchall()
    assert tuple(rows[0]) == ("wf", "pending", 2)
    assert tuple(rows[1]) == ("other", "leased", 3)
    assert len(runs) == 1


def test_exhausted_outbox_does_not_gain_retry_budget(studio):
    client, conn, runs = studio
    version = update(conn, state="exporting", operator_stage=None, operator_action=None)
    quarantine(conn, "product_video.export", attempt=5)
    assert not any(o["id"].startswith("retry-job:") for o in client.get(URL).json()["options"])
    assert client.post(URL, json=command("retry-job:1:5", version), headers=ORIGIN).status_code == 409
    assert conn.execute("SELECT attempt_count FROM workflow_outbox").fetchone()[0] == 5
    assert not runs


def test_keyframe_quarantine_maps_to_actual_step_and_offers_exact_presend_operation(studio):
    client, conn, _ = studio
    update(conn, state="creating_storyboard", operator_stage=None, operator_action=None)
    error = "GOOGLE_FLOW_KEYFRAME_FAILED: GOOGLE_FLOW_NEW_PROJECT_SELECTOR_NOT_UNIQUE"
    quarantine(conn, "product_video.storyboard", error)
    KeyframeOperationStore(conn)
    conn.execute("INSERT INTO video_factory_keyframe_operation(operation_id,workflow_id,project_id,frame_id,request_digest,status,error_code,error_message,provider_metadata_json) VALUES('op','wf','project','scene1','digest','failed','GOOGLE_FLOW_KEYFRAME_FAILED',?,?)", (error, json.dumps({"send_attempted": False})))
    conn.commit()
    detail = client.get("/api/operator/workflow/wf").json()
    assert detail["quarantined_job"]["step"] == "KEYFRAME_GENERATION"
    assert "không còn ở bước tạo brief" not in detail["quarantined_job"]["reason"]
    option = next(o for o in client.get(URL).json()["options"] if o["id"] == "recover-keyframe:op")
    assert option["params"]["operation_id"] == "op"
    conn.execute("UPDATE video_factory_keyframe_operation SET provider_metadata_json=?", (json.dumps({"send_attempted": True}),))
    conn.commit()
    assert not any(o["id"] == "recover-keyframe:op" for o in client.get(URL).json()["options"])


def test_uncertain_handler_result_leaves_an_intent_that_cannot_replay(studio, monkeypatch):
    client, conn, _ = studio
    from video_factory.application import operator_handler

    monkeypatch.setattr(operator_handler, "recover_creative_stage", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("Lost receipt")))
    # TestClient surfaces unhandled server errors; this is the same HTTP 5xx path.
    body = command()
    with pytest.raises(RuntimeError, match="Lost receipt"):
        client.post(URL, json=body, headers=ORIGIN)
    assert client.post(URL, json=body, headers=ORIGIN).status_code == 409
    assert conn.execute("SELECT COUNT(*) FROM workflow_event WHERE event_type='StudioCommandStarted'").fetchone()[0] == 1
    assert client.get(URL + '/' + body["request_id"]).json()["state"] == "unresolved"


def test_command_receipt_absence_is_not_a_proof_it_is_safe_to_resend(studio):
    client, _, runs = studio
    response = client.get(URL + '/' + str(uuid4()))
    assert response.status_code == 200 and response.json()["state"] == "not_found"
    assert not runs


def test_continuing_a_queued_job_runs_without_enqueuing_another(studio):
    client, conn, runs = studio
    quarantine(conn, "product_video.creative_brief", "cannot be dispatched on registry")
    conn.execute("UPDATE workflow_outbox SET state='pending'")
    conn.commit()
    response = client.post(URL, json=command("run-pending:1:1"), headers=ORIGIN)
    assert response.status_code == 202, response.text
    assert len(runs) == 1
    assert conn.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0] == 1


def test_recover_creative_keeps_the_existing_two_retry_cap(studio):
    client, conn, runs = studio
    version = update(conn, operator_stage="script", creative_brief={"brief_id": "b1"})
    for i in range(2):
        conn.execute("INSERT INTO workflow_event VALUES(?, 'wf', 'CreativeStageRecovered', CURRENT_TIMESTAMP, ?)", (f"prior-{i}", json.dumps({"stage": "script"})))
    conn.commit()
    body = command(version=version)
    response = client.post(URL, json=body, headers=ORIGIN)
    assert response.status_code == 409 and "two Scene Plan retries" in response.json()["detail"]
    assert conn.execute("SELECT COUNT(*) FROM workflow_event WHERE event_type='CreativeStageRecovered'").fetchone()[0] == 2
    assert client.get(URL + '/' + body["request_id"]).json()["state"] == "rejected"
    assert not runs


@pytest.mark.parametrize('saved_policy, expected_level', [
    ({'qc_level': 'low', 'disable_keyframe_qc': True}, 'low'),
    ({'qc_level': 'middle'}, 'middle'),
    ({'qc_level': 'high'}, 'high'),
    ({}, 'middle'),
])
def test_prepare_runner_binds_exact_ready_batch_and_saved_policy(tmp_path, monkeypatch, saved_policy, expected_level):
    from test_studio_creative_batch import _ready_two_product_batch

    from video_factory.creative.agents.contracts import BackendProfile

    inbox, keys = _ready_two_product_batch(tmp_path)
    monkeypatch.setenv("HERMES_EXCHANGE_INBOX", str(inbox))
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"profile": BackendProfile(
        provider_kind="fixture", image_provider_kind="fixture", version="test",
        duration_options=(8,), image_modes=("reference",), max_reference_images=2,
        supports_end_frame=False, supports_negative_prompt=True, audio_policy="silent",
    ).to_dict()}), encoding="utf-8")
    monkeypatch.setenv("VF_CREATIVE_RUN_CONFIG", str(profile))
    conn = connect(tmp_path / "runner.db")
    try:
        WorkflowRepository(conn).commit_workflow("wf", json.dumps({
            "owner_user_id": "local-owner", "creative_policy": saved_policy,
            "source_context": {"product_key": keys["Lay's"], "source_date": "2026-10-25", "source_batch_id": "run_studio_test"},
        }), [], expected_version=0)
        result = studio_commands.prepare_runner(conn, "wf")
        assert result["product_keys"] == [keys["Lay's"]]
        assert result["target_date"] == "2026-10-25"
        assert result["qc_level"] == expected_level
        assert result["disable_keyframe_qc"] is bool(saved_policy.get('disable_keyframe_qc'))
        assert result["db_path"] == (tmp_path / "runner.db").resolve()
        update(conn, source_context={"product_key": keys["Lay's"], "source_date": "2026-10-25", "source_batch_id": "other-batch"})
        with pytest.raises(ValueError, match="READY batch"):
            studio_commands.prepare_runner(conn, "wf")
        assert conn.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0] == 0
    finally:
        conn.close()


def test_version_change_at_handler_boundary_is_rejected(studio, monkeypatch):
    client, conn, runs = studio
    from video_factory.application import operator_handler

    original = operator_handler.recover_creative_stage

    def concurrent_change(*args, **kwargs):
        update(conn, operator_reason="Changed by another actor")
        return original(*args, **kwargs)

    monkeypatch.setattr(operator_handler, "recover_creative_stage", concurrent_change)
    response = client.post(URL, json=command(), headers=ORIGIN)
    assert response.status_code == 409 and "đã thay đổi" in response.json()["detail"]
    assert conn.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0] == 0
    assert WorkflowRepository(conn).get_workflow("wf")[0]["operator_reason"] == "Changed by another actor"
    assert not runs


def test_retry_id_fences_outbox_changes_even_when_aggregate_version_is_unchanged(studio):
    client, conn, runs = studio
    version = update(conn, state="exporting", operator_stage=None, operator_action=None)
    quarantine(conn, "product_video.export", attempt=2)
    option = next(o for o in client.get(URL).json()["options"] if o["id"].startswith("retry-job:"))
    conn.execute("UPDATE workflow_outbox SET attempt_count=3")
    conn.commit()
    assert client.post(URL, json=command(option["id"], version), headers=ORIGIN).status_code == 409
    assert conn.execute("SELECT attempt_count FROM workflow_outbox").fetchone()[0] == 3
    assert not runs
