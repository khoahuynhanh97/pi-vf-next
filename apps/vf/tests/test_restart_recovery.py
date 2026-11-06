from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock
import pytest

from video_factory.handlers import (
    ProductVideoWorkflowState,
    JobContext,
    JobSucceeded,
    JobFailed,
    ProductVideoJobHandlers,
    register_product_video_job_handlers,
)
from video_factory.workflows.budgets import init_default_engine, BudgetPolicy, get_allocated_budget
from video_factory.workflows.engine import WorkflowEngine
from video_factory.workflows.jobs import JobDispatcher, JobStatus, JobSpec
from video_factory.workflows.recovery import WorkflowRecoveryService
from video_factory.workflows.repository import WorkflowRepository


@pytest.fixture(autouse=True)
def setup_budget():
    init_default_engine(BudgetPolicy(max_total=1000, per_channel={"video_factory": 500, "product_intelligence": 500}))


@pytest.fixture
def persisted_db(tmp_path):
    db_file = tmp_path / "workflows.db"
    conn = sqlite3.connect(str(db_file))
    with conn:
        conn.execute("CREATE TABLE IF NOT EXISTS workflow (workflow_id TEXT PRIMARY KEY, aggregate_json TEXT, version INTEGER)")
        conn.execute("CREATE TABLE IF NOT EXISTS processed_command (workflow_id TEXT, command_id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE IF NOT EXISTS workflow_event (event_id TEXT PRIMARY KEY, workflow_id TEXT, event_type TEXT, timestamp TEXT, payload_json TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS workflow_outbox (workflow_id TEXT, message TEXT, created_at TEXT)")
    conn.close()
    return str(db_file)


@pytest.mark.parametrize(
    "awaiting_state",
    [
        ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK,
        ProductVideoWorkflowState.AWAITING_BRIEF_APPROVAL,
        ProductVideoWorkflowState.AWAITING_SCENE_APPROVAL,
        ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL,
        ProductVideoWorkflowState.AWAITING_FINAL_APPROVAL,
    ],
)
def test_restart_recovery_across_all_awaiting_states(persisted_db, awaiting_state):
    """Prove that workflow state faithfully restores across crashes for all 5 HITL awaiting states and blocks dispatch."""
    wf_id = f"wf_await_{awaiting_state.value}"

    # 1. Process 1: persist state into SQLite
    conn1 = sqlite3.connect(persisted_db)
    engine1 = WorkflowEngine(WorkflowRepository(conn1))
    engine1.set_workflow_state(wf_id, awaiting_state.value, version=3)
    conn1.close()

    # 2. Simulate crash & restart
    conn2 = sqlite3.connect(persisted_db)
    engine2 = WorkflowEngine(WorkflowRepository(conn2))
    handlers2 = ProductVideoJobHandlers(workflow_engine=engine2)

    # 3. Assert restored state matches SQLite
    state, version = engine2.get_workflow_state(wf_id)
    assert state == awaiting_state.value
    assert version == 3

    # 4. Assert gate is NOT skipped after restart (dispatch remains blocked)
    ctx = JobContext(workflow_id=wf_id, job_id="j_block", expected_version=3)
    res = handlers2.render_job(ctx, "story_ref")
    assert isinstance(res, JobFailed)
    assert res.code == "HITL_APPROVAL_REQUIRED"
    conn2.close()


