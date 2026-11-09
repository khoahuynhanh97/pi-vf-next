from __future__ import annotations

import base64
import hashlib
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from video_factory.integrations.keyframe_provider import (
    KeyframeOperationStore,
    KeyframeRuntime,
    VertexKeyframeProvider,
)
from video_factory.integrations.ports import (
    KeyframeGenerationRequest,
    KeyframeGenerationResult,
)
from video_factory.qc.keyframe_qc import run_keyframe_qc
from video_factory.qc.models import NormalizedProduct, QCVerdict


def _write_image(path: Path, color: str) -> None:
    Image.new("RGB", (16, 16), color=color).save(path, format="PNG")


def _request(reference: Path) -> KeyframeGenerationRequest:
    return KeyframeGenerationRequest(
        operation_id="keyframe-pv-1-scene-1",
        workflow_id="pv-1",
        project_id="project-1",
        frame_id="scene-1-start",
        prompt="Create a real-life desk scene with Baseus Bowie WM02 product [1].",
        product_description="Baseus Bowie WM02 wireless earbuds and charging case",
        reference_image_paths=(str(reference),),
        aspect_ratio="9:16",
    )


def test_vertex_keyframe_provider_uses_product_references_and_writes_image(
    tmp_path: Path,
) -> None:
    reference = tmp_path / "reference.jpg"
    reference.write_bytes(b"source-image")
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"generated"
    calls: list[dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> Any:
        calls.append({"url": url, **kwargs})
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
            "candidates": [{
                "content": {"parts": [{"inlineData": {
                    "data": base64.b64encode(png_bytes).decode(),
                    "mimeType": "image/png",
                }}]}
            }]
            },
        )

    result = VertexKeyframeProvider(
        project_id="gen-lang-client-0816609628",
        output_dir=tmp_path / "keyframes",
        credential_resolver=lambda: ("token", "ignored"),
        post_fn=post,
    ).submit(_request(reference))

    assert result.status == "completed"
    assert Path(result.image_path or "").read_bytes() == png_bytes
    body = calls[0]["json"]
    assert body["generationConfig"]["imageConfig"]["aspectRatio"] == "9:16"
    assert body["contents"][0]["parts"][1]["inlineData"]["mimeType"] == "image/jpeg"
    assert "seller" in body["contents"][0]["parts"][0]["text"]


class _ResumableProvider:
    def __init__(self, completed_path: Path) -> None:
        self.completed_path = completed_path
        self.submit_calls: list[str] = []
        self.poll_calls: list[str] = []

    def submit(self, request: KeyframeGenerationRequest) -> KeyframeGenerationResult:
        self.submit_calls.append(request.operation_id)
        raise AssertionError("resume must not resubmit")

    def get_status(self, operation_id: str) -> KeyframeGenerationResult:
        self.poll_calls.append(operation_id)
        return KeyframeGenerationResult(
            operation_id="keyframe-pv-1-scene-1",
            status="completed",
            provider_operation_id=operation_id,
            image_path=str(self.completed_path),
        )


def test_resume_polls_existing_keyframe_operation_without_resubmit(tmp_path: Path) -> None:
    reference = tmp_path / "reference.jpg"
    reference.write_bytes(b"source-image")
    completed = tmp_path / "generated.png"
    completed.write_bytes(b"generated-image")
    connection = sqlite3.connect(":memory:")
    store = KeyframeOperationStore(connection)
    request = _request(reference)
    store.save_result(
        request,
        KeyframeGenerationResult(
            operation_id=request.operation_id,
            status="running",
            provider_operation_id="operations/kf-1",
        ),
    )
    provider = _ResumableProvider(completed)

    result = KeyframeRuntime(store=store, provider=provider).run(request)

    assert result.status == "completed"
    assert provider.submit_calls == []
    assert provider.poll_calls == ["operations/kf-1"]
    saved = store.get(request.operation_id)
    assert saved is not None
    assert saved["output_sha256"] == hashlib.sha256(completed.read_bytes()).hexdigest()


