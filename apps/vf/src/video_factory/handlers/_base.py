from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from video_factory.integrations.product_intelligence.client import ProductIntelligenceClient
from video_factory.integrations.client import VideoFactoryClient
from video_factory.capabilities.snapshots import CatalogSnapshot
import video_factory.handlers as handlers_pkg

def reserve_budget(*args: Any, **kwargs: Any) -> Any:
    return handlers_pkg.reserve_budget(*args, **kwargs)

def commit_budget(*args: Any, **kwargs: Any) -> Any:
    return handlers_pkg.commit_budget(*args, **kwargs)

def release_budget(*args: Any, **kwargs: Any) -> Any:
    return handlers_pkg.release_budget(*args, **kwargs)

def get_reservation(*args: Any, **kwargs: Any) -> Any:
    return handlers_pkg.get_reservation(*args, **kwargs)


def _creative_failure(exc: Exception, allocation: Any, retry_code: str) -> JobFailed:
    receipt = getattr(exc, "route_receipt", None)
    request_sent = bool(
        getattr(exc, "request_sent", False)
        or getattr(exc, "reconcile_required", False)
        or receipt
    )
    if allocation:
        try:
            (commit_budget if request_sent else release_budget)(allocation)
        except Exception as budget_exc:
            if request_sent:
                receipt = dict(receipt) if isinstance(receipt, dict) else {}
                receipt["budget_reconciliation_error"] = type(budget_exc).__name__
    if not request_sent:
        return JobFailed(retryable=bool(getattr(exc, "retryable", True)),
                         code=str(getattr(exc, "code", retry_code) or retry_code), message=str(exc))

    uncertain = bool(getattr(exc, "reconcile_required", False))
    code = "CREATIVE_OUTCOME_UNCERTAIN" if uncertain else "CREATIVE_RESPONSE_INVALID"
    message = str(exc)
    if isinstance(receipt, dict) and "route_receipt=" not in message:
        message += " | route_receipt=" + json.dumps(receipt, sort_keys=True, ensure_ascii=True)
    return JobFailed(retryable=False, code=code, message=message)

from video_factory.workflows.engine import WorkflowEngine
from video_factory.qc.models import QCStage
from video_factory.qc.repository import QCReportRepository
from video_factory.job_contracts import (
    ExecutionBindingPreflightError,
    JobContext,
    JobDeferred,
    JobFailed,
    JobSucceeded,
    LeaseLostError,
    TerminalJobError,
)
from video_factory.workflow_state import (
    HITL_WAITING_STATES,
    ProductVideoWorkflowState,
)
from video_factory.provider_runtime import (
    validate_polling_setting,
    validate_provider_mode,
)
from video_factory.mcp_adapters import (
    MCPProductIntelligenceAdapter,
    MCPVideoFactoryAdapter,
    _get_global_tool_registry,
    _resolve_snapshot_execution_wire,
)

from video_factory.workflows.jobs import JobDispatcher, JobResult, JobSpec, JobStatus
from video_factory.workflows.repository import WorkflowRepository

# Import helper functions from common module (used by merged methods)
from video_factory.handlers.common import (
    _assert_approved_images_in_resource_pack,
    _build_exchange_research_result,
    _build_exchange_resource_pack,
    _has_exchange_local_images,
    _is_exchange_source_context,
    _is_exchange_payload,
    _lock_exchange_resource_pack,
    _paid_budget_kwargs,
    _video_factory_brief_config,
    verify_job_digests,
    _recover_creative_brief,
    _require_configured_execution_binding,
    _resolve_durable_resume_job_id,
    _confirm_vf_storyboard_write,
    _locked_research_dossier,
    _resolve_image_bytes_hash,
    _paid_render_send_count,
    _is_qc_pass,
)

from video_factory.schemas import VideoWorkflowAggregate

logger = logging.getLogger(__name__)


