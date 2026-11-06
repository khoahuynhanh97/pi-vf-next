from __future__ import annotations

import json
import base64
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from video_factory.integrations.nine_router import (
    NineRouterConfigurationError,
    NineRouterOpenAIClient,
    NineRouterRoute,
    NineRouterResponseSchemaError,
    NineRouterSubmissionUncertain,
    NineRouterVisionEvaluator,
    _active_provider_connection_count,
    _live_route_preflight,
    configured_qc_evaluator,
    verify_local_router_route,
)
from video_factory.qc.evaluator import MultimodalEvaluator
from video_factory.qc.keyframe_qc import run_keyframe_qc
from video_factory.qc.image_qc import evaluate_image_item
from video_factory.qc.image_qc import run_image_qc
from video_factory.qc.models import NormalizedProduct, QCVerdict
from video_factory.creative.model_adapter import NineRouterHermesStructuredCreativeModel
from video_factory.creative.generator import StructuredGenerationError
from video_factory.handlers._base import _creative_failure
from video_factory.handlers.registry import create_product_video_topic_handlers
from video_factory.job_contracts import JobFailed
from video_factory.workflows.envelope import build_envelope, parse_envelope
from video_factory.workflows.repository import WorkflowRepository


def _route(*, capabilities: list[str] | None = None, **overrides: object) -> NineRouterRoute:
    value = {
        "enabled": True,
        "endpoint": "http://127.0.0.1:20128/v1",
        "combo_id": "bf9cb8e0-4385-446a-a8f0-c4865db864ce",
        "combo_name": "Vision-QC-Check",
        "model_id": "actual-model",
        "execution_mode": "single_model",
        "capabilities": capabilities or ["json_schema", "vision_single_image", "vision_multiple_images"],
        "fallback_policy": "forbid",
        "retry_attempts": 0,
        "router_no_fallback_attested": True,
        "router_no_retry_attested": True,
        "contract_tested": True,
        "cost_authorization_id": "cost-auth-1",
    }
    value.update(overrides)
    return NineRouterRoute.from_mapping("vision_qc", value)


def test_disabled_router_keeps_vertex_evaluator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("video_factory.integrations.nine_router.load_config", lambda: {})
    assert isinstance(configured_qc_evaluator(), MultimodalEvaluator)


def test_client_preflight_receives_api_key_by_keyword() -> None:
    route = _route()
    seen: list[str] = []

    def preflight(selected_route: NineRouterRoute, *, api_key: str) -> str | None:
        assert selected_route is route
        seen.append(api_key)
        return None

    client = NineRouterOpenAIClient(route, api_key="local-test-key", transport=lambda *_: {}, preflight=preflight)

    assert client.configured_preflight() is None
    assert seen == ["local-test-key"]


def test_enabled_router_rejects_missing_vision_capability() -> None:
    # Capability strings are diagnostic only, not runtime proof.
    assert "vision_single_image" not in _route(capabilities=["json_schema"]).capabilities


def test_combo_member_without_tested_vision_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "router.sqlite"
    db = sqlite3.connect(db_path)
    db.execute("create table combos (id text, models text)")
    db.execute("create table settings (data text)")
    db.execute("insert into combos values (?, ?)", ("bf9cb8e0-4385-446a-a8f0-c4865db864ce", json.dumps(["member-a"])))
    db.execute("insert into settings values (?)", (json.dumps({"providerStrategies": {}}),))
    db.commit()
    db.close()
    route = _route()
    object.__setattr__(route, "expected_members", ("member-a",))
    object.__setattr__(route, "member_capabilities", {"member-a": ["json_schema"]})
    with pytest.raises(NineRouterConfigurationError, match="member lacks"):
        verify_local_router_route(route, db_path=db_path)


def test_route_config_does_not_use_self_asserted_contract_flags() -> None:
    route = _route(fallback_policy="allow", retry_attempts=5, router_no_fallback_attested=True, contract_tested=True)
    assert route.execution_mode == "single_model"


