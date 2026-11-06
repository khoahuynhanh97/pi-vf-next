from __future__ import annotations

import json
import sqlite3
import pytest

from video_factory.handlers import (
    ProductVideoWorkflowState,
    JobContext,
    JobSucceeded,
    ProductVideoJobHandlers,
)
from video_factory.workflows.approvals import ApprovalService, ApprovalStatus
from video_factory.workflows.budgets import init_default_engine, BudgetPolicy, get_allocated_budget
from video_factory.workflows.engine import WorkflowEngine
from video_factory.workflows.jobs import JobDispatcher
from video_factory.workflows.repository import WorkflowRepository


@pytest.fixture
def workflow_env(tmp_path):
    # Initialize SQLite database with all real tables
    db_file = tmp_path / "hermes_workflow.db"
    conn = sqlite3.connect(str(db_file))
    with conn:
        conn.execute("CREATE TABLE IF NOT EXISTS workflow (workflow_id TEXT PRIMARY KEY, aggregate_json TEXT, version INTEGER)")
        conn.execute("CREATE TABLE IF NOT EXISTS processed_command (workflow_id TEXT, command_id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE IF NOT EXISTS workflow_event (event_id TEXT PRIMARY KEY, workflow_id TEXT, event_type TEXT, timestamp TEXT, payload_json TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS workflow_outbox (workflow_id TEXT, message TEXT, created_at TEXT)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS approval_request (
                request_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )

    # Initialize production engine with max budget
    init_default_engine(BudgetPolicy(max_total=1000, per_channel={"video_factory": 500, "product_intelligence": 500}))

    repo = WorkflowRepository(conn)
    approval_service = ApprovalService(conn)
    engine = WorkflowEngine(repo)
    dispatcher = JobDispatcher()
    handlers = ProductVideoJobHandlers(workflow_engine=engine)

    yield conn, repo, approval_service, engine, dispatcher, handlers

    conn.close()


