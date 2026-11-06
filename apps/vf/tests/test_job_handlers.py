from __future__ import annotations

import sqlite3
from types import SimpleNamespace
import pytest

from video_factory.handlers import (
    ProductVideoWorkflowState,
    HITL_WAITING_STATES,
    JobContext,
    JobSucceeded,
    JobFailed,
    ProductVideoJobHandlers,
    verify_job_digests,
    _compute_digest,
    register_product_video_job_handlers,
)
from video_factory.workflows.budgets import init_default_engine, BudgetPolicy
from video_factory.workflows.engine import WorkflowEngine
from video_factory.workflows.jobs import JobDispatcher
from video_factory.workflows.repository import WorkflowRepository


@pytest.fixture(autouse=True)
def setup_budget():
    init_default_engine(BudgetPolicy(max_total=1000, per_channel={"video_factory": 500, "product_intelligence": 500}))


@pytest.fixture
def test_engine():
    conn = sqlite3.connect(":memory:")
    repo = WorkflowRepository(conn)
    with conn:
        conn.execute("CREATE TABLE IF NOT EXISTS workflow (workflow_id TEXT PRIMARY KEY, aggregate_json TEXT, version INTEGER)")
        conn.execute("CREATE TABLE IF NOT EXISTS processed_command (workflow_id TEXT, command_id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE IF NOT EXISTS workflow_event (event_id TEXT PRIMARY KEY, workflow_id TEXT, event_type TEXT, timestamp TEXT, payload_json TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS workflow_outbox (workflow_id TEXT, message TEXT, created_at TEXT)")
    return WorkflowEngine(repo)


def test_research_job(test_engine):
    test_engine.set_workflow_state("wf1", ProductVideoWorkflowState.RESEARCHING.value)
    handlers = ProductVideoJobHandlers(workflow_engine=test_engine)
    ctx = JobContext(workflow_id="wf1", job_id="j1")
    result = handlers.research_job(ctx, "Smart Watch", ["health", "fitness"])
    assert isinstance(result, JobSucceeded)
    assert "summary" in result.artifact_ref


def test_research_job_forwards_daily_feed_identity_to_pi(test_engine):
    class CapturingPI:
        def __init__(self):
            self.kwargs = None

        def research(self, _name, _keywords, **kwargs):
            self.kwargs = kwargs
            return {"summary": "grounded"}

    pi = CapturingPI()
    test_engine.set_workflow_state("wf-identity", ProductVideoWorkflowState.RESEARCHING.value)
    handlers = ProductVideoJobHandlers(workflow_engine=test_engine, pi_client=pi)

    result = handlers.research_job(
        JobContext(workflow_id="wf-identity", job_id="job-identity"),
        "Xiaomi Mijia Desk Lamp Lite",
        ["desk lamp"],
        brand="Xiaomi",
        model="Mijia Desk Lamp Lite",
        canonical_product_key="xiaomi:mijia-desk-lamp-lite",
    )

    assert isinstance(result, JobSucceeded)
    assert pi.kwargs["brand"] == "Xiaomi"
    assert pi.kwargs["model"] == "Mijia Desk Lamp Lite"
    assert pi.kwargs["canonical_product_key"] == "xiaomi:mijia-desk-lamp-lite"


def test_resource_pack_job(test_engine):
    test_engine.set_workflow_state("wf1", ProductVideoWorkflowState.BUILDING_RESOURCE_PACK.value)
    handlers = ProductVideoJobHandlers(workflow_engine=test_engine)
    ctx = JobContext(workflow_id="wf1", job_id="j2")
    research_result = {"report": '{"product_name": "Watch", "keywords": ["fitness"]}'}
    result = handlers.resource_pack_job(ctx, research_result)
    assert isinstance(result, JobSucceeded)
    assert "assets_mapping" in result.artifact_ref


