from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from video_factory.qc.models import QCStage, QCVerdict
from video_factory.qc.repository import QCReportRepository
from video_factory.job_contracts import (
    ExecutionBindingPreflightError,
    JobContext,
    TerminalJobError,
)
from video_factory.provider_runtime import (
    validate_polling_setting,
)


logger = logging.getLogger(__name__)

_LISTING_COPY_MARKERS = re.compile(
    r"\s*(?:[.!|•]\s*|[-–—]\s*)?(?:"
    r"giá\s*(?:(?:từ|chỉ)\s*)?:?\s*[\d][\d.,]*(?:\s*(?:₫|đ|vnd|vnđ))?"
    r"|[\d][\d.,]*\s*(?:₫|đ|vnd|vnđ)(?![A-Za-z])"
    r"|\d+\s+(?:nơi\s+bán|shop|người\s+bán)\b"
    r"|(?:mua\s+ngay|đặt\s+hàng|flash\s+sale|khuyến\s+mãi|giảm\s+giá|ưu\s+đãi|freeship|free\s+ship|deal\s+sốc|voucher|chỉ\s+hôm\s+nay)\b"
    r")",
    re.IGNORECASE,
)


def _creative_identity_text(value: object) -> str:
    """Remove obvious listing suffixes from creative identity while leaving source data untouched."""
    text = " ".join(str(value or "").split()).strip()
    match = _LISTING_COPY_MARKERS.search(text)
    if match:
        text = text[:match.start()].rstrip(" .-–—|•")
    return text




def _qc_context(data: dict[str, Any], workflow_id: str | None = None) -> dict[str, Any]:
    res: dict[str, Any] = {}
    auth = data.get("qc_authorization")
    if isinstance(auth, dict):
        auth_dict = dict(auth)
        if workflow_id:
            auth_dict["workflow_id"] = workflow_id
        res["qc_authorization"] = auth_dict
    max_cost = data.get("qc_max_cost_usd")
    if max_cost is not None:
        try:
            res["qc_max_cost_usd"] = float(max_cost)
        except (ValueError, TypeError):
            pass
    return res


def _paid_budget_kwargs(
    ctx: "JobContext",
    capability_id: str,
    kwargs: dict[str, Any],
    operation_input_digest: str | None = None,
) -> dict[str, Any]:
    """Bind every paid reservation to its workflow and optional operator grant."""
    result: dict[str, Any] = {
        "owner_user_id": ctx.owner_user_id,
        "scope_id": ctx.workflow_id,
        "capability_id": capability_id,
        "operation_input_digest": operation_input_digest or ctx.idempotency_key or ctx.job_id,
        "attempt_id": f"{ctx.workflow_id}:{capability_id.rsplit('.', 1)[-1]}:{ctx.job_id}",
    }
    authorization = kwargs.get("qc_authorization")
    if not isinstance(authorization, dict):
        return result

    authorization_id = str(
        authorization.get("authorization_id") or authorization.get("run_id") or ""
    ).strip()
    if authorization_id:
        result["authorization_id"] = authorization_id
        result["external_reference"] = authorization_id

    campaign_id = str(authorization.get("campaign_id") or "product-video-default").strip()
    if campaign_id:
        result["campaign_id"] = campaign_id

    def _usd_to_cents(value: Any) -> int | None:
        try:
            cents = int(round(float(value) * 100))
        except (TypeError, ValueError):
            return None
        return cents if cents > 0 else None

    campaign_max = _usd_to_cents(authorization.get("campaign_max_cost_usd"))
    if campaign_max is not None:
        result["campaign_max"] = campaign_max
    scope_max = _usd_to_cents(
        authorization.get("workflow_max_cost_usd", authorization.get("max_cost_usd"))
    )
    if scope_max is not None:
        result["scope_max"] = scope_max
    return result


def _paid_render_send_count(connection: Any, workflow_id: str) -> int:
    """Count non-released render reservations, including unresolved sent outcomes."""
    row = connection.execute(
        "SELECT COUNT(*) FROM budget_reservation "
        "WHERE scope_id=? AND capability_id='product_video.render' AND state IN ('reserved','committed')",
        (workflow_id,),
    ).fetchone()
    return int(row[0] if row else 0)


def _is_qc_pass(qc_res: Any) -> bool:
    if not isinstance(qc_res, dict):
        return False
    if qc_res.get("status") != "completed":
        return False
    rep = qc_res.get("report")
    verdict = None
    score = None
    if rep is not None:
        if isinstance(rep, dict):
            verdict = rep.get("verdict")
            score = rep.get("score")
        elif hasattr(rep, "verdict"):
            verdict = getattr(rep, "verdict")
            score = getattr(rep, "score", None)
    if verdict is None:
        verdict = qc_res.get("verdict")
    if score is None:
        score = qc_res.get("score")
    stage = None
    if isinstance(rep, dict):
        stage = rep.get("stage")
    elif rep is not None:
        stage = getattr(rep, "stage", None)
    if stage is None:
        stage = qc_res.get("stage")
    stage = stage.value if hasattr(stage, "value") else str(stage or "")
    is_source_image_qc = stage.upper() == "IMAGE"
    has_bound_final_review = (
        stage.upper() == "VIDEO"
        and isinstance(rep, dict)
        and (rep.get("findings") or {}).get("content_evidence_source") == "creative_final_review"
    ) or (
        stage.upper() == "VIDEO"
        and hasattr(rep, "findings")
        and (getattr(rep, "findings", {}) or {}).get("content_evidence_source") == "creative_final_review"
    )
    is_technical_only_final = (
        stage.upper() == "VIDEO"
        and isinstance(rep, dict)
        and (rep.get("findings") or {}).get("technical_only_final") is True
        and (rep.get("findings") or {}).get("technical_specs", {}).get("technical_pass") is True
    ) or (
        stage.upper() == "VIDEO"
        and hasattr(rep, "findings")
        and (getattr(rep, "findings", {}) or {}).get("technical_only_final") is True
        and ((getattr(rep, "findings", {}) or {}).get("technical_specs", {}) or {}).get("technical_pass") is True
    )
    if verdict is None or (score is None and not (is_source_image_qc or is_technical_only_final)):
        return False
    if is_source_image_qc:
        score_val = 0.0
    else:
        try:
            score_val = float(score)
        except (TypeError, ValueError):
            return False
    hard_failure = False
    if isinstance(rep, dict):
        hard_failure = bool(rep.get("hard_failure"))
    elif hasattr(rep, "hard_failure"):
        hard_failure = bool(getattr(rep, "hard_failure"))
    v_str = verdict.value if hasattr(verdict, "value") else str(verdict)
    return v_str.upper() == "PASS" and (is_source_image_qc or has_bound_final_review or
        is_technical_only_final or score_val >= 80) and not hard_failure