def test_live_single_model_route_allows_provider_rotation_without_changing_model(monkeypatch: pytest.MonkeyPatch) -> None:
    route = _route(model_id="gemini/gemini-3.8-flash")
    payloads = {
        "/api/combos/" + route.combo_id: {
            "data": {"id": route.combo_id, "name": route.combo_name, "models": [route.model_id]},
        },
        "/api/settings": {
            "comboStrategy": "fallback",
            "providerStrategies": {"gemini": {"fallbackStrategy": "round-robin"}},
        },
        "/v1/models": {"data": [{"id": route.model_id, "capabilities": {"vision": True}}]},
    }
    monkeypatch.setattr("video_factory.integrations.nine_router._admin_get", lambda path, **_kwargs: payloads[path])
    monkeypatch.setattr("video_factory.integrations.nine_router._active_provider_connection_count", lambda _provider: 4)

    reason = _live_route_preflight(route, api_key="local-test-key")

    assert reason is None


def test_live_combo_preflight_accepts_router_managed_round_robin(monkeypatch: pytest.MonkeyPatch) -> None:
    route = _route(model_id="gemini/gemini-3.8-flash")
    object.__setattr__(route, "execution_mode", "combo")
    payloads = {
        "/api/combos/" + route.combo_id: {
            "data": {"id": route.combo_id, "name": route.combo_name, "models": ["gemini/one", "gemini/two"]},
        },
        "/api/settings": {
            "comboStrategy": "round-robin",
        },
        "/v1/models": {"data": [
            {"id": "gemini/one", "capabilities": {"vision": True}},
            {"id": "gemini/two", "capabilities": {"vision": True}},
        ]},
    }
    monkeypatch.setattr("video_factory.integrations.nine_router._admin_get", lambda path, **_kwargs: payloads[path])

    reason = _live_route_preflight(route, api_key="local-test-key")

    assert reason is None


def test_live_combo_preflight_accepts_single_request_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    route = _route(model_id="gemini/gemini-3.8-flash")
    object.__setattr__(route, "execution_mode", "combo")
    payloads = {
        "/api/combos/" + route.combo_id: {
            "data": {"id": route.combo_id, "name": route.combo_name, "models": ["gemini/one", "gemini/two"]},
        },
        "/api/settings": {"comboStrategy": "fallback", "comboStrategies": {}},
        "/v1/models": {"data": [
            {"id": "gemini/one", "capabilities": {"vision": True}},
            {"id": "gemini/two", "capabilities": {"vision": True}},
        ]},
    }
    monkeypatch.setattr("video_factory.integrations.nine_router._admin_get", lambda path, **_kwargs: payloads[path])

    assert _live_route_preflight(route, api_key="local-test-key") is None
    assert route.expected_members == ("gemini/one", "gemini/two")
    assert route.live_combo_strategy == "fallback"


def test_combo_request_sends_purpose_name_and_not_pinned_member() -> None:
    route = _route(model_id="must-not-be-sent", execution_mode="combo")
    object.__setattr__(route, "expected_members", ("actual-upstream-model",))
    captured: list[dict[str, Any]] = []

    def transport(request: Any, _timeout: float) -> dict[str, Any]:
        captured.append(json.loads(request.data.decode("utf-8")))
        return {
            "model": "actual-upstream-model",
            "choices": [{"message": {"content": "{}"}}],
            "router": {
                "requested_combo_id": route.combo_id,
                "fallback_used": False,
                "attempt_id": "router-attempt-1",
                "upstream_provider": "gemini",
                "upstream_model": "actual-upstream-model",
                "cost_status": "unknown",
                "cost_usd": None,
            },
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        }

    client = NineRouterOpenAIClient(
        route, api_key="local-test-key", transport=transport,
        preflight=lambda *_a, **_k: None,
    )
    response, receipt = client.complete(messages=[{"role": "user", "content": "check"}])

    assert captured[0]["model"] == "Vision-QC-Check"
    assert captured[0]["model"] != "must-not-be-sent"
    assert receipt["upstream_model"] == "actual-upstream-model"
    assert response["router"]["upstream_model"] == "actual-upstream-model"


def test_live_combo_preflight_rejects_a_text_only_vision_member(monkeypatch: pytest.MonkeyPatch) -> None:
    route = _route()
    object.__setattr__(route, "execution_mode", "combo")
    payloads = {
        "/api/combos/" + route.combo_id: {
            "data": {"id": route.combo_id, "name": route.combo_name, "models": ["gemini/one", "text/two"]},
        },
        "/api/settings": {"comboStrategy": "fallback", "comboStrategies": {}},
        "/v1/models": {"data": [
            {"id": "gemini/one", "capabilities": {"vision": True}},
            {"id": "text/two", "capabilities": {"vision": False}},
        ]},
    }
    monkeypatch.setattr("video_factory.integrations.nine_router._admin_get", lambda path, **_kwargs: payloads[path])

    reason = _live_route_preflight(route, api_key="local-test-key")

    assert reason is not None
    assert "text/two" in reason and "vision" in reason


