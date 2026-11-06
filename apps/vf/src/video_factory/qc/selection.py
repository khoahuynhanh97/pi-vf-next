"""Internal image reference selection and candidate evaluation algorithms."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from video_factory.qc.fingerprint import IMAGE_POLICY_VERSION, IMAGE_RUBRIC_VERSION
from video_factory.qc.evaluator import is_eligible_image_evidence
from video_factory.qc.image_qc import (
    _is_decodable_image,
    compute_file_sha256,
    evaluate_image_item,
)
from video_factory.qc.models import ImageQCItem, NormalizedProduct, QCReport, QCStage, QCVerdict
from video_factory.qc.repository import QCReportRepository
from video_factory.qc.retry_policy import (
    QCQuotaExhaustedError,
    claim_qc_attempt,
    complete_qc_attempt,
    get_item_remaining_recoveries,
)
from video_factory.qc.visual_similarity import are_images_perceptually_duplicate
from video_factory.handlers.common import _resolve_image_bytes_hash
from datetime import datetime, timezone


def _get_path(it: dict[str, Any]) -> str:
    return str(it.get("local_path") or it.get("path") or it.get("absolute_path") or "").strip()


def candidate_image_attempt_hash(image_sha256: str, context_hash: str, identity_reference: ImageQCItem | None = None) -> str:
    # Keep the public call shape for replay compatibility; source QC has no anchor.
    return hashlib.sha256(f"{image_sha256}:{context_hash}".encode("utf-8")).hexdigest()


def _item_provider_cost(item: Any) -> tuple[float | None, str, dict[str, Any]]:
    findings = getattr(item, "findings", {})
    receipt = findings.get("route_receipt") if isinstance(findings, dict) else None
    if not isinstance(receipt, dict):
        return None, "unknown", {}
    try:
        cost = float(receipt["cost_usd"]) if receipt.get("cost_status") in {"recorded", "charged"} else None
    except (KeyError, TypeError, ValueError):
        cost = None
    return cost, "recorded" if cost is not None else "unknown", receipt


def select_distinct_pass_images(
    *,
    connection: sqlite3.Connection,
    repository: QCReportRepository,
    evaluator: Any,
    product: NormalizedProduct,
    context_hash: str,
    assets: list[dict[str, Any]],
    source_context: dict[str, Any],
    workflow_id: str,
    product_key: str,
    n_recoveries: int,
    provider_mode: str,
    owner: str,
    proj: str,
    init_claim: dict[str, Any],
    resource_pack: dict[str, Any],
    budget_alloc: Any | None = None,
    budget_repository: Any | None = None,
    attempt_token: str = "",
    max_references: int = 1,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
    """Evaluate initial assets until the requested number of distinct PASS images.

    Preserves exact evaluation order, durable retry accounting, perceptual deduplication,
    and fail-closed budget/attempt handling.
    """
    distinct_passed: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    initially_evaluated_hashes: set[str] = set()
    evaluated_items: list[dict[str, Any]] = []

    # 1. Evaluate initial assets
    for idx, asset in enumerate(assets):
        asset_id = str(asset.get("asset_id") or f"init_{idx+1}")
        loc = _get_path(asset)
        if not loc:
            continue
        p = Path(loc)
        if not p.is_file():
            continue
        sha = _resolve_image_bytes_hash({"local_path": loc}) or compute_file_sha256(p)
        if not sha:
            continue
        if sha in initially_evaluated_hashes:
            continue
        initially_evaluated_hashes.add(sha)
        if not _is_decodable_image(p):
            continue
        qc_item = evaluate_image_item(
            asset_id=asset_id,
            local_path=p,
            product=product,
            evaluator=evaluator,
            uri=asset.get("uri") or f"file:///{str(p).replace(chr(92), '/')}",
            identity_reference=None,
        )
        item_record = qc_item.to_dict()
        item_record["qc_attempt_number"] = int(init_claim.get("attempt_number") or 1)
        item_record["operation_id"] = str(init_claim.get("operation_id") or "")
        evaluated_items.append(item_record)
        if qc_item.findings.get("schema_invalid"):
            cost, _cost_status, receipt = _item_provider_cost(qc_item)
            complete_qc_attempt(
                connection,
                attempt_id=init_claim["attempt_id"],
                status="response_invalid",
                verdict="unknown",
                cost_usd=cost,
                reason_text=json.dumps({
                    "reason": qc_item.reason,
                    "asset_id": asset_id,
                    "input_sha256": sha,
                    "provider": receipt.get("upstream_provider"),
                    "model": receipt.get("upstream_model"),
                    "cost_status": receipt.get("cost_status", "unknown"),
                    "operation_id": init_claim.get("operation_id"),
                }, ensure_ascii=False, sort_keys=True),
            )
            return distinct_passed, evaluated_items, {
                "status": "pending",
                "code": "QC_RESPONSE_INVALID",
                "reason": f"QC response for asset '{asset_id}' was received but failed schema validation; retry is allowed within the durable limit",
                "usage_status": "consumed",
                "cost_status": "recorded" if cost is not None else "unknown",
                "cost_usd": cost,
                "retry_allowed": True,
                "resource_pack": resource_pack,
            }
        if qc_item.findings.get("technical_unknown"):
            complete_qc_attempt(
                connection,
                attempt_id=init_claim["attempt_id"],
                status="technical_unknown",
                verdict="unknown",
                budget_reference=budget_alloc.reservation_id if budget_alloc else "",
                reason_text=qc_item.reason,
            )
            return (
                distinct_passed,
                evaluated_items,
                {
                    "status": "pending",
                    "code": "QC_OUTCOME_UNCERTAIN",
                    "reason": f"Image QC outcome is unknown for asset '{asset_id}'; reconcile before another paid evaluation",
                    "resource_pack": resource_pack,
                },
            )
        if qc_item.eligible and sha not in seen_hashes:
            is_dup = False
            for passed in distinct_passed:
                passed_p = _get_path(passed)
                if passed_p and are_images_perceptually_duplicate(p, passed_p):
                    is_dup = True
                    break
            if not is_dup:
                seen_hashes.add(sha)
                distinct_passed.append({
                    "asset_id": asset_id,
                    "local_path": str(p),
                    "absolute_path": str(p),
                    "uri": asset.get("uri") or f"file:///{str(p).replace(chr(92), '/')}",
                    "sha256": sha,
                    "digest": sha,
                    "score": qc_item.score,
                    "eligible": True,
                    "verdict": "pass",
                    "hard_failure": False,
                    "findings": qc_item.findings,
                })
                if len(distinct_passed) >= max_references:
                    break

    # 2. Candidate substitution loop only when no initial image was usable.
    candidate_pool = list(source_context.get("images", []))
    if source_context.get("source") == "hermes_product_video_exchange" and not source_context.get("auto_admitted"):
        selected_ids = set((source_context.get("input_review") or {}).get("confirmed_image_asset_ids") or [])
        candidate_pool = [c for c in candidate_pool if isinstance(c, dict) and c.get("asset_id") in selected_ids]
    remaining_cand = []
    candidate_hashes = set(initially_evaluated_hashes)
    for candidate in candidate_pool:
        if not isinstance(candidate, dict):
            continue
        candidate_path = _get_path(candidate)
        if not candidate_path or not Path(candidate_path).is_file() or not _is_decodable_image(Path(candidate_path)):
            continue
        candidate_hash = _resolve_image_bytes_hash({"local_path": candidate_path})
        if not candidate_hash or candidate_hash in candidate_hashes:
            continue
        candidate_hashes.add(candidate_hash)
        remaining_cand.append(candidate)

    while len(distinct_passed) < 1 and remaining_cand:
        rem = get_item_remaining_recoveries(
            connection,
            workflow_id=workflow_id,
            retry_type="source_image_qc",
            item_id=product_key,
            max_recoveries=n_recoveries,
        )
        if rem <= 0:
            break

        cand = remaining_cand.pop(0)
        cand_loc = _get_path(cand)
        cand_p = Path(cand_loc)
        cand_sha = _resolve_image_bytes_hash({"local_path": cand_loc}) or compute_file_sha256(cand_p)
        cand_id = str(cand.get("asset_id") or f"cand_{len(evaluated_items)+1}")
        candidate_input_hash = candidate_image_attempt_hash(cand_sha, context_hash)

        candidate_op_id = f"op_rec_{workflow_id}_{cand_id}"
        if attempt_token:
            candidate_op_id = f"{candidate_op_id}_{attempt_token}"

        try:
            rec_claim = claim_qc_attempt(
                connection,
                workflow_id=workflow_id,
                retry_type="source_image_qc",
                item_id=product_key,
                input_hash=candidate_input_hash,
                operation_id=candidate_op_id,
                max_recoveries=n_recoveries,
            )
        except QCQuotaExhaustedError:
            # Initial images may already have reached the evaluator.  Let the
            # caller settle their reservation from evaluated_items below.
            break
        except ValueError as exc:
            if budget_alloc is not None and budget_repository is not None:
                if evaluated_items:
                    budget_repository.commit_budget(budget_alloc)
                else:
                    budget_repository.release_budget(budget_alloc)
            return (
                distinct_passed,
                evaluated_items,
                {
                    "status": "pending",
                    "code": "QC_OUTCOME_UNCERTAIN",
                    "reason": f"Candidate Image QC replay input changed or lacks a prior hash: {exc}",
                    "resource_pack": resource_pack,
                },
            )

        if rec_claim.get("reused") and rec_claim.get("status") != "completed":
            return (
                distinct_passed,
                evaluated_items,
                {
                    "status": "pending",
                    "code": "QC_OUTCOME_UNCERTAIN",
                    "reason": f"Candidate Image QC attempt '{rec_claim.get('operation_id')}' is already claimed; reconcile before retrying",
                    "resource_pack": resource_pack,
                },
            )

        if rec_claim.get("reused") and rec_claim.get("status") == "completed":
            persisted_rep = None
            rep_id = rec_claim.get("qc_report_id")
            if rep_id:
                persisted_rep = repository.get_report(rep_id)
            if not persisted_rep:
                persisted_rep = repository.get_latest_report(
                    workflow_id=workflow_id,
                    stage=QCStage.IMAGE,
                    expected_artifact_digest=cand_sha,
                )

            prior_hash = rec_claim.get("input_hash")
            hash_changed = bool(prior_hash and prior_hash != candidate_input_hash)

            valid_reuse = False
            if persisted_rep and not hash_changed:
                clean_art_digest = persisted_rep.artifact_digest.removeprefix("sha256:").lower()
                if (
                    persisted_rep.workflow_id == workflow_id
                    and clean_art_digest == cand_sha.lower()
                    and persisted_rep.rubric_version == IMAGE_RUBRIC_VERSION
                    and persisted_rep.policy_version == IMAGE_POLICY_VERSION
                    and (provider_mode != "configured" or persisted_rep.provider_mode == "configured")
                    and persisted_rep.verdict == QCVerdict.PASS
                    and persisted_rep.owner_user_id == owner
                    and persisted_rep.project_id == proj
                    and is_eligible_image_evidence(persisted_rep.findings)
                ):
                    valid_reuse = True

            if not valid_reuse:
                return (
                    distinct_passed,
                    evaluated_items,
                    {
                        "status": "failed",
                        "code": "IMAGE_QC_FAILED",
                        "reason": f"Reused candidate attempt '{rec_claim.get('operation_id')}' lacks verified persisted PASS item evidence or input bytes changed; fail-closed.",
                        "resource_pack": resource_pack,
                    },
                )

            cand_eligible = True
            cand_reason = str(persisted_rep.findings.get("reason") or "Reused verified prior item evaluation")
            cand_item_score = persisted_rep.score
            cand_findings = persisted_rep.findings["findings"]
            evaluated_items.append({
                "asset_id": cand_id,
                "uri": cand.get("uri") or f"file:///{str(cand_p).replace(chr(92), '/')}",
                "digest": cand_sha,
                "score": cand_item_score,
                "verdict": "pass",
                "eligible": True,
                "hard_failure": False,
                "reason": cand_reason,
                "findings": cand_findings,
            })
        else:
            cand_item = evaluate_image_item(
                asset_id=cand_id,
                local_path=cand_p,
                product=product,
                evaluator=evaluator,
                uri=cand.get("uri") or f"file:///{str(cand_p).replace(chr(92), '/')}",
                identity_reference=None,
            )
            cand_record = cand_item.to_dict()
            cand_record["qc_attempt_number"] = int(rec_claim.get("attempt_number") or 1)
            cand_record["operation_id"] = str(rec_claim.get("operation_id") or "")
            evaluated_items.append(cand_record)
            if cand_item.findings.get("schema_invalid"):
                cost, _cost_status, receipt = _item_provider_cost(cand_item)
                reason_log = json.dumps({
                    "reason": cand_item.reason,
                    "asset_id": cand_id,
                    "input_sha256": cand_sha,
                    "provider": receipt.get("upstream_provider"),
                    "model": receipt.get("upstream_model"),
                    "cost_status": receipt.get("cost_status", "unknown"),
                    "operation_id": rec_claim.get("operation_id"),
                }, ensure_ascii=False, sort_keys=True)
                complete_qc_attempt(
                    connection, attempt_id=rec_claim["attempt_id"],
                    status="response_invalid", verdict="unknown", cost_usd=cost,
                    reason_text=reason_log,
                )
                complete_qc_attempt(
                    connection, attempt_id=init_claim["attempt_id"],
                    status="response_invalid", verdict="unknown", cost_usd=cost,
                    reason_text=reason_log,
                )
                return distinct_passed, evaluated_items, {
                    "status": "pending",
                    "code": "QC_RESPONSE_INVALID",
                    "reason": f"QC response for candidate '{cand_id}' was received but failed schema validation; retry is allowed within the durable limit",
                    "usage_status": "consumed",
                    "cost_status": "recorded" if cost is not None else "unknown",
                    "cost_usd": cost,
                    "retry_allowed": True,
                    "resource_pack": resource_pack,
                }
            if cand_item.findings.get("technical_unknown"):
                complete_qc_attempt(
                    connection,
                    attempt_id=rec_claim["attempt_id"],
                    status="technical_unknown",
                    verdict="unknown",
                    budget_reference=budget_alloc.reservation_id if budget_alloc else "",
                    reason_text=cand_item.reason,
                )
                complete_qc_attempt(
                    connection,
                    attempt_id=init_claim["attempt_id"],
                    status="technical_unknown",
                    verdict="unknown",
                    budget_reference=budget_alloc.reservation_id if budget_alloc else "",
                    reason_text=cand_item.reason,
                )
                return (
                    distinct_passed,
                    evaluated_items,
                    {
                        "status": "pending",
                        "code": "QC_OUTCOME_UNCERTAIN",
                        "reason": f"Image QC outcome is unknown for candidate '{cand_id}'; reconcile before another paid evaluation",
                        "resource_pack": resource_pack,
                    },
                )
            cand_eligible = cand_item.eligible
            cand_reason = cand_item.reason
            cand_item_score = cand_item.score
            cand_findings = cand_item.findings

            cand_rep_id = f"qc_img_cand_{workflow_id}_{cand_id}_v{rec_claim['attempt_number']}"
            cand_rep = QCReport(
                report_id=cand_rep_id,
                workflow_id=workflow_id,
                stage=QCStage.IMAGE,
                artifact_id=cand_id,
                artifact_version=rec_claim["attempt_number"],
                artifact_digest=cand_sha,
                reference_digests=[cand_sha],
                evaluator_name=str(getattr(evaluator, "evaluator_name", "image_qc_multimodal")),
                evaluator_version=str(getattr(evaluator, "evaluator_version", "1.0")),
                model_identifier=str(getattr(evaluator, "model", type(evaluator).__name__)),
                rubric_version=IMAGE_RUBRIC_VERSION,
                policy_version=IMAGE_POLICY_VERSION,
                created_at=datetime.now(timezone.utc).isoformat(),
                score=cand_item.score,
                verdict=cand_item.verdict,
                findings={
                    **cand_item.to_dict(),
                    "product_brand": product.brand,
                    "product_model": product.model,
                },
                provider_mode=provider_mode,
                owner_user_id=owner,
                project_id=proj,
            )
            repository.save_report(cand_rep)

            complete_qc_attempt(
                connection,
                attempt_id=rec_claim["attempt_id"],
                status="completed",
                verdict=cand_item.verdict.value,
                qc_report_id=cand_rep_id,
                output_hash=cand_sha,
                budget_reference=budget_alloc.reservation_id if budget_alloc else "",
                cost_usd=_item_provider_cost(cand_item)[0],
                reason_text=cand_item.reason,
            )

        if cand_eligible and cand_sha not in seen_hashes:
            is_dup = False
            for passed in distinct_passed:
                passed_p = _get_path(passed)
                if passed_p and are_images_perceptually_duplicate(cand_p, passed_p):
                    is_dup = True
                    break
            if not is_dup:
                seen_hashes.add(cand_sha)
                distinct_passed.append({
                    "asset_id": cand_id,
                    "local_path": str(cand_p),
                    "absolute_path": str(cand_p),
                    "uri": cand.get("uri") or f"file:///{str(cand_p).replace(chr(92), '/')}",
                    "sha256": cand_sha,
                    "digest": cand_sha,
                    "score": cand_item_score,
                    "eligible": True,
                    "verdict": "pass",
                    "hard_failure": False,
                    "findings": cand_findings,
                })

    return distinct_passed, evaluated_items, None