def test_lock_job(test_engine):
    test_engine.set_workflow_state("wf1", ProductVideoWorkflowState.LOCKING_RESOURCE_PACK.value)
    handlers = ProductVideoJobHandlers(workflow_engine=test_engine)
    ctx = JobContext(workflow_id="wf1", job_id="j3")
    pack_dict = {"summary": "test pack", "assets_mapping": {"a1": "url1"}}
    result = handlers.lock_job(ctx, pack_dict)
    assert isinstance(result, JobSucceeded)
    assert result.artifact_ref["status"] == "locked"


def test_creative_brief_job(test_engine):
    test_engine.set_workflow_state("wf1", ProductVideoWorkflowState.CREATING_BRIEF.value)
    handlers = ProductVideoJobHandlers(workflow_engine=test_engine)
    ctx = JobContext(workflow_id="wf1", job_id="j4")
    result = handlers.creative_brief_job(ctx, "p1")
    assert isinstance(result, JobSucceeded)
    assert "artifact_ref" in result.artifact_ref


def test_scene_plan_job(test_engine):
    test_engine.set_workflow_state("wf1", ProductVideoWorkflowState.CREATING_SCENE_PLAN.value)
    handlers = ProductVideoJobHandlers(workflow_engine=test_engine)
    ctx = JobContext(workflow_id="wf1", job_id="j5")
    result = handlers.scene_plan_job(ctx, "brief_ref")
    assert isinstance(result, JobSucceeded)


def test_storyboard_job(test_engine):
    test_engine.set_workflow_state("wf1", ProductVideoWorkflowState.CREATING_STORYBOARD.value)
    handlers = ProductVideoJobHandlers(workflow_engine=test_engine)
    ctx = JobContext(workflow_id="wf1", job_id="j6")
    result = handlers.storyboard_job(ctx, "scene_ref")
    assert isinstance(result, JobSucceeded)


def test_render_job(test_engine):
    test_engine.set_workflow_state("wf1", ProductVideoWorkflowState.RENDERING.value)
    handlers = ProductVideoJobHandlers(workflow_engine=test_engine)
    ctx = JobContext(workflow_id="wf1", job_id="j7")
    result = handlers.render_job(ctx, "story_ref")
    assert isinstance(result, JobSucceeded)


def test_export_job(test_engine):
    test_engine.set_workflow_state("wf1", ProductVideoWorkflowState.EXPORTING.value)
    handlers = ProductVideoJobHandlers(workflow_engine=test_engine)
    ctx = JobContext(workflow_id="wf1", job_id="j8")
    result = handlers.export_job(ctx, "render_ref")
    assert isinstance(result, JobSucceeded)


@pytest.mark.parametrize("hitl_state", list(HITL_WAITING_STATES))
def test_no_job_dispatch_from_hitl_approval_states(test_engine, hitl_state):
    test_engine.set_workflow_state("wf_hitl", hitl_state.value if isinstance(hitl_state, ProductVideoWorkflowState) else hitl_state)
    handlers = ProductVideoJobHandlers(workflow_engine=test_engine)
    ctx = JobContext(workflow_id="wf_hitl", job_id="j_hitl")

    res_research = handlers.research_job(ctx, "Watch", ["fit"])
    assert isinstance(res_research, JobFailed)
    assert res_research.code == "HITL_APPROVAL_REQUIRED"

    res_brief = handlers.creative_brief_job(ctx, "p1")
    assert isinstance(res_brief, JobFailed)
    assert res_brief.code == "HITL_APPROVAL_REQUIRED"

    res_scene = handlers.scene_plan_job(ctx, "brief_ref")
    assert isinstance(res_scene, JobFailed)
    assert res_scene.code == "HITL_APPROVAL_REQUIRED"

    res_story = handlers.storyboard_job(ctx, "scene_ref")
    assert isinstance(res_story, JobFailed)
    assert res_story.code == "HITL_APPROVAL_REQUIRED"

    res_render = handlers.render_job(ctx, "story_ref")
    assert isinstance(res_render, JobFailed)
    assert res_render.code == "HITL_APPROVAL_REQUIRED"

    res_export = handlers.export_job(ctx, "render_ref")
    assert isinstance(res_export, JobFailed)
    assert res_export.code == "HITL_APPROVAL_REQUIRED"


