"""Shared fingerprints for Product Video QC cache and approval guards."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from video_factory.qc.models import NormalizedProduct, QCStage


IMAGE_POLICY_VERSION = "product-video-image-policy-5"
VIDEO_POLICY_VERSION = "product-video-video-policy-3"
KEYFRAME_POLICY_VERSION = "product-video-keyframe-policy-5"
IMAGE_RUBRIC_VERSION = "5.0"
VIDEO_RUBRIC_VERSION = "2.1"
KEYFRAME_RUBRIC_VERSION = "3.2"


def minimum_pass_score(stage: QCStage) -> int:
    """Scores rank images; evidence-based verdicts govern source and keyframe QC."""
    if stage in {QCStage.IMAGE, QCStage.KEYFRAME}:
        return 0
    return 80


def qc_fingerprint(
    workflow_id: str,
    stage: QCStage,
    artifact_digest: str,
    product: NormalizedProduct,
    *,
    evaluator_name: str,
    evaluator_version: str,
    model_identifier: str,
    storyboard: dict[str, Any] | None = None,
    video_sha256: str = "",
    reference_digests: list[str] | None = None,
    attempt_id: str = "",
    scene_provider_kinds: list[str] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "workflow_id": workflow_id,
        "stage": stage.value,
        "artifact_digest": artifact_digest,
        "identity": {
            "brand": product.brand,
            "model": product.model,
            "variant": product.variant,
            "display_name": product.display_name,
        },
        "verified_claims": list(product.verified_claims),
        "evaluator": {
            "name": evaluator_name,
            "version": evaluator_version,
            "model": model_identifier,
        },
        "rubric_version": (
            IMAGE_RUBRIC_VERSION
            if stage == QCStage.IMAGE
            else KEYFRAME_RUBRIC_VERSION
            if stage == QCStage.KEYFRAME
            else VIDEO_RUBRIC_VERSION
        ),
        "policy_version": (
            IMAGE_POLICY_VERSION
            if stage == QCStage.IMAGE
            else KEYFRAME_POLICY_VERSION
            if stage == QCStage.KEYFRAME
            else VIDEO_POLICY_VERSION
        ),
        "reference_digests": sorted(reference_digests or []),
        # A configured retry is a new paid operation.  Its operator-supplied
        # authorization run id makes the claim and report immutable per attempt.
        "attempt_id": attempt_id,
    }
    if stage == QCStage.IMAGE:
        payload["source_identity"] = {
            key: product.source_context.get(key, "")
            for key in ("identity_mode", "canonical_product_key", "source_snapshot_digest")
        }
    if stage == QCStage.VIDEO:
        payload["storyboard"] = storyboard or {}
        payload["video_sha256"] = video_sha256
        payload["scene_provider_kinds"] = scene_provider_kinds or []
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