def test_runtime_keeps_terminal_failure_on_same_attempt(tmp_path: Path) -> None:
    reference = tmp_path / "reference.jpg"
    reference.write_bytes(b"source-image")
    connection = sqlite3.connect(":memory:")
    store = KeyframeOperationStore(connection)
    request = _request(reference)

    class RetryProvider:
        def __init__(self) -> None:
            self.submit_calls: list[str] = []

        def submit(self, req: KeyframeGenerationRequest) -> KeyframeGenerationResult:
            self.submit_calls.append(req.operation_id)
            return KeyframeGenerationResult(
                operation_id=req.operation_id,
                status="completed",
                provider_operation_id=req.operation_id,
            )

        def get_status(self, _: str) -> KeyframeGenerationResult:
            raise AssertionError("failed operation must not be polled")

    store.save_result(
        request,
        KeyframeGenerationResult(
            operation_id=request.operation_id,
            status="failed",
            error_code="HTTP_404",
            error_message="model unavailable",
        ),
    )
    provider = RetryProvider()

    result = KeyframeRuntime(store=store, provider=provider).run(request)

    assert result.status == "failed"
    assert result.error_code == "HTTP_404"
    assert provider.submit_calls == []


def test_two_sqlite_workers_claim_one_first_keyframe_submission(tmp_path: Path) -> None:
    reference = tmp_path / "reference.jpg"
    reference.write_bytes(b"source-image")
    request = _request(reference)
    db_path = tmp_path / "keyframe-claims.sqlite"
    KeyframeOperationStore(sqlite3.connect(db_path)).connection.close()
    barrier = threading.Barrier(2)
    calls: list[str] = []
    results: list[KeyframeGenerationResult] = []

    class CountedProvider:
        def submit(self, req: KeyframeGenerationRequest) -> KeyframeGenerationResult:
            calls.append(req.operation_id)
            time.sleep(0.1)
            return KeyframeGenerationResult(
                operation_id=req.operation_id, status="running",
                provider_operation_id="external-operation-1",
            )

        def get_status(self, operation_id: str) -> KeyframeGenerationResult:
            return KeyframeGenerationResult(
                operation_id=request.operation_id, status="running",
                provider_operation_id=operation_id,
            )

    provider = CountedProvider()

    def worker() -> None:
        connection = sqlite3.connect(db_path)
        runtime = KeyframeRuntime(store=KeyframeOperationStore(connection), provider=provider)
        barrier.wait()
        results.append(runtime.run(request))
        connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert calls == [request.operation_id]
    assert len(results) == 2
    assert {result.status for result in results} <= {"running", "unknown"}
    connection = sqlite3.connect(db_path)
    stored = KeyframeOperationStore(connection).get(request.operation_id)
    assert stored is not None
    assert stored["provider_operation_id"] == "external-operation-1"
    assert stored["provider_kind"] == "vertex_keyframe"
    assert stored["submitted_at"]
    connection.close()


