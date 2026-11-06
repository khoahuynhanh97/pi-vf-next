"""Shared classification of terminal product failures and pipeline blockers."""

from __future__ import annotations

from enum import Enum
import re


class FailureCategory(str, Enum):
    PRODUCT_REJECTED = "PRODUCT_REJECTED"
    QC_INFRA_PAUSED = "QC_INFRA_PAUSED"
    CONTRACT_BROKEN = "CONTRACT_BROKEN"


_PRODUCT_REJECTED = {
    "PRODUCT_INPUT_INVALID",
    "PRODUCT_IDENTITY_REQUIRED",
    "PRODUCT_IMAGE_QC_REJECTED",
    "EXCHANGE_IMAGE_QC_FAILED",
    "IMAGE_QC_RETRIES_EXHAUSTED",
    "IMAGE_QC_NON_RECOVERABLE_FAIL",
    "KEYFRAME_QC_RETRIES_EXHAUSTED",
    "VIDEO_QC_RETRIES_EXHAUSTED",
    "VIDEO_QC_UNTARGETED_FAIL",
    "IMAGE_QC_TERMINAL_FAILURE",
    "INSUFFICIENT_CANDIDATES",
    "INSUFFICIENT_GROUNDING",
    "PREPRODUCTION_FAIL",
    "KEYFRAME_CONTENT_FAIL",
    "STORYBOARD_CONTENT_FAIL",
    "CLIP_CONTENT_FAIL",
    "FINAL_CONTENT_FAIL",
    "EXCHANGE_INPUT_REVIEW_REQUIRED",
    "INVALID_ADMISSION",
    "INPUT_REVIEW_REQUIRED",
    "CATALOG_IDENTITY_NOT_VALID",
    "CATALOG_NOT_READY_FOR_IMAGE_QC",
    "NOT_LISTED_IN_CATALOG_V2",
    "NOT_FOUND_IN_MANIFEST",
}

_QC_INFRA_PAUSED = {
    'PREPRODUCTION_UNKNOWN','KEYFRAME_CONTENT_UNKNOWN','STORYBOARD_CONTENT_UNKNOWN',
    'CLIP_CONTENT_UNKNOWN','FINAL_CONTENT_UNKNOWN','PI_ACQUISITION_UNCERTAIN',
    'CREATIVE_REPAIR_BLOCKED',
    # Unqualified QC failures do not prove a semantic product rejection. They
    # pause the checkout until the caller supplies a report-backed category.
    "IMAGE_QC_FAILED",
    "KEYFRAME_QC_FAILED",
    "VIDEO_QC_FAILED",
    "QC_RETRY_EXHAUSTED",
    "QC_QUOTA_EXHAUSTED",
    "QC_OUTCOME_UNCERTAIN",
    "TECHNICAL_UNKNOWN",
    "QC_PROVIDER_UNAVAILABLE",
    "QC_RESPONSE_INVALID",
    "QC_SCHEMA_INVALID",
    "MISSING_PROVIDER_CONFIG",
    "NINE_ROUTER_CONFIGURATION_ERROR",
    "INVALID_QC_PROVIDER_CONFIG",
    "QC_ROUTE_CONFIG_INVALID",
    "ROUTE_PREFLIGHT_FAILED",
    # These indicate the evaluator/retry budget could not produce a valid
    # semantic verdict. They are not evidence that the product itself failed.
    "IMAGE_QC_ERROR",
    "KEYFRAME_QC_ERROR",
    "VIDEO_QC_ERROR",
    "RATE_LIMITED",
    "HTTP_429",
    "HTTP_500",
    "HTTP_502",
    "HTTP_503",
    "HTTP_504",
    "RESOURCE_EXHAUSTED",
    "PROVIDER_TIMEOUT",
    "PROVIDER_NETWORK_ERROR",
    "PROVIDER_CAPACITY_WAIT",
    "ROUTE_NOT_VERIFIED",
}

