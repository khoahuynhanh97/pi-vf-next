from __future__ import annotations

import json
import sqlite3
from io import BytesIO
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from video_factory.creative.contracts import (
    CreativeBriefV2,
    EligibleAsset,
    EvidenceClaim,
    ProductIdentityV2,
    ResearchDossier,
)
from video_factory.creative.generator import SingleModelCreativeGenerator, StructuredGenerationError
from video_factory.creative.model_adapter import NineRouterHermesStructuredCreativeModel
from video_factory.integrations.nine_router import (
    NineRouterHTTPResponseInvalid,
    NineRouterOpenAIClient,
    NineRouterRoute,
    NineRouterSubmissionUncertain,
    _default_transport,
)
from video_factory.application.operator_handler import recover_creative_stage
from video_factory.workflows.repository import WorkflowRepository


COMBO_ID = "combo-story-script"


def _route() -> NineRouterRoute:
    route = NineRouterRoute.from_mapping("creative", {
        "enabled": True,
        "endpoint": "http://127.0.0.1:20128/v1",
        "combo_id": COMBO_ID,
        "combo_name": "Story-Script-Agent",
        "execution_mode": "combo",
        "capabilities": ["json_schema"],
    })
    object.__setattr__(route, "expected_members", ("gemini/model-a", "groq/model-b"))
    return route


def _dossier() -> ResearchDossier:
    return ResearchDossier.create(
        workflow_id="wf-creative-receipt-test",
        product_identity=ProductIdentityV2("Baseus", "Bowie WM02", "Baseus Bowie WM02"),
        verified_claims=(
            EvidenceClaim(
                "claim-01", "Tai nghe đi kèm hộp sạc", ("evidence-01",),
                source_locators=("https://www.baseus.com/products/bowie-wm02",),
                evidence_snippets=("The charging case stores the earbuds.",),
                evidence_locator="https://www.baseus.com/products/bowie-wm02#details",
                evidence_record_id="record-test",
            ),
        ),
        eligible_assets=(
            EligibleAsset("asset-front", "front.jpg", "1" * 64, "image-qc"),
            EligibleAsset("asset-rear", "rear.jpg", "2" * 64, "image-qc"),
        ),
    )


def _valid_brief(dossier: ResearchDossier) -> dict[str, Any]:
    return {
        "schema_version": "creative-brief-v2",
        "audience": {"segment": "Người thường xuyên di chuyển", "situation": "Rời bàn làm việc", "desired_outcome": "Cất tai nghe gọn"},
        "consumer_insight": "Thao tác cất gọn giúp rời bàn nhanh hơn.",
        "problem": "Tai nghe dễ bị bỏ quên khi cất lại bất tiện.",
        "creative_angle": "Theo chân thao tác cất tai nghe trước khi rời bàn.",
        "hook": "Bạn thường cất tai nghe trước khi đi thế nào?",
        "promise": "Cho thấy thao tác cất tai nghe vào hộp trong tình huống thực tế.",
        "reason_to_believe": [{"text": "Tai nghe đi kèm hộp sạc", "evidence_refs": ["claim-01"]}],
        "tone": "Đời thường, nhanh và rõ",
        "story_archetype": "day_in_the_life",
        "cta": "Xem thêm thông tin sản phẩm.",
        "restrictions": ["Không thêm thông số ngoài dossier"],
        "target_duration_seconds": 18,
        "prompt_version": "creative-direction-v2",
        "dossier_digest": dossier.digest(),
    }


class _TrustedEvidenceForTest:
    def verify(self, *_args: object, **_kwargs: object) -> bool:
        return True


def _response(content: str) -> dict[str, Any]:
    return {
        "id": "response-test",
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
    }


def test_creative_combo_allows_missing_route_receipt_metadata_only_for_content_validation() -> None:
    dossier = _dossier()
    content = json.dumps(_valid_brief(dossier))
    client = NineRouterOpenAIClient(_route(), transport=lambda _request, _timeout: _response(content))
    model = NineRouterHermesStructuredCreativeModel(client)

    brief = SingleModelCreativeGenerator(model, source_verifier=_TrustedEvidenceForTest()).create_direction(dossier)

    assert brief.dossier_digest == dossier.digest()
    assert brief.provenance.route_receipt["requested_model"] == "Story-Script-Agent"
    assert brief.provenance.route_receipt["upstream_model"] == "unknown"
    assert brief.provenance.route_receipt["upstream_provider"] == "unknown"
    assert brief.provenance.route_receipt["fallback_used"] == "unknown"
    assert brief.provenance.route_receipt["cost_status"] == "unknown"
    assert brief.provenance.route_receipt["response_received"] is True
    assert len(brief.provenance.route_receipt["response_sha256"]) == 64


