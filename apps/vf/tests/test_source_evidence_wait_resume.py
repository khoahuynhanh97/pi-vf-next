"""Configured Product Lock waits for independent source review without calling creative."""

from __future__ import annotations

import json

from video_factory.handlers import JobSucceeded, create_product_video_topic_handlers
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.envelope import build_envelope, parse_envelope
from video_factory.workflows.repository import WorkflowRepository


def test_configured_lock_waits_for_source_evidence_and_replay_does_not_enqueue(tmp_path) -> None:
    database = tmp_path / "workflow.db"
    connection = connect(database)
    repository = WorkflowRepository(connection)
    workflow_id = "wf-source-review"
    pack = {
        "resource_pack_id": "pack-1",
        "product_identity": {"brand": "Xiaomi", "model": "Mijia Desk Lamp Lite"},
    }
    locked = {"resource_pack_id": "pack-1", "schema": "product_resource_pack_locked"}
    repository.commit_workflow(workflow_id, json.dumps({
        "state": "locking_resource_pack",
        "project_id": "project-1",
        "owner_user_id": "owner",
        "resource_pack": pack,
        "latest_approval_receipt": {"approval_request_id": "approval-1"},
    }), [], expected_version=None)

    class LockOnly:
        def get_handlers_map(self):
            return {"product_video.lock": lambda *_args, **_kwargs: JobSucceeded(artifact_ref=locked)}

    dispatch = create_product_video_topic_handlers(
        workflow_repository=repository,
        handlers_service=LockOnly(),
        provider_mode="configured",
        capability_snapshot=object(),
    )["job.dispatch"]
    envelope = parse_envelope(build_envelope(
        workflow_id=workflow_id, topic="job.dispatch", idempotency_key="lock-1",
        payload={"job_name": "product_video.lock", "workflow_id": workflow_id,
                 "args": [pack], "kwargs": {}, "owner_user_id": "owner"},
    ))
    dispatch(envelope)
    aggregate, version = repository.get_workflow(workflow_id)
    assert aggregate["state"] == "WaitingForOperator"
    assert aggregate["operator_action"] == "attach_source_evidence"
    assert "SOURCE_BACKED_REQUIRED" in aggregate["operator_reason"]
    assert aggregate["locked_pack"] == locked
    assert connection.execute("SELECT COUNT(*) FROM workflow_outbox WHERE workflow_id=?", (workflow_id,)).fetchone()[0] == 0
    dispatch(envelope)
    assert repository.get_workflow(workflow_id)[1] == version
    connection.close()

    restarted = connect(database)
    assert WorkflowRepository(restarted).get_workflow(workflow_id)[0]["operator_action"] == "attach_source_evidence"
    restarted.close()
