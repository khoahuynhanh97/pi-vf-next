"""Unified ProductVideoCoordinator for Scheduler, CLI, and REST entry points."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from video_factory.application.operator_handler import approve_and_advance_workflow
from video_factory.automation.failure_classification import FailureCategory, classify_failure
from video_factory.handlers import (
    ProductVideoWorkflowState,
    start_product_video_workflow,
)
from video_factory.workflows.repository import WorkflowRepository
from video_factory.workflows.envelope import build_envelope
from video_factory.workflows.worker import run_worker

from .contracts import (
    BatchStartResult,
    CandidateItem,
    ExchangeProduct,
    SkippedProduct,
    StartedProduct,
)
from .importer import (
    ExchangeImporter,
)
from .repository import DailyImportRepository


def _is_outbox_lease_active(lease_expires_at: str | None, now: datetime | None = None) -> bool:
    """Check if an outbox lease expiration timestamp in ISO-8601 format is in the future."""
    if not lease_expires_at or not str(lease_expires_at).strip():
        return False
    try:
        raw = str(lease_expires_at).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        current = now if now is not None else datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return dt > current
    except (ValueError, TypeError):
        return False


class ProductVideoCoordinator:
    """The single coordinator for Daily Product Selection, workflow starting, and approvals.

    All entry points (Windows Task Scheduler, CLI commands, and REST endpoints)
    must call through this coordinator to ensure identical business logic,
    strict idempotency, and atomic claim protection.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        inbox_root: Path | None = None,
        db_path: Path | None = None,
        catalog_candidates_path: Path | None = None,
    ) -> None:
        self.connection = connection
        self.importer = ExchangeImporter(inbox_root)
        self.import_repo = DailyImportRepository(connection)
        self.workflow_repo = WorkflowRepository(connection)
        self.db_path = db_path
        self.catalog_candidates_path = Path(catalog_candidates_path) if catalog_candidates_path else None

    def _pause_blocks_selection(self, pause: dict[str, Any] | None,
                                selected_product_keys: set[str] | None = None) -> bool:
        if not pause:
            return False
        from video_factory.workflows.runtime_control import is_shared_infra_blocker
        if is_shared_infra_blocker(pause.get("code")):
            return True
        paused_workflow = str(pause.get("workflow_id") or "")
        if not paused_workflow:
            return True
        if selected_product_keys is None:
            return False
        row = self.connection.execute(
            "SELECT aggregate_json FROM workflow WHERE workflow_id=?", (paused_workflow,)
        ).fetchone()
        if not row:
            return True
        try:
            aggregate = json.loads(row[0])
        except (TypeError, ValueError):
            return True
        source = aggregate.get("source_context") or {}
        identities = {
            str(value).casefold() for value in (
                source.get("canonical_product_key"), source.get("product_key"),
                aggregate.get("product_key"),
            ) if value
        }
        return bool(identities & {str(value).casefold() for value in selected_product_keys})

    def _resume_reconciled_image_qc(
        self,
        workflow_id: str,
        *,
        owner_user_id: str,
        qc_authorization: dict[str, Any] | None,
        qc_max_cost_usd: float | None,
        max_qc_retries: int | None,
    ) -> bool:
        """Rebuild a missing pre-lock resource pack after an operator reconciles QC."""
        workflow = self.workflow_repo.get_workflow(workflow_id)
        if workflow is None:
            return False
        aggregate, version = workflow
        if not (
            aggregate.get("state") == ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
            and aggregate.get("operator_action") in {
                "qc_retry_authorization", "qc_authorization", "qc_reconciliation", "qc_provider_retry",
            }
            and aggregate.get("operator_stage") == "image"
            and aggregate.get("failure_code") in {
                "QC_OUTCOME_UNCERTAIN",
                "QC_QUOTA_EXHAUSTED",
                "INSUFFICIENT_BUDGET",
            }
            and isinstance(aggregate.get("research_result"), dict)
            and isinstance(aggregate.get("source_context"), dict)
            and aggregate["source_context"].get("source") == "hermes_product_video_exchange"
        ):
            return False

        # QC retries use the durable attempt ledger, not a money authorization.
        # A supplied authorization is retained only for later paid generation.
        auth = dict(qc_authorization or {})
        auth_id = str(auth.get("authorization_id") or auth.get("run_id") or "").strip()

        reconciliations = aggregate.get("qc_reconciliations")
        if not isinstance(reconciliations, list):
            return False
        reconciliation = next((
            item for item in reversed(reconciliations)
            if isinstance(item, dict)
            and item.get("outcome") in {"consumed", "not_sent"}
            and (
                aggregate.get("failure_code") in {"QC_QUOTA_EXHAUSTED", "INSUFFICIENT_BUDGET"}
                or not item.get("resource_pack_resume_scheduled")
            )
        ), None)
        if reconciliation is None:
            return False
        if aggregate.get("failure_code") == "QC_QUOTA_EXHAUSTED":
            try:
                from video_factory.qc.retry_policy import clamp_max_retries

                previous_limit = clamp_max_retries(aggregate.get("max_qc_retries"))
                requested_limit = clamp_max_retries(
                    max_qc_retries if max_qc_retries is not None else aggregate.get("max_qc_retries")
                )
                if requested_limit <= previous_limit:
                    return False
            except (TypeError, ValueError):
                return False

        source_context = aggregate["source_context"]
        images = source_context.get("images")
        review = source_context.get("input_review")
        selected_ids = set(review.get("confirmed_image_asset_ids") or []) if isinstance(review, dict) else set()
        if not isinstance(images, list) or not images:
            return False
        ids = [str(item.get("asset_id") or "").strip() for item in images if isinstance(item, dict)]
        hashes = [str(item.get("sha256") or "").lower() for item in images if isinstance(item, dict)]
        if (
            len(ids) != len(images)
            or len(set(ids)) < 1
            or len(set(hashes)) != len(images)
            or (
                source_context.get("auto_admitted") is not True
                and (not selected_ids or not set(ids).issubset(selected_ids))
            )
        ):
            return False

        outbox = self.connection.execute(
            "SELECT state, lease_expires_at FROM workflow_outbox WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchall()
        if any(
            str(row["state"] or "") == "pending"
            or (str(row["state"] or "") == "leased" and _is_outbox_lease_active(row["lease_expires_at"]))
            for row in outbox
        ):
            return False

        from video_factory.workflow import transition
        next_aggregate = dict(aggregate)
        next_aggregate["state"] = transition(
            ProductVideoWorkflowState.WAITING_FOR_OPERATOR,
            ProductVideoWorkflowState.BUILDING_RESOURCE_PACK,
        ).value
        if auth:
            auth["workflow_id"] = workflow_id
            next_aggregate["qc_authorization"] = auth
        if qc_max_cost_usd is not None:
            next_aggregate["qc_max_cost_usd"] = float(qc_max_cost_usd)
        if max_qc_retries is not None:
            next_aggregate["max_qc_retries"] = int(max_qc_retries)
        reconciliations = [dict(item) if isinstance(item, dict) else item for item in reconciliations]
        reconciliation_index = next(
            i for i, item in enumerate(reconciliations)
            if isinstance(item, dict) and item.get("attempt_id") == reconciliation.get("attempt_id")
        )
        reconciliations[reconciliation_index]["resource_pack_resume_scheduled"] = True
        next_aggregate["qc_reconciliations"] = reconciliations
        for key in ("operator_action", "operator_stage", "operator_reason", "failure_code"):
            next_aggregate.pop(key, None)

        resume_identity = str(reconciliation.get("attempt_id") or reconciliation.get("evidence_reference") or workflow_id)
        token = hashlib.sha256(resume_identity.encode("utf-8")).hexdigest()[:12]
        kwargs = {
            "project_id": aggregate.get("project_id"),
            "source_context": source_context,
            "images": images,
            "research_snapshot_id": aggregate.get("research_snapshot_id"),
            "max_qc_retries": max_qc_retries or aggregate.get("max_qc_retries"),
        }
        if auth:
            kwargs["qc_authorization"] = auth
        if qc_max_cost_usd is not None:
            kwargs["qc_max_cost_usd"] = qc_max_cost_usd
        envelope = build_envelope(
            workflow_id=workflow_id,
            topic="job.dispatch",
            idempotency_key=f"resume-image-pack-{workflow_id}-{token}",
            payload={
                "job_name": "product_video.resource_pack",
                "workflow_id": workflow_id,
                "project_id": aggregate.get("project_id"),
                "args": [aggregate["research_result"]],
                "kwargs": kwargs,
                "owner_user_id": owner_user_id,
            },
        )
        now = datetime.now(timezone.utc).isoformat()
        self.workflow_repo.commit_workflow(
            workflow_id,
            json.dumps(next_aggregate, ensure_ascii=False),
            [{
                "event_id": f"evt_resume_image_pack_{token}",
                "command_id": f"cmd_resume_image_pack_{token}",
                "event_type": "WorkflowResumedForImageQC",
                "timestamp": now,
                "payload": {"reconciled_attempt_id": reconciliation.get("attempt_id"), "authorization_run_id": auth_id},
            }],
            outbox_message=envelope,
            expected_version=version,
        )
        return True

    def _quarantined_outbox_error(
        self,
        workflow_id: str,
        wf_state: str | None = None,
    ) -> str | None:
        """Return the terminal outbox error if the workflow is blocked in a non-HITL step."""
        if not workflow_id:
            return None
        if wf_state is None:
            wf_data = self.workflow_repo.get_workflow(workflow_id)
            if wf_data is None:
                return None
            wf_state = str(wf_data[0].get("state") or "")
        if str(wf_state or "").strip().lower() in {
            ProductVideoWorkflowState.COMPLETED.value.lower(),
            "skipped",
        }:
            return None
        try:
            row = self.connection.execute(
                "SELECT state, last_error FROM workflow_outbox WHERE workflow_id = ? "
                "ORDER BY outbox_id DESC LIMIT 1",
                (workflow_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None or row["state"] != "quarantined":
            return None
        return str(row["last_error"] or "Worker job was quarantined")

    def _unresolved_qc_attempt(self, product_keys: set[str] | None = None) -> dict[str, str] | None:
        """Find a QC operation that may still be running or needs reconciliation."""
        tables = {
            str(row[0])
            for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        candidates: list[dict[str, str]] = []
        if "workflow_qc_retries" in tables:
            rows = self.connection.execute(
                "SELECT workflow_id, retry_type, item_id, operation_id, status FROM workflow_qc_retries "
                "WHERE status IN ('reserved','claimed','sent','technical_unknown') "
                "AND NOT EXISTS (SELECT 1 FROM workflow_event e WHERE e.workflow_id=workflow_qc_retries.workflow_id AND e.event_type='OperatorWorkflowSkipped') "
                "ORDER BY created_at, id"
            ).fetchall()
            for row in rows:
                if product_keys is not None:
                    data = self.workflow_repo.get_workflow(str(row['workflow_id']))
                    aggregate = data[0] if data else {}
                    context = aggregate.get('source_context') or {}
                    key = str(context.get('canonical_product_key') or
                        (aggregate.get('prepared_product') or {}).get('product_key') or
                        aggregate.get('product_key') or '').casefold()
                    if key not in product_keys:
                        continue
                candidates.append({column: str(row[column] or '') for column in row.keys()})
                break
        if "product_video_operation_attempt" in tables:
            rows = self.connection.execute(
                "SELECT workflow_id, capability_id AS retry_type, attempt_id AS operation_id, "
                "status FROM product_video_operation_attempt "
                "WHERE capability_id LIKE 'product_video.qc.%' "
                "AND status IN ('reserved','claimed','sent','technical_unknown') "
                "AND NOT EXISTS (SELECT 1 FROM workflow_event e WHERE e.workflow_id=product_video_operation_attempt.workflow_id AND e.event_type='OperatorWorkflowSkipped') "
                "ORDER BY created_at, attempt_number, attempt_id"
            ).fetchall()
            for row in rows:
                if product_keys is not None:
                    data = self.workflow_repo.get_workflow(str(row['workflow_id']))
                    aggregate = data[0] if data else {}
                    context = aggregate.get('source_context') or {}
                    key = str(context.get('canonical_product_key') or
                        (aggregate.get('prepared_product') or {}).get('product_key') or
                        aggregate.get('product_key') or '').casefold()
                    if key not in product_keys:
                        continue
                candidates.append({column: str(row[column] or '') for column in row.keys()})
                break
        return candidates[0] if candidates else None

    def list_candidates(
        self,
        target_date: date | None = None,
        status_filter: str | None = None,
        qc_level: str = "middle",
    ) -> list[CandidateItem]:
        """Read model: lists all candidate products from the latest exchange manifest."""
        batch_dir = self.importer.find_latest_ready_batch(target_date)
        manifest = self.importer.load_manifest(batch_dir)

        ledger_by_key = self.import_repo.get_all_for_date(manifest.production_date)
        ledger_by_canon = self.import_repo.get_all_active_by_canonical_key()
        candidates: list[CandidateItem] = []
        seen_keys: set[str] = set()

        for p in manifest.products:
            if p.product_key in seen_keys or (p.canonical_product_key and p.canonical_product_key in seen_keys):
                continue
            if p.product_key:
                seen_keys.add(p.product_key)
            if p.canonical_product_key:
                seen_keys.add(p.canonical_product_key)
            if qc_level == "middle":
                is_valid, rejection_reason, valid_imgs = self.importer.validate_admission(p, batch_dir)
            else:
                is_valid, rejection_reason, valid_imgs = self.importer.validate_admission(
                    p, batch_dir, qc_level=qc_level)
            ledger_entry = ledger_by_key.get(p.product_key) or ledger_by_canon.get(p.canonical_product_key)

            enriched_imgs = []
            for v_img in valid_imgs:
                item = dict(v_img)
                rel = item.get("relative_path")
                if rel:
                    item["asset_url"] = f"/api/personal/product-video/assets/{manifest.production_date}/{manifest.batch_id}/{rel}"
                enriched_imgs.append(item)

            if not is_valid and ledger_entry is None:
                invalid_imgs = []
                review_by_id = {str(img.get("asset_id")): img for img in valid_imgs}
                for img in p.images:
                    item = dict(review_by_id.get(str(img.asset_id)) or img.to_dict())
                    if not item.get("identity_review_reason"):
                        item["identity_review_reason"] = "IMAGE_FILE_MISSING_OR_UNREADABLE: inspect this asset"
                    if img.relative_path:
                        item["asset_url"] = f"/api/personal/product-video/assets/{manifest.production_date}/{manifest.batch_id}/{img.relative_path}"
                    invalid_imgs.append(item)

                candidates.append(
                    CandidateItem(
                        priority=p.priority,
                        source_date=manifest.production_date,
                        source_batch_id=manifest.batch_id,
                        source_product_id=p.source_product_id,
                        canonical_product_key=p.canonical_product_key,
                        product_key=p.product_key,
                        name=p.name,
                        brand=p.brand,
                        model=p.model,
                        variant=p.variant,
                        price_display=p.price_display,
                        price_min=p.price_min,
                        price_max=p.price_max,
                        hot_reason=p.hot_reason,
                        key_features=p.key_features,
                        keywords=p.keywords,
                        images=invalid_imgs,
                        status="awaiting_product_lock" if str(rejection_reason or "").startswith("REVIEW_REQUIRED:") else "invalid",
                        internal_state="input_review_required" if str(rejection_reason or "").startswith("REVIEW_REQUIRED:") else "invalid",
                        rejection_reason=rejection_reason,
                    )
                )
                continue

            if ledger_entry is None:
                candidates.append(
                    CandidateItem(
                        priority=p.priority,
                        source_date=manifest.production_date,
                        source_batch_id=manifest.batch_id,
                        source_product_id=p.source_product_id,
                        canonical_product_key=p.canonical_product_key,
                        product_key=p.product_key,
                        name=p.name,
                        brand=p.brand,
                        model=p.model,
                        variant=p.variant,
                        price_display=p.price_display,
                        price_min=p.price_min,
                        price_max=p.price_max,
                        hot_reason=p.hot_reason,
                        key_features=p.key_features,
                        keywords=p.keywords,
                        images=enriched_imgs,
                        status="unprocessed",
                        internal_state="imported",
                    )
                )
                continue

            int_status = ledger_entry["status"]
            wf_id = ledger_entry["workflow_id"]
            wf_data = self.workflow_repo.get_workflow(wf_id)

            if wf_data is None:
                candidates.append(
                    CandidateItem(
                        priority=p.priority,
                        source_date=manifest.production_date,
                        source_batch_id=manifest.batch_id,
                        source_product_id=p.source_product_id,
                        canonical_product_key=p.canonical_product_key,
                        product_key=p.product_key,
                        name=p.name,
                        brand=p.brand,
                        model=p.model,
                        variant=p.variant,
                        price_display=p.price_display,
                        price_min=p.price_min,
                        price_max=p.price_max,
                        hot_reason=p.hot_reason,
                        key_features=p.key_features,
                        keywords=p.keywords,
                        images=enriched_imgs,
                        status="invalid" if int_status == "error" or not is_valid else "unprocessed",
                        internal_state=int_status,
                        rejection_reason=(
                            None if is_valid and int_status != "error"
                            else (ledger_entry.get("last_error") or rejection_reason)
                        ),
                    )
                )
                continue

            agg, _version = wf_data
            wf_state = str(agg.get("state") or "")
            approval_id = agg.get("pending_approval_id")
            gate = agg.get("pending_approval_gate")
            quarantined_err = self._quarantined_outbox_error(wf_id, wf_state)
            item_rejection_reason: str | None = rejection_reason if not is_valid else None
            internal_st = wf_state

            if agg.get("failure_category") == "PRODUCT_REJECTED":
                ui_status = "blocked"
                internal_st = "skipped"
                item_rejection_reason = agg.get("skip_reason") or agg.get("failure_reason") or ledger_entry.get("last_error")
            elif (
                agg.get("failure_category") in {
                    FailureCategory.QC_INFRA_PAUSED.value,
                    FailureCategory.CONTRACT_BROKEN.value,
                }
                or classify_failure(quarantined_err)
                in {FailureCategory.QC_INFRA_PAUSED, FailureCategory.CONTRACT_BROKEN}
            ):
                ui_status = "waiting_for_operator"
                internal_st = str(agg.get("failure_category") or wf_state)
                item_rejection_reason = (
                    agg.get("operator_reason") or agg.get("failure_reason")
                    or ledger_entry.get("last_error") or quarantined_err
                )
            elif int_status == "error" or quarantined_err:
                ui_status = "technical_error"
                internal_st = "error" if int_status == "error" else "quarantined"
                item_rejection_reason = ledger_entry.get("last_error") or f"WORKER_BLOCKED: {quarantined_err}"
            elif wf_state == "skipped":
                ui_status = "blocked"
                item_rejection_reason = agg.get("skip_reason") or agg.get("failure_code") or "Product workflow was skipped"
            elif wf_state in {"failed", "contract_broken"}:
                ui_status = "technical_error"
                item_rejection_reason = agg.get("failure_code") or agg.get("operator_reason") or wf_state
            elif wf_state == ProductVideoWorkflowState.COMPLETED.value:
                ui_status = "completed"
            elif wf_state == ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value:
                ui_status = "awaiting_product_lock"
            elif self._is_idle_pr1_gate(agg, wf_id):
                ui_status = "ready_to_resume"
                item_rejection_reason = "Workflow hiện có đang chờ tiếp tục; bấm Chạy để resume an toàn."
            elif wf_state == ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value:
                ui_status = "waiting_for_operator"
                item_rejection_reason = agg.get("operator_reason") or agg.get("failure_code")
            else:
                ui_status = "in_production"

            candidates.append(
                CandidateItem(
                    priority=p.priority,
                    source_date=manifest.production_date,
                    source_batch_id=manifest.batch_id,
                    source_product_id=p.source_product_id,
                    canonical_product_key=p.canonical_product_key,
                    product_key=p.product_key,
                    name=p.name,
                    brand=p.brand,
                    model=p.model,
                    variant=p.variant,
                    price_display=p.price_display,
                    price_min=p.price_min,
                    price_max=p.price_max,
                    hot_reason=p.hot_reason,
                    key_features=p.key_features,
                    keywords=p.keywords,
                    images=enriched_imgs,
                    status=ui_status,
                    internal_state=internal_st,
                    rejection_reason=item_rejection_reason,
                    workflow_id=wf_id,
                    approval_id=approval_id,
                    gate=gate,
                )
            )

        if status_filter and status_filter != "all":
            return [c for c in candidates if c.status == status_filter]
        return candidates

    def _is_idle_pr1_gate(self, aggregate: dict[str, Any], workflow_id: str) -> bool:
        """Expose an existing PR-1 approval checkpoint for Studio resume only when idle."""
        policy = aggregate.get("creative_policy") or {}
        if (aggregate.get("state") not in {
                ProductVideoWorkflowState.AWAITING_BRIEF_APPROVAL.value,
                ProductVideoWorkflowState.AWAITING_SCENE_APPROVAL.value,
                ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL.value,
            }
                or policy.get("creative_mode") != "four_agent"
                or policy.get("auto_export") is not True
                or policy.get("preparation_only") is not True
                or not aggregate.get("pending_approval_id")):
            return False
        try:
            row = self.connection.execute(
                "SELECT 1 FROM workflow_outbox WHERE workflow_id = ? "
                "AND (state IN ('pending', 'quarantined') OR "
                "(state = 'leased' AND (lease_expires_at IS NULL OR lease_expires_at > datetime('now')))) "
                "LIMIT 1",
                (workflow_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        return row is None

    def evaluate_catalog_candidate(
        self,
        product: ExchangeProduct,
        batch_dir: Path,
        catalog_statuses: dict[str, tuple[str, str]] | None = None,
    ) -> tuple[bool, str | None]:
        """Use Catalog V2 as a warning source; Exchange admission and Image QC remain gates."""
        canon_key = product.canonical_product_key.casefold()
        if catalog_statuses is None:
            return True, None
        status_pair = catalog_statuses.get(canon_key)
        if status_pair is None:
            return True, None
        identity_status, pipeline_status = status_pair
        if identity_status != "VALID":
            return True, "CATALOG_IDENTITY_NOT_VALID"
        if pipeline_status != "READY_FOR_IMAGE_QC":
            return True, "CATALOG_NOT_READY_FOR_IMAGE_QC"
        return True, None

    def start_auto(
        self,
        limit: int = 5,
        owner_user_id: str = "local-owner",
        target_date: date | None = None,
        worker_id: str | None = None,
        qc_authorization: dict[str, Any] | None = None,
        qc_max_cost_usd: float | None = None,
        research_snapshot_id: str | None = None,
        max_qc_retries: int | None = None,
        execution_mode: str = "auto",
    ) -> BatchStartResult:
        """Atomically claim and start up to `limit` eligible UNPROCESSED products.

        Strictly preserves Product-Intelligence published manifest priority.
        Does NOT rerank products.
        Enforces a bounded batch size across the READY batch's production date.
        """
        if limit < 1 or limit > 100:
            raise ValueError(f"Limit must be between 1 and 100, got: {limit}")

        from video_factory.workflows.runtime_control import ensure_pipeline_pause

        pause = ensure_pipeline_pause(self.connection)
        if self._pause_blocks_selection(pause):
            return BatchStartResult(
                started=(),
                skipped=(SkippedProduct(
                    product_key=pause.get("workflow_id") or "pipeline",
                    reason=(
                        f"BATCH_STOPPED:{pause.get('category')}:{pause.get('code')}: "
                        f"{pause.get('message')}"
                    ),
                    workflow_id=pause.get("workflow_id") or None,
                ),),
            )

        unresolved_qc = self._unresolved_qc_attempt()
        if unresolved_qc:
            workflow_id = unresolved_qc.get("workflow_id") or "unknown"
            operation_id = unresolved_qc.get("operation_id") or "unknown"
            reason = (
                "BATCH_STOPPED:QC_OUTCOME_UNCERTAIN: "
                f"workflow={workflow_id}, operation={operation_id}, "
                f"status={unresolved_qc.get('status') or 'unknown'}; reconcile this exact attempt before starting another product"
            )
            return BatchStartResult(
                started=(),
                skipped=(SkippedProduct(
                    product_key=unresolved_qc.get("item_id") or workflow_id,
                    reason=reason,
                    workflow_id=workflow_id,
                ),),
            )

        batch_dir = self.importer.find_latest_ready_batch(target_date)
        manifest = self.importer.load_manifest(batch_dir)
        effective_worker_id = worker_id or f"coord-{os.getpid()}"

        # Auto-cleanup orphaned DB records (workflow deleted but DB still shows 'started')
        for row in self.import_repo.get_all_for_date(manifest.production_date).values():
            if row["status"] == "started" and self.workflow_repo.get_workflow(row["workflow_id"]) is None:
                self.import_repo.delete_by_product_key(row["product_key"])

        candidates = self.list_candidates(target_date)
        catalog_statuses: dict[str, tuple[str, str]] | None = None
        catalog_skips: list[SkippedProduct] = []
        if self.catalog_candidates_path is not None:
            from .catalog_candidates import load_candidate_statuses

            catalog_statuses = load_candidate_statuses(self.catalog_candidates_path)

        def persist_auto_skip(candidate: CandidateItem, reason: str) -> None:
            product = next((p for p in manifest.products if p.product_key == candidate.product_key), None)
            if product is None:
                catalog_skips.append(SkippedProduct(candidate.product_key, reason))
                return
            claim = self.import_repo.claim_product(
                canonical_product_key=product.canonical_product_key,
                source_date=manifest.production_date,
                source_batch_id=manifest.batch_id,
                source_product_id=product.source_product_id,
                source_path=str(batch_dir / "manifest.json"),
                worker_id=effective_worker_id,
            )
            if claim.status == "claimed":
                self.import_repo.mark_error(claim.product_key, reason)
            catalog_skips.append(SkippedProduct(candidate.product_key, reason, workflow_id=claim.workflow_id))

        unprocessed: list[CandidateItem] = []
        skipped_in_this_batch = {
            value
            for row in self.import_repo.get_all_for_date(manifest.production_date).values()
            if row.get("status") == "error" and row.get("source_batch_id") == manifest.batch_id
            # Older releases rejected products absent from the static Catalog
            # list. Reconsider those rows under current admission rules.
            and not str(row.get("last_error") or "").startswith("NOT_LISTED_IN_CATALOG_V2")
            for value in (row.get("product_key"), row.get("canonical_product_key"))
            if value
        }
        for row in self.import_repo.get_all_for_date(manifest.production_date).values():
            if row.get("status") != "started" or row.get("source_batch_id") != manifest.batch_id:
                continue
            current = self.workflow_repo.get_workflow(str(row.get("workflow_id") or ""))
            if current is None:
                continue
            aggregate, _ = current
            current_state = str(aggregate.get("state") or "")
            if (pause and not self._pause_blocks_selection(pause)
                    and str(row.get("workflow_id") or "") == str(pause.get("workflow_id") or "")):
                # A product-scoped pause does not prevent this batch from
                # admitting independent products; the paused product remains
                # bound to its existing workflow for explicit reconciliation.
                continue
            failure_category = str(aggregate.get("failure_category") or "")
            quarantined = self._quarantined_outbox_error(str(row.get("workflow_id") or ""), current_state)
            if aggregate.get("failure_category") == "PRODUCT_REJECTED" or current_state == "skipped":
                continue
            categorized_blocker = failure_category in {
                FailureCategory.QC_INFRA_PAUSED.value,
                FailureCategory.CONTRACT_BROKEN.value,
            }
            if (current_state == ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
                    and str(aggregate.get('failure_code') or '') in {
                        'CREATIVE_RESPONSE_INVALID','CREATIVE_OUTCOME_UNCERTAIN'}):
                catalog_skips.append(SkippedProduct(
                    product_key=str(row.get('canonical_product_key') or row.get('product_key') or 'unknown'),
                    reason=f"PRODUCT_RECONCILIATION_REQUIRED:{aggregate.get('failure_code')}",
                    workflow_id=str(row.get('workflow_id') or '')))
                continue
            if current_state != ProductVideoWorkflowState.COMPLETED.value and (
                quarantined
                or categorized_blocker
                or current_state in {
                    ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value,
                    "contract_broken",
                }
            ):
                code = str(aggregate.get("failure_code") or "WORKFLOW_PENDING_OPERATOR")
                reason = quarantined or str(aggregate.get("operator_reason") or current_state)
                return BatchStartResult(
                    started=(),
                    skipped=(SkippedProduct(
                        product_key=str(row.get("canonical_product_key") or row.get("product_key") or "unknown"),
                        reason=f"BATCH_STOPPED:{code}:{reason}",
                        workflow_id=str(row.get("workflow_id") or ""),
                    ),),
                )
        for candidate in candidates:
            existing_workflow = (
                self.workflow_repo.get_workflow(candidate.workflow_id)
                if candidate.workflow_id else None
            )
            if candidate.status not in {"unprocessed", "awaiting_product_lock", "invalid"} or existing_workflow:
                continue
            # Do not spin on the same rejected bytes during auto rollover. A
            # corrected READY batch has a new batch ID and is reconsidered.
            # Existing workflows and paid attempts are excluded above.
            if (candidate.product_key in skipped_in_this_batch
                    or candidate.canonical_product_key in skipped_in_this_batch):
                continue
            product_for_cand = next((p for p in manifest.products if p.product_key == candidate.product_key), None)
            if product_for_cand is not None:
                _catalog_admitted, _catalog_warning = self.evaluate_catalog_candidate(
                    product_for_cand, batch_dir, catalog_statuses,
                )
                is_valid, admission_reason, _ = self.importer.validate_admission(product_for_cand, batch_dir)
                if not is_valid:
                    persist_auto_skip(candidate, f"AUTO_ADMISSION_BLOCKED: {admission_reason or 'INVALID_ADMISSION'}")
                    continue
            elif catalog_statuses is not None:
                persist_auto_skip(candidate, "NOT_FOUND_IN_MANIFEST")
                continue
            unprocessed.append(candidate)

        # Only count products that exist in current manifest AND not already completed
        manifest_keys = {p.product_key for p in manifest.products}
        manifest_canonical_keys = {p.canonical_product_key for p in manifest.products}
        already_started = 0
        for row in self.import_repo.get_all_for_date(manifest.production_date).values():
            if row["status"] not in {"started", "claimed"} or not (
                row["product_key"] in manifest_keys
                or row.get("canonical_product_key") in manifest_canonical_keys
            ):
                continue
            if self._quarantined_outbox_error(row["workflow_id"]):
                continue
            wf_data = self.workflow_repo.get_workflow(row["workflow_id"])
            if wf_data is None:
                continue
            agg, _ = wf_data
            state = agg.get("state")
            if state not in {ProductVideoWorkflowState.COMPLETED.value, ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value}:
                already_started += 1
        # Repeated scheduled runs may fill a shortfall, but never exceed the
        # daily cap. Keep Product-Intelligence manifest priority.
        target_count = max(0, limit - already_started)
        selected: list[CandidateItem] = []
        for candidate in unprocessed:
            if len(selected) >= target_count:
                break
            product = next((p for p in manifest.products if p.product_key == candidate.product_key), None)
            if product is None:
                continue
            valid, reason, _images = self.importer.validate_admission(product, batch_dir)
            if not valid:
                # Persist auto-admission failures in the existing import ledger so
                # restarts do not repeatedly select a blocked candidate.
                persist_auto_skip(candidate, f"AUTO_ADMISSION_BLOCKED: {reason}")
                continue
            selected.append(candidate)

        selected_keys = [c.product_key for c in selected]
        result = self._start_products_by_keys(
            product_keys=selected_keys,
            manifest=manifest,
            batch_dir=batch_dir,
            owner_user_id=owner_user_id,
            worker_id=effective_worker_id,
            qc_authorization=qc_authorization,
            qc_max_cost_usd=qc_max_cost_usd,
            research_snapshot_id=research_snapshot_id,
            max_qc_retries=max_qc_retries,
            execution_mode=execution_mode,
        )
        if catalog_skips:
            result = BatchStartResult(started=result.started, skipped=tuple(catalog_skips) + result.skipped)
        return result

    def start_selection(
        self,
        product_keys: list[str],
        owner_user_id: str = "local-owner",
        target_date: date | None = None,
        worker_id: str | None = None,
        qc_authorization: dict[str, Any] | None = None,
        qc_max_cost_usd: float | None = None,
        research_snapshot_id: str | None = None,
        input_reviews: dict[str, dict[str, Any]] | None = None,
        max_qc_retries: int | None = None,
        execution_mode: str = "manual",
        creative_policy: dict[str, Any] | None = None,
        ready_batch_dir: Path | None = None,
    ) -> BatchStartResult:
        """Start workflows for a specific set of selected product keys with write-time revalidation."""
        deduped = list(dict.fromkeys(product_keys))
        if len(deduped) > 5:
            raise ValueError(f"Cannot select more than 5 products at a time, got: {len(deduped)}")
        if not deduped:
            return BatchStartResult(started=(), skipped=())

        from video_factory.workflows.runtime_control import ensure_pipeline_pause

        pause = ensure_pipeline_pause(self.connection)
        if ready_batch_dir is None:
            batch_dir = self.importer.find_latest_ready_batch(target_date)
        else:
            batch_dir = Path(ready_batch_dir).resolve()
            try:
                batch_dir.relative_to(self.importer.inbox_root.resolve())
            except ValueError as exc:
                raise ValueError("pinned READY batch is outside the configured exchange inbox") from exc
            if (not (batch_dir / "READY").is_file() or not (batch_dir / "manifest.json").is_file()
                    or (target_date and batch_dir.parent.name != target_date.isoformat())):
                raise ValueError("pinned READY batch is missing its marker/manifest or has the wrong date")
        manifest = self.importer.load_manifest(batch_dir)
        by_key = {p.product_key:p for p in manifest.products}
        by_canonical = {p.canonical_product_key:p for p in manifest.products}
        selected_identity_keys = {
            str((by_key.get(key) or by_canonical.get(key)).canonical_product_key).casefold()
            for key in deduped if by_key.get(key) or by_canonical.get(key)
        }
        if self._pause_blocks_selection(pause, selected_identity_keys):
            return BatchStartResult(started=(), skipped=(SkippedProduct(
                product_key=pause.get("workflow_id") or "pipeline",
                reason=f"PIPELINE_PAUSED:{pause.get('category')}:{pause.get('code')}: {pause.get('message')}",
                workflow_id=pause.get("workflow_id") or None),))
        unresolved_qc = self._unresolved_qc_attempt(selected_identity_keys)
        if unresolved_qc:
            return BatchStartResult(started=(), skipped=(SkippedProduct(
                product_key="pipeline",
                reason=("UNRESOLVED_QC_ATTEMPT: "
                    f"workflow={unresolved_qc.get('workflow_id')} "
                    f"operation={unresolved_qc.get('operation_id')} "
                    f"status={unresolved_qc.get('status')}"),
                workflow_id=unresolved_qc.get("workflow_id") or None),))
        effective_worker_id = worker_id or f"coord-{os.getpid()}"

        return self._start_products_by_keys(
            product_keys=deduped,
            manifest=manifest,
            batch_dir=batch_dir,
            owner_user_id=owner_user_id,
            worker_id=effective_worker_id,
            qc_authorization=qc_authorization,
            qc_max_cost_usd=qc_max_cost_usd,
            research_snapshot_id=research_snapshot_id,
            input_reviews=input_reviews,
            max_qc_retries=max_qc_retries,
            execution_mode=execution_mode,
            creative_policy=creative_policy,
        )

    def _start_products_by_keys(
        self,
        product_keys: list[str],
        manifest: Any,
        batch_dir: Path,
        owner_user_id: str,
        worker_id: str,
        qc_authorization: dict[str, Any] | None = None,
        qc_max_cost_usd: float | None = None,
        research_snapshot_id: str | None = None,
        input_reviews: dict[str, dict[str, Any]] | None = None,
        max_qc_retries: int | None = None,
        execution_mode: str = "manual",
        creative_policy: dict[str, Any] | None = None,
    ) -> BatchStartResult:
        prod_by_key = {p.product_key: p for p in manifest.products}
        from video_factory.creative.contracts import canonical_digest
        prod_by_canon = {p.canonical_product_key: p for p in manifest.products}
        catalog_statuses: dict[str, tuple[str, str]] | None = None
        if self.catalog_candidates_path is not None:
            from .catalog_candidates import load_candidate_statuses

            catalog_statuses = load_candidate_statuses(self.catalog_candidates_path)
        started: list[StartedProduct] = []
        skipped: list[SkippedProduct] = []

        for key_req in product_keys:
            product = prod_by_key.get(key_req) or prod_by_canon.get(key_req)
            if product is None:
                skipped.append(SkippedProduct(product_key=key_req, reason="NOT_FOUND_IN_MANIFEST"))
                continue

            pkey = product.product_key

            is_cat_ok, cat_reason = self.evaluate_catalog_candidate(product, batch_dir, catalog_statuses)

            review = (input_reviews or {}).get(product.product_key) or (input_reviews or {}).get(product.canonical_product_key)
            is_valid, reason, valid_imgs = self.importer.validate_admission(product, batch_dir, input_review=review)
            if not is_valid:
                prefix = "INPUT_REVIEW_REQUIRED" if str(reason or "").startswith("REVIEW_REQUIRED:") else "INVALID_ADMISSION"
                skipped.append(SkippedProduct(product_key=pkey, reason=f"{prefix}: {reason}"))
                continue

            # Atomic claim under immediate transaction
            claim = self.import_repo.claim_product(
                canonical_product_key=product.canonical_product_key,
                source_date=manifest.production_date,
                source_batch_id=manifest.batch_id,
                source_product_id=product.source_product_id,
                source_path=str(batch_dir / "manifest.json"),
                worker_id=worker_id,
            )

            if claim.status != "claimed":
                if claim.status == "already_started" and claim.workflow_id and self._resume_reconciled_image_qc(
                    claim.workflow_id,
                    owner_user_id=owner_user_id,
                    qc_authorization=qc_authorization,
                    qc_max_cost_usd=qc_max_cost_usd,
                    max_qc_retries=max_qc_retries,
                ):
                    started.append(StartedProduct(
                        product_key=pkey,
                        canonical_product_key=product.canonical_product_key,
                        workflow_id=claim.workflow_id,
                        name=product.name,
                    ))
                    continue
                if claim.status == "already_started":
                    reason = "ALREADY_STARTED"
                elif claim.status == "already_claimed":
                    reason = "ALREADY_CLAIMED"
                else:
                    reason = f"CLAIM_ERROR: {claim.error or claim.status}"
                skipped.append(SkippedProduct(
                    product_key=pkey,
                    reason=reason,
                    workflow_id=claim.workflow_id,
                    error=claim.error,
                ))
                continue

            workflow_id = claim.workflow_id
            exact_identity = product.name if product.identity_mode == 'source_product' else f"{product.brand} {product.model}".strip()
            # Exchange crawling is untrusted input.  Keep only controlled product
            # identity in the research request; do not put crawl text in prompts.
            keywords = [exact_identity]
            source_context = {
                "source": "hermes_product_video_exchange",
                "source_date": manifest.production_date,
                "source_batch_id": manifest.batch_id,
                "source_file": str(batch_dir / "manifest.json"),
                "source_product_id": product.source_product_id,
                "canonical_product_key": product.canonical_product_key,
                "product_key": pkey,
                **({'name':product.name, 'category':product.category, 'identity_mode':'source_product',
                    'source_snapshot':product.source_snapshot,
                    'source_snapshot_digest':canonical_digest(product.source_snapshot)}
                   if product.identity_mode == 'source_product' else {}),
                "brand": product.brand,
                "model": product.model,
                "variant": product.variant,
                **({"creative_input": product.creative_input} if product.creative_input else {}),
                **({"seller_description": product.seller_description} if product.seller_description else {}),
                "source_url": product.source_url,
                "auto_admitted": not bool((review or {}).get("confirmed_image_asset_ids")),
                "images": valid_imgs,
                "input_review": review or {},
                "catalog_status": catalog_statuses.get(product.canonical_product_key.casefold()) if catalog_statuses else None,
                "catalog_warning": cat_reason,
            }

            # A quarantined workflow can carry paid QC attempts and artifacts.
            # Retrying it requires the guarded reset path, never an implicit
            # purge from a new start command.
            existing_wf = self.workflow_repo.get_workflow(workflow_id)
            if existing_wf is not None:
                quarantine_reason = self._quarantined_outbox_error(workflow_id)
                if quarantine_reason:
                    reason = f"ERROR_RESET_REQUIRED: existing quarantined workflow must be reset before start: {quarantine_reason}"
                    self.import_repo.mark_error(claim.product_key, reason)
                else:
                    reason = "ALREADY_STARTED"
                    self.import_repo.mark_started(claim.product_key)
                skipped.append(SkippedProduct(
                    product_key=pkey, reason=reason, workflow_id=workflow_id,
                ))
                continue
            # A missing aggregate may leave pre-start command/outbox remnants.
            self.workflow_repo.purge_workflow(workflow_id)

            try:
                start_product_video_workflow(
                    workflow_repository=self.workflow_repo,
                    workflow_id=workflow_id,
                    product_name=exact_identity,
                    keywords=keywords,
                    pdp_url=product.source_url,
                    owner_user_id=owner_user_id,
                    idempotency_key=f"exchange-import:{pkey}",
                    project_id=f"proj-{workflow_id}",
                    source_context=source_context,
                    qc_authorization=qc_authorization,
                    qc_max_cost_usd=qc_max_cost_usd,
                    research_snapshot_id=research_snapshot_id,
                    max_qc_retries=max_qc_retries,
                    execution_mode=execution_mode,
                    creative_policy=creative_policy,
                )
                self.import_repo.mark_started(claim.product_key)
                from video_factory.workflows.steps import record_pipeline_step

                admission_digest = hashlib.sha256(
                    json.dumps(
                        {
                            "canonical_product_key": product.canonical_product_key,
                            "brand": product.brand,
                            "model": product.model,
                            "variant": product.variant,
                            "catalog_status": catalog_statuses.get(product.canonical_product_key.casefold()) if catalog_statuses else None,
                            "images": [
                                {"asset_id": image.get("asset_id"), "sha256": image.get("sha256")}
                                for image in valid_imgs
                            ],
                        },
                        sort_keys=True,
                        default=str,
                    ).encode("utf-8")
                ).hexdigest()
                record_pipeline_step(
                    self.connection, workflow_id=workflow_id,
                    product_key=product.product_key, step_name="INPUT_ADMISSION",
                    status="completed", input_digest=f"sha256:{admission_digest}",
                    cost_status="not_applicable",
                )
                started.append(StartedProduct(
                    product_key=pkey,
                    canonical_product_key=product.canonical_product_key,
                    workflow_id=workflow_id,
                    name=product.name,
                ))
            except Exception as exc:  # noqa: BLE001
                if self.workflow_repo.get_workflow(workflow_id) is not None:
                    # Recovery: Workflow was created before worker crashed; mark started safely
                    self.import_repo.mark_started(claim.product_key)
                    started.append(StartedProduct(
                        product_key=pkey,
                        canonical_product_key=product.canonical_product_key,
                        workflow_id=workflow_id,
                        name=product.name,
                    ))
                else:
                    err_msg = str(exc) or exc.__class__.__name__
                    self.import_repo.mark_error(claim.product_key, err_msg)
                    skipped.append(SkippedProduct(
                        product_key=pkey,
                        reason=f"START_FAILED: {err_msg}",
                        workflow_id=workflow_id,
                        error=err_msg,
                    ))

        return BatchStartResult(started=tuple(started), skipped=tuple(skipped))

    def advance_to_product_lock(
        self,
        workflow_ids: list[str],
        provider_mode: str = "configured",
        max_cycles: int = 10,
    ) -> dict[str, Any]:
        """Drive newly started workflows to AwaitingProductLock via durable outbox.

        Only targets the specified workflow_ids.
        Returns a structured report.
        """
        if not workflow_ids:
            return {"succeeded": True, "workflows": {}}

        from video_factory.application.operator_handler import ApprovalService

        effective_db_path = self.db_path
        if effective_db_path is None:
            rows = self.connection.execute("PRAGMA database_list").fetchall()
            for r in rows:
                if (r["name"] if hasattr(r, "keys") else r[1]) == "main":
                    file_path = r["file"] if hasattr(r, "keys") else r[2]
                    if file_path:
                        effective_db_path = Path(file_path)
                    break

        if effective_db_path is None:
            raise ValueError("db_path required for advance_to_product_lock")

        wf_states: dict[str, Any] = {}
        approval_svc = ApprovalService(self.connection)
        from video_factory.workflows.runtime_control import ensure_pipeline_pause

        pause = ensure_pipeline_pause(self.connection)
        if pause:
            return {
                "succeeded": False,
                "error": f"PIPELINE_PAUSED:{pause.get('category')}:{pause.get('code')}: {pause.get('message')}",
                "workflows": {},
                "locked_workflows": [],
            }

        for cycle in range(max_cycles):
            report = run_worker(
                db_path=effective_db_path,
                provider_mode=provider_mode,
                once=True,
                batch_size=1,
                workflow_ids=workflow_ids,
            )

            all_at_lock = True
            has_failure = False
            quarantined_wfs: dict[str, str] = {}
            locked_wfs: list[str] = []
            skipped_wfs: list[str] = []

            for wf_id in workflow_ids:
                wf_data = self.workflow_repo.get_workflow(wf_id)
                if not wf_data:
                    all_at_lock = False
                    wf_states[wf_id] = {"state": "missing", "error": "Workflow aggregate not found"}
                    has_failure = True
                    continue

                agg, ver = wf_data
                state = agg.get("state")
                approval_id = agg.get("pending_approval_id")
                gate = agg.get("pending_approval_gate")

                if state == ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value or state == ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK:
                    if not approval_id:
                        all_at_lock = False
                        wf_states[wf_id] = {"state": state, "error": "Missing pending_approval_id"}
                        has_failure = True
                    else:
                        req = approval_svc.get_request(approval_id)
                        if not req or req.status.value != "pending":
                            all_at_lock = False
                            wf_states[wf_id] = {"state": state, "error": f"Approval request not pending: {req.status if req else None}"}
                            has_failure = True
                        else:
                            wf_states[wf_id] = {"state": state, "approval_id": approval_id, "gate": gate}
                            locked_wfs.append(wf_id)
                elif agg.get("failure_category") == "PRODUCT_REJECTED" or state == "skipped":
                    wf_states[wf_id] = {
                        "state": "skipped",
                        "failure_category": "PRODUCT_REJECTED",
                        "reason": agg.get("skip_reason") or agg.get("operator_reason") or agg.get("failure_code"),
                    }
                    skipped_wfs.append(wf_id)
                else:
                    all_at_lock = False
                    quarantined_err = self._quarantined_outbox_error(wf_id, str(state or ""))
                    if quarantined_err:
                        wf_states[wf_id] = {
                            "state": state,
                            "error": quarantined_err,
                            "quarantined": True,
                        }
                        quarantined_wfs[wf_id] = quarantined_err
                        has_failure = True
                    elif state == ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value:
                        error = str(agg.get("operator_reason") or agg.get("failure_code") or "Workflow requires operator action")
                        wf_states[wf_id] = {"state": state, "error": error, "pending_operator": True}
                        has_failure = True
                    else:
                        wf_states[wf_id] = {"state": state}

            if has_failure:
                return {
                    "succeeded": False,
                    "error": "Batch stopped at the first workflow requiring operator action or recovery",
                    "workflows": wf_states,
                    "locked_workflows": locked_wfs,
                    "quarantined_workflows": quarantined_wfs,
                }

            if all_at_lock and not skipped_wfs:
                return {
                    "succeeded": True,
                    "cycles": cycle + 1,
                    "workflows": wf_states,
                    "locked_workflows": locked_wfs,
                    "quarantined_workflows": {},
                }

            if len(locked_wfs) + len(skipped_wfs) == len(workflow_ids):
                return {
                    "succeeded": True,
                    "cycles": cycle + 1,
                    "workflows": wf_states,
                    "locked_workflows": locked_wfs,
                    "skipped_workflows": skipped_wfs,
                    "quarantined_workflows": {},
                }

            if report and report.succeeded == 0 and report.failed == 0 and report.claimed == 0:
                err_msg = "No further outbox messages eligible, but workflows have not reached AwaitingProductLock"
                if quarantined_wfs:
                    first_err = next(iter(quarantined_wfs.values()))
                    err_msg = f"{err_msg} ({first_err})"
                return {
                    "succeeded": False,
                    "error": err_msg,
                    "workflows": wf_states,
                    "locked_workflows": locked_wfs,
                    "quarantined_workflows": quarantined_wfs,
                }

        return {
            "succeeded": False,
            "error": f"Exceeded maximum cycles ({max_cycles}) advancing to Product Lock",
            "workflows": wf_states,
        }

    def reset_product_or_workflow(
        self,
        target_id: str,
        *,
        skip: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Reset a stalled or errored product/workflow so it can be retried or skipped."""
        ledger_entry = self.import_repo.find_by_target(target_id)
        wf_id = ledger_entry["workflow_id"] if ledger_entry else target_id.strip()
        product_key = ledger_entry["product_key"] if ledger_entry else None

        if ledger_entry and ledger_entry.get("status") == "completed":
            raise ValueError(f"Không thể đặt lại workflow đã hoàn thành (workflow_id={wf_id}).")

        wf_data = self.workflow_repo.get_workflow(wf_id)
        recovery_evidence = skip or bool(
            ledger_entry
            and (
                ledger_entry.get("status") == "error"
                or str(ledger_entry.get("last_error") or "").strip()
            )
        )
        if wf_data is not None:
            agg, _version = wf_data
            wf_state = str(agg.get("state") or "")
            if wf_state == ProductVideoWorkflowState.COMPLETED.value:
                raise ValueError(f"Không thể đặt lại workflow đã hoàn thành (workflow_id={wf_id}).")

            past_lock_states = {
                ProductVideoWorkflowState.LOCKING_RESOURCE_PACK.value,
                ProductVideoWorkflowState.CREATING_BRIEF.value,
                ProductVideoWorkflowState.AWAITING_BRIEF_APPROVAL.value,
                ProductVideoWorkflowState.CREATING_SCENE_PLAN.value,
                ProductVideoWorkflowState.AWAITING_SCENE_APPROVAL.value,
                ProductVideoWorkflowState.CREATING_STORYBOARD.value,
                ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL.value,
                ProductVideoWorkflowState.RENDERING.value,
                ProductVideoWorkflowState.AWAITING_FINAL_APPROVAL.value,
                ProductVideoWorkflowState.EXPORTING.value,
            }
            if wf_state in past_lock_states or agg.get("locked_pack"):
                raise ValueError(
                    f"Không thể đặt lại workflow đã vượt qua Product Lock (trạng thái: {wf_state}, workflow_id={wf_id})."
                )
            recovery_evidence = recovery_evidence or bool(
                agg.get("recovery_required") or agg.get("quarantined")
            )

        try:
            outbox_rows = self.connection.execute(
                "SELECT outbox_id, state, lease_expires_at FROM workflow_outbox WHERE workflow_id = ?",
                (wf_id,),
            ).fetchall()
            for r in outbox_rows:
                r_st = str(r["state"] or "")
                if r_st == "leased" and _is_outbox_lease_active(r["lease_expires_at"]):
                    raise ValueError(
                        f"Không thể đặt lại: workflow đang trong quá trình thực thi (active lease đến {r['lease_expires_at']}, workflow_id={wf_id})."
                    )
                if r_st == "pending" and wf_data is not None and not skip:
                    raise ValueError(
                        f"Không thể đặt lại: workflow đang có job chờ thực thi trong outbox (state='pending', workflow_id={wf_id})."
                    )
                if r_st == "quarantined":
                    recovery_evidence = True

        except sqlite3.OperationalError:
            pass

        if wf_data is None:
            recovery_evidence = True  # orphan aggregate (with or without ledger) is an explicit recovery case

        if not recovery_evidence:
            raise ValueError(
                f"Không thể đặt lại workflow khỏe mạnh hoặc chưa có bằng chứng lỗi/quarantine "
                f"(trạng thái: {wf_state if wf_data is not None else 'unknown'}, workflow_id={wf_id})."
            )

        if not ledger_entry and wf_data is None:
            # Check if there are any orphaned rows in workflow_outbox or processed_command
            has_orphan = False
            for tbl in ("workflow_outbox", "processed_command", "workflow_event"):
                try:
                    row = self.connection.execute(f"SELECT 1 FROM {tbl} WHERE workflow_id = ? LIMIT 1", (wf_id,)).fetchone()
                    if row:
                        has_orphan = True
                        break
                except sqlite3.OperationalError:
                    pass
            if not has_orphan:
                raise KeyError(f"Không tìm thấy sản phẩm hoặc workflow khớp với '{target_id}'")

        self.workflow_repo.purge_workflow(wf_id)
        action = "purged_orphan"
        if product_key:
            if skip:
                skip_reason = reason or "SKIPPED_BY_OPERATOR"
                self.import_repo.mark_error(product_key, skip_reason)
                action = "skipped"
            else:
                self.import_repo.delete_by_product_key(product_key)
                action = "reset_to_unprocessed"

        return {
            "target_id": target_id,
            "workflow_id": wf_id,
            "product_key": product_key,
            "action": action,
        }

    def reset_all_blocked(
        self,
        *,
        skip: bool = False,
        reason: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find and reset (or skip) all stalled/quarantined/errored/incomplete workflows in the import ledger."""
        rows = self.connection.execute(
            "SELECT product_key, workflow_id, status, last_error FROM daily_product_video_import"
        ).fetchall()
        results: list[dict[str, Any]] = []
        for r in rows:
            pkey = r["product_key"]
            wf_id = r["workflow_id"]
            st = r["status"]
            is_blocked = st == "error" or bool(str(r["last_error"] or "").strip())
            blocked_reason = r["last_error"]
            if not is_blocked and st == "started":
                wf_data = self.workflow_repo.get_workflow(wf_id)
                if wf_data is None:
                    is_blocked = True
                    blocked_reason = "MISSING_WORKFLOW_AGGREGATE"
                else:
                    wf_state = str(wf_data[0].get("state") or "")
                    q_err = self._quarantined_outbox_error(wf_id, wf_state)
                    if q_err is not None:
                        is_blocked = True
                        blocked_reason = q_err
            if is_blocked:
                res = self.reset_product_or_workflow(
                    pkey,
                    skip=skip,
                    reason=reason or blocked_reason or "BLOCKED_WORKFLOW_CLEARED",
                )
                results.append(res)
        return results


    def approve_product_lock(
        self,
        target_id: str,
        owner_user_id: str = "local-owner",
    ) -> dict[str, Any]:
        """Approve Product Lock gate and resume the existing state machine.

        Guarantees that approval only advances the workflow state to BUILDING_RESOURCE_PACK
        and enqueues the next outbox job, with NO alternate fast-path bypassing the worker.
        """
        return approve_and_advance_workflow(
            connection=self.connection,
            target_id=target_id,
            owner_user_id=owner_user_id,
        )

    def get_workflow_status(self, workflow_id: str) -> dict[str, Any] | None:
        """Inspect workflow aggregate status, approval details, and research snapshot."""
        wf_data = self.workflow_repo.get_workflow(workflow_id)
        if wf_data is None:
            return None
        agg, version = wf_data

        pending_app_id = agg.get("pending_approval_id")
        approval_dict = None
        if pending_app_id:
            try:
                from video_factory.workflows.approvals import ApprovalService

                app_svc = ApprovalService(self.connection)
                req = app_svc.get_request(pending_app_id)
                approval_dict = {
                    "request_id": req.request_id,
                    "status": req.status.value,
                    "gate": req.payload.get("gate", agg.get("pending_approval_gate")),
                    "created_at": req.created_at,
                    "updated_at": req.updated_at,
                    "payload": req.payload,
                }
            except Exception:
                pass

        events = []
        try:
            cur = self.connection.execute(
                "SELECT event_id, event_type, timestamp, payload_json FROM workflow_event WHERE workflow_id = ? ORDER BY timestamp DESC LIMIT 10",
                (workflow_id,),
            )
            for r in cur.fetchall():
                events.append({
                    "event_id": r[0],
                    "event_type": r[1],
                    "timestamp": r[2],
                    "payload": json.loads(r[3]) if r[3] else {},
                })
        except Exception:
            pass

        latest_art = agg.get("latest_artifact")
        research_snapshot = None
        if latest_art and isinstance(latest_art, dict):
            rep = latest_art.get("report")
            rep_data = json.loads(rep) if isinstance(rep, str) else (rep or {})
            research_snapshot = {
                "snapshot_id": latest_art.get("snapshot_id") or rep_data.get("snapshot_id"),
                "status": rep_data.get("status"),
                "summary": rep_data.get("summary") or latest_art.get("summary"),
                "source_summary": rep_data.get("source_summary"),
                "discovered_sources": rep_data.get("discovered_sources", []),
                "canonical_products": rep_data.get("canonical_products", []),
                "media_assets": rep_data.get("media_assets", []),
                "warnings": rep_data.get("warnings", []),
                "conflicts": rep_data.get("conflicts", []),
                "facts": rep_data.get("key_facts") or rep_data.get("facts", []),
                "selling_points": rep_data.get("selling_points") or rep_data.get("selling_angles", []),
                "competitors": rep_data.get("competitors", []),
                "risks": rep_data.get("risks") or rep_data.get("concerns", []),
            }

        return {
            "workflow_id": workflow_id,
            "version": version,
            "state": agg.get("state"),
            "pending_approval_gate": agg.get("pending_approval_gate"),
            "pending_approval_id": pending_app_id,
            "product_key": agg.get("product_key"),
            "source_context": agg.get("source_context"),
            "latest_artifact": latest_art,
            "approval": approval_dict,
            "events": events,
            "research_snapshot": research_snapshot,
        }

    def run_research_worker(
        self,
        provider_mode: str = "configured",
        batch_size: int = 5,
        once: bool = True,
    ) -> Any:
        """Run durable worker one-shot to advance research jobs up to AwaitingProductLock."""
        if not self.db_path:
            raise ValueError("db_path required to launch durable worker")
        return run_worker(
            db_path=self.db_path,
            provider_mode=provider_mode,
            once=once,
            batch_size=batch_size,
            workflow_id_prefix="pv-daily-",
        )

    def start_fresh_creative(self, *, run_id, query, run_config, owner_user_id='local-owner', qc_authorization=None):
        """One fresh acquisition on the existing workflow/outbox, with stable resume identity."""
        import re
        from video_factory.creative.agents.contracts import BackendProfile
        from video_factory.creative.contracts import canonical_digest
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,96}',run_id) or not str(query).strip():
            raise ValueError('explicit run ID and product query required')
        profile=BackendProfile.from_dict(run_config['profile'])
        from video_factory.creative.agents.playbooks import select_playbook
        select_playbook(run_config['category'],profile,tuple(run_config['visible_details']))
        if profile.provider_kind!='google_flow' or profile.image_provider_kind!='google_flow':
            raise ValueError('this pilot is pinned to google_flow')
        if (run_config.get('max_products')!=1 or not 1<=run_config.get('max_scenes',0)<=5 or
            not 1<=run_config.get('image_call_limit',0)<=24 or not 1<=run_config.get('video_call_limit',0)<=12):
            raise ValueError('explicit single-product pilot call/scene caps required')
        if 'baseus' in query.casefold():
            raise ValueError('Baseus is excluded from this pilot')
        material={'query':query,'config':run_config,'owner':owner_user_id}
        fingerprint=canonical_digest(material)
        workflow_id='pv-creative-'+run_id
        existing=self.workflow_repo.get_workflow(workflow_id)
        if existing:
            if existing[0].get('creative_input_digest')!=fingerprint:
                raise ValueError('run ID already bound to different input')
            return {'workflow_id':workflow_id,'reused':True,'state':existing[0]['state']}
        for row in self.connection.execute("SELECT aggregate_json FROM workflow"):
            prior=json.loads(row[0])
            if prior.get('state')=='completed' and prior.get('creative_query','').casefold()==query.casefold():
                raise ValueError('product already has a completed creative output')
        from .creative_policy import preproduction_policy
        policy={**run_config, **preproduction_policy(run_config),
                'creative_mode':'four_agent','auto_export':True,'fresh_acquisition':True,
                'creative_input_digest':fingerprint,'query':query}
        pdp_url = query if str(query).startswith(('http://', 'https://')) else None
        start_product_video_workflow(self.workflow_repo,workflow_id,query,[query],
            pdp_url=pdp_url,
            owner_user_id=owner_user_id,project_id='proj_'+workflow_id,
            idempotency_key=workflow_id,execution_mode='auto',max_qc_retries=2,
            qc_authorization=qc_authorization,creative_policy=policy)
        return {'workflow_id':workflow_id,'reused':False,'state':'researching'}

    @staticmethod
    def _catalog_run_policy(run_config, *, preparation_id, product_key):
        from video_factory.creative.agents.contracts import BackendProfile
        from video_factory.creative.prepared_catalog import creative_profile_digest
        from video_factory.creative.contracts import canonical_digest
        profile = BackendProfile.from_dict(run_config['profile'])
        creative_profile_digest(run_config)
        if (not 1 <= run_config.get('max_scenes', 0) <= 5
                or not 1 <= run_config.get('image_call_limit', 0) <= 24
                or not 1 <= run_config.get('video_call_limit', 0) <= 12):
            raise ValueError('explicit scene/image/video call caps required')
        if profile.audio_policy != 'silent':
            raise ValueError('catalog production requires a silent profile')
        from .creative_policy import preproduction_policy
        return {**run_config, **preproduction_policy(run_config), 'creative_mode':'four_agent', 'auto_export':True,
                'fresh_acquisition':False, 'preparation_only':True,
                'preparation_id':preparation_id, 'query':product_key,
                'creative_input_digest':canonical_digest({'product_key':product_key, 'config':run_config})}

    def prepare_catalog(self, *, preparation_id, run_config, product_key=None,
                        source_workflow_id=None, owner_user_id='local-owner', target_date=None):
        """Prepare exactly one selected existing source; never run PI acquisition."""
        import re
        from video_factory.creative.contracts import canonical_digest
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,96}', preparation_id):
            raise ValueError('explicit stable preparation ID required')
        if bool(product_key) == bool(source_workflow_id):
            raise ValueError('select exactly one product key or source workflow ID')
        policy = self._catalog_run_policy(run_config, preparation_id=preparation_id,
                                          product_key=product_key or source_workflow_id)
        if product_key:
            # start_selection's validated local-image Exchange seam starts at
            # resource_pack, bypassing product_video.research entirely.
            result = self.start_selection([product_key], owner_user_id=owner_user_id,
                target_date=target_date, execution_mode='auto', max_qc_retries=2,
                creative_policy=policy, qc_authorization=run_config.get('qc_authorization'))
            if result.started:
                source_workflow_id = result.started[0].workflow_id
            elif (len(result.skipped) == 1 and result.skipped[0].reason == 'ALREADY_STARTED'
                  and result.skipped[0].workflow_id):
                source_workflow_id = result.skipped[0].workflow_id
            else:
                return {'action':'stop', 'skipped':[{'product_key':s.product_key, 'reason':s.reason}
                                                   for s in result.skipped]}
        data = self.workflow_repo.get_workflow(source_workflow_id)
        if data is None:
            raise KeyError(source_workflow_id)
        aggregate, version = data
        if aggregate.get('owner_user_id') != owner_user_id:
            raise ValueError('source owner mismatch')
        if aggregate.get('state') == 'skipped':
            from video_factory.application.operator_handler import (
                can_resume_legacy_invalid_response_skip, recover_creative_stage,
            )
            if can_resume_legacy_invalid_response_skip(self.connection, source_workflow_id):
                recover_creative_stage(self.connection, workflow_id=source_workflow_id,
                    owner_user_id=owner_user_id,
                    note='Resume the pinned batch after a recorded invalid Scene Plan response; preserve prior attempt and quota.')
                aggregate, version = self.workflow_repo.get_workflow(source_workflow_id)
        allowed = {'building_resource_pack', 'AwaitingProductLock', 'locking_resource_pack',
                   'creating_brief', 'AwaitingBriefApproval', 'creating_scene_plan', 'AwaitingSceneApproval'}
        recoverable_creative_failure = (
            aggregate.get('state') == ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
            and aggregate.get('failure_code') == 'CREATIVE_RESPONSE_INVALID'
            and aggregate.get('operator_action') == 'creative_review'
            and aggregate.get('operator_stage') in {'direction','script','storyboard'}
        )
        legacy_source_ready = (aggregate.get('state') == 'WaitingForOperator'
                               and isinstance(aggregate.get('locked_pack'), dict)
                               and not aggregate.get('creative_policy'))
        if (aggregate.get('state') not in allowed and not legacy_source_ready
                and not recoverable_creative_failure) or aggregate.get('production_run_id'):
            raise ValueError('source workflow is blocked, uncertain, completed, or already in production')
        quarantined = self.connection.execute(
            "SELECT last_error FROM workflow_outbox WHERE workflow_id=? AND state='quarantined' "
            "ORDER BY outbox_id DESC LIMIT 1", (source_workflow_id,)).fetchone()
        if quarantined:
            from .failure_classification import (FailureCategory, classify_failure,
                is_exhausted_legacy_creative_response)
            details = self.connection.execute(
                "SELECT attempt_count,max_attempts FROM workflow_outbox WHERE workflow_id=? "
                "AND state='quarantined' ORDER BY outbox_id DESC LIMIT 1", (source_workflow_id,)).fetchone()
            legacy_invalid = details and is_exhausted_legacy_creative_response(
                quarantined['last_error'], details['attempt_count'], details['max_attempts'])
            if (classify_failure(quarantined['last_error']) != FailureCategory.PRODUCT_REJECTED
                    and not legacy_invalid and not recoverable_creative_failure):
                raise ValueError('source workflow requires reconciliation')
        if aggregate.get('locked_pack') and str(policy.get('qc_level') or 'middle') != 'low':
            from video_factory.qc.models import QCStage
            from video_factory.qc.validation import require_current_pass_report
            require_current_pass_report(self.connection, workflow_id=source_workflow_id,
                stage=QCStage.IMAGE, artifact=aggregate.get('resource_pack') or {},
                owner_user_id=owner_user_id, project_id=aggregate.get('project_id'),
                provider_mode='configured')
        prior = aggregate.get('creative_policy') or {}
        if prior.get('preparation_id') and prior['preparation_id'] != preparation_id:
            raise ValueError('source already bound to a different preparation ID')
        if prior.get('preparation_id'):
            for key in ('preproduction_gate', 'preproduction_threshold'):
                if key in run_config and key in prior and run_config[key] != prior[key]:
                    raise ValueError(f'{key} is pinned for this preparation')
                if key in prior:
                    policy[key] = prior[key]
                else:
                    policy.pop(key, None)
        if prior.get('creative_mode') == 'four_agent':
            from video_factory.creative.prepared_catalog import creative_profile_digest
            if creative_profile_digest(prior) != creative_profile_digest(policy):
                raise ValueError('PROFILE_STALE: explicit new preparation revision required')
        # A source-only workflow may predate four-agent mode. Reusing its lock
        # and current Image QC is safe; legacy scripts require an explicit new
        # preparation revision instead of silently publishing old receipts.
        updated = {**aggregate, 'creative_policy':{**policy, 'query':prior.get('query') or policy['query']},
                   'execution_mode':'auto'}
        events = []
        outbox_msg = None
        if legacy_source_ready:
            updated['state'] = 'creating_brief'
            for k in ('operator_action', 'operator_stage', 'operator_reason', 'creative_brief',
                      'scene_plan', 'storyboard', 'latest_artifact', 'pending_approval_id', 'pending_approval_gate'):
                updated.pop(k, None)
        if updated['state'] == 'creating_brief':
            has_brief_job = self.connection.execute(
                "SELECT 1 FROM workflow_outbox WHERE workflow_id=? AND state IN ('pending','leased')",
                (source_workflow_id,)).fetchone()
            if not has_brief_job:
                from video_factory.workflows.envelope import build_envelope
                rp = aggregate.get('locked_pack') or aggregate.get('resource_pack') or {}
                product_id = str(rp.get('product_id') or rp.get('resource_pack_id') or rp.get('id')
                                 or f'prod_{source_workflow_id}')
                brief_config = {'resource_pack': rp, 'project_id': aggregate.get('project_id')}
                outbox_msg = build_envelope(
                    workflow_id=source_workflow_id, topic='job.dispatch',
                    idempotency_key=f'idemp_brief_{source_workflow_id}_v{version+1}',
                    payload={'job_name': 'product_video.creative_brief', 'workflow_id': source_workflow_id,
                             'args': [product_id, brief_config], 'kwargs': {},
                             'owner_user_id': owner_user_id, 'project_id': aggregate.get('project_id')}
                )
        if canonical_digest(updated) != canonical_digest(aggregate) or outbox_msg:
            self.workflow_repo.commit_workflow(source_workflow_id, json.dumps(updated), events, outbox_message=outbox_msg, expected_version=version)
        return {'workflow_id':source_workflow_id, 'state':updated['state'],
                'action':'prepared' if updated.get('prepared_product') else 'prepare',
                'reused':bool(aggregate.get('prepared_product'))}

    def advance_preparation(self, workflow_id, *, provider_mode='configured'):
        """Tick only source/creative work and publish before any media job."""
        from video_factory.creative.prepared_catalog import current_prepared_product
        data = self.workflow_repo.get_workflow(workflow_id)
        if data is None:
            raise KeyError(workflow_id)
        aggregate, version = data
        if not aggregate.get('creative_policy', {}).get('preparation_only'):
            raise ValueError('explicit preparation-only policy required')
        if aggregate['state'] == ProductVideoWorkflowState.AWAITING_SCENE_APPROVAL.value:
            prepared = current_prepared_product(self.connection, workflow_id, aggregate, provider_mode=provider_mode).to_dict()
            previous = aggregate.get('prepared_product')
            if previous and previous['digest'] != prepared['digest']:
                raise ValueError('PREPARED_STALE: explicit new preparation revision required')
            if not previous:
                aggregate['prepared_product'] = prepared
                self.workflow_repo.commit_workflow(workflow_id, json.dumps(aggregate), [], expected_version=version)
            return {'workflow_id':workflow_id, 'state':aggregate['state'], 'action':'prepared',
                    'prepared_ref':workflow_id, 'prepared_digest':prepared['digest']}
        if aggregate['state'] not in {'building_resource_pack', 'AwaitingProductLock', 'locking_resource_pack',
                                      'creating_brief', 'AwaitingBriefApproval', 'creating_scene_plan'}:
            return {'workflow_id':workflow_id, 'state':aggregate['state'], 'action':'stop'}
        return self.advance_creative(workflow_id, provider_mode=provider_mode, _preparation_tick=True)

    def produce_from_catalog(self, *, run_id, prepared_refs=None, limit=1, run_config,
                             owner_user_id='local-owner', target_date=None, target_batch_id=None,
                             provider_mode='configured'):
        """Pin one ordered selection and atomically claim its prepared variants.

        This operation invokes no model, evaluator, acquisition, or worker. The
        selected workflows continue through advance_creative's existing gates.
        """
        import re
        from .creative_policy import preproduction_policy
        from video_factory.creative.contracts import canonical_digest
        from video_factory.creative.prepared_catalog import creative_profile_digest, current_prepared_product

        preproduction_fields = preproduction_policy(run_config).keys()
        production_config = {key: value for key, value in run_config.items() if key not in preproduction_fields}
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,96}', run_id) or (limit is not None and (type(limit) is not int or limit < 1)):
            raise ValueError('explicit stable run ID and positive selection limit required')
        batch_date=target_date.isoformat() if hasattr(target_date,'isoformat') else str(target_date or '')
        if not prepared_refs and not batch_date:
            raise ValueError('date or explicit prepared references required; implicit backlog selection is disabled')
        if batch_date and target_batch_id is None:
            ref_batches = set()
            for ref in prepared_refs or []:
                source = self.workflow_repo.get_workflow(ref)
                source_context = (source[0].get('source_context') or {}) if source else {}
                ref_batches.add(str(source_context.get('source_batch_id') or ''))
            nonempty_batches = {value for value in ref_batches if value}
            if len(nonempty_batches) > 1 or (nonempty_batches and '' in ref_batches):
                raise ValueError('prepared refs span multiple source batches; pin one batch per production run')
            if nonempty_batches:
                target_batch_id=next(iter(nonempty_batches))
            elif not prepared_refs:
                pinned_dir=self.importer.find_latest_ready_batch(date.fromisoformat(batch_date))
                target_batch_id=self.importer.load_manifest(pinned_dir).batch_id
        self._catalog_run_policy(run_config, preparation_id='validation', product_key='selection')
        refs = list(dict.fromkeys(prepared_refs or []))
        requested_refs = list(refs)
        selection_id = 'pv-production-' + run_id
        fingerprint = canonical_digest({'refs':refs, 'limit':limit, 'date':batch_date, 'batch_id':target_batch_id,
                                        'config':run_config, 'owner':owner_user_id})
        existing = self.workflow_repo.get_workflow(selection_id)
        existing_version = None
        existing_selection = None
        if existing:
            existing_selection, existing_version = existing
            if existing_selection.get('selection_input_digest') == fingerprint:
                return {**existing_selection['selection_result'], 'reused':True}
            prior_refs = list(existing_selection.get('ordered_prepared_refs') or [])
            prior_fingerprint = canonical_digest({'refs':prior_refs, 'limit':limit, 'date':batch_date,
                'batch_id':target_batch_id, 'config':run_config, 'owner':owner_user_id})
            if (existing_selection.get('selection_input_digest') != prior_fingerprint
                    or requested_refs[:len(prior_refs)] != prior_refs
                    or len(requested_refs) <= len(prior_refs)):
                raise ValueError('production run ID already bound to different input')
            refs = requested_refs[len(prior_refs):]
            prior_selected = list((existing_selection.get('selection_result') or {}).get('selected') or [])
            if limit is not None and len(prior_selected) >= limit:
                refs = []
        if not refs:
            refs = [str(row['workflow_id']) for row in self.connection.execute(
                "SELECT workflow_id,aggregate_json FROM workflow ORDER BY workflow_id")
                    if json.loads(row['aggregate_json']).get('prepared_product') and
                    (json.loads(row['aggregate_json']).get('source_context') or {}).get('source_date') == batch_date and
                    (json.loads(row['aggregate_json']).get('source_context') or {}).get('source_batch_id') == target_batch_id]
        wanted_profile = creative_profile_digest(run_config)
        candidates, skipped = [], []
        accepted_products = set()
        active_products = {}
        from video_factory.creative.agents.video_review import require_final_pass
        from video_factory.integrations.composer import probe_video
        for row in self.connection.execute("SELECT workflow_id, aggregate_json FROM workflow"):
            prior = json.loads(row['aggregate_json'])
            prior_context = prior.get('source_context') or {}
            prior_prepared = prior.get('prepared_product') or {}
            prior_identity = (prior.get('resource_pack') or {}).get('product_identity') or {}
            prior_key = str(prior_context.get('canonical_product_key') or prior_prepared.get('product_key')
                            or prior.get('product_key') or '')
            prior_variant = str(prior_identity.get('variant') or prior_context.get('variant') or '')
            if (str(row['workflow_id']) not in refs and prior_key
                    and prior.get('state') not in {'completed', 'skipped', 'contract_broken'}):
                active_products.setdefault((prior_key.casefold(), prior_variant.casefold()), set()).add(
                    str(row['workflow_id']))
            if prior.get('state') != 'completed':
                continue
            final, exported = prior.get('render_result'), prior.get('export_result')
            if not prior_key or not isinstance(final, dict) or not isinstance(exported, dict):
                continue
            try:
                require_final_pass(final)
                raw = json.loads(exported['payload']) if isinstance(exported.get('payload'), str) else exported
                if probe_video(raw['file_path'], decode_all=True).sha256 != final['sha256']:
                    continue
            except (KeyError, TypeError, ValueError, OSError):
                continue
            accepted_products.add((prior_key, prior_variant))
        for ref in refs:
            data = self.workflow_repo.get_workflow(ref)
            if data is None:
                skipped.append({'prepared_ref':ref, 'reason':'PREPARED_NOT_FOUND'})
                continue
            aggregate, version = data
            identity = (aggregate.get('resource_pack') or {}).get('product_identity') or {}
            candidate_key = str((aggregate.get('source_context') or {}).get('canonical_product_key')
                                or (aggregate.get('prepared_product') or {}).get('product_key')
                                or aggregate.get('product_key') or '')
            candidate_variant = str(identity.get('variant') or
                                    (aggregate.get('source_context') or {}).get('variant') or '')
            if candidate_key and (candidate_key, candidate_variant) in accepted_products:
                skipped.append({'prepared_ref':ref, 'reason':'CURRENT_ACCEPTED_FINAL_EXISTS'})
                continue
            duplicates = active_products.get((candidate_key.casefold(), candidate_variant.casefold()), set()) - {ref}
            if duplicates:
                skipped.append({'prepared_ref':ref, 'reason':'ACTIVE_IDENTITY_VARIANT:' + ','.join(sorted(duplicates))})
                continue
            if batch_date and (aggregate.get('source_context') or {}).get('source_date') != batch_date:
                skipped.append({'prepared_ref':ref, 'reason':'OUTSIDE_BATCH_DATE'})
                continue
            if target_batch_id and (aggregate.get('source_context') or {}).get('source_batch_id') != target_batch_id:
                skipped.append({'prepared_ref':ref, 'reason':'OUTSIDE_PINNED_BATCH'})
                continue
            if aggregate.get('owner_user_id') != owner_user_id:
                skipped.append({'prepared_ref':ref, 'reason':'OWNER_MISMATCH'})
                continue
            if aggregate.get('state') != ProductVideoWorkflowState.AWAITING_SCENE_APPROVAL.value:
                skipped.append({'prepared_ref':ref, 'reason':'BLOCKED_UNCERTAIN_OR_COMPLETED'})
                continue
            if aggregate.get('production_run_id'):
                skipped.append({'prepared_ref':ref, 'reason':'ALREADY_CLAIMED'})
                continue
            saved = aggregate.get('prepared_product')
            try:
                if not saved:
                    raise ValueError('PREPARED_NOT_PUBLISHED')
                if saved['creative_profile_digest'] != wanted_profile:
                    raise ValueError('PROFILE_STALE: explicit preparation/recheck required')
                current = current_prepared_product(self.connection, ref, aggregate, provider_mode=provider_mode)
                if current.digest != saved['digest']:
                    raise ValueError('PREPARED_STALE: dependency bindings changed')
                if self.connection.execute("SELECT 1 FROM workflow_outbox WHERE workflow_id=? "
                    "AND state IN ('pending','leased','quarantined')", (ref,)).fetchone():
                    raise ValueError('ACTIVE_OR_UNCERTAIN_WORKFLOW')
                receipt = saved['source_qc_receipts'][0]
                report_row = None
                if receipt.get('report_id'):
                    report_row = self.connection.execute('SELECT * FROM product_video_qc_reports WHERE report_id=?',
                                                         (receipt['report_id'],)).fetchone()
                    if report_row is None:
                        raise ValueError('SOURCE_QC_RECEIPT_MISSING')
                elif str((aggregate.get('creative_policy') or {}).get('qc_level') or 'middle') != 'low':
                    raise ValueError('SOURCE_QC_RECEIPT_MISSING')
                candidates.append((ref, aggregate, version, tuple(report_row) if report_row else None))
            except (KeyError, TypeError, ValueError) as exc:
                skipped.append({'prepared_ref':ref, 'reason':str(exc)})
        # Candidate validation can read existing schemas. The claim transaction
        # below does no schema/DDL work, so BEGIN IMMEDIATE remains atomic.
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            existing = self.workflow_repo.get_workflow(selection_id)
            if existing:
                if existing[0].get('selection_input_digest') == fingerprint:
                    self.connection.rollback()
                    return {**existing[0]['selection_result'], 'reused':True}
                if (existing_selection is None or existing[1] != existing_version
                        or existing[0].get('selection_input_digest') != existing_selection.get('selection_input_digest')):
                    raise ValueError('production run ID already bound to different input')
            selected, variants, selected_identity_variants = [], set(), set()
            prior_variants = set()
            for row in self.connection.execute('SELECT aggregate_json FROM workflow'):
                prior = json.loads(row[0])
                if prior.get('production_variant_digest'):
                    prior_variants.add(prior['production_variant_digest'])
            for ref, aggregate, version, report_snapshot in candidates:
                if limit is not None and len(selected) >= limit:
                    break
                fresh = self.workflow_repo.get_workflow(ref)
                receipt_id = aggregate['prepared_product']['source_qc_receipts'][0].get('report_id')
                report_row = (self.connection.execute('SELECT * FROM product_video_qc_reports WHERE report_id=?',
                    (receipt_id,)).fetchone() if receipt_id else None)
                low_without_report = (not receipt_id and report_snapshot is None
                    and str((aggregate.get('creative_policy') or {}).get('qc_level') or 'middle') == 'low')
                report_unchanged = low_without_report if not receipt_id else bool(
                    report_row and tuple(report_row) == report_snapshot)
                if fresh[1] != version or not report_unchanged:
                    skipped.append({'prepared_ref':ref, 'reason':'PREPARED_CHANGED_DURING_CLAIM'})
                    continue
                prepared = aggregate['prepared_product']
                identity = (aggregate.get('resource_pack') or {}).get('product_identity') or {}
                identity_variant=(str(prepared['product_key']).casefold(),
                    str(identity.get('variant') or (aggregate.get('source_context') or {}).get('variant') or '').casefold())
                if identity_variant in selected_identity_variants:
                    skipped.append({'prepared_ref':ref, 'reason':'DUPLICATE_IDENTITY_VARIANT_IN_SELECTION'})
                    continue
                variant = canonical_digest({'product':prepared['product_key'], 'script':prepared['script_digest'],
                                            'profile':wanted_profile})
                if variant in prior_variants or variant in variants:
                    skipped.append({'prepared_ref':ref, 'reason':'VARIANT_ALREADY_CLAIMED'})
                    continue
                variants.add(variant)
                selected_identity_variants.add(identity_variant)
                auth = aggregate.get('qc_authorization') or run_config.get('qc_authorization')
                policy = {**aggregate['creative_policy'], **production_config, 'preparation_only':False,
                          'production_only':True, 'fresh_acquisition':False}
                claimed = {**aggregate, 'creative_policy':policy, 'production_run_id':selection_id,
                           'production_variant_digest':variant}
                if auth:
                    claimed['qc_authorization'] = auth
                self.workflow_repo.commit_workflow(ref, json.dumps(claimed), [], expected_version=version,
                                                   in_transaction=True)
                selected.append({'prepared_ref':ref, 'workflow_id':ref, 'prepared_digest':prepared['digest'],
                                 'source_run_id':prepared['artifact_refs']['source_workflow_id'],
                                 'preparation_run_id':prepared['artifact_refs']['preparation_run_id'],
                                 'variant_digest':variant})
            previous_result = (existing_selection or {}).get('selection_result') or {}
            all_selected = [*(previous_result.get('selected') or []), *selected]
            all_skipped = [*(previous_result.get('skipped') or []), *skipped]
            result = {'production_run_id':selection_id, 'selected':all_selected, 'skipped':all_skipped,
                      'requested':limit if limit is not None else 'all',
                      'shortfall':max(0, limit-len(all_selected)) if limit is not None else 0, 'reused':False}
            selection = {'state':'completed', 'workflow_kind':'catalog_production_selection',
                         'selection_input_digest':fingerprint, 'selection_result':result,
                         'owner_user_id':owner_user_id, 'ordered_prepared_refs':requested_refs}
            self.workflow_repo.commit_workflow(selection_id, json.dumps(selection), [],
                                               expected_version=existing_version if existing_selection else 0,
                                               in_transaction=True)
            self.connection.commit()
            return result
        except BaseException:
            self.connection.rollback()
            raise

    def catalog_product_outcome(self, canonical_product_key: str, variant: str = '') -> dict[str, str] | None:
        """Return an existing identity/variant outcome that must be excluded from a new selection."""
        from video_factory.creative.agents.video_review import require_final_pass
        from video_factory.integrations.composer import probe_video

        key = str(canonical_product_key or '').strip().casefold()
        wanted_variant = str(variant or '').strip().casefold()
        if not key:
            return None
        for row in self.connection.execute("SELECT workflow_id, aggregate_json FROM workflow"):
            aggregate = json.loads(row['aggregate_json'])
            source = aggregate.get('source_context') or {}
            prepared = aggregate.get('prepared_product') or {}
            identity = (aggregate.get('resource_pack') or {}).get('product_identity') or {}
            prior_key = str(source.get('canonical_product_key') or prepared.get('product_key')
                            or aggregate.get('product_key') or '').strip().casefold()
            prior_variant = str(identity.get('variant') or source.get('variant') or '').strip().casefold()
            if (prior_key, prior_variant) != (key, wanted_variant):
                continue
            workflow_id = str(row['workflow_id'])
            state = str(aggregate.get('state') or '')
            if state == ProductVideoWorkflowState.COMPLETED.value:
                final, exported = aggregate.get('render_result'), aggregate.get('export_result')
                try:
                    require_final_pass(final)
                    export = json.loads(exported['payload']) if isinstance(exported.get('payload'), str) else exported
                    if probe_video(export['file_path'], decode_all=True).sha256 == final['sha256']:
                        return {'workflow_id':workflow_id, 'reason':'CURRENT_ACCEPTED_FINAL_EXISTS'}
                except (KeyError, TypeError, ValueError, OSError, AttributeError):
                    pass
                return {'workflow_id':workflow_id, 'reason':'COMPLETED_FINAL_NOT_CURRENT'}
            if state in {'skipped', 'contract_broken'}:
                if state == 'skipped':
                    from video_factory.application.operator_handler import can_resume_legacy_invalid_response_skip
                    if can_resume_legacy_invalid_response_skip(self.connection, workflow_id):
                        return {'workflow_id':workflow_id, 'reason':'RESUMABLE_LEGACY_CREATIVE_FAILURE'}
                reason = (aggregate.get('skip_reason') or aggregate.get('failure_code')
                          or aggregate.get('operator_reason') or state)
                return {'workflow_id':workflow_id, 'reason':f'TERMINAL_WORKFLOW:{reason}'}
            if (state == ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
                    and aggregate.get('failure_code') == 'CREATIVE_RESPONSE_INVALID'
                    and aggregate.get('operator_action') == 'creative_review'
                    and aggregate.get('operator_stage') in {'direction','script','storyboard'}):
                return {'workflow_id':workflow_id, 'reason':'RESUMABLE_CREATIVE_FAILURE'}
            return {'workflow_id':workflow_id, 'reason':f'ACTIVE_IDENTITY_VARIANT:{workflow_id}'}
        return None

    def revalidate_completed_final(self, workflow_id: str) -> dict[str, Any]:
        """Revalidate final technical bytes without repeating post-render content QC."""
        from video_factory.creative.agents.video_review import require_final_pass

        data = self.workflow_repo.get_workflow(workflow_id)
        if not data or data[0].get('state') != ProductVideoWorkflowState.COMPLETED.value:
            return {'status':'blocked', 'reason':'WORKFLOW_NOT_COMPLETED'}
        aggregate, _version = data
        final = aggregate.get('render_result') or {}
        exported = aggregate.get('export_result') or {}
        try:
            require_final_pass(final)
            raw = json.loads(exported['payload']) if isinstance(exported.get('payload'), str) else exported
            if self._export_sha(raw) == final.get('sha256'):
                return {'status':'accepted', 'workflow_id':workflow_id, 'sha256':final['sha256'], 'reused':True}
        except (KeyError, TypeError, ValueError, OSError, AttributeError) as exc:
            return {'status':'unresolved', 'workflow_id':workflow_id,
                'reason':f'FINAL_TECHNICAL_REVALIDATION_FAILED:{str(exc)[:300]}'}
        return {'status':'unresolved', 'workflow_id':workflow_id, 'reason':'FINAL_EXPORT_SHA_MISMATCH'}

    @staticmethod
    def _export_sha(exported: dict[str, Any]) -> str:
        from video_factory.integrations.composer import probe_video
        return probe_video(exported['file_path'], decode_all=True).sha256

    def _verified_render_finalization_checkpoint(self, workflow_id: str, batch_id: str,
                                                 provider_kind: str) -> dict[str, Any] | None:
        """Prove only polling/finalization remains; never admit a planned scene send."""
        configured_db = os.environ.get('HERMES_VIDEO_FACTORY_DB_PATH', '').strip()
        if configured_db:
            render_db = Path(configured_db).expanduser().resolve()
        else:
            from video_factory.constants import get_hermes_home
            render_db = get_hermes_home() / 'data' / 'video_factory.sqlite'
        if not render_db.is_file():
            return None
        try:
            conn = sqlite3.connect(f"file:{render_db.as_posix()}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            batch = conn.execute(
                "SELECT project_id, workflow_id, scene_count FROM video_factory_render_batches "
                "WHERE render_batch_id=?", (batch_id,),
            ).fetchone()
            if batch is None or batch['workflow_id'] != workflow_id or int(batch['scene_count']) < 1:
                return None
            rows = conn.execute(
                "SELECT operation_id, scene_id, status, provider_operation_id, output_path, "
                "output_sha256, provider_kind, render_batch_id FROM video_factory_scene_operation "
                "WHERE render_batch_id=? ORDER BY scene_order", (batch_id,),
            ).fetchall()
            if len(rows) != int(batch['scene_count']):
                return None
            scene_evidence = []
            active = []
            for index, row in enumerate(rows):
                if (not row['provider_operation_id'] or row['provider_kind'] != provider_kind
                        or row['render_batch_id'] != batch_id):
                    return None
                if row['status'] == 'completed':
                    output = Path(str(row['output_path'] or ''))
                    expected = str(row['output_sha256'] or '').removeprefix('sha256:').lower()
                    if (not output.is_file() or not expected
                            or hashlib.sha256(output.read_bytes()).hexdigest() != expected):
                        return None
                elif row['status'] in {'submitted', 'running'} and index == len(rows) - 1:
                    active.append(row)
                else:
                    return None
                scene_evidence.append({
                    'operation_id': row['operation_id'], 'scene_id': row['scene_id'],
                    'provider_operation_id': row['provider_operation_id'], 'status': row['status'],
                    'sha256': expected if row['status'] == 'completed' else None,
                })
            if len(active) > 1:
                return None
            return {'project_id': batch['project_id'], 'scenes': scene_evidence}
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return None
        finally:
            try:
                conn.close()
            except UnboundLocalError:
                pass

    def advance_creative(self, workflow_id, *, provider_mode='configured', _preparation_tick=False):
        """One start/watch tick, always using the existing durable worker and approval path."""
        if not self.db_path:
            raise ValueError('worker-local db_path required')
        data=self.workflow_repo.get_workflow(workflow_id)
        if data is None:
            raise KeyError(workflow_id)
        agg,version=data
        policy=agg.get('creative_policy',{})
        if policy.get('preparation_only') and not _preparation_tick:
            return self.advance_preparation(workflow_id, provider_mode=provider_mode)
        if policy.get('creative_mode')!='four_agent':
            raise ValueError('explicit creative run policy required')
        if agg['state']=='completed' and provider_mode=='configured':
            from video_factory.integrations.composer import probe_video
            final=agg.get('render_result') or {}
            exported=agg.get('export_result') or {}
            raw=json.loads(exported['payload']) if isinstance(exported.get('payload'),str) else exported
            if not final.get('sha256') or probe_video(raw['file_path'],decode_all=True).sha256 != final['sha256']:
                raise ValueError('accepted export bytes no longer match the technical render SHA')
        if agg['state'] in {'completed','skipped','WaitingForOperator','contract_broken'}:
            return {'state':agg['state'],'action':'stop'}
        from video_factory.workflows.runtime_control import (
            get_pipeline_pause,
            is_shared_infra_blocker,
            workflow_has_inflight_operation,
        )
        pause=get_pipeline_pause(self.connection)
        if pause and pause.get('paused'):
            pause_code = str(pause.get('code') or '')
            paused_wf = str(pause.get('workflow_id') or '')
            has_inflight = (
                pause_code == "PROVIDER_CAPACITY_WAIT"
                and workflow_has_inflight_operation(self.connection, workflow_id)
            )
            if not has_inflight:
                if is_shared_infra_blocker(pause_code) or (paused_wf and paused_wf == workflow_id) or not paused_wf:
                    return {'workflow_id':workflow_id,'state':agg['state'],'action':'blocker',
                        'code':pause.get('code'),'reason':pause.get('message') or 'pipeline paused'}
        if policy.get('production_only') and str(policy.get('qc_level') or 'middle') != 'low':
            from video_factory.qc.models import QCStage
            from video_factory.qc.validation import require_current_pass_report, QCValidationError
            try:
                require_current_pass_report(self.connection, workflow_id=workflow_id,
                    stage=QCStage.IMAGE, artifact=agg.get('resource_pack') or {},
                    owner_user_id=agg.get('owner_user_id'), project_id=agg.get('project_id'),
                    provider_mode=provider_mode)
            except QCValidationError as exc:
                return {'workflow_id':workflow_id, 'state':agg['state'], 'action':'stop',
                        'code':exc.code, 'reason':exc.reason}
        waiting={s.value for s in (
            ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK,ProductVideoWorkflowState.AWAITING_BRIEF_APPROVAL,
            ProductVideoWorkflowState.AWAITING_SCENE_APPROVAL,ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL,
            ProductVideoWorkflowState.AWAITING_FINAL_APPROVAL)}
        if agg['state'] in waiting:
            if (
                agg['state'] == ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL.value
                and str(policy.get('qc_level') or 'middle') != 'low'
            ):
                artifact = agg.get('latest_artifact')
                receipt = artifact.get('keyframe_qc_receipt') if isinstance(artifact, dict) else None
                package_digest = str(artifact.get('package_digest') or '') if isinstance(artifact, dict) else ''
                receipt_is_current_pass = (
                    isinstance(receipt, dict)
                    and receipt.get('stage') == 'keyframe'
                    and receipt.get('verdict') == 'pass'
                    and receipt.get('artifact_digest') == package_digest
                )
                if not receipt_is_current_pass and isinstance(artifact, dict):
                    from video_factory.qc.models import QCStage, QCVerdict
                    from video_factory.qc.validation import (
                        QCValidationError,
                        require_current_pass_report,
                    )

                    try:
                        report = require_current_pass_report(
                            self.connection,
                            workflow_id=workflow_id,
                            stage=QCStage.KEYFRAME,
                            artifact=artifact,
                            owner_user_id=agg.get('owner_user_id'),
                            project_id=agg.get('project_id'),
                            provider_mode=provider_mode,
                        )
                    except QCValidationError:
                        report = None
                    if report is not None and report.verdict == QCVerdict.PASS:
                        artifact = dict(artifact)
                        artifact['keyframe_qc_receipt'] = {
                            'report_id': report.report_id,
                            'stage': QCStage.KEYFRAME.value,
                            'verdict': report.verdict.value,
                            'score': report.score,
                            'artifact_digest': report.artifact_digest,
                            'findings': report.findings,
                        }
                        recovered = dict(agg)
                        recovered['storyboard'] = artifact
                        recovered['latest_artifact'] = artifact
                        if recovered.get('failure_category') == FailureCategory.QC_INFRA_PAUSED.value:
                            for key in ('failure_category', 'failure_code', 'failure_reason', 'operator_reason'):
                                recovered.pop(key, None)
                        self.workflow_repo.commit_workflow(
                            workflow_id,
                            json.dumps(recovered, ensure_ascii=False),
                            [{
                                'event_id': f'evt_keyframe_qc_receipt_recovered_{workflow_id}_{report.report_id}',
                                'command_id': f'cmd_keyframe_qc_receipt_recovered_{workflow_id}_{report.report_id}',
                                'event_type': 'KeyframeQCPassReceiptRecovered',
                                'timestamp': datetime.now(timezone.utc).isoformat(),
                                'payload': {
                                    'report_id': report.report_id,
                                    'artifact_digest': report.artifact_digest,
                                },
                            }],
                            expected_version=version,
                        )
                        version += 1
                        agg = recovered
            if agg['state']!='AwaitingProductLock':
                from .creative_policy import evaluate_auto_gate
                from video_factory.creative.agents.contracts import CreativeReview
                artifact=agg['latest_artifact']
                reviews=tuple(CreativeReview.from_dict(artifact[k]) for k in
                    ('storyboard_review','final_review') if k in artifact)
                decision=evaluate_auto_gate(state=agg['state'],artifact=artifact,reviews=reviews,policy=policy)
                if decision['action']!='advance':
                    return {'workflow_id':workflow_id, **decision,'state':agg['state']}
            auth = agg.get('qc_authorization')
            adv_payload = {
                'authorize_cost': bool(auth),
                'provider_mode': provider_mode,
                'automation_actor': 'creative_auto_policy',
            }
            if auth:
                adv_payload['qc_authorization'] = auth
            approve_and_advance_workflow(
                connection=self.connection,
                target_id=workflow_id,
                owner_user_id=agg['owner_user_id'],
                provider_mode=provider_mode,
                payload=adv_payload,
            )
        if agg['state'] == ProductVideoWorkflowState.RENDERING.value:
            outbox = self.connection.execute(
                "SELECT outbox_id, state, attempt_count, last_error, message FROM workflow_outbox "
                "WHERE workflow_id=? ORDER BY outbox_id DESC LIMIT 1",
                (workflow_id,),
            ).fetchone()
            render_batch_table = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_factory_render_batches'"
            ).fetchone()
            has_render_batch = bool(
                render_batch_table
                and self.connection.execute(
                    "SELECT 1 FROM video_factory_render_batches WHERE workflow_id=? LIMIT 1",
                    (workflow_id,),
                ).fetchone()
            )
            known_preflight_error = (
                not agg.get('active_render_job_id')
                and outbox is not None
                and outbox['state'] == 'quarantined'
                and any(error in str(outbox['last_error'] or '') for error in (
                    'Four-agent render requires an unchanged storyboard package',
                    'KEYFRAME_QC_REQUIRED: END keyframe is not permitted by the scene policy',
                ))
                and not has_render_batch
            )
            bound_render_message = False
            if outbox is not None:
                try:
                    envelope = json.loads(outbox['message'])
                    if isinstance(envelope, str):
                        envelope = json.loads(envelope)
                    payload = envelope.get('payload') or {}
                    bound_render_message = (
                        payload.get('job_name') == 'product_video.render'
                        and payload.get('workflow_id') == workflow_id
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            finalization_recovery_used = self.connection.execute(
                "SELECT 1 FROM workflow_event WHERE workflow_id=? AND event_type='RenderFinalizationResumed' LIMIT 1",
                (workflow_id,),
            ).fetchone() is not None
            uncertain_checkpointed_render = (
                bool(agg.get('active_render_job_id'))
                and str(agg.get('active_render_job_id')).startswith('batch_')
                and outbox is not None
                and outbox['state'] == 'quarantined'
                and any(code in str(outbox['last_error'] or '') for code in (
                    '[RECONCILIATION_REQUIRED]',
                    '[GOOGLE_FLOW_VIDEO_ACCEPTANCE_UNKNOWN]',
                    '[UNKNOWN_PAID_OUTCOME]',
                ))
                and bound_render_message
                and not finalization_recovery_used
            )
            pinned_storyboard = agg.get('latest_artifact') or agg.get('storyboard') or {}
            pinned_provider = str((pinned_storyboard.get('backend_profile') or {}).get('provider_kind') or '')
            finalized_render_evidence = None
            exhausted_render_finalize = (
                bool(agg.get('active_render_job_id'))
                and str(agg.get('active_render_job_id')).startswith('batch_')
                and outbox is not None
                and outbox['state'] == 'quarantined'
                and 'max_attempts_exceeded_on_claim' in str(outbox['last_error'] or '')
                and bound_render_message
                and pinned_provider in {'google_flow', 'vertex_ai'}
                and not finalization_recovery_used
            )
            if exhausted_render_finalize:
                finalized_render_evidence = self._verified_render_finalization_checkpoint(
                    workflow_id, str(agg['active_render_job_id']), pinned_provider,
                )
                exhausted_render_finalize = bool(
                    finalized_render_evidence
                    and finalized_render_evidence.get('project_id') == agg.get('project_id')
                )
            legacy_storyboard_gate = (
                not agg.get('active_render_job_id')
                and outbox is not None
                and outbox['state'] == 'quarantined'
                and 'Current full storyboard content PASS with unchanged pixels is required'
                in str(outbox['last_error'] or '')
            )
            if legacy_storyboard_gate:
                try:
                    recovery = self._resume_verified_storyboard_quarantine(workflow_id)
                except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
                    return {'workflow_id':workflow_id,'state':agg['state'],'action':'quarantined',
                        'code':'STORYBOARD_RECOVERY_NOT_SAFE','reason':str(exc)}
                if recovery.get('status') == 'requeued':
                    version += 1
            if exhausted_render_finalize:
                from video_factory.workflows.outbox_repository import OutboxRepository

                audit_payload = {
                    'outbox_id': int(outbox['outbox_id']),
                    'previous_error': str(outbox['last_error'] or ''),
                    'batch_id': str(agg['active_render_job_id']),
                    'reason': 'Every scene is completed with a matching output SHA except at most the final already-accepted scene; grant one delivery to poll its pinned operation and finalize without submitting a scene.',
                    'provider_kind': pinned_provider,
                    'project_id': finalized_render_evidence['project_id'],
                    'scene_operations': finalized_render_evidence['scenes'],
                }
                with self.connection:
                    self.workflow_repo.commit_workflow(
                        workflow_id,
                        json.dumps(dict(agg), ensure_ascii=False),
                        [{
                            'event_id': f"evt_renderfinalizationresumed_{workflow_id}_{outbox['outbox_id']}_{outbox['attempt_count']}",
                            'command_id': f"cmd_renderfinalizationresumed_{workflow_id}_{outbox['outbox_id']}_{outbox['attempt_count']}",
                            'event_type': 'RenderFinalizationResumed',
                            'timestamp': datetime.now(timezone.utc).isoformat(),
                            'payload': audit_payload,
                        }],
                        expected_version=version,
                        in_transaction=True,
                    )
                    updated = self.connection.execute(
                        "UPDATE workflow_outbox SET max_attempts=attempt_count+1 "
                        "WHERE outbox_id=? AND state='quarantined' AND attempt_count>=max_attempts",
                        (int(outbox['outbox_id']),),
                    )
                    if updated.rowcount != 1 or not OutboxRepository(self.connection).requeue_quarantined_in_transaction(
                        int(outbox['outbox_id']), preserve_attempts=True,
                    ):
                        raise RuntimeError('Exhausted render changed before verified finalization resume')
                version += 1
            elif known_preflight_error or uncertain_checkpointed_render:
                from video_factory.workflows.outbox_repository import OutboxRepository

                next_aggregate = dict(agg)
                if known_preflight_error:
                    event_type = 'RenderPreflightFailureRecovered'
                    reason = 'Render digest preflight was fixed; no render batch/provider operation was created.'
                    audit_payload = {
                        'outbox_id': int(outbox['outbox_id']),
                        'previous_error': str(outbox['last_error'] or ''),
                        'reason': reason,
                        'storyboard_digest': str((agg.get('storyboard') or {}).get('package_digest') or ''),
                    }
                else:
                    event_type = 'RenderReconciliationResumed'
                    reason = 'Resume the checkpointed render batch by polling its existing scene operations; no new batch is created.'
                    audit_payload = {
                        'outbox_id': int(outbox['outbox_id']),
                        'previous_error': str(outbox['last_error'] or ''),
                        'batch_id': str(agg['active_render_job_id']),
                        'delivery_attempt_count': int(outbox['attempt_count']),
                        'reason': reason,
                    }
                with self.connection:
                    self.workflow_repo.commit_workflow(
                        workflow_id,
                        json.dumps(next_aggregate, ensure_ascii=False),
                        [{
                            'event_id': f"evt_{event_type.lower()}_{workflow_id}_{outbox['outbox_id']}_{outbox['attempt_count']}",
                            'command_id': f"cmd_{event_type.lower()}_{workflow_id}_{outbox['outbox_id']}_{outbox['attempt_count']}",
                            'event_type': event_type,
                            'timestamp': datetime.now(timezone.utc).isoformat(),
                            'payload': audit_payload,
                        }],
                        expected_version=version,
                        in_transaction=True,
                    )
                    if not OutboxRepository(self.connection).requeue_quarantined_in_transaction(
                        int(outbox['outbox_id']), preserve_attempts=True,
                    ):
                        raise RuntimeError('Quarantined render job changed before safe resume')
                version += 1
        report=run_worker(db_path=self.db_path,provider_mode=provider_mode,once=True,batch_size=1,
                          workflow_ids=[workflow_id],render_poll_timeout=900.0)
        current=self.workflow_repo.get_workflow(workflow_id)[0]
        if current['state'] in waiting and current['state'] != agg['state']:
            # A recovered job can finish while its old quarantined delivery is
            # retained. Let the next tick validate the new gate's own receipt.
            return {'workflow_id':workflow_id, 'state':current['state'], 'action':'worker_tick'}
        outbox=self.connection.execute(
            "SELECT state, attempt_count, max_attempts, next_attempt_at, lease_expires_at, last_error "
            "FROM workflow_outbox WHERE workflow_id=? ORDER BY outbox_id DESC LIMIT 1",
            (workflow_id,),
        ).fetchone()
        if outbox is not None:
            status=str(outbox['state'])
            if status == 'pending':
                return {'workflow_id':workflow_id,'state':current['state'],
                    'action':'retry_scheduled' if outbox['next_attempt_at'] else 'pending',
                    'failed':report.failed if report else None,'next_attempt_at':outbox['next_attempt_at'],
                    'last_error':outbox['last_error']}
            if status == 'leased':
                return {'workflow_id':workflow_id,'state':current['state'],'action':'running',
                    'failed':report.failed if report else None,'next_attempt_at':outbox['lease_expires_at']}
            if status == 'quarantined':
                exhausted=int(outbox['attempt_count']) >= int(outbox['max_attempts'])
                category=classify_failure(outbox['last_error'])
                from .failure_classification import is_exhausted_legacy_creative_response
                legacy_invalid = is_exhausted_legacy_creative_response(
                    outbox['last_error'], outbox['attempt_count'], outbox['max_attempts'])
                if category == FailureCategory.PRODUCT_REJECTED or (exhausted and legacy_invalid):
                    from video_factory.workflows.runtime_control import skip_unresolved_workflow
                    reason = (outbox['last_error'] if category == FailureCategory.PRODUCT_REJECTED else
                        'CREATIVE_RESPONSE_INVALID: legacy Director prompt response exhausted its retry deliveries')
                    skip_unresolved_workflow(self.connection, workflow_id,
                        str(reason or 'Known product failure exhausted retries'),
                        known_product_failure=True)
                    return {'workflow_id':workflow_id,'state':'skipped','action':'skipped',
                        'reason':reason}
                return {'workflow_id':workflow_id,'state':current['state'],
                    'action':'quarantined',
                    'failed':report.failed if report else None,'last_error':outbox['last_error']}
        return {'workflow_id':workflow_id, 'state':current['state'],'action':'worker_tick','failed':report.failed if report else None}

    def _resume_verified_storyboard_quarantine(self, workflow_id: str):
        """Resume only a render quarantined by the superseded storyboard content gate."""
        import hashlib
        from datetime import datetime, timezone
        from video_factory.creative.agents.gates import require_storyboard_qc_pass
        from video_factory.creative.storyboard import storyboard_package_digest
        from video_factory.workflows.outbox_repository import OutboxRepository

        blocked_gate_text = 'Current full storyboard content PASS with unchanged pixels is required'
        current = self.workflow_repo.get_workflow(workflow_id)
        if current is None:
            return {'status':'not_found','workflow_id':workflow_id}
        aggregate, version = current
        if aggregate.get('state') == 'completed':
            return {'status':'already_completed','workflow_id':workflow_id}
        if aggregate.get('state') != 'rendering':
            raise ValueError('workflow is not at the render stage')

        outbox = self.connection.execute(
            "SELECT outbox_id, state, attempt_count, max_attempts, last_error, message FROM workflow_outbox "
            "WHERE workflow_id=? ORDER BY outbox_id DESC LIMIT 1", (workflow_id,)
        ).fetchone()
        if outbox is None:
            raise ValueError('render outbox record is missing')
        if outbox['state'] in {'pending', 'leased'}:
            return {'status':'already_pending' if outbox['state'] == 'pending' else 'already_running',
                    'workflow_id':workflow_id}
        if outbox['state'] != 'quarantined' or blocked_gate_text not in str(outbox['last_error'] or ''):
            raise ValueError('render outbox is not quarantined for the superseded storyboard gate')
        if int(outbox['attempt_count']) >= int(outbox['max_attempts']):
            raise ValueError('render outbox delivery attempts are exhausted')
        message = json.loads(outbox['message'])
        payload = message.get('payload') or {}
        if (payload.get('job_name') != 'product_video.render'
                or payload.get('workflow_id') != workflow_id):
            raise ValueError('quarantined outbox is not bound to this render workflow')

        storyboard = aggregate.get('latest_artifact') or {}
        receipt = storyboard.get('keyframe_qc_receipt') or {}
        if (storyboard.get('workflow_id') != workflow_id
                or storyboard.get('project_id') != aggregate.get('project_id')
                or storyboard.get('creative_mode') != 'four_agent'
                or storyboard.get('package_digest') != storyboard_package_digest(storyboard)):
            raise ValueError('stored storyboard digest or workflow binding is stale')
        try:
            require_storyboard_qc_pass(storyboard)
        except ValueError as exc:
            raise ValueError(f'storyboard QC receipt is not current: {exc}') from exc

        report_id = str(receipt.get('report_id') or '')
        report = self.connection.execute(
            "SELECT workflow_id, stage, artifact_digest, verdict, findings_json "
            "FROM product_video_qc_reports WHERE report_id=?", (report_id,)
        ).fetchone() if report_id else None
        if (report is None or report['workflow_id'] != workflow_id or report['stage'] != 'keyframe'
                or report['verdict'] != 'pass' or report['artifact_digest'] != storyboard['package_digest']):
            raise ValueError('persisted keyframe QC report is missing or does not match the storyboard digest')

        generated = storyboard.get('generated_keyframes') or []
        findings = receipt.get('findings') or {}
        items = findings.get('items') if isinstance(findings.get('items'), list) else []
        frame_ids = [str(frame.get('frame_id') or '') for frame in generated if isinstance(frame, dict)]
        expected_ids = set(frame_ids)
        if not expected_ids or len(expected_ids) != len(frame_ids):
            raise ValueError('storyboard keyframe set is empty or contains duplicate frame IDs')
        item_by_id = {str(item.get('frame_id')): item for item in items if isinstance(item, dict)}
        if set(item_by_id) != expected_ids:
            raise ValueError('persisted QC receipt does not cover every storyboard keyframe')
        operation_rows = self.connection.execute(
            "SELECT operation_id, project_id, frame_id, status, provider_operation_id, output_path, output_sha256 "
            "FROM video_factory_keyframe_operation WHERE workflow_id=?", (workflow_id,)
        ).fetchall()
        operation_by_id = {str(row['frame_id']): row for row in operation_rows}
        if set(operation_by_id) != expected_ids:
            raise ValueError('keyframe operation ledger does not match the storyboard frame set')
        frame_sha256s = {}
        for frame in generated:
            frame_id = str(frame['frame_id'])
            item = item_by_id[frame_id]
            operation = operation_by_id[frame_id]
            output_path = Path(str(frame.get('image_path') or ''))
            if (item.get('verdict') != 'pass' or item.get('hard_failure') is True
                    or item.get('image_path') != str(output_path)
                    or not output_path.is_file()):
                raise ValueError(f'keyframe {frame_id} lacks current PASS evidence or its file is missing')
            actual_sha = hashlib.sha256(output_path.read_bytes()).hexdigest()
            expected_sha = str(frame.get('sha256') or '').removeprefix('sha256:').lower()
            if (actual_sha != expected_sha
                    or str(item.get('sha256') or '').removeprefix('sha256:').lower() != actual_sha
                    or operation['status'] != 'completed'
                    or operation['project_id'] != aggregate.get('project_id')
                    or operation['provider_operation_id'] is None
                    or str(operation['output_path']) != str(output_path)
                    or str(operation['output_sha256']).lower() != actual_sha):
                raise ValueError(f'keyframe {frame_id} bytes do not match the QC receipt and operation ledger')
            frame_sha256s[frame_id] = actual_sha

        provider_kind = str((storyboard.get('backend_profile') or {}).get('provider_kind') or '').strip()
        if provider_kind not in {'google_flow', 'vertex_ai'}:
            raise ValueError('storyboard is missing an official pinned render provider')

        scene_table = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_factory_scene_operation'"
        ).fetchone()
        if scene_table and self.connection.execute(
            "SELECT 1 FROM video_factory_scene_operation WHERE project_id=? LIMIT 1",
            (aggregate.get('project_id'),),
        ).fetchone():
            raise ValueError('scene render operations already exist; reconcile them before resuming')
        capacity_table = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_factory_provider_capacity'"
        ).fetchone()
        if capacity_table and self.connection.execute(
            "SELECT 1 FROM video_factory_provider_capacity WHERE operation_id IN (%s) LIMIT 1"
            % ','.join('?' for _ in operation_rows),
            tuple(row['operation_id'] for row in operation_rows),
        ).fetchone():
            raise ValueError('keyframe operation still holds provider capacity; reconcile before resuming')

        old_error = str(outbox['last_error'] or '')
        source = aggregate.get('source_context') or {}
        recovery = {
            'prior_state': aggregate.get('state'),
            'prior_error': old_error,
            'reason': 'Current storyboard QC receipt, persisted report, keyframe SHAs, and operation lineage were verified; resume the existing render without regenerating keyframes.',
            'artifact_digest': storyboard['package_digest'],
            'frame_sha256s': frame_sha256s,
            'production_run_id': aggregate.get('production_run_id'),
            'batch_id': source.get('source_batch_id'),
            'provider_kind': provider_kind,
            'outbox_id': int(outbox['outbox_id']),
            'attempt_count': int(outbox['attempt_count']),
        }
        updated = dict(aggregate)
        updated.setdefault('render_recovery_history', []).append(recovery)
        now = datetime.now(timezone.utc).isoformat()
        event = {
            'event_id': f'evt_verified_storyboard_quarantine_{outbox["outbox_id"]}_{outbox["attempt_count"]}',
            'command_id': f'cmd_verified_storyboard_quarantine_{outbox["outbox_id"]}_{outbox["attempt_count"]}',
            'event_type': 'VerifiedStoryboardQuarantineRecovered',
            'timestamp': now,
            'payload': recovery,
        }
        outbox_repository = OutboxRepository(self.connection)
        with self.connection:
            self.workflow_repo.commit_workflow_in_transaction(
                workflow_id, json.dumps(updated, ensure_ascii=False), [event], expected_version=version
            )
            if not outbox_repository.requeue_quarantined_in_transaction(
                    int(outbox['outbox_id']), preserve_attempts=True):
                raise RuntimeError('quarantined render changed while recovery was being recorded')
        return {'status':'requeued','workflow_id':workflow_id,'artifact_digest':storyboard['package_digest'],
                'frame_sha256s':frame_sha256s}
