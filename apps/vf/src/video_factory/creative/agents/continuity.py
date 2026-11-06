"""Admit an explicit continuous scene from a checked predecessor endpoint."""
from dataclasses import replace
from io import BytesIO
from pathlib import Path
import hashlib
import os
import re
import tempfile

import av

from ..ports import MediaReference
from .gates import board_media


def continues_previous_clip(scene):
    return re.match(r"^continue_previous_clip\b",
                    str(scene.get("continuity_from_previous") or "").strip()) is not None


def scene_start_bindings(operations):
    import json
    bindings = {}
    for operation in operations:
        raw = json.loads(operation.get("request_json") or "{}")
        binding = (raw.get("provider_options") or {}).get("continuity_binding")
        if binding:
            bindings[f"{operation['scene_id']}-start"] = binding
    return bindings


def prepare_continuity_start(*, package, scene, operation, previous, store, critic,
                             output_dir, clip_assessment):
    from video_factory.integrations.scene_renderer import stable_scene_request_digest

    clip = Path(previous.get("output_path") or "")
    if previous.get("status") != "completed" or not clip.is_file():
        raise ValueError("continuity predecessor is not completed")
    clip_sha = hashlib.sha256(clip.read_bytes()).hexdigest()
    if (clip_sha != str(previous.get("output_sha256") or "").removeprefix("sha256:")
            or clip_assessment.get("sha256") != clip_sha
            or clip_assessment["technical_qc"]["verdict"] != "PASS"
            or clip_assessment["clip_review"]["verdict"] != "PASS"):
        raise ValueError("continuity requires a current clip content and technical PASS")
    request = store.request_from_record(operation)
    existing = request.provider_options.get("continuity_binding")
    if existing:
        if (existing.get("previous_operation_id") != previous["operation_id"]
                or existing.get("previous_output_sha256") != clip_sha
                or existing.get("clip_review_digest") != clip_assessment["clip_review"]["input_digest"]):
            raise ValueError("continuity binding is stale; reconcile before submitting")
        return request, None
    if operation["status"] != "planned":
        raise ValueError("continuity cannot change a claimed operation")

    last = None
    with av.open(str(clip)) as container:
        for frame in container.decode(video=0):
            last = frame
        if last is None:
            raise ValueError("continuity predecessor contains no decoded frames")
        timestamp = float(last.time or 0)
        image = last.to_image()
    encoded = BytesIO()
    image.save(encoded, format="PNG")
    pixels = encoded.getvalue()
    frame_sha = hashlib.sha256(pixels).hexdigest()
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{frame_sha}.png"
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != frame_sha:
            raise ValueError("continuity frame digest changed")
    else:
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(pixels)
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    media = tuple(ref for ref in board_media(package) if not ref.role.startswith("keyframe:")) + (
        MediaReference(str(path), frame_sha, f"clip:{previous['scene_id']}:t={timestamp}"),)
    context = {
        "scene_ids": [previous["scene_id"]], "next_scene": scene,
        "video_sha256": clip_sha, "package_digest": package["package_digest"],
        "required_timestamps": [timestamp],
        "endpoint_requirement": "Check this actual last frame against source identity and the next scene START intent. "
            "A continuous next shot must preserve the visible inventory, setting, pose and physical state. "
            "Reject blur, occlusion, malformed or duplicated products, unreadable identity, and insufficient framing margin.",
    }
    review = critic.review(stage="clip", context=context, media=media)
    endpoint_assessment = {**clip_assessment, "clip_review": review.to_dict(), "review_context": context,
                           "review_media": [{"path": ref.path, "sha256": ref.sha256, "role": ref.role} for ref in media]}
    if review.verdict != "PASS":
        return None, endpoint_assessment
    binding = {
        "base_request_digest": stable_scene_request_digest(request),
        "previous_operation_id": previous["operation_id"], "previous_output_sha256": clip_sha,
        "frame_path": str(path), "frame_sha256": frame_sha, "frame_timestamp": timestamp,
        "clip_review_digest": clip_assessment["clip_review"]["input_digest"],
        "endpoint_review_digest": review.input_digest,
    }
    bound = replace(request, reference_image_paths=(str(path),),
                    provider_options={**request.provider_options, "continuity_binding": binding})
    saved = store.bind_continuity_start(bound, expected_digest=operation["request_digest"])
    return store.request_from_record(saved), None
