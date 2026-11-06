"""Image Quality Control (Vision QC) for Product Video Resource Pack."""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from video_factory.qc.evaluator import (
    QCResponseParseError,
    QCTechnicalError,
    MultimodalEvaluator,
    is_eligible_image_result,
    is_technical_error,
    validate_qc_response,
)
from video_factory.qc.fingerprint import (
    IMAGE_POLICY_VERSION,
    IMAGE_RUBRIC_VERSION,
    qc_fingerprint,
)
from video_factory.qc.models import (
    ImageQCItem,
    NormalizedProduct,
    QCReport,
    QCStage,
    QCVerdict,
)
from video_factory.qc.normalization import (
    _is_decodable_image,
    _media_manifest,
    _unwrap_resource_pack,
    resource_pack_fingerprint,
)
from video_factory.qc.repository import QCReportRepository
from video_factory.qc.rubric import (
    IMAGE_QC_SYSTEM_PROMPT,
    build_image_qc_prompt,
)

logger = logging.getLogger(__name__)


def compute_file_sha256(path: str | Path) -> str:
    p = Path(path)
    if not p.is_file():
        return ""
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_artifact_digest(artifact: dict[str, Any]) -> str:
    """Compute the canonical envelope-independent resource-pack fingerprint."""
    return resource_pack_fingerprint(artifact)


def _file_url_to_path(url_or_path: str | Path) -> Path:
    s = str(url_or_path).strip()
    if s.startswith("file://"):
        from urllib.parse import unquote, urlparse
        parsed = urlparse(s)
        path_part = unquote(parsed.path)
        netloc_part = unquote(parsed.netloc)
        combined = f"{netloc_part}{path_part}" if netloc_part else path_part
        if len(combined) >= 3 and combined[0] in ("/", "\\") and combined[2] == ":" and combined[1].isalpha():
            combined = combined[1:]
        return Path(combined)
    return Path(s)


def evaluate_image_item(
    *,
    asset_id: str,
    local_path: str | Path,
    product: NormalizedProduct,
    evaluator: MultimodalEvaluator,
    uri: str = "",
    identity_reference: ImageQCItem | None = None,
) -> ImageQCItem:
    """Evaluate a single image candidate using core Image QC rubric and evaluator."""
    if evaluator is None:
        from video_factory.integrations.nine_router import configured_qc_evaluator
        evaluator = configured_qc_evaluator()
    p = Path(local_path)
    if not uri:
        uri = f"file:///{str(p).replace(chr(92), '/')}"

    if not p.is_file():
        return ImageQCItem(
            asset_id=asset_id,
            uri=uri,
            digest="",
            score=0,
            identity_score=0,
            clarity_score=0,
            usability_score=0,
            verdict=QCVerdict.UNKNOWN,
            eligible=False,
            hard_failure=False,
            reason=f"Image file missing on disk: {local_path}",
            findings={"technical_unknown": True, "usage_status": "not_sent", "retry_allowed": False},
        )

    digest = compute_file_sha256(p)

    if not _is_decodable_image(p):
        return ImageQCItem(
            asset_id=asset_id,
            uri=uri,
            digest=digest,
            score=0,
            identity_score=0,
            clarity_score=0,
            usability_score=0,
            verdict=QCVerdict.FAIL,
            eligible=False,
            hard_failure=True,
            reason="Image bytes are not decodable",
        )

    prompt = build_image_qc_prompt(
        brand=product.brand,
        model=product.model,
        variant=product.variant,
        display_name=product.display_name,
        key_features=product.verified_claims,
        identity_mode=str(product.source_context.get("identity_mode") or "sku"),
    )

    raw_response = None
    try:
        raw_response = evaluator.evaluate_image(
            image_path=str(p),
            prompt=prompt,
            system_instruction=IMAGE_QC_SYSTEM_PROMPT,
        )
        res = validate_qc_response(raw_response, QCStage.IMAGE)
        score = int(res.get("total_score", 0))
        identity_score = int(res.get("identity_score", 0))
        clarity_score = int(res.get("clarity_score", 0))
        usability_score = int(res.get("usability_score", 0))
        hard_failure = bool(res.get("hard_failure", False))
        raw_verdict = str(res.get("verdict", "fail")).lower()
        verdict = (
            QCVerdict.PASS
            if raw_verdict == "pass"
            else (QCVerdict.UNKNOWN if raw_verdict == "unknown" else QCVerdict.FAIL)
        )
        reason = str(res.get("reason", ""))

        eligible = is_eligible_image_result(res)
        if hard_failure:
            eligible = False
            verdict = QCVerdict.FAIL

        return ImageQCItem(
            asset_id=asset_id,
            uri=uri,
            digest=digest,
            score=score,
            identity_score=identity_score,
            clarity_score=clarity_score,
            usability_score=usability_score,
            verdict=verdict,
            eligible=eligible,
            hard_failure=hard_failure,
            reason=reason,
            findings=res,
        )
    except Exception as exc:
        logger.error("Failed to evaluate image %s (%s): %s", asset_id, local_path, exc)
        if isinstance(exc, QCResponseParseError):
            diagnostics = {
                "raw_response": exc.raw_response,
                "finish_reason": exc.finish_reason,
                "parse_error": exc.parse_error,
                "schema_invalid": True,
                "technical_unknown": False,
                "usage_status": "consumed",
                "reconcile_required": False,
                "retry_allowed": True,
            }
        else:
            technical = is_technical_error(exc)
            response_received = raw_response is not None or bool(getattr(exc, "response_received", False))
            diagnostics = {
                "technical_unknown": technical and not response_received,
                "usage_status": str(getattr(exc, "usage_status", "uncertain" if technical else "unknown")),
                "reconcile_required": bool(getattr(exc, "reconcile_required", technical and not response_received)),
                "retry_allowed": bool(getattr(exc, "retry_allowed", not technical or response_received)),
            }
            if response_received:
                diagnostics["schema_invalid"] = True
                diagnostics["usage_status"] = "consumed"
                diagnostics["raw_response"] = raw_response if isinstance(raw_response, (dict, list, str, int, float, bool)) else f"<invalid {type(raw_response).__name__} response>"
            receipt = getattr(exc, "route_receipt", None)
            if not isinstance(receipt, dict) and isinstance(raw_response, dict):
                receipt = raw_response.get("route_receipt")
            if isinstance(receipt, dict):
                diagnostics["route_receipt"] = receipt
        return ImageQCItem(
            asset_id=asset_id,
            uri=uri,
            digest=digest,
            score=0,
            identity_score=0,
            clarity_score=0,
            usability_score=0,
            verdict=QCVerdict.UNKNOWN,
            eligible=False,
            hard_failure=False,
            reason=f"Evaluation call failed: {exc}",
            findings=diagnostics,
        )


