from __future__ import annotations

import os

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from video_factory.integrations.product_intelligence.client import ProductIntelligenceClient
from video_factory.integrations.client import VideoFactoryClient
from video_factory.capabilities.snapshots import CatalogSnapshot
from video_factory.workflows.approvals import ApprovalService
from video_factory.workflows.engine import WorkflowEngine
from video_factory.workflows.envelope import (
    MalformedEnvelopeError,
    OutboxEnvelope,
    build_envelope,
)
from video_factory.job_contracts import (
    JobContext,
    JobDeferred,
    JobDispatchError,
    JobFailed,
    JobSucceeded,
    TerminalJobError,
)
from video_factory.automation.failure_classification import FailureCategory, classify_failure
from video_factory.workflow_state import (
    ProductVideoWorkflowState,
)
from video_factory.qc.validation import QCValidationError, require_current_pass_report
from video_factory.qc.retry_policy import (
    clamp_max_retries,
    ensure_qc_retry_schema,
    get_item_retry_count,
    get_qc_retry_count,
    record_qc_retry,
)

from video_factory.workflows.jobs import JobDispatcher
from video_factory.workflows.repository import WorkflowRepository

from video_factory.handlers.common import (
    _build_exchange_research_result,
    _has_exchange_local_images,
    _is_exchange_source_context,
    _locked_research_dossier,
    _qc_context,
    _qc_report_payload,
)

# Import the ProductVideoJobHandlers class
from video_factory.handlers._base import ProductVideoJobHandlers

logger = logging.getLogger(__name__)