def _qc_report_payload(qc_res: Any) -> dict[str, Any]:
    if not isinstance(qc_res, dict):
        return {}
    rep = qc_res.get("report")
    if isinstance(rep, dict):
        return rep
    if hasattr(rep, "verdict"):
        v = rep.verdict
        if hasattr(v, "value"):
            v = v.value
        stage = getattr(rep, "stage", None)
        if hasattr(stage, "value"):
            stage = stage.value
        return {
            "report_id": getattr(rep, "report_id", qc_res.get("report_id")),
            "workflow_id": getattr(rep, "workflow_id", None),
            "stage": stage,
            "verdict": v,
            "score": getattr(rep, "score", 0),
            "artifact_id": getattr(rep, "artifact_id", None),
            "artifact_version": getattr(rep, "artifact_version", None),
            "artifact_digest": getattr(rep, "artifact_digest", None),
            "findings": getattr(rep, "findings", {}),
            "hard_failure": getattr(rep, "hard_failure", False),
        }
    return qc_res


def _video_factory_brief_config(
    base_config: dict[str, Any],
    brief: Any,
) -> dict[str, Any]:
    """Project a grounded V2 brief onto Video Factory's persisted brief contract."""
    audience = getattr(brief, "audience")
    reasons = getattr(brief, "reason_to_believe")
    return {
        **base_config,
        "objective": brief.creative_angle,
        "target_audience": f"{audience.segment} — {audience.situation}",
        "core_message": brief.promise,
        "tone": brief.tone,
        "pace": "Nhanh gọn, đời thường",
        "cta": brief.cta,
        "content_blocks": [brief.hook, brief.problem, brief.promise],
        "verified_selling_points": [
            {
                "claim": reason.text,
                "status": "verified",
                "evidence_refs": list(reason.evidence_refs),
            }
            for reason in reasons
        ],
        "restrictions": list(brief.restrictions),
        "target_duration_seconds": brief.target_duration_seconds,
    }


def create_configured_creative_generator(
    *,
    record_lookup: Callable[[str], Mapping[str, Any] | None] | None = None,
    connection: Any | None = None,
    creative_mode: str | None = None,
) -> Any:
    """Build the single-model grounded creative boundary from user configuration.

    Defaults select the already-supported Vertex route for the personal MVP;
    provider and model remain overridable in ``config.yaml`` at
    ``product_video.creative``. Credentials are deliberately left to Hermes'
    provider resolver rather than duplicated in this workflow package.
    """
    from video_factory.creative.generator import SingleModelCreativeGenerator
    from video_factory.creative.model_adapter import HermesStructuredCreativeModel
    from video_factory.creative.source_backed import SourceEvidenceVerifier
    from video_factory.creative.trusted_evidence import validated_record_lookup
    from video_factory.integrations.nine_router import configured_creative_model

    if record_lookup is None and connection is not None:
        from video_factory.creative.trusted_evidence import validated_workflow_record_lookup
        lookup = validated_workflow_record_lookup(connection)
    else:
        record_dir = os.environ.get("VF_TRUSTED_EVIDENCE_DIR", "").strip()
        lookup = record_lookup or (validated_record_lookup(Path(record_dir)) if record_dir else None)
    source_verifier = SourceEvidenceVerifier(record_lookup=lookup)
    nine_router_model = configured_creative_model()
    if nine_router_model is not None:
        return _creative_mode(SingleModelCreativeGenerator(nine_router_model, source_verifier=source_verifier), creative_mode)

    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly()
    except Exception:
        config = {}
    if not config:
        try:
            import yaml
            cfg_path = Path("config.yaml")
            if cfg_path.is_file():
                config = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            config = {}
    product_video = config.get("product_video") if isinstance(config, dict) else None
    creative = product_video.get("creative") if isinstance(product_video, dict) else None
    provider = str(creative.get("provider") or "vertex") if isinstance(creative, dict) else "vertex"
    model = str(creative.get("model") or "google/gemini-2.5-flash") if isinstance(creative, dict) else "google/gemini-2.5-flash"
    timeout = 120.0
    if isinstance(creative, dict) and creative.get("timeout_seconds") is not None:
        timeout = validate_polling_setting("creative.timeout_seconds", creative["timeout_seconds"], timeout)
    return _creative_mode(SingleModelCreativeGenerator(
        HermesStructuredCreativeModel(
            provider=provider,
            model=model,
            timeout=timeout,
        ),
        source_verifier=source_verifier,
    ), creative_mode)