def test_invalid_creative_content_with_missing_route_metadata_is_not_a_brief_pass() -> None:
    calls = 0

    def transport(_request: object, _timeout: float) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return _response(json.dumps({"creative_angle": "missing required fields"}))

    client = NineRouterOpenAIClient(
        _route(),
        transport=transport,
    )
    generator = SingleModelCreativeGenerator(
        NineRouterHermesStructuredCreativeModel(client),
        source_verifier=_TrustedEvidenceForTest(),
    )

    with pytest.raises(StructuredGenerationError, match="2 invalid structured responses") as raised:
        generator.create_direction(_dossier())

    assert calls == 2  # initial response plus the single allowed repair request
    assert "audience" in str(raised.value)
    assert raised.value.route_receipt["upstream_model"] == "unknown"


@pytest.mark.parametrize(
    ("field", "value", "diagnostic"),
    [
        ("reason_to_believe", [{"text": "unsupported claim", "evidence_refs": ["not-in-dossier"]}], "unknown claims"),
        ("dossier_digest", "sha256:stale", "dossier_digest does not match"),
    ],
)
def test_semantically_invalid_brief_is_not_repaired_before_validation(
    field: str, value: object, diagnostic: str,
) -> None:
    dossier = _dossier()
    payload = _valid_brief(dossier)
    payload[field] = value
    client = NineRouterOpenAIClient(
        _route(), transport=lambda _request, _timeout: _response(json.dumps(payload)),
    )
    generator = SingleModelCreativeGenerator(
        NineRouterHermesStructuredCreativeModel(client),
        source_verifier=_TrustedEvidenceForTest(),
    )

    with pytest.raises(StructuredGenerationError, match=diagnostic):
        generator.create_direction(dossier)


def test_creative_transport_failure_without_response_remains_unknown() -> None:
    client = NineRouterOpenAIClient(_route(), transport=lambda _request, _timeout: (_ for _ in ()).throw(TimeoutError()))

    with pytest.raises(NineRouterSubmissionUncertain) as raised:
        client.complete(messages=[])

    assert raised.value.route_receipt["response_received"] is False
    assert raised.value.route_receipt["upstream_model"] == "unknown"


def test_default_transport_records_response_digest_and_rejects_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Response:
        status = 200

        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return self.body

    monkeypatch.setattr(
        "video_factory.integrations.nine_router.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(b'{"choices":[]}'),
    )
    response = _default_transport(Request("http://127.0.0.1:20128/v1/chat/completions"), 1)
    assert response["__vf_transport_trace"]["http_status"] == 200
    assert len(response["__vf_transport_trace"]["response_sha256"]) == 64

    monkeypatch.setattr(
        "video_factory.integrations.nine_router.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(b"not-json"),
    )
    with pytest.raises(NineRouterHTTPResponseInvalid, match="not valid JSON") as raised:
        _default_transport(Request("http://127.0.0.1:20128/v1/chat/completions"), 1)
    assert raised.value.route_receipt["response_received"] is True
    assert raised.value.route_receipt["validation_error"] == "invalid_json"