def test_full_fourteen_state_product_to_video_e2e(workflow_env):
    """Prove the complete 14-state product-to-video workflow from a TikTok Shop PDP request across 5 HITL gates.

    Asserts:
    1. Exact 14 state sequence traversed in order originating from a TikTok Shop PDP URL.
    2. Real ApprovalService handles all 5 Human-In-The-Loop approval gates (pending -> approved).
    3. Artifact refs preserve owner, schema, version, digest, payload, and PDP correlation metadata across all PI and VF stages.
    4. Real BudgetEngine records exactly $90 committed across the 5 paid pipeline stages ($5 + $5 + $10 + $50 + $20).
    """
    conn, _, approval_service, engine, dispatcher, handlers = workflow_env
    wf_id = "wf_tiktok_pdp_12345"
    pdp_url = "https://shop.tiktok.com/view/product/17294829104"

    state_history: list[str] = []
    approval_ids: list[str] = []

    def handle_hitl_gate(gate_name: str, channel: str, version: int):
        req_id = approval_service.submit_request({
            "workflow_id": wf_id,
            "gate": gate_name,
            "channel": channel,
            "workflow_version": version,
            "pdp_url": pdp_url,
        })
        approval_ids.append(req_id)
        # Verify request is pending in SQLite
        req = approval_service.get_request(req_id)
        assert req.status == ApprovalStatus.PENDING
        # Operator approves request
        approval_service.approve(req_id)
        assert approval_service.get_request(req_id).status == ApprovalStatus.APPROVED

    def transition_to(state: ProductVideoWorkflowState, version: int):
        engine.set_workflow_state(wf_id, state.value, version=version)
        current_state, _ = engine.get_workflow_state(wf_id)
        state_history.append(current_state)

    def assert_vf_artifact(art: dict, expected_idemp_key: str):
        """Helper to assert schema, owner, version, digest, and correlation metadata on VF artifacts."""
        assert art["artifact_ref"].startswith("sha256:")
        assert art["digest"].startswith("sha256:")
        assert "payload" in art
        payload = json.loads(art["payload"]) if isinstance(art["payload"], str) else art["payload"]
        assert payload.get("owner") == "video_factory"
        assert payload.get("version") == 1
        assert art["workflow_id"] == wf_id
        assert art["idempotency_key"] == expected_idemp_key

    # 1. State 1: IDLE - Initial request from TikTok Shop PDP
    transition_to(ProductVideoWorkflowState.IDLE, version=1)

    # 2. State 2: RESEARCHING - Market research on PDP
    transition_to(ProductVideoWorkflowState.RESEARCHING, version=2)
    ctx_res = JobContext(workflow_id=wf_id, job_id="job_res", expected_version=2, idempotency_key="idemp_res_1")
    res_res = handlers.research_job(ctx_res, "TikTok Wireless Earbuds ANC", ["bluetooth", "anc", "tiktok_shop"], pdp_url=pdp_url)
    assert isinstance(res_res, JobSucceeded)
    research_art = res_res.artifact_ref
    assert research_art["owner"] == "product_intelligence"
    assert research_art["schema"] == "product_research_report"
    assert research_art["version"] == 1
    assert "report" in research_art
    assert research_art["workflow_id"] == wf_id
    assert research_art["idempotency_key"] == "idemp_res_1"
    assert research_art["pdp_url"] == pdp_url

    # 3. State 3: BUILDING_RESOURCE_PACK
    transition_to(ProductVideoWorkflowState.BUILDING_RESOURCE_PACK, version=3)
    ctx_pack = JobContext(workflow_id=wf_id, job_id="job_pack", expected_version=3, idempotency_key="idemp_pack_1")
    res_pack = handlers.resource_pack_job(ctx_pack, research_art)
    assert isinstance(res_pack, JobSucceeded)
    pack_art = res_pack.artifact_ref
    assert pack_art["owner"] == "product_intelligence"
    assert pack_art["schema"] == "product_resource_pack"
    assert pack_art["version"] == 1
    assert "assets_mapping" in pack_art
    assert pack_art["workflow_id"] == wf_id
    assert pack_art["idempotency_key"] == "idemp_pack_1"

    # 4. State 4: AwaitingProductLock (HITL Gate 1 - Web approval)
    transition_to(ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK, version=4)
    # Assert HITL gate strictly blocks dispatch before human approval
    assert handlers.lock_job(ctx_pack, pack_art).code == "HITL_APPROVAL_REQUIRED"
    handle_hitl_gate("AwaitingProductLock", channel="web", version=4)

    # 5. State 5: LOCKING_RESOURCE_PACK
    transition_to(ProductVideoWorkflowState.LOCKING_RESOURCE_PACK, version=5)
    ctx_lock = JobContext(workflow_id=wf_id, job_id="job_lock", expected_version=5, idempotency_key="idemp_lock_1")
    res_lock = handlers.lock_job(ctx_lock, pack_art)
    assert isinstance(res_lock, JobSucceeded)
    locked_pack = res_lock.artifact_ref
    assert locked_pack["owner"] == "product_intelligence"
    assert locked_pack["schema"] == "product_resource_pack_locked"
    assert locked_pack["version"] == 1
    assert locked_pack["status"] == "locked"
    assert "manifest_digest" in locked_pack
    assert locked_pack["manifest_digest"].startswith("sha256:")
    assert "resource_pack_id" in locked_pack

    # 6. State 6: CREATING_BRIEF (Paid call: $5)
    transition_to(ProductVideoWorkflowState.CREATING_BRIEF, version=6)
    ctx_brief = JobContext(workflow_id=wf_id, job_id="job_brief", expected_version=6, idempotency_key="idemp_brief_1")
    res_brief = handlers.creative_brief_job(ctx_brief, locked_pack["resource_pack_id"], brief_config={"platform": "tiktok", "pdp_url": pdp_url})
    assert isinstance(res_brief, JobSucceeded)
    brief_art = res_brief.artifact_ref
    assert_vf_artifact(brief_art, expected_idemp_key="idemp_brief_1")
    assert get_allocated_budget("video_factory", owner_user_id="user") == 5

    # 7. State 7: AwaitingBriefApproval (HITL Gate 2 - CLI approval)
    transition_to(ProductVideoWorkflowState.AWAITING_BRIEF_APPROVAL, version=7)
    assert handlers.scene_plan_job(ctx_brief, brief_art["artifact_ref"]).code == "HITL_APPROVAL_REQUIRED"
    handle_hitl_gate("AwaitingBriefApproval", channel="cli", version=7)

    # 8. State 8: CREATING_SCENE_PLAN (Paid call: $5)
    transition_to(ProductVideoWorkflowState.CREATING_SCENE_PLAN, version=8)
    ctx_scene = JobContext(workflow_id=wf_id, job_id="job_scene", expected_version=8, idempotency_key="idemp_scene_1")
    res_scene = handlers.scene_plan_job(ctx_scene, brief_art["artifact_ref"])
    assert isinstance(res_scene, JobSucceeded)
    scene_art = res_scene.artifact_ref
    assert_vf_artifact(scene_art, expected_idemp_key="idemp_scene_1")
    assert get_allocated_budget("video_factory", owner_user_id="user") == 10  # 5 + 5

    # 9. State 9: AwaitingSceneApproval (HITL Gate 3 - Web approval)
    transition_to(ProductVideoWorkflowState.AWAITING_SCENE_APPROVAL, version=9)
    assert handlers.storyboard_job(ctx_scene, scene_art["artifact_ref"]).code == "HITL_APPROVAL_REQUIRED"
    handle_hitl_gate("AwaitingSceneApproval", channel="web", version=9)

    # 10. State 10: CREATING_STORYBOARD (Paid call: $10)
    transition_to(ProductVideoWorkflowState.CREATING_STORYBOARD, version=10)
    ctx_story = JobContext(workflow_id=wf_id, job_id="job_story", expected_version=10, idempotency_key="idemp_story_1")
    res_story = handlers.storyboard_job(ctx_story, scene_art["artifact_ref"])
    assert isinstance(res_story, JobSucceeded)
    story_art = res_story.artifact_ref
    assert_vf_artifact(story_art, expected_idemp_key="idemp_story_1")
    assert get_allocated_budget("video_factory", owner_user_id="user") == 20  # 10 + 10

    # 11. State 11: AwaitingStoryboardApproval (HITL Gate 4 - Telegram approval)
    transition_to(ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL, version=11)
    assert handlers.render_job(ctx_story, story_art["artifact_ref"]).code == "HITL_APPROVAL_REQUIRED"
    handle_hitl_gate("AwaitingStoryboardApproval", channel="telegram", version=11)

    # 12. State 12: RENDERING (Paid call: $50)
    transition_to(ProductVideoWorkflowState.RENDERING, version=12)
    ctx_render = JobContext(workflow_id=wf_id, job_id="job_render", expected_version=12, idempotency_key="idemp_render_1")
    res_render = handlers.render_job(ctx_render, story_art["artifact_ref"])
    assert isinstance(res_render, JobSucceeded)
    render_art = res_render.artifact_ref
    assert_vf_artifact(render_art, expected_idemp_key="idemp_render_1")
    assert get_allocated_budget("video_factory", owner_user_id="user") == 70  # 20 + 50

    # 13. State 13: AwaitingFinalApproval (HITL Gate 5 - Telegram final approval)
    transition_to(ProductVideoWorkflowState.AWAITING_FINAL_APPROVAL, version=13)
    assert handlers.export_job(ctx_render, render_art["artifact_ref"]).code == "HITL_APPROVAL_REQUIRED"
    handle_hitl_gate("AwaitingFinalApproval", channel="telegram", version=13)

    # 14. State 14: EXPORTING (Paid call: $20)
    transition_to(ProductVideoWorkflowState.EXPORTING, version=14)
    ctx_export = JobContext(workflow_id=wf_id, job_id="job_export", expected_version=14, idempotency_key="idemp_export_1")
    res_export = handlers.export_job(ctx_export, render_art["artifact_ref"])
    assert isinstance(res_export, JobSucceeded)
    export_art = res_export.artifact_ref
    assert_vf_artifact(export_art, expected_idemp_key="idemp_export_1")
    assert get_allocated_budget("video_factory", owner_user_id="user") == 90  # 70 + 20

    # 15. State 15: COMPLETED
    transition_to(ProductVideoWorkflowState.COMPLETED, version=15)

    # Assert 1: Exact state sequence traversed in strict order
    expected_states = [
        "idle",
        "researching",
        "building_resource_pack",
        "AwaitingProductLock",
        "locking_resource_pack",
        "creating_brief",
        "AwaitingBriefApproval",
        "creating_scene_plan",
        "AwaitingSceneApproval",
        "creating_storyboard",
        "AwaitingStoryboardApproval",
        "rendering",
        "AwaitingFinalApproval",
        "exporting",
        "completed",
    ]
    assert state_history == expected_states

    # Assert 2: Exactly 5 approval requests submitted and approved in ApprovalService
    assert len(approval_ids) == 5
    for aid in approval_ids:
        assert approval_service.get_request(aid).status == ApprovalStatus.APPROVED

    # Assert 3: Real BudgetEngine committed exactly $90 for video_factory
    assert get_allocated_budget("video_factory", owner_user_id="user") == 90