def _creative_mode(generator, mode=None):
    mode = mode or os.environ.get('VF_CREATIVE_MODE', 'legacy')
    if mode == 'legacy':
        return generator
    if mode != 'four_agent':
        raise ValueError('creative_mode must be legacy or four_agent')
    from video_factory.creative.agents.contracts import BackendProfile
    from video_factory.creative.agents.service import AgenticCreativeGenerator
    path = os.environ.get('VF_CREATIVE_RUN_CONFIG', '')
    if not path:
        raise ValueError('four_agent requires explicit VF_CREATIVE_RUN_CONFIG')
    config = json.loads(Path(path).read_text(encoding='utf8'))
    return AgenticCreativeGenerator(generator.model, profile=BackendProfile.from_dict(config['profile']),
        category=config['category'], visible_details=config['visible_details'], source_verifier=generator.source_verifier)


def _compute_digest(obj: Any) -> str:
    """Compute SHA-256 digest of JSON-serializable object."""
    payload = json.dumps(obj, sort_keys=True).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def verify_job_digests(job_name: str, inputs: dict[str, Any], expected_digests: dict[str, str]) -> bool:
    """Verify that job inputs match expected digests."""
    for key, expected in expected_digests.items():
        if key not in inputs:
            return False
        if _compute_digest(inputs[key]) != expected:
            return False
    return True

def _resolve_durable_resume_job_id(params: dict[str, Any], aggregate: dict[str, Any], durable_job_key: str, forbidden_aliases: tuple[str, ...] = ()) -> str:
    """Return the only identity eligible for a no-resubmit resume."""
    from video_factory.job_contracts import TerminalJobError
    noncanonical_aliases = [alias for alias in forbidden_aliases if str(params.pop(alias, "") or "").strip()]
    if noncanonical_aliases:
        raise TerminalJobError(f"Noncanonical resume identity field(s) {', '.join(noncanonical_aliases)} are not supported at the workflow boundary (noncanonical_resume_identity)", code="JOB_BINDING_MISMATCH")
    requested_job_id = str(params.get("active_job_id") or "").strip()
    durable_job_id = str(aggregate.get(durable_job_key) or "").strip()
    if requested_job_id and requested_job_id != durable_job_id:
        reason = "unbound_durable_identity" if not durable_job_id else "durable_identity_mismatch"
        raise TerminalJobError(f"Requested active_job_id '{requested_job_id}' does not match durable job identity '{durable_job_id}' ({reason})", code="JOB_BINDING_MISMATCH")
    if durable_job_id:
        params["active_job_id"] = durable_job_id
    else:
        params.pop("active_job_id", None)
    return durable_job_id


def _brand_owned_product_url(brand: str, url: str) -> bool:
    """Recognize an HTTPS brand.com source when PI left its class unknown."""
    brand_domain = "".join(char for char in brand.casefold() if char.isascii() and char.isalnum())
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    return bool(brand_domain) and parsed.scheme == "https" and host == f"{brand_domain}.com"


