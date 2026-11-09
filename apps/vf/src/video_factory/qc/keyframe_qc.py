"""QC for generated storyboard keyframes before human Gate 4."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .evaluator import QCResponseParseError, MultimodalEvaluator, is_technical_error, validate_qc_response
from .fingerprint import (
    KEYFRAME_POLICY_VERSION,
    KEYFRAME_RUBRIC_VERSION,
    qc_fingerprint,
)
from .models import NormalizedProduct, QCReport, QCStage, QCVerdict
from .repository import QCReportRepository
from .rubric import KEYFRAME_QC_SYSTEM_PROMPT, build_keyframe_qc_prompt
from video_factory.creative.storyboard import storyboard_package_digest


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_sha256(path: Path) -> str | None:
    try:
        return _sha256(path)
    except OSError:
        return None


def _image_dimensions(path: Path) -> tuple[int, int] | None:
    """Return dimensions only for an image that Pillow can fully decode."""
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
        return (width, height) if width > 0 and height > 0 else None
    except Exception:
        return None


def _artifact_digest(package: dict[str, Any]) -> str:
    supplied = str(package.get("package_digest") or "")
    if package.get("schema_version") == "storyboard-package-v2":
        computed = storyboard_package_digest(package)
        if supplied and supplied != computed:
            raise ValueError("storyboard package digest mismatch")
        return computed
    if supplied:
        return supplied
    encoded = json.dumps(package, sort_keys=True, ensure_ascii=False, default=str).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _approved_script_scene(package: Mapping[str, Any], scene_id: str) -> Mapping[str, Any] | None:
    script = package.get("script")
    scenes = script.get("scenes") if isinstance(script, Mapping) else None
    if not isinstance(scenes, list):
        scenes = package.get("scenes")
    if not isinstance(scenes, list):
        return None
    return next(
        (
            scene
            for scene in scenes
            if isinstance(scene, Mapping)
            and str(scene.get("scene_id") or "") == scene_id
        ),
        None,
    )


def run_keyframe_qc(
    *,
    workflow_id: str,
    package: dict[str, Any],
    product: NormalizedProduct,
    evaluator: MultimodalEvaluator | Any | None = None,
    repo: QCReportRepository | None = None,
    attempt_id: str = "",
    owner_user_id: str = "",
    project_id: str = "",
    essential_only: bool = False,
) -> QCReport:
    owner_user_id = str(owner_user_id or package.get("owner_user_id") or "").strip()
    project_id = str(project_id or package.get("project_id") or "").strip()
    if not owner_user_id or not project_id:
        raise ValueError("owner_user_id and project_id are required for Keyframe QC")
    artifact_id = str(package.get("storyboard_id") or "").strip()
    if not artifact_id:
        raise ValueError("storyboard_id is required for Keyframe QC")
    artifact_version = package.get("version")
    if (
        isinstance(artifact_version, bool)
        or not isinstance(artifact_version, int)
        or artifact_version < 1
    ):
        raise ValueError("storyboard version must be a positive integer for Keyframe QC")

    frames = package.get("generated_keyframes")
    if not isinstance(frames, list) or not frames:
        raise ValueError("generated keyframe package is empty")
    if evaluator is None:
        from video_factory.integrations.nine_router import configured_qc_evaluator
        evaluator = configured_qc_evaluator()
    provider_mode = "fake" if getattr(evaluator, "fake_mode", False) else "configured"
    evaluator_name = str(getattr(evaluator, "evaluator_name", "unknown"))
    evaluator_version = str(getattr(evaluator, "evaluator_version", "unknown"))
    model_identifier = str(getattr(evaluator, "model", ""))
    artifact_digest = _artifact_digest(package)
    report_id = f"qc_keyframe_{workflow_id}_{uuid.uuid4().hex[:8]}"
    reference_digests: list[str] = []
    items: list[dict[str, Any]] = []
    evaluator_calls = 0

    for frame in frames:
        frame_id = str(frame.get("frame_id") or "")
        scene_id = str(frame.get("scene_id") or frame_id)
        approved_scene = _approved_script_scene(package, scene_id)
        if approved_scene is None:
            items.append(
                {
                    "frame_id": frame_id,
                    "scene_id": scene_id,
                    "score": 0,
                    "verdict": "unknown",
                    "hard_failure": False,
                    "reason": "approved script scene is missing; prompt and script alignment cannot be checked",
                }
            )
            break
        image_path = Path(str(frame.get("image_path") or "")).resolve()
        expected_sha = str(frame.get("sha256") or "").removeprefix("sha256:")
        references = [Path(str(value)).resolve() for value in frame.get("reference_image_paths", [])]
        expected_reference_shas = [
            str(value or "").removeprefix("sha256:")
            for value in frame.get("reference_sha256s", [])
        ]
        if (
            not references
            or len(expected_reference_shas) != len(references)
            or any(not value for value in expected_reference_shas)
        ):
            items.append({
                "frame_id": frame_id,
                "scene_id": scene_id,
                "score": 0,
                "verdict": "unknown",
                "hard_failure": False,
                "reason": "approved reference SHA-256 evidence is missing; cannot bind QC to the approved source images",
                "findings": {"contract_error": "APPROVED_REFERENCE_DIGEST_MISSING"},
            })
            break
        if any(
            not path.is_file()
            or path.stat().st_size == 0
            or _safe_sha256(path) != expected_sha_value
            or _image_dimensions(path) is None
            for path, expected_sha_value in zip(references, expected_reference_shas)
        ):
            items.append({
                "frame_id": frame_id,
                "scene_id": scene_id,
                "score": 0,
                "verdict": "unknown",
                "hard_failure": False,
                "reason": "approved reference is missing, changed, or undecodable; cannot verify this storyboard contract",
                "findings": {"contract_error": "APPROVED_REFERENCE_DIGEST_MISMATCH"},
            })
            break
        if (
            not frame_id
            or not image_path.is_file()
            or image_path.stat().st_size == 0
            or _safe_sha256(image_path) != expected_sha
        ):
            items.append(
                {
                    "frame_id": frame_id,
                    "scene_id": scene_id,
                    "score": 0,
                    "verdict": "unknown",
                    "hard_failure": False,
                    "reason": "generated keyframe bytes are missing or differ from the recorded SHA-256",
                    "findings": {"contract_error": "GENERATED_KEYFRAME_DIGEST_MISMATCH"},
                }
            )
            break
        candidate_dimensions = _image_dimensions(image_path)
        if candidate_dimensions is None:
            items.append({
                "frame_id": frame_id,
                "scene_id": scene_id,
                "image_path": str(image_path),
                "sha256": expected_sha,
                "score": 0,
                "verdict": "fail",
                "hard_failure": True,
                "reason": "generated keyframe bytes are corrupt or cannot be decoded as an image",
                "reference_digests": expected_reference_shas,
                "reference_sha256s": expected_reference_shas,
                "recommended_action": "Regenerate this keyframe with Google Flow, then QC the new bytes.",
            })
            continue
        width, height = candidate_dimensions
        aspect_class = "square" if width == height else "portrait" if height > width else "landscape"
        if aspect_class == "landscape" or (
            aspect_class == "portrait"
            and abs((width / height) - (9 / 16)) > (9 / 16) * 0.01
        ):
            items.append({
                "frame_id": frame_id,
                "scene_id": scene_id,
                "image_path": str(image_path),
                "sha256": expected_sha,
                "reference_digests": expected_reference_shas,
                "score": 0,
                "verdict": "fail",
                "hard_failure": True,
                "reason": (
                    f"Keyframe must be square or native 9:16 portrait; decoded bytes are {width}x{height}."
                ),
                "findings": {"contract_error": "KEYFRAME_ASPECT_RATIO_INVALID"},
                "recommended_action": "Generate a square or native 9:16 portrait Flow image; do not pad or crop it.",
            })
            continue
        frame_reference_digests = [_sha256(path) for path in references]
        reference_digests.extend(frame_reference_digests)
        try:
            evaluator_calls += 1
            response = evaluator.evaluate_multiple_images(
                [*references, image_path],
                prompt=build_keyframe_qc_prompt(
                    brand=product.brand,
                    model=product.model,
                    scene_id=str(frame.get("scene_id") or frame_id),
                    scene_prompt=str(frame.get("prompt") or ""),
                    candidate_dimensions=candidate_dimensions,
                    approved_script_scene=approved_scene,
                    essential_only=essential_only,
                ),
                system_instruction=KEYFRAME_QC_SYSTEM_PROMPT,
            )
            normalized = validate_qc_response(response, QCStage.KEYFRAME)
            if essential_only:
                hard_failure = bool(
                    response["hard_failure"]
                    or normalized["identity_mismatch"]
                    or normalized["source_branding_detected"]
                    or normalized["black_bars_detected"] is True
                    or not normalized["prompt_reference_consistent"]
                    or not normalized["script_adherence"]
                    or not normalized["important_details_preserved"]
                )
                normalized["hard_failure"] = hard_failure
                normalized["verdict"] = (
                    "unknown" if response["verdict"] == "unknown" else "fail" if hard_failure else "pass"
                )
            score = int(normalized["total_score"])
            hard_failure = bool(normalized["hard_failure"])
            verdict = str(normalized["verdict"])
            if hard_failure or verdict != "pass":
                verdict = "fail" if verdict != "unknown" else "unknown"
            items.append(
                {
                    "frame_id": frame_id,
                    "scene_id": frame.get("scene_id"),
                    "image_path": str(image_path),
                    "sha256": expected_sha,
                    "dimensions": [width, height],
                    "aspect_class": aspect_class,
                    "aspect_ratio_warning": aspect_class != "portrait",
                    "reference_digests": frame_reference_digests,
                    "score": score,
                    "verdict": verdict,
                    "hard_failure": hard_failure,
                    "black_bars_detected": bool(normalized["black_bars_detected"]),
                    "reason": str(normalized.get("reason") or ""),
                    "prompt_reference_consistent": bool(
                        normalized.get("prompt_reference_consistent", True)
                    ),
                    "script_adherence": bool(normalized["script_adherence"]),
                    "important_details_preserved": bool(
                        normalized.get("important_details_preserved", True)
                    ),
                    "composition_usable": bool(normalized.get("composition_usable", True)),
                    "animation_suitable": bool(normalized.get("animation_suitable", True)),
                    "recommended_action": str(normalized.get("recommended_action") or ""),
                    "findings": normalized,
                }
            )
            if verdict == "unknown":
                # Any uncertain provider outcome must be reconciled before
                # another frame can submit a potentially paid request.
                break
        except Exception as exc:
            diagnostics: dict[str, Any] = {}
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
                receipt = getattr(exc, "route_receipt", None)
                if isinstance(receipt, dict):
                    diagnostics["route_receipt"] = receipt
            elif bool(getattr(exc, "response_received", False)):
                diagnostics = {
                    "schema_invalid": True,
                    "technical_unknown": False,
                    "usage_status": str(getattr(exc, "usage_status", "consumed")),
                    "reconcile_required": False,
                    "retry_allowed": bool(getattr(exc, "retry_allowed", True)),
                }
                receipt = getattr(exc, "route_receipt", None)
                if isinstance(receipt, dict):
                    diagnostics["route_receipt"] = receipt
            elif is_technical_error(exc):
                diagnostics = {
                    "technical_unknown": True,
                    "usage_status": "uncertain",
                    "reconcile_required": True,
                    "retry_allowed": False,
                }
                receipt = getattr(exc, "route_receipt", None)
                if isinstance(receipt, dict):
                    diagnostics["route_receipt"] = receipt
            items.append(
                {
                    "frame_id": frame_id,
                    "score": 0,
                    "verdict": "unknown",
                    "hard_failure": False,
                    "reason": f"Evaluation call failed: {exc}",
                    "findings": diagnostics,
                }
            )
            break

    selected_candidates: list[dict[str, Any]] = []
    from video_factory.creative.storyboard import select_best_keyframe_candidate
    for index, item in enumerate(list(items)):
        if str(item.get("verdict") or "").lower() == "unknown":
            continue
        frame = next((frame for frame in frames
                      if isinstance(frame, dict) and str(frame.get("frame_id") or "") == str(item.get("frame_id") or "")), None)
        if frame is None:
            continue
        current = {
            "frame_id": item.get("frame_id"), "image_path": item.get("image_path"),
            "sha256": item.get("sha256"), "qc_verdict": item.get("verdict"),
            "qc_score": item.get("score"), "qc_hard_failure": item.get("hard_failure"),
            "qc_findings": item.get("findings"),
            "qc_receipt": {"report_id": report_id, "artifact_digest": artifact_digest},
            "qc_item": item,
        }
        history = [entry for entry in (frame.get("candidate_history") or [])
                   if isinstance(entry, dict) and entry.get("frame_id") == item.get("frame_id")]
        selected = select_best_keyframe_candidate([*history, current])
        if selected is None:
            continue
        selected_item = selected.get("qc_item")
        if isinstance(selected_item, dict):
            items[index] = dict(selected_item)
        selected_candidates.append({
            "frame_id": item.get("frame_id"), "image_path": selected.get("image_path"),
            "sha256": selected.get("sha256"), "score": selected.get("qc_score"),
            "receipt": selected.get("qc_receipt"),
        })

    if any(item["verdict"] == "unknown" for item in items):
        verdict = QCVerdict.UNKNOWN
    elif all(item["verdict"] == "pass" and not item["hard_failure"] for item in items):
        verdict = QCVerdict.PASS
    else:
        verdict = QCVerdict.FAIL
    score = round(sum(int(item["score"]) for item in items) / len(items))
    fingerprint = qc_fingerprint(
        workflow_id,
        QCStage.KEYFRAME,
        artifact_digest,
        product,
        evaluator_name=evaluator_name,
        evaluator_version=evaluator_version,
        model_identifier=model_identifier,
        storyboard=package,
        reference_digests=reference_digests,
        attempt_id=attempt_id,
        keyframe_essential_only=essential_only,
    )
    recovery_frames: list[dict[str, Any]] = []
    for item in items:
        if item.get("verdict") == "pass" and not item.get("hard_failure"):
            continue
        actions: list[str] = []
        if (
            bool(item.get("hard_failure"))
            or not bool(item.get("important_details_preserved", True))
            or bool(item.get("findings", {}).get("identity_mismatch"))
        ):
            actions.append("reselect_reference")
        if not bool(item.get("prompt_reference_consistent", True)):
            actions.append("rewrite_prompt")
        if not bool(item.get("script_adherence", True)):
            actions.append("rewrite_prompt")
        if not bool(item.get("composition_usable", True)) or not bool(
            item.get("animation_suitable", True)
        ):
            actions.append("adjust_composition")
        # Every content-QC retry must produce a revised prompt as well as a
        # fresh artifact; otherwise the provider may repeat the same failure.
        if "rewrite_prompt" not in actions:
            actions.append("rewrite_prompt")
        actions.extend(("regenerate_keyframe", "rerun_keyframe_qc"))
        recovery_frames.append(
            {
                "frame_id": item.get("frame_id"),
                "scene_id": item.get("scene_id"),
                "actions": actions,
                "feedback": str(
                    item.get("recommended_action") or item.get("reason") or "Keyframe QC failed"
                ),
            }
        )

    report = QCReport(
        report_id=report_id,
        workflow_id=workflow_id,
        stage=QCStage.KEYFRAME,
        artifact_id=artifact_id,
        artifact_version=artifact_version,
        artifact_digest=artifact_digest,
        reference_digests=reference_digests,
        evaluator_name=evaluator_name,
        evaluator_version=evaluator_version,
        rubric_version=KEYFRAME_RUBRIC_VERSION,
        created_at=datetime.now(timezone.utc).isoformat(),
        score=score,
        verdict=verdict,
        findings={
            "items": items,
            "evaluator_calls": evaluator_calls,
            "frame_sha256s": {
                str(item.get("frame_id")): str(item.get("sha256") or "").removeprefix("sha256:")
                for item in items if item.get("frame_id") and item.get("sha256")
            },
            "selected_candidates": selected_candidates,
            "passed_count": sum(item["verdict"] == "pass" for item in items),
            "total_count": len(items),
            "attempt_id": attempt_id,
            "recovery_plan": {
                "blocked_from_video": bool(recovery_frames),
                "frames": recovery_frames,
                "policy": "reselect/rewrite/adjust/regenerate/qc",
            },
        },
        fingerprint=fingerprint,
        provider_mode=provider_mode,
        model_identifier=model_identifier,
        policy_version=KEYFRAME_POLICY_VERSION,
        owner_user_id=owner_user_id,
        project_id=project_id,
    )
    if repo is not None:
        repo.save_report(report)
    return report
