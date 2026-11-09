"""Runtime entrypoint for governed Product Video QC."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from video_factory.qc.evaluator import MultimodalEvaluator
from video_factory.qc.image_qc import compute_artifact_digest, run_image_qc
from video_factory.qc.keyframe_qc import run_keyframe_qc
from video_factory.qc.models import QCStage, QCVerdict
from video_factory.qc.normalization import normalize_product_identity
from video_factory.qc.fingerprint import qc_fingerprint
from video_factory.qc.repository import QCReportRepository
from video_factory.qc.video_qc import (
    FOUR_AGENT_VIDEO_TECHNICAL_CONTRACT,
    compute_file_sha256,
    run_video_qc,
)
from video_factory.workflows.budgets import BudgetPolicy, BudgetRepository
from video_factory.workflows.repository import WorkflowRepository


def _configured_qc_evaluator(stage: str = "image") -> Any:
    """Lazy import prevents the evaluator module's package import cycle."""
    from video_factory.integrations.nine_router import configured_qc_evaluator
    return configured_qc_evaluator(stage=stage)


def _recorded_route_cost(items: list[dict[str, Any]]) -> tuple[float | None, str]:
    """Sum only actual costs explicitly returned in provider receipts."""
    costs: list[float] = []
    seen: set[str] = set()
    for item in items:
        findings = item.get("findings") if isinstance(item, Mapping) else None
        if not isinstance(findings, Mapping):
            continue
        receipt = findings.get("route_receipt")
        if not isinstance(receipt, Mapping) and isinstance(findings.get("findings"), Mapping):
            receipt = findings["findings"].get("route_receipt")
        if not isinstance(receipt, Mapping) or receipt.get("cost_status") != "recorded":
            continue
        try:
            cost = float(receipt["cost_usd"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(cost) or cost < 0:
            continue
        receipt_id = str(receipt.get("attempt_id") or receipt.get("router_usage_id") or item.get("asset_id") or "")
        if receipt_id in seen:
            continue
        seen.add(receipt_id)
        costs.append(cost)
    return (sum(costs), "recorded") if costs else (None, "unknown")


def _run_configured_preflight(evaluator: Any) -> str | None:
    """Call an implemented evaluator preflight and ignore dynamic mock attrs."""
    preflight = getattr(type(evaluator), "configured_preflight", None)
    if not callable(preflight):
        return None
    try:
        return preflight(evaluator)
    except Exception as exc:  # Preflight is read-only and runs before claim/send.
        from video_factory.integrations.nine_router import NineRouterConfigurationError

        if isinstance(exc, NineRouterConfigurationError):
            return f"Configured QC preflight failed before provider request: {exc}"
        return f"Configured QC preflight failed before provider request ({type(exc).__name__})"


def _file_url_to_path(value: str | Path) -> Path:
    from video_factory.storage.layout import get_workspace_layout
    ws = get_workspace_layout()
    return ws.resolve_path(value)


def _reference_url_to_path(value: str | Path) -> Path:
    """Resolve approved external source images without widening video output paths."""
    from video_factory.storage.layout import (
        get_trusted_exchange_roots,
        get_workspace_layout,
    )

    return get_workspace_layout().resolve_path(
        value, allowed_roots=get_trusted_exchange_roots(),
    )


def _payload_dict(artifact: dict[str, Any] | None) -> dict[str, Any]:
    if not artifact:
        return {}
    payload = artifact.get("payload")
    if isinstance(payload, str):
        try:
            decoded = json.loads(payload)
            return decoded if isinstance(decoded, dict) else {}
        except (TypeError, ValueError):
            return {}
    return payload if isinstance(payload, dict) else {}


def _image_qc_context_hash(product: Any, evaluator: Any) -> str:
    from video_factory.qc.fingerprint import IMAGE_POLICY_VERSION, IMAGE_RUBRIC_VERSION
    from video_factory.qc.rubric import IMAGE_QC_SYSTEM_PROMPT, build_image_qc_prompt

    context = {
        "prompt": build_image_qc_prompt(
            brand=product.brand, model=product.model, variant=product.variant,
            display_name=product.display_name, key_features=product.verified_claims,
            identity_mode=str(product.source_context.get("identity_mode") or "sku"),
        ),
        "system_instruction": IMAGE_QC_SYSTEM_PROMPT,
        "evaluator_name": str(getattr(evaluator, "evaluator_name", type(evaluator).__name__)),
        "evaluator_version": str(getattr(evaluator, "evaluator_version", "1.0")),
        "model_identifier": str(getattr(evaluator, "model", "")),
        "rubric_version": IMAGE_RUBRIC_VERSION,
        "policy_version": IMAGE_POLICY_VERSION,
    }
    return hashlib.sha256(
        json.dumps(context, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _initial_image_attempt_hash(assets: list[list[str]], context_hash: str) -> str:
    return hashlib.sha256(
        json.dumps({"assets": assets, "context_hash": context_hash}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _candidate_image_attempt_hash(image_sha256: str, context_hash: str) -> str:
    return hashlib.sha256(f"{image_sha256}:{context_hash}".encode("ascii")).hexdigest()


def _exchange_image_pack_gate(
    source_context: dict[str, Any] | None,
    resource_pack: dict[str, Any],
) -> tuple[dict[str, Any], str | None, str | None]:
    """Gate Exchange-backed packs by current image bytes immediately before QC."""
    source = source_context or {}
    if source.get("source") != "hermes_product_video_exchange":
        return resource_pack, None, None

    images = [item for item in source.get("images", []) if isinstance(item, Mapping)]
    review = source.get("input_review")
    selected_ids = {
        str(asset_id).strip()
        for asset_id in (review.get("confirmed_image_asset_ids") or [])
    } if isinstance(review, Mapping) else set()
    auto_admitted = bool(source.get("auto_admitted"))
    if source.get('identity_mode') == 'source_product':
        from video_factory.creative.contracts import canonical_digest
        snapshot = source.get('source_snapshot') or {}
        if (not source.get('canonical_product_key')
                or snapshot.get('canonical_product_id') != source['canonical_product_key']
                or source.get('source_snapshot_digest') != canonical_digest(snapshot)):
            return resource_pack, "EXCHANGE_IMAGE_ADMISSION_BLOCKED", "Source product snapshot/identity binding is missing or changed"
    if len(images) < 1 or (not auto_admitted and len(selected_ids) < 1):
        return resource_pack, "EXCHANGE_IMAGE_ADMISSION_BLOCKED", "At least one Exchange image must be selected"
    if selected_ids and not selected_ids.issubset({str(item.get("asset_id") or "").strip() for item in images}):
        return resource_pack, "EXCHANGE_IMAGE_ADMISSION_BLOCKED", "A selected Exchange asset is missing from source_context"

    source_hash_to_id: dict[str, str] = {}
    source_ids: set[str] = set()
    from video_factory.qc.normalization import _is_decodable_image

    for image in images:
        asset_id = str(image.get("asset_id") or "").strip()
        identity_reason = str(image.get("identity_review_reason") or "")
        if not asset_id or asset_id in source_ids:
            return resource_pack, "EXCHANGE_IMAGE_ADMISSION_BLOCKED", "Exchange images contain a missing or duplicate asset ID"
        source_ids.add(asset_id)
        if "DUPLICATE_FILE_REFERENCE" in identity_reason:
            continue
        if selected_ids and asset_id not in selected_ids:
            continue
        location = image.get("absolute_path") or image.get("local_path") or image.get("path")
        try:
            path = Path(str(location)).expanduser().resolve(strict=True)
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else ""
        except (OSError, RuntimeError):
            digest = ""
            path = None
        declared = str(image.get("sha256") or "").removeprefix("sha256:").lower()
        if not digest or (declared and declared != digest) or path is None:
            continue
        if not _is_decodable_image(path):
            continue
        if digest in source_hash_to_id:
            continue
        source_hash_to_id[digest] = asset_id

    if len(source_hash_to_id) < 1:
        return resource_pack, "EXCHANGE_IMAGE_ADMISSION_BLOCKED", "Exchange admission has no usable image bytes"

    from video_factory.qc.normalization import _media_manifest
    pack_manifest = _media_manifest(dict(resource_pack or {}))
    matched: list[dict[str, Any]] = []
    matched_hashes: set[str] = set()
    for item in pack_manifest:
        digest = str(item.get("sha256") or "").lower()
        if not item.get("eligible") or digest not in source_hash_to_id or digest in matched_hashes:
            continue
        matched.append({**item, "asset_id": source_hash_to_id[digest]})
        matched_hashes.add(digest)
    if len(matched) < 1:
        return resource_pack, "APPROVED_IMAGE_MISMATCH", (
            "Resource pack does not contain a byte-identical usable Exchange image "
            f"({len(matched)}/1 SHA-256 matches)"
        )

    filtered = dict(resource_pack)
    filtered["assets"] = matched
    filtered["product_references"] = matched
    filtered["media_assets"] = matched
    if "raw_result" in filtered and isinstance(filtered["raw_result"], dict):
        filtered_raw = dict(filtered["raw_result"])
        filtered_raw["assets"] = matched
        filtered_raw["product_references"] = matched
        filtered_raw["media_assets"] = matched
        filtered["raw_result"] = filtered_raw
    return filtered, None, None


def _persist_exchange_image_pack(
    workflow_repository: WorkflowRepository,
    workflow_id: str,
    resource_pack: dict[str, Any],
    report_id: str,
) -> None:
    current = workflow_repository.get_workflow(workflow_id)
    if current is None:
        return
    aggregate, version = current
    next_aggregate = dict(aggregate)
    next_aggregate["resource_pack"] = resource_pack
    next_aggregate["latest_artifact"] = resource_pack
    workflow_repository.commit_workflow(
        workflow_id,
        json.dumps(next_aggregate, ensure_ascii=False),
        [{
            "event_id": f"evt_exchange_image_pack_qc_{report_id}",
            "command_id": f"cmd_exchange_image_pack_qc_{report_id}",
            "event_type": "ExchangeImagePackAdmittedByQC",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": {"report_id": report_id},
        }],
        expected_version=version,
    )


class ProductVideoQCService:
    """Thin runtime adapter shared by operator CLI and future worker hooks."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        evaluator: Any | None = None,
        *,
        budget_policy: BudgetPolicy | None = None,
    ) -> None:
        self.connection = connection
        self.repository = QCReportRepository(connection)
        self.workflow_repository = WorkflowRepository(connection)
        self.evaluator = evaluator
        self.budget_repository = BudgetRepository(connection)
        self.budget_policy = budget_policy or BudgetPolicy(
            max_total=100_000,
            per_channel={"video_factory": 100_000},
            policy_id="product-video-qc",
            policy_version=1,
        )

    def reconcile_presend_reservation(self, workflow_id: str, reservation_id: str) -> dict[str, str]:
        """Release a quota-rejected reservation only when the durable record proves no send."""
        from video_factory.qc.retry_policy import ensure_qc_retry_schema

        ensure_qc_retry_schema(self.connection)
        event_id = f"evt_qc_presend_release_{hashlib.sha256(reservation_id.encode()).hexdigest()[:24]}"
        now = datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                """SELECT state, attempt_id, capability_id, created_at
                   FROM budget_reservation WHERE reservation_id=? AND scope_id=?""",
                (reservation_id, workflow_id),
            ).fetchone()
            if row is None or row["capability_id"] != "product_video.qc.image":
                raise ValueError("reservation is missing or is not this workflow's Image QC reservation")
            if row["state"] == "released":
                audited = self.connection.execute(
                    "SELECT 1 FROM workflow_event WHERE event_id=? AND workflow_id=?",
                    (event_id, workflow_id),
                ).fetchone()
                if audited:
                    return {"workflow_id": workflow_id, "reservation_id": reservation_id, "reservation_status": "released"}
            if row["state"] != "reserved":
                raise ValueError("reservation is not awaiting pre-send reconciliation")
            attempt_id = str(row["attempt_id"] or "")
            prefix = f"img_claim_{workflow_id}_v"
            if not attempt_id.startswith(prefix):
                raise ValueError("reservation does not have an initial Image QC claim ID")
            operation_id = attempt_id.replace("img_claim_", "op_init_img_", 1)
            if self.connection.execute(
                """SELECT 1 FROM workflow_qc_retries
                   WHERE workflow_id=? AND (operation_id=? OR budget_reference=?) LIMIT 1""",
                (workflow_id, operation_id, reservation_id),
            ).fetchone() or self.connection.execute(
                """SELECT 1 FROM product_video_operation_attempt
                   WHERE workflow_id=? AND (reservation_id=? OR attempt_id=?) LIMIT 1""",
                (workflow_id, reservation_id, attempt_id),
            ).fetchone():
                raise ValueError("an attempt or provider receipt exists; use normal QC reconciliation")

            try:
                reserved_at = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("reservation creation time is invalid") from exc
            proven_quota_event = False
            for event in self.connection.execute(
                """SELECT timestamp, payload_json FROM workflow_event
                   WHERE workflow_id=? AND event_type='WorkflowWaitingForOperator'""",
                (workflow_id,),
            ):
                try:
                    payload = json.loads(event["payload_json"])
                    happened_at = datetime.fromisoformat(str(event["timestamp"]).replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                if (
                    isinstance(payload, dict)
                    and payload.get("code") == "QC_QUOTA_EXHAUSTED"
                    and reserved_at.tzinfo is not None
                    and happened_at.tzinfo is not None
                    and abs(happened_at - reserved_at) <= timedelta(seconds=2)
                ):
                    proven_quota_event = True
                    break
            if not proven_quota_event:
                raise ValueError("matching QC_QUOTA_EXHAUSTED quota event is missing")

            current = self.workflow_repository.get_workflow(workflow_id)
            if current is None:
                raise ValueError("workflow is unavailable for reconciliation audit")
            aggregate, version = current
            history = list(aggregate.get("qc_presend_reconciliations") or [])
            history.append({
                "reservation_id": reservation_id,
                "attempt_id": attempt_id,
                "outcome": "not_sent",
                "reason": "quota rejected before initial QC attempt claim",
                "reconciled_at": now,
            })
            aggregate["qc_presend_reconciliations"] = history
            self.connection.execute(
                """UPDATE budget_reservation SET state='released', updated_at=?
                   WHERE reservation_id=? AND state='reserved'""",
                (now, reservation_id),
            )
            self.workflow_repository.commit_workflow(
                workflow_id, json.dumps(aggregate, ensure_ascii=False), [
                    {
                        "event_id": event_id,
                        "command_id": f"cmd_qc_presend_release_{reservation_id}",
                        "event_type": "QCPreSendReservationReleased",
                        "timestamp": now,
                        "payload": {"reservation_id": reservation_id, "attempt_id": attempt_id, "outcome": "not_sent"},
                    },
                ], expected_version=version, in_transaction=True,
            )
        return {"workflow_id": workflow_id, "reservation_id": reservation_id, "reservation_status": "released"}

    def reconcile_uncertain_attempt(
        self,
        workflow_id: str,
        attempt_id: str,
        *,
        outcome: str,
        evidence_reference: str,
        note: str,
    ) -> dict[str, Any]:
        """Record an operator's provider reconciliation before any QC retry."""
        if outcome not in {"consumed", "not_sent"}:
            raise ValueError("outcome must be 'consumed' or 'not_sent'")
        evidence_reference = evidence_reference.strip()
        note = note.strip()
        if not evidence_reference or len(evidence_reference) > 500:
            raise ValueError("a provider-ledger evidence reference (1-500 characters) is required")
        if not note or len(note) > 1000:
            raise ValueError("an operator reconciliation note (1-1000 characters) is required")

        attempt = self.connection.execute(
            """SELECT reservation_id, status FROM product_video_operation_attempt
               WHERE workflow_id = ? AND attempt_id = ?""",
            (workflow_id, attempt_id),
        ).fetchone()
        retry_rows = False
        if attempt is None:
            # The worker's image selector persists durable attempts in this
            # table; reconcile those through the same operator command.
            attempt = self.connection.execute(
                """SELECT budget_reference AS reservation_id, status
                   FROM workflow_qc_retries
                   WHERE workflow_id = ? AND operation_id = ?
                   ORDER BY attempt_number DESC LIMIT 1""",
                (workflow_id, attempt_id),
            ).fetchone()
            retry_rows = attempt is not None
        if attempt is None or str(attempt["status"]) != "technical_unknown":
            raise ValueError("attempt is missing or is not awaiting provider reconciliation")
        reservation_id = str(attempt["reservation_id"] or "")

        now = datetime.now(timezone.utc).isoformat()
        outcome_status = "reconciled_consumed" if outcome == "consumed" else "reconciled_not_sent"
        event_key = hashlib.sha256(
            f"{workflow_id}:{attempt_id}:{outcome}".encode("utf-8")
        ).hexdigest()[:24]
        aggregate_data = self.workflow_repository.get_workflow(workflow_id)
        if aggregate_data is None:
            raise ValueError("workflow is unavailable for reconciliation audit")
        aggregate, version = aggregate_data
        reconciliations = list(aggregate.get("qc_reconciliations") or [])
        reconciliations.append(
            {
                "attempt_id": attempt_id,
                "outcome": outcome,
                "evidence_reference": evidence_reference,
                "note": note,
                "reconciled_at": now,
            }
        )
        aggregate["qc_reconciliations"] = reconciliations[-20:]

        with self.connection:
            reservation_state = "none"
            if reservation_id:
                reservation = self.connection.execute(
                    "SELECT state FROM budget_reservation WHERE reservation_id = ?",
                    (reservation_id,),
                ).fetchone()
                if reservation is None or str(reservation["state"]) != "reserved":
                    raise ValueError("uncertain attempt reservation is missing or no longer reserved")
                reservation_state = "committed" if outcome == "consumed" else "released"
                self.connection.execute(
                    "UPDATE budget_reservation SET state = ?, updated_at = ? WHERE reservation_id = ?",
                    (reservation_state, now, reservation_id),
                )
            if retry_rows:
                if reservation_id:
                    self.connection.execute(
                        """UPDATE workflow_qc_retries SET status = ?, updated_at = ?
                           WHERE workflow_id = ? AND budget_reference = ? AND status = 'technical_unknown'""",
                        (outcome_status, now, workflow_id, reservation_id),
                    )
                else:
                    self.connection.execute(
                        """UPDATE workflow_qc_retries SET status = ?, updated_at = ?
                           WHERE workflow_id = ? AND operation_id = ? AND status = 'technical_unknown'""",
                        (outcome_status, now, workflow_id, attempt_id),
                    )
            else:
                self.connection.execute(
                    "UPDATE product_video_operation_attempt SET status = ?, updated_at = ? WHERE workflow_id = ? AND attempt_id = ?",
                    (outcome_status, now, workflow_id, attempt_id),
                )
            self.workflow_repository.commit_workflow(
                workflow_id,
                json.dumps(aggregate, ensure_ascii=False),
                [
                    {
                        "event_id": f"evt_qc_reconcile_{event_key}",
                        "command_id": f"cmd_qc_reconcile_{event_key}",
                        "event_type": "QCOutcomeReconciled",
                        "timestamp": now,
                        "payload": {
                            "attempt_id": attempt_id,
                            "outcome": outcome,
                            "evidence_reference": evidence_reference,
                            "note": note,
                        },
                    }
                ],
                expected_version=version,
                in_transaction=True,
            )
        return {
            "workflow_id": workflow_id,
            "attempt_id": attempt_id,
            "outcome": outcome,
            "attempt_status": outcome_status,
            "reservation_status": reservation_state,
        }

    def run(
        self,
        workflow_id: str,
        stage: QCStage,
        *,
        provider_mode: str = "configured",
        authorize_cost: bool = False,
        max_cost_usd: float | None = None,
        qc_budget_mode: str | None = None,
        authorization: dict[str, Any] | None = None,
        attempt_id: str | None = None,
        artifact_override: dict[str, Any] | None = None,
        storyboard_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if provider_mode not in ("configured", "fake"):
            raise ValueError("provider_mode must be 'configured' or 'fake'")
        workflow = self.workflow_repository.get_workflow(workflow_id)
        if workflow is None:
            return {"status": "failed", "stage": stage.value, "reason": f"Workflow '{workflow_id}' not found"}
        aggregate, _version = workflow
        source_context = aggregate.get("source_context") or {}

        owner_user_id = str(aggregate.get("owner_user_id") or source_context.get("owner_user_id") or "").strip()
        project_id = str(aggregate.get("project_id") or source_context.get("project_id") or "").strip()
        if not owner_user_id or not project_id:
            return {
                "status": "failed",
                "stage": stage.value,
                "reason": "Workflow aggregate is missing required owner_user_id or project_id",
            }

        # This ID namespaces durable QC attempts only; it is not a cost grant.
        auth_terms = {
            "authorization_id": f"qc-unbudgeted:{workflow_id}",
            "campaign_id": "qc-unbudgeted",
        }
        unlimited_qc = True

        capability_id = f"product_video.qc.{stage.value}"
        if stage == QCStage.IMAGE:
            from video_factory.qc.retry_policy import ensure_qc_retry_schema

            ensure_qc_retry_schema(self.connection)
            unresolved_image = self.connection.execute(
                """SELECT operation_id FROM workflow_qc_retries
                   WHERE workflow_id = ? AND retry_type = 'source_image_qc'
                     AND status = 'technical_unknown'
                   ORDER BY id DESC LIMIT 1""",
                (workflow_id,),
            ).fetchone()
            if unresolved_image:
                return {
                    "status": "pending", "stage": stage.value,
                    "code": "QC_OUTCOME_UNCERTAIN",
                    "reason": (
                        "Image selector attempt "
                        f"{unresolved_image['operation_id']} requires reconciliation before Image QC"
                    ),
                }

        def _backfill_legacy_attempts() -> None:
            if provider_mode != "configured" or not auth_terms:
                return
            authorization_id = auth_terms["authorization_id"]
            campaign_id = auth_terms["campaign_id"]
            # Existing reservations predate grant/attempt fields. Preserve and
            # attribute them; the live unknown report is therefore attempt 1.
            legacy_rows = self.connection.execute(
                """
                SELECT reservation_id, capability_id, operation_input_digest, amount, state, created_at
                FROM budget_reservation
                WHERE scope_id = ? AND external_reference = ?
                  AND (authorization_id = '' OR campaign_id = '' OR attempt_id = '')
                ORDER BY created_at, reservation_id
                """,
                (workflow_id, authorization_id),
            ).fetchall()
            for row in legacy_rows:
                legacy_capability = str(row["capability_id"])
                count = self.connection.execute(
                    """SELECT COUNT(*) FROM product_video_operation_attempt
                       WHERE authorization_id = ? AND workflow_id = ? AND capability_id = ?""",
                    (authorization_id, workflow_id, legacy_capability),
                ).fetchone()[0]
                legacy_attempt_id = f"legacy:{row['reservation_id']}"
                self.connection.execute(
                    """UPDATE budget_reservation
                       SET campaign_id = ?, authorization_id = ?, attempt_id = ?
                       WHERE reservation_id = ?""",
                    (campaign_id, authorization_id, legacy_attempt_id, row["reservation_id"]),
                )
                self.connection.execute(
                    """INSERT OR IGNORE INTO product_video_operation_attempt (
                         attempt_id, authorization_id, campaign_id, owner_user_id, workflow_id,
                         capability_id, attempt_number, input_fingerprint, estimated_cost,
                         reservation_id, status, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        legacy_attempt_id, authorization_id, campaign_id,
                        str(owner_user_id), workflow_id,
                        legacy_capability, count + 1, str(row["operation_input_digest"]),
                        int(row["amount"]), row["reservation_id"],
                        "technical_unknown" if legacy_capability == capability_id else str(row["state"]),
                        str(row["created_at"]), str(row["created_at"]),
                    ),
                )

        _backfill_legacy_attempts()

        def _next_attempt_id() -> tuple[str | None, int | None, str | None]:
            if provider_mode != "configured":
                return "", 0, None
            authorization_id = auth_terms["authorization_id"]
            rows = self.connection.execute(
                """SELECT attempt_number, status FROM product_video_operation_attempt
                   WHERE workflow_id = ? AND capability_id = ?
                   ORDER BY attempt_number DESC""",
                (workflow_id, capability_id),
            ).fetchall()
            prior = self.connection.execute(
                """SELECT attempt_id, attempt_number, status, input_fingerprint
                   FROM product_video_operation_attempt
                   WHERE workflow_id = ? AND capability_id = ?
                   ORDER BY attempt_number DESC LIMIT 1""",
                (workflow_id, capability_id),
            ).fetchone()
            if prior and str(prior["status"]) in {"reserved", "claimed", "sent", "technical_unknown"}:
                return None, None, f"QC_OUTCOME_UNCERTAIN: attempt {prior['attempt_id']} requires reconciliation before another QC request"
            if prior and str(prior["status"]) == "pass":
                latest = self.repository.get_latest_report(workflow_id, stage)
                artifact = artifact_override or (
                    aggregate.get("resource_pack") if stage == QCStage.IMAGE
                    else aggregate.get("storyboard") if stage == QCStage.KEYFRAME
                    else aggregate.get("render_result")
                ) or {}
                current_digest = str(
                    artifact.get("sha256") if stage == QCStage.VIDEO
                    else artifact.get("package_digest") if stage == QCStage.KEYFRAME
                    else compute_artifact_digest(artifact)
                ).removeprefix("sha256:")
                if latest is not None and latest.verdict == QCVerdict.PASS and latest.artifact_digest.removeprefix("sha256:") == current_digest:
                    return str(prior["attempt_id"]), int(prior["attempt_number"]), None
            from video_factory.qc.retry_policy import clamp_max_retries
            max_attempts = 1 + clamp_max_retries(aggregate.get("max_qc_retries"))
            if (
                stage == QCStage.KEYFRAME
                and aggregate.get("keyframe_policy_recovery_pending")
            ):
                # This increment is allowed only while a reviewed operator
                # recovery is actively executing; it does not reset prior rows.
                max_attempts += min(
                    2,
                    max(0, int(aggregate.get("keyframe_policy_recovery_count", 0) or 0)),
                )
            if len(rows) >= max_attempts:
                return None, None, f"QC_RETRY_EXHAUSTED: {len(rows)}/{max_attempts} attempts used; inspect the latest report and create a corrected artifact before any further evaluation"
            if attempt_id and any(str(row["attempt_id"]) == attempt_id for row in rows):
                return None, None, "QC attempt ID already exists; use the stored report"
            if prior and str(prior["status"]) == "semantic_fail":
                latest = self.repository.get_latest_report(workflow_id, stage)
                artifact = artifact_override or (
                    aggregate.get("resource_pack") if stage == QCStage.IMAGE
                    else aggregate.get("storyboard") if stage == QCStage.KEYFRAME
                    else aggregate.get("render_result")
                ) or {}
                current_digest = str(
                    artifact.get("sha256") if stage == QCStage.VIDEO
                    else artifact.get("package_digest") if stage == QCStage.KEYFRAME
                    else compute_artifact_digest(artifact)
                ).removeprefix("sha256:")
                if latest is None or latest.verdict != QCVerdict.FAIL:
                    return None, None, "Previous QC failure report is missing; operator review is required"
                if not current_digest or latest.artifact_digest.removeprefix("sha256:") == current_digest:
                    return None, None, "QC retry requires a new artifact with a different SHA-256 digest"
                if stage == QCStage.KEYFRAME:
                    old_frames = latest.findings.get("frame_sha256s") if isinstance(latest.findings, dict) else None
                    new_frames = {
                        str(frame.get("frame_id")): str(frame.get("sha256") or "").removeprefix("sha256:")
                        for frame in artifact.get("generated_keyframes", [])
                        if isinstance(frame, dict) and frame.get("frame_id")
                    }
                    if not isinstance(old_frames, dict) or not old_frames or not new_frames or new_frames == old_frames:
                        return None, None, "Keyframe QC retry requires new frame bytes; previous hashes are missing or unchanged"
            number = len(rows) + 1
            return attempt_id or f"qc:{workflow_id}:{stage.value}:{number}", number, None

        effective_attempt_id, attempt_number, attempt_error = _next_attempt_id()
        if attempt_error:
            code = (
                "QC_OUTCOME_UNCERTAIN"
                if attempt_error.startswith("QC_OUTCOME_UNCERTAIN:")
                else "QC_RETRY_EXHAUSTED"
            )
            reason = attempt_error.partition(":")[2].strip() if ":" in attempt_error else attempt_error
            return {"status": "pending", "stage": stage.value, "code": code, "reason": reason}

        def _admit(fingerprint: str) -> tuple[Any | None, dict[str, Any] | None]:
            completed = self.repository.claim(workflow_id, stage, fingerprint)
            if completed is not None:
                return None, {"status": "completed", "stage": stage.value, "report_id": completed.report_id, "report": completed}
            if provider_mode != "configured":
                return None, None
            now = datetime.now(timezone.utc).isoformat()
            self.connection.execute(
                """INSERT OR IGNORE INTO product_video_operation_attempt (
                     attempt_id, authorization_id, campaign_id, owner_user_id, workflow_id,
                     capability_id, attempt_number, input_fingerprint, estimated_cost,
                     reservation_id, status, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 'claimed', ?, ?)""",
                (
                    str(effective_attempt_id), auth_terms["authorization_id"], auth_terms["campaign_id"],
                    str(aggregate.get("owner_user_id") or "default-owner"), workflow_id, capability_id,
                    int(attempt_number or 0), fingerprint, now, now,
                ),
            )
            return None, None

        def _finish(fingerprint: str, report: Any, allocation: Any | None) -> dict[str, Any]:
            self.repository.complete_claim(workflow_id, stage, fingerprint, report.report_id)
            observed_costs: list[float] = []
            receipt_ids: set[str] = set()
            item_findings = report.findings.get("items", []) if isinstance(report.findings, dict) else []
            route_receipts: list[Mapping[str, Any]] = []

            def collect_receipts(value: Any) -> None:
                if isinstance(value, Mapping):
                    receipt = value.get("route_receipt")
                    if isinstance(receipt, Mapping):
                        route_receipts.append(receipt)
                    for child in value.values():
                        collect_receipts(child)
                elif isinstance(value, list):
                    for child in value:
                        collect_receipts(child)

            collect_receipts(report.findings)
            for receipt in route_receipts:
                if receipt.get("cost_status") not in {"recorded", "charged"}:
                    continue
                try:
                    cost = float(receipt["cost_usd"])
                except (KeyError, TypeError, ValueError):
                    continue
                if math.isfinite(cost) and cost >= 0:
                    receipt_id = str(receipt.get("attempt_id") or receipt.get("router_usage_id") or "")
                    if not receipt_id or receipt_id not in receipt_ids:
                        observed_costs.append(cost)
                        if receipt_id:
                            receipt_ids.add(receipt_id)
            cost_status = "recorded" if observed_costs else "unknown"
            cost_usd = sum(observed_costs) if observed_costs else None
            report.cost_usd = cost_usd
            report.findings["cost_status"] = cost_status
            report.findings["cost_usd"] = cost_usd
            actual_receipt = route_receipts[0] if route_receipts else {}
            report.findings["actual_provider"] = str(
                actual_receipt.get("upstream_provider") or report.evaluator_name or "unknown"
            )
            report.findings["actual_model"] = str(
                actual_receipt.get("upstream_model") or report.model_identifier or "unknown"
            )
            outcome = "pass" if getattr(report.verdict, "value", report.verdict) == "pass" else "semantic_fail"
            if (
                stage == QCStage.KEYFRAME
                and report.verdict == QCVerdict.FAIL
                and isinstance(report.findings, dict)
                and report.findings.get("evaluator_calls") == 0
            ):
                outcome = "pre_send_fail"
            if (
                stage == QCStage.KEYFRAME
                and report.verdict == QCVerdict.UNKNOWN
                and isinstance(report.findings, dict)
                and report.findings.get("evaluator_calls") == 0
            ):
                # Missing approved script context is an input gate failure, not
                # a paid/uncertain provider result.
                outcome = "pre_send_fail"
            if stage == QCStage.VIDEO and isinstance(report.findings, dict):
                scenes = report.findings.get("visual_scenes", [])
                if not isinstance(scenes, list):
                    scenes = []
                technical_video = any(
                    isinstance(scene, dict)
                    and (
                        (isinstance(scene.get("diagnostics"), dict) and bool(scene["diagnostics"].get("technical_unknown")))
                        or bool(scene.get("diagnostics", {}).get("parse_error"))
                        or bool(scene.get("findings", {}).get("parse_error"))
                        or (str(scene.get("verdict", "")).lower() == "unknown" and bool(scene.get("error")))
                    )
                    for scene in scenes
                )
                if technical_video or getattr(report.verdict, "value", report.verdict) == "unknown":
                    outcome = "technical_unknown"
                    report.verdict = QCVerdict.UNKNOWN
            if stage == QCStage.KEYFRAME and outcome == "semantic_fail":
                package = artifact_override or aggregate.get("storyboard") or {}
                frames = package.get("generated_keyframes", []) if isinstance(package, dict) else []
                provider_incomplete = any(
                    not isinstance(item, dict)
                    or str(item.get("status", "")).lower() != "completed"
                    or not item.get("image_path")
                    for item in frames
                )
                if provider_incomplete:
                    outcome = "technical_unknown"
            schema_invalid = False
            response_status = str(getattr(report.verdict, "value", report.verdict)).lower()
            def find_schema_invalid(value: Any) -> None:
                nonlocal schema_invalid
                if isinstance(value, Mapping):
                    if value.get("schema_invalid") is True:
                        schema_invalid = True
                    for child in value.values():
                        find_schema_invalid(child)
                elif isinstance(value, list):
                    for child in value:
                        find_schema_invalid(child)
            find_schema_invalid(report.findings)
            if response_status == "unknown" and schema_invalid:
                outcome = "response_invalid"
            if getattr(report.verdict, "value", report.verdict) == "unknown" and outcome != "technical_unknown":
                items = report.findings.get("items", []) if isinstance(report.findings, dict) else []
                technical = any(
                    bool(item.get("findings", {}).get("technical_unknown"))
                    or bool(item.get("findings", {}).get("parse_error"))
                    or (
                        str(item.get("verdict", "")).lower() == "unknown"
                        and str(item.get("reason", "")).startswith("Evaluation call failed:")
                    )
                    for item in items if isinstance(item, dict)
                )
                if outcome not in {"response_invalid", "pre_send_fail"}:
                    outcome = "technical_unknown" if technical else "semantic_unknown"
            if outcome in {"pass", "semantic_fail", "semantic_unknown", "response_invalid"}:
                report.findings["usage_status"] = "consumed"
                report.findings["reconcile_required"] = False
            elif outcome in {"technical_unknown", "unknown"}:
                outcome = "technical_unknown"
                report.findings["usage_status"] = "uncertain"
                report.findings["reconcile_required"] = True
                report.findings["retry_allowed"] = False
            elif outcome == "pre_send_fail":
                cost_status = "not_sent"
                cost_usd = None
                report.findings["cost_status"] = cost_status
                report.findings["cost_usd"] = None
                report.findings["usage_status"] = "not_sent"
                report.findings["reconcile_required"] = False
            self.repository.save_report(report)
            if provider_mode == "configured":
                with self.connection:
                    self.connection.execute(
                        """UPDATE product_video_operation_attempt
                           SET status = ?, actual_cost = NULL, actual_cost_usd = ?, cost_status = ?,
                               actual_provider = ?, actual_model = ?, provider_operation_id = ?, updated_at = ? WHERE attempt_id = ?""",
                        (
                            outcome, cost_usd, cost_status,
                            report.findings["actual_provider"], report.findings["actual_model"],
                            str(actual_receipt.get("attempt_id") or actual_receipt.get("router_usage_id") or "") or None,
                            datetime.now(timezone.utc).isoformat(), str(effective_attempt_id),
                        ),
                    )
            result = {
                "status": "pending" if outcome in {"technical_unknown", "response_invalid"} else "completed",
                "stage": stage.value,
                "report_id": report.report_id,
                "report": report,
            }
            if outcome == "technical_unknown":
                result.update({
                    "code": "QC_OUTCOME_UNCERTAIN",
                    "reason": "QC request outcome is uncertain; reconcile this operation before retrying",
                })
            elif outcome == "response_invalid":
                result.update({
                    "code": "QC_RESPONSE_INVALID",
                    "reason": "Provider response was received and counted; retry the same artifact only within the durable QC retry limit",
                    "retry_allowed": True,
                })
            elif outcome == "pre_send_fail" and response_status == "unknown":
                result.update({
                    "status": "pending",
                    "code": "QC_INPUT_UNVERIFIABLE",
                    "reason": "Approved script scene is unavailable for this keyframe; attach the matching script scene before QC.",
                })
            return result

        if stage == QCStage.IMAGE:
            artifact = artifact_override or aggregate.get("resource_pack") or {}
            if not artifact:
                return {"status": "unknown", "stage": stage.value, "reason": "Durable resource_pack artifact is missing"}
            artifact, gate_code, gate_reason = _exchange_image_pack_gate(source_context, artifact)
            if gate_code:
                return {
                    "status": "failed",
                    "stage": stage.value,
                    "code": gate_code,
                    "reason": gate_reason,
                    "resource_pack": artifact,
                }
            product = normalize_product_identity(artifact, source_context=source_context)
            evaluator = self.evaluator or (
                MultimodalEvaluator(fake_mode=True)
                if provider_mode == "fake"
                else _configured_qc_evaluator(stage.value)
            )
            provider_error = _run_configured_preflight(evaluator) if provider_mode == "configured" else None
            if provider_error:
                if provider_error:
                    return {"status": "pending", "stage": stage.value, "code": "QC_PROVIDER_UNAVAILABLE", "reason": provider_error}
            artifact_digest = compute_artifact_digest(artifact)
            fingerprint = qc_fingerprint(
                workflow_id,
                stage,
                artifact_digest,
                product,
                evaluator_name=str(getattr(evaluator, "evaluator_name", "unknown")),
                evaluator_version=str(getattr(evaluator, "evaluator_version", "unknown")),
                model_identifier=str(getattr(evaluator, "model", "")),
                attempt_id=str(effective_attempt_id),
            )
            allocation, completed = _admit(fingerprint)
            if completed:
                if (
                    source_context.get("source") == "hermes_product_video_exchange"
                    and getattr(getattr(completed.get("report"), "verdict", None), "value", None) == "pass"
                ):
                    completed["resource_pack"] = artifact
                    _persist_exchange_image_pack(
                        self.workflow_repository,
                        workflow_id,
                        artifact,
                        str(completed.get("report_id") or "reused-image-qc"),
                    )
                return completed
            report, eligible = run_image_qc(
                workflow_id,
                artifact,
                product,
                evaluator=evaluator,
                repo=self.repository,
                attempt_id=str(effective_attempt_id),
                owner_user_id=owner_user_id,
                project_id=project_id,
            )
            result = _finish(fingerprint, report, allocation)
            result["eligible_count"] = len(eligible)
            if source_context.get("source") == "hermes_product_video_exchange":
                result["resource_pack"] = artifact
                if (
                    result.get("status") == "completed"
                    and getattr(getattr(report, "verdict", None), "value", None) == "pass"
                ):
                    rep_digests = [str(d).removeprefix("sha256:").lower() for d in (report.reference_digests or [])]
                    if rep_digests and "assets" in artifact:
                        matching_assets = [a for a in artifact["assets"] if (a.get("sha256") or a.get("digest") or "").lower() in rep_digests]
                        other_assets = [a for a in artifact["assets"] if (a.get("sha256") or a.get("digest") or "").lower() not in rep_digests]
                        ordered_assets = matching_assets + other_assets
                        artifact["assets"] = ordered_assets
                        artifact["product_references"] = ordered_assets
                        artifact["media_assets"] = ordered_assets
                        if "raw_result" in artifact and isinstance(artifact["raw_result"], dict):
                            artifact["raw_result"]["assets"] = ordered_assets
                            artifact["raw_result"]["product_references"] = ordered_assets
                            artifact["raw_result"]["media_assets"] = ordered_assets
                    _persist_exchange_image_pack(
                        self.workflow_repository,
                        workflow_id,
                        artifact,
                        report.report_id,
                    )
            return result

        if stage == QCStage.KEYFRAME:
            package = artifact_override or aggregate.get("storyboard") or {}
            essential_only = (aggregate.get("creative_policy") or {}).get("qc_level") == "low"
            if not isinstance(package, dict) or not package.get("generated_keyframes"):
                return {
                    "status": "unknown",
                    "stage": stage.value,
                    "reason": "Durable generated keyframe package is missing",
                }
            pack = aggregate.get("resource_pack") or {}
            product = normalize_product_identity(pack, source_context=source_context)
            
            # Fail-closed: never backfill missing digest from current disk bytes
            package_digest = str(package.get("package_digest") or "").strip()
            if not package_digest:
                return {
                    "status": "failed",
                    "stage": stage.value,
                    "reason": "Keyframe package is missing required package_digest; cannot compute from current disk state",
                }
            artifact_digest = package_digest
            evaluator = self.evaluator or (
                MultimodalEvaluator(fake_mode=True)
                if provider_mode == "fake"
                else _configured_qc_evaluator(stage.value)
            )
            provider_error = _run_configured_preflight(evaluator) if provider_mode == "configured" else None
            if provider_error:
                if provider_error:
                    return {"status": "pending", "stage": stage.value, "code": "QC_PROVIDER_UNAVAILABLE", "reason": provider_error}
            fingerprint = qc_fingerprint(
                workflow_id,
                stage,
                artifact_digest,
                product,
                evaluator_name=str(getattr(evaluator, "evaluator_name", "unknown")),
                evaluator_version=str(getattr(evaluator, "evaluator_version", "unknown")),
                model_identifier=str(getattr(evaluator, "model", "")),
                storyboard=package,
                reference_digests=[
                    compute_file_sha256(_reference_url_to_path(path))
                    for item in package.get("generated_keyframes", [])
                    if isinstance(item, dict)
                    for path in item.get("reference_image_paths", [])
                ],
                attempt_id=str(effective_attempt_id),
                keyframe_essential_only=essential_only,
            )
            allocation, completed = _admit(fingerprint)
            if completed:
                return completed
            report = run_keyframe_qc(
                workflow_id=workflow_id,
                package=package,
                product=product,
                evaluator=evaluator,
                repo=self.repository,
                attempt_id=str(effective_attempt_id),
                owner_user_id=owner_user_id,
                project_id=project_id,
                essential_only=essential_only,
            )
            return _finish(fingerprint, report, allocation)

        render_artifact = artifact_override or aggregate.get("render_result") or {}
        required_render_lineage = (
            "owner_user_id",
            "project_id",
            "workflow_id",
            "artifact_id",
            "artifact_version",
            "sha256",
        )
        missing_render_lineage = [
            field
            for field in required_render_lineage
            if field not in render_artifact
            or render_artifact[field] is None
            or (
                isinstance(render_artifact[field], str)
                and not render_artifact[field].strip()
            )
        ]
        if missing_render_lineage:
            return {
                "status": "failed",
                "stage": stage.value,
                "reason": "Render artifact is missing canonical lineage fields: "
                + ", ".join(missing_render_lineage),
            }

        render_owner = str(render_artifact["owner_user_id"]).strip()
        render_project = str(render_artifact["project_id"]).strip()
        render_workflow = str(render_artifact["workflow_id"]).strip()
        artifact_id = str(render_artifact["artifact_id"]).strip()
        if render_owner != owner_user_id:
            return {
                "status": "failed",
                "stage": stage.value,
                "reason": "Render owner_user_id does not match workflow owner_user_id",
            }
        if render_project != project_id:
            return {
                "status": "failed",
                "stage": stage.value,
                "reason": "Render project_id does not match workflow project_id",
            }
        if render_workflow != workflow_id:
            return {
                "status": "failed",
                "stage": stage.value,
                "reason": "Render workflow_id does not match the workflow being evaluated",
            }

        render_ver_raw = render_artifact["artifact_version"]
        if (
            isinstance(render_ver_raw, bool)
            or not isinstance(render_ver_raw, int)
            or render_ver_raw < 1
        ):
            return {
                "status": "failed",
                "stage": stage.value,
                "reason": "Render artifact_version must be a positive integer",
            }
        render_ver = render_ver_raw
        compatibility_version = render_artifact.get("version")
        if compatibility_version is not None and compatibility_version != render_ver:
            return {
                "status": "failed",
                "stage": stage.value,
                "reason": "Render version does not match artifact_version",
            }

        expected_sha = (
            str(render_artifact["sha256"])
            .strip()
            .removeprefix("sha256:")
            .lower()
        )
        if len(expected_sha) != 64 or any(
            ch not in "0123456789abcdef" for ch in expected_sha
        ):
            return {
                "status": "failed",
                "stage": stage.value,
                "reason": "Render sha256 must be a canonical 64-character hexadecimal digest",
            }

        storyboard = storyboard_override or aggregate.get("storyboard") or {}
        storyboard = _payload_dict(storyboard) or storyboard
        if isinstance(storyboard, dict):
            # Scene plan is the durable source for approved boundaries when a
            # renderer stores only the frame prompts in its storyboard output.
            scene_plan = _payload_dict(aggregate.get("scene_plan") or {})
            if not (storyboard.get("scene_boundaries") or storyboard.get("scenes")):
                plan_scenes = scene_plan.get("scene_boundaries") or scene_plan.get("scenes")
                if isinstance(plan_scenes, list) and plan_scenes:
                    storyboard = dict(storyboard)
                    storyboard["scene_boundaries"] = plan_scenes
            if not storyboard.get("frames") and isinstance(storyboard.get("scenes"), list):
                storyboard = dict(storyboard)
                storyboard["frames"] = [
                    {
                        "description": scene.get("visual_objective") or scene.get("subject_action") or "",
                        "duration_seconds": scene.get("duration_seconds"),
                    }
                    for scene in storyboard["scenes"]
                    if isinstance(scene, dict)
                ]
        video_value = (
            render_artifact.get("file_path")
            or render_artifact.get("video_url")
            or render_artifact.get("final_video_url")
            or render_artifact.get("output_url")
        )
        if not video_value:
            return {"status": "unknown", "stage": stage.value, "reason": "Current render has no local video output"}
        if not storyboard:
            return {"status": "unknown", "stage": stage.value, "reason": "Approved storyboard is missing from workflow aggregate"}

        pack = aggregate.get("resource_pack") or {}
        product = normalize_product_identity(pack, source_context=source_context)
        image_digest = compute_artifact_digest(pack)
        image_report = self.repository.get_latest_report(
            workflow_id,
            QCStage.IMAGE,
            expected_artifact_digest=image_digest,
        )
        if image_report is None or image_report.verdict.value != "pass":
            return {"status": "pending", "stage": stage.value, "reason": "Current image QC PASS is required before video QC"}
        reference_manifest = image_report.findings.get("eligible_reference_manifest", [])
        if not isinstance(reference_manifest, list) or not reference_manifest:
            reference_manifest = image_report.findings.get("items", [])
        approved_paths = []
        for item in reference_manifest if isinstance(reference_manifest, list) else []:
            if not isinstance(item, dict) or not item.get("eligible"):
                continue
            reference_value = item.get("local_path") or item.get("uri") or item.get("path")
            if reference_value:
                approved_paths.append(_reference_url_to_path(reference_value))
        if len(approved_paths) < 1 or any(not path.is_file() for path in approved_paths):
            return {"status": "unknown", "stage": stage.value, "reason": "Current image QC has no usable eligible reference manifest"}
        render_payload = _payload_dict(render_artifact)
        creative_policy = aggregate.get("creative_policy") or {}
        four_agent = creative_policy.get("creative_mode") == "four_agent"
        technical_only = provider_mode == "configured" and creative_policy.get("qc_level") == "low"
        video_path = _file_url_to_path(video_value)
        video_sha = compute_file_sha256(video_path)
        if video_sha != expected_sha:
            return {
                "status": "failed",
                "stage": stage.value,
                "reason": f"Render artifact SHA ({expected_sha}) does not match video on disk ({video_sha})",
            }
        final_review_evidence = None
        if four_agent and not technical_only and render_payload.get("final_review"):
            final_review_evidence = {
                key: render_payload[key]
                for key in ("sha256", "technical_qc", "final_review", "review_context", "review_media")
                if key in render_payload
            }
            try:
                from video_factory.creative.agents.video_review import require_final_content_pass
                require_final_content_pass({**final_review_evidence, "file_path": str(video_path)})
            except (KeyError, ValueError, OSError) as exc:
                return {"status": "unknown", "stage": stage.value,
                        "reason": f"Current final review cannot be safely reused: {exc}"}
            reviewer_receipt = final_review_evidence["final_review"].get("evaluator_receipt") or {}
            reviewer_provider = str(reviewer_receipt.get("provider") or "").strip()
            reviewer_model = str(reviewer_receipt.get("model") or "").strip()
            if provider_mode == "configured" and (
                not reviewer_provider
                or not reviewer_model
                or reviewer_provider.casefold() in {"fake", "mock", "offline", "test"}
            ):
                return {"status": "unknown", "stage": stage.value,
                        "reason": "Final review receipt is not from a configured content evaluator"}
            lineage = render_payload.get("creative_lineage") or {}
            if (
                str(render_payload.get("sha256") or "").removeprefix("sha256:") != video_sha
                or (lineage.get("package_digest")
                    != final_review_evidence["review_context"].get("package_digest"))
            ):
                return {"status": "unknown", "stage": stage.value,
                        "reason": "Final review is not bound to the current render package and SHA"}

        if four_agent and creative_policy.get('qc_level') == 'high' and final_review_evidence is None:
            return {"status":"unknown", "stage":stage.value,
                    "reason":"High QC requires current final content evidence"}
        technical_only_final = technical_only or (four_agent and final_review_evidence is None)
        if technical_only_final:
            from types import SimpleNamespace
            evaluator = SimpleNamespace(evaluator_name="technical_probe", evaluator_version="1",
                model="", fake_mode=False)
        elif final_review_evidence:
            from types import SimpleNamespace
            review = final_review_evidence["final_review"]
            receipt = review.get("evaluator_receipt") or {}
            route = receipt.get("route") if isinstance(receipt.get("route"), dict) else {}
            evaluator = self.evaluator or SimpleNamespace(
                evaluator_name=str(receipt.get("provider") or "creative_critic_final"),
                evaluator_version=str(receipt.get("prompt_version") or "unknown"),
                model=str(receipt.get("model") or route.get("upstream_model") or route.get("model") or ""),
                fake_mode=str(receipt.get("provider") or "").casefold() in {"fake", "mock", "offline", "test"},
            )
        else:
            evaluator = self.evaluator or (
                MultimodalEvaluator(fake_mode=True)
                if provider_mode == "fake"
                else _configured_qc_evaluator(stage.value)
            )
        provider_error = (
            _run_configured_preflight(evaluator)
            if provider_mode == "configured" and not final_review_evidence and not technical_only_final else None
        )
        if provider_error:
            return {"status": "pending", "stage": stage.value, "code": "QC_PROVIDER_UNAVAILABLE", "reason": provider_error}
        duration_value = render_artifact.get("duration_seconds")
        if duration_value is None and isinstance(storyboard, dict):
            boundaries = storyboard.get("scene_boundaries") or storyboard.get("scenes") or []
            durations: list[float] = []
            for boundary in boundaries if isinstance(boundaries, list) else []:
                if not isinstance(boundary, dict):
                    continue
                try:
                    if boundary.get("duration_seconds") is not None:
                        durations.append(float(boundary["duration_seconds"]))
                    elif boundary.get("start_seconds") is not None and boundary.get("end_seconds") is not None:
                        durations.append(float(boundary["end_seconds"]) - float(boundary["start_seconds"]))
                except (TypeError, ValueError):
                    durations = []
                    break
            duration_value = sum(durations) if durations and all(value > 0 for value in durations) else 16.0
        expected_duration = float(duration_value or 16.0)
        if four_agent:
            storyboard = {**storyboard, "_technical_contract": {
                **FOUR_AGENT_VIDEO_TECHNICAL_CONTRACT, "expected_duration": expected_duration,
            }}
            if isinstance(render_payload.get("final_review"), dict):
                storyboard["_creative_final_review_input_digest"] = render_payload["final_review"].get("input_digest")
        raw_scene_providers = render_payload.get("scene_provider_kinds")
        scene_provider_kinds = (
            raw_scene_providers
            if isinstance(raw_scene_providers, list)
            and render_payload.get("artifact_id") == artifact_id
            and str(render_payload.get("sha256") or "").removeprefix("sha256:") == video_sha
            and render_payload.get("render_id") == render_artifact.get("render_id")
            and all(kind in {"google_flow", "vertex_ai"} for kind in raw_scene_providers)
            else []
        )

        fingerprint = qc_fingerprint(
            workflow_id,
            stage,
            video_sha,
            product,
            evaluator_name=str(getattr(evaluator, "evaluator_name", "unknown")),
            evaluator_version=str(getattr(evaluator, "evaluator_version", "unknown")),
            model_identifier=str(getattr(evaluator, "model", "")),
            storyboard=storyboard,
            video_sha256=video_sha,
            reference_digests=[compute_file_sha256(path) for path in approved_paths],
            attempt_id=str(effective_attempt_id),
            scene_provider_kinds=scene_provider_kinds,
        )
        allocation, completed = _admit(fingerprint)
        if completed:
            return completed
        report = run_video_qc(
            workflow_id,
            video_path,
            storyboard,
            product,
            approved_paths,
            expected_duration=expected_duration,
            evaluator=evaluator,
            repo=self.repository,
            attempt_id=str(effective_attempt_id),
            owner_user_id=owner_user_id,
            project_id=project_id,
            artifact_id=artifact_id,
            artifact_version=render_ver,
            scene_provider_kinds=scene_provider_kinds,
            four_agent=four_agent,
            final_review_evidence=final_review_evidence,
            technical_only=technical_only,
        )
        result = _finish(fingerprint, report, allocation)
        result["video_sha256"] = video_sha
        return result

    def log_manual_review(
        self,
        scene_id: str,
        video_path: Path,
        collision_zones: list[str],
        action_centroid: tuple[float, float],
        review_log_path: Path = Path("workspace/manual_review_queue.jsonl"),
    ) -> dict[str, Any]:
        """Log safe zone collision or borderline scene to manual review queue."""
        review_log_path = Path(review_log_path)
        review_log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "scene_id": scene_id,
            "video_path": str(video_path),
            "collision_zones": collision_zones,
            "action_centroid": list(action_centroid),
            "status": "pending_manual_review",
        }
        with open(review_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def list_manual_reviews(
        self,
        review_log_path: Path = Path("workspace/manual_review_queue.jsonl"),
        status_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """List manual review records, optionally filtered by status."""
        review_log_path = Path(review_log_path)
        if not review_log_path.exists():
            return []
        items = []
        with open(review_log_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        record = json.loads(line)
                        if status_filter is None or record.get("status") == status_filter:
                            items.append(record)
                    except json.JSONDecodeError:
                        continue
        return items

    def resolve_manual_review(
        self,
        scene_id: str,
        resolution: str,  # 'approved', 'rejected', 'rerender_requested'
        notes: str = "",
        review_log_path: Path = Path("workspace/manual_review_queue.jsonl"),
    ) -> bool:
        """Update resolution for an item in the manual review queue."""
        review_log_path = Path(review_log_path)
        if not review_log_path.exists():
            return False
        records = []
        updated = False
        with open(review_log_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        rec = json.loads(line)
                        if rec.get("scene_id") == scene_id and rec.get("status") == "pending_manual_review":
                            rec["status"] = resolution
                            rec["resolution_notes"] = notes
                            rec["resolved_at"] = datetime.now(timezone.utc).isoformat()
                            updated = True
                        records.append(rec)
                    except json.JSONDecodeError:
                        continue
        if updated:
            with open(review_log_path, "w", encoding="utf-8") as f:
                for rec in records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return updated

    def select_image_references(
        self,
        workflow_id: str,
        *,
        resource_pack: dict[str, Any],
        source_context: dict[str, Any] | None = None,
        provider_mode: str = "configured",
        authorization: dict[str, Any] | None = None,
        max_cost_usd: float | None = None,
        qc_budget_mode: str | None = None,
        max_recoveries: int | None = None,
        owner_user_id: str | None = None,
        project_id: str | None = None,
        max_references: int = 1,
    ) -> dict[str, Any]:
        """Select at least one verified PASS image reference using durable quota and evaluation.

        If initial candidate images yield no PASS reference, substitutes
        candidates from source_context['images'] bounded by the durable quota ledger.
        Freezes selected pack, persists verified PASS QCReport, and completes attempt in ledger.
        """
        source_ctx = source_context or {}
        owner = owner_user_id or resource_pack.get("owner_user_id") or source_ctx.get("owner_user_id") or "local-owner"
        proj = project_id or resource_pack.get("project_id") or source_ctx.get("project_id") or f"proj_{workflow_id}"
        pack_id = str(resource_pack.get("resource_pack_id") or resource_pack.get("id") or f"pack_{workflow_id}")
        version = int(resource_pack.get("version") or 1)

        from video_factory.automation.contracts import compute_canonical_product_key
        from video_factory.qc.models import NormalizedProduct, QCReport, QCStage, QCVerdict
        from video_factory.qc.normalization import normalize_product_identity
        from video_factory.qc.fingerprint import IMAGE_POLICY_VERSION, IMAGE_RUBRIC_VERSION
        from video_factory.qc.normalization import resource_pack_fingerprint
        from video_factory.qc.retry_policy import (
            claim_qc_attempt,
            complete_qc_attempt,
            ensure_qc_retry_schema,
            get_qc_retry_count,
            QCQuotaExhaustedError,
            clamp_max_retries,
        )
        from video_factory.qc.image_qc import (
            compute_file_sha256,
        )

        assets = list(
            resource_pack.get("assets", [])
            or resource_pack.get("product_references", [])
            or resource_pack.get("media_assets", [])
        )
        if not assets:
            mapping = resource_pack.get("assets_mapping")
            if isinstance(mapping, list):
                assets = [dict(item) for item in mapping if isinstance(item, Mapping)]
            elif isinstance(mapping, Mapping):
                assets = [
                    {"asset_id": asset_id, "local_path": path}
                    for asset_id, path in mapping.items()
                ]
        assets = [
            {
                **asset,
                "local_path": str(
                    asset.get("local_path")
                    or asset.get("absolute_path")
                    or asset.get("path")
                    or asset.get("file_path")
                    or ""
                ),
            }
            for asset in assets
            if isinstance(asset, Mapping)
        ]
        resource_pack, gate_code, gate_reason = _exchange_image_pack_gate(
            source_ctx,
            {**resource_pack, "assets": assets},
        )
        if gate_code:
            return {
                "status": "failed",
                "code": gate_code,
                "reason": gate_reason,
                "resource_pack": resource_pack,
            }
        if source_ctx.get("source") == "hermes_product_video_exchange":
            assets = list(resource_pack.get("assets") or [])
        initial_inputs = []
        for asset in assets:
            loc = str(asset.get("local_path") or asset.get("path") or asset.get("absolute_path") or "")
            try:
                disk_hash = compute_file_sha256(Path(loc)) if loc and Path(loc).is_file() else "missing"
            except OSError:
                disk_hash = "unreadable"
            initial_inputs.append([str(asset.get("asset_id") or ""), loc, disk_hash])
        from video_factory.qc.normalization import _unwrap_resource_pack
        identity_pack = _unwrap_resource_pack(resource_pack)
        prod_ident = resource_pack.get("product_identity")
        if not isinstance(prod_ident, dict) or not prod_ident:
            prod_ident = identity_pack.get("product_identity") or {}
        brand = str(source_ctx.get("brand") or resource_pack.get("brand") or prod_ident.get("brand") or "").strip()
        model = str(source_ctx.get("model") or resource_pack.get("model") or prod_ident.get("model") or "").strip()
        variant = str(source_ctx.get("variant") or resource_pack.get("variant") or prod_ident.get("variant") or "").strip() or None
        display_name = str(source_ctx.get("display_name") or prod_ident.get("name") or resource_pack.get("product_name") or f"{brand} {model}".strip()).strip()
        if brand and model:
            product_key = compute_canonical_product_key(brand, model, variant)
        else:
            product_key = pack_id
        input_revision = str(source_ctx.get("source_input_revision") or "").removeprefix("sha256:")
        if input_revision and source_ctx.get("source") == "hermes_product_video_exchange":
            product_key = f"{product_key}:input:{input_revision}"

        try:
            grounded = normalize_product_identity(resource_pack, source_context=source_ctx)
        except ValueError:
            grounded = None
        product = NormalizedProduct(
            brand=brand or "Unknown",
            model=model or "Unknown",
            variant=variant or "",
            display_name=display_name or f"{brand} {model}".strip(),
            category=str(source_ctx.get("category") or prod_ident.get("category") or "Thiết bị tiện ích"),
            # Cache/replay must carry only claims that passed the Catalog v2
            # SOURCE_BACKED locator+snippet check; caller-provided strings are
            # input context, never proof.
            verified_claims=list(grounded.verified_claims if grounded else []),
            source_context=source_ctx,
        )

        n_recoveries = clamp_max_retries(max_recoveries)
        evaluator = self.evaluator or (
            MultimodalEvaluator(fake_mode=True)
            if provider_mode == "fake"
            else _configured_qc_evaluator("image")
        )
        ensure_qc_retry_schema(self.connection)
        context_hash = _image_qc_context_hash(product, evaluator)
        initial_input_hash = _initial_image_attempt_hash(initial_inputs, context_hash)

        # Legacy QC money flags are accepted by the API for compatibility but
        # deliberately do not participate in admission or retry identity.
        budget_alloc = None
        attempt_token = ""
        operation_suffix = ""
        claim_attempt_id = f"img_claim_{workflow_id}_v{version}"
        initial_op_id = f"op_init_img_{workflow_id}_v{version}"
        prior_attempt = False
        reconciled_prior = self.connection.execute(
            """SELECT 1 FROM workflow_qc_retries
               WHERE workflow_id = ? AND retry_type = 'source_image_qc'
                 AND status IN ('reconciled_consumed', 'reconciled_not_sent', 'response_invalid') LIMIT 1""",
            (workflow_id,),
        ).fetchone()
        if reconciled_prior:
            next_attempt = get_qc_retry_count(self.connection, workflow_id, "source_image_qc") + 1
            attempt_token = f"a{next_attempt}"
            operation_suffix = f"_{attempt_token}"
            claim_attempt_id = f"img_claim_{workflow_id}_v{version}{operation_suffix}"
            initial_op_id = f"op_init_img_{workflow_id}_v{version}{operation_suffix}"
        prior_attempt = self.connection.execute(
            """SELECT 1 FROM workflow_qc_retries
               WHERE workflow_id = ? AND retry_type = 'source_image_qc'
                 AND item_id = ? AND operation_id = ? LIMIT 1""",
            (workflow_id, product_key, initial_op_id),
        ).fetchone()
        if not prior_attempt and provider_mode == "configured":
            provider_error = _run_configured_preflight(evaluator)
            if provider_error:
                return {
                    "status": "pending",
                    "code": "QC_PROVIDER_UNAVAILABLE",
                    "reason": provider_error,
                    "resource_pack": resource_pack,
                }

        try:
            init_claim = claim_qc_attempt(
                self.connection,
                workflow_id=workflow_id,
                retry_type="source_image_qc",
                item_id=product_key,
                input_hash=initial_input_hash,
                operation_id=initial_op_id,
                max_recoveries=n_recoveries,
            )
        except QCQuotaExhaustedError as e:
            # Quota rejection happens before any evaluator call.  A configured
            # reservation made above therefore has no provider exposure.
            if budget_alloc is not None:
                self.budget_repository.release_budget(budget_alloc)
            return {
                "status": "failed",
                "code": "QC_QUOTA_EXHAUSTED",
                "reason": f"Image QC quota exhausted for product '{product_key}': {e.attempts_used}/{e.max_allowed} attempts used",
                "resource_pack": resource_pack,
            }
        except ValueError as exc:
            # Replay input validation also fails before a provider request.
            if budget_alloc is not None:
                self.budget_repository.release_budget(budget_alloc)
            return {
                "status": "pending",
                "code": "QC_OUTCOME_UNCERTAIN",
                "reason": f"Initial Image QC replay input changed or lacks a prior hash: {exc}",
                "resource_pack": resource_pack,
            }

        if init_claim.get("reused") and init_claim.get("status") != "completed":
            return {
                "status": "pending",
                "code": "QC_OUTCOME_UNCERTAIN",
                "reason": "Initial Image QC attempt is already claimed; reconcile before retrying",
                "resource_pack": resource_pack,
            }

        if init_claim.get("reused") and init_claim.get("status") == "completed":
            from video_factory.qc.validation import require_current_pass_report, QCValidationError
            try:
                prior_id = str(init_claim.get("qc_report_id") or "")
                prior_saved = self.repository.get_report(prior_id) if prior_id else None
                selected = (prior_saved.findings or {}).get("eligible_reference_manifest") if prior_saved else None
                if not isinstance(selected, list) or len(selected) < 1:
                    raise QCValidationError("QC_INSUFFICIENT_COVERAGE", "Completed attempt lacks its selected image manifest")
                selected_pack = dict(resource_pack)
                selected_pack["product_references"] = selected
                selected_pack["media_assets"] = selected
                selected_pack["assets"] = selected
                selected_pack["pack_status"] = "ready_to_lock"
                prior_rep = require_current_pass_report(
                    self.repository,
                    workflow_id=workflow_id,
                    stage=QCStage.IMAGE,
                    artifact=selected_pack,
                    owner_user_id=owner,
                    project_id=proj,
                    provider_mode=provider_mode,
                )
                if prior_rep.report_id != prior_id:
                    raise QCValidationError("QC_STALE_REPORT", "Completed attempt points to a different Image QC report")
                return {
                    "status": "completed",
                    "resource_pack": selected_pack,
                    "report_id": prior_rep.report_id,
                    "report": prior_rep,
                }
            except QCValidationError as err:
                return {
                    "status": "failed",
                    "code": "IMAGE_QC_FAILED",
                    "reason": f"Reused initial attempt does not match current resource pack: {err.reason}",
                    "resource_pack": resource_pack,
                }

        from video_factory.qc.selection import select_distinct_pass_images

        distinct_passed, evaluated_items, early_error = select_distinct_pass_images(
            connection=self.connection,
            repository=self.repository,
            evaluator=evaluator,
            product=product,
            context_hash=context_hash,
            assets=assets,
            source_context=source_ctx,
            workflow_id=workflow_id,
            product_key=product_key,
            n_recoveries=n_recoveries,
            provider_mode=provider_mode,
            owner=owner,
            proj=proj,
            init_claim=init_claim,
            resource_pack=resource_pack,
            budget_alloc=budget_alloc,
            budget_repository=self.budget_repository,
            attempt_token=attempt_token,
            max_references=max_references,
        )
        qc_cost_usd, qc_cost_status = _recorded_route_cost(evaluated_items)
        if early_error is not None:
            attempt_row = self.connection.execute(
                "SELECT status,verdict FROM workflow_qc_retries WHERE id=?",
                (init_claim["attempt_id"],),
            ).fetchone()
            if attempt_row is not None:
                complete_qc_attempt(
                    self.connection,
                    attempt_id=init_claim["attempt_id"],
                    status=str(attempt_row["status"]),
                    verdict=str(attempt_row["verdict"]),
                    budget_reference=budget_alloc.reservation_id if budget_alloc else "",
                    cost_usd=qc_cost_usd,
                )
            if isinstance(early_error, dict):
                early_error["cost_status"] = qc_cost_status
                early_error["cost_usd"] = qc_cost_usd
                early_error["evaluated_items"] = evaluated_items
            return early_error

        if len(distinct_passed) < 1:
            evaluator_was_called = bool(evaluated_items)
            has_unknown = any(it.get("verdict") == "unknown" for it in evaluated_items) or not evaluator_was_called
            if budget_alloc is not None:
                if evaluator_was_called:
                    self.budget_repository.commit_budget(budget_alloc)
                else:
                    self.budget_repository.release_budget(budget_alloc)
            complete_qc_attempt(
                self.connection,
                attempt_id=init_claim["attempt_id"],
                status="completed",
                verdict="unknown" if has_unknown else "fail",
                budget_reference=budget_alloc.reservation_id if budget_alloc else "",
                cost_usd=qc_cost_usd,
                reason_text=json.dumps({'reason':"No eligible image reference",
                                        'evaluated_items':evaluated_items}, ensure_ascii=False),
            )
            return {
                "status": "pending" if has_unknown else "failed",
                "code": "IMAGE_QC_FAILED",
                "reason": "Visual Image QC failed: no usable eligible image reference is available.",
                "resource_pack": resource_pack,
                "distinct_passed_count": len(distinct_passed),
                "usage_status": "consumed" if evaluator_was_called else "not_sent",
                "cost_status": qc_cost_status,
                "cost_usd": qc_cost_usd,
                "evaluated_items": evaluated_items,
            }

        # Keep distinct verified references in source order.
        selected_refs = distinct_passed
        if max_references > 1:
            for rank, reference in enumerate(selected_refs, start=1):
                findings = reference["findings"]
                reference.update(
                    reference_rank=rank,
                    view_angle=findings.get("view_angle") or "",
                    shot_type=findings.get("shot_type") or "",
                    visible_details=findings.get("visible_details") or [],
                    scene_suitability=findings.get("scene_suitability") or [],
                )
        result_pack = dict(resource_pack)
        result_pack["product_references"] = selected_refs
        result_pack["media_assets"] = selected_refs
        result_pack["assets"] = selected_refs
        result_pack["pack_status"] = "ready_to_lock"

        pack_digest = resource_pack_fingerprint(result_pack)
        ref_hashes = [r["sha256"] for r in selected_refs]
        report_id = f"qc_img_{workflow_id}_v{version}"
        avg_score = round(sum(int(r.get("score", 0)) for r in selected_refs) / len(selected_refs))

        passed_findings = []
        for r in selected_refs:
            digest_val = r.get("sha256") or r.get("digest") or ""
            passed_findings.append({
                "asset_id": r["asset_id"],
                "score": r["score"],
                "verdict": "pass",
                "eligible": True,
                "hard_failure": False,
                "findings": r.get("findings") or {},
                "sha256": digest_val,
                "digest": digest_val,
            })

        observed_receipts: list[Mapping[str, Any]] = []
        for item in evaluated_items:
            findings = item.get("findings") if isinstance(item, dict) else None
            receipt = findings.get("route_receipt") if isinstance(findings, dict) else None
            if isinstance(receipt, Mapping):
                observed_receipts.append(receipt)
        actual_route = observed_receipts[0] if observed_receipts else {}

        report = QCReport(
            report_id=report_id,
            workflow_id=workflow_id,
            stage=QCStage.IMAGE,
            artifact_id=pack_id,
            artifact_version=version,
            artifact_digest=pack_digest,
            reference_digests=ref_hashes,
            evaluator_name=str(getattr(evaluator, "evaluator_name", "image_qc_multimodal")),
            evaluator_version=str(getattr(evaluator, "evaluator_version", "1.0")),
            model_identifier=str(getattr(evaluator, "model", type(evaluator).__name__)),
            rubric_version=IMAGE_RUBRIC_VERSION,
            policy_version=IMAGE_POLICY_VERSION,
            created_at=datetime.now(timezone.utc).isoformat(),
            score=avg_score,
            verdict=QCVerdict.PASS,
            findings={
                "automatic": True,
                "eligible_count": len(selected_refs),
                "total_count": len(evaluated_items),
                "items": passed_findings,
                "evaluation_history": evaluated_items,
                "eligible_reference_manifest": selected_refs,
                "cost_status": qc_cost_status,
                "cost_usd": qc_cost_usd,
                "actual_provider": str(actual_route.get("upstream_provider") or getattr(evaluator, "evaluator_name", "unknown")),
                "actual_model": str(actual_route.get("upstream_model") or getattr(evaluator, "model", "unknown")),
            },
            cost_usd=qc_cost_usd,
            provider_mode=provider_mode,
            owner_user_id=owner,
            project_id=proj,
        )
        self.repository.save_report(report)

        if budget_alloc is not None:
            self.budget_repository.commit_budget(budget_alloc)

        complete_qc_attempt(
            self.connection,
            attempt_id=init_claim["attempt_id"],
            status="completed",
            verdict="pass",
            qc_report_id=report_id,
            output_hash=pack_digest,
            budget_reference=budget_alloc.reservation_id if budget_alloc else "",
            cost_usd=qc_cost_usd,
        )

        return {
            "status": "completed",
            "verdict": "pass",
            "resource_pack": result_pack,
            "report_id": report_id,
            "report": report,
            "selected_count": len(selected_refs),
        }