def _locked_research_dossier(
    *,
    workflow_id: str,
    aggregate: dict[str, Any],
    locked_pack: dict[str, Any],
    connection: Any,
) -> dict[str, Any]:
    """Build the immutable creative input from locked PI data and Image QC."""
    from video_factory.creative.dossier import build_research_dossier

    resource_pack = dict(aggregate.get("resource_pack") or {})
    pack_raw = resource_pack.get("raw_result")
    if isinstance(pack_raw, dict):
        resource_pack = {**resource_pack, **pack_raw}
    locked_raw = locked_pack.get("raw_result")
    if isinstance(locked_raw, dict):
        resource_pack = {**resource_pack, **locked_raw}
    source_context = aggregate.get("source_context")
    if not isinstance(source_context, dict):
        source_context = {}
    identity = resource_pack.get("product_identity")
    if not isinstance(identity, dict):
        identity = {}
    locked_identity = resource_pack.get("locked_product_identity")
    if not isinstance(locked_identity, dict):
        locked_identity = {}
    resolved = resource_pack.get("resolved_values")
    if not isinstance(resolved, dict):
        resolved = {}
    brand = str(identity.get("brand") or resolved.get("brand") or source_context.get("brand") or "").strip()
    raw_model = str(identity.get("model") or resolved.get("model") or source_context.get("model") or "").strip()
    model = _creative_identity_text(raw_model)
    raw_display_name = str(
        resource_pack.get("product_name")
        or identity.get("name")
        or identity.get("display_name")
        or resolved.get("name")
        or f"{brand} {model}".strip()
    )
    display_name = _creative_identity_text(raw_display_name) or f"{brand} {model}".strip()
    if brand and model.casefold().startswith(brand.casefold() + " "):
        model = model[len(brand):].strip()
    if brand and display_name and not display_name.casefold().startswith(brand.casefold()):
        display_name = f"{brand} {display_name}".strip()
    resource_pack["product_identity"] = {
        "brand": brand,
        "model": model,
        "display_name": display_name,
        "variant": str(
            identity.get("variant")
            or resolved.get("variant")
            or source_context.get("variant")
            or ""
        ),
        "distinctive_features": list(
            identity.get("distinctive_features")
            or locked_identity.get("distinctive_features")
            or []
        ),
        "shape": str(identity.get("shape") or locked_identity.get("shape") or ""),
        "color": str(identity.get("color") or locked_identity.get("color") or ""),
        "materials": str(
            identity.get("materials")
            or identity.get("material")
            or locked_identity.get("materials")
            or locked_identity.get("material")
            or ""
        ),
        "logo_placement": str(
            identity.get("logo_placement") or locked_identity.get("logo_placement") or ""
        ),
        "silhouette": str(
            identity.get("silhouette") or locked_identity.get("silhouette") or ""
        ),
        "main_proportions": str(
            identity.get("main_proportions")
            or locked_identity.get("main_proportions")
            or ""
        ),
        "product_count": str(
            identity.get("product_count") or locked_identity.get("product_count") or ""
        ),
        "control_placement": str(
            identity.get("control_placement")
            or locked_identity.get("control_placement")
            or ""
        ),
        "port_count": str(
            identity.get("port_count") or locked_identity.get("port_count") or ""
        ),
        "port_placement": str(
            identity.get("port_placement") or locked_identity.get("port_placement") or ""
        ),
        "display_shape": str(
            identity.get("display_shape") or locked_identity.get("display_shape") or ""
        ),
    }
    identity_mode=identity.get('identity_mode') or resource_pack.get('identity_mode') or 'sku'
    if identity_mode=='source_product':
        resource_pack['product_identity'].update(identity_mode='source_product',
            canonical_product_id=identity.get('canonical_product_id') or resource_pack.get('canonical_product_id') or '')

    research_result = aggregate.get("research_result")
    research_report: dict[str, Any] = {}
    if isinstance(research_result, dict):
        raw_report = research_result.get("report")
        if isinstance(raw_report, str):
            try:
                parsed = json.loads(raw_report)
                if isinstance(parsed, dict):
                    research_report = parsed
            except json.JSONDecodeError:
                pass
    research_raw = research_result.get("raw_result") if isinstance(research_result, dict) else None
    if not isinstance(research_raw, dict):
        research_raw = research_report
    if identity_mode=='source_product':
        from video_factory.creative.contracts import canonical_digest
        actual_source_digest=canonical_digest(research_raw)
        if identity.get('source_snapshot_digest',actual_source_digest)!=actual_source_digest:
            raise ValueError('source_product source JSON changed since lock')
        resource_pack['product_identity']['source_snapshot_digest']=actual_source_digest
    source_results = resource_pack.get("source_results") or research_raw.get("source_results") or research_report.get("source_results") or []
    discovered = resource_pack.get("discovered_sources") or research_raw.get("discovered_sources") or research_report.get("discovered_sources") or []
    usable_urls = {
        str(url)
        for item in source_results
        if isinstance(item, dict)
        and (item.get("usable") is True or item.get("acquisition_status") == "usable")
        for url in (item.get("requested_url"), item.get("final_url"), item.get("canonical_url"), item.get("url"))
        if url
    }
    official_sources = sorted({
        str(item.get("url"))
        for item in discovered
        if isinstance(item, dict)
        and str(item.get("source_class") or "").lower() in {"official", "manufacturer", "brand_official"}
        and item.get("url")
        and str(item["url"]) in usable_urls
    } | {
        str(item.get("url"))
        for item in source_results
        if isinstance(item, dict)
        and str(item.get("source_class") or "").lower() in {"official", "manufacturer", "brand_official"}
        and item.get("url")
        and str(item["url"]) in usable_urls
    } | {
        str(item.get("url"))
        for item in source_results
        if isinstance(item, dict)
        and str(item.get("source_class") or "").lower() == "unknown"
        and item.get("url")
        and str(item["url"]) in usable_urls
        and _brand_owned_product_url(brand, str(item["url"]))
    })
    evidence_items = resource_pack.get("evidence") or research_raw.get("evidence") or research_raw.get("product_evidence") or []
    evidence_by_id = {
        str(item.get("evidence_id") or item.get("id")): item
        for item in evidence_items
        if isinstance(item, dict) and (item.get("evidence_id") or item.get("id"))
    }
    snapshot_id = str(resource_pack.get("snapshot_id") or "")
    product_id = str(resource_pack.get("canonical_product_id") or "")
    variant_id = str(resource_pack.get("variant_id") or "")
    claims = []
    seen_claims: set[tuple[str, tuple[str, ...]]] = set()
    for item in resource_pack.get("claims") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("claim") or "").strip()
        claim_id = str(item.get("claim_id") or item.get("id") or "").strip()
        status = str(item.get("support_status") or item.get("status") or "").lower()
        refs = item.get("evidence_refs") or item.get("source_evidence_ids") or []
        if not (text and claim_id and status in {"supported", "verified", "confirmed", "verified_spec", "source_claim", "source_backed"}):
            continue
        if not isinstance(refs, (list, tuple)) or not refs:
            continue
        normalized_refs = tuple(str(ref).strip() for ref in refs if str(ref).strip())
        if not normalized_refs or len(normalized_refs) != len(refs):
            continue
        if any(
            ref not in evidence_by_id
            or not snapshot_id
            or not product_id
            or str(evidence_by_id[ref].get("snapshot_id") or "") != snapshot_id
            or str(evidence_by_id[ref].get("canonical_product_id") or "") != product_id
            or (variant_id and str(evidence_by_id[ref].get("variant_id") or "") not in {"", variant_id})
            for ref in normalized_refs
        ):
            continue
        if status == "source_claim" and any(
            str(
                (evidence_by_id[ref].get("source") or {}).get("final_url")
                or (evidence_by_id[ref].get("source") or {}).get("canonical_url")
                or (evidence_by_id[ref].get("source") or {}).get("requested_url")
                or ""
            ) not in official_sources
            for ref in normalized_refs
        ):
            continue
        if any(
            str(item.get(field) or "") not in {"", expected}
            for field, expected in (
                ("snapshot_id", snapshot_id),
                ("canonical_product_id", product_id),
                ("variant_id", variant_id),
            )
        ):
            continue
        claim_key = (" ".join(text.casefold().split()), normalized_refs)
        if claim_key in seen_claims:
            continue
        seen_claims.add(claim_key)
        claim_data = {
            "claim_id": claim_id,
            "text": text,
            # A PI label alone cannot cross the independent source gate.
            "status": "supported",
            "evidence_refs": list(normalized_refs),
        }
        claims.append(claim_data)

    # Operator attachment is a separate durable decision. Never read a
    # SOURCE_BACKED label or a record payload from PI/Exchange as authority.
    trusted_urls: set[str] = set()
    receipts = aggregate.get("trusted_source_claims")
    if isinstance(receipts, list) and receipts:
        from video_factory.creative.trusted_evidence import locked_product_key, validated_record_lookup

        try:
            approved_key = locked_product_key(aggregate)
        except ValueError:
            approved_key = ""
        for receipt in receipts:
            if not isinstance(receipt, dict) or not approved_key:
                continue
            record_id = str(receipt.get("record_id") or "")
            record_dir = str(receipt.get("record_dir") or "")
            if not record_dir or not Path(record_dir).is_absolute():
                continue
            record = validated_record_lookup(Path(record_dir))(record_id)
            if record is None or record.get("canonical_product_key") != approved_key:
                continue
            claim_id = str(record["canonical_claim_id"])
            claim_text = str(record["claim_text"])
            if any(item["claim_id"] == claim_id and item["text"] != claim_text for item in claims):
                continue
            claims = [item for item in claims if item["claim_id"] != claim_id]
            claims.append({
                "claim_id": claim_id,
                "text": claim_text,
                "status": "SOURCE_BACKED",
                "evidence_refs": [str(record["source_url"])],
                "source_url": str(record["source_url"]),
                "evidence_locator": str(record["evidence_locator"]),
                "evidence_snippet": str(record["evidence_snippet"]),
                "evidence_record_id": record_id,
            })
            trusted_urls.add(str(record["source_url"]))
    if any(item.get("status") == "SOURCE_BACKED" for item in claims):
        claims = [item for item in claims if item.get("status") == "SOURCE_BACKED"]
    else:
        # PI/Exchange evidence labels are not independent source validation.
        # When no trusted source record is attached, build only neutral
        # identity and visual observations from the passing QC manifest.
        claims = []

    report = None
    exchange_lock = None
    if ((aggregate.get("creative_policy") or {}).get("qc_level") == "low"
            and _is_exchange_source_context(source_context)):
        from video_factory.qc.normalization import _media_manifest

        lock_digest = locked_pack.get("manifest_digest")
        approved = {image["asset_id"]: image.get("sha256") for image in source_context.get("images", [])
                    if image.get("asset_id") and not image.get("identity_conflict")}
        eligible_manifest = [
            item for item in _media_manifest(locked_pack)
            if item["eligible"] and item["sha256"] == approved.get(item["asset_id"])
        ]
        if locked_pack.get("status") != "locked" or not lock_digest or not eligible_manifest:
            raise ValueError("locked Exchange pack has no verified PI reference")
        exchange_lock = (f"pi-lock:{lock_digest}", eligible_manifest)
    else:
        report = QCReportRepository(connection).get_latest_report(workflow_id, QCStage.IMAGE)
        if report is None or report.verdict != QCVerdict.PASS:
            raise ValueError("a passing persisted Image QC report is required before creative generation")
        eligible_manifest = (report.findings or {}).get("eligible_reference_manifest") or []

    # Without a trusted feature claim, use only the locked product name and
    # details observed in images covered by this passing Image QC report.
    if not claims:
        primary_asset_ids = [
            str(item.get("asset_id")) for item in eligible_manifest
            if isinstance(item, dict) and item.get("eligible") is True and item.get("asset_id")
        ]
        fallback_ref = primary_asset_ids or ["source_image_qc_pass"]
        prod_name = display_name or f"{brand} {model}".strip()
        if prod_name:
            claims.append({
                "claim_id": "claim-sku-01",
                "text": f"Sản phẩm {prod_name}",
                "status": "supported",
                "evidence_refs": list(fallback_ref),
            })
        for idx, asset in enumerate(eligible_manifest, start=len(claims) + 1):
            if isinstance(asset, dict) and asset.get("eligible") is True:
                obs = asset.get("visual_observations") or asset.get("visible_details") or []
                for o_idx, o in enumerate(obs):
                    obs_text = str(o).strip()
                    if obs_text and len(obs_text) > 3:
                        claims.append({
                            "claim_id": f"claim-vis-{idx:02d}-{o_idx:02d}",
                            "text": obs_text,
                            "status": "supported",
                            "evidence_refs": [str(asset.get("asset_id"))],
                        })
    return build_research_dossier(
        workflow_id=workflow_id,
        resource_pack=resource_pack,
        qc_report=report,
        exchange_lock=exchange_lock,
        research_snapshot={
            "claims": claims,
            "source_refs": sorted(set(official_sources or usable_urls) | trusted_urls),
            # Candidate hot_reason is scouting copy, not claim-level PI evidence.
            # Passing it as a use case lets creative generation promote unverified
            # health or performance promises into the approved dossier.
            "use_cases": [],
        },
        source_snapshot=research_raw,
        reference_identity_mode='content' if aggregate.get('creative_policy',{}).get('creative_mode')=='four_agent' else 'legacy_path',
    ).to_dict()


