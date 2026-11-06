from __future__ import annotations

import json
import sqlite3

from video_factory.cli import _advance_qc_waiting_workflow
from video_factory.qc.models import QCStage
from video_factory.workflows.repository import WorkflowRepository


def test_semantic_keyframe_failure_schedules_flow_recovery_without_cost_authorization() -> None:
    connection = sqlite3.connect(":memory:")
    repo = WorkflowRepository(connection)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS workflow (
            workflow_id TEXT PRIMARY KEY, aggregate_json TEXT NOT NULL, version INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS processed_command (
            workflow_id TEXT NOT NULL, command_id TEXT NOT NULL, PRIMARY KEY(workflow_id, command_id)
        );
        CREATE TABLE IF NOT EXISTS workflow_event (
            event_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, event_type TEXT NOT NULL,
            timestamp TEXT NOT NULL, payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workflow_outbox (
            workflow_id TEXT, message TEXT, created_at TEXT
        );
        """
    )
    workflow_id = "wf-keyframe-no-cost"
    repo.commit_workflow(
        workflow_id,
        json.dumps({
            "state": "WaitingForOperator",
            "operator_action": "qc_retry_authorization",
            "operator_stage": "keyframe",
            "operator_qc_report_id": "qc-fail-1",
            "project_id": "project-1",
            "owner_user_id": "local-owner",
            "scene_plan": {"schema_version": "scene-plan-v2", "scenes": []},
            "storyboard": {
                "storyboard_id": "storyboard-1",
                "package_digest": "sha256:failed-package",
                "generated_keyframes": [
                    {"frame_id": "scene-1-start", "sha256": "old-sha"},
                    {"frame_id": "scene-1-end", "sha256": "old-end-sha"},
                ],
            },
            "keyframe_recovery_plan": {
                "frames": [{
                    "frame_id": "scene-1-start",
                    "scene_id": "scene-1",
                    "actions": ["regenerate_keyframe"],
                    "feedback": "Product identity mismatch",
                }],
            },
        }),
        [],
        expected_version=0,
    )

    recovery_id = _advance_qc_waiting_workflow(
        connection, workflow_id, QCStage.KEYFRAME
    )

    assert recovery_id.startswith("recovery:")
    aggregate, _ = repo.get_workflow(workflow_id)
    assert aggregate["state"] == "creating_storyboard"
    assert "qc_authorization" not in aggregate
    row = connection.execute(
        "SELECT message FROM workflow_outbox WHERE workflow_id=?", (workflow_id,)
    ).fetchone()
    payload = json.loads(row[0])["payload"]
    assert payload["kwargs"]["recovery_plan"]["frames"]
    assert "qc_authorization" not in payload["kwargs"]
    assert "qc_max_cost_usd" not in payload["kwargs"]