def test_legacy_keyframe_operation_schema_migrates_provider_pin(tmp_path: Path) -> None:
    reference = tmp_path / "reference.jpg"
    reference.write_bytes(b"source-image")
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE video_factory_keyframe_operation (
            operation_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            frame_id TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            provider_operation_id TEXT,
            status TEXT NOT NULL,
            output_path TEXT,
            output_sha256 TEXT NOT NULL DEFAULT '',
            error_code TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    store = KeyframeOperationStore(connection)
    request = _request(reference)

    assert store.claim_first_submission(request, "google_flow") is True
    row = store.get(request.operation_id)
    assert row is not None
    assert row["provider_kind"] == "google_flow"
    assert row["submitted_at"]


def test_keyframe_qc_requires_every_generated_frame_to_pass(tmp_path: Path) -> None:
    reference = tmp_path / "reference.jpg"
    keyframe = tmp_path / "keyframe.png"
    _write_image(reference, "white")
    _write_image(keyframe, "black")

    class Evaluator:
        fake_mode = True
        evaluator_name = "fake"
        evaluator_version = "1"
        model = "fake-vision"

        def __init__(self) -> None:
            self.calls = 0

        def evaluate_multiple_images(self, *_: Any, **__: Any) -> dict[str, Any]:
            self.calls += 1
            return {
                "identity_consistency": 46,
                "scene_adherence": 27,
                "visual_cleanliness": 18,
                "total_score": 91,
                "verdict": "pass",
                "hard_failure": False,
                "source_branding_detected": False,
                "identity_mismatch": False,
                "prompt_reference_consistent": True,
                "script_adherence": True,
                "important_details_preserved": True,
                "composition_usable": True,
                "animation_suitable": True,
                "black_bars_detected": False,
                "recommended_action": "none",
                "reason": "grounded and clean",
            }

    evaluator = Evaluator()
    report = run_keyframe_qc(
        workflow_id="pv-1",
        package={
            "storyboard_id": "story-1",
            "package_digest": "sha256:" + "a" * 64,
            "version": 1,
            "owner_user_id": "owner-pv-1",
            "project_id": "project-1",
            "script": {"scenes": [{"scene_id": "scene-1", "subject_action": "User places earbuds into charging case"}]},
            "generated_keyframes": [
                {
                    "frame_id": "scene-1-start",
                    "scene_id": "scene-1",
                    "image_path": str(keyframe),
                    "sha256": hashlib.sha256(keyframe.read_bytes()).hexdigest(),
                    "prompt": "User places earbuds into charging case",
                    "reference_image_paths": [str(reference)],
                    "reference_sha256s": [hashlib.sha256(reference.read_bytes()).hexdigest()],
                }
            ],
        },
        product=NormalizedProduct(brand="Baseus", model="Bowie WM02"),
        evaluator=evaluator,
    )

    assert report.verdict == QCVerdict.PASS
    assert report.score == 91
    assert report.findings["items"][0]["verdict"] == "pass"
    assert report.findings["items"][0]["sha256"] == hashlib.sha256(
        keyframe.read_bytes()
    ).hexdigest()
    assert report.findings["items"][0]["reference_digests"] == [
        hashlib.sha256(reference.read_bytes()).hexdigest()
    ]
    assert evaluator.calls == 1
    assert report.findings["recovery_plan"]["frames"] == []
    assert report.findings["selected_candidates"][0]["sha256"] == report.findings["items"][0]["sha256"]


@pytest.mark.parametrize(("essential_only", "script_mismatch"), [(False, True), (True, True), (True, False)])
def test_keyframe_qc_reports_mismatch_with_approved_script_scene(
    tmp_path: Path, essential_only: bool, script_mismatch: bool,
) -> None:
    reference = tmp_path / "script-reference.png"
    keyframe = tmp_path / "script-keyframe.png"
    _write_image(reference, "white")
    _write_image(keyframe, "black")

    class Evaluator:
        fake_mode = True
        evaluator_name = "fake"
        evaluator_version = "1"
        model = "fake-vision"

        def __init__(self) -> None:
            self.prompt = ""

        def evaluate_multiple_images(self, _images: list[str], prompt: str, **_: Any) -> dict[str, Any]:
            self.prompt = prompt
            return {
                "identity_consistency": 46 if script_mismatch else 30,
                "scene_adherence": 27 if script_mismatch else 15,
                "visual_cleanliness": 18 if script_mismatch else 10,
                "total_score": 91 if script_mismatch else 55,
                "verdict": "pass" if script_mismatch else "fail",
                "hard_failure": False,
                "source_branding_detected": False,
                "identity_mismatch": False,
                "prompt_reference_consistent": True,
                "script_adherence": not script_mismatch,
                "important_details_preserved": True,
                "composition_usable": script_mismatch,
                "animation_suitable": True,
                "black_bars_detected": False,
                "recommended_action": "Show the speaker being placed on the picnic blanket as narrated.",
                "reason": "The frame shows the speaker on a bicycle, against the approved script.",
            }

    evaluator = Evaluator()
    report = run_keyframe_qc(
        workflow_id="pv-script-qc",
        package={
            "storyboard_id": "story-script-qc",
            "package_digest": "sha256:" + "c" * 64,
            "version": 1,
            "owner_user_id": "owner-script-qc",
            "project_id": "project-script-qc",
            "script": {
                "scenes": [
                    {
                        "scene_id": "scene-1",
                        "story_role": "Demonstration",
                        "visual_objective": "Show the speaker at a picnic.",
                        "subject_action": "A hand places the speaker on the blanket.",
                        "start_state": "Speaker is held above a picnic blanket.",
                        "end_state": "Speaker rests on the picnic blanket.",
                        "narration": {"narration_lines": [{"text": "Đặt loa xuống và tận hưởng âm nhạc."}]},
                    }
                ]
            },
            "generated_keyframes": [
                {
                    "frame_id": "scene-1-start",
                    "scene_id": "scene-1",
                    "image_path": str(keyframe),
                    "sha256": hashlib.sha256(keyframe.read_bytes()).hexdigest(),
                    "prompt": "A hand places the speaker on the picnic blanket.",
                    "reference_image_paths": [str(reference)],
                    "reference_sha256s": [hashlib.sha256(reference.read_bytes()).hexdigest()],
                }
            ],
        },
        product=NormalizedProduct(brand="Tribit", model="XSound Go"),
        evaluator=evaluator,
        essential_only=essential_only,
    )

    assert "Approved script scene" in evaluator.prompt
    assert "Đặt loa xuống và tận hưởng âm nhạc." in evaluator.prompt
    if not script_mismatch:
        assert report.verdict == QCVerdict.PASS
        assert report.findings["recovery_plan"]["frames"] == []
        return
    assert report.verdict == QCVerdict.FAIL
    assert report.findings["items"][0]["script_adherence"] is False
    recovery = report.findings["recovery_plan"]["frames"][0]
    assert recovery["frame_id"] == "scene-1-start"
    assert "regenerate_keyframe" in recovery["actions"]
    assert "rerun_keyframe_qc" in recovery["actions"]
    assert recovery["feedback"] == "Show the speaker being placed on the picnic blanket as narrated."


def test_keyframe_qc_keeps_prior_pass_candidate_when_repair_is_worse_or_wrong_identity(tmp_path: Path) -> None:
    from video_factory.creative.storyboard import storyboard_package_digest

    reference = tmp_path / "candidate-reference.png"
    prior_path = tmp_path / "prior-pass.png"
    new_path = tmp_path / "new-wrong.png"
    _write_image(reference, "white")
    _write_image(prior_path, "blue")
    _write_image(new_path, "red")
    prior_sha = hashlib.sha256(prior_path.read_bytes()).hexdigest()
    reference_sha = hashlib.sha256(reference.read_bytes()).hexdigest()
    prior_item = {
        "frame_id": "scene-1-start", "scene_id": "scene-1", "image_path": str(prior_path),
        "sha256": prior_sha, "score": 79, "verdict": "pass", "hard_failure": False,
        "findings": {"identity_mismatch": False},
    }
    current_frame = {
        "frame_id": "scene-1-start", "scene_id": "scene-1", "image_path": str(new_path),
        "sha256": hashlib.sha256(new_path.read_bytes()).hexdigest(), "prompt": "Show the correct product",
        "reference_image_paths": [str(reference)], "reference_sha256s": [reference_sha],
        "candidate_history": [{
            "frame_id": "scene-1-start", "image_path": str(prior_path), "sha256": prior_sha,
            "qc_verdict": "pass", "qc_score": 79, "qc_hard_failure": False,
            "qc_findings": {"identity_mismatch": False},
            "qc_receipt": {"report_id": "previous-pass", "artifact_digest": "previous-package"},
            "qc_item": prior_item,
        }],
    }
    package = {
        "schema_version": "storyboard-package-v2", "storyboard_id": "story-candidate", "version": 1,
        "owner_user_id": "owner-candidate", "project_id": "project-candidate",
        "script": {"scenes": [{"scene_id": "scene-1", "subject_action": "Show the correct product"}]},
        "generated_keyframes": [current_frame],
    }
    package["package_digest"] = storyboard_package_digest(package)

    class Evaluator:
        fake_mode = True
        evaluator_name = "fake"
        evaluator_version = "1"
        model = "fake-vision"

        def evaluate_multiple_images(self, *_args, **_kwargs):
            return {
                "identity_consistency": 50, "scene_adherence": 30, "visual_cleanliness": 20,
                "total_score": 100, "verdict": "fail", "hard_failure": True,
                "source_branding_detected": False, "identity_mismatch": True,
                "prompt_reference_consistent": True, "script_adherence": True,
                "important_details_preserved": False, "composition_usable": True,
                "animation_suitable": True, "black_bars_detected": False,
                "recommended_action": "Use the approved model", "reason": "Wrong product identity",
            }

    report = run_keyframe_qc(workflow_id="wf-candidate", package=package,
        product=NormalizedProduct(brand="Brand", model="Exact Model"), evaluator=Evaluator())

    assert report.verdict is QCVerdict.PASS
    assert report.findings["items"][0]["sha256"] == prior_sha
    assert report.findings["selected_candidates"][0]["receipt"]["report_id"] == "previous-pass"


def test_keyframe_qc_rejects_corrupt_generated_bytes_without_calling_evaluator(tmp_path: Path) -> None:
    reference = tmp_path / "corrupt-reference.png"
    keyframe = tmp_path / "corrupt-keyframe.png"
    _write_image(reference, "white")
    keyframe.write_bytes(b"not an image")

    class Evaluator:
        fake_mode = True
        evaluator_name = "fake"
        evaluator_version = "1"
        model = "fake-vision"
        calls = 0

        def evaluate_multiple_images(self, *_: Any, **__: Any) -> dict[str, Any]:
            self.calls += 1
            raise AssertionError("corrupt artifact must not be sent to QC")

    evaluator = Evaluator()
    keyframe_sha = hashlib.sha256(keyframe.read_bytes()).hexdigest()
    reference_sha = hashlib.sha256(reference.read_bytes()).hexdigest()
    report = run_keyframe_qc(
        workflow_id="wf-corrupt-keyframe",
        package={
            "storyboard_id": "story-corrupt",
            "package_digest": "sha256:" + "e" * 64,
            "version": 1,
            "owner_user_id": "owner-corrupt",
            "project_id": "project-corrupt",
            "script": {"scenes": [{"scene_id": "scene-1", "subject_action": "Show product"}]},
            "generated_keyframes": [{
                "frame_id": "scene-1-start", "scene_id": "scene-1",
                "image_path": str(keyframe), "sha256": keyframe_sha,
                "prompt": "Show product", "reference_image_paths": [str(reference)],
                "reference_sha256s": [reference_sha],
            }],
        },
        product=NormalizedProduct(brand="Brand", model="Model"),
        evaluator=evaluator,
    )

    assert evaluator.calls == 0
    assert report.verdict is QCVerdict.FAIL
    assert report.findings["evaluator_calls"] == 0
    assert report.findings["items"][0]["hard_failure"] is True
    assert "corrupt" in report.findings["items"][0]["reason"].lower()


def test_keyframe_qc_marks_changed_approved_reference_unknown_without_calling_evaluator(
    tmp_path: Path,
) -> None:
    reference = tmp_path / "changed-reference.png"
    keyframe = tmp_path / "valid-keyframe.png"
    _write_image(reference, "white")
    _write_image(keyframe, "black")
    approved_reference_sha = hashlib.sha256(reference.read_bytes()).hexdigest()
    _write_image(reference, "red")

    class Evaluator:
        fake_mode = True
        evaluator_name = "fake"
        evaluator_version = "1"
        model = "fake-vision"
        calls = 0

        def evaluate_multiple_images(self, *_: Any, **__: Any) -> dict[str, Any]:
            self.calls += 1
            raise AssertionError("changed approved reference must not be sent to QC")

    evaluator = Evaluator()
    report = run_keyframe_qc(
        workflow_id="wf-reference-digest-mismatch",
        package={
            "storyboard_id": "story-reference-mismatch",
            "package_digest": "sha256:" + "f" * 64,
            "version": 1,
            "owner_user_id": "owner-reference-mismatch",
            "project_id": "project-reference-mismatch",
            "script": {"scenes": [{"scene_id": "scene-1", "subject_action": "Show product"}]},
            "generated_keyframes": [{
                "frame_id": "scene-1-start", "scene_id": "scene-1",
                "image_path": str(keyframe), "sha256": hashlib.sha256(keyframe.read_bytes()).hexdigest(),
                "prompt": "Show product", "reference_image_paths": [str(reference)],
                "reference_sha256s": [approved_reference_sha],
            }],
        },
        product=NormalizedProduct(brand="Brand", model="Model"),
        evaluator=evaluator,
    )

    assert evaluator.calls == 0
    assert report.verdict is QCVerdict.UNKNOWN
    assert report.findings["evaluator_calls"] == 0
    assert "approved reference is missing, changed, or undecodable" in report.findings["items"][0]["reason"].lower()


def test_keyframe_qc_does_not_call_evaluator_without_matching_approved_script_scene(
    tmp_path: Path,
) -> None:
    reference = tmp_path / "missing-script-reference.png"
    keyframe = tmp_path / "missing-script-keyframe.png"
    reference.write_bytes(b"reference")
    keyframe.write_bytes(b"keyframe")

    class Evaluator:
        fake_mode = True
        evaluator_name = "fake"
        evaluator_version = "1"
        model = "fake-vision"

        def __init__(self) -> None:
            self.calls = 0

        def evaluate_multiple_images(self, *_: Any, **__: Any) -> dict[str, Any]:
            self.calls += 1
            raise AssertionError("QC must not run without the approved script scene")

    evaluator = Evaluator()
    report = run_keyframe_qc(
        workflow_id="pv-missing-script-qc",
        package={
            "storyboard_id": "story-missing-script",
            "package_digest": "sha256:" + "d" * 64,
            "version": 1,
            "owner_user_id": "owner-missing-script",
            "project_id": "project-missing-script",
            "script": {"scenes": []},
            "generated_keyframes": [
                {
                    "frame_id": "scene-1-start",
                    "scene_id": "scene-1",
                    "image_path": str(keyframe),
                    "sha256": hashlib.sha256(keyframe.read_bytes()).hexdigest(),
                    "prompt": "Show the product on a table.",
                    "reference_image_paths": [str(reference)],
                }
            ],
        },
        product=NormalizedProduct(brand="Brand", model="Model"),
        evaluator=evaluator,
    )

    assert evaluator.calls == 0
    assert report.verdict == QCVerdict.UNKNOWN
    assert "approved script scene is missing" in report.findings["items"][0]["reason"]