def _confirm_vf_storyboard_write(
    result: Any,
    *,
    owner_user_id: str,
    project_id: str,
    scene_ids: tuple[str, ...],
) -> None:
    """Require the VF response to describe the matching canonical write."""
    if not isinstance(result, dict):
        raise ValueError("VF storyboard write returned no canonical result")
    raw = result
    if isinstance(result.get("payload"), str):
        try:
            raw = json.loads(result["payload"])
        except json.JSONDecodeError as exc:
            raise ValueError("VF storyboard write returned an invalid payload") from exc
    if not isinstance(raw, dict) or raw.get("ok") is not True:
        reason = raw.get("error") if isinstance(raw, dict) else None
        raise ValueError(f"VF storyboard write failed for {project_id}: {reason or 'unconfirmed result'}")
    project = raw.get("project")
    if (
        raw.get("project_id") != project_id
        or not isinstance(project, dict)
        or project.get("id") != project_id
        or project.get("owner_user_id") != owner_user_id
        or project.get("scene_plan_approval") != "approved"
    ):
        raise ValueError(f"VF storyboard write identity mismatch for {project_id}")
    storyboard = project.get("storyboard")
    if (
        not isinstance(storyboard, dict)
        or not storyboard.get("storyboard_id")
        or storyboard.get("storyboard_id") != raw.get("storyboard_id")
    ):
        raise ValueError(f"VF storyboard write was not persisted for {project_id}")
    scene_plan = project.get("scene_plan")
    if not isinstance(scene_plan, dict):
        raise ValueError(f"VF approved scene plan is missing for {project_id}")
    for label, frames in (
        ("scene plan", scene_plan.get("scenes")),
        ("storyboard", storyboard.get("frames")),
        ("response", raw.get("frames")),
    ):
        if (
            not isinstance(frames, list)
            or tuple(item.get("scene_id") for item in frames if isinstance(item, dict))
            != scene_ids
        ):
            raise ValueError(f"VF {label} scenes do not match approved plan for {project_id}")