def run_image_qc(
    workflow_id: str,
    resource_pack: dict[str, Any],
    product: NormalizedProduct,
    evaluator: MultimodalEvaluator | None = None,
    repo: QCReportRepository | None = None,
    attempt_id: str = "",
    owner_user_id: str = "",
    project_id: str = "",
) -> tuple[QCReport, list[ImageQCItem]]:
    """Run multimodal vision QC on all candidate images in the resource pack."""
    raw_rp = _unwrap_resource_pack(resource_pack)
    if isinstance(product, dict):
        product = NormalizedProduct(
            brand=str(product.get("brand") or "").strip(),
            model=str(product.get("model") or "").strip(),
            variant=str(product.get("variant") or "").strip(),
            display_name=str(product.get("display_name") or product.get("name") or "").strip(),
            category=str(product.get("category") or "Thiết bị tiện ích"),
            verified_claims=list(product.get("verified_claims") or []),
            approved_media=list(product.get("approved_media") or []),
            source_context=dict(product.get("source_context") or {}),
        )
    elif product is None:
        product = NormalizedProduct(
            brand=str(raw_rp.get("brand") or "").strip(),
            model=str(raw_rp.get("model") or "").strip(),
            display_name=str(raw_rp.get("product_identity") or "").strip(),
        )
    owner_user_id = str(owner_user_id or raw_rp.get("owner_user_id") or "").strip()
    project_id = str(project_id or raw_rp.get("project_id") or "").strip()
    if not owner_user_id or not project_id:
        raise ValueError("owner_user_id and project_id are required for Image QC")

    artifact_id = str(raw_rp.get("resource_pack_id") or raw_rp.get("id") or "").strip()
    if not artifact_id:
        raise ValueError("resource_pack_id is required for Image QC")
    artifact_version = raw_rp.get("version")
    if (
        isinstance(artifact_version, bool)
        or not isinstance(artifact_version, int)
        or artifact_version < 1
    ):
        raise ValueError("resource pack version must be a positive integer for Image QC")
    artifact_digest = compute_artifact_digest(raw_rp)

    if evaluator is None:
        from video_factory.integrations.nine_router import configured_qc_evaluator
        evaluator = configured_qc_evaluator()
    provider_mode = "fake" if getattr(evaluator, "fake_mode", False) else "configured"
    evaluator_name = str(getattr(evaluator, "evaluator_name", "unknown"))
    evaluator_version = str(getattr(evaluator, "evaluator_version", "unknown"))
    model_identifier = str(getattr(evaluator, "model", ""))
    report_fingerprint = qc_fingerprint(
        workflow_id,
        QCStage.IMAGE,
        artifact_digest,
        product,
        evaluator_name=evaluator_name,
        evaluator_version=evaluator_version,
        model_identifier=model_identifier,
        attempt_id=attempt_id,
    )

    # 1. Check if an up-to-date valid QC report already exists for this exact artifact digest
    if repo is not None:
        cached_report = repo.get_latest_report(
            workflow_id,
            QCStage.IMAGE,
            expected_artifact_digest=artifact_digest,
            expected_fingerprint=report_fingerprint,
            owner_user_id=owner_user_id,
            project_id=project_id,
        )
        if cached_report is not None:
            # Invalidate cached report if any image file on disk has changed its byte SHA-256
            cache_valid = True
            for d in cached_report.findings.get("items", []):
                file_uri = d.get("uri") or ""
                p = _file_url_to_path(file_uri)
                if not p.is_file() or compute_file_sha256(p) != d.get("digest"):
                    cache_valid = False
                    break
            if cache_valid:
                items: list[ImageQCItem] = []
                for d in cached_report.findings.get("items", []):
                    items.append(
                        ImageQCItem(
                            asset_id=d["asset_id"],
                            uri=d["uri"],
                            digest=d["digest"],
                            score=d["score"],
                            identity_score=d["identity_score"],
                            clarity_score=d["clarity_score"],
                            usability_score=d["usability_score"],
                            verdict=QCVerdict(d["verdict"]),
                            eligible=d["eligible"],
                            hard_failure=d["hard_failure"],
                            reason=d["reason"],
                            findings=d.get("findings") or {},
                        )
                    )
                eligible_order = {
                    str(item.get("asset_id")): index
                    for index, item in enumerate(
                        cached_report.findings.get("eligible_reference_manifest", [])
                    )
                    if isinstance(item, dict)
                }
                eligible_items = [item for item in items if item.eligible]
                eligible_items.sort(key=lambda item: eligible_order.get(item.asset_id, 10_000))
                return cached_report, eligible_items
            cached_report = None

    # 2. Extract asset list
    assets = _media_manifest(raw_rp)

    items: list[ImageQCItem] = []
    ref_digests: list[str] = []
    seen_digests: dict[str, str] = {}

    for i, asset in enumerate(assets):
        asset_id = str(asset.get("asset_id") or f"asset_{i+1}")
        local_path = asset.get("local_path") or asset.get("path") or asset.get("absolute_path")
        uri = asset.get("uri") or f"file:///{str(local_path).replace(chr(92), '/')}"

        if not local_path or not Path(local_path).is_file():
            items.append(
                ImageQCItem(
                    asset_id=asset_id,
                    uri=uri,
                    digest="",
                    score=0,
                    identity_score=0,
                    clarity_score=0,
                    usability_score=0,
                    verdict=QCVerdict.FAIL,
                    eligible=False,
                    hard_failure=True,
                    reason=f"Image file missing on disk: {local_path}",
                )
            )
            continue

        digest = compute_file_sha256(local_path)
        ref_digests.append(digest)

        duplicate_of = seen_digests.get(digest)
        if duplicate_of is not None:
            items.append(
                ImageQCItem(
                    asset_id=asset_id,
                    uri=uri,
                    digest=digest,
                    score=0,
                    identity_score=0,
                    clarity_score=0,
                    usability_score=0,
                    verdict=QCVerdict.FAIL,
                    eligible=False,
                    hard_failure=False,
                    reason=f"Exact duplicate of {duplicate_of}; excluded from reference diversity",
                    findings={"duplicate_of": duplicate_of, "risk_flags": ["duplicate"]},
                )
            )
            continue
        seen_digests[digest] = asset_id

        item = evaluate_image_item(
            asset_id=asset_id,
            local_path=local_path,
            product=product,
            evaluator=evaluator,
            uri=uri,
            identity_reference=None,
        )
        items.append(item)
        if bool(item.findings.get("technical_unknown")) or bool(item.findings.get("reconcile_required")):
            # The request may have reached a paid provider. Stop this batch at
            # the first uncertain result; a later image must wait for ledger
            # reconciliation.
            break
        if bool(item.findings.get("schema_invalid")):
            # The provider response consumed this attempt but did not satisfy
            # the QC contract. Stop the current selection pass; any retry is a
            # new durable QC attempt rather than another immediate image call.
            break

    # 3. A single usable reference is enough to establish product identity.
    raw_eligible = sorted(
        (it for it in items if it.eligible),
        key=lambda item: (
            -item.score,
            -item.usability_score,
            -item.clarity_score,
            item.asset_id,
        ),
    )
    # Deduplicate eligible items by distinct content SHA-256 and perceptual similarity
    from video_factory.qc.visual_similarity import are_images_perceptually_duplicate

    distinct_eligible_items: list[ImageQCItem] = []
    seen_eligible_digests: set[str] = set()
    for it in raw_eligible:
        if not it.digest or it.digest in seen_eligible_digests:
            continue
        # Check perceptual similarity against already selected distinct items
        is_perceptual_dup = False
        it_p = _file_url_to_path(it.uri)
        if it_p and it_p.is_file():
            for accepted in distinct_eligible_items:
                acc_p = _file_url_to_path(accepted.uri)
                if acc_p and acc_p.is_file() and are_images_perceptually_duplicate(it_p, acc_p):
                    is_perceptual_dup = True
                    break
        if not is_perceptual_dup:
            seen_eligible_digests.add(it.digest)
            distinct_eligible_items.append(it)

    eligible_items = distinct_eligible_items
    has_hard_fail_any = any(it.hard_failure for it in items)
    has_unknown = any(it.verdict == QCVerdict.UNKNOWN for it in items)

    evaluator_unresolved = any(
        it.findings.get("schema_invalid") or it.findings.get("technical_unknown")
        or it.findings.get("reconcile_required") for it in items
    )
    if evaluator_unresolved:
        overall_verdict = QCVerdict.UNKNOWN
        overall_score = 0
        eligible_items = []
    elif len(eligible_items) >= 1:
        overall_verdict = QCVerdict.PASS
        overall_score = round(sum(it.score for it in eligible_items) / len(eligible_items))
    elif has_unknown and not has_hard_fail_any:
        overall_verdict = QCVerdict.UNKNOWN
        overall_score = round(sum(it.score for it in items) / len(items)) if items else 0
    else:
        overall_verdict = QCVerdict.FAIL
        overall_score = round(sum(it.score for it in items) / len(items)) if items else 0

    recovery_actions = []
    if len(eligible_items) < 1:
        recovery_actions.append("reacquire_references")
        if overall_verdict == QCVerdict.PASS:
            overall_verdict = QCVerdict.FAIL

    report_id = f"qc_img_{workflow_id}_{uuid.uuid4().hex[:8]}"
    created_at = datetime.now(timezone.utc).isoformat()
    findings = {
        "items": [it.to_dict() for it in items],
        "eligible_count": len(eligible_items),
        "total_count": len(items),
        "eligible_source_count": len(eligible_items),
        "source_count_total": len(items),
        "sources_different_content": len(eligible_items) >= 2,
        "recovery_actions": recovery_actions,
        "recovery_reason": (
            f"No usable eligible reference is available."
            if len(eligible_items) < 1
            else ""
        ),
        "eligible_reference_manifest": [
            {
                "asset_id": item.asset_id,
                "uri": item.uri,
                "sha256": item.digest,
                "eligible": True,
                "reference_rank": rank,
                "qc_score": item.score,
                "identity_score": item.identity_score,
                "clarity_score": item.clarity_score,
                "usability_score": item.usability_score,
                "view_angle": str(item.findings.get("view_angle") or ""),
                "shot_type": str(item.findings.get("shot_type") or ""),
                "visible_details": list(item.findings.get("visible_details") or []),
                "scene_suitability": list(item.findings.get("scene_suitability") or []),
                "risk_flags": list(item.findings.get("risk_flags") or []),
                "text_coverage_percent": int(item.findings.get("text_coverage_percent") or 0),
                "visual_observations": [
                    value
                    for value in (
                        str(item.findings.get("view_angle") or "").strip(),
                        str(item.findings.get("shot_type") or "").strip(),
                        *[str(value).strip() for value in item.findings.get("visible_details", [])],
                    )
                    if value
                ],
            }
            for rank, item in enumerate(eligible_items, start=1)
        ],
        "rejected_reference_manifest": [
            {
                "asset_id": item.asset_id,
                "uri": item.uri,
                "sha256": item.digest,
                "reason": item.reason,
                "hard_failure": item.hard_failure,
                "risk_flags": list(item.findings.get("risk_flags") or []),
            }
            for item in items
            if not item.eligible
        ],
        "product_evaluated": product.to_dict(),
        "automatic": True,
        "provider_mode": provider_mode,
        "model_identifier": model_identifier,
        "policy_version": IMAGE_POLICY_VERSION,
        "attempt_id": attempt_id,
    }

    report = QCReport(
        report_id=report_id,
        workflow_id=workflow_id,
        stage=QCStage.IMAGE,
        artifact_id=artifact_id,
        artifact_version=artifact_version,
        artifact_digest=artifact_digest,
        reference_digests=ref_digests,
        evaluator_name=evaluator_name,
        evaluator_version=evaluator_version,
        rubric_version=IMAGE_RUBRIC_VERSION,
        created_at=created_at,
        score=overall_score,
        verdict=overall_verdict,
        findings=findings,
        fingerprint=report_fingerprint,
        provider_mode=provider_mode,
        model_identifier=model_identifier,
        policy_version=IMAGE_POLICY_VERSION,
        owner_user_id=owner_user_id,
        project_id=project_id,
    )

    if repo is not None:
        repo.save_report(report)

    return report, eligible_items