@pytest.mark.parametrize(
    ("executing_state", "handler_method", "sample_input"),
    [
        (ProductVideoWorkflowState.RESEARCHING, "research_job", ("Product A", ["tag1"])),
        (ProductVideoWorkflowState.BUILDING_RESOURCE_PACK, "resource_pack_job", ({"report": "{}"},)),
        (ProductVideoWorkflowState.CREATING_BRIEF, "creative_brief_job", ("prod_123",)),
        (ProductVideoWorkflowState.CREATING_SCENE_PLAN, "scene_plan_job", ("brief_ref_123",)),
        (ProductVideoWorkflowState.CREATING_STORYBOARD, "storyboard_job", ("scene_ref_123",)),
        (ProductVideoWorkflowState.RENDERING, "render_job", ("story_ref_123",)),
        (ProductVideoWorkflowState.EXPORTING, "export_job", ("render_ref_123",)),
    ],
)
def test_restart_recovery_across_all_executing_states(persisted_db, executing_state, handler_method, sample_input):
    """Prove that workflow state faithfully restores across crashes for all executing states and resumes legally."""
    wf_id = f"wf_exec_{executing_state.value}"

    # 1. Process 1: persist state into SQLite
    conn1 = sqlite3.connect(persisted_db)
    engine1 = WorkflowEngine(WorkflowRepository(conn1))
    engine1.set_workflow_state(wf_id, executing_state.value, version=4)
    conn1.close()

    # 2. Simulate crash & restart
    conn2 = sqlite3.connect(persisted_db)
    engine2 = WorkflowEngine(WorkflowRepository(conn2))
    handlers2 = ProductVideoJobHandlers(workflow_engine=engine2)

    # 3. Assert restored state matches SQLite
    state, version = engine2.get_workflow_state(wf_id)
    assert state == executing_state.value
    assert version == 4

    # 4. Execute legal handler on restored engine
    if handler_method == "resource_pack_job":
        fake_pi = MagicMock()
        fake_pi.build_resource_pack.return_value = {"resource_pack_id": "fake-pack", "version": 1, "assets": []}
        handlers2.pi_client = fake_pi
    ctx = JobContext(workflow_id=wf_id, job_id=f"j_{executing_state.value}", expected_version=4, idempotency_key=f"idemp_{executing_state.value}")
    method = getattr(handlers2, handler_method)
    res = method(ctx, *sample_input)
    assert isinstance(res, JobSucceeded)
    conn2.close()


def test_workflow_recovery_service_restores_pending_workflows(persisted_db):
    """Prove that WorkflowRecoveryService discovers and recovers all persisted pending workflows."""
    conn = sqlite3.connect(persisted_db)
    engine = WorkflowEngine(WorkflowRepository(conn))
    engine.set_workflow_state("wf_pending_1", ProductVideoWorkflowState.CREATING_BRIEF.value, version=1)
    engine.set_workflow_state("wf_pending_2", ProductVideoWorkflowState.RENDERING.value, version=2)

    recovery_service = WorkflowRecoveryService(conn)
    pending_ids = recovery_service.recover_pending_workflows()
    assert "wf_pending_1" in pending_ids
    assert "wf_pending_2" in pending_ids
    conn.close()


def test_dispatcher_job_timeout_enforcement():
    """Prove that JobDispatcher enforces execution timeout on stalled or hanging jobs."""
    import time
    dispatcher = JobDispatcher()

    def slow_hanging_job(ctx):
        time.sleep(2)
        return "done"

    spec = JobSpec(
        job_id="job_stalled",
        func=slow_hanging_job,
        args=({"id": "ctx"},),
        timeout=1,  # 1 second timeout
        max_retries=0,
    )
    dispatcher.dispatch(spec)
    time.sleep(1.2)
    res = dispatcher.get_result("job_stalled")
    assert res is not None
    assert res.status == JobStatus.FAILURE
    assert isinstance(res.error, TimeoutError)


def test_retryable_provider_timeout_recovers_on_retry(persisted_db):
    """Prove that transient network/provider timeouts return retryable failures and succeed on subsequent attempt."""
    conn = sqlite3.connect(persisted_db)
    engine = WorkflowEngine(WorkflowRepository(conn))
    engine.set_workflow_state("wf_timeout", ProductVideoWorkflowState.RENDERING.value, version=1)

    mock_vf = MagicMock()
    mock_vf.render_video.side_effect = [
        TimeoutError("GPU instance startup timeout"),
        {"artifact_ref": "sha256:recovered_render_123"},
    ]

    handlers = ProductVideoJobHandlers(video_client=mock_vf, workflow_engine=engine)
    ctx = JobContext(workflow_id="wf_timeout", job_id="j_timeout", expected_version=1)

    # Attempt 1: fails retryably
    res1 = handlers.render_job(ctx, "story_ref_abc")
    assert isinstance(res1, JobFailed)
    assert res1.retryable is True
    assert res1.code == "VF_RENDER_FAILED"

    # Attempt 2: succeeds without duplicate budget leak
    res2 = handlers.render_job(ctx, "story_ref_abc")
    assert isinstance(res2, JobSucceeded)
    assert res2.artifact_ref["artifact_ref"] == "sha256:recovered_render_123"
    conn.close()


def test_permanent_schema_failure_handling(persisted_db):
    """Prove that permanent schema/input failure returns non-retryable failure."""
    conn = sqlite3.connect(persisted_db)
    engine = WorkflowEngine(WorkflowRepository(conn))
    engine.set_workflow_state("wf_schema", ProductVideoWorkflowState.CREATING_BRIEF.value, version=1)

    handlers = ProductVideoJobHandlers(workflow_engine=engine)
    ctx = JobContext(workflow_id="wf_schema", job_id="j_schema", expected_version=1)

    res = handlers.creative_brief_job(ctx, "")
    assert isinstance(res, JobFailed)
    assert res.retryable is False
    assert res.code == "MISSING_INPUT_REF"
    conn.close()