def _recover_creative_brief(
    persisted: dict[str, Any],
    dossier: Any,
) -> Any | None:
    """Rebuild a V2 artifact after a crash following an already persisted brief.

    The recovery provenance makes clear that the original raw model response
    was not durably captured before the process failed; it must never be
    presented as a fresh model invocation.
    """
    from video_factory.creative.contracts import (
        AudienceSpec,
        CreativeBriefV2,
        GenerationProvenance,
        ReasonToBelieve,
    )

    target = str(persisted.get("target_audience") or "").strip()
    segment, separator, situation = target.partition("—")
    reasons = []
    for item in persisted.get("verified_selling_points") or []:
        if not isinstance(item, dict):
            continue
        refs = tuple(str(ref) for ref in item.get("evidence_refs") or [] if str(ref).strip())
        claim = str(item.get("claim") or "").strip()
        if claim and refs:
            reasons.append(ReasonToBelieve(text=claim, evidence_refs=refs))
    blocks = [str(item).strip() for item in persisted.get("content_blocks") or [] if str(item).strip()]
    if not (target and reasons and blocks):
        return None
    try:
        return CreativeBriefV2.create(
            audience=AudienceSpec(
                segment=segment.strip(),
                situation=(situation if separator else "Product use").strip(),
                desired_outcome=str(persisted.get("core_message") or "").strip(),
            ),
            consumer_insight=blocks[1] if len(blocks) > 1 else blocks[0],
            problem=blocks[1] if len(blocks) > 1 else blocks[0],
            creative_angle=str(persisted.get("objective") or "").strip(),
            hook=blocks[0],
            promise=str(persisted.get("core_message") or "").strip(),
            reason_to_believe=tuple(reasons),
            tone=str(persisted.get("tone") or "").strip(),
            story_archetype="problem_solution_demo",
            cta=str(persisted.get("cta") or "").strip(),
            restrictions=tuple(str(item) for item in persisted.get("restrictions") or []),
            target_duration_seconds=int(persisted.get("target_duration_seconds") or 20),
            dossier_digest=dossier.digest(),
            provenance=GenerationProvenance(
                prompt_version="creative-direction-v2",
                provider="recovered_video_factory",
                model="",
                input_digests=(dossier.digest(),),
                parse_error="Recovered after Video Factory persisted the brief before the budget commit completed.",
            ),
        )
    except (TypeError, ValueError):
        return None


def _require_configured_execution_binding(
    client: Any,
    stage_key: str,
    capability_id: str,
) -> str:
    """Fail closed when a configured client cannot prove its execution binding.

    This is deliberately a structural capability check rather than a concrete
    adapter check: every configured client that can submit paid work must make
    the same immutable-binding guarantee.
    """
    try:
        preflight = getattr(client, "require_execution_binding", None)
    except Exception as exc:
        raise ExecutionBindingPreflightError(capability_id, exc) from exc
    if not callable(preflight):
        raise TerminalJobError(
            f"BINDING_UNAVAILABLE: Configured video client is missing execution binding "
            f"preflight for capability '{capability_id}' (missing_preflight_contract)",
            code="BINDING_UNAVAILABLE",
        )
    try:
        wire = preflight(stage_key, capability_id)
    except TerminalJobError:
        raise
    except Exception as exc:
        raise ExecutionBindingPreflightError(capability_id, exc) from exc
    if not isinstance(wire, str) or not wire.strip():
        raise TerminalJobError(
            f"BINDING_UNAVAILABLE: Configured video client returned no execution wire "
            f"for capability '{capability_id}' (invalid_preflight_result)",
            code="BINDING_UNAVAILABLE",
        )
    if capability_id == "video.render" and wire != "mcp__video_factory__render":
        raise TerminalJobError(
            f"BINDING_UNAVAILABLE: Configured video client returned a noncanonical "
            f"execution wire for capability '{capability_id}' (noncanonical_wire)",
            code="BINDING_UNAVAILABLE",
        )
    return wire


