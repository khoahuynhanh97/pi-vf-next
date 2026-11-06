from __future__ import annotations

from typing import Any

from video_factory.application.operator_handler import approve_and_advance_workflow
from video_factory.mcp_adapters import MCPVideoFactoryAdapter
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.approvals import ApprovalService
from video_factory.workflows.envelope import parse_envelope
from video_factory.workflows.repository import WorkflowRepository


def _adapter(calls: list[tuple[str, dict[str, Any]]]) -> MCPVideoFactoryAdapter:
    def brief_approve(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(("brief_approve", payload))
        return {"ok": True, "status": "approved"}

    def create_scene_plan(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(("create_scene_plan", payload))
        return {"ok": True, "scene_plan_id": "scene-1", "scenes": [{"scene_id": "scene-1"}]}

    return MCPVideoFactoryAdapter(
        {
            "mcp__video_factory__creative_brief_approve": brief_approve,
            "mcp__video_factory__create_scene_plan": create_scene_plan,
        },
        {
            "brief_approve": "mcp__video_factory__creative_brief_approve",
            "scene_plan": "mcp__video_factory__create_scene_plan",
        },
    )


def test_legacy_approval_receipt_does_not_invent_workflow_version() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    adapter = _adapter(calls)

    adapter.create_scene_plan(
        {"project_id": "project-1", "scenes": [{"scene_id": "scene-1"}]},
        workflow_id="workflow-1",
        owner_user_id="owner-1",
        approval_receipt={"approval_request_id": "approval-1"},
    )

    approval_payload = calls[0][1]
    assert approval_payload["approval_request_id"] == "approval-1"
    assert approval_payload["workflow_id"] == "workflow-1"
    assert approval_payload["workflow_version"] is None


def test_approval_receipt_workflow_version_is_forwarded_when_present() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    adapter = _adapter(calls)

    adapter.create_scene_plan(
        {"project_id": "project-1", "scenes": [{"scene_id": "scene-1"}]},
        workflow_id="workflow-1",
        owner_user_id="owner-1",
        approval_receipt={"approval_request_id": "approval-1", "workflow_version": 13},
    )

    assert calls[0][1]["workflow_version"] == 13


def test_new_operator_receipt_preserves_approved_request_workflow_version(tmp_path: Any) -> None:
    connection = connect(tmp_path / "approval.db")
    repository = WorkflowRepository(connection)
    approvals = ApprovalService(connection)
    payload = {
        "owner_user_id": "owner-1",
        "workflow_id": "workflow-1",
        "project_id": "project-1",
        "gate": "AwaitingBriefApproval",
        "workflow_version": 1,
        "artifact_id": "brief-1",
        "artifact_version": 1,
        "artifact_ref": {"brief_id": "brief-1", "version": 1, "project_id": "project-1"},
    }
    request_id = approvals.submit_request(payload)
    repository.commit_workflow(
        "workflow-1",
        '{"state":"AwaitingBriefApproval","owner_user_id":"owner-1",'
        '"project_id":"project-1","latest_artifact":{"brief_id":"brief-1",'
        '"version":1,"project_id":"project-1"},"pending_approval_id":"' + request_id + '",'
        '"pending_approval_gate":"AwaitingBriefApproval"}',
        [],
        expected_version=0,
    )

    approve_and_advance_workflow(
        connection,
        request_id,
        owner_user_id="owner-1",
        expected_version=1,
    )

    envelope = parse_envelope(connection.execute("SELECT message FROM workflow_outbox").fetchone()[0])
    assert envelope.payload["kwargs"]["approval_receipt"]["workflow_version"] == 1
    connection.close()