def test_active_provider_connection_count_uses_live_management_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "video_factory.integrations.nine_router._admin_get",
        lambda _path, **_kwargs: {
            "connections": [
                {"provider": "gemini", "isActive": True, "apiKey": "must-not-be-read"},
                {"provider": "gemini", "isActive": False},
                {"provider": "groq", "isActive": True},
            ]
        },
    )
    assert _active_provider_connection_count("gemini") == 1


def test_usage_reconciliation_uses_live_totals_and_requires_one_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from video_factory.integrations.nine_router import _read_usage_since, _usage_marker

    snapshots = iter((
        {
            "totalRequests": 20, "totalCost": 2.5,
            "recentRequests": [{"timestamp": "2026-10-19T09:00:00Z"}],
        },
        {
            "totalRequests": 21, "totalCost": 2.501,
            "recentRequests": [{
                "timestamp": "2026-10-19T09:00:01Z", "provider": "gemini",
                "model": "gemini-3.8-flash", "promptTokens": 10,
                "completionTokens": 3, "status": "ok",
            }],
        },
    ))
    monkeypatch.setattr("video_factory.integrations.nine_router._admin_get", lambda *_a, **_k: next(snapshots))

    marker = _usage_marker("ignored")
    rows = _read_usage_since(marker, "ignored")

    assert len(rows) == 1
    assert rows[0]["cost"] == pytest.approx(0.001)
    assert rows[0]["tokens"] == 13


def test_usage_reconciliation_refuses_concurrent_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    from video_factory.integrations.nine_router import _read_usage_since

    monkeypatch.setattr(
        "video_factory.integrations.nine_router._admin_get",
        lambda *_a, **_k: {
            "totalRequests": 22, "totalCost": 2.6,
            "recentRequests": [
                {"timestamp": "2026-10-19T09:00:02Z", "model": "gemini-a"},
                {"timestamp": "2026-10-19T09:00:02Z", "model": "gemini-b"},
            ],
        },
    )
    assert _read_usage_since(
        {"total_requests": 20, "total_cost": 2.5, "latest_timestamp": "2026-10-19T09:00:00Z"},
        "ignored",
    ) == []


def test_response_requires_router_receipt_and_refuses_fallback() -> None:
    route = _route()

    def transport(_request: object, _timeout: float) -> dict[str, object]:
        return {
            "id": "request-1",
            "model": "actual-model",
            "choices": [{"message": {"content": "{}"}}],
            "router": {
                "requested_combo_id": route.combo_id,
                "fallback_used": True,
                "attempt_id": "attempt-1",
                "upstream_provider": "upstream",
                "upstream_model": "actual-model",
                "cost_status": "charged",
                "cost_usd": 0.01,
            },
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }

    with pytest.raises(NineRouterSubmissionUncertain, match="unverified"):
        NineRouterOpenAIClient(route, transport=transport).complete(messages=[])


def test_single_model_receipt_requires_explicit_no_fallback() -> None:
    route = _route()
    response = {
        "choices": [{"message": {"content": "{}"}}],
        "router": {
            "requested_combo_id": route.combo_id,
            "fallback_used": None,
            "attempt_id": "single-model-unknown-fallback",
            "upstream_provider": "provider",
            "upstream_model": route.model_id,
            "cost_status": "charged",
            "cost_usd": 0.01,
        },
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }

    with pytest.raises(NineRouterSubmissionUncertain, match="unverified"):
        NineRouterOpenAIClient(route, transport=lambda *_args: response).complete(messages=[])