def _has_exchange_local_images(
    source_context: dict[str, Any] | None,
    research_snapshot_id: str | None = None,
) -> bool:
    """Return True if Exchange source_context contains at least one image with a path."""
    if not _is_exchange_source_context(source_context):
        return False
    images = source_context.get("images")
    if not isinstance(images, list) or not images:
        return False
    distinct_paths: set[str] = set()
    for img in images:
        if isinstance(img, dict):
            p = img.get("absolute_path") or img.get("local_path") or img.get("path")
            if p:
                distinct_paths.add(str(p))
    return bool(distinct_paths)


def _is_exchange_source_context(source_context: dict[str, Any] | None) -> bool:
    """Return whether the coordinator explicitly marked this as an Exchange flow."""
    return isinstance(source_context, dict) and source_context.get("source") == "hermes_product_video_exchange"


def _normalize_feature_list(raw_features: Any) -> list[str]:
    features: list[str] = []
    if isinstance(raw_features, str):
        stripped = raw_features.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            import ast
            try:
                parsed = ast.literal_eval(stripped)
                if isinstance(parsed, (list, tuple)):
                    raw_features = parsed
            except Exception:
                pass
    if isinstance(raw_features, str):
        for chunk in raw_features.replace(";", "\n").split("\n"):
            item = chunk.strip(" -•\t\r")
            if item:
                features.append(item)
    elif isinstance(raw_features, (list, tuple)):
        for chunk in raw_features:
            item = str(chunk or "").strip()
            if item:
                features.append(item)
    return features


def _build_exchange_research_result(
    *,
    workflow_id: str,
    product_name: str,
    keywords: list[str],
    pdp_url: str | None,
    source_context: dict[str, Any],
) -> dict[str, Any]:
    """Build research result dictionary from local Exchange product data."""
    images = source_context.get("images") or []
    features = _normalize_feature_list(source_context.get("key_features"))
    result = {
        "owner": "product_intelligence",
        "schema": "product_research_result",
        "version": 1,
        "product_name": product_name,
        "keywords": keywords,
        "pdp_url": pdp_url,
        "snapshot_id": f"snapshot_{workflow_id}",
        "persistence_status": "saved",
        "source_context": source_context,
        "canonical_products": [{
            "brand": source_context.get("brand"),
            "model": source_context.get("model"),
            "variant": source_context.get("variant"),
        }],
        "media_assets": images,
        "report": json.dumps({
            "product_name": product_name,
            "brand": source_context.get("brand"),
            "model": source_context.get("model"),
            "variant": source_context.get("variant"),
            "key_features": features,
            "hot_reason": source_context.get("hot_reason", ""),
            "source_url": pdp_url,
        }),
    }
    if source_context.get('identity_mode') == 'source_product':
        result['raw_result'] = source_context['source_snapshot']
    return result


def _is_exchange_payload(payload: dict[str, Any] | None, kwargs: dict[str, Any] | None = None) -> bool:
    """Check if the payload or kwargs originate from an Exchange source context."""
    if isinstance(payload, dict):
        if _is_exchange_source_context(payload.get("source_context")):
            return True
        if payload.get("source") == "hermes_product_video_exchange":
            return True
        src = payload.get("source_context")
        if isinstance(src, dict) and src.get("source") == "hermes_product_video_exchange":
            return True
    if isinstance(kwargs, dict):
        src_kw = kwargs.get("source_context")
        if isinstance(src_kw, dict) and src_kw.get("source") == "hermes_product_video_exchange":
            return True
    return False


def _resolve_image_bytes_hash(item: Any) -> str | None:
    """Resolve SHA-256 hex digest from the physical file bytes on disk.

    In configured mode, only images with an existing readable file on disk
    whose SHA-256 is computed from real bytes are eligible for comparison.
    If the file does not exist, cannot be read, or is empty, returns None (fail-closed).
    """
    from pathlib import Path

    if hasattr(item, "to_dict") and callable(item.to_dict):
        item = item.to_dict()
    elif not isinstance(item, dict):
        if hasattr(item, "__dict__"):
            item = item.__dict__
        else:
            return None

    # Check for local file path
    for key in ("absolute_path", "local_path", "path", "file_path"):
        val = item.get(key)
        if val:
            try:
                candidate = Path(str(val)).expanduser()
                if candidate.is_file() and candidate.stat().st_size > 0:
                    return hashlib.sha256(candidate.read_bytes()).hexdigest().lower()
            except OSError:
                pass
            return None

    return None