def test_illegal_workflow_state_rejection(test_engine):
    test_engine.set_workflow_state("wf_ill", ProductVideoWorkflowState.RESEARCHING.value)
    handlers = ProductVideoJobHandlers(workflow_engine=test_engine)
    ctx = JobContext(workflow_id="wf_ill", job_id="j_illegal")
    res = handlers.render_job(ctx, "story_ref")
    assert isinstance(res, JobFailed)
    assert res.code == "ILLEGAL_WORKFLOW_STATE"


def test_register_product_video_job_handlers(test_engine):
    dispatcher = JobDispatcher()
    registered = register_product_video_job_handlers(dispatcher, workflow_engine=test_engine)
    assert len(registered) == 9  # Updated: added retry_image_qc_job
    assert "product_video.research" in registered
    assert "product_video.retry_image_qc" in registered
    assert "product_video.creative_brief" in registered
    assert "product_video.render" in registered
    assert registered.dispatcher is dispatcher


def test_job_idempotency_via_deterministic_clients(test_engine):
    test_engine.set_workflow_state("wf1", ProductVideoWorkflowState.CREATING_BRIEF.value)
    handlers = ProductVideoJobHandlers(workflow_engine=test_engine)
    ctx = JobContext(workflow_id="wf1", job_id="j9")
    res1 = handlers.creative_brief_job(ctx, "p1")
    res2 = handlers.creative_brief_job(ctx, "p1")
    assert res1.artifact_ref["artifact_ref"] == res2.artifact_ref["artifact_ref"]


def test_verify_job_digests():
    data = {"key": "value"}
    digest = _compute_digest(data)
    assert verify_job_digests("test", {"key": data}, {"key": digest}) is True
    assert verify_job_digests("test", {"key": data}, {"key": "wrong"}) is False


def test_budget_exhaustion_fails_job(test_engine):
    init_default_engine(BudgetPolicy(max_total=10, per_channel={"video_factory": 5}))
    test_engine.set_workflow_state("wf1", ProductVideoWorkflowState.RENDERING.value)
    handlers = ProductVideoJobHandlers(workflow_engine=test_engine)
    ctx = JobContext(workflow_id="wf1", job_id="j10")
    result = handlers.render_job(ctx, "story_ref")
    assert isinstance(result, JobFailed)
    assert result.code == "VF_RENDER_FAILED"


def test_paid_job_budget_is_bound_to_workflow_campaign_and_authorization(test_engine, monkeypatch):
    test_engine.set_workflow_state("wf-budget", ProductVideoWorkflowState.CREATING_BRIEF.value)
    captured: dict = {}

    def _reserve(channel, amount, **kwargs):
        captured.update({"channel": channel, "amount": amount, **kwargs})
        return SimpleNamespace(reservation_id="res-budget")

    monkeypatch.setattr("video_factory.handlers.reserve_budget", _reserve)
    monkeypatch.setattr("video_factory.handlers.commit_budget", lambda _allocation: None)
    handlers = ProductVideoJobHandlers(workflow_engine=test_engine)
    authorization = {
        "authorization_id": "auth-live-1",
        "campaign_id": "campaign-live",
        "campaign_max_cost_usd": 100,
        "workflow_max_cost_usd": 10,
    }

    result = handlers.creative_brief_job(
        JobContext(workflow_id="wf-budget", job_id="job-budget", owner_user_id="operator-1"),
        "product-1",
        qc_authorization=authorization,
    )

    assert isinstance(result, JobSucceeded)
    assert captured["owner_user_id"] == "operator-1"
    assert captured["scope_id"] == "wf-budget"
    assert captured["capability_id"] == "product_video.creative_brief"
    assert captured["campaign_id"] == "campaign-live"
    assert captured["campaign_max"] == 10_000
    assert captured["scope_max"] == 1_000
    assert captured["authorization_id"] == "auth-live-1"
    assert captured["attempt_id"] == "wf-budget:creative_brief:job-budget"