def test_combo_receipt_with_fallback_counts_as_one_verified_request() -> None:
    route = _route()
    object.__setattr__(route, "execution_mode", "combo")
    object.__setattr__(route, "expected_members", ("gemini/first", "gemini/second"))
    def transport(_request: object, _timeout: float) -> dict[str, object]:
        return {
            "choices": [{"message": {"content": "{}"}}],
            "router": {
                "requested_combo_id": route.combo_id,
                "fallback_used": True,
                "attempt_id": "combo-attempt-1",
                "upstream_provider": "gemini",
                "upstream_model": "gemini/second",
                "cost_status": "charged",
                "cost_usd": 0.01,
            },
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        }

    response, receipt = NineRouterOpenAIClient(route, transport=transport).complete(messages=[])

    assert response["router"]["fallback_used"] is True
    assert receipt["upstream_model"] == "gemini/second"
    assert receipt["router_usage_id"] == "combo-attempt-1"


def test_combo_receipt_without_fallback_result_is_uncertain() -> None:
    route = _route()
    object.__setattr__(route, "execution_mode", "combo")
    object.__setattr__(route, "expected_members", (route.model_id,))
    response = {
        "choices": [{"message": {"content": "{}"}}],
        "router": {
            "requested_combo_id": route.combo_id,
            "fallback_used": None,
            "attempt_id": "combo-attempt-unknown",
            "upstream_provider": "gemini",
            "upstream_model": route.model_id,
            "cost_status": "charged",
            "cost_usd": 0.01,
        },
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }

    with pytest.raises(NineRouterSubmissionUncertain, match="unverified"):
        NineRouterOpenAIClient(route, transport=lambda *_args: response).complete(messages=[])


def test_qc_receipt_with_unknown_cost_is_accepted_and_recorded_as_unknown() -> None:
    route = _route()
    response = {
        "choices": [{"message": {"content": "{}"}}],
        "router": {
            "requested_combo_id": route.combo_id,
            "fallback_used": False,
            "attempt_id": "attempt-cost-unknown",
            "upstream_provider": "provider",
            "upstream_model": route.model_id,
            "cost_status": "uncertain",
            "cost_usd": None,
        },
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }

    _payload, receipt = NineRouterOpenAIClient(
        route, transport=lambda *_args: response
    ).complete(messages=[])

    assert receipt["router_usage_id"] == "attempt-cost-unknown"
    assert receipt["cost_status"] == "unknown"
    assert receipt["cost_usd"] is None


def test_keyframe_schema_error_consumes_one_attempt_and_stops_later_frames(tmp_path: Path) -> None:
    from PIL import Image
    import hashlib

    reference = tmp_path / "approved.png"
    frame_one = tmp_path / "frame-one.png"
    frame_two = tmp_path / "frame-two.png"
    for path, color in ((reference, "red"), (frame_one, "blue"), (frame_two, "green")):
        Image.new("RGB", (8, 8), color=color).save(path)
    frames = [
        {
            "frame_id": frame_id,
            "scene_id": frame_id,
            "image_path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "reference_image_paths": [str(reference)],
                "reference_sha256s": [hashlib.sha256(reference.read_bytes()).hexdigest()],
            "prompt": "product hero",
        }
        for frame_id, path in (("frame-1", frame_one), ("frame-2", frame_two))
    ]
    evaluator = MagicMock()
    evaluator.evaluator_name = "nine_router_vision_qc"
    evaluator.evaluator_version = "1"
    evaluator.model = "Vision-QC-Check"
    evaluator.fake_mode = False
    evaluator.evaluate_multiple_images.side_effect = NineRouterResponseSchemaError(
        "schema mismatch",
        route_receipt={"attempt_id": "router-schema-1", "upstream_model": "gemini/member-2", "cost_status": "unknown"},
    )

    report = run_keyframe_qc(
        workflow_id="wf-keyframe-invalid-schema",
        package={
            "storyboard_id": "storyboard-1", "version": 1,
            "owner_user_id": "owner", "project_id": "project",
            "script": {"scenes": [{"scene_id": "frame-1", "subject_action": "product hero"}, {"scene_id": "frame-2", "subject_action": "product hero"}]},
            "generated_keyframes": frames,
        },
        product=NormalizedProduct(brand="Brand", model="Model", display_name="Brand Model"),
        evaluator=evaluator,
    )

    assert report.verdict == QCVerdict.UNKNOWN
    assert evaluator.evaluate_multiple_images.call_count == 1
    item_findings = report.findings["items"][0]["findings"]
    assert item_findings["schema_invalid"] is True
    assert item_findings["usage_status"] == "consumed"
    assert item_findings["reconcile_required"] is False