class ProductVideoJobHandlers:
    """Deterministic product-to-video job handlers with injected dependencies and workflow state loading."""

    def __init__(
        self,
        pi_client: ProductIntelligenceClient | None = None,
        video_client: VideoFactoryClient | None = None,
        workflow_engine: WorkflowEngine | None = None,
        workflow_repository: WorkflowRepository | None = None,
        dispatcher: JobDispatcher | None = None,
        tool_registry: Any | None = None,
        provider_mode: str = "fake",
        poll_timeout: float = 60.0,
        poll_interval: float = 0.5,
        creative_generator: Any | None = None,
        keyframe_runtime_factory: Callable[[], Any] | None = None,
        capability_snapshot: CatalogSnapshot | None = None,
        evaluator: Any | None = None,
        qc_service_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self.provider_mode = validate_provider_mode(provider_mode)
        self.evaluator = evaluator
        self.qc_service_factory = qc_service_factory
        self.tool_registry = tool_registry
        self.poll_timeout = validate_polling_setting("poll_timeout", poll_timeout, 60.0)
        self.poll_interval = validate_polling_setting("poll_interval", poll_interval, 0.5)
        self.creative_generator = creative_generator
        self.keyframe_runtime_factory = keyframe_runtime_factory
        self.capability_snapshot = capability_snapshot

        if self.provider_mode == "configured":
            if pi_client is None:
                pi_client = self._resolve_pi_adapter(tool_registry)
            if video_client is None:
                video_client = self._resolve_video_adapter(tool_registry)

            self._missing_pi_provider = pi_client is None
            self._missing_video_provider = video_client is None
            self._missing_live_provider = (
                self._missing_pi_provider or self._missing_video_provider
            )
            if self._missing_live_provider:
                missing = []
                if pi_client is None:
                    missing.append("ProductIntelligence active capability binding")
                if video_client is None:
                    missing.append("VideoFactory active capability binding (canonical mcp__video_factory__render required)")
                self._missing_provider_details = ", ".join(missing)
            else:
                self._missing_provider_details = None
        else:
            self._missing_pi_provider = False
            self._missing_video_provider = False
            self._missing_live_provider = False
            self._missing_provider_details = None

        self.pi_client = pi_client or ProductIntelligenceClient()
        self.video_client = video_client or VideoFactoryClient()
        self.workflow_engine = workflow_engine
        self.workflow_repository = workflow_repository or (workflow_engine.repo if workflow_engine else None)
        self.dispatcher = dispatcher
        self._idempotency_cache: dict[str, JobSucceeded] = {}

        if self.creative_generator is None and self.provider_mode == "configured":
            from video_factory.handlers.common import create_configured_creative_generator
            lookup = None
            if self.workflow_repository is not None and getattr(self.workflow_repository, "conn", None) is not None:
                from video_factory.creative.trusted_evidence import validated_workflow_record_lookup
                lookup = validated_workflow_record_lookup(self.workflow_repository.conn)
            self.creative_generator = create_configured_creative_generator(record_lookup=lookup)

    def _load_agg(self, workflow_id: str) -> tuple[VideoWorkflowAggregate, int]:
        """Load workflow aggregate as Pydantic model and version."""
        data = self.workflow_repository.get_workflow(workflow_id)
        if data is None:
            raise KeyError(f"Workflow {workflow_id} not found")
        agg_dict, version = data
        # The persisted aggregate is keyed by workflow_id in its own column and
        # does not repeat it inline, so seed it before validating.
        return VideoWorkflowAggregate.model_validate(
            {"workflow_id": workflow_id, **agg_dict}
        ), version

    def _save_agg(self, agg: VideoWorkflowAggregate, version: int) -> None:
        """Save Pydantic model to workflow repository."""
        self.workflow_repository.commit_workflow(
            workflow_id=agg.workflow_id,
            aggregate_json=agg.model_dump_json(),
            expected_version=version,
        )

    def _resolve_pi_adapter(self, registry: Any | None) -> Any | None:
        effective_registry = registry or _get_global_tool_registry()
        if effective_registry is None or self.capability_snapshot is None:
            return None
        capability_keys = {
            "research": "product.research",
            "build_resource_pack": "product.resource_pack.build",
            "lock_resource_pack": "product.resource_pack.lock",
        }
        resolved = {
            key: _resolve_snapshot_execution_wire(self.capability_snapshot, capability_id)
            for key, capability_id in capability_keys.items()
        }
        return MCPProductIntelligenceAdapter(
            effective_registry,
            {
                key: wire for key, (wire, _reason) in resolved.items() if wire
            } | {"get_research": "mcp__product_intelligence__get_product_research"},
            {key: reason for key, (_wire, reason) in resolved.items() if reason},
        )

    def _resolve_video_adapter(self, registry: Any | None) -> Any | None:
        effective_registry = registry or _get_global_tool_registry()
        if effective_registry is None or self.capability_snapshot is None:
            return None
        capability_keys = {
            "brief": "video.brief.create",
            "scene_plan": "video.scene_plan.create",
            "storyboard": "video.storyboard.create",
            "render": "video.render",
            "export": "video.export",
        }
        resolved = {
            key: _resolve_snapshot_execution_wire(self.capability_snapshot, capability_id)
            for key, capability_id in capability_keys.items()
        }
        # Execution-stage wires come only from the active snapshot. The
        # remaining static names are non-capability factory maintenance/status
        # calls and are never selected by alias preference.
        wire_map = {
            key: wire for key, (wire, _reason) in resolved.items() if wire
        }
        render_wire = wire_map.get("render")
        if render_wire is not None and render_wire != "mcp__video_factory__render":
            wire_map.pop("render")
        wire_map.update(
            {
                "project_create": "mcp__video_factory__video_project_create",
                "resource_pack_save": "mcp__video_factory__resource_pack_save",
                "resource_pack_lock": "mcp__video_factory__resource_pack_lock",
                "creative_brief_get": "mcp__video_factory__creative_brief_get",
                "brief_approve": "mcp__video_factory__creative_brief_approve",
                "scene_plan_approve": "mcp__video_factory__scene_plan_approve",
                "storyboard_approve": "mcp__video_factory__storyboard_approve",
                "final_video_approve": "mcp__video_factory__final_video_approve",
                "get_job_status": "mcp__video_factory__get_job_status",
            }
        )
        return MCPVideoFactoryAdapter(
            effective_registry,
            wire_map,
            poll_timeout=self.poll_timeout,
            poll_interval=self.poll_interval,
            binding_reasons={key: reason for key, (_wire, reason) in resolved.items() if reason},
        )

    def _get_workflow_state_and_version(self, ctx: JobContext) -> tuple[str, int] | None:
        """Load state and version through public WorkflowEngine or WorkflowRepository methods."""
        if self.workflow_repository:
            data = self.workflow_repository.get_workflow(ctx.workflow_id)
            if data is not None:
                agg, ver = data
                return agg.get("state", "initial"), ver
        if self.workflow_engine:
            return self.workflow_engine.get_workflow_state(ctx.workflow_id)
        return None




    # Methods from qc_handler.py
    def retry_image_qc_job(self, ctx: JobContext, resource_pack: dict, **kwargs: Any) -> JobSucceeded | JobFailed:
        """Auto-retry Image QC with image cleaning for recoverable failures."""
        from video_factory.qc.auto_retry_helpers import (
            extract_artifacts_from_qc,
            is_cleanable_failure,
            replace_images_in_resource_pack,
        )
        
        # 1. Validate state
        err = self._validate_state_and_version(
            ctx,
            {ProductVideoWorkflowState.RETRYING_IMAGE_QC, ProductVideoWorkflowState.BUILDING_RESOURCE_PACK},
            {"resource_pack": resource_pack},
        )
        if err:
            return err
        workflow = self.workflow_repository.get_workflow(ctx.workflow_id) if self.workflow_repository else None
        source_context = workflow[0].get("source_context") if workflow else None
        if _is_exchange_source_context(source_context):
            return JobFailed(
                retryable=False,
                code="QC_AUTHORIZATION_REQUIRED",
                message=(
                    "Exchange image retry requires governed Image QC and operator-approved "
                    "source images; legacy image cleaning cannot establish that admission"
                ),
            )
        
        # 2. Extract original QC report
        initial_qc = resource_pack.get("initial_qc_report")
        if not initial_qc or initial_qc.get("verdict") != "FAIL":
            return JobFailed(
                retryable=False,
                code="INVALID_STATE",
                message="retry_image_qc_job requires a FAIL QC report"
            )
        initial_findings = initial_qc.get("findings") or {}
        if initial_findings.get("usage_status") == "uncertain" or initial_findings.get("reconcile_required"):
            return JobFailed(
                retryable=False,
                code="QC_OUTCOME_UNCERTAIN",
                message="Image QC request may have reached the provider; reconcile usage before retrying",
            )
        
        # 3. Check retry count (one initial evaluation plus at most three retries)
        retry_count = resource_pack.get("image_qc_retry_count", 0)
        if retry_count >= 3:
            return JobFailed(
                retryable=False,
                code="IMAGE_QC_RETRIES_EXHAUSTED",
                message="Image QC already retried three times (4 evaluations total); manual review required"
            )
        
        # 4. Check if failure is cleanable
        if not is_cleanable_failure(initial_qc):
            return JobFailed(
                retryable=False,
                code="IMAGE_QC_NON_RECOVERABLE_FAIL",
                message="Image QC failure is terminal; manual review required"
            )
        
        # 5. Run Image Cleaner
        from video_factory.preprocessing.image_cleaner import ProductImageCleaner
        
        cleaner = ProductImageCleaner()
        cleaning_result = cleaner.clean_resource_pack(
            resource_pack=resource_pack,
            output_dir=None,  # Auto-generates in workspace
            detected_artifacts=extract_artifacts_from_qc(initial_qc),
        )
        
        if not cleaning_result.get("cleaned_images"):
            return JobFailed(
                retryable=False,
                code="CLEANING_FAILED",
                message="Image Cleaner produced no cleaned images"
            )
        
        # 6. Update resource pack with cleaned images
        cleaned_pack = replace_images_in_resource_pack(
            resource_pack=resource_pack,
            cleaned_images=cleaning_result["cleaned_images"]
        )
        cleaned_pack["image_qc_retry_count"] = retry_count + 1
        cleaned_pack["cleaning_summary"] = cleaning_result.get("summary")
        
        # 7. Re-run Image QC on cleaned images
        from video_factory.qc.image_qc import run_image_qc
        from video_factory.qc.contracts import QCVerdict
        from video_factory.qc.repository import QCReportRepository
        
        product = resource_pack.get("research_snapshot", {}).get("product")
        retry_qc_report, retry_eligible = run_image_qc(
            workflow_id=ctx.workflow_id,
            resource_pack=cleaned_pack,
            product=product,
            repo=QCReportRepository(ctx.connection) if hasattr(ctx, 'connection') else None,
            attempt_id=f"retry_{retry_count + 1}",
        )
        
        cleaned_pack["retry_qc_report"] = retry_qc_report.to_dict() if hasattr(retry_qc_report, 'to_dict') else retry_qc_report
        
        if retry_qc_report.verdict == QCVerdict.PASS:
            # SUCCESS: Cleaned images passed QC
            return JobSucceeded(
                artifact_ref=cleaned_pack,
                next_command={"command": "lock_resource_pack"}
            )
        else:
            # FAIL: Cleaning did not fix the issue -> terminal failure
            return JobFailed(
                retryable=False,
                code="IMAGE_QC_TERMINAL_FAILURE",
                message=f"Image QC failed after cleaning: {retry_qc_report.findings.get('recovery_reason', 'unknown')}"
            )


    def _validate_state_and_version(
        self,
        ctx: JobContext,
        legal_states: set[str | ProductVideoWorkflowState],
        inputs: dict[str, Any] | None = None,
        *,
        provider_family: str | None = None,
    ) -> JobFailed | None:
        missing_provider = (
            provider_family == "product_intelligence" and self._missing_pi_provider
        ) or (provider_family == "video_factory" and self._missing_video_provider)
        if missing_provider:
            details = getattr(self, "_missing_provider_details", None) or "live API provider adapters unconfigured"
            raise RuntimeError(
                f"MISSING_PROVIDER_CONFIG: {details} for provider_mode='configured'. "
                "Register the required MCP tools in config.yaml under 'mcp_servers' or set provider_mode='fake' for explicit offline/test verification."
            )

        res = self._get_workflow_state_and_version(ctx)
        if res is None:
            return JobFailed(
                retryable=False,
                code="WORKFLOW_NOT_FOUND",
                message=f"Workflow '{ctx.workflow_id}' not found in engine/repository",
            )

        current_state, current_version = res

        # 1. Reject if state is in HITL waiting states
        for hitl_st in HITL_WAITING_STATES:
            if current_state == hitl_st.value or current_state == hitl_st:
                return JobFailed(
                    retryable=False,
                    code="HITL_APPROVAL_REQUIRED",
                    message=f"Cannot dispatch job from HITL waiting state: {current_state}",
                )

        # 2. Reject if state is not legal for this job
        legal_state_values = {s.value if isinstance(s, ProductVideoWorkflowState) else str(s) for s in legal_states}
        if current_state not in legal_state_values:
            return JobFailed(
                retryable=False,
                code="ILLEGAL_WORKFLOW_STATE",
                message=f"State '{current_state}' is not legal for this stage. Allowed: {legal_state_values}",
            )

        # 3. Check version mismatch if expected_version is provided
        if ctx.expected_version is not None and ctx.expected_version != current_version:
            return JobFailed(
                retryable=False,
                code="VERSION_CONFLICT",
                message=f"Stale workflow version: expected {ctx.expected_version}, current {current_version}",
            )

        # 4. Check expected digests if provided
        if ctx.expected_digests and inputs:
            if not verify_job_digests(ctx.job_id, inputs, ctx.expected_digests):
                return JobFailed(
                    retryable=False,
                    code="DIGEST_MISMATCH",
                    message="Input digest verification failed",
                )

        return None

    def _run_preview_image_qc(
        self,
        workflow_id: str,
        resource_pack: dict,
        product_metadata: dict | None = None,
        owner_user_id: str = "",
        project_id: str = "",
    ) -> tuple[dict, list]:
        """Run preview Image QC on resource pack to determine if retry is needed."""
        from video_factory.qc.image_qc import run_image_qc
        from video_factory.qc.repository import QCReportRepository
        
        # Extract connection from workflow repository if available
        connection = self.workflow_repository.conn if self.workflow_repository else None
        repo = QCReportRepository(connection) if connection else None
        
        # If not provided, try to extract from resource pack
        if not owner_user_id:
            owner_user_id = resource_pack.get("owner_user_id", "test_user")
        if not project_id:
            project_id = resource_pack.get("project_id", f"proj_{workflow_id}")
        
        qc_report, eligible_items = run_image_qc(
            workflow_id=workflow_id,
            resource_pack=resource_pack,
            product=product_metadata,
            repo=None,
            attempt_id="preview_qc",
            owner_user_id=owner_user_id,
            project_id=project_id,
        )
        
        return qc_report.to_dict() if hasattr(qc_report, 'to_dict') else qc_report, eligible_items


    # Methods from research_handler.py
    def research_job(self, ctx: JobContext, product_name: str, keywords: list[str], pdp_url: str | None = None, **kwargs: Any) -> JobSucceeded | JobFailed:
        inputs = {"product_name": product_name, "keywords": keywords}
        src_ctx = kwargs.get("source_context") if isinstance(kwargs.get("source_context"), dict) else dict(kwargs)
        is_exchange = _is_exchange_source_context(src_ctx)
        err = self._validate_state_and_version(
            ctx,
            {ProductVideoWorkflowState.RESEARCHING},
            inputs,
            provider_family="product_intelligence",
        )
        if err:
            return err

        res_info = self._get_workflow_state_and_version(ctx)
        cur_ver = res_info[1] if res_info else 1
        cache_key = f"{ctx.workflow_id}:research:v{cur_ver}:{ctx.idempotency_key}" if ctx.idempotency_key else None
        alt_key = f"{ctx.workflow_id}:research:{ctx.idempotency_key}" if ctx.idempotency_key else None
        if cache_key and cache_key in self._idempotency_cache:
            return self._idempotency_cache[cache_key]
        if alt_key and alt_key in self._idempotency_cache:
            return self._idempotency_cache[alt_key]

        if not product_name:
            return JobFailed(retryable=False, code="PRODUCT_INPUT_INVALID", message="product_name cannot be empty")
        fresh_run = False
        if self.workflow_repository:
            current = self.workflow_repository.get_workflow(ctx.workflow_id)
            fresh_run = bool(current and current[0].get('creative_policy',{}).get('fresh_acquisition'))
            if fresh_run:
                aggregate,version = current
                if aggregate.get('fresh_acquisition_result'):
                    return JobSucceeded(artifact_ref=aggregate['fresh_acquisition_result'],
                        next_command={'command':'build_resource_pack','product_name':product_name})
                if aggregate.get('fresh_acquisition_status') == 'submitting':
                    return JobFailed(retryable=False,code='PI_ACQUISITION_UNCERTAIN',
                        message='Fresh acquisition was already dispatched; reconcile before another request')
                self.workflow_repository.commit_workflow(ctx.workflow_id,
                    json.dumps({**aggregate,'fresh_acquisition_status':'submitting'}),[],expected_version=version)
        try:
            extra_kwargs = {
                key: kwargs[key]
                for key in ("brand", "model", "variant", "canonical_product_key", "research_snapshot_id")
                if kwargs.get(key) is not None
            }
            if is_exchange:
                result = _build_exchange_research_result(
                    workflow_id=ctx.workflow_id,
                    product_name=product_name,
                    keywords=keywords,
                    pdp_url=pdp_url,
                    source_context=src_ctx,
                )
            else:
                result = self.pi_client.research(
                    product_name,
                    keywords,
                    workflow_id=ctx.workflow_id,
                    job_id=ctx.job_id,
                    idempotency_key=ctx.idempotency_key,
                    **extra_kwargs,
                )
            if pdp_url and not is_exchange and not result.get("pdp_url"):
                result["pdp_url"] = pdp_url
            if self.workflow_repository:
                current=self.workflow_repository.get_workflow(ctx.workflow_id)
                if current and current[0].get('creative_policy',{}).get('fresh_acquisition'):
                    raw=result.get('raw_result') or result
                    products=raw.get('canonical_products') or []
                    if (not raw.get('snapshot_id') or raw.get('persistence_status') not in {'saved','persisted'} or
                        not raw.get('research_id') or len(products)!=1 or not products[0].get('canonical_product_id')):
                        return JobFailed(retryable=False,code='PI_FRESH_RECEIPT_REQUIRED',
                            message='Fresh research/snapshot/canonical product receipt required',artifact_ref=result)
                    from video_factory.creative.contracts import canonical_digest
                    result['acquisition_receipt']={'producer':'Product-Intelligence',
                        'producer_run_id':raw['research_id'],'snapshot_id':raw['snapshot_id'],
                        'canonical_product_id':products[0]['canonical_product_id'],
                        'snapshot_digest':canonical_digest(raw),'observed_completed_at':datetime.now(timezone.utc).isoformat(),
                        'attestation':'VF received successful fresh research response; not snapshot retrieval'}
                    aggregate,version=self.workflow_repository.get_workflow(ctx.workflow_id)
                    self.workflow_repository.commit_workflow(ctx.workflow_id,
                        json.dumps({**aggregate,'fresh_acquisition_status':'completed','fresh_acquisition_result':result}),
                        [],expected_version=version)
            res = JobSucceeded(artifact_ref=result, next_command={"command": "build_resource_pack", "product_name": product_name})
            if cache_key:
                self._idempotency_cache[cache_key] = res
            return res
        except TerminalJobError as e:
            return JobFailed(retryable=False, code=e.code, message=str(e))
        except ValueError as e:
            return JobFailed(retryable=False, code="INVALID_INPUT", message=str(e))
        except Exception as e:
            if fresh_run:
                return JobFailed(retryable=False,code='PI_ACQUISITION_UNCERTAIN',
                    message='Fresh acquisition transport failed; inspect the existing operation before retrying')
            return JobFailed(retryable=True, code="PI_RESEARCH_FAILED", message=str(e))

    def resource_pack_job(self, ctx: JobContext, research_result: dict, **kwargs: Any) -> JobSucceeded | JobFailed:
        from video_factory.qc.auto_retry_helpers import (
            extract_product_from_resource_pack,
            is_cleanable_failure,
        )
        from video_factory.qc.models import QCVerdict
        
        inputs = {"research_result": research_result}
        is_exchange = _is_exchange_payload(research_result, kwargs)
        err = self._validate_state_and_version(
            ctx,
            {
                ProductVideoWorkflowState.BUILDING_RESOURCE_PACK,
                ProductVideoWorkflowState.RESEARCHING,
            },
            inputs,
            provider_family=None if is_exchange else "product_intelligence",
        )
        if err:
            return err

        res_info = self._get_workflow_state_and_version(ctx)
        cur_ver = res_info[1] if res_info else 1
        cache_key = f"{ctx.workflow_id}:resource_pack:v{cur_ver}:{ctx.idempotency_key}" if ctx.idempotency_key else None
        alt_key = f"{ctx.workflow_id}:resource_pack:{ctx.idempotency_key}" if ctx.idempotency_key else None
        if cache_key and cache_key in self._idempotency_cache:
            return self._idempotency_cache[cache_key]
        if alt_key and alt_key in self._idempotency_cache:
            return self._idempotency_cache[alt_key]

        if not research_result:
            return JobFailed(retryable=False, code="MISSING_INPUT_REF", message="research_result cannot be empty")

        # In configured provider_mode, validate research snapshot persistence prerequisite before dispatch
        if getattr(self, "provider_mode", "fake") == "configured" and not is_exchange:
            snapshot_id = research_result.get("snapshot_id")
            persistence_status = research_result.get("persistence_status")
            if not snapshot_id:
                return JobFailed(
                    retryable=False,
                    code="PREREQUISITE_FAILED",
                    message="Missing snapshot_id in research_result for configured mode",
                )
            if persistence_status not in ("saved", "persisted"):
                return JobFailed(
                    retryable=False,
                    code="PREREQUISITE_FAILED",
                    message=f"Research snapshot is not successfully persisted (persistence_status={persistence_status!r})",
                )

        try:
            source_ctx = kwargs.get("source_context")
            if not source_ctx and isinstance(research_result, dict):
                source_ctx = research_result.get("source_context")
            if not source_ctx and getattr(self, "workflow_repository", None):
                wf_data = self.workflow_repository.get_workflow(ctx.workflow_id)
                if wf_data:
                    source_ctx = wf_data[0].get("source_context")
            elif not source_ctx and getattr(self, "workflow_engine", None):
                if hasattr(self.workflow_engine, "repo"):
                    wf_data = self.workflow_engine.repo.get_workflow(ctx.workflow_id)
                    if wf_data:
                        source_ctx = wf_data[0].get("source_context")
            source_ctx = source_ctx or {}

            if is_exchange:
                result = _build_exchange_resource_pack(
                    workflow_id=ctx.workflow_id,
                    research_result=research_result,
                    kwargs=kwargs,
                )
            else:
                pack_kwargs = dict(kwargs)
                pack_kwargs["source_context"] = source_ctx
                result = self.pi_client.build_resource_pack(
                    research_result,
                    workflow_id=ctx.workflow_id,
                    job_id=ctx.job_id,
                    idempotency_key=ctx.idempotency_key,
                    **pack_kwargs,
                )

            # In configured mode, enforce approved images match before any Image QC or evaluator
            if getattr(self, "provider_mode", "fake") == "configured":
                if _is_exchange_source_context(source_ctx):
                    if not (source_ctx or {}).get("auto_admitted", False):
                        _assert_approved_images_in_resource_pack(
                            source_context=source_ctx,
                            resource_pack=result,
                        )

            # Auto-run Image QC to check if cleanable failures exist
            # This is a preview QC run - not persisted yet, just for routing decision
            # Skip QC if in fake/test mode or missing required metadata
            has_required_metadata = (
                result.get("resource_pack_id") 
                and result.get("version")
                and ctx.owner_user_id
            )
            
            if has_required_metadata and getattr(self, "provider_mode", "fake") == "configured":
                conn = self.workflow_repository.conn if self.workflow_repository else None
                qc_evaluator = getattr(self, "evaluator", None)
                if self.qc_service_factory is not None:
                    qc_svc = self.qc_service_factory(conn)
                else:
                    from video_factory.qc.service import ProductVideoQCService
                    qc_svc = ProductVideoQCService(conn, evaluator=qc_evaluator)
                qc_provider = str(
                    getattr(qc_evaluator, "provider_name", None)
                    or getattr(qc_evaluator, "evaluator_name", None)
                    or type(qc_evaluator).__name__
                )
                qc_auth = kwargs.get("qc_authorization")
                qc_max = kwargs.get("qc_max_cost_usd")
                wf_data = self.workflow_repository.get_workflow(ctx.workflow_id) if self.workflow_repository else None
                aggregate = wf_data[0] if wf_data else {}
                if not qc_auth:
                    qc_auth = aggregate.get("qc_authorization")
                    if qc_max is None:
                        qc_max = aggregate.get("qc_max_cost_usd")
                food_references = is_exchange and (aggregate.get("creative_policy") or {}).get("category") == "food_pov"

                step_started_at = datetime.now(timezone.utc).isoformat()
                step_started_mono = time.monotonic()
                step_input_digest = hashlib.sha256(
                    json.dumps(
                        [
                            {
                                "asset_id": str(asset.get("asset_id") or ""),
                                "sha256": str(asset.get("sha256") or asset.get("digest") or ""),
                            }
                            for asset in result.get("assets", []) if isinstance(asset, dict)
                        ],
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()
                if self.workflow_repository:
                    from video_factory.workflows.steps import record_pipeline_step

                    record_pipeline_step(
                        conn, workflow_id=ctx.workflow_id,
                        product_key=str(source_ctx.get("product_key") or ctx.workflow_id),
                        step_name="IMAGE_QC", status="running",
                        input_digest=f"sha256:{step_input_digest}", provider=qc_provider,
                        started_at=step_started_at, cost_status="unknown",
                    )

                try:
                    sel_res = qc_svc.select_image_references(
                        workflow_id=ctx.workflow_id,
                        resource_pack=result,
                        source_context=source_ctx,
                        provider_mode="configured",
                        authorization=qc_auth,
                        max_cost_usd=qc_max,
                        qc_budget_mode=(qc_auth or {}).get("qc_budget_mode") if isinstance(qc_auth, dict) else None,
                        max_recoveries=kwargs.get("max_qc_retries"),
                        owner_user_id=ctx.owner_user_id,
                        project_id=kwargs.get("project_id", f"proj_{ctx.workflow_id}"),
                        max_references=5 if food_references else 1,
                    )
                except Exception as exc:
                    if self.workflow_repository:
                        from video_factory.workflows.steps import record_pipeline_step

                        record_pipeline_step(
                            conn, workflow_id=ctx.workflow_id,
                            product_key=str(source_ctx.get("product_key") or ctx.workflow_id),
                            step_name="IMAGE_QC", status="failed",
                            input_digest=f"sha256:{step_input_digest}", provider=qc_provider,
                            started_at=step_started_at,
                            duration_ms=int((time.monotonic() - step_started_mono) * 1000),
                            cost_status="unknown", error_code="IMAGE_QC_ERROR",
                            message=f"Image QC service failed ({type(exc).__name__}).",
                            next_action="Inspect the durable attempt; reconcile if the request may have been sent.",
                        )
                    return JobFailed(
                        retryable=False,
                        code="IMAGE_QC_ERROR",
                        message=f"Image QC service failed with unexpected error: {exc}",
                    )

                evaluated = sel_res.get("evaluated_items", []) if isinstance(sel_res, dict) else []
                route_receipt = next(
                    (
                        item.get("findings", {}).get("route_receipt")
                        for item in evaluated
                        if isinstance(item, dict)
                        and isinstance(item.get("findings"), dict)
                        and isinstance(item["findings"].get("route_receipt"), dict)
                    ),
                    {},
                )
                actual_qc_provider = str(route_receipt.get("upstream_provider") or qc_provider)
                actual_qc_model = str(route_receipt.get("upstream_model") or "") or None
                if self.workflow_repository:
                    from video_factory.qc.retry_policy import ensure_qc_retry_schema

                    ensure_qc_retry_schema(conn)

                if not isinstance(sel_res, dict) or sel_res.get("status") != "completed":
                    err_code = str(sel_res.get("code") or "IMAGE_QC_FAILED") if isinstance(sel_res, dict) else "IMAGE_QC_FAILED"
                    err_reason = str(sel_res.get("reason") or "Visual Image QC failed to select a usable reference") if isinstance(sel_res, dict) else "Image QC returned unexpected result"
                    if err_code == "IMAGE_QC_FAILED" and isinstance(sel_res, dict):
                        # Only explicit, consumed semantic item verdicts prove
                        # that this SKU's source images failed QC. A generic
                        # error or missing report must pause rather than skip.
                        from video_factory.automation.failure_classification import has_consumed_semantic_image_qc_evidence

                        if has_consumed_semantic_image_qc_evidence(sel_res):
                            err_code = (
                                "EXCHANGE_IMAGE_QC_FAILED"
                                if source_ctx.get("source") == "hermes_product_video_exchange"
                                else "PRODUCT_IMAGE_QC_REJECTED"
                            )
                    is_retryable = err_code in ("TECHNICAL_UNKNOWN", "QC_PROVIDER_UNAVAILABLE", "QC_RESPONSE_INVALID")
                    if self.workflow_repository:
                        from video_factory.workflows.steps import record_pipeline_step

                        is_uncertain = "UNKNOWN" in err_code.upper() or "UNCERTAIN" in err_code.upper()
                        retry_row = conn.execute(
                            "SELECT attempt_number,operation_id FROM workflow_qc_retries "
                            "WHERE workflow_id=? AND retry_type='source_image_qc' ORDER BY id DESC LIMIT 1",
                            (ctx.workflow_id,),
                        ).fetchone()
                        record_pipeline_step(
                            conn, workflow_id=ctx.workflow_id,
                            product_key=str(source_ctx.get("product_key") or ctx.workflow_id),
                            step_name="IMAGE_QC",
                            status="uncertain" if is_uncertain else ("pending_operator" if not is_retryable else "failed"),
                            attempt_number=int(retry_row["attempt_number"]) if retry_row else None,
                            operation_id=str(retry_row["operation_id"]) if retry_row and retry_row["operation_id"] else None,
                            input_digest=f"sha256:{step_input_digest}", provider=actual_qc_provider,
                            actual_model=actual_qc_model,
                            started_at=step_started_at,
                            duration_ms=int((time.monotonic() - step_started_mono) * 1000),
                            cost_status="unknown" if is_uncertain or sel_res.get("usage_status") == "consumed" else "not_applicable",
                            error_code=err_code, message=err_reason,
                            next_action=(
                                "Reconcile the exact provider attempt before retry." if is_uncertain
                                else "Retry this QC with the same input only within the durable attempt limit." if err_code == "QC_RESPONSE_INVALID"
                                else "Review the recorded failure and create a corrected image before retrying."
                            ),
                        )
                    return JobFailed(
                        retryable=is_retryable,
                        code=err_code,
                        message=err_reason,
                        artifact_ref=result,
                    )
                result = sel_res["resource_pack"]
                if self.workflow_repository:
                    from video_factory.workflows.steps import record_pipeline_step

                    qc_report = sel_res.get("report")
                    cost_usd = getattr(qc_report, "cost_usd", None)
                    retry_row = conn.execute(
                        "SELECT attempt_number,operation_id FROM workflow_qc_retries "
                        "WHERE workflow_id=? AND retry_type='source_image_qc' ORDER BY id DESC LIMIT 1",
                        (ctx.workflow_id,),
                    ).fetchone()
                    record_pipeline_step(
                        conn, workflow_id=ctx.workflow_id,
                        product_key=str(source_ctx.get("product_key") or ctx.workflow_id),
                        step_name="IMAGE_QC", status="completed",
                        attempt_number=int(retry_row["attempt_number"]) if retry_row else None,
                        operation_id=str(retry_row["operation_id"]) if retry_row and retry_row["operation_id"] else None,
                        input_digest=f"sha256:{step_input_digest}", provider=actual_qc_provider,
                        requested_model=str(getattr(qc_svc.evaluator, "model", "") or "") or None,
                        actual_model=actual_qc_model or str((qc_report.findings if qc_report and isinstance(qc_report.findings, dict) else {}).get("actual_model") or "") or None,
                        started_at=step_started_at,
                        duration_ms=int((time.monotonic() - step_started_mono) * 1000),
                        cost_status="recorded" if cost_usd is not None else "unknown",
                        cost_usd=float(cost_usd) if cost_usd is not None else None,
                    )

            result_with_qc = dict(result)
            res = JobSucceeded(
                artifact_ref=result_with_qc,
                next_command={"command": "lock_resource_pack"}
            )

            if cache_key:
                self._idempotency_cache[cache_key] = res
            return res
        except TerminalJobError as e:
            return JobFailed(retryable=False, code=e.code, message=str(e))
        except ValueError as e:
            return JobFailed(retryable=False, code="PREREQUISITE_FAILED", message=str(e))
        except Exception as e:
            return JobFailed(retryable=True, code="PI_PACK_FAILED", message=str(e))

    def lock_job(self, ctx: JobContext, pack_dict: dict, **kwargs: Any) -> JobSucceeded | JobFailed:
        inputs = {"pack_dict": pack_dict}
        is_exchange = _is_exchange_payload(pack_dict, kwargs)
        err = self._validate_state_and_version(
            ctx,
            {
                ProductVideoWorkflowState.LOCKING_RESOURCE_PACK,
                ProductVideoWorkflowState.BUILDING_RESOURCE_PACK,
                ProductVideoWorkflowState.RETRYING_IMAGE_QC,
            },
            inputs,
            provider_family=None if is_exchange else "product_intelligence",
        )
        if err:
            return err

        res_info = self._get_workflow_state_and_version(ctx)
        cur_ver = res_info[1] if res_info else 1
        cache_key = f"{ctx.workflow_id}:lock:v{cur_ver}:{ctx.idempotency_key}" if ctx.idempotency_key else None
        alt_key = f"{ctx.workflow_id}:lock:{ctx.idempotency_key}" if ctx.idempotency_key else None
        if cache_key and cache_key in self._idempotency_cache:
            return self._idempotency_cache[cache_key]
        if alt_key and alt_key in self._idempotency_cache:
            return self._idempotency_cache[alt_key]

        if not pack_dict:
            return JobFailed(retryable=False, code="MISSING_INPUT_REF", message="pack_dict cannot be empty")

        resource_pack_id = pack_dict.get("resource_pack_id") or pack_dict.get("id") or pack_dict.get("pack_id")
        if not resource_pack_id:
            if getattr(self, "provider_mode", "fake") == "fake":
                resource_pack_id = f"pack_{ctx.workflow_id}"
            else:
                return JobFailed(
                    retryable=False,
                    code="PREREQUISITE_FAILED",
                    message="Cannot lock resource pack: missing resource_pack_id in pack_dict",
                )

        pack_status = pack_dict.get("pack_status") or pack_dict.get("status")
        exchange_source = _is_exchange_payload(pack_dict, kwargs)
        missing_exchange_status = exchange_source and pack_status is None
        if pack_status in ("needs_review", "incomplete", "rejected", "unknown") or (
            getattr(self, "provider_mode", "fake") == "configured"
            and pack_status != "ready_to_lock"
            and not missing_exchange_status
        ):
            return JobFailed(
                retryable=False,
                code="NON_LOCKABLE_STATUS",
                message=f"Cannot lock resource pack with non-lockable status '{pack_status}': pack is not ready_to_lock",
            )

        proj_id = kwargs.get("project_id")
        if not proj_id and self.workflow_repository:
            try:
                wf_d = self.workflow_repository.get_workflow(ctx.workflow_id)
                if wf_d:
                    proj_id = wf_d[0].get("project_id")
            except Exception:
                pass
        if not proj_id:
            proj_id = f"proj_{ctx.workflow_id}"

        # In configured mode, enforce physical byte SHA-256 match for resource pack assets
        if getattr(self, "provider_mode", "fake") == "configured":
            assets_to_check = pack_dict.get("assets", []) or pack_dict.get("product_references", [])
            if not assets_to_check:
                return JobFailed(
                    retryable=False,
                    code="APPROVED_IMAGE_MISMATCH",
                    message="Resource pack must contain a usable image",
                )
            asset_hashes = []
            for asset in assets_to_check:
                if isinstance(asset, dict):
                    actual_sha = _resolve_image_bytes_hash(asset)
                    if not actual_sha:
                        return JobFailed(
                            retryable=False,
                            code="APPROVED_IMAGE_MISMATCH",
                            message=f"Image file missing or unreadable for asset {asset.get('asset_id')}",
                        )
                    expected_sha = asset.get("sha256")
                    if expected_sha and actual_sha and expected_sha.lower() != actual_sha.lower():
                        return JobFailed(
                            retryable=False,
                            code="APPROVED_IMAGE_MISMATCH",
                            message=f"Byte hash mismatch for asset {asset.get('asset_id')}: expected {expected_sha}, got {actual_sha}",
                        )
                    asset_hashes.append(actual_sha.lower())
            if not asset_hashes:
                return JobFailed(
                    retryable=False,
                    code="APPROVED_IMAGE_MISMATCH",
                    message="Resource pack has no readable image content",
                )

            # Strict Quality Gate: Resource pack must have a verified PASS Image QC report matching exact pack digest
            if self.workflow_repository and self.workflow_repository.conn:
                from video_factory.qc.repository import QCReportRepository
                from video_factory.qc.models import QCStage
                from video_factory.qc.validation import require_current_pass_report, QCValidationError

                repo_qc = QCReportRepository(self.workflow_repository.conn)
                workflow_data = self.workflow_repository.get_workflow(ctx.workflow_id)
                qc_level = ((workflow_data[0].get("creative_policy") or {}).get("qc_level", "middle")
                            if workflow_data else "middle")
                if qc_level != "low":
                    try:
                        require_current_pass_report(
                            repo_qc,
                            workflow_id=ctx.workflow_id,
                            stage=QCStage.IMAGE,
                            artifact=pack_dict,
                            owner_user_id=ctx.owner_user_id,
                            project_id=proj_id,
                            provider_mode=getattr(self, "provider_mode", "fake"),
                        )
                    except QCValidationError as e:
                        return JobFailed(
                            retryable=False,
                            code="APPROVED_IMAGE_MISMATCH",
                            message=f"Product Lock blocked: {e.reason}",
                        )

        # ECC Quality Gate: Ensure assets have 9:16 vertical clean keyframes
        try:
            from video_factory.preprocessing.image_cleaner import format_clean_916_keyframe
            assets = pack_dict.get("assets", [])
            for asset in assets:
                if isinstance(asset, dict) and (asset.get("local_path") or asset.get("path")):
                    raw_p = Path(asset.get("local_path") or asset.get("path"))
                    if raw_p.is_file() and not asset.get("clean_916_formatted"):
                        clean_916_p = raw_p.parent / f"{raw_p.stem}_clean_916.png"
                        if not clean_916_p.is_file():
                            format_clean_916_keyframe(raw_p, clean_916_p)
                        asset["clean_916_path"] = str(clean_916_p)
                        asset["clean_916_formatted"] = True
        except Exception as exc:
            logger.warning("Auto 9:16 keyframe formatting skipped: %s", exc)

        try:
            if is_exchange:
                result = _lock_exchange_resource_pack(
                    pack_dict=pack_dict,
                    owner_user_id=ctx.owner_user_id,
                    workflow_id=ctx.workflow_id,
                )
            else:
                result = self.pi_client.lock_resource_pack(
                    pack_dict,
                    ctx.owner_user_id,
                    workflow_id=ctx.workflow_id,
                    job_id=ctx.job_id,
                    idempotency_key=ctx.idempotency_key,
                    project_id=proj_id,
                )
            res = JobSucceeded(artifact_ref=result, next_command={"command": "create_creative_brief"})
            if cache_key:
                self._idempotency_cache[cache_key] = res
            return res
        except TerminalJobError as e:
            return JobFailed(retryable=False, code=e.code, message=str(e))
        except ValueError as e:
            return JobFailed(retryable=False, code="PREREQUISITE_FAILED", message=str(e))
        except Exception as e:
            return JobFailed(retryable=True, code="PI_LOCK_FAILED", message=str(e))


    # Methods from creative_handler.py
    def _bind_creative_run(self, ctx):
        if not self.workflow_repository:
            return
        current=self.workflow_repository.get_workflow(ctx.workflow_id)
        policy=current[0].get('creative_policy',{}) if current else {}
        if policy.get('creative_mode')!='four_agent':
            return
        from video_factory.creative.agents.contracts import BackendProfile
        from video_factory.creative.agents.service import AgenticCreativeGenerator
        director_config_version = policy.get('director_config_version')
        if self.provider_mode == "fake":
            from video_factory.creative.agents.fakes import FakeRoles
            target_scenes = int(policy.get("max_scenes", 4))
            fake_roles = FakeRoles(target_scenes=target_scenes)
            self.creative_generator = AgenticCreativeGenerator(
                fake_roles,
                profile=BackendProfile.from_dict(policy['profile']),
                category=policy['category'],
                visible_details=policy['visible_details'],
                source_verifier=None,
                director_config_version=director_config_version,
            )
            return
        if self.creative_generator is None:
            from video_factory.handlers.common import create_configured_creative_generator
            self.creative_generator=create_configured_creative_generator(connection=self.workflow_repository.conn,
                                                                          creative_mode='legacy')
        self.creative_generator=AgenticCreativeGenerator(self.creative_generator.model,
            profile=BackendProfile.from_dict(policy['profile']), category=policy['category'],
            visible_details=policy['visible_details'],source_verifier=self.creative_generator.source_verifier,
            director_config_version=director_config_version)

    def creative_brief_job(self, ctx: JobContext, product_id: str, brief_config: dict | None = None, **kwargs: Any) -> JobSucceeded | JobFailed:
        self._bind_creative_run(ctx)
        inputs = {"product_id": product_id, "brief_config": brief_config or {}}
        err = self._validate_state_and_version(
            ctx,
            {
                ProductVideoWorkflowState.CREATING_BRIEF,
                ProductVideoWorkflowState.LOCKING_RESOURCE_PACK,
                ProductVideoWorkflowState.BUILDING_RESOURCE_PACK,
            },
            inputs,
            provider_family="video_factory",
        )
        if err:
            return err

        res_info = self._get_workflow_state_and_version(ctx)
        cur_ver = res_info[1] if res_info else 1
        cache_key = f"{ctx.workflow_id}:creative_brief:v{cur_ver}:{ctx.idempotency_key}" if ctx.idempotency_key else None
        alt_key = f"{ctx.workflow_id}:creative_brief:{ctx.idempotency_key}" if ctx.idempotency_key else None
        if cache_key and cache_key in self._idempotency_cache:
            return self._idempotency_cache[cache_key]
        if alt_key and alt_key in self._idempotency_cache:
            return self._idempotency_cache[alt_key]

        if not product_id:
            return JobFailed(retryable=False, code="MISSING_INPUT_REF", message="product_id cannot be empty")
        alloc = None
        try:
            if isinstance(kwargs.get("qc_authorization"), dict):
                alloc = reserve_budget(
                    "video_factory",
                    5,
                    idempotency_key=cache_key,
                    **_paid_budget_kwargs(ctx, "product_video.creative_brief", kwargs, cache_key),
                )
            proj_id = (brief_config or {}).get("project_id") or kwargs.get("project_id")
            if not proj_id and self.workflow_repository:
                try:
                    wf_d = self.workflow_repository.get_workflow(ctx.workflow_id)
                    if wf_d:
                        proj_id = wf_d[0].get("project_id")
                except Exception:
                    pass
            if not proj_id:
                proj_id = f"proj_{ctx.workflow_id}"

            cfg = dict(brief_config or {})
            cfg.setdefault("project_id", proj_id)

            if self.creative_generator is not None:
                dossier_data = cfg.get("research_dossier")
                if self.workflow_repository is not None:
                    from video_factory.creative.generator import SingleModelCreativeGenerator

                    if isinstance(self.creative_generator, SingleModelCreativeGenerator):
                        workflow_data = self.workflow_repository.get_workflow(ctx.workflow_id)
                        if workflow_data is not None:
                            aggregate, _ = workflow_data
                            locked_pack = aggregate.get("locked_pack")
                            if isinstance(locked_pack, dict):
                                # The queued brief job may contain a dossier
                                # built before the approved lock was mirrored.
                                # Rebuild from the durable lock and current
                                # passing QC instead of trusting that stale copy.
                                dossier_data = _locked_research_dossier(
                                    workflow_id=ctx.workflow_id,
                                    aggregate=aggregate,
                                    locked_pack=locked_pack,
                                    connection=self.workflow_repository.conn,
                                )
                                cfg["research_dossier"] = dossier_data
                dossier = None
                if dossier_data:
                    from video_factory.creative.contracts import ResearchDossier
                    dossier = ResearchDossier.from_dict(dossier_data) if isinstance(dossier_data, dict) else dossier_data
                if dossier is None:
                    dossier = getattr(self.creative_generator, "dossier", None)
                if dossier is not None:
                    recovered_brief = None
                    if isinstance(self.video_client, MCPVideoFactoryAdapter):
                        workflow_data = self.workflow_repository.get_workflow(ctx.workflow_id) if self.workflow_repository else None
                        if workflow_data is None:
                            raise ValueError("workflow state is required to synchronize the approved Product Lock")
                        aggregate, _ = workflow_data
                        locked_pack = aggregate.get("locked_pack")
                        approval_receipt = aggregate.get("latest_approval_receipt")
                        if not isinstance(locked_pack, dict) or not isinstance(approval_receipt, dict):
                            raise ValueError("approved Product Lock artifact and receipt are required before creative generation")
                        dossier_dict = dossier.to_dict() if hasattr(dossier, "to_dict") else dossier
                        if not isinstance(dossier_dict, dict):
                            raise ValueError("grounded dossier must be serializable")
                        self.video_client.ensure_project_locked(
                            owner_user_id=ctx.owner_user_id,
                            project_id=proj_id,
                            resource_pack=locked_pack,
                            dossier=dossier_dict,
                            approval_receipt=approval_receipt,
                            workflow_id=ctx.workflow_id,
                            # This downstream mirror is created after the
                            # approval transition, so the current workflow
                            # version is newer than the approved artifact.
                            workflow_version=None,
                        )
                        recovered_brief = _recover_creative_brief(
                            self.video_client.get_existing_creative_brief(
                                owner_user_id=ctx.owner_user_id,
                                project_id=proj_id,
                            ) or {},
                            dossier,
                        )
                    brief_v2 = recovered_brief or self.creative_generator.create_direction(dossier)
                    sent_config = _video_factory_brief_config(dict(cfg), brief_v2)
                    result = (
                        {"project_id": proj_id, "brief_id": f"brief_{product_id}"}
                        if recovered_brief is not None
                        else self.video_client.create_creative_brief(
                            product_id=product_id,
                            brief_config=sent_config,
                            workflow_id=ctx.workflow_id,
                            job_id=ctx.job_id,
                            project_id=proj_id,
                            owner_user_id=ctx.owner_user_id,
                            idempotency_key=ctx.idempotency_key,
                        )
                    )
                    res_dict = dict(brief_v2.to_dict())
                    res_dict["schema_version"] = "creative-brief-v2"
                    res_dict["contract_version"] = 2
                    res_dict["brief_digest"] = brief_v2.digest()
                    res_dict["project_id"] = proj_id
                    res_dict["research_dossier"] = dossier.to_dict() if hasattr(dossier, "to_dict") else dossier_data
                    if isinstance(result, dict):
                        if "version" in result:
                            res_dict["version"] = result["version"]
                        if "brief_id" in result:
                            res_dict["brief_id"] = result["brief_id"]
                    if alloc:
                        commit_budget(alloc)
                    res = JobSucceeded(artifact_ref=res_dict, next_command={"command": "create_scene_plan", "product_id": product_id})
                    if cache_key:
                        self._idempotency_cache[cache_key] = res
                    return res

            extra_brief_kw: dict[str, Any] = {}
            if kwargs.get("project_id"):
                extra_brief_kw["project_id"] = kwargs["project_id"]
            if kwargs.get("owner_user_id"):
                extra_brief_kw["owner_user_id"] = kwargs["owner_user_id"]

            result = self.video_client.create_creative_brief(
                product_id=product_id,
                brief_config=cfg,
                workflow_id=ctx.workflow_id,
                job_id=ctx.job_id,
                idempotency_key=ctx.idempotency_key,
                **extra_brief_kw,
            )
            if alloc:
                commit_budget(alloc)
            res = JobSucceeded(artifact_ref=result, next_command={"command": "create_scene_plan", "product_id": product_id})
            if cache_key:
                self._idempotency_cache[cache_key] = res
            return res
        except Exception as e:
            return _creative_failure(e, alloc, "VF_BRIEF_FAILED")

    def scene_plan_job(self, ctx: JobContext, brief_ref: Any, **kwargs: Any) -> JobSucceeded | JobFailed:
        self._bind_creative_run(ctx)
        inputs = {"brief_ref": brief_ref}
        err = self._validate_state_and_version(
            ctx,
            {
                ProductVideoWorkflowState.CREATING_SCENE_PLAN,
                ProductVideoWorkflowState.AWAITING_BRIEF_APPROVAL,
            },
            inputs,
            provider_family="video_factory",
        )
        if err:
            return err

        res_info = self._get_workflow_state_and_version(ctx)
        cur_ver = res_info[1] if res_info else 1
        cache_key = f"{ctx.workflow_id}:scene_plan:v{cur_ver}:{ctx.idempotency_key}" if ctx.idempotency_key else None
        alt_key = f"{ctx.workflow_id}:scene_plan:{ctx.idempotency_key}" if ctx.idempotency_key else None
        if cache_key and cache_key in self._idempotency_cache:
            return self._idempotency_cache[cache_key]
        if alt_key and alt_key in self._idempotency_cache:
            return self._idempotency_cache[alt_key]

        if not brief_ref:
            return JobFailed(retryable=False, code="MISSING_INPUT_REF", message="brief_ref cannot be empty")
        alloc = None
        try:
            proj_id = kwargs.get("project_id")
            if not proj_id and isinstance(brief_ref, dict):
                proj_id = brief_ref.get("project_id")
            if not proj_id and self.workflow_repository:
                try:
                    wf_d = self.workflow_repository.get_workflow(ctx.workflow_id)
                    if wf_d:
                        proj_id = wf_d[0].get("project_id")
                except Exception:
                    pass
            if not proj_id:
                proj_id = f"proj_{ctx.workflow_id}"

            if self.creative_generator is not None:
                brief_dict = brief_ref if isinstance(brief_ref, dict) else {}
                dossier_dict = brief_dict.get("research_dossier")
                from video_factory.creative.contracts import (
                    CreativeBriefV2, ResearchDossier, ScriptScenePlanV2,
                )
                dossier = getattr(self.creative_generator, "dossier", None)
                if dossier is None and dossier_dict:
                    dossier = ResearchDossier.from_dict(dossier_dict)
                brief_v2 = getattr(self.creative_generator, "brief", None)
                if brief_v2 is None and brief_dict:
                    try:
                        brief_v2 = CreativeBriefV2.from_dict(brief_dict)
                    except Exception:
                        pass
                if dossier and brief_v2:
                    revision=kwargs.get('creative_revision')
                    repair_source=revision or {}
                    four_agent = getattr(self.creative_generator, 'creative_mode', '') == 'four_agent'
                    draft_key = None
                    draft = {}
                    if four_agent and not revision and self.workflow_repository:
                        from video_factory.creative.contracts import canonical_digest
                        draft_key = canonical_digest({'dossier':dossier.digest(), 'brief':brief_v2.digest(),
                            'profile':self.creative_generator.profile.digest, 'contract':'director-script-v1.1',
                            'playbook':self.creative_generator.recipe['digest'],
                            'revision_feedback':kwargs.get('revision_feedback',''), 'idempotency_key':ctx.idempotency_key})
                        current = self.workflow_repository.get_workflow(ctx.workflow_id)
                        draft = current[0].get('creative_script_checkpoint', {}) if current else {}
                    if revision:
                        script_v2 = ScriptScenePlanV2.from_dict(revision)
                    elif draft_key and draft.get('input_digest') == draft_key:
                        script_v2 = ScriptScenePlanV2.from_dict(draft['script'])
                    elif (kwargs.get('revision_feedback') and
                          getattr(self.creative_generator, 'creative_mode', '') == 'four_agent'):
                        script_v2 = self.creative_generator.create_script(
                            dossier, brief_v2, revision_feedback=kwargs['revision_feedback'])
                    else:
                        script_v2 = self.creative_generator.create_script(dossier, brief_v2)
                    if draft_key and draft.get('input_digest') != draft_key:
                        current, version = self.workflow_repository.get_workflow(ctx.workflow_id)
                        current['creative_script_checkpoint'] = {'input_digest':draft_key, 'script':script_v2.to_dict()}
                        self.workflow_repository.commit_workflow(ctx.workflow_id, json.dumps(current), [], expected_version=version)
                    creative_envelopes = {}
                    if getattr(self.creative_generator, 'creative_mode', '') == 'four_agent':
                        if revision:
                            from video_factory.creative.agents.contracts import ScenePromptBundle
                            bundle=ScenePromptBundle.from_dict(revision['scene_prompts'])
                        else:
                            bundle = self.creative_generator.create_scene_prompts(dossier, script_v2)
                        profile = self.creative_generator.profile
                        creative_envelopes = {'creative_mode':'four_agent', 'scene_prompts':bundle.to_dict(),
                            'backend_profile':profile.to_dict(), 'creative_brief':brief_v2.to_dict()}
                        if repair_source:
                            creative_envelopes['repair_history']=repair_source.get('repair_history',[])
                        current=self.workflow_repository.get_workflow(ctx.workflow_id) if self.workflow_repository else None
                        policy=current[0].get('creative_policy',{}) if current else {}
                        if len(script_v2.scenes)>policy.get('max_scenes',4):
                            return JobFailed(retryable=False,code='CREATIVE_SCENE_CAP',message='Scene count exceeds run policy')
                        run_report={'workflow_id':ctx.workflow_id,'product':dossier.product_identity.__dict__,
                            'scene_count':len(script_v2.scenes),'source_refs':[{'asset_id':a.asset_id,'path':a.path,'sha256':a.sha256}
                                for a in dossier.eligible_assets],'profile':profile.to_dict(),'prompts':bundle.to_dict(),
                            'image_call_limit':min(24,policy.get('image_call_limit',24)),
                            'video_call_limit':min(12,policy.get('video_call_limit',12)),
                            'per_logical_item_generations':3,'max_script_rewrites':2,
                            'acquisition_receipt':(current[0].get('research_result',{}).get('acquisition_receipt') if current else None),
                            'generation_status':'not_started','human_review':'not_performed'}
                        report_dir=Path('data')/'creative_runs'/ctx.workflow_id
                        report_dir.mkdir(parents=True,exist_ok=True)
                        report_path=report_dir/(bundle.digest.removeprefix('sha256:')+'-pre-generation.json')
                        if not report_path.exists():
                            report_path.write_text(json.dumps(run_report,ensure_ascii=False,indent=2),encoding='utf8')
                    legacy_scenes = [
                        {
                            "scene_id": s.scene_id,
                            "order": s.order,
                            "title": f"Scene {s.order}: {s.story_role}",
                            "objective": s.visual_objective,
                            "content": s.narration,
                            "main_action": s.subject_action,
                            "duration_seconds": s.duration_seconds,
                            "start_state": s.start_state,
                            "end_state": s.end_state,
                            "camera_intention": s.camera_movement,
                        }
                        for s in script_v2.scenes
                    ]
                    sent_brief = dict(brief_dict)
                    sent_brief["scenes"] = legacy_scenes
                    sent_brief.setdefault("project_id", proj_id)
                    appr_id = kwargs.get("approval_request_id") or (kwargs.get("approval_receipt") or {}).get("approval_request_id")
                    result = self.video_client.create_scene_plan(
                        sent_brief,
                        workflow_id=ctx.workflow_id,
                        job_id=ctx.job_id,
                        project_id=proj_id,
                        owner_user_id=ctx.owner_user_id,
                        idempotency_key=ctx.idempotency_key,
                        approval_request_id=appr_id,
                        approval_receipt=kwargs.get("approval_receipt"),
                    )
                    res_dict = dict(script_v2.to_dict())
                    res_dict.update(creative_envelopes)
                    res_dict["schema_version"] = "script-scene-plan-v2"
                    res_dict["script_digest"] = script_v2.digest() if four_agent else ScriptScenePlanV2.from_dict({**res_dict, "script_digest": ""}).digest()
                    res_dict["project_id"] = proj_id
                    res_dict["research_dossier"] = dossier.to_dict() if hasattr(dossier, "to_dict") else dossier_dict
                    if isinstance(result, dict) and "scene_plan_id" in result:
                        res_dict["scene_plan_id"] = result["scene_plan_id"]
                    if four_agent and policy.get("preproduction_gate") == "observe" and policy.get("qc_level") != "low":
                        from video_factory.creative.agents.contracts import CreativeReview
                        from video_factory.creative.agents.critic import CreativeCritic, review_input_digest
                        from video_factory.creative.agents.gates import scored_preproduction_inputs

                        if policy.get("rubric_version") == "v2":
                            res_dict["rubric_version"] = "v2"
                            res_dict["director_config_version"] = policy.get("director_config_version", "legacy")

                        threshold = policy.get("preproduction_threshold", 80)
                        review_digest = res_dict["script_digest"]
                        try:
                            context, media = scored_preproduction_inputs(res_dict, threshold)
                            review_digest = review_input_digest("preproduction", context, media)
                            from video_factory.storage.layout import get_workspace_layout

                            review = CreativeCritic(
                                self.creative_generator.model,
                                receipt_root=get_workspace_layout().root / "preproduction_reviews" / ctx.workflow_id,
                            ).review(stage="preproduction", context=context, media=media)
                        except Exception as exc:
                            logging.getLogger(__name__).warning(
                                "Preproduction observation for %s failed: %s", ctx.workflow_id, type(exc).__name__
                            )
                            coverage = {"error_type": type(exc).__name__}
                            evaluator_receipt = {}
                            if policy.get("rubric_version") == "v2":
                                evaluator_receipt["rubric_version"] = "v2"
                                evaluator_receipt["director_config_version"] = res_dict.get("director_config_version")
                            review = CreativeReview("preproduction", "UNKNOWN", review_digest, (),
                                                    coverage, evaluator_receipt)
                        res_dict.update(
                            preproduction_review=review.to_dict(), preproduction_threshold=threshold,
                            review_input_digests={"preproduction": review.input_digest},
                        )
                    res = JobSucceeded(artifact_ref=res_dict, next_command={"command": "create_storyboard"})
                    if cache_key:
                        self._idempotency_cache[cache_key] = res
                    return res

            extra_scene_kw: dict[str, Any] = {}
            if kwargs.get("project_id"):
                extra_scene_kw["project_id"] = kwargs["project_id"]
            if kwargs.get("owner_user_id"):
                extra_scene_kw["owner_user_id"] = kwargs["owner_user_id"]
            if kwargs.get("approval_request_id"):
                extra_scene_kw["approval_request_id"] = kwargs["approval_request_id"]
            if kwargs.get("approval_receipt"):
                extra_scene_kw["approval_receipt"] = kwargs["approval_receipt"]

            result = self.video_client.create_scene_plan(
                brief_ref,
                workflow_id=ctx.workflow_id,
                job_id=ctx.job_id,
                idempotency_key=ctx.idempotency_key,
                **extra_scene_kw,
            )
            res = JobSucceeded(artifact_ref=result, next_command={"command": "create_storyboard"})
            if cache_key:
                self._idempotency_cache[cache_key] = res
            return res
        except Exception as e:
            return _creative_failure(e, alloc, "VF_SCENE_FAILED")


    def storyboard_job(self, ctx: JobContext, scene_ref: Any, **kwargs: Any) -> JobSucceeded | JobFailed:
        self._bind_creative_run(ctx)
        inputs = {"scene_ref": scene_ref}
        err = self._validate_state_and_version(
            ctx,
            {
                ProductVideoWorkflowState.CREATING_STORYBOARD,
                ProductVideoWorkflowState.AWAITING_SCENE_APPROVAL,
            },
            inputs,
            provider_family="video_factory",
        )
        if err:
            return err

        res_info = self._get_workflow_state_and_version(ctx)
        cur_ver = res_info[1] if res_info else 1
        cache_key = f"{ctx.workflow_id}:storyboard:v{cur_ver}:{ctx.idempotency_key}" if ctx.idempotency_key else None
        alt_key = f"{ctx.workflow_id}:storyboard:{ctx.idempotency_key}" if ctx.idempotency_key else None
        if cache_key and cache_key in self._idempotency_cache:
            return self._idempotency_cache[cache_key]
        if alt_key and alt_key in self._idempotency_cache:
            return self._idempotency_cache[alt_key]

        if not scene_ref:
            return JobFailed(retryable=False, code="MISSING_INPUT_REF", message="scene_ref cannot be empty")
        alloc = None
        try:
            proj_id = kwargs.get("project_id")
            if not proj_id and isinstance(scene_ref, dict):
                proj_id = scene_ref.get("project_id")
            if not proj_id and self.workflow_repository:
                try:
                    wf_d = self.workflow_repository.get_workflow(ctx.workflow_id)
                    if wf_d:
                        proj_id = wf_d[0].get("project_id")
                except Exception:
                    pass
            if not proj_id:
                proj_id = f"proj_{ctx.workflow_id}"

            appr_id = kwargs.get("approval_request_id") or (kwargs.get("approval_receipt") or {}).get("approval_request_id")

            if self.keyframe_runtime_factory is not None:
                from video_factory.creative.contracts import ResearchDossier, ScriptScenePlanV2
                from video_factory.creative.storyboard import (
                    build_keyframe_recovery_request,
                        bind_recovery_reference_revisions,
                    cascade_keyframe_qc_failures,
                    compile_keyframe_requests,
                    plan_initial_storyboard_recovery,
                    prepare_keyframe_generation_request,
                    resolve_reusable_storyboard_frames,
                        StoryboardContractError,
                    storyboard_package_digest,
                )
                dossier = getattr(self.creative_generator, "dossier", None)
                if dossier is None and isinstance(scene_ref, dict) and "research_dossier" in scene_ref:
                    dossier = ResearchDossier.from_dict(scene_ref["research_dossier"])
                if dossier and isinstance(scene_ref, dict):
                    eligible_ids = [asset.asset_id for asset in dossier.eligible_assets]
                    for scene in scene_ref.get("scenes", []) or []:
                        if isinstance(scene, dict) and not scene.get("reference_asset_ids"):
                            scene["reference_asset_ids"] = eligible_ids[:2]
                script = getattr(self.creative_generator, "script", None)
                if script is None and isinstance(scene_ref, dict):
                    try:
                        script = ScriptScenePlanV2.from_dict(scene_ref)
                    except (KeyError, TypeError, ValueError) as exc:
                        if scene_ref.get("schema_version") == "script-scene-plan-v2":
                            return JobFailed(
                                retryable=False, code="INVALID_SCRIPT_DIGEST",
                                message=f"Stored V2 scene plan failed validation: {exc}",
                            )
                if dossier and script:
                    bundle = profile = None
                    if isinstance(scene_ref, dict) and scene_ref.get('creative_mode') == 'four_agent':
                        from video_factory.creative.agents.contracts import BackendProfile, ScenePromptBundle
                        try:
                            bundle = ScenePromptBundle.from_dict(scene_ref['scene_prompts'])
                            profile = BackendProfile.from_dict(scene_ref['backend_profile'])
                        except ValueError as exc:
                            raise StoryboardContractError(str(exc)) from exc
                    if profile is not None and profile.image_provider_kind=='muse':
                        current=self.workflow_repository.get_workflow(ctx.workflow_id)
                        runtime=self.keyframe_runtime_factory(creative_policy=current[0]['creative_policy'])
                    else:
                        runtime = self.keyframe_runtime_factory()
                    if profile is not None and runtime.provider_kind != profile.image_provider_kind:
                        return JobFailed(retryable=False,code='CREATIVE_PROVIDER_MISMATCH',
                                         message='Image runtime differs from pinned run profile')
                    vf_storyboard = self.video_client.create_storyboard(
                        scene_ref,
                        workflow_id=ctx.workflow_id,
                        job_id=ctx.job_id,
                        project_id=proj_id,
                        owner_user_id=ctx.owner_user_id,
                        idempotency_key=ctx.idempotency_key,
                        approval_request_id=appr_id,
                        approval_receipt=kwargs.get("approval_receipt"),
                    )
                    try:
                        _confirm_vf_storyboard_write(
                            vf_storyboard,
                            owner_user_id=ctx.owner_user_id,
                            project_id=proj_id,
                            scene_ids=tuple(scene.scene_id for scene in script.scenes),
                        )
                    except ValueError as exc:
                        raise StoryboardContractError(str(exc)) from exc
                    recovery_plan = (
                        kwargs.get("recovery_plan")
                        if isinstance(kwargs.get("recovery_plan"), dict)
                        else None
                    )
                    try:
                        if recovery_plan and bundle is not None:
                            recovery_plan = bind_recovery_reference_revisions(
                                recovery_plan, script=script, prompt_bundle=bundle, dossier=dossier
                            )
                        requests = compile_keyframe_requests(
                            workflow_id=ctx.workflow_id,
                            project_id=proj_id,
                            dossier=dossier,
                            script=script,
                            prompt_bundle=bundle,
                            backend_profile=profile,
                            recovery_plan=recovery_plan,
                            recovery_attempt=int(kwargs.get("recovery_attempt", 0) or 0),
                            previous_package=(
                                kwargs.get("previous_storyboard")
                                if isinstance(kwargs.get("previous_storyboard"), dict)
                                else None
                            ),
                        )
                        recovery_frame_ids, feedback_map = plan_initial_storyboard_recovery(recovery_plan)
                    except ValueError as exc:
                        raise StoryboardContractError(str(exc)) from exc
                    previous_package = kwargs.get("previous_storyboard")
                    unchanged_scene_ids, previous_frames = resolve_reusable_storyboard_frames(
                        previous_package,
                        workflow_id=ctx.workflow_id,
                        project_id=proj_id,
                        script=script,
                    )
                    if previous_package and self.workflow_repository:
                        from video_factory.qc.models import QCStage
                        prior_report = QCReportRepository(self.workflow_repository.conn).get_latest_report(
                            ctx.workflow_id, QCStage.KEYFRAME,
                            expected_artifact_digest=str(previous_package.get("package_digest") or ""),
                        )
                        if prior_report:
                            report_items = {
                                str(item.get("frame_id") or ""): item
                                for item in (prior_report.findings.get("items") or [])
                                if isinstance(item, dict)
                            }
                            for frame_id, prior_frame in tuple(previous_frames.items()):
                                item = report_items.get(frame_id)
                                if (not item or item.get("verdict") != "pass"
                                        or item.get("hard_failure") is not False
                                        or not isinstance(item.get("findings"), dict)
                                        or item["findings"].get("identity_mismatch") is not False
                                        or str(item.get("sha256") or "") != str(prior_frame.get("sha256") or "")):
                                    continue
                                history = list(prior_frame.get("candidate_history") or [])
                                if not any(str(entry.get("sha256") or "") == str(item.get("sha256") or "")
                                           for entry in history if isinstance(entry, dict)):
                                    history.append({
                                        **{key: prior_frame.get(key) for key in (
                                            "frame_id", "scene_id", "frame_role", "image_path", "sha256",
                                            "reference_sha256s", "conditioning_start_sha256",
                                        )},
                                        "qc_verdict": "pass", "qc_score": item.get("score"),
                                        "qc_hard_failure": False, "qc_findings": item["findings"],
                                        "qc_receipt": {"report_id": prior_report.report_id,
                                            "artifact_digest": prior_report.artifact_digest},
                                        "qc_item": item,
                                    })
                                previous_frames[frame_id] = {**prior_frame, "candidate_history": history}
                    generated_keyframes = []
                    for req in requests:
                        prior = previous_frames.get(req.frame_id)
                        scene_id = req.frame_id.removesuffix("-start").removesuffix("-end")
                        if prior is not None and scene_id in unchanged_scene_ids and req.frame_id not in recovery_frame_ids:
                            prior_path = Path(str(prior.get("image_path") or ""))
                            if (
                                prior.get("status") == "completed"
                                and prior_path.is_file()
                                and hashlib.sha256(prior_path.read_bytes()).hexdigest() == prior.get("sha256")
                            ):
                                generated_keyframes.append(dict(prior))
                                continue
                        conditioning_start_sha = ""
                        conditioning_start_path = ""
                        if req.frame_role == "end":
                            start_frame = next(
                                (
                                    item for item in generated_keyframes
                                    if item.get("frame_id") == f"{scene_id}-start"
                                ),
                                None,
                            )
                            if not isinstance(start_frame, dict):
                                raise ValueError("END keyframe requires its completed START frame")
                            start_path = Path(str(start_frame.get("image_path") or ""))
                            conditioning_start_sha = str(start_frame.get("sha256") or "")
                            if (
                                not start_path.is_file()
                                or hashlib.sha256(start_path.read_bytes()).hexdigest() != conditioning_start_sha
                            ):
                                raise ValueError("END keyframe START conditioning image changed")
                            conditioning_start_path = str(start_path)
                        req = prepare_keyframe_generation_request(
                            req, feedback=feedback_map.get(req.frame_id, ""),
                            conditioning_start_path=conditioning_start_path,
                        )
                        if ctx.heartbeat_fn is not None and not ctx.heartbeat_fn():
                            return JobFailed(retryable=True, code="STALE_LEASE", message="Storyboard lease expired before keyframe generation")
                        generation_attempt = None
                        if profile is not None:
                            from video_factory.qc.retry_policy import claim_qc_attempt, complete_qc_attempt, ensure_qc_retry_schema
                            connection = runtime.store.connection
                            ensure_qc_retry_schema(connection)
                            prior_op = runtime.store.get(req.operation_id)
                            count = connection.execute("SELECT COUNT(*) FROM workflow_qc_retries WHERE workflow_id=? AND retry_type='four_agent_image_send'",
                                (ctx.workflow_id,)).fetchone()[0]
                            current=self.workflow_repository.get_workflow(ctx.workflow_id) if self.workflow_repository else None
                            image_limit=min(24,current[0].get('creative_policy',{}).get('image_call_limit',24)) if current else 24
                            if prior_op is None and count >= image_limit:
                                return JobFailed(retryable=False,code='CREATIVE_IMAGE_CALL_CAP',message='Pilot image call cap reached')
                            generation_attempt = claim_qc_attempt(connection,workflow_id=ctx.workflow_id,retry_type='four_agent_image_send',
                                item_id=req.frame_id,operation_id=req.operation_id,
                                input_hash=hashlib.sha256(req.prompt.encode()).hexdigest(),max_recoveries=2)
                        kf_res = runtime.run(req)
                        if generation_attempt is not None:
                            complete_qc_attempt(connection,attempt_id=generation_attempt['attempt_id'],
                                status='completed' if kf_res.status=='completed' else 'technical_unknown',
                                verdict='generated' if kf_res.status=='completed' else 'unknown')
                        if kf_res.status != "completed" or not kf_res.image_path:
                            if kf_res.error_code == "PROVIDER_CAPACITY_WAIT":
                                return JobFailed(
                                    retryable=True, code="PROVIDER_CAPACITY_WAIT",
                                    message=kf_res.error_message or "Keyframe provider is at capacity",
                                )
                            if str(kf_res.error_code or "").upper() in {
                                "HTTP_429", "HTTP_500", "HTTP_502", "HTTP_503", "HTTP_504",
                            }:
                                return JobFailed(
                                    retryable=True, code=str(kf_res.error_code).upper(),
                                    message=kf_res.error_message or (
                                        f"Keyframe provider returned {kf_res.error_code}; retry later"
                                    ),
                                )
                            if kf_res.status in {"submitted", "running"} and kf_res.provider_operation_id:
                                return JobFailed(
                                    retryable=True, code="KEYFRAME_PENDING",
                                    message="Keyframe operation is pending under its existing provider ID",
                                )
                            return JobFailed(
                                retryable=False,
                                code=kf_res.error_code or "RECONCILIATION_REQUIRED",
                                message=kf_res.error_message or "Keyframe operation did not complete",
                            )
                        ref_shas = [
                            hashlib.sha256(Path(path).read_bytes()).hexdigest()
                            for path in req.reference_image_paths
                        ]
                        scene_id = req.frame_id.removesuffix("-start").removesuffix("-end")
                        kf_p = Path(kf_res.image_path) if kf_res.image_path else None
                        kf_sha = hashlib.sha256(kf_p.read_bytes()).hexdigest() if (kf_p and kf_p.is_file()) else ""
                        generated_keyframes.append({
                            "frame_id": req.frame_id,
                            "scene_id": scene_id,
                            "frame_role": req.frame_role,
                            "image_path": kf_res.image_path,
                            "sha256": kf_sha,
                            "status": kf_res.status,
                            "prompt": req.prompt,
                            "negative_prompt": req.negative_prompt,
                            "reference_image_paths": list(req.reference_image_paths),
                            "reference_sha256s": ref_shas,
                            "conditioning_start_sha256": conditioning_start_sha,
                            "candidate_history": [
                                entry for entry in (prior.get("candidate_history") or [])
                                if scene_id in unchanged_scene_ids and isinstance(entry, dict)
                                and entry.get("reference_sha256s") == ref_shas
                                and str(entry.get("conditioning_start_sha256") or "") == conditioning_start_sha
                            ] if prior else [],
                        })
                    package_dict = {
                        "schema_version": "storyboard-package-v2",
                        "storyboard_id": f"sb_{proj_id}",
                        "version": 1,
                        "owner_user_id": ctx.owner_user_id,
                        "generated_keyframes": generated_keyframes,
                        "storyboard_frames": [
                            {
                                "scene_id": scene.scene_id,
                                "keyframe_image_path": next(
                                    (
                                        item["image_path"]
                                        for item in generated_keyframes
                                        if item["scene_id"] == scene.scene_id
                                        and item["frame_role"] == "start"
                                    ),
                                    None,
                                ),
                                "reference_image_paths": list(
                                    next(
                                        (
                                            req.reference_image_paths
                                            for req in requests
                                            if req.frame_id == f"{scene.scene_id}-start"
                                        ),
                                        (),
                                    )
                                ),
                                "visual_objective": scene.visual_objective,
                                "overlay_text": scene.overlay_text,
                                "text_policy": "post_production_only",
                            }
                            for scene in script.scenes
                        ],
                        "project_id": proj_id,
                        "workflow_id": ctx.workflow_id,
                        "script": script.to_dict() if hasattr(script, "to_dict") else (asdict(script) if is_dataclass(script) else script),
                        "scenes": [s.to_dict() if hasattr(s, "to_dict") else (asdict(s) if is_dataclass(s) else s) for s in script.scenes],
                        "recovery_attempt": int(kwargs.get("recovery_attempt", 0) or 0),
                        "end_keyframe_omissions": [
                            {
                                "scene_id": s.scene_id,
                                "policy": s.end_keyframe_policy,
                                "reason": s.end_keyframe_omission_reason,
                            }
                            for s in script.scenes
                            if s.end_keyframe_policy == "omitted"
                        ],
                    }
                    if profile is not None:
                        package_dict.update({key:scene_ref[key] for key in (
                            'creative_mode','scene_prompts','backend_profile')})
                        if recovery_plan:
                            package_dict['recovery_plan'] = recovery_plan
                        package_dict['research_dossier'] = dossier.to_dict()
                        current=self.workflow_repository.get_workflow(ctx.workflow_id) if self.workflow_repository else None
                        package_dict['video_call_limit']=min(12,current[0].get('creative_policy',{}).get('video_call_limit',12)) if current else 12
                        package_dict['repair_history']=scene_ref.get('repair_history',[])
                        if current and current[0].get('creative_policy'):
                            package_dict['qc_level'] = current[0]['creative_policy'].get('qc_level', 'middle')
                    package_dict["package_digest"] = storyboard_package_digest(package_dict)

                    # Governed Keyframe QC runs after this package is persisted by the
                    # dispatcher; it owns the durable workflow-wide quality cycle ledger.
                    res = JobSucceeded(artifact_ref=package_dict, next_command={"command": "render_video"})
                    if cache_key:
                        self._idempotency_cache[cache_key] = res
                    return res

            extra_sb_kw: dict[str, Any] = {}
            if kwargs.get("project_id"):
                extra_sb_kw["project_id"] = kwargs["project_id"]
            if kwargs.get("owner_user_id"):
                extra_sb_kw["owner_user_id"] = kwargs["owner_user_id"]
            if appr_id:
                extra_sb_kw["approval_request_id"] = appr_id
            if isinstance(kwargs.get("approval_receipt"), dict):
                extra_sb_kw["approval_receipt"] = kwargs["approval_receipt"]

            result = self.video_client.create_storyboard(
                scene_ref,
                workflow_id=ctx.workflow_id,
                job_id=ctx.job_id,
                idempotency_key=ctx.idempotency_key,
                **extra_sb_kw,
            )
            res = JobSucceeded(artifact_ref=result, next_command={"command": "render_video"})
            if cache_key:
                self._idempotency_cache[cache_key] = res
            return res
        except StoryboardContractError as e:
            return JobFailed(retryable=False, code="STORYBOARD_INPUT_INVALID", message=str(e))
        except Exception as e:
            return JobFailed(retryable=True, code="VF_STORYBOARD_FAILED", message=str(e))


    # Methods from render_handler.py
    def render_job(self, ctx: JobContext, story_ref: Any, render_params: dict | None = None, **kwargs: Any) -> JobSucceeded | JobFailed:
        inputs = {"story_ref": story_ref}
        err = self._validate_state_and_version(
            ctx,
            {
                ProductVideoWorkflowState.RENDERING,
                ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL,
            },
            inputs,
            provider_family="video_factory",
        )
        if err:
            return err
        if not story_ref:
            return JobFailed(retryable=False, code="MISSING_INPUT_REF", message="story_ref cannot be empty")

        params = dict(render_params or {})
        if isinstance(story_ref,dict) and story_ref.get('creative_mode')=='four_agent':
            pinned_kind = story_ref['backend_profile']['provider_kind']
            if params.get('provider_kind',pinned_kind)!=pinned_kind:
                return JobFailed(retryable=False,code='CREATIVE_PROVIDER_MISMATCH',message='Render provider differs from run profile')
            params['provider_kind']=pinned_kind
        wf_data = self.workflow_repository.get_workflow(ctx.workflow_id) if self.workflow_repository else None
        agg = wf_data[0] if wf_data else {}
        cur_ver = wf_data[1] if wf_data else 1
        try:
            active_job_id = _resolve_durable_resume_job_id(
                params,
                agg,
                "active_render_job_id",
                ("job_id", "export_id"),
            )
        except TerminalJobError as exc:
            return JobFailed(retryable=False, code=exc.code, message=str(exc))

        cache_key = f"{ctx.workflow_id}:render:v{cur_ver}:{ctx.idempotency_key}" if ctx.idempotency_key else None
        alt_key = f"{ctx.workflow_id}:render:{ctx.idempotency_key}" if ctx.idempotency_key else None
        if cache_key and cache_key in self._idempotency_cache:
            return self._idempotency_cache[cache_key]
        if alt_key and alt_key in self._idempotency_cache:
            return self._idempotency_cache[alt_key]

        is_four_agent = (
            (isinstance(story_ref, dict) and story_ref.get("creative_mode") == "four_agent")
            or agg.get("creative_mode") == "four_agent"
            or agg.get("creative_policy", {}).get("creative_mode") == "four_agent"
        )

        disp_status = agg.get("render_dispatch_status")
        if disp_status in ("DISPATCHING", "UNKNOWN_OUTCOME"):
            return JobFailed(retryable=False, code="UNKNOWN_PAID_OUTCOME", message=f"Cannot resume from unconfirmed dispatch status '{disp_status}'")
        if disp_status == "COMPLETED":
            if not is_four_agent:
                res_id = agg.get("render_budget_reservation_id")
                if not res_id or res_id == "absent":
                    return JobFailed(retryable=False, code="BUDGET_RECOVERY_FAILED", message="Completed render missing valid budget reservation")
                res_obj = get_reservation(res_id)
                if not res_obj:
                    return JobFailed(retryable=False, code="BUDGET_RECOVERY_FAILED", message=f"Reservation '{res_id}' not found")
                commit_budget(res_obj)
            return JobSucceeded(artifact_ref=agg.get("render_result", {}), next_command={"command": "export_video"})

        if getattr(self, "provider_mode", "fake") == "configured" and not active_job_id:
            try:
                _require_configured_execution_binding(
                    self.video_client,
                    "render",
                    "video.render",
                )
            except TerminalJobError as exc:
                return JobFailed(
                    retryable=False,
                    code=exc.code,
                    message=str(exc),
                )
            except ExecutionBindingPreflightError as exc:
                return JobFailed(
                    retryable=exc.retryable,
                    code=exc.code,
                    message=str(exc),
                )
            if not is_four_agent:
                qc_auth = kwargs.get("qc_authorization") or params.get("qc_authorization") or agg.get("qc_authorization")
                if not qc_auth or not isinstance(qc_auth, dict):
                    return JobFailed(retryable=False, code="COST_AUTHORIZATION_REQUIRED", message="Configured render requires scoped qc_authorization")
                max_cost = qc_auth.get("max_cost_usd", qc_auth.get("workflow_max_cost_usd"))
                if max_cost is None or float(max_cost) < 0.50:
                    return JobFailed(retryable=False, code="COST_AUTHORIZATION_REQUIRED", message=f"Authorization max_cost_usd {max_cost} is below durable admission threshold (0.50)")
                if self.workflow_repository is not None:
                    try:
                        render_sends_used = _paid_render_send_count(self.workflow_repository.conn, ctx.workflow_id)
                    except Exception as exc:
                        return JobFailed(
                            retryable=False,
                            code="RENDER_ATTEMPT_LEDGER_UNAVAILABLE",
                            message=f"Cannot verify the five-send pilot limit before render: {type(exc).__name__}",
                        )
                    if render_sends_used >= 5:
                        return JobFailed(
                            retryable=False,
                            code="PAID_RENDER_SEND_LIMIT_REACHED",
                            message=f"The five-send paid render pilot limit is reached ({render_sends_used}/5); reconcile or stop this workflow before another send.",
                        )

        def _checkpoint_accepted(job_type: str, prov_job_id: str) -> None:
            nonlocal active_job_id
            if not self.workflow_repository or not ctx.workflow_id:
                return
            try:
                data = self.workflow_repository.get_workflow(ctx.workflow_id)
                if not data:
                    raise RuntimeError(f"Workflow '{ctx.workflow_id}' not found during checkpoint")
                agg_data, ver = data
                agg_dict = dict(agg_data)
                agg_dict["active_render_job_id"] = prov_job_id
                if alloc:
                    agg_dict["render_budget_reservation_id"] = alloc.reservation_id
                self.workflow_repository.commit_workflow(
                    workflow_id=ctx.workflow_id,
                    aggregate_json=json.dumps(agg_dict),
                    events=[],
                    expected_version=ver,
                )
                active_job_id = prov_job_id
            except Exception as ex:
                raise TerminalJobError(
                    f"CHECKPOINT_PERSISTENCE_FAILED: Failed to checkpoint accepted provider job_id '{prov_job_id}' in workflow repository: {ex}. "
                    "Auto-resubmission halted to prevent duplicate provider job submission.",
                    code="VF_CHECKPOINT_FAILED",
                ) from ex

        alloc = None
        provider_call_started = False
        try:
            proj_id = kwargs.get("project_id") or agg.get("project_id") or f"proj_{ctx.workflow_id}"
            if not active_job_id:
                if not is_four_agent:
                    alloc = reserve_budget(
                        "video_factory",
                        50,
                        idempotency_key=cache_key,
                        **_paid_budget_kwargs(ctx, "product_video.render", kwargs, cache_key),
                    )
                    params["admission_context"] = {
                        "reservation_id": alloc.reservation_id,
                        "budget_reservation_id": alloc.reservation_id,
                        "workflow_id": ctx.workflow_id,
                    }
                else:
                    params["creative_mode"] = "four_agent"
            elif agg.get("render_budget_reservation_id"):
                if not is_four_agent:
                    alloc = get_reservation(agg["render_budget_reservation_id"])
                    if alloc is None or alloc.state == "released":
                        return JobFailed(
                            retryable=False, code="BUDGET_RECOVERY_FAILED",
                            message="Accepted render budget reservation is missing or released",
                        )
            appr_id = kwargs.get("approval_request_id") or (kwargs.get("approval_receipt") or {}).get("approval_request_id")
            story_dict = dict(story_ref) if isinstance(story_ref, dict) else {}
            if "scenes" not in story_dict and isinstance(agg.get("scene_plan"), dict) and agg["scene_plan"].get("scenes"):
                story_dict["scenes"] = agg["scene_plan"]["scenes"]
            workflow_data = self.workflow_repository.get_workflow(ctx.workflow_id) if self.workflow_repository else None
            creative_policy = (workflow_data[0].get("creative_policy") or {}) if workflow_data else {}
            qc_level = creative_policy.get("qc_level", "middle")
            story_dict["qc_level"] = qc_level
            story_dict["disable_keyframe_qc"] = bool(creative_policy.get("disable_keyframe_qc"))
            if story_dict.get("schema_version") == "storyboard-package-v2":
                curr_rcpt = story_dict.get("keyframe_qc_receipt")
                if not isinstance(curr_rcpt, dict) or curr_rcpt.get("artifact_digest") != story_dict.get("package_digest"):
                    qc_repo = QCReportRepository(self.workflow_repository.conn)
                    latest_qc = qc_repo.get_latest_report(ctx.workflow_id, QCStage.KEYFRAME)
                    if latest_qc and getattr(latest_qc.verdict, "value", str(latest_qc.verdict)).lower() == "pass":
                        story_dict["keyframe_qc_receipt"] = {
                            "report_id": latest_qc.report_id,
                            "workflow_id": latest_qc.workflow_id,
                            "stage": getattr(latest_qc.stage, "value", str(latest_qc.stage)),
                            "verdict": getattr(latest_qc.verdict, "value", str(latest_qc.verdict)),
                            "score": latest_qc.score,
                            "artifact_id": latest_qc.artifact_id,
                            "artifact_version": latest_qc.artifact_version,
                            "artifact_digest": latest_qc.artifact_digest,
                            "findings": latest_qc.findings,
                            "hard_failure": getattr(latest_qc, "hard_failure", False),
                        }
                    elif getattr(self, "provider_mode", "fake") == "configured" and qc_level != "low":
                        if alloc:
                            release_budget(alloc)
                        return JobFailed(
                            retryable=False,
                            code="KEYFRAME_QC_REQUIRED",
                            message="Video render rejected: keyframes have not passed Keyframe QC",
                        )
                    elif not curr_rcpt:
                        story_dict["keyframe_qc_receipt"] = kwargs.get("approval_receipt") or {
                            "approval_request_id": appr_id or "appr_auto",
                            "gate": "AwaitingStoryboardApproval",
                            "status": "approved",
                        }
                keyframe_receipt = story_dict.get("keyframe_qc_receipt")
                if (isinstance(keyframe_receipt, dict)
                        and keyframe_receipt.get("artifact_digest") == story_dict.get("package_digest")):
                    selected = ([] if qc_level == "low" else
                                (keyframe_receipt.get("findings") or {}).get("selected_candidates") or [])
                    generated = [dict(frame) for frame in story_dict.get("generated_keyframes", [])
                                 if isinstance(frame, dict)]
                    for choice in selected:
                        if not isinstance(choice, dict):
                            continue
                        path = Path(str(choice.get("image_path") or ""))
                        expected_sha = str(choice.get("sha256") or "").removeprefix("sha256:")
                        if (not path.is_file() or not expected_sha
                                or hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha):
                            raise TerminalJobError(
                                f"Selected keyframe candidate changed after QC: {choice.get('frame_id')}",
                                code="KEYFRAME_SELECTION_STALE",
                            )
                        for index, frame in enumerate(generated):
                            if frame.get("frame_id") == choice.get("frame_id"):
                                generated[index] = {**frame, "image_path": str(path), "sha256": expected_sha}
                    story_dict["generated_keyframes"] = generated
                    for storyboard_frame in story_dict.get("storyboard_frames", []):
                        if not isinstance(storyboard_frame, dict):
                            continue
                        scene_id = str(storyboard_frame.get("scene_id") or "")
                        start = next((frame for frame in generated
                                      if frame.get("frame_id") == f"{scene_id}-start"), None)
                        if start:
                            storyboard_frame["keyframe_image_path"] = start["image_path"]
            provider_call_started = True
            result = self.video_client.render_video(
                story_dict,
                render_params=params,
                workflow_id=ctx.workflow_id,
                job_id=ctx.job_id,
                idempotency_key=ctx.idempotency_key,
                on_job_accepted=_checkpoint_accepted,
                heartbeat_fn=ctx.heartbeat_fn,
                owner_user_id=ctx.owner_user_id,
                project_id=proj_id,
                approval_request_id=appr_id,
            )

            if alloc:
                commit_budget(alloc)
            res = JobSucceeded(artifact_ref=result, next_command={"command": "export_video"})
            if cache_key:
                self._idempotency_cache[cache_key] = res
            return res
        except JobDeferred as deferred:
            # Keep the accepted operation and its budget reservation for continuation.
            if active_job_id:
                raise
            return JobFailed(
                retryable=False, code="UNKNOWN_PAID_OUTCOME",
                message=f"Render deferred without a durable provider job; reconcile before retrying: {deferred}",
            )
        except TerminalJobError as te:
            if getattr(te, 'artifact_ref', None) and te.code.startswith('CLIP_TECHNICAL_'):
                if alloc:
                    commit_budget(alloc)
                return JobFailed(retryable=False, code=te.code, message=str(te), artifact_ref=te.artifact_ref)
            if getattr(te,'artifact_ref',None) and te.code == 'FINAL_TECHNICAL_FAIL':
                from video_factory.creative.contracts import canonical_digest
                failure=te.artifact_ref
                assessment=failure['assessment']
                review={'digest':canonical_digest({'code':te.code,'sha256':assessment.get('sha256'),
                    'technical_qc':assessment.get('technical_qc')})}
                repaired={'assembly_repair':{'changes':{'rebuild':True},
                    'reason':assessment.get('technical_qc',{}).get('reason') or 'Rebuild final from completed scene clips'},
                    'review_digest':review['digest']}
                if alloc:
                    commit_budget(alloc)
                return JobFailed(retryable=False,code=te.code,message=str(te),artifact_ref={
                    'creative_repair':repaired,'review':review,'assessment':assessment})
            if getattr(te,'artifact_ref',None) and te.code in {'CLIP_CONTENT_FAIL','FINAL_CONTENT_FAIL','CLIP_CONTENT_UNKNOWN','FINAL_CONTENT_UNKNOWN'}:
                from video_factory.creative.agents.contracts import CreativeReview
                from video_factory.creative.contracts import ResearchDossier, ScriptScenePlanV2
                failure=te.artifact_ref
                assessment=failure['assessment']
                review=CreativeReview.from_dict(assessment.get('clip_review') or assessment['final_review'])
                repaired=None
                if review.verdict=='FAIL' and self.workflow_repository:
                    self._bind_creative_run(ctx)
                    current=self.workflow_repository.get_workflow(ctx.workflow_id)[0]
                    if review.digest not in current.get('creative_applied_repairs',[]):
                        repaired=self.creative_generator.prepare_targeted_repair(
                            dossier=ResearchDossier.from_dict(failure['package']['research_dossier']),
                            script=ScriptScenePlanV2.from_dict(failure['package']['script']),
                            scene_ref=current['scene_plan'],review=review,connection=self.workflow_repository.conn)
                if repaired and repaired.get('repair_unknown'):
                    review=CreativeReview.from_dict(repaired['review'])
                if alloc:
                    commit_budget(alloc)  # Completed provider media was paid even when content failed.
                terminal_code=te.code
                if review.verdict=='UNKNOWN':
                    terminal_code=terminal_code.replace('_FAIL','_UNKNOWN')
                return JobFailed(retryable=False,code=terminal_code,message=str(te),artifact_ref={
                    'creative_repair':None if review.verdict=='UNKNOWN' else repaired,
                    'review':review.to_dict(),'assessment':assessment})
            uncertain_paid_codes = {
                "RECONCILIATION_REQUIRED",
                "GOOGLE_FLOW_VIDEO_ACCEPTANCE_UNKNOWN",
                "UNKNOWN_PAID_OUTCOME",
                "VF_CHECKPOINT_FAILED",
                "MISSING_JOB_ID",
                "MISSING_POLLING_TOOL",
                "RENDER_LINEAGE_INVALID",
                "RENDER_LINEAGE_MISMATCH",
            }
            if alloc and not (
                provider_call_started
                and self.provider_mode == "configured"
                and te.code in uncertain_paid_codes
            ):
                try:
                    release_budget(alloc)
                except Exception:
                    pass
            return JobFailed(retryable=False, code=te.code, message=str(te))
        except LeaseLostError as le:
            if alloc and not (provider_call_started and self.provider_mode == "configured"):
                try:
                    release_budget(alloc)
                except Exception:
                    pass
            return JobFailed(retryable=False, code="LEASE_LOST", message=str(le))
        except Exception as e:
            if provider_call_started and self.provider_mode == "configured":
                durable_job = None
                if self.workflow_repository:
                    try:
                        resumed = self.workflow_repository.get_workflow(ctx.workflow_id)
                        durable_job = resumed[0].get("active_render_job_id") if resumed else None
                    except Exception:
                        durable_job = None
                if not durable_job:
                    return JobFailed(
                        retryable=False, code="UNKNOWN_PAID_OUTCOME",
                        message=f"Render outcome is ambiguous; reconcile the provider before retrying: {e}",
                    )
                return JobFailed(retryable=True, code="VF_RENDER_FAILED", message=str(e))
            if alloc:
                try:
                    release_budget(alloc)
                except Exception:
                    pass
            return JobFailed(retryable=True, code="VF_RENDER_FAILED", message=str(e))


    # Methods from export_handler.py
    def export_job(self, ctx: JobContext, render_ref: Any, export_params: dict | None = None, **kwargs: Any) -> JobSucceeded | JobFailed:
        inputs = {"render_ref": render_ref}
        err = self._validate_state_and_version(
            ctx,
            {
                ProductVideoWorkflowState.EXPORTING,
                ProductVideoWorkflowState.AWAITING_FINAL_APPROVAL,
            },
            inputs,
            provider_family="video_factory",
        )
        if err:
            return err
        if not render_ref:
            return JobFailed(retryable=False, code="MISSING_INPUT_REF", message="render_ref cannot be empty")

        params = dict(export_params or {})
        wf_data = self.workflow_repository.get_workflow(ctx.workflow_id) if self.workflow_repository else None
        agg = wf_data[0] if wf_data else {}
        cur_ver = wf_data[1] if wf_data else 1
        try:
            active_job_id = _resolve_durable_resume_job_id(
                params,
                agg,
                "active_export_job_id",
                ("job_id", "export_id"),
            )
        except TerminalJobError as exc:
            return JobFailed(retryable=False, code=exc.code, message=str(exc))

        cache_key = f"{ctx.workflow_id}:export:v{cur_ver}:{ctx.idempotency_key}" if ctx.idempotency_key else None
        alt_key = f"{ctx.workflow_id}:export:{ctx.idempotency_key}" if ctx.idempotency_key else None
        if cache_key and cache_key in self._idempotency_cache:
            return self._idempotency_cache[cache_key]
        if alt_key and alt_key in self._idempotency_cache:
            return self._idempotency_cache[alt_key]

        disp_status = agg.get("export_dispatch_status")
        if disp_status in ("DISPATCHING", "UNKNOWN_OUTCOME"):
            return JobFailed(retryable=False, code="UNKNOWN_PAID_OUTCOME", message=f"Cannot resume from unconfirmed dispatch status '{disp_status}'")
        if disp_status == "COMPLETED":
            # Settle reservations from older exports without charging new local exports.
            res_id = agg.get("export_budget_reservation_id")
            if res_id:
                res_obj = get_reservation(res_id)
                if not res_obj:
                    return JobFailed(retryable=False, code="BUDGET_RECOVERY_FAILED", message=f"Reservation '{res_id}' not found")
                commit_budget(res_obj)
            return JobSucceeded(artifact_ref=agg.get("export_result", {}), next_command={"command": "complete"})

        def _checkpoint_accepted(job_type: str, prov_job_id: str) -> None:
            if not self.workflow_repository or not ctx.workflow_id:
                return
            try:
                data = self.workflow_repository.get_workflow(ctx.workflow_id)
                if not data:
                    raise RuntimeError(f"Workflow '{ctx.workflow_id}' not found during checkpoint")
                agg_data, ver = data
                agg_dict = dict(agg_data)
                agg_dict["active_export_job_id"] = prov_job_id
                self.workflow_repository.commit_workflow(
                    workflow_id=ctx.workflow_id,
                    aggregate_json=json.dumps(agg_dict),
                    events=[],
                    expected_version=ver,
                )
            except Exception as ex:
                raise TerminalJobError(
                    f"CHECKPOINT_PERSISTENCE_FAILED: Failed to checkpoint accepted provider job_id '{prov_job_id}' in workflow repository: {ex}. "
                    "Auto-resubmission halted to prevent duplicate provider job submission.",
                    code="VF_CHECKPOINT_FAILED",
                ) from ex

        alloc = None
        try:
            proj_id = kwargs.get("project_id") or agg.get("project_id") or f"proj_{ctx.workflow_id}"
            appr_id = kwargs.get("approval_request_id") or (kwargs.get("approval_receipt") or {}).get("approval_request_id")
            if getattr(self, "provider_mode", "fake") == "configured" and not active_job_id:
                try:
                    _require_configured_execution_binding(
                        self.video_client,
                        "export",
                        "video.export",
                    )
                except TerminalJobError as exc:
                    return JobFailed(
                        retryable=False,
                        code=exc.code,
                        message=str(exc),
                    )
                except ExecutionBindingPreflightError as exc:
                    return JobFailed(
                        retryable=exc.retryable,
                        code=exc.code,
                        message=str(exc),
                    )
            result = self.video_client.export_video(
                render_ref,
                export_params=params,
                workflow_id=ctx.workflow_id,
                job_id=ctx.job_id,
                idempotency_key=ctx.idempotency_key,
                on_job_accepted=_checkpoint_accepted,
                heartbeat_fn=ctx.heartbeat_fn,
                owner_user_id=ctx.owner_user_id,
                project_id=proj_id,
                approval_request_id=appr_id,
            )

            res = JobSucceeded(artifact_ref=result, next_command={"command": "complete"})
            if cache_key:
                self._idempotency_cache[cache_key] = res
            return res
        except TerminalJobError as te:
            if alloc:
                try:
                    release_budget(alloc)
                except Exception:
                    pass
            return JobFailed(retryable=False, code=te.code, message=str(te))
        except LeaseLostError as le:
            if alloc:
                try:
                    release_budget(alloc)
                except Exception:
                    pass
            return JobFailed(retryable=False, code="LEASE_LOST", message=str(le))
        except Exception as e:
            if alloc:
                try:
                    release_budget(alloc)
                except Exception:
                    pass
            return JobFailed(retryable=True, code="VF_EXPORT_FAILED", message=str(e))


    def get_handlers_map(self) -> dict[str, Callable[..., Any]]:
        return {
            "product_video.research": self.research_job,
            "product_video.resource_pack": self.resource_pack_job,
            "product_video.retry_image_qc": self.retry_image_qc_job,
            "product_video.lock": self.lock_job,
            "product_video.creative_brief": self.creative_brief_job,
            "product_video.scene_plan": self.scene_plan_job,
            "product_video.storyboard": self.storyboard_job,
            "product_video.render": self.render_job,
            "product_video.export": self.export_job,
        }

    def __len__(self) -> int:
        return len(self.get_handlers_map())

    def __getitem__(self, item: str) -> Callable[..., Any]:
        return self.get_handlers_map()[item]

    def __contains__(self, item: str) -> bool:
        return item in self.get_handlers_map()

    def dispatch_registered_job(self, job_name: str, ctx: JobContext, *args: Any, **kwargs: Any) -> JobResult:
        """Create a Gate 4 JobSpec and dispatch it through JobDispatcher."""
        handlers = self.get_handlers_map()
        if job_name not in handlers:
            raise ValueError(f"Unknown job handler: {job_name}")
        handler_fn = handlers[job_name]
        spec = JobSpec(
            job_id=ctx.job_id,
            func=handler_fn,
            args=(ctx, *args),
            kwargs=kwargs,
        )
        if not self.dispatcher:
            raise RuntimeError("No JobDispatcher attached to ProductVideoJobHandlers")
        return self.dispatcher.dispatch(spec)

    def wait_for_job(self, job_id: str, timeout: float = 2.0) -> JobResult | None:
        """Wait for a dispatched job to reach terminal state."""
        if not self.dispatcher:
            return None
        start = time.time()
        while time.time() - start < timeout:
            res = self.dispatcher.get_result(job_id)
            if res and res.status in (JobStatus.SUCCESS, JobStatus.FAILURE):
                return res
            time.sleep(0.01)
        return self.dispatcher.get_result(job_id)