_CONTRACT_BROKEN = {
    "JOB_BINDING_MISMATCH",
    # A stage was dispatched without its required upstream artifact or state.
    # Product input validation has dedicated PRODUCT_* codes; these generic
    # handler errors indicate a broken workflow contract and must fail-stop.
    "MISSING_INPUT_REF",
    "PREREQUISITE_FAILED",
    "SCENE_PLAN_DIGEST_MISMATCH",
    "INVALID_SCENE_PLAN_DIGEST",
    "ARTIFACT_DIGEST_MISMATCH",
    "WORKFLOW_STATE_MISMATCH",
    "INVALID_WORKFLOW_TRANSITION",
    "WORKFLOWTRANSITIONERROR",
    "QC_INPUT_UNVERIFIABLE",
}


def classify_failure(code: str | None) -> FailureCategory | None:
    """Classify only explicit, known failure codes; unknown codes are never product skips."""
    normalized = str(code or "").strip().upper().replace("-", "_")
    # Handlers sometimes wrap a typed error as ``RuntimeError: CODE: detail``.
    # Match explicit code tokens in that form without classifying by vague
    # words like "fail", "timeout" or "invalid" found in provider prose.
    for category, codes in (
        (FailureCategory.PRODUCT_REJECTED, _PRODUCT_REJECTED),
        (FailureCategory.QC_INFRA_PAUSED, _QC_INFRA_PAUSED),
        (FailureCategory.CONTRACT_BROKEN, _CONTRACT_BROKEN),
    ):
        if any(
            re.search(rf"(?<![A-Z0-9_]){re.escape(known_code)}(?![A-Z0-9_])", normalized)
            for known_code in codes
        ):
            return category
    if normalized.startswith("WORKFLOWTRANSITIONERROR:") or "CANNOT TRANSITION FROM" in normalized:
        return FailureCategory.CONTRACT_BROKEN
    if normalized.startswith(("HTTP_429", "HTTP_500", "HTTP_502", "HTTP_503", "HTTP_504")):
        return FailureCategory.QC_INFRA_PAUSED
    if normalized.startswith(("RESOURCE_EXHAUSTED", "RATE_LIMITED", "PROVIDER_TIMEOUT", "PROVIDER_NETWORK_ERROR")):
        return FailureCategory.QC_INFRA_PAUSED
    if normalized.startswith(("QC_OUTCOME_UNCERTAIN", "TECHNICAL_UNKNOWN")):
        return FailureCategory.QC_INFRA_PAUSED
    if ("DIGEST" in normalized and "MISMATCH" in normalized) or (
        normalized.startswith("INVALID_") and "DIGEST" in normalized
    ):
        return FailureCategory.CONTRACT_BROKEN
    return None


def is_exhausted_legacy_creative_response(message: str | None, attempts: int, maximum: int) -> bool:
    """Match only the old Director parser error after its durable outbox is exhausted."""
    return (int(attempts) >= int(maximum) and int(maximum) > 0 and
            "invalid Director prompt JSON:" in str(message or ""))


def has_consumed_semantic_image_qc_evidence(result: object) -> bool:
    """Whether a failed Image QC result proves SKU-specific semantic rejection."""
    if not isinstance(result, dict) or result.get("status") != "failed":
        return False
    if result.get("usage_status") != "consumed":
        return False
    items = result.get("evaluated_items")
    if not isinstance(items, list) or not items:
        return False

    for item in items:
        if not isinstance(item, dict) or str(item.get("verdict") or "").lower() not in {"pass", "fail"}:
            return False
        findings = item.get("findings")
        if findings is not None and not isinstance(findings, dict):
            return False
        diagnostics = findings if isinstance(findings, dict) else {}
        if any(bool(diagnostics.get(key)) for key in ("technical_unknown", "schema_invalid", "parse_error")):
            return False
    return True