def test_creative_records_actual_upstream_and_allows_one_validator_repair() -> None:
    route = NineRouterRoute.from_mapping("creative", {
        "enabled": True,
        "endpoint": "http://127.0.0.1:20128/v1",
        "combo_id": "916bc948-97bf-41ea-95d7-f7373cc5ee08",
        "combo_name": "Story-Script-Agent",
        "model_id": "actual-creative-model",
        "execution_mode": "single_model",
        "capabilities": ["json_schema"],
        "fallback_policy": "forbid",
        "retry_attempts": 0,
        "router_no_fallback_attested": True,
        "router_no_retry_attested": True,
        "contract_tested": True,
        "cost_authorization_id": "cost-auth-1",
    })
    calls = 0

    def transport(_request: object, _timeout: float) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {
            "choices": [{"message": {"content": "not json"}}],
            "router": {
                "requested_combo_id": route.combo_id,
                "fallback_used": False,
                "attempt_id": "attempt-creative",
                "upstream_provider": "upstream",
                "upstream_model": "actual-creative-model",
                "cost_status": "charged",
                "cost_usd": 0.01,
            },
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }

    model = NineRouterHermesStructuredCreativeModel(NineRouterOpenAIClient(route, transport=transport))
    result = model.generate_json(
        system_prompt="system", user_prompt="user", schema_name="schema", json_schema={}, temperature=0.1,
    )
    assert calls == 1
    assert result.provider == "9router"
    assert result.model == "actual-creative-model"
    assert result.route_receipt and result.route_receipt["combo_id"] == route.combo_id
    assert result.retry_allowed is True


def test_timeout_after_send_is_uncertain_and_called_once() -> None:
    route = _route()
    calls = 0

    def transport(_request: object, _timeout: float) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise TimeoutError("timeout after send")

    with pytest.raises(NineRouterSubmissionUncertain, match="reconciliation") as raised:
        NineRouterOpenAIClient(route, transport=transport).complete(messages=[])
    assert calls == 1
    assert raised.value.route_receipt["combo_id"] == route.combo_id
    assert raised.value.route_receipt["cost_status"] == "uncertain"


def test_creative_timeout_is_terminal_and_commits_reserved_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    committed: list[object] = []
    released: list[object] = []
    monkeypatch.setattr("video_factory.handlers._base.commit_budget", lambda allocation: committed.append(allocation))
    monkeypatch.setattr("video_factory.handlers._base.release_budget", lambda allocation: released.append(allocation))
    allocation = object()
    exc = NineRouterSubmissionUncertain(
        "response timed out",
        route_receipt={"attempt_id": "attempt-unknown", "cost_status": "uncertain"},
    )

    result = _creative_failure(exc, allocation, "VF_BRIEF_FAILED")

    assert result.code == "CREATIVE_OUTCOME_UNCERTAIN"
    assert result.retryable is False
    assert "attempt-unknown" in (result.message or "")
    assert committed == [allocation]
    assert released == []


def test_creative_invalid_json_after_send_is_terminal_and_keeps_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    committed: list[object] = []
    released: list[object] = []
    monkeypatch.setattr("video_factory.handlers._base.commit_budget", lambda allocation: committed.append(allocation))
    monkeypatch.setattr("video_factory.handlers._base.release_budget", lambda allocation: released.append(allocation))
    allocation = object()
    exc = StructuredGenerationError(
        "invalid JSON response",
        route_receipt={"attempt_id": "attempt-invalid-json", "cost_status": "charged"},
        request_sent=True,
    )

    result = _creative_failure(exc, allocation, "VF_BRIEF_FAILED")

    assert result.code == "CREATIVE_RESPONSE_INVALID"
    assert result.retryable is False
    assert "attempt-invalid-json" in (result.message or "")
    assert committed == [allocation]
    assert released == []