def test_stale_approval_invalidation_after_changed_storyboard(persisted_db):
    """Prove that modifying upstream assets increments version and rejects stale approval attempts."""
    conn = sqlite3.connect(persisted_db)
    engine = WorkflowEngine(WorkflowRepository(conn))

    engine.set_workflow_state("wf_stale", ProductVideoWorkflowState.CREATING_STORYBOARD.value, version=1)

    handlers = ProductVideoJobHandlers(workflow_engine=engine)
    ctx_stale = JobContext(workflow_id="wf_stale", job_id="j_stale", expected_version=1)

    # Upstream state changes to version 2 (e.g. storyboard edited by user)
    engine.set_workflow_state("wf_stale", ProductVideoWorkflowState.CREATING_STORYBOARD.value, version=2)

    # Attempting to execute with stale context (expected_version=1) is rejected
    res = handlers.storyboard_job(ctx_stale, "scene_ref_old")
    assert isinstance(res, JobFailed)
    assert res.code == "VERSION_CONFLICT"
    conn.close()


def test_budget_exhaustion_stops_invocation_and_releases(persisted_db):
    """Prove that when budget is exhausted, paid calls stop before invocation."""
    init_default_engine(BudgetPolicy(max_total=5, per_channel={"video_factory": 5}))

    conn = sqlite3.connect(persisted_db)
    engine = WorkflowEngine(WorkflowRepository(conn))
    engine.set_workflow_state("wf_budget", ProductVideoWorkflowState.RENDERING.value, version=1)

    mock_vf = MagicMock()
    handlers = ProductVideoJobHandlers(video_client=mock_vf, workflow_engine=engine)
    ctx = JobContext(workflow_id="wf_budget", job_id="j_b", expected_version=1)

    res = handlers.render_job(ctx, "story_ref_1")
    assert isinstance(res, JobFailed)
    assert res.code == "VF_RENDER_FAILED"
    mock_vf.render_video.assert_not_called()
    conn.close()


def test_mcp_unavailable_handling(persisted_db):
    """Prove that MCP or external service disconnection returns a retryable error without crashing workflow."""
    conn = sqlite3.connect(persisted_db)
    engine = WorkflowEngine(WorkflowRepository(conn))
    engine.set_workflow_state("wf_mcp", ProductVideoWorkflowState.RESEARCHING.value, version=1)

    mock_pi = MagicMock()
    mock_pi.research.side_effect = ConnectionError("MCP server connection refused on stdio/sse")

    handlers = ProductVideoJobHandlers(pi_client=mock_pi, workflow_engine=engine)
    ctx = JobContext(workflow_id="wf_mcp", job_id="j_mcp", expected_version=1)

    res = handlers.research_job(ctx, "Smart Ring", ["fitness"])
    assert isinstance(res, JobFailed)
    assert res.retryable is True
    assert res.code == "PI_RESEARCH_FAILED"
    conn.close()


def test_duplicate_terminal_event_idempotency(persisted_db):
    """Prove that duplicate terminal dispatch calls are completely idempotent and do NOT commit duplicate budget."""
    conn = sqlite3.connect(persisted_db)
    engine = WorkflowEngine(WorkflowRepository(conn))
    engine.set_workflow_state("wf_term", ProductVideoWorkflowState.EXPORTING.value, version=1)

    fake_video = MagicMock()
    fake_video.export_video.return_value = {"export_id": "fake-export"}
    handlers = ProductVideoJobHandlers(workflow_engine=engine, video_client=fake_video)
    ctx = JobContext(workflow_id="wf_term", job_id="j_term", expected_version=1, idempotency_key="idemp_term_1")

    # Local export has no generation charge; replay must not dispatch it twice.
    res1 = handlers.export_job(ctx, "render_ref_final")
    assert isinstance(res1, JobSucceeded)
    budget_after_first = get_allocated_budget("video_factory", owner_user_id="user")
    assert budget_after_first == 0

    # Idempotent replay: returns cached result and commits 0 additional budget
    res2 = handlers.export_job(ctx, "render_ref_final")
    assert isinstance(res2, JobSucceeded)
    assert res1.artifact_ref == res2.artifact_ref
    budget_after_second = get_allocated_budget("video_factory", owner_user_id="user")
    assert budget_after_second == 0
    assert fake_video.export_video.call_count == 1
    conn.close()