def _assert_approved_images_in_resource_pack(
    *,
    source_context: dict[str, Any] | None,
    resource_pack: dict[str, Any] | None,
) -> None:
    """Require the configured PI pack to retain one source-bound usable asset.

    Matching is based strictly on content SHA-256 byte digest:
    - Same ID or URL but differing bytes must be rejected.
    - Copying files or changing asset IDs with identical bytes is accepted.
    - The selected approved image must match one pack image by content.
    - Missing files or insufficient data to prove match fails closed.
    """
    approved = list((source_context or {}).get("images") or [])
    if not approved:
        raise TerminalJobError(
            "APPROVED_IMAGE_MISMATCH: no approved Exchange image is present",
            code="APPROVED_IMAGE_MISMATCH",
        )

    # Compute sha256 for all approved images
    approved_hashes: list[str] = []
    for item in approved:
        h = _resolve_image_bytes_hash(item)
        if h:
            approved_hashes.append(h)

    # At least one distinct approved image must have readable content.
    distinct_approved_hashes = list(dict.fromkeys(approved_hashes))
    if not distinct_approved_hashes:
        raise TerminalJobError(
            "APPROVED_IMAGE_MISMATCH: no distinct approved Exchange image with readable content is present",
            code="APPROVED_IMAGE_MISMATCH",
        )

    from video_factory.qc.normalization import _media_manifest

    # Extract eligible pack images
    pack_manifest = _media_manifest(dict(resource_pack or {}))
    pack_images = [item for item in pack_manifest if item.get("eligible")]
    if not pack_images:
        raise TerminalJobError(
            "APPROVED_IMAGE_MISMATCH: resource pack has no eligible image",
            code="APPROVED_IMAGE_MISMATCH",
        )

    # Match each distinct approved hash to a distinct pack image with matching content hash
    matched_pack_indexes: set[int] = set()
    matched_approved_hashes: set[str] = set()

    for target_hash in distinct_approved_hashes:
        for idx, pack_item in enumerate(pack_images):
            if idx in matched_pack_indexes:
                continue
            pack_hash = _resolve_image_bytes_hash(pack_item)
            if pack_hash == target_hash:
                matched_pack_indexes.add(idx)
                matched_approved_hashes.add(target_hash)
                break

    if not matched_pack_indexes or not matched_approved_hashes:
        raise TerminalJobError(
            "APPROVED_IMAGE_MISMATCH: resource pack does not contain the source-approved image",
            code="APPROVED_IMAGE_MISMATCH",
        )


def _build_exchange_resource_pack(
    *,
    workflow_id: str,
    research_result: dict[str, Any],
    kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build resource pack dictionary from local Exchange product data."""
    source_ctx = kwargs.get("source_context") if isinstance(kwargs, dict) and kwargs.get("source_context") else {}
    if not source_ctx and isinstance(research_result, dict):
        source_ctx = research_result.get("source_context") or {}
    raw_images = list(source_ctx.get("images") or research_result.get("media_assets") or [])
    if not raw_images:
        raise TerminalJobError(
            "Exchange input cannot build a resource pack without a source image",
            code="EXCHANGE_INPUT_REVIEW_REQUIRED",
        )
    assets = []
    for img in raw_images:
        if isinstance(img, dict):
            asset_copy = dict(img)
            p = asset_copy.get("absolute_path") or asset_copy.get("local_path") or asset_copy.get("path")
            asset_copy["local_path"] = p
            assets.append(asset_copy)
    pack = {
        "resource_pack_id": f"rp_{workflow_id}",
        "version": 1,
        "brand": source_ctx.get("brand"),
        "model": source_ctx.get("model"),
        "variant": source_ctx.get("variant"),
        "assets": assets,
        "product_references": assets,
        "media_assets": assets,
        "source_context": source_ctx,
    }
    if source_ctx.get('identity_mode') == 'source_product':
        pack.update(identity_mode='source_product', canonical_product_id=source_ctx['canonical_product_key'],
            product_identity={'identity_mode':'source_product', 'brand':source_ctx.get('brand') or '',
                'model':source_ctx.get('model') or '', 'name':source_ctx['name'],
                'category':source_ctx.get('category'), 'canonical_product_id':source_ctx['canonical_product_key'],
                'source_snapshot_digest':source_ctx['source_snapshot_digest']})
    return pack

def _lock_exchange_resource_pack(
    *,
    pack_dict: dict[str, Any],
    owner_user_id: str,
    workflow_id: str,
) -> dict[str, Any]:
    """Lock an Exchange-backed Resource Pack locally without calling external MCP."""
    from datetime import datetime, timezone
    from video_factory.qc.normalization import resource_pack_fingerprint

    effective_owner = str(owner_user_id or "local-owner").strip()
    pack_id = str(pack_dict.get("resource_pack_id") or pack_dict.get("id") or f"rp_exchange_{workflow_id}")
    lock_id = f"lock_{pack_id}"
    manifest_digest = "sha256:" + resource_pack_fingerprint(pack_dict)
    assets = pack_dict.get("assets") or pack_dict.get("product_references") or []
    locked: dict[str, Any] = {
        **pack_dict,
        "owner": "product_intelligence",
        "schema": "product_resource_pack_locked",
        "version": int(pack_dict.get("version") or 1),
        "lock_id": lock_id,
        "resource_pack_id": pack_id,
        "owner_user_id": effective_owner,
        "manifest_digest": manifest_digest,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "locked",
        "pack_status": "locked",
        "assets": assets,
    }
    locked["raw_result"] = dict(locked)
    return locked


def _save_exchange_image_qc_report(
    *,
    connection: Any = None,
    workflow_id: str = "",
    aggregate: dict[str, Any] | None = None,
    resource_pack: dict[str, Any] | None = None,
    provider_mode: str = "configured",
) -> Any:
    """Deprecated: Auto Exchange admission cannot infer or create an Image QC PASS report.

    All candidate images must undergo multimodal visual QC via ProductVideoQCService.
    """
    raise TerminalJobError(
        "Exchange admission cannot create an Image QC PASS report: visual QC required",
        code="EXCHANGE_INPUT_REVIEW_REQUIRED",
    )