def test_creative_unknown_is_persisted_for_operator_and_not_advanced() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE workflow (workflow_id TEXT PRIMARY KEY, aggregate_json TEXT NOT NULL, version INTEGER NOT NULL);
        CREATE TABLE processed_command (workflow_id TEXT, command_id TEXT PRIMARY KEY);
        CREATE TABLE workflow_event (event_id TEXT PRIMARY KEY, workflow_id TEXT, event_type TEXT, timestamp TEXT, payload_json TEXT);
        CREATE TABLE workflow_outbox (outbox_id TEXT PRIMARY KEY, workflow_id TEXT, message TEXT, created_at TEXT, attempt_count INTEGER DEFAULT 0);
        """
    )
    repo = WorkflowRepository(connection)
    repo.commit_workflow(
        "wf-creative-unknown",
        json.dumps({"state": "creating_brief", "owner_user_id": "local-owner"}),
        [],
        expected_version=0,
    )

    class _HandlerService:
        def get_handlers_map(self):
            return {
                "product_video.creative_brief": lambda *_args, **_kwargs: JobFailed(
                    retryable=False,
                    code="CREATIVE_OUTCOME_UNCERTAIN",
                    message='reconcile attempt vf9r_test with route_receipt={"cost_status":"uncertain"}',
                )
            }

    topics = create_product_video_topic_handlers(
        workflow_repository=repo,
        handlers_service=_HandlerService(),
        provider_mode="fake",
    )
    envelope = parse_envelope(build_envelope(
        workflow_id="wf-creative-unknown",
        topic="job.dispatch",
        idempotency_key="creative-attempt-1",
        payload={"job_name": "product_video.creative_brief", "workflow_id": "wf-creative-unknown"},
    ))

    topics["job.dispatch"](envelope)

    aggregate, _ = repo.get_workflow("wf-creative-unknown")
    assert aggregate["state"] == "WaitingForOperator"
    assert aggregate["operator_action"] == "provider_reconciliation"
    assert aggregate["operator_stage"] == "direction"
    assert aggregate["failure_code"] == "CREATIVE_OUTCOME_UNCERTAIN"
    assert "vf9r_test" in aggregate["operator_reason"]
    assert connection.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0] == 0
    connection.close()


def test_timeout_receipt_includes_live_usage_when_router_reconciles(monkeypatch: pytest.MonkeyPatch) -> None:
    route = _route()
    calls = 0

    def timed_out_transport(_request: object, _timeout: float) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise TimeoutError("client response timeout after upstream completed")

    monkeypatch.setattr("video_factory.integrations.nine_router._default_transport", timed_out_transport)
    monkeypatch.setattr("video_factory.integrations.nine_router._live_route_preflight", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "video_factory.integrations.nine_router._usage_marker",
        lambda _key: {"total_requests": 7, "total_cost": 0.10, "latest_timestamp": "before"},
    )
    monkeypatch.setattr(
        "video_factory.integrations.nine_router._read_usage_since",
        lambda _marker, _key: [{
            "id": "2026-10-19T09:00:00Z", "provider": "gemini", "model": "gemini-3.8-flash",
            "cost": 0.001, "promptTokens": 9, "completionTokens": 3, "tokens": 12, "status": "ok",
        }],
    )

    with pytest.raises(NineRouterSubmissionUncertain) as raised:
        NineRouterOpenAIClient(route, api_key="test", timeout=0.01).complete(messages=[])

    receipt = raised.value.route_receipt
    assert calls == 1
    assert receipt["attempt_id"].startswith("vf9r_")
    assert receipt["upstream_model"] == "gemini-3.8-flash"
    assert receipt["cost_status"] == "recorded"
    assert receipt["cost_usd"] == pytest.approx(0.001)
    assert receipt["total_tokens"] == 12
    assert "route_receipt=" in str(raised.value)


def test_qc_schema_failure_is_unknown_not_a_pass(tmp_path: Path) -> None:
    route = _route()

    def transport(_request: object, _timeout: float) -> dict[str, object]:
        return {
            "id": "request-2",
            "model": "actual-model",
            "choices": [{"message": {"content": json.dumps({"verdict": "pass"})}}],
            "router": {
                "requested_combo_id": route.combo_id,
                "fallback_used": False,
                "attempt_id": "attempt-2",
                "upstream_provider": "upstream",
                "upstream_model": "actual-model",
                "cost_status": "charged",
                "cost_usd": 0.01,
            },
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }

    image = tmp_path / "source.png"
    from PIL import Image
    Image.new("RGB", (8, 8), "white").save(image)
    item = evaluate_image_item(
        asset_id="asset-1",
        local_path=image,
        product=NormalizedProduct(brand="Brand", model="Model", variant="", display_name="Brand Model"),
        evaluator=configured_qc_evaluator(route=route, transport=transport),
    )
    assert item.verdict == QCVerdict.UNKNOWN
    assert item.eligible is False


def test_image_qc_sends_strict_schema_matching_the_runtime_validator(tmp_path: Path) -> None:
    route = _route(model_id="gemini/gemini-3.8-flash")
    image = tmp_path / "source.png"
    image.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLx5wAAAABJRU5ErkJggg=="))
    response = {
        "choices": [{"message": {"content": json.dumps({
            "identity_score": 50,
            "clarity_score": 20,
            "usability_score": 12,
            "total_score": 82,
            "verdict": "pass",
            "hard_failure": False,
            "reason": "Product identity is clear",
            "eligible": True,
            "source_branding_detected": False,
            "identity_mismatch": False,
            "view_angle": "front",
            "shot_type": "product",
            "visible_details": ["Kingston logo"],
            "scene_suitability": ["product is clearly visible"],
            "risk_flags": [],
            "text_coverage_percent": 2,
        })}}],
        "router": {
            "requested_combo_id": route.combo_id,
            "fallback_used": False,
            "attempt_id": "image-schema-contract",
            "upstream_provider": "gemini",
            "upstream_model": route.model_id,
            "cost_status": "charged",
            "cost_usd": 0.01,
        },
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }

    def transport(request: object, _timeout: float) -> dict[str, object]:
        body = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
        assert body["max_tokens"] == 4096
        assert body["response_format"]["type"] == "json_schema"
        schema = body["response_format"]["json_schema"]["schema"]
        assert set(schema["required"]) >= {
            "source_branding_detected", "identity_mismatch", "eligible", "text_coverage_percent"
        }
        assert schema["additionalProperties"] is False
        system_message = next(item["content"] for item in body["messages"] if item["role"] == "system")
        user_message = next(item["content"] for item in body["messages"] if item["role"] == "user")
        user_text = next(item["text"] for item in user_message if item["type"] == "text")
        assert "Return one JSON object only, using every required field and its declared type" in system_message
        assert "If evidence is uncertain, return pass with no exclusionary flags" in user_text
        assert "total_score must equal the sum" in user_text
        return response

    evaluator = NineRouterVisionEvaluator(
        NineRouterOpenAIClient(route, api_key="test", transport=transport), stage="image"
    )
    from video_factory.qc.rubric import IMAGE_QC_SYSTEM_PROMPT, build_image_qc_prompt

    result = evaluator.evaluate_image(
        image,
        build_image_qc_prompt("Brand", "Model"),
        system_instruction=IMAGE_QC_SYSTEM_PROMPT,
    )

    assert result["verdict"] == "pass"


def test_qc_schema_failure_stops_before_sending_next_image(tmp_path: Path) -> None:
    route = _route()
    calls = 0

    def transport(_request: object, _timeout: float) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {
            "choices": [{"message": {"content": json.dumps({"verdict": "pass"})}}],
            "router": {
                "requested_combo_id": route.combo_id,
                "fallback_used": False,
                "attempt_id": f"attempt-{calls}",
                "upstream_provider": "provider",
                "upstream_model": route.model_id,
                "cost_status": "charged",
                "cost_usd": 0.01,
            },
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }

    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    from PIL import Image
    Image.new("RGB", (12, 12), "white").save(first)
    Image.new("RGB", (12, 12), "black").save(second)
    report, eligible = run_image_qc(
        workflow_id="wf-router-schema-stop",
        resource_pack={
            "resource_pack_id": "pack-router-schema-stop",
            "version": 1,
            "owner_user_id": "owner",
            "project_id": "project",
            "assets": [
                {"asset_id": "one", "local_path": str(first)},
                {"asset_id": "two", "local_path": str(second)},
            ],
        },
        product=NormalizedProduct(brand="Brand", model="Model", variant="", display_name="Brand Model"),
        evaluator=configured_qc_evaluator(route=route, transport=transport),
        owner_user_id="owner",
        project_id="project",
    )
    assert calls == 1
    assert report.verdict == QCVerdict.UNKNOWN
    assert eligible == []
    assert report.findings["items"][0]["findings"].get("schema_invalid") is True
    assert report.findings["items"][0]["findings"]["usage_status"] == "consumed"
    assert report.findings["items"][0]["findings"]["reconcile_required"] is False