def _recovery_db(*, stage: str = "direction") -> tuple[sqlite3.Connection, str, str]:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE workflow (workflow_id TEXT PRIMARY KEY, aggregate_json TEXT NOT NULL, version INTEGER NOT NULL);
        CREATE TABLE processed_command (workflow_id TEXT, command_id TEXT PRIMARY KEY);
        CREATE TABLE workflow_event (event_id TEXT PRIMARY KEY, workflow_id TEXT, event_type TEXT, timestamp TEXT, payload_json TEXT);
        CREATE TABLE workflow_outbox (outbox_id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_id TEXT, message TEXT, created_at TEXT);
        """
    )
    workflow_id = "wf-creative-unknown"
    attempt_id = "vf9r_old-unknown"
    aggregate = {
        "state": "WaitingForOperator",
        "operator_action": "provider_reconciliation",
        "operator_stage": stage,
        "failure_code": "CREATIVE_OUTCOME_UNCERTAIN",
        "operator_reason": (
            '9Router returned an unverified outcome | route_receipt='
            + json.dumps({"attempt_id": attempt_id, "cost_status": "uncertain"})
        ),
        "project_id": "project-test",
        "locked_pack": {"resource_pack_id": "pack-test"},
    }
    WorkflowRepository(connection).commit_workflow(workflow_id, json.dumps(aggregate), [], expected_version=0)
    return connection, workflow_id, attempt_id


def test_recover_creative_requires_explicit_closure_of_unknown_attempt() -> None:
    connection, workflow_id, _attempt_id = _recovery_db()
    with pytest.raises(ValueError, match="must be reconciled or explicitly closed"):
        recover_creative_stage(connection, workflow_id=workflow_id)
    assert connection.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0] == 0


def test_operator_closure_is_audited_before_recover_creative_requeues() -> None:
    from video_factory.application.operator_handler import close_creative_attempt

    connection, workflow_id, attempt_id = _recovery_db(stage="script")

    closed = close_creative_attempt(
        connection,
        workflow_id=workflow_id,
        attempt_id=attempt_id,
        resolution="response_received_unverified",
        evidence_reference="9Router usage history: successful response at 2026-10-21T16:48:12.855Z; output content unavailable",
        note="Close old request as consumed for audit; do not treat its brief as valid.",
    )
    recovered = recover_creative_stage(connection, workflow_id=workflow_id)

    assert closed["status"] == "closed"
    assert recovered["status"] == "recovered"
    assert connection.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0] == 1
    event = connection.execute(
        "SELECT payload_json FROM workflow_event WHERE event_type='CreativeAttemptClosed'"
    ).fetchone()
    assert json.loads(event[0])["resolution"] == "response_received_unverified"


def test_recover_creative_reopens_legacy_invalid_response_skip_with_audit():
    connection, workflow_id, _attempt_id = _recovery_db(stage="script")
    repo = WorkflowRepository(connection)
    aggregate, version = repo.get_workflow(workflow_id)
    reason = "2 invalid structured responses for script-scene-plan-v2: invalid Director JSON"
    reason += " | route_receipt=" + json.dumps({"attempt_id": "vf9r_received",
        "router_usage_id": "usage-1", "cost_status": "recorded"})
    aggregate.update({"state": "skipped", "failure_code": "CREATIVE_RESPONSE_INVALID",
        "failure_category": "PRODUCT_REJECTED", "skip_reason": reason,
        "latest_artifact": {"brief_id": "brief-1"}, "creative_policy": {"creative_mode": "four_agent"}})
    for key in ("operator_action", "operator_stage", "operator_reason"):
        aggregate.pop(key, None)
    repo.commit_workflow(workflow_id, json.dumps(aggregate), [{"event_id": "skip-old",
        "command_id": "skip-old", "event_type": "ProductSkipped", "timestamp": "2026-10-24T00:00:00+00:00",
        "payload": {"code": "CREATIVE_RESPONSE_INVALID"}}], expected_version=version)
    connection.execute("INSERT INTO workflow_event(event_id,workflow_id,event_type,timestamp,payload_json) VALUES(?,?,?,?,?)",
        ("scene-step", workflow_id, "PipelineStep", "2026-10-24T00:00:01+00:00", json.dumps({
            "step_name": "SCENE_PLAN", "status": "pending_operator", "error_code": "CREATIVE_RESPONSE_INVALID"})))

    result = recover_creative_stage(connection, workflow_id=workflow_id, note="Resume bounded script retry")

    assert result["status"] == "recovered" and result["stage"] == "script"
    resumed, _ = repo.get_workflow(workflow_id)
    assert resumed["state"] == "creating_scene_plan"
    assert resumed["legacy_terminal_skip"]["failure_code"] == "CREATIVE_RESPONSE_INVALID"
    assert connection.execute("SELECT COUNT(*) FROM workflow_event WHERE event_type='ProductSkipped'").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0] == 1


def test_operator_can_authorize_new_attempt_while_preserving_old_unknown() -> None:
    from video_factory.application.operator_handler import close_creative_attempt

    connection, workflow_id, attempt_id = _recovery_db(stage="script")

    closed = close_creative_attempt(
        connection,
        workflow_id=workflow_id,
        attempt_id=attempt_id,
        resolution="operator_retry_authorized",
        evidence_reference="Operator instruction: rerun Scene Plan; prior outcome remains UNKNOWN",
        note="Keep prior request and cost UNKNOWN; operator authorizes a new request that may duplicate cost.",
    )
    recovered = recover_creative_stage(connection, workflow_id=workflow_id)

    assert closed["resolution"] == "operator_retry_authorized"
    assert recovered["status"] == "recovered"
    aggregate, _version = WorkflowRepository(connection).get_workflow(workflow_id)
    closure = aggregate["creative_attempt_closures"][attempt_id]
    assert closure["resolution"] == "operator_retry_authorized"
    assert "remains UNKNOWN" in closure["evidence_reference"]
    event = connection.execute(
        "SELECT payload_json FROM workflow_event WHERE event_type='CreativeAttemptClosed'"
    ).fetchone()
    assert json.loads(event[0])["resolution"] == "operator_retry_authorized"


def test_cli_exposes_explicit_creative_attempt_closure() -> None:
    from video_factory.cli import build_parser, cmd_close_creative_attempt

    args = build_parser().parse_args([
        "close-creative-attempt", "wf-creative-unknown",
        "--attempt-id", "vf9r_old-unknown",
        "--resolution", "response_received_unverified",
        "--evidence-reference", "router receipt",
    ])
    assert args.func is cmd_close_creative_attempt
    assert args.resolution == "response_received_unverified"

    retry_args = build_parser().parse_args([
        "close-creative-attempt", "wf-creative-unknown",
        "--attempt-id", "vf9r_old-unknown",
        "--resolution", "operator_retry_authorized",
        "--evidence-reference", "operator instruction",
        "--note", "Prior outcome remains UNKNOWN; duplicate request/cost is possible.",
    ])
    assert retry_args.resolution == "operator_retry_authorized"


def test_operator_retry_requires_explicit_unknown_and_duplicate_cost_note() -> None:
    from video_factory.application.operator_handler import close_creative_attempt

    connection, workflow_id, attempt_id = _recovery_db()
    with pytest.raises(ValueError, match="duplicate request or cost is possible"):
        close_creative_attempt(
            connection,
            workflow_id=workflow_id,
            attempt_id=attempt_id,
            resolution="operator_retry_authorized",
            evidence_reference="operator instruction",
        )


def test_abandoned_unknown_closure_allows_scene_plan_recovery_without_reclassifying_attempt() -> None:
    from video_factory.application.operator_handler import close_creative_attempt

    connection, workflow_id, attempt_id = _recovery_db(stage="script")
    note = "Prior attempt remains UNKNOWN; operator authorizes a new request and possible duplicate cost."
    closed = close_creative_attempt(
        connection,
        workflow_id=workflow_id,
        attempt_id=attempt_id,
        resolution="abandoned_unknown",
        evidence_reference="No router requestDetails row matched the attempt ID",
        note=note,
    )
    recovered = recover_creative_stage(connection, workflow_id=workflow_id)

    assert closed["resolution"] == "abandoned_unknown"
    assert recovered["status"] == "recovered"
    aggregate, _version = WorkflowRepository(connection).get_workflow(workflow_id)
    closure = aggregate["creative_attempt_closures"][attempt_id]
    assert closure["resolution"] == "abandoned_unknown"
    assert "remains UNKNOWN" in closure["note"]


def test_scene_plan_recovery_stops_after_two_retries() -> None:
    from video_factory.application.operator_handler import close_creative_attempt

    connection, workflow_id, attempt_id = _recovery_db(stage="script")
    for index in range(2):
        connection.execute(
            "INSERT INTO workflow_event (event_id,workflow_id,event_type,timestamp,payload_json) VALUES (?,?,?,?,?)",
            (f"evt-recovered-{index}", workflow_id, "CreativeStageRecovered", f"2026-10-22T00:0{index}:00+00:00", json.dumps({"stage": "script"})),
        )
    close_creative_attempt(
        connection,
        workflow_id=workflow_id,
        attempt_id=attempt_id,
        resolution="abandoned_unknown",
        evidence_reference="router trace unavailable",
        note="Prior attempt remains UNKNOWN; duplicate request/cost possible.",
    )

    with pytest.raises(ValueError, match="two Scene Plan retries"):
        recover_creative_stage(connection, workflow_id=workflow_id)


def test_operator_can_override_scene_plan_retry_cap_once_and_audit_it() -> None:
    from video_factory.application.operator_handler import close_creative_attempt

    connection, workflow_id, attempt_id = _recovery_db(stage="script")
    for index in range(2):
        connection.execute(
            "INSERT INTO workflow_event (event_id,workflow_id,event_type,timestamp,payload_json) VALUES (?,?,?,?,?)",
            (f"evt-recovered-{index}", workflow_id, "CreativeStageRecovered", f"2026-10-22T00:0{index}:00+00:00", json.dumps({"stage": "script"})),
        )
    close_creative_attempt(
        connection,
        workflow_id=workflow_id,
        attempt_id=attempt_id,
        resolution="abandoned_unknown",
        evidence_reference="updated Story-Script-Agent combo; prior result remains unknown",
        note="Prior outcome remains UNKNOWN; one additional request and duplicate cost are authorized.",
    )

    recovered = recover_creative_stage(
        connection,
        workflow_id=workflow_id,
        allow_one_extra_retry=True,
        note="Operator requested one additional run after updating the combo members.",
    )

    assert recovered["status"] == "recovered"
    event_payload = json.loads(connection.execute(
        "SELECT payload_json FROM workflow_event WHERE event_type='CreativeStageRecovered' ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()[0])
    assert event_payload["retry_cap_override"] is True


def test_operator_cannot_use_scene_plan_retry_cap_override_twice() -> None:
    from video_factory.application.operator_handler import close_creative_attempt

    connection, workflow_id, attempt_id = _recovery_db(stage="script")
    for index in range(2):
        connection.execute(
            "INSERT INTO workflow_event (event_id,workflow_id,event_type,timestamp,payload_json) VALUES (?,?,?,?,?)",
            (f"evt-recovered-{index}", workflow_id, "CreativeStageRecovered", f"2026-10-22T00:0{index}:00+00:00", json.dumps({"stage": "script"})),
        )
    connection.execute(
        "INSERT INTO workflow_event (event_id,workflow_id,event_type,timestamp,payload_json) VALUES (?,?,?,?,?)",
        ("evt-cap-override", workflow_id, "CreativeStageRecovered", "2026-10-22T00:03:00+00:00", json.dumps({"stage": "script", "retry_cap_override": True})),
    )
    close_creative_attempt(
        connection,
        workflow_id=workflow_id,
        attempt_id=attempt_id,
        resolution="abandoned_unknown",
        evidence_reference="new attempt has no correlated response",
        note="Prior outcome remains UNKNOWN; duplicate cost is possible.",
    )

    with pytest.raises(ValueError, match="allowed only once"):
        recover_creative_stage(
            connection,
            workflow_id=workflow_id,
            allow_one_extra_retry=True,
            note="This override was already used.",
        )


def test_cli_exposes_single_extra_scene_plan_retry_override() -> None:
    from video_factory.cli import build_parser

    args = build_parser().parse_args([
        "recover-creative", "wf-creative-unknown",
        "--allow-one-extra-retry",
        "--note", "Operator authorizes one more attempt after combo update.",
    ])
    assert args.allow_one_extra_retry is True


def test_cli_accepts_abandoned_unknown_resolution() -> None:
    from video_factory.cli import build_parser

    args = build_parser().parse_args([
        "close-creative-attempt", "wf-creative-unknown",
        "--attempt-id", "vf9r_old-unknown",
        "--resolution", "abandoned_unknown",
        "--evidence-reference", "router trace not found",
        "--note", "Prior attempt remains UNKNOWN; retry may duplicate cost.",
    ])
    assert args.resolution == "abandoned_unknown"


def test_http_error_receipt_captures_status_request_id_and_redacted_body() -> None:
    error_body = json.dumps({
        "error": {
            "message": "Authorization Bearer secret-test-value; [413]: {\"error\":{\"message\":\"Request too large; TPM Limit 8000, Requested 8084\",\"type\":\"tokens\",\"code\":\"rate_limit_exceeded\"}}",
            "code": "invalid_request_error",
        },
        "request_id": "body-request-123",
    }).encode()
    http_error = HTTPError(
        "http://127.0.0.1:20128/v1/chat/completions",
        413,
        "Request Entity Too Large",
        {"X-Request-ID": "router-request-456"},
        BytesIO(error_body),
    )
    client = NineRouterOpenAIClient(
        _route(),
        api_key="secret-test-value",
        transport=lambda _request, _timeout: (_ for _ in ()).throw(http_error),
    )

    with pytest.raises(NineRouterSubmissionUncertain) as raised:
        client.complete(messages=[])

    receipt = raised.value.route_receipt
    assert receipt["requested_model"] == "Story-Script-Agent"
    assert receipt["combo_id"] == COMBO_ID
    assert receipt["http_status"] == 413
    assert receipt["error_category"] == "quota_rate_limit"
    assert receipt["response_received"] is True
    assert receipt["router_request_id"] == "router-request-456"
    assert receipt["response_id"] == "body-request-123"
    assert receipt["response_body_sha256"]
    assert receipt["observed_at"]
    assert "secret-test-value" not in receipt["response_body_excerpt"]
    assert receipt["cost_status"] == "uncertain"