def _run_qc_with_step_log(qc_service: Any, workflow_id: str, stage: Any, *, provider_mode: str, aggregate: dict[str, Any], artifact: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Run one QC evaluation and persist its durable step outcome."""
    import hashlib
    import json
    import time
    from datetime import datetime, timezone
    from video_factory.workflows.steps import record_pipeline_step

    stage_value = str(getattr(stage, "value", stage))
    step_name = {"image": "IMAGE_QC", "keyframe": "KEYFRAME_QC", "video": "VIDEO_QC"}.get(stage_value)
    evaluator = getattr(qc_service, "evaluator", None)
    qc_provider = str(
        getattr(evaluator, "provider_name", None)
        or getattr(evaluator, "evaluator_name", None)
        or type(evaluator).__name__
    )
    started_at = datetime.now(timezone.utc).isoformat()
    started_mono = time.monotonic()
    digest = hashlib.sha256(json.dumps(artifact or {}, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    product_key = str(aggregate.get("product_key") or workflow_id)
    record_pipeline_step(
        qc_service.connection, workflow_id=workflow_id, product_key=product_key,
        step_name=step_name, status="running", input_digest=f"sha256:{digest}",
        provider=qc_provider, requested_model=str(getattr(evaluator, "model", "") or "") or None,
        started_at=started_at, cost_status="unknown" if provider_mode == "configured" else "not_applicable",
    )
    try:
        result = qc_service.run(
            workflow_id,
            stage=stage,
            provider_mode=provider_mode,
            artifact_override=artifact,
            **kwargs,
        )
    except Exception as exc:
        uncertain = bool(getattr(exc, "reconcile_required", False) or getattr(exc, "request_sent", False))
        record_pipeline_step(
            qc_service.connection, workflow_id=workflow_id, product_key=product_key,
            step_name=step_name, status="uncertain" if uncertain else "failed",
            input_digest=f"sha256:{digest}", provider=qc_provider, started_at=started_at,
            duration_ms=int((time.monotonic() - started_mono) * 1000),
            cost_status="unknown" if uncertain else "not_applicable",
            error_code=str(getattr(exc, "code", None) or type(exc).__name__),
            message="QC request outcome requires reconciliation." if uncertain else "QC evaluation failed before a result was recorded.",
            next_action="Reconcile the exact provider attempt before retry." if uncertain else "Review the failure before retrying.",
        )
        raise

    report = result.get("report") if isinstance(result, dict) else None
    verdict = str(getattr(getattr(report, "verdict", None), "value", getattr(report, "verdict", ""))).lower()
    result_status = str(result.get("status") or "unknown") if isinstance(result, dict) else "unknown"
    code = str(result.get("code") or "") if isinstance(result, dict) else ""
    if result_status == "completed" and verdict == "pass":
        status = "completed"
    elif result_status in {"unknown", "technical_unknown"} or "UNCERTAIN" in code.upper():
        status = "uncertain"
    elif result_status == "pending":
        status = "pending_operator"
    else:
        status = "failed"
    findings = getattr(report, "findings", {})
    findings = findings if isinstance(findings, dict) else {}
    cost_usd = getattr(report, "cost_usd", None)
    latest = qc_service.connection.execute(
        "SELECT attempt_id,attempt_number FROM product_video_operation_attempt "
        "WHERE workflow_id=? AND capability_id=? ORDER BY created_at DESC LIMIT 1",
        (workflow_id, f"product_video.qc.{stage_value}"),
    ).fetchone()
    reason = str((result.get("reason") if isinstance(result, dict) else "") or "")
    record_pipeline_step(
        qc_service.connection, workflow_id=workflow_id, product_key=product_key,
        step_name=step_name, status=status,
        attempt_number=int(latest["attempt_number"]) if latest else None,
        operation_id=str(latest["attempt_id"]) if latest else None,
        input_digest=f"sha256:{digest}", provider=qc_provider,
        requested_model=str(getattr(evaluator, "model", "") or "") or None,
        actual_model=str(findings.get("actual_model") or "") or None,
        started_at=started_at, duration_ms=int((time.monotonic() - started_mono) * 1000),
        cost_status=str(findings.get("cost_status") or ("unknown" if provider_mode == "configured" else "not_applicable")),
        cost_usd=float(cost_usd) if cost_usd is not None else None,
        error_code=code or ("QC_SEMANTIC_FAIL" if status == "failed" and verdict == "fail" else None),
        message=reason or (f"QC verdict: {verdict}" if verdict else None),
        next_action=("Reconcile the exact provider attempt before retry." if status == "uncertain" else "Review QC findings and create a new artifact before retry." if status == "failed" else None),
    )
    return result


def _qc_pending_operator_action(result: dict[str, Any]) -> str:
    code = str(result.get("code") or "").upper()
    report = _qc_report_payload(result)
    verdict = str(report.get("verdict") or "").lower()
    if "UNCERTAIN" in code or verdict == "unknown":
        return "qc_reconciliation"
    if code == "QC_INPUT_UNVERIFIABLE":
        return "qc_input_review"
    if str(result.get("status") or "").lower() == "pending":
        return "qc_authorization"
    if "EXHAUSTED" in code or "QUOTA" in code:
        return "qc_retry_exhausted"
    return "qc_provider_retry"


def _pause_for_qc_provider_failure(connection: Any, workflow_id: str, result: dict[str, Any]) -> FailureCategory | None:
    code = str(result.get("code") or "")
    category = classify_failure(code)
    if category in {FailureCategory.QC_INFRA_PAUSED, FailureCategory.CONTRACT_BROKEN}:
        from video_factory.workflows.runtime_control import pause_pipeline

        pause_pipeline(
            connection,
            category=category,
            code=code or "QC_PROVIDER_UNAVAILABLE",
            message=str(result.get("reason") or "QC provider did not return a usable result."),
            workflow_id=workflow_id,
        )
    return category


def _record_qc_pending_blocker(
    connection: Any,
    workflow_id: str,
    aggregate: dict[str, Any],
    stage: QCStage,
    result: dict[str, Any],
) -> FailureCategory | None:
    category = _pause_for_qc_provider_failure(connection, workflow_id, result)
    if category not in {FailureCategory.QC_INFRA_PAUSED, FailureCategory.CONTRACT_BROKEN}:
        return category
    code = str(result.get("code") or "")
    aggregate["failure_category"] = category.value
    aggregate["failure_code"] = code
    aggregate["failure_reason"] = str(result.get("reason") or "QC could not verify its required input contract.")
    if category == FailureCategory.CONTRACT_BROKEN:
        aggregate["state"] = "contract_broken"
        aggregate["operator_action"] = "repair_contract"
    aggregate["operator_stage"] = stage.value
    aggregate["operator_reason"] = aggregate["failure_reason"]
    return category


def _record_keyframe_qc_result(connection: Any, workflow_id: str, artifact: dict[str, Any], result: Any) -> tuple[int | None, str]:
    """Persist each completed keyframe verdict once so automatic recovery survives restarts."""
    if not isinstance(result, dict) or str(result.get("status") or "") != "completed":
        return None, ""
    report = _qc_report_payload(result)
    report_id = str(report.get("report_id") or result.get("report_id") or "").strip()
    verdict = str(report.get("verdict") or "").lower()
    if not report_id or verdict not in {"pass", "fail", "unknown"}:
        return None, verdict

    from video_factory.qc.retry_policy import ensure_qc_retry_schema

    ensure_qc_retry_schema(connection)
    existing = connection.execute(
        "SELECT attempt_number FROM workflow_qc_retries WHERE workflow_id=? "
        "AND retry_type='keyframe_qc' AND qc_report_id=? LIMIT 1",
        (workflow_id, report_id),
    ).fetchone()
    if existing:
        return int(existing[0]), verdict

    attempt_number = get_qc_retry_count(connection, workflow_id, "keyframe_qc") + 1
    findings = report.get("findings") if isinstance(report.get("findings"), dict) else {}
    record_qc_retry(
        connection,
        workflow_id,
        "keyframe_qc",
        item_id="storyboard",
        attempt_number=attempt_number,
        verdict=verdict,
        input_hash=str(artifact.get("package_digest") or ""),
        qc_report_id=report_id,
        cost_usd=report.get("cost_usd"),
        action_taken="keyframe_qc",
        reason_text=str(result.get("reason") or findings.get("reason") or f"Keyframe QC verdict: {verdict}"),
    )
    return attempt_number, verdict


def _prepare_video_qc_scene_retries(
    connection: Any,
    *,
    workflow_id: str,
    render_artifact: dict[str, Any],
    report: dict[str, Any],
    max_retries: int | None,
) -> dict[str, Any]:
    """Persist a valid Video QC failure and plan new clips for only failed scenes."""
    from video_factory.integrations.scene_renderer import SceneRenderOperationStore

    report_id = str(report.get("report_id") or "").strip()
    report_digest = str(report.get("artifact_digest") or "").removeprefix("sha256:")
    render_digest = str(render_artifact.get("sha256") or "").removeprefix("sha256:")
    batch_id = str(render_artifact.get("render_id") or render_artifact.get("job_id") or "").strip()
    if (
        not report_id
        or str(report.get("stage") or "").lower() != "video"
        or str(report.get("verdict") or "").lower() != "fail"
        or not report_digest
        or report_digest != render_digest
        or not batch_id
    ):
        return {"status": "contract_error", "reason": "Video QC failure is not bound to the current render artifact"}

    findings = report.get("findings") if isinstance(report.get("findings"), dict) else {}
    technical = findings.get("technical_specs") if isinstance(findings.get("technical_specs"), dict) else {}
    visual_scenes = findings.get("visual_scenes")
    coverage = findings.get("sample_coverage") if isinstance(findings.get("sample_coverage"), dict) else {}
    if (technical.get("technical_pass") is False and coverage.get("coverage_valid") is True
            and isinstance(visual_scenes, list) and visual_scenes
            and all(isinstance(item, dict) and str(item.get("verdict") or "").lower() == "pass"
                    and type(item.get("score")) in (int, float) and item["score"] >= 80
                    and not item.get("hard_failure")
                    for item in visual_scenes)):
        return {"status": "technical_only", "batch_id": batch_id,
                "scene_ids": [], "operation_ids": [],
                "reason": "Technical QC failed while every covered scene passed content QC"}
    failed_scenes = [
        item for item in (visual_scenes or [])
        if isinstance(item, dict) and str(item.get("verdict") or "").lower() == "fail"
    ]
    if not failed_scenes:
        return {"status": "untargeted", "reason": "Video QC FAIL did not identify a failed scene"}

    max_recoveries = clamp_max_retries(max_retries)
    ensure_qc_retry_schema(connection)
    store = SceneRenderOperationStore(connection)
    targets: list[dict[str, Any]] = []
    for item in failed_scenes:
        try:
            scene_order = int(item.get("scene_number"))
        except (TypeError, ValueError):
            return {"status": "contract_error", "reason": "Video QC failure has no valid scene number"}
        operation = connection.execute(
            "SELECT * FROM video_factory_scene_operation WHERE render_batch_id=? AND scene_order=? "
            "ORDER BY attempt DESC LIMIT 1",
            (batch_id, scene_order),
        ).fetchone()
        if operation is None:
            return {"status": "contract_error", "reason": f"Video QC scene {scene_order} has no durable render operation"}
        operation = dict(operation)
        scene_id = str(operation.get("scene_id") or "").strip()
        if not scene_id:
            return {"status": "contract_error", "reason": f"Video QC scene {scene_order} has no stable scene ID"}
        prior_report = connection.execute(
            "SELECT attempt_number FROM workflow_qc_retries WHERE workflow_id=? "
            "AND retry_type='video_qc' AND item_id=? AND qc_report_id=? LIMIT 1",
            (workflow_id, scene_id, report_id),
        ).fetchone()
        marker = f"Video QC retry: {report_id}"
        existing_retry = connection.execute(
            "SELECT * FROM video_factory_scene_operation WHERE render_batch_id=? AND scene_order=? "
            "AND error_message LIKE ? ORDER BY attempt DESC LIMIT 1",
            (batch_id, scene_order, marker + "%"),
        ).fetchone()
        if prior_report is not None and existing_retry is not None:
            existing_retry = dict(existing_retry)
            if existing_retry.get("status") == "failed":
                return {"status": "contract_error", "reason": f"Video QC retry for {scene_id} already failed"}
            targets.append({
                "scene_id": scene_id,
                "scene_order": scene_order,
                "attempt_number": int(prior_report[0]),
                "operation": operation,
                "existing_retry": existing_retry,
            })
            continue
        if operation.get("status") != "completed" or not operation.get("output_sha256"):
            return {"status": "contract_error", "reason": f"Video QC scene {scene_order} has no completed clip to recover"}
        if prior_report is not None:
            attempt_number = int(prior_report[0])
        else:
            attempt_number = get_item_retry_count(connection, workflow_id, "video_qc", scene_id) + 1
            record_qc_retry(
                connection,
                workflow_id,
                "video_qc",
                item_id=scene_id,
                attempt_number=attempt_number,
                verdict="fail",
                input_hash=render_digest,
                qc_report_id=report_id,
                action_taken="retry_scene" if attempt_number <= max_recoveries else "skip_product",
                reason_text="Valid Video QC rejected this scene against the approved storyboard.",
            )
        targets.append({
            "scene_id": scene_id,
            "scene_order": scene_order,
            "attempt_number": attempt_number,
            "operation": operation,
            "existing_retry": dict(existing_retry) if existing_retry is not None else None,
        })

    exhausted = [target for target in targets if target["attempt_number"] > max_recoveries]
    scene_ids = sorted({target["scene_id"] for target in targets})
    if exhausted:
        return {"status": "exhausted", "scene_ids": scene_ids, "operation_ids": []}

    operation_ids: list[str] = []
    provider_kinds: set[str] = set()
    for target in targets:
        operation = target["operation"]
        if target.get("existing_retry") is not None:
            retry_operation = target["existing_retry"]
            if retry_operation.get("status") == "failed":
                return {"status": "contract_error", "reason": f"Video QC retry for {target['scene_id']} already failed"}
        else:
            retry_operation = store.retry_qc_failed_scene(
                str(operation["operation_id"]),
                qc_report_id=report_id,
                provider_kind=str(operation.get("provider_kind") or "") or None,
                feedback=(
                    "Regenerate only this scene as a new clip. Match the approved script, "
                    "product identity, and QC-approved keyframe; preserve the other scenes."
                ),
            )
        operation_ids.append(str(retry_operation["operation_id"]))
        if retry_operation.get("provider_kind"):
            provider_kinds.add(str(retry_operation["provider_kind"]))

    return {
        "status": "retry",
        "batch_id": batch_id,
        "scene_ids": scene_ids,
        "operation_ids": operation_ids,
        "provider_kind": next(iter(provider_kinds)) if len(provider_kinds) == 1 else None,
    }


def _has_recompose_checkpoint(connection: Any, *, batch_id: str, expected_scene_count: int) -> bool:
    """Require intact completed scene operations before a local-only recompose."""
    try:
        rows = connection.execute(
            "SELECT * FROM video_factory_scene_operation AS op WHERE op.render_batch_id=? "
            "AND op.attempt=(SELECT MAX(other.attempt) FROM video_factory_scene_operation AS other "
            "WHERE other.render_batch_id=op.render_batch_id AND other.scene_id=op.scene_id) "
            "ORDER BY op.scene_order ASC", (batch_id,),
        ).fetchall()
    except Exception:
        return False
    if len(rows) != expected_scene_count:
        return False
    from video_factory.integrations.composer import compute_file_sha256
    for row in rows:
        path = Path(str(row["output_path"] or ""))
        expected_sha = str(row["output_sha256"] or "").removeprefix("sha256:")
        if (row["status"] != "completed" or not expected_sha or not path.is_file()
                or compute_file_sha256(path) != expected_sha):
            return False
    return True


def register_product_video_job_handlers(
    dispatcher: JobDispatcher | Any,
    handlers_service: ProductVideoJobHandlers | None = None,
    workflow_engine: WorkflowEngine | None = None,
    pi_client: ProductIntelligenceClient | None = None,
    video_client: VideoFactoryClient | None = None,
    provider_mode: str = "fake",
) -> ProductVideoJobHandlers:
    """Register all product-to-video pipeline job handlers with the Gate 4 dispatcher/registry."""
    if handlers_service is not None:
        service = handlers_service
        if isinstance(dispatcher, JobDispatcher):
            service.dispatcher = dispatcher
    else:
        service = ProductVideoJobHandlers(
            pi_client=pi_client,
            video_client=video_client,
            workflow_engine=workflow_engine,
            dispatcher=dispatcher if isinstance(dispatcher, JobDispatcher) else None,
            provider_mode=provider_mode,
        )

    if hasattr(dispatcher, "register"):
        for job_name, handler_func in service.get_handlers_map().items():
            dispatcher.register(job_name, handler_func)
    elif isinstance(dispatcher, dict):
        dispatcher.update(service.get_handlers_map())
    return service


def create_product_video_topic_handlers(
    workflow_engine: WorkflowEngine | None = None,
    workflow_repository: WorkflowRepository | None = None,
    handlers_service: ProductVideoJobHandlers | None = None,
    pi_client: ProductIntelligenceClient | None = None,
    video_client: VideoFactoryClient | None = None,
    tool_registry: Any | None = None,
    provider_mode: str = "configured",
    poll_timeout: float = 60.0,
    poll_interval: float = 0.5,
    creative_generator: Any | None = None,
    keyframe_runtime_factory: Callable[[], Any] | None = None,
    qc_service_factory: Callable[[Any], Any] | None = None,
    capability_snapshot: CatalogSnapshot | None = None,
) -> dict[str, Callable[[OutboxEnvelope], None]]:
    """Create production topic handlers for durable outbox delivery.

    Binds topic 'job.dispatch' to ProductVideoJobHandlers.
    """
    if (
        provider_mode == "configured"
        and capability_snapshot is None
        and workflow_repository is not None
    ):
        from video_factory.capabilities.discovery import CapabilityDiscovery
        from video_factory.capabilities.snapshots import CapabilitySnapshotRepository

        snapshot_repository = CapabilitySnapshotRepository(workflow_repository.conn)
        capability_snapshot = snapshot_repository.get_active()
        if capability_snapshot is None and tool_registry is not None and hasattr(
            tool_registry, "get_all_tool_names"
        ):
            # This is composition-root discovery, before any workflow handler
            # runs. Handlers receive only the resulting immutable snapshot and
            # never select an alias from the live registry.
            capability_snapshot = snapshot_repository.save_and_activate(
                CapabilityDiscovery().scan(tool_registry).bindings
            )
    if handlers_service is None:
        if (provider_mode == "configured" or os.environ.get("VF_ENABLE_KEYFRAME_RUNTIME") == "1") and keyframe_runtime_factory is None and workflow_repository is not None:
            from video_factory.integrations.keyframe_provider import (
                KeyframeOperationStore,
                KeyframeRuntime,
            )
            from video_factory.integrations.keyframe_provider_factory import (
                resolve_keyframe_provider,
            )
            from video_factory.integrations.capacity import (
                ProviderCapacityStore, configured_capacity,
            )
            from video_factory.storage.layout import get_workspace_layout

            keyframe_output = get_workspace_layout().root / "keyframes"

            def keyframe_runtime_factory(*, creative_policy=None) -> KeyframeRuntime:
                # Resolved per job so a missing Flow session fails that job
                # (retryable by the operator) instead of blocking gateway start.
                if creative_policy and creative_policy.get('profile',{}).get('image_provider_kind')=='muse':
                    from video_factory.integrations.muse_adapter import configured_muse_adapter
                    adapter=configured_muse_adapter(creative_policy['muse_execution'])
                    provider,cap_kind=resolve_keyframe_provider(keyframe_output,image_provider_kind='muse',
                        creative_mode='four_agent',muse_adapter=adapter)
                else:
                    provider, cap_kind = resolve_keyframe_provider(keyframe_output, provider_mode=provider_mode)
                from video_factory.integrations.capacity import process_lease_owner
                return KeyframeRuntime(
                    store=KeyframeOperationStore(workflow_repository.conn),
                    provider=provider,
                    capacity_store=ProviderCapacityStore(workflow_repository.conn),
                    capacity_limit=configured_capacity(cap_kind),
                    lease_owner=process_lease_owner(),
                )
        if provider_mode == "configured" and creative_generator is None and workflow_repository is not None:
            from video_factory.handlers.common import create_configured_creative_generator
            from video_factory.creative.trusted_evidence import validated_workflow_record_lookup
            creative_generator = create_configured_creative_generator(
                record_lookup=validated_workflow_record_lookup(workflow_repository.conn)
            )
        handlers_service = ProductVideoJobHandlers(
            pi_client=pi_client,
            video_client=video_client,
            workflow_engine=workflow_engine,
            workflow_repository=workflow_repository,
            tool_registry=tool_registry,
            provider_mode=provider_mode,
            poll_timeout=poll_timeout,
            poll_interval=poll_interval,
            creative_generator=creative_generator,
            keyframe_runtime_factory=keyframe_runtime_factory,
            capability_snapshot=capability_snapshot,
            qc_service_factory=qc_service_factory,
        )
    handlers_map = handlers_service.get_handlers_map()

    def _create_qc_service(connection: Any) -> Any:
        if qc_service_factory is not None:
            return qc_service_factory(connection)
        from video_factory.qc.service import ProductVideoQCService

        return ProductVideoQCService(connection)

    def handle_job_dispatch(envelope: OutboxEnvelope, heartbeat_fn: Callable[[], bool] | None = None) -> None:
        payload = envelope.payload
        job_name = payload.get("job_name")
        if not job_name:
            raise MalformedEnvelopeError("Missing 'job_name' in job.dispatch payload")

        if job_name not in handlers_map:
            raise ValueError(f"Unconfigured product_video job handler: {job_name!r}")

        target_wf = envelope.workflow_id
        repo = workflow_repository or (workflow_engine.repo if workflow_engine else None)
        if repo:
            with repo.conn:
                cur = repo.conn.cursor()
                cur.execute(
                    "SELECT command_id FROM processed_command WHERE workflow_id = ? AND command_id = ?",
                    (target_wf, f"cmd_{envelope.message_id}"),
                )
                if cur.fetchone():
                    return

        current_version = None
        if workflow_repository:
            data = workflow_repository.get_workflow(target_wf)
            if data is not None:
                current_version = data[1]
        elif workflow_engine:
            res = workflow_engine.get_workflow_state(target_wf)
            if res is not None:
                current_version = res[1]

        ctx = JobContext(
            workflow_id=target_wf,
            job_id=f"{job_name}.{envelope.message_id}",
            owner_user_id=payload.get("owner_user_id", "local-owner"),
            expected_version=current_version,
            idempotency_key=envelope.idempotency_key,
            heartbeat_fn=heartbeat_fn,
        )

        handler_fn = handlers_map[job_name]
        args = payload.get("args", [])
        kwargs = payload.get("kwargs", {})
        step_by_job = {
            "product_video.lock": "PRODUCT_LOCK",
            "product_video.creative_brief": "CREATIVE_BRIEF",
            "product_video.scene_plan": "SCENE_PLAN",
            "product_video.storyboard": "KEYFRAME_GENERATION",
            "product_video.render": "VIDEO_RENDER",
            "product_video.export": "EXPORT",
        }
        step_name = step_by_job.get(str(job_name))
        step_started_at = datetime.now(timezone.utc).isoformat()
        step_started_mono = time.monotonic()
        step_digest = hashlib.sha256(
            json.dumps({"args": args, "kwargs": kwargs}, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        step_attempt = None
        if repo and step_name:
            from video_factory.workflows.steps import record_pipeline_step

            outbox_row = repo.conn.execute(
                "SELECT attempt_count FROM workflow_outbox WHERE outbox_id=?", (envelope.message_id,)
            ).fetchone()
            step_attempt = int(outbox_row["attempt_count"]) if outbox_row else None
            record_pipeline_step(
                repo.conn, workflow_id=target_wf, product_key=str((repo.get_workflow(target_wf) or ({}, 0))[0].get("product_key") or target_wf),
                step_name=step_name, status="running", attempt_number=step_attempt,
                operation_id=envelope.message_id, input_digest=f"sha256:{step_digest}",
                started_at=step_started_at, cost_status="not_applicable",
            )
        try:
            result = handler_fn(ctx, *args, **kwargs)
        except JobDeferred:
            raise
        except Exception as exc:
            error_code = str(getattr(exc, "code", None) or type(exc).__name__)
            failure_category = classify_failure(f"{error_code}:{exc}")
            if failure_category == FailureCategory.CONTRACT_BROKEN:
                if error_code == "WorkflowTransitionError":
                    error_code = "WORKFLOW_STATE_MISMATCH"
                elif error_code in {"ValueError", "RuntimeError", "AssertionError"}:
                    error_code = "SCENE_PLAN_DIGEST_MISMATCH" if "SCENE" in str(exc).upper() or "PLAN" in str(exc).upper() else "ARTIFACT_DIGEST_MISMATCH"
            if repo and failure_category in {
                FailureCategory.QC_INFRA_PAUSED,
                FailureCategory.CONTRACT_BROKEN,
            }:
                local_qc_response_error = (
                    failure_category == FailureCategory.QC_INFRA_PAUSED
                    and error_code in {"QC_RESPONSE_INVALID", "QC_SCHEMA_INVALID"}
                )
                if not local_qc_response_error:
                    from video_factory.workflows.runtime_control import pause_pipeline

                    pause_pipeline(
                        repo.conn,
                        category=failure_category,
                        code=error_code,
                        message=str(exc),
                        workflow_id=target_wf,
                    )
            if repo and step_name:
                from video_factory.workflows.steps import record_pipeline_step

                uncertain = "UNKNOWN" in error_code.upper() or "UNCERTAIN" in error_code.upper()
                record_pipeline_step(
                    repo.conn, workflow_id=target_wf, product_key=str((repo.get_workflow(target_wf) or ({}, 0))[0].get("product_key") or target_wf),
                    step_name=step_name, status="uncertain" if uncertain else "failed",
                    attempt_number=step_attempt, operation_id=envelope.message_id,
                    input_digest=f"sha256:{step_digest}",
                    started_at=step_started_at, duration_ms=int((time.monotonic() - step_started_mono) * 1000),
                    cost_status="unknown" if uncertain else "not_applicable", error_code=error_code,
                    message=str(exc), next_action="Reconcile provider evidence before any retry." if uncertain else "Inspect the error and retry only when the operation is safe.",
                )
            if repo and failure_category in {
                FailureCategory.QC_INFRA_PAUSED,
                FailureCategory.CONTRACT_BROKEN,
            }:
                local_qc_response_error = (
                    failure_category == FailureCategory.QC_INFRA_PAUSED
                    and error_code in {"QC_RESPONSE_INVALID", "QC_SCHEMA_INVALID"}
                )
                wf_data = repo.get_workflow(target_wf)
                if wf_data:
                    agg, version = wf_data
                    paused_agg = dict(agg)
                    paused_agg["failure_category"] = failure_category.value
                    paused_agg["failure_code"] = error_code
                    paused_agg["failure_reason"] = str(exc)
                    paused_agg["state"] = (
                        ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
                        if failure_category == FailureCategory.QC_INFRA_PAUSED
                        else "contract_broken"
                    )
                    if failure_category == FailureCategory.QC_INFRA_PAUSED:
                        paused_agg["operator_action"] = (
                            "qc_reconciliation"
                            if local_qc_response_error or "UNKNOWN" in error_code.upper() or "UNCERTAIN" in error_code.upper()
                            else "qc_provider_recovery"
                        )
                        paused_agg["operator_stage"] = (
                            "image" if job_name in {"product_video.resource_pack", "product_video.retry_image_qc"}
                            else "keyframe" if job_name == "product_video.storyboard"
                            else "video" if job_name == "product_video.render"
                            else "unknown"
                        )
                    repo.commit_workflow(
                        workflow_id=target_wf,
                        aggregate_json=json.dumps(paused_agg),
                        events=[{
                            "event_id": f"evt_pipeline_pause_{envelope.message_id}",
                            "command_id": f"cmd_pipeline_pause_{envelope.message_id}",
                            "event_type": "WorkflowWaitingForOperator" if local_qc_response_error else "PipelinePaused",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "payload": {"category": failure_category.value, "code": error_code,
                                        "reason": str(exc), "product_local": local_qc_response_error},
                        }],
                        expected_version=version,
                    )
                if local_qc_response_error:
                    return
                raise TerminalJobError(
                    f"{failure_category.value}[{error_code}]: {exc}",
                    code=error_code,
                ) from exc
            raise

        if repo and step_name:
            from video_factory.workflows.steps import record_pipeline_step

            if isinstance(result, JobSucceeded):
                status = "completed"
                code = None
                message = None
                next_action = None
            elif isinstance(result, JobFailed):
                code = str(result.code or "JOB_FAILED")
                status = "uncertain" if "UNKNOWN" in code.upper() or "UNCERTAIN" in code.upper() else (
                    "pending_operator" if not result.retryable else "failed"
                )
                message = result.message
                next_action = "Reconcile provider evidence before retry." if status == "uncertain" else (
                    "Operator review is required." if status == "pending_operator" else "Durable retry may proceed within policy."
                )
            else:
                status, code, message, next_action = "failed", "UNEXPECTED_JOB_RESULT", type(result).__name__, "Inspect handler result."
            record_pipeline_step(
                repo.conn, workflow_id=target_wf, product_key=str((repo.get_workflow(target_wf) or ({}, 0))[0].get("product_key") or target_wf),
                step_name=step_name, status=status, attempt_number=step_attempt,
                operation_id=envelope.message_id, input_digest=f"sha256:{step_digest}",
                provider=(str((result.artifact_ref or {}).get("provider") or (result.artifact_ref or {}).get("provider_name") or "") or None)
                if isinstance(result, JobSucceeded) and isinstance(result.artifact_ref, dict) else None,
                requested_model=(str((result.artifact_ref or {}).get("requested_model") or "") or None)
                if isinstance(result, JobSucceeded) and isinstance(result.artifact_ref, dict) else None,
                actual_model=(str((result.artifact_ref or {}).get("actual_model") or (result.artifact_ref or {}).get("model") or "") or None)
                if isinstance(result, JobSucceeded) and isinstance(result.artifact_ref, dict) else None,
                started_at=step_started_at, duration_ms=int((time.monotonic() - step_started_mono) * 1000),
                cost_status="unknown" if status == "uncertain" else "not_applicable",
                error_code=code, message=message, next_action=next_action,
            )

        if isinstance(result, JobFailed):
            if repo and result.retryable and result.code == "PROVIDER_CAPACITY_WAIT":
                from video_factory.workflows.runtime_control import pause_pipeline

                pause_pipeline(repo.conn, category=FailureCategory.QC_INFRA_PAUSED,
                               code=result.code, message=result.message, workflow_id=target_wf)
                raise JobDeferred(result.message, delay_seconds=15.0)
            if (repo and result.code in {'KEYFRAME_CONTENT_FAIL','STORYBOARD_CONTENT_FAIL','CLIP_CONTENT_FAIL','FINAL_CONTENT_FAIL','FINAL_TECHNICAL_FAIL'} and
                isinstance(result.artifact_ref,dict) and isinstance(result.artifact_ref.get('creative_repair'),dict)):
                wf_data = repo.get_workflow(target_wf)
                agg, version = wf_data
                revised = result.artifact_ref['creative_repair']
                review_digest=result.artifact_ref['review'].get('digest')
                if review_digest in agg.get('creative_applied_repairs',[]):
                    raise TerminalJobError('Same repair failed current recheck; stop without more generation',code='CREATIVE_REPAIR_NO_PROGRESS')
                assembly=bool(revised.get('assembly_repair'))
                next_agg = {**agg,'creative_applied_repairs':[*agg.get('creative_applied_repairs',[]),review_digest],
                    'state':ProductVideoWorkflowState.RENDERING.value if assembly else ProductVideoWorkflowState.CREATING_SCENE_PLAN.value}
                next_kwargs={k:v for k,v in kwargs.items() if k not in {'render_params','creative_revision'}}
                if assembly:
                    next_args=[agg['storyboard']]
                    next_job='product_video.render'
                    next_kwargs['render_params']={'active_job_id':agg['active_render_job_id'],'force_recompose':True}
                else:
                    next_agg.update(scene_plan=revised,latest_artifact=revised)
                    next_agg['creative_prior_render_job_id']=agg.get('active_render_job_id')
                    next_agg.pop('active_render_job_id',None)
                    next_agg.pop('render_dispatch_status',None)
                    next_job='product_video.scene_plan'
                    next_args=[agg['creative_brief']]
                    next_kwargs['creative_revision']=revised
                repair_message = build_envelope(workflow_id=target_wf,topic='job.dispatch',
                    idempotency_key='creative-repair-'+envelope.message_id,
                    payload={'job_name':next_job,'workflow_id':target_wf,
                        'project_id':agg.get('project_id'),'owner_user_id':payload.get('owner_user_id','local-owner'),
                        'args':next_args, 'kwargs':next_kwargs})
                repo.commit_workflow(workflow_id=target_wf,aggregate_json=json.dumps(next_agg),
                    events=[{'event_id':'evt_repair_'+envelope.message_id,'command_id':'cmd_repair_'+envelope.message_id,
                        'event_type':'CreativeRepairPlanned','timestamp':datetime.now(timezone.utc).isoformat(),
                        'payload':result.artifact_ref}],outbox_message=repair_message,expected_version=version)
                return
            failure_category = classify_failure(result.code)
            if repo and failure_category is not None:
                wf_data = repo.get_workflow(target_wf)
                if wf_data:
                    agg, version = wf_data
                    next_agg = dict(agg)
                    next_agg["failure_category"] = failure_category.value
                    next_agg["failure_code"] = result.code
                    next_agg["failure_reason"] = result.message
                    if result.artifact_ref is not None:
                        next_agg["failure_report"] = result.artifact_ref
                    if failure_category == FailureCategory.PRODUCT_REJECTED:
                        next_agg["state"] = "skipped"
                        next_agg["skip_reason"] = result.message
                        next_agg.pop("operator_action", None)
                        event_type = "ProductSkipped"
                    else:
                        local_qc_response_error = (
                            failure_category == FailureCategory.QC_INFRA_PAUSED
                            and result.code in {"QC_RESPONSE_INVALID", "QC_SCHEMA_INVALID"}
                        )
                        if not local_qc_response_error:
                            from video_factory.workflows.runtime_control import pause_pipeline

                            pause_pipeline(
                                repo.conn,
                                category=failure_category,
                                code=result.code,
                                message=result.message,
                                workflow_id=target_wf,
                            )
                        if failure_category == FailureCategory.QC_INFRA_PAUSED:
                            next_agg["state"] = ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
                            if job_name == "product_video.resource_pack" and isinstance(result.artifact_ref, dict):
                                # Keep the newly built, not-yet-approved pack so
                                # governed CLI QC can resume after a route repair.
                                # Persisting it does not create an Image QC PASS.
                                next_agg["resource_pack"] = result.artifact_ref
                                next_agg["latest_artifact"] = result.artifact_ref
                            next_agg["operator_action"] = (
                                "qc_reconciliation"
                                if local_qc_response_error or "UNKNOWN" in result.code.upper() or "UNCERTAIN" in result.code.upper()
                                else "qc_provider_recovery"
                            )
                            next_agg["operator_stage"] = (
                                "image" if job_name in {"product_video.resource_pack", "product_video.retry_image_qc"}
                                else "keyframe" if job_name == "product_video.storyboard"
                                else "video" if job_name == "product_video.render"
                                else "unknown"
                            )
                        else:
                            next_agg["state"] = "contract_broken"
                            next_agg["operator_action"] = "repair_contract"
                        next_agg["operator_reason"] = result.message
                        event_type = "WorkflowWaitingForOperator" if local_qc_response_error else "PipelinePaused"
                    repo.commit_workflow(
                        workflow_id=target_wf,
                        aggregate_json=json.dumps(next_agg),
                        events=[{
                            "event_id": f"evt_fail_{envelope.message_id}",
                            "command_id": f"cmd_fail_{envelope.message_id}",
                            "event_type": event_type,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "payload": {
                                "category": failure_category.value,
                                "code": result.code,
                                "reason": result.message,
                                "product_key": agg.get("product_key"),
                            },
                        }],
                        expected_version=version,
                    )
                    if failure_category == FailureCategory.PRODUCT_REJECTED:
                        from video_factory.automation.repository import DailyImportRepository

                        DailyImportRepository(repo.conn).mark_error_by_workflow_id(
                            target_wf,
                            f"{failure_category.value}:{result.code}:{result.message}",
                        )
                return
            if result.code in (
                "IMAGE_QC_FAILED",
                "EXCHANGE_IMAGE_QC_FAILED",
                "KEYFRAME_QC_FAILED",
                "QC_RETRY_EXHAUSTED",
                "INSUFFICIENT_CANDIDATES",
                "QC_OUTCOME_UNCERTAIN",
                "BUDGET_EXHAUSTED",
                "INSUFFICIENT_BUDGET",
                "QC_AUTHORIZATION_REQUIRED",
                "QC_PROVIDER_UNAVAILABLE",
                "INSUFFICIENT_GROUNDING",
                "EXCHANGE_INPUT_REVIEW_REQUIRED",
                "CREATIVE_OUTCOME_UNCERTAIN",
                "CREATIVE_RESPONSE_INVALID",
            ):
                if repo:
                    wf_data = repo.get_workflow(target_wf)
                    if wf_data:
                        agg, version = wf_data
                        next_agg = dict(agg)
                        next_agg["state"] = ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
                        if job_name == "product_video.resource_pack" and result.artifact_ref is not None:
                            next_agg["resource_pack"] = result.artifact_ref
                            next_agg["latest_artifact"] = result.artifact_ref
                        if result.code == "CREATIVE_OUTCOME_UNCERTAIN":
                            next_agg["operator_action"] = "provider_reconciliation"
                        elif result.code == "CREATIVE_RESPONSE_INVALID":
                            next_agg["operator_action"] = "creative_review"
                        elif "BUDGET" in result.code or "AUTHORIZATION" in result.code:
                            next_agg["operator_action"] = "qc_authorization"
                        else:
                            next_agg["operator_action"] = "qc_retry_authorization"
                        next_agg["operator_stage"] = (
                            "image" if job_name in ("product_video.resource_pack", "product_video.retry_image_qc")
                            else "keyframe" if job_name == "product_video.storyboard"
                            else "direction" if job_name == "product_video.creative_brief"
                            else "script" if job_name == "product_video.scene_plan"
                            else "video"
                        )
                        next_agg["operator_reason"] = result.message
                        next_agg["failure_code"] = result.code
                        repo.commit_workflow(
                            workflow_id=target_wf,
                            aggregate_json=json.dumps(next_agg),
                            events=[
                                {
                                    "event_id": f"evt_fail_{envelope.message_id}",
                                    "command_id": f"cmd_fail_{envelope.message_id}",
                                    "event_type": "WorkflowWaitingForOperator",
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                    "payload": {"code": result.code, "message": result.message},
                                }
                            ],
                            expected_version=version,
                        )
                return
            if not result.retryable:
                raise TerminalJobError(f"Job execution failed [{result.code}]: {result.message}", code=result.code)
            raise JobDispatchError(f"Job execution failed [{result.code}]: {result.message}", code=result.code, retryable=True)

        if isinstance(result, JobSucceeded):
            if repo:
                wf_data = repo.get_workflow(target_wf)
                if wf_data:
                    agg, version = wf_data
                    from video_factory.workflow import transition
                    from video_factory.qc.models import QCStage

                    stage_key_map = {
                        "product_video.research": "research_result",
                        "product_video.resource_pack": "resource_pack",
                        "product_video.lock": "locked_pack",
                        "product_video.creative_brief": "creative_brief",
                        "product_video.scene_plan": "scene_plan",
                        "product_video.storyboard": "storyboard",
                        "product_video.render": "render_result",
                        "product_video.export": "export_result",
                    }
                    next_agg = dict(agg)
                    st_key = stage_key_map.get(job_name)
                    if st_key and result.artifact_ref is not None:
                        next_agg[st_key] = result.artifact_ref
                    if result.artifact_ref is not None:
                        next_agg["latest_artifact"] = result.artifact_ref

                    next_agg.pop("active_render_job_id", None)
                    next_agg.pop("active_export_job_id", None)

                    is_hitl = False
                    gate_name = None
                    outbox_next = None

                    forward_kwargs: dict[str, Any] = {}
                    if agg.get("project_id"):
                        forward_kwargs["project_id"] = agg["project_id"]
                    if agg.get("qc_authorization"):
                        forward_kwargs["qc_authorization"] = agg["qc_authorization"]
                    if agg.get("qc_max_cost_usd") is not None:
                        forward_kwargs["qc_max_cost_usd"] = agg["qc_max_cost_usd"]
                    if agg.get("max_qc_retries") is not None:
                        forward_kwargs["max_qc_retries"] = agg["max_qc_retries"]
                    if agg.get("source_context"):
                        forward_kwargs["source_context"] = agg["source_context"]
                        if agg["source_context"].get("images"):
                            forward_kwargs["images"] = agg["source_context"]["images"]

                    if job_name == "product_video.research":
                        next_validated = transition(ProductVideoWorkflowState.RESEARCHING, ProductVideoWorkflowState.BUILDING_RESOURCE_PACK)
                        next_agg["state"] = next_validated.value
                        outbox_next = build_envelope(
                            workflow_id=target_wf,
                            topic="job.dispatch",
                            idempotency_key=f"idemp_pack_{target_wf}_v{version + 1}",
                            payload={
                                "job_name": "product_video.resource_pack",
                                "workflow_id": target_wf,
                                "args": [result.artifact_ref],
                                "kwargs": forward_kwargs,
                                "owner_user_id": payload.get("owner_user_id", "local-owner"),
                                "project_id": agg.get("project_id"),
                            },
                        )
                    elif job_name == "product_video.resource_pack":
                        if provider_mode in ("configured", "fake"):
                            source_ctx = agg.get("source_context") or {}
                            is_exchange = _is_exchange_source_context(source_ctx)
                            is_auto = bool(source_ctx.get("auto_admitted", False))
                            if is_exchange and not is_auto:
                                from video_factory.handlers.common import _assert_approved_images_in_resource_pack

                                _assert_approved_images_in_resource_pack(
                                    source_context=source_ctx,
                                    resource_pack=next_agg.get("resource_pack") or {},
                                )

                            from video_factory.qc.repository import QCReportRepository
                            from video_factory.qc.models import QCStage
                            pack = next_agg.get("resource_pack") or {}
                            repo_qc = QCReportRepository(repo.conn)
                            has_pass = False
                            try:
                                require_current_pass_report(
                                    repo_qc,
                                    workflow_id=target_wf,
                                    stage=QCStage.IMAGE,
                                    artifact=pack,
                                    owner_user_id=payload.get("owner_user_id"),
                                    project_id=agg.get("project_id"),
                                    provider_mode=provider_mode,
                                )
                                has_pass = True
                            except QCValidationError:
                                has_pass = False

                            low_qc = (next_agg.get("creative_policy") or {}).get("qc_level", "middle") == "low"
                            if low_qc:
                                from video_factory.qc.normalization import resource_pack_fingerprint
                                next_agg["source_qc_receipt"] = {
                                    "stage": "image", "verdict": "not_performed",
                                    "artifact_digest": resource_pack_fingerprint(pack),
                                    "reason": "Source identity review is omitted at low QC; Product Lock remains required.",
                                }
                                next_agg["state"] = transition(
                                    ProductVideoWorkflowState.BUILDING_RESOURCE_PACK,
                                    ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK,
                                ).value
                                is_hitl = True
                                gate_name = "AwaitingProductLock"
                            elif has_pass:
                                next_validated = transition(ProductVideoWorkflowState.BUILDING_RESOURCE_PACK, ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK)
                                next_agg["state"] = next_validated.value
                                is_hitl = True
                                gate_name = "AwaitingProductLock"
                            else:
                                qc_auth = agg.get("qc_authorization") or forward_kwargs.get("qc_authorization")
                                qc_max = agg.get("qc_max_cost_usd") or forward_kwargs.get("qc_max_cost_usd")
                                if qc_auth and isinstance(qc_auth, dict) and "max_cost_usd" in qc_auth and qc_max is None:
                                    qc_max = qc_auth.get("max_cost_usd")
                                repo.commit_workflow(
                                    workflow_id=target_wf,
                                    aggregate_json=json.dumps(next_agg),
                                    events=[],
                                    expected_version=version,
                                )
                                version += 1
                                qc_svc = _create_qc_service(repo.conn)
                                if heartbeat_fn is not None and not heartbeat_fn():
                                    raise RuntimeError("Storyboard lease expired before keyframe QC")
                                qc_res = _run_qc_with_step_log(
                                    qc_svc, target_wf,
                                    stage=QCStage.IMAGE,
                                    provider_mode=provider_mode,
                                    aggregate=next_agg,
                                    artifact=pack,
                                    authorize_cost=bool(qc_auth),
                                    max_cost_usd=float(qc_max) if qc_max is not None else None,
                                    qc_budget_mode=(qc_auth or {}).get("qc_budget_mode") if isinstance(qc_auth, dict) else None,
                                    authorization=qc_auth,
                                )
                                if isinstance(qc_res, dict) and qc_res.get("status") == "pending":
                                    next_agg["state"] = ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
                                    next_agg["operator_action"] = _qc_pending_operator_action(qc_res)
                                    next_agg["operator_stage"] = QCStage.IMAGE.value
                                    next_agg["operator_reason"] = qc_res.get("reason", "Configured QC requires explicit cost authorization")
                                    _record_qc_pending_blocker(repo.conn, target_wf, next_agg, QCStage.IMAGE, qc_res)
                                    is_hitl = False
                                else:
                                    try:
                                        require_current_pass_report(
                                            repo_qc,
                                            workflow_id=target_wf,
                                            stage=QCStage.IMAGE,
                                            artifact=pack,
                                            owner_user_id=payload.get("owner_user_id"),
                                            project_id=agg.get("project_id"),
                                            provider_mode=provider_mode,
                                        )
                                        next_validated = transition(ProductVideoWorkflowState.BUILDING_RESOURCE_PACK, ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK)
                                        next_agg["state"] = next_validated.value
                                        is_hitl = True
                                        gate_name = "AwaitingProductLock"
                                    except QCValidationError as val_err:
                                        next_agg["state"] = ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
                                        next_agg["operator_action"] = "qc_retry_authorization"
                                        next_agg["operator_stage"] = QCStage.IMAGE.value
                                        next_agg["operator_qc_report_id"] = (qc_res.get("report_id") if isinstance(qc_res, dict) else None) or (_qc_report_payload(qc_res).get("report_id") if isinstance(qc_res, dict) else None)
                                        next_agg["operator_reason"] = (qc_res.get("reason") if isinstance(qc_res, dict) else None) or str(val_err)
                                        is_hitl = False
                        else:
                            next_validated = transition(ProductVideoWorkflowState.BUILDING_RESOURCE_PACK, ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK)
                            next_agg["state"] = next_validated.value
                            is_hitl = True
                            gate_name = "AwaitingProductLock"
                    elif job_name == "product_video.retry_image_qc":
                        # Transitions back to AwaitingProductLock if retry passed, or terminal fail
                        next_validated = transition(ProductVideoWorkflowState.RETRYING_IMAGE_QC, ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK)
                        next_agg["state"] = next_validated.value
                        is_hitl = True
                        gate_name = "AwaitingProductLock"
                    elif job_name == "product_video.lock":
                        dossier_data = None
                        if provider_mode in ("configured", "fake"):
                            dossier_data = _locked_research_dossier(
                                workflow_id=target_wf,
                                aggregate=next_agg,
                                locked_pack=result.artifact_ref,
                                connection=repo.conn,
                            )
                        next_agg["state"] = transition(
                            ProductVideoWorkflowState.LOCKING_RESOURCE_PACK,
                            ProductVideoWorkflowState.CREATING_BRIEF,
                        ).value
                        rp_dict = result.artifact_ref.get("resource_pack", result.artifact_ref) if isinstance(result.artifact_ref, dict) else {}
                        product_id = str(rp_dict.get("product_id") or rp_dict.get("resource_pack_id") or rp_dict.get("id") or f"prod_{target_wf}")
                        brief_config: dict[str, Any] = {
                            "resource_pack": result.artifact_ref,
                            "project_id": agg.get("project_id"),
                        }
                        if next_agg["state"] == ProductVideoWorkflowState.CREATING_BRIEF.value:
                            if dossier_data is not None:
                                brief_config["research_dossier"] = dossier_data
                            outbox_next = build_envelope(
                                workflow_id=target_wf,
                                topic="job.dispatch",
                                idempotency_key=f"idemp_brief_{target_wf}_v{version + 1}",
                                payload={
                                    "job_name": "product_video.creative_brief",
                                    "workflow_id": target_wf,
                                    "args": [product_id, brief_config],
                                    "kwargs": forward_kwargs,
                                    "owner_user_id": payload.get("owner_user_id", "local-owner"),
                                    "project_id": agg.get("project_id"),
                                },
                            )
                    elif job_name == "product_video.creative_brief":
                        next_validated = transition(ProductVideoWorkflowState.CREATING_BRIEF, ProductVideoWorkflowState.AWAITING_BRIEF_APPROVAL)
                        next_agg["state"] = next_validated.value
                        is_hitl = True
                        gate_name = "AwaitingBriefApproval"
                    elif job_name == "product_video.scene_plan":
                        next_validated = transition(ProductVideoWorkflowState.CREATING_SCENE_PLAN, ProductVideoWorkflowState.AWAITING_SCENE_APPROVAL)
                        next_agg["state"] = next_validated.value
                        is_hitl = True
                        gate_name = "AwaitingSceneApproval"
                    elif job_name == "product_video.storyboard":
                        if provider_mode == "configured":
                            repo.commit_workflow(
                                workflow_id=target_wf,
                                aggregate_json=json.dumps(next_agg),
                                events=[],
                                expected_version=version,
                            )
                            version += 1
                            qc_svc = _create_qc_service(repo.conn)
                            qc_auth = agg.get("qc_authorization") or forward_kwargs.get("qc_authorization")
                            qc_max = agg.get("qc_max_cost_usd") or forward_kwargs.get("qc_max_cost_usd")
                            if qc_auth and isinstance(qc_auth, dict) and "max_cost_usd" in qc_auth and qc_max is None:
                                qc_max = qc_auth.get("max_cost_usd")
                            low_qc = (agg.get("creative_policy") or {}).get("qc_level", "middle") == "low"
                            if low_qc and (agg.get("creative_policy") or {}).get("disable_keyframe_qc"):
                                qc_res = {"status": "not_performed", "reason": "Keyframe QC disabled by low QC setting."}
                            else:
                                try:
                                    qc_res = _run_qc_with_step_log(
                                        qc_svc, target_wf,
                                        stage=QCStage.KEYFRAME,
                                        provider_mode=provider_mode,
                                        aggregate=agg,
                                        artifact=dict(result.artifact_ref) if isinstance(result.artifact_ref, dict) else {},
                                        authorize_cost=bool(qc_auth),
                                        max_cost_usd=float(qc_max) if qc_max is not None else None,
                                        qc_budget_mode=(qc_auth or {}).get("qc_budget_mode") if isinstance(qc_auth, dict) else None,
                                        authorization=qc_auth,
                                    )
                                except Exception as exc:
                                    if not low_qc:
                                        raise
                                    qc_res = {"status": "unknown", "verdict": "unknown",
                                        "code": str(getattr(exc, "code", None) or type(exc).__name__),
                                        "reason": str(exc)[:500]}
                            expected_artifact = dict(result.artifact_ref)
                            if isinstance(qc_res, dict) and qc_res.get("status") == "not_performed":
                                qc_attempt_number, qc_verdict = None, "not_performed"
                            else:
                                qc_attempt_number, qc_verdict = _record_keyframe_qc_result(
                                    repo.conn, target_wf, expected_artifact, qc_res
                                )
                            if low_qc and qc_verdict != "fail":
                                receipt = _qc_report_payload(qc_res) if isinstance(qc_res, dict) else {}
                                receipt = dict(receipt or (qc_res if isinstance(qc_res, dict) else {}))
                                receipt.setdefault("stage", QCStage.KEYFRAME.value)
                                receipt.setdefault("verdict", qc_verdict or "not_performed")
                                receipt["artifact_digest"] = expected_artifact.get("package_digest")
                                if receipt.get("status") == "pending":
                                    receipt["verdict"] = "not_performed"
                                elif qc_verdict == "unknown" or receipt.get("status") in {"unknown", "technical_unknown"}:
                                    receipt["verdict"] = "unknown"
                                expected_artifact["keyframe_qc_receipt"] = receipt
                                next_agg["state"] = transition(
                                    ProductVideoWorkflowState.CREATING_STORYBOARD,
                                    ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL,
                                ).value
                                next_agg["storyboard"] = expected_artifact
                                next_agg["latest_artifact"] = expected_artifact
                                if receipt.get("verdict") != "pass":
                                    findings = receipt.get("findings") if isinstance(receipt.get("findings"), dict) else {}
                                    items = findings.get("items") if isinstance(findings.get("items"), list) else []
                                    reason = (receipt.get("reason") or findings.get("reason") or
                                        next((item.get("reason") for item in items if isinstance(item, dict) and item.get("reason")), None))
                                    next_agg["keyframe_qc_warning"] = reason or "Keyframe QC did not PASS at low QC level."
                                next_agg.pop("keyframe_recovery_plan", None)
                                next_agg.pop("keyframe_policy_recovery_pending", None)
                                is_hitl = True
                                gate_name = "AwaitingStoryboardApproval"
                                result = JobSucceeded(artifact_ref=expected_artifact, next_command=result.next_command)
                            elif isinstance(qc_res, dict) and qc_res.get("status") == "pending":
                                next_agg["state"] = ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
                                next_agg["operator_action"] = _qc_pending_operator_action(qc_res)
                                next_agg["operator_stage"] = QCStage.KEYFRAME.value
                                next_agg["operator_reason"] = qc_res.get("reason", "Configured QC requires authorization")
                                _record_qc_pending_blocker(repo.conn, target_wf, next_agg, QCStage.KEYFRAME, qc_res)
                                is_hitl = False
                            else:
                                try:
                                    require_current_pass_report(
                                        repo,
                                        workflow_id=target_wf,
                                        stage=QCStage.KEYFRAME,
                                        artifact=expected_artifact,
                                        owner_user_id=agg.get("owner_user_id") or payload.get("owner_user_id"),
                                        project_id=agg.get("project_id"),
                                        provider_mode=provider_mode,
                                    )
                                    next_validated = transition(ProductVideoWorkflowState.CREATING_STORYBOARD, ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL)
                                    next_agg["state"] = next_validated.value
                                    is_hitl = True
                                    gate_name = "AwaitingStoryboardApproval"
                                    rep = _qc_report_payload(qc_res)
                                    expected_artifact["keyframe_qc_receipt"] = rep if rep else qc_res
                                    next_agg["storyboard"] = expected_artifact
                                    next_agg["latest_artifact"] = expected_artifact
                                    next_agg.pop("keyframe_recovery_plan", None)
                                    next_agg.pop("keyframe_policy_recovery_pending", None)
                                    for stale_key in ("failure_category", "failure_code", "failure_report", "failure_reason", "skip_reason", "operator_reason"):
                                        next_agg.pop(stale_key, None)
                                    result = JobSucceeded(artifact_ref=expected_artifact, next_command=result.next_command)
                                except QCValidationError as val_err:
                                    next_agg["operator_stage"] = QCStage.KEYFRAME.value
                                    next_agg["operator_qc_report_id"] = qc_res.get("report_id") or (_qc_report_payload(qc_res).get("report_id"))
                                    failed_report = _qc_report_payload(qc_res)
                                    failed_findings = (
                                        failed_report.get("findings", {})
                                        if isinstance(failed_report, dict)
                                        else {}
                                    )
                                    recovery_plan = (
                                        failed_findings.get("recovery_plan")
                                        if isinstance(failed_findings, dict)
                                        else None
                                    )
                                    recovery_frames = (
                                        recovery_plan.get("frames")
                                        if isinstance(recovery_plan, dict)
                                        else None
                                    )
                                    if isinstance(recovery_plan, dict):
                                        next_agg["keyframe_recovery_plan"] = recovery_plan

                                    if qc_verdict == "unknown":
                                        next_agg["state"] = ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
                                        next_agg["failure_category"] = FailureCategory.QC_INFRA_PAUSED.value
                                        next_agg["failure_code"] = "QC_OUTCOME_UNCERTAIN"
                                        next_agg["operator_action"] = "qc_reconciliation"
                                        next_agg["operator_reason"] = (
                                            "Keyframe QC returned UNKNOWN; reconcile this provider attempt before retrying."
                                        )
                                        from video_factory.workflows.runtime_control import pause_pipeline

                                        pause_pipeline(
                                            repo.conn,
                                            category=FailureCategory.QC_INFRA_PAUSED,
                                            code="QC_OUTCOME_UNCERTAIN",
                                            message=next_agg["operator_reason"],
                                            workflow_id=target_wf,
                                        )
                                    elif agg.get("keyframe_policy_recovery_pending"):
                                        # One Flow operation, one full QC report, then operator review.
                                        next_agg.pop("keyframe_policy_recovery_pending", None)
                                        next_agg["storyboard"] = expected_artifact
                                        next_agg["latest_artifact"] = expected_artifact
                                        next_agg["failure_report"] = failed_report
                                        next_agg["failure_category"] = FailureCategory.PRODUCT_REJECTED.value
                                        recovery_count = int(agg.get("keyframe_policy_recovery_count", 0) or 0)
                                        if recovery_count >= 2:
                                            next_agg["state"] = "skipped"
                                            next_agg["failure_code"] = "KEYFRAME_POLICY_RECOVERY_EXHAUSTED"
                                            next_agg["skip_reason"] = (
                                                f"Keyframe policy recovery {recovery_count}/2 still failed; "
                                                f"QC report {next_agg.get('operator_qc_report_id') or 'not recorded'}."
                                            )
                                            next_agg["operator_reason"] = next_agg["skip_reason"]
                                            from video_factory.automation.repository import DailyImportRepository

                                            DailyImportRepository(repo.conn).mark_error_by_workflow_id(
                                                target_wf,
                                                f"PRODUCT_REJECTED:KEYFRAME_POLICY_RECOVERY_EXHAUSTED:{next_agg['skip_reason']}",
                                            )
                                        else:
                                            next_agg["state"] = ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
                                            next_agg["failure_code"] = "KEYFRAME_POLICY_RECOVERY_REVIEW_REQUIRED"
                                            next_agg["operator_action"] = "keyframe_policy_recovery"
                                            next_agg["operator_reason"] = (
                                                f"Single-frame recovery {recovery_count}/2 completed with semantic FAIL; "
                                                "review the new report and explicitly authorize the next failed frame."
                                            )
                                    else:
                                        try:
                                            max_retries = clamp_max_retries(agg.get("max_qc_retries", 3))
                                        except (TypeError, ValueError):
                                            max_retries = 0
                                        scene_plan = agg.get("scene_plan")
                                        can_regenerate = (
                                            qc_verdict == "fail"
                                            and isinstance(recovery_plan, dict)
                                            and isinstance(recovery_frames, list)
                                            and bool(recovery_frames)
                                            and isinstance(scene_plan, dict)
                                            and qc_attempt_number is not None
                                            and qc_attempt_number <= max_retries
                                        )
                                        if can_regenerate:
                                            recovery_attempt = int(agg.get("keyframe_recovery_attempt", 0) or 0) + 1
                                            next_agg["state"] = ProductVideoWorkflowState.CREATING_STORYBOARD.value
                                            next_agg["keyframe_recovery_attempt"] = recovery_attempt
                                            next_agg["keyframe_recovery_plan"] = recovery_plan
                                            next_agg["storyboard"] = expected_artifact
                                            next_agg["latest_artifact"] = expected_artifact
                                            next_agg.pop("operator_action", None)
                                            next_agg.pop("operator_reason", None)
                                            next_agg.pop("operator_stage", None)
                                            next_agg.pop("operator_qc_report_id", None)
                                            recovery_kwargs = dict(forward_kwargs)
                                            recovery_kwargs.update({
                                                "recovery_plan": recovery_plan,
                                                "recovery_attempt": recovery_attempt,
                                                "previous_storyboard": expected_artifact,
                                            })
                                            outbox_next = build_envelope(
                                                workflow_id=target_wf,
                                                topic="job.dispatch",
                                                idempotency_key=(
                                                    f"idemp_keyframe_qc_recovery_{target_wf}_a{recovery_attempt}_v{version + 1}"
                                                ),
                                                payload={
                                                    "job_name": "product_video.storyboard",
                                                    "workflow_id": target_wf,
                                                    "project_id": agg.get("project_id"),
                                                    "args": args,
                                                    "kwargs": recovery_kwargs,
                                                    "owner_user_id": payload.get("owner_user_id", "local-owner"),
                                                },
                                            )
                                        else:
                                            if qc_verdict == "fail" and qc_attempt_number is not None and qc_attempt_number > max_retries:
                                                next_agg["state"] = "skipped"
                                                next_agg["failure_category"] = FailureCategory.PRODUCT_REJECTED.value
                                                next_agg["failure_code"] = "KEYFRAME_QC_RETRIES_EXHAUSTED"
                                                next_agg["failure_report"] = failed_report
                                                next_agg["skip_reason"] = (
                                                    f"Keyframe QC failed after {qc_attempt_number} evaluations "
                                                    f"(initial plus {max_retries} retries); QC report "
                                                    f"{next_agg.get('operator_qc_report_id') or 'not recorded'}."
                                                )
                                                next_agg["operator_reason"] = next_agg["skip_reason"]
                                                next_agg.pop("operator_action", None)
                                                from video_factory.automation.repository import DailyImportRepository

                                                DailyImportRepository(repo.conn).mark_error_by_workflow_id(
                                                    target_wf,
                                                    f"PRODUCT_REJECTED:KEYFRAME_QC_RETRIES_EXHAUSTED:{next_agg['skip_reason']}",
                                                )
                                            else:
                                                next_agg["state"] = ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
                                                next_agg["operator_action"] = "qc_provider_retry"
                                                next_agg["operator_reason"] = (
                                                    (qc_res.get("reason") if isinstance(qc_res, dict) else None)
                                                    or str(val_err)
                                                    or "Keyframe QC failed without a safe regeneration plan."
                                                )
                                    is_hitl = False
                        else:
                            expected_artifact = dict(result.artifact_ref) if isinstance(result.artifact_ref, dict) else {}
                            pkg_digest = expected_artifact.get("package_digest") or "fake-package-digest"
                            expected_artifact.setdefault("package_digest", pkg_digest)
                            expected_artifact.setdefault("keyframe_qc_receipt", {
                                "stage": "keyframe",
                                "verdict": "pass",
                                "score": 95,
                                "artifact_digest": pkg_digest,
                                "findings": {"automatic": True, "items": [{"verdict": "pass", "score": 95, "hard_failure": False}]},
                            })
                            next_agg["storyboard"] = expected_artifact
                            next_agg["latest_artifact"] = expected_artifact
                            result = JobSucceeded(artifact_ref=expected_artifact, next_command=result.next_command)
                            next_validated = transition(ProductVideoWorkflowState.CREATING_STORYBOARD, ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL)
                            next_agg["state"] = next_validated.value
                            is_hitl = True
                            gate_name = "AwaitingStoryboardApproval"
                    elif job_name == "product_video.render":
                        policy = agg.get("creative_policy") or {}
                        if (provider_mode == "configured" and policy.get("creative_mode") == "four_agent"
                                and policy.get("qc_level") == "low"):
                            from video_factory.creative.agents.video_review import require_final_pass

                            require_final_pass(result.artifact_ref)
                            next_agg["state"] = transition(
                                ProductVideoWorkflowState.RENDERING, ProductVideoWorkflowState.AWAITING_FINAL_APPROVAL,
                            ).value
                            is_hitl = True
                            gate_name = "AwaitingFinalApproval"
                        elif provider_mode == "configured":
                            repo.commit_workflow(
                                workflow_id=target_wf,
                                aggregate_json=json.dumps(next_agg),
                                events=[],
                                expected_version=version,
                            )
                            version += 1
                            qc_svc = _create_qc_service(repo.conn)
                            qc_auth = agg.get("qc_authorization") or forward_kwargs.get("qc_authorization")
                            qc_max = agg.get("qc_max_cost_usd") or forward_kwargs.get("qc_max_cost_usd")
                            if qc_auth and isinstance(qc_auth, dict) and "max_cost_usd" in qc_auth and qc_max is None:
                                qc_max = qc_auth.get("max_cost_usd")
                            qc_res = _run_qc_with_step_log(
                                qc_svc, target_wf,
                                stage=QCStage.VIDEO,
                                provider_mode=provider_mode,
                                aggregate=agg,
                                artifact=dict(result.artifact_ref) if isinstance(result.artifact_ref, dict) else {},
                                authorize_cost=bool(qc_auth),
                                max_cost_usd=float(qc_max) if qc_max is not None else None,
                                qc_budget_mode=(qc_auth or {}).get("qc_budget_mode") if isinstance(qc_auth, dict) else None,
                                authorization=qc_auth,
                            )
                            if isinstance(qc_res, dict) and qc_res.get("status") == "pending":
                                next_agg["state"] = ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
                                next_agg["operator_action"] = _qc_pending_operator_action(qc_res)
                                next_agg["operator_stage"] = QCStage.VIDEO.value
                                next_agg["operator_reason"] = qc_res.get("reason", "Configured QC requires explicit cost authorization")
                                _record_qc_pending_blocker(repo.conn, target_wf, next_agg, QCStage.VIDEO, qc_res)
                                is_hitl = False
                            else:
                                render_art = dict(result.artifact_ref) if isinstance(result.artifact_ref, dict) else {}
                                try:
                                    require_current_pass_report(
                                        repo,
                                        workflow_id=target_wf,
                                        stage=QCStage.VIDEO,
                                        artifact=render_art,
                                        owner_user_id=agg.get("owner_user_id") or payload.get("owner_user_id"),
                                        project_id=agg.get("project_id"),
                                        provider_mode=provider_mode,
                                    )
                                    next_validated = transition(ProductVideoWorkflowState.RENDERING, ProductVideoWorkflowState.AWAITING_FINAL_APPROVAL)
                                    next_agg["state"] = next_validated.value
                                    is_hitl = True
                                    gate_name = "AwaitingFinalApproval"
                                except QCValidationError as val_err:
                                    next_agg["operator_stage"] = QCStage.VIDEO.value
                                    next_agg["operator_qc_report_id"] = qc_res.get("report_id") or (_qc_report_payload(qc_res).get("report_id"))
                                    reason = (qc_res.get("reason") if isinstance(qc_res, dict) else None) or str(val_err)
                                    verdict = str(qc_res.get("verdict") or "").lower() if isinstance(qc_res, dict) else ""
                                    result_status = str(qc_res.get("status") or "") if isinstance(qc_res, dict) else ""
                                    if verdict == "unknown" or result_status in {"unknown", "technical_unknown"}:
                                        error_code = str(qc_res.get("code") or "QC_OUTCOME_UNCERTAIN")
                                        next_agg["state"] = ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
                                        next_agg["failure_category"] = FailureCategory.QC_INFRA_PAUSED.value
                                        next_agg["failure_code"] = error_code
                                        next_agg["operator_action"] = "qc_reconciliation"
                                        next_agg["operator_reason"] = reason
                                        from video_factory.workflows.runtime_control import pause_pipeline

                                        pause_pipeline(
                                            repo.conn,
                                            category=FailureCategory.QC_INFRA_PAUSED,
                                            code=error_code,
                                            message=reason,
                                            workflow_id=target_wf,
                                        )
                                    else:
                                        report_payload = _qc_report_payload(qc_res)
                                        recovery = _prepare_video_qc_scene_retries(
                                            repo.conn,
                                            workflow_id=target_wf,
                                            render_artifact=render_art,
                                            report=report_payload,
                                            max_retries=agg.get("max_qc_retries"),
                                        )
                                        if recovery["status"] == "technical_only":
                                            scene_plan = agg.get("scene_plan")
                                            planned_scenes = scene_plan.get("scenes") if isinstance(scene_plan, dict) else None
                                            if not isinstance(planned_scenes, list) or not planned_scenes:
                                                storyboard = agg.get("storyboard")
                                                planned_scenes = storyboard.get("scenes") if isinstance(storyboard, dict) else None
                                            if (not isinstance(planned_scenes, list) or not planned_scenes
                                                    or not _has_recompose_checkpoint(
                                                        repo.conn, batch_id=str(recovery["batch_id"]),
                                                        expected_scene_count=len(planned_scenes))):
                                                recovery = {
                                                    "status": "technical_recompose_blocked",
                                                    "reason": "Completed scene operation lineage or SHA-verified clip checkpoint is missing; local recompose is unsafe",
                                                }
                                        if recovery["status"] == "technical_only":
                                            report_id = str(report_payload.get("report_id") or "")
                                            saved_technical_attempts = agg.get("video_qc_technical_recompositions", [])
                                            tried = list(saved_technical_attempts) if isinstance(saved_technical_attempts, list) else []
                                            max_recompositions = 1 + clamp_max_retries(agg.get("max_qc_retries"))
                                            if report_id and report_id not in tried and len(tried) < max_recompositions:
                                                tried.append(report_id)
                                                next_agg["video_qc_technical_recompositions"] = tried
                                                recovery["status"] = "recompose"
                                            else:
                                                recovery = {"status": "technical_recompose_blocked",
                                                    "reason": "Technical-only Video QC did not change after local recompose; inspect media/profile contract"}
                                        if recovery["status"] in {"retry", "recompose"}:
                                            next_agg["state"] = ProductVideoWorkflowState.RENDERING.value
                                            next_agg["active_render_job_id"] = recovery["batch_id"]
                                            next_agg["render_dispatch_status"] = (
                                                "QC_TECHNICAL_RECOMPOSE" if recovery["status"] == "recompose"
                                                else "QC_SCENE_RECOVERY")
                                            for key in (
                                                "operator_action",
                                                "operator_stage",
                                                "operator_reason",
                                                "operator_qc_report_id",
                                            ):
                                                next_agg.pop(key, None)
                                            next_agg["video_qc_recovery"] = {
                                                "report_id": report_payload.get("report_id"),
                                                "action": recovery["status"],
                                                "scene_ids": recovery["scene_ids"],
                                                "operation_ids": recovery["operation_ids"],
                                            }
                                            retry_kwargs: dict[str, Any] = {
                                                "owner_user_id": payload.get("owner_user_id", "local-owner"),
                                                "project_id": agg.get("project_id"),
                                                "approval_receipt": kwargs.get("approval_receipt") or agg.get("latest_approval_receipt"),
                                                "qc_authorization": agg.get("qc_authorization") or kwargs.get("qc_authorization"),
                                                "qc_max_cost_usd": agg.get("qc_max_cost_usd"),
                                                "render_params": {"active_job_id": recovery["batch_id"]},
                                            }
                                            if recovery["status"] == "recompose":
                                                retry_kwargs["render_params"]["force_recompose"] = True
                                            if recovery.get("provider_kind"):
                                                retry_kwargs["render_params"]["provider_kind"] = recovery["provider_kind"]
                                            outbox_next = build_envelope(
                                                workflow_id=target_wf,
                                                topic="job.dispatch",
                                                idempotency_key=(
                                                    f"idemp_video_qc_{recovery['status']}_{target_wf}_"
                                                    f"{report_payload.get('report_id')}_v{version + 1}"
                                                ),
                                                payload={
                                                    "job_name": "product_video.render",
                                                    "workflow_id": target_wf,
                                                    "project_id": agg.get("project_id"),
                                                    "args": [agg.get("storyboard")],
                                                    "kwargs": retry_kwargs,
                                                    "owner_user_id": payload.get("owner_user_id", "local-owner"),
                                                },
                                            )
                                            is_hitl = False
                                        elif recovery["status"] == "technical_recompose_blocked":
                                            error_code = "VIDEO_QC_TECHNICAL_RECOMPOSE_NO_PROGRESS"
                                            recovery_reason = str(recovery.get("reason"))
                                            next_agg["state"] = ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
                                            next_agg["failure_category"] = FailureCategory.QC_INFRA_PAUSED.value
                                            next_agg["failure_code"] = error_code
                                            next_agg["operator_action"] = "qc_reconciliation"
                                            next_agg["operator_reason"] = recovery_reason
                                            from video_factory.workflows.runtime_control import pause_pipeline

                                            pause_pipeline(repo.conn, category=FailureCategory.QC_INFRA_PAUSED,
                                                code=error_code, message=recovery_reason, workflow_id=target_wf)
                                            is_hitl = False
                                        elif recovery["status"] == "contract_error":
                                            error_code = "VIDEO_QC_RECOVERY_CONTRACT_BROKEN"
                                            recovery_reason = str(recovery.get("reason") or "Video QC recovery contract is invalid")
                                            next_agg["state"] = ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
                                            next_agg["failure_category"] = FailureCategory.CONTRACT_BROKEN.value
                                            next_agg["failure_code"] = error_code
                                            next_agg["operator_action"] = "contract_reconciliation"
                                            next_agg["operator_reason"] = recovery_reason
                                            from video_factory.workflows.runtime_control import pause_pipeline

                                            pause_pipeline(
                                                repo.conn,
                                                category=FailureCategory.CONTRACT_BROKEN,
                                                code=error_code,
                                                message=recovery_reason,
                                                workflow_id=target_wf,
                                            )
                                        else:
                                            next_agg["state"] = "skipped"
                                            next_agg["failure_category"] = FailureCategory.PRODUCT_REJECTED.value
                                            next_agg["failure_code"] = (
                                                "VIDEO_QC_RETRIES_EXHAUSTED"
                                                if recovery["status"] == "exhausted"
                                                else "VIDEO_QC_UNTARGETED_FAIL"
                                            )
                                            next_agg["failure_report"] = report_payload
                                            next_agg["skip_reason"] = reason
                                            next_agg["operator_reason"] = reason
                                            from video_factory.automation.repository import DailyImportRepository

                                            DailyImportRepository(repo.conn).mark_error_by_workflow_id(
                                                target_wf,
                                                f"PRODUCT_REJECTED:{next_agg['failure_code']}:{reason}",
                                            )
                                    is_hitl = False
                        else:
                            render_art = dict(result.artifact_ref) if isinstance(result.artifact_ref, dict) else {}
                            sha = render_art.get("sha256") or "fake-render-sha"
                            render_art.setdefault("sha256", sha)
                            render_art.setdefault("technical_qc", {"sha256": sha, "verdict": "PASS"})
                            next_agg["render_result"] = render_art
                            next_agg["latest_artifact"] = render_art
                            result = JobSucceeded(artifact_ref=render_art, next_command=result.next_command)
                            next_validated = transition(ProductVideoWorkflowState.RENDERING, ProductVideoWorkflowState.AWAITING_FINAL_APPROVAL)
                            next_agg["state"] = next_validated.value
                            is_hitl = True
                            gate_name = "AwaitingFinalApproval"

                            try:
                                from video_factory.qc.models import QCReport, QCStage, QCVerdict
                                from video_factory.qc.repository import QCReportRepository
                                from video_factory.qc.fingerprint import VIDEO_POLICY_VERSION
                                scene_count = len((next_agg.get("scene_plan") or {}).get("scenes") or []) or 4
                                samples_per_scene = 5
                                total_samples = scene_count * samples_per_scene
                                findings = {
                                    "automatic": True,
                                    "technical_specs": {"technical_pass": True},
                                    "sample_coverage": {
                                        "coverage_valid": True,
                                        "expected_samples": total_samples,
                                        "samples_per_scene": samples_per_scene,
                                        "total_samples": total_samples,
                                    },
                                    "visual_scenes": [
                                        {"verdict": "pass", "score": 95, "hard_failure": False, "samples_extracted": samples_per_scene}
                                        for _ in range(scene_count)
                                    ],
                                }
                                v_report = QCReport(
                                    report_id=f"fake-video-{sha[-12:]}",
                                    workflow_id=target_wf,
                                    stage=QCStage.VIDEO,
                                    artifact_id=render_art.get("render_id") or render_art.get("artifact_id") or f"render_{target_wf}",
                                    artifact_version=render_art.get("version", 1),
                                    artifact_digest=sha,
                                    reference_digests=[],
                                    evaluator_name="offline_fake",
                                    evaluator_version="1",
                                    rubric_version="1",
                                    created_at=datetime.now(timezone.utc).isoformat(),
                                    score=95,
                                    verdict=QCVerdict.PASS,
                                    findings=findings,
                                    provider_mode=provider_mode,
                                    owner_user_id=payload.get("owner_user_id", "local-owner"),
                                    project_id=agg.get("project_id") or f"proj_{target_wf}",
                                    policy_version=VIDEO_POLICY_VERSION,
                                )
                                QCReportRepository(repo.conn).save_report(v_report)
                            except Exception:
                                pass
                    elif job_name == "product_video.export":
                        next_validated = transition(ProductVideoWorkflowState.EXPORTING, ProductVideoWorkflowState.COMPLETED)
                        next_agg["state"] = next_validated.value
                    else:
                        raise ValueError(f"Unknown job_name in handle_job_dispatch: {job_name}")

                    with repo.conn:
                        cur = repo.conn.cursor()
                        if is_hitl and gate_name:
                            approval_svc = ApprovalService(repo.conn)
                            proj_id = agg.get("project_id") or payload.get("project_id") or f"proj_{target_wf}"
                            art_dict = result.artifact_ref if isinstance(result.artifact_ref, dict) else {}
                            art_id = (
                                art_dict.get("artifact_id")
                                or art_dict.get("brief_id")
                                or art_dict.get("scene_plan_id")
                                or art_dict.get("storyboard_id")
                                or art_dict.get("render_id")
                                or art_dict.get("export_id")
                                or art_dict.get("resource_pack_id")
                                or art_dict.get("id")
                            )
                            art_ver = art_dict.get("version") or art_dict.get("resource_pack_version")
                            approval_payload = {
                                "workflow_id": target_wf,
                                "project_id": proj_id,
                                "gate": gate_name,
                                "workflow_version": version + 1,
                                "artifact_ref": result.artifact_ref,
                                "owner_user_id": payload.get("owner_user_id", "local-owner"),
                            }
                            if art_id is not None:
                                approval_payload["artifact_id"] = art_id
                            if art_ver is not None:
                                approval_payload["artifact_version"] = art_ver
                            req_id = approval_svc.submit_request(
                                approval_payload,
                                cursor=cur,
                            )
                            next_agg["pending_approval_gate"] = gate_name
                            next_agg["pending_approval_id"] = req_id
                        else:
                            next_agg.pop("pending_approval_gate", None)
                            next_agg.pop("pending_approval_id", None)

                        repo.commit_workflow(
                            workflow_id=target_wf,
                            aggregate_json=json.dumps(next_agg),
                            events=[
                                {
                                    "event_id": f"evt_{envelope.message_id}",
                                    "command_id": f"cmd_{envelope.message_id}",
                                    "event_type": (
                                        "ProductSkipped"
                                        if next_agg.get("failure_category") == FailureCategory.PRODUCT_REJECTED.value
                                        else "JobSucceeded"
                                    ),
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                    "payload": {
                                        "job_name": job_name,
                                        "artifact_ref": result.artifact_ref,
                                        "failure_category": next_agg.get("failure_category"),
                                        "reason": next_agg.get("skip_reason"),
                                        "product_key": next_agg.get("product_key"),
                                    },
                                }
                            ],
                            outbox_message=outbox_next,
                            expected_version=version,
                        )

    return {"job.dispatch": handle_job_dispatch}


def start_product_video_workflow(
    workflow_repository: WorkflowRepository,
    workflow_id: str,
    product_name: str,
    keywords: list[str],
    pdp_url: str | None = None,
    owner_user_id: str = "local-owner",
    idempotency_key: str | None = None,
    project_id: str | None = None,
    source_context: dict[str, Any] | None = None,
    qc_authorization: dict[str, Any] | None = None,
    qc_max_cost_usd: float | None = None,
    research_snapshot_id: str | None = None,
    max_qc_retries: int | None = None,
    execution_mode: str = "manual",
    creative_policy: dict[str, Any] | None = None,
) -> str:
    """Production entrypoint to create and enqueue a durable Product-to-Video workflow.

    Commits aggregate state 'researching', appends 'WorkflowStarted' event,
    and enqueues the 'job.dispatch' OutboxEnvelope for product_video.research.
    """
    if not project_id or not str(project_id).strip():
        raise ValueError("project_id is required")
    if execution_mode not in ("auto", "manual"):
        raise ValueError("execution_mode must be 'auto' or 'manual'")
    if _is_exchange_source_context(source_context):
        approved_images = source_context.get("images")
        distinct_ids: set[str] = set()
        distinct_paths: set[str] = set()
        if isinstance(approved_images, list):
            for image in approved_images:
                if not isinstance(image, dict):
                    continue
                asset_id = str(image.get("asset_id") or "").strip()
                path_value = image.get("absolute_path") or image.get("local_path") or image.get("path")
                if asset_id:
                    distinct_ids.add(asset_id)
                if path_value:
                    try:
                        distinct_paths.add(str(Path(str(path_value)).expanduser().resolve()))
                    except OSError:
                        distinct_paths.add(str(path_value))
        if (
            not isinstance(approved_images, list)
            or not approved_images
            or not distinct_ids
            or not distinct_paths
            or len(distinct_ids) != len(approved_images)
            or len(distinct_paths) != len(approved_images)
        ):
            raise TerminalJobError(
                "EXCHANGE_INPUT_REVIEW_REQUIRED: Exchange workflow needs one distinct approved image before it can start",
                code="EXCHANGE_INPUT_REVIEW_REQUIRED",
            )
    effective_project_id = str(project_id).strip()
    idemp = idempotency_key or f"idemp_res_{workflow_id}"
    effective_kwargs: dict[str, Any] = {"project_id": effective_project_id}
    if source_context:
        effective_kwargs["source_context"] = source_context
        for k in ("canonical_product_key", "brand", "model", "variant", "images", "source_product_id"):
            if k in source_context and source_context[k] is not None:
                effective_kwargs[k] = source_context[k]
    effective_kwargs.update(_qc_context({
        "qc_authorization": qc_authorization,
        "qc_max_cost_usd": qc_max_cost_usd,
    }, workflow_id=workflow_id))
    if research_snapshot_id:
        effective_kwargs["research_snapshot_id"] = str(research_snapshot_id)
    if max_qc_retries is not None:
        effective_kwargs["max_qc_retries"] = max_qc_retries

    use_exchange_direct_pack = _has_exchange_local_images(source_context, research_snapshot_id)
    if use_exchange_direct_pack and isinstance(source_context, dict):
        exchange_research = _build_exchange_research_result(
            workflow_id=workflow_id,
            product_name=product_name,
            keywords=keywords,
            pdp_url=pdp_url,
            source_context=source_context,
        )
        initial_state = ProductVideoWorkflowState.BUILDING_RESOURCE_PACK.value
        initial_job_name = "product_video.resource_pack"
        initial_args: list[Any] = [exchange_research]
    else:
        exchange_research = None
        initial_state = ProductVideoWorkflowState.RESEARCHING.value
        initial_job_name = "product_video.research"
        initial_args = [product_name, keywords, pdp_url]

    outbox_msg = build_envelope(
        workflow_id=workflow_id,
        topic="job.dispatch",
        idempotency_key=idemp,
        payload={
            "job_name": initial_job_name,
            "workflow_id": workflow_id,
            "project_id": effective_project_id,
            "args": initial_args,
            "owner_user_id": owner_user_id,
            "kwargs": effective_kwargs,
        },
    )
    aggregate: dict[str, Any] = {
        "state": initial_state,
        "execution_mode": execution_mode,
        "project_id": effective_project_id,
        "owner_user_id": owner_user_id,
        "pdp_url": pdp_url,
    }
    if creative_policy is not None:
        aggregate.update(creative_policy=creative_policy,
            creative_input_digest=creative_policy['creative_input_digest'],creative_query=creative_policy['query'])
    if exchange_research is not None:
        aggregate["research_result"] = exchange_research
        aggregate["latest_artifact"] = exchange_research
    if source_context is not None:
        aggregate["source_context"] = source_context
        aggregate["product_key"] = source_context.get("product_key")
    aggregate.update(_qc_context({
        "qc_authorization": qc_authorization,
        "qc_max_cost_usd": qc_max_cost_usd,
    }, workflow_id=workflow_id))
    if research_snapshot_id:
        aggregate["research_snapshot_id"] = str(research_snapshot_id)
    if max_qc_retries is not None:
        aggregate["max_qc_retries"] = max_qc_retries

    workflow_repository.commit_workflow(
        workflow_id=workflow_id,
        aggregate_json=json.dumps(aggregate),
        events=[
            {
                "event_id": f"evt_start_{workflow_id}",
                "command_id": f"cmd_start_{workflow_id}",
                "event_type": "WorkflowStarted",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "payload": {"product_name": product_name, "keywords": keywords, "pdp_url": pdp_url, "project_id": effective_project_id},
            }
        ],
        outbox_message=outbox_msg,
        expected_version=0,
    )
    return workflow_id
