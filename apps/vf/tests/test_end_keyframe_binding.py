from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from video_factory.integrations.scene_renderer import (
    SceneRenderOperationStore,
    SceneRenderRuntime,
    compile_grounded_scene_request,
    stable_scene_request_digest,
)


def _scene() -> dict:
    return {
        "scene_id": "scene-1", "duration_seconds": 4,
        "subject_action": "Person places earbuds in case",
        "product_interaction": "The case lid closes",
        "start_state": "Lid open", "end_state": "Lid closed",
        "visual_change": "Lid closes", "environment": "Desk",
        "end_keyframe_policy": "required",
    }


def _inputs(tmp_path: Path) -> tuple[dict, dict, dict]:
    frames = []
    items = []
    for role in ("start", "end"):
        path = tmp_path / f"scene-1-{role}.png"
        path.write_bytes(role.encode())
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        frames.append({
            "scene_id": "scene-1", "frame_id": f"scene-1-{role}", "frame_role": role,
            "image_path": str(path), "sha256": digest,
            "reference_sha256s": ["a" * 64],
        })
        items.append({
            "frame_id": f"scene-1-{role}", "verdict": "pass", "score": 90,
            "sha256": digest, "reference_digests": ["a" * 64],
        })
    receipt = {
        "report_id": "keyframe-qc-1", "stage": "keyframe", "verdict": "pass",
        "score": 90, "artifact_digest": "sha256:package", "findings": {"items": items},
    }
    return frames[0], frames[1], receipt


def _compile(tmp_path: Path):
    start, end, receipt = _inputs(tmp_path)
    request = compile_grounded_scene_request(
        owner_user_id="owner", workflow_id="workflow", project_id="project",
        scene=_scene(), keyframe=start, end_keyframe=end,
        keyframe_qc_receipt=receipt, package_digest="sha256:package",
        product_identity="Baseus WM02", provider_model="end-capable-provider",
    )
    return request, start, end, receipt


def test_end_binding_is_distinct_and_changes_scene_identity(tmp_path: Path) -> None:
    first, start, end, receipt = _compile(tmp_path)
    assert first.scene_id == "scene-1"
    assert first.reference_image_paths == (start["image_path"],)
    assert first.end_keyframe is not None
    assert first.end_keyframe.frame_id == "scene-1-end"
    assert first.end_keyframe.qc_report_id == "keyframe-qc-1"
    old_digest = stable_scene_request_digest(first)
    Path(end["image_path"]).write_bytes(b"new-end-frame")
    end["sha256"] = hashlib.sha256(Path(end["image_path"]).read_bytes()).hexdigest()
    receipt["findings"]["items"][1]["sha256"] = end["sha256"]
    receipt["artifact_digest"] = "sha256:new-package"
    second = compile_grounded_scene_request(
        owner_user_id="owner", workflow_id="workflow", project_id="project",
        scene=_scene(), keyframe=start, end_keyframe=end,
        keyframe_qc_receipt=receipt, package_digest="sha256:new-package",
        product_identity="Baseus WM02", provider_model="end-capable-provider",
    )
    assert first.request_id != second.request_id
    assert old_digest != stable_scene_request_digest(second)


def test_missing_or_mismatched_end_is_rejected(tmp_path: Path) -> None:
    _request, start, end, receipt = _compile(tmp_path)
    with pytest.raises(ValueError, match="required END"):
        compile_grounded_scene_request(
            owner_user_id="owner", workflow_id="workflow", project_id="project",
            scene=_scene(), keyframe=start, keyframe_qc_receipt=receipt,
            package_digest="sha256:package", product_identity="Baseus WM02",
            provider_model="provider",
        )
    end["scene_id"] = "other-scene"
    with pytest.raises(ValueError, match="END keyframe scene binding"):
        compile_grounded_scene_request(
            owner_user_id="owner", workflow_id="workflow", project_id="project",
            scene=_scene(), keyframe=start, end_keyframe=end,
            keyframe_qc_receipt=receipt, package_digest="sha256:package",
            product_identity="Baseus WM02", provider_model="provider",
        )


def test_unsupported_provider_never_submits_end_conditioned_scene(tmp_path: Path) -> None:
    request, _start, _end, _receipt = _compile(tmp_path)

    class Provider:
        provider_kind = "vertex_ai"
        model = "fake"

        def __init__(self):
            self.submitted = False

        def submit(self, _request):
            self.submitted = True
            raise AssertionError("paid submit must not occur")

        def get_status(self, _operation_id):
            raise AssertionError("no prior operation")

    provider = Provider()
    runtime = SceneRenderRuntime(store=SceneRenderOperationStore(sqlite3.connect(":memory:")), provider=provider)
    result = runtime.run(request)
    assert result.status == "failed"
    assert result.error_code == "PROVIDER_UNAVAILABLE"
    assert provider.submitted is False
