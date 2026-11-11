from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from video_factory.integrations.ports import (
    VideoGenerationRequest,
    VideoGenerationResult,
)
from video_factory.integrations.scene_renderer import (
    SceneRenderOperationStore,
    SceneRenderRuntime,
    compile_grounded_scene_request,
)
from video_factory.creative.storyboard import storyboard_package_digest


def _request(
    keyframe: Path, *, duration: int = 4, keyframe_qc_score: int = 92
) -> VideoGenerationRequest:
    keyframe_sha = hashlib.sha256(keyframe.read_bytes()).hexdigest()
    source_sha = "b" * 64
    return compile_grounded_scene_request(
        owner_user_id="operator-1",
        workflow_id="workflow-1",
        project_id="project-1",
        scene={
            "scene_id": "scene-01",
            "duration_seconds": duration,
            "subject_action": "A commuter takes one earbud from the charging case",
            "product_interaction": "The hand lifts the right earbud naturally",
            "start_state": "Both earbuds rest inside the open charging case",
            "end_state": "One earbud is held beside the open case",
            "visual_change": "The right earbud moves from the case into the commuter's hand",
            "environment": "A clean morning commute desk",
            "camera_framing": "Medium close-up",
            "camera_movement": "Subtle handheld tracking",
            "overlay_text": "Pin 25 giờ",
        },
        keyframe={
            "frame_id": "scene-01-start",
            "scene_id": "scene-01",
            "image_path": str(keyframe),
            "sha256": keyframe_sha,
            "reference_sha256s": [source_sha],
        },
        keyframe_qc_receipt={
            "stage": "keyframe",
            "verdict": "pass",
            "score": keyframe_qc_score,
            "artifact_digest": "sha256:approved-package",
            "findings": {
                "items": [
                    {
                        "frame_id": "scene-01-start",
                        "sha256": keyframe_sha,
                        "reference_digests": [source_sha],
                        "score": keyframe_qc_score,
                        "verdict": "pass",
                        "hard_failure": False,
                    }
                ]
            },
        },
        package_digest="sha256:approved-package",
        product_identity="Baseus Bowie WM02 wireless earbuds",
        provider_model="veo-3.1-generate-001",
    )


def test_quarantine_inactive_flow_scene_preserves_unconfirmed_submit(tmp_path: Path) -> None:
    from video_factory.integrations.capacity import ProviderCapacityStore

    image = tmp_path / "start.png"
    image.write_bytes(b"keyframe")
    request = _request(image)
    project = "https://flow.google.com/project/00000000-0000-0000-0000-000000000001"
    with sqlite3.connect(":memory:") as connection:
        store = SceneRenderOperationStore(connection)
        capacity = ProviderCapacityStore(connection)
        store.mark_planned(request, render_batch_id="batch-1", scene_order=1)
        with connection:
            connection.execute(
                "UPDATE video_factory_scene_operation SET status='submitting', provider_kind='google_flow', "
                "provider_operation_id=? WHERE operation_id=?",
                (project, request.request_id),
            )
            connection.execute(
                "INSERT INTO video_factory_provider_capacity VALUES (?, ?, ?, ?, ?, ?)",
                ("google_flow", request.request_id, "", "0:inactive", 0, project),
            )
        result = store.quarantine_inactive_submission(
            request.request_id, evidence="Exact project inspected after the worker exited", confirmed_inactive=True,
        )
        assert result["status"] == "submitting"
        assert result["provider_operation_id"] == project
        assert result["error_code"] == "OPERATOR_QUARANTINED_UNCERTAIN"
        assert capacity.active("google_flow") == []


@pytest.mark.parametrize("inaccessible", [False, True])
def test_operator_can_retry_one_reconciled_flow_timeout(tmp_path: Path, inaccessible: bool) -> None:
    from video_factory.integrations.capacity import ProviderCapacityStore

    image = tmp_path / "start.png"
    image.write_bytes(b"keyframe")
    request = _request(image)
    project = "https://flow.google.com/project/00000000-0000-0000-0000-000000000001"
    with sqlite3.connect(":memory:") as connection:
        store = SceneRenderOperationStore(connection)
        capacity = ProviderCapacityStore(connection)
        store.mark_planned(request, render_batch_id="batch-1", scene_order=1)
        with connection:
            connection.execute(
                "UPDATE video_factory_scene_operation SET status='failed', provider_kind='google_flow', "
                "provider_operation_id=?, error_code='SCENE_TIMEOUT_EXPIRED' WHERE operation_id=?",
                (project, request.request_id),
            )
        with pytest.raises(ValueError, match="evidence"):
            store.retry_reconciled_flow_scene(request.request_id, reconciliation_evidence="")
        if inaccessible:
            with connection:
                connection.execute(
                    "UPDATE video_factory_scene_operation SET status='submitted', error_code='' WHERE operation_id=?",
                    (request.request_id,),
                )
                connection.execute(
                    "INSERT INTO video_factory_provider_capacity VALUES (?, ?, ?, ?, ?, ?)",
                    ("google_flow", request.request_id, "", "0:inactive", 0, project),
                )
            evidence = {"project_url": project, "reconciliation_evidence": "New account redirects to /404"}
            with pytest.raises(ValueError, match="exact inaccessible"):
                store.replace_inaccessible_flow_scene(
                    request.request_id, **{**evidence, "project_url": project[:-1] + "2"}
                )
            with connection:
                connection.execute("UPDATE video_factory_provider_capacity SET lease_expires_at=4102444800")
            with pytest.raises(ValueError, match="active Flow lease"):
                store.replace_inaccessible_flow_scene(request.request_id, **evidence)
            with connection:
                connection.execute("UPDATE video_factory_provider_capacity SET lease_expires_at=0")
            retry = store.replace_inaccessible_flow_scene(request.request_id, **evidence)
            assert store.replace_inaccessible_flow_scene(request.request_id, **evidence) == retry
            assert capacity.active("google_flow") == []
        else:
            retry = store.retry_reconciled_flow_scene(
                request.request_id, reconciliation_evidence="Project contains only START; credit activity verified."
            )
        assert retry["provider_kind"] == "google_flow"
        assert retry["attempt"] == 2
        assert retry["status"] == "planned"
        assert retry["provider_operation_id"] is None
        assert store.request_from_record(retry) == replace(request, request_id=retry["operation_id"])
        assert store.get(request.request_id)["provider_operation_id"] == project
        assert store.get(request.request_id)["error_code"] == (
            "OPERATOR_FLOW_PROJECT_INACCESSIBLE" if inaccessible else "OPERATOR_RECONCILED_FAILED"
        )


def test_compile_scene_uses_only_generated_keyframe_and_excludes_overlay_text(
    tmp_path: Path,
) -> None:
    keyframe = tmp_path / "generated-keyframe.png"
    keyframe.write_bytes(b"generated-keyframe")

    request = _request(keyframe)

    assert request.reference_image_paths == (str(keyframe.resolve()),)
    assert "takes one earbud" in request.prompt
    assert "Both earbuds rest" in request.prompt
    assert "One earbud is held" in request.prompt
    assert "right earbud moves" in request.prompt
    assert "Pin 25 giờ" not in request.prompt
    assert "watermark" in request.provider_options["negativePrompt"]
    assert "marketplace UI" in request.provider_options["negativePrompt"]
    assert "source-store branding" in request.provider_options["negativePrompt"]
    assert "Minh Gear" not in request.provider_options["negativePrompt"]


def test_compile_scene_accepts_contextually_passed_keyframe_at_keyframe_threshold(
    tmp_path: Path,
) -> None:
    keyframe = tmp_path / "square-keyframe.png"
    keyframe.write_bytes(b"generated-square-keyframe")

    request = _request(keyframe, keyframe_qc_score=65)

    assert request.reference_image_paths == (str(keyframe.resolve()),)


def test_compiled_scene_requests_approved_narration_without_requiring_audio(tmp_path: Path) -> None:
    keyframe = tmp_path / "generated-keyframe.png"
    keyframe.write_bytes(b"generated-keyframe")
    source_sha = "b" * 64
    keyframe_sha = hashlib.sha256(keyframe.read_bytes()).hexdigest()
    request = compile_grounded_scene_request(
        owner_user_id="operator-1",
        workflow_id="workflow-audio",
        project_id="project-1",
        scene={
            "scene_id": "scene-01",
            "duration_seconds": 4,
            "subject_action": "A commuter lifts one earbud",
            "product_interaction": "The hand lifts the earbud from its case",
            "start_state": "Both earbuds are in the open case",
            "end_state": "One earbud is held beside the case",
            "visual_change": "The earbud moves into the hand",
            "environment": "Morning desk",
            "narration": "Cất gọn tai nghe chỉ với một thao tác.",
        },
        keyframe={
            "frame_id": "scene-01-start",
            "scene_id": "scene-01",
            "image_path": str(keyframe),
            "sha256": keyframe_sha,
            "reference_sha256s": [source_sha],
        },
        keyframe_qc_receipt={
            "stage": "keyframe",
            "verdict": "pass",
            "score": 92,
            "artifact_digest": "sha256:approved-package",
            "findings": {"items": [{
                "frame_id": "scene-01-start",
                "sha256": keyframe_sha,
                "reference_digests": [source_sha],
                "score": 92,
                "verdict": "pass",
                "hard_failure": False,
            }]},
        },
        package_digest="sha256:approved-package",
        product_identity="Baseus Bowie WM02",
        provider_model="veo-3.1-generate-001",
    )
    assert request.request_audio is True
    assert "Cất gọn tai nghe chỉ với một thao tác." in request.prompt
    assert "Do not add subtitles" in request.prompt


@pytest.mark.parametrize(
    ("receipt_patch", "message"),
    [
        ({"verdict": "fail"}, "QC PASS"),
        ({"hard_failure": True}, "hard failure"),
        ({"artifact_digest": "sha256:stale"}, "digest"),
    ],
)
def test_compile_scene_rejects_stale_or_failed_keyframe_qc(
    tmp_path: Path, receipt_patch: dict[str, object], message: str
) -> None:
    keyframe = tmp_path / "generated-keyframe.png"
    keyframe.write_bytes(b"generated-keyframe")
    keyframe_sha = hashlib.sha256(keyframe.read_bytes()).hexdigest()
    source_sha = "b" * 64
    receipt = {
        "stage": "keyframe",
        "verdict": "pass",
        "score": 92,
        "artifact_digest": "sha256:approved-package",
        "findings": {
            "items": [
                {
                    "frame_id": "scene-01-start",
                    "sha256": keyframe_sha,
                    "reference_digests": [source_sha],
                    "score": 92,
                    "verdict": "pass",
                    "hard_failure": False,
                }
            ]
        },
    }
    receipt.update(receipt_patch)

    with pytest.raises(ValueError, match=message):
        compile_grounded_scene_request(
            owner_user_id="operator-1",
            workflow_id="workflow-1",
            project_id="project-1",
            scene={
                "scene_id": "scene-01",
                "duration_seconds": 4,
                "subject_action": "A commuter takes one earbud from the case",
                "product_interaction": "The hand lifts the earbud",
                "start_state": "Earbuds rest in the open case",
                "end_state": "One earbud is held beside the case",
                "visual_change": "One earbud moves into the hand",
                "environment": "Morning commute desk",
                "camera_framing": "Medium close-up",
                "camera_movement": "Subtle handheld tracking",
            },
            keyframe={
                "frame_id": "scene-01-start",
                "scene_id": "scene-01",
                "image_path": str(keyframe),
                "sha256": keyframe_sha,
                "reference_sha256s": [source_sha],
            },
            keyframe_qc_receipt=receipt,
            package_digest="sha256:approved-package",
            product_identity="Baseus Bowie WM02 wireless earbuds",
            provider_model="veo-3.1-generate-001",
        )


@pytest.mark.parametrize("duration", [3, 5, 7, 10])
def test_compile_scene_rejects_unsupported_duration_before_submit(
    tmp_path: Path, duration: int
) -> None:
    keyframe = tmp_path / "generated-keyframe.png"
    keyframe.write_bytes(b"generated-keyframe")

    with pytest.raises(ValueError, match="allowed values are 4, 6, 8"):
        _request(keyframe, duration=duration)


def test_scene_operation_id_is_deterministic_sha256(tmp_path: Path) -> None:
    keyframe = tmp_path / "generated-keyframe.png"
    keyframe.write_bytes(b"generated-keyframe")

    first = _request(keyframe)
    second = _request(keyframe)

    assert first.request_id == second.request_id
    assert first.request_id == "video-scene-" + hashlib.sha256(
        (
            "workflow-1|scene-01|sha256:approved-package|"
            "veo-3.1-generate-001|4"
        ).encode()
    ).hexdigest()[:24]


class _ResumeProvider:
    provider_kind = "google_flow"

    def __init__(self, provider_kind: str = "google_flow") -> None:
        self.provider_kind = provider_kind
        self.submit_calls: list[str] = []
        self.poll_calls: list[str] = []

    def submit(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        self.submit_calls.append(request.request_id)
        raise AssertionError("resume must not submit another paid operation")

    def get_status(self, operation_id: str) -> VideoGenerationResult:
        self.poll_calls.append(operation_id)
        return VideoGenerationResult(
            request_id=operation_id,
            status="running",
            provider_operation_id=operation_id,
        )


def test_vertex_resume_polls_the_durably_pinned_official_provider(tmp_path: Path) -> None:
    keyframe = tmp_path / "generated-keyframe.png"
    keyframe.write_bytes(b"generated-keyframe")
    request = _request(keyframe)
    store = SceneRenderOperationStore(sqlite3.connect(":memory:"))
    store.mark_planned(request)
    claim = store.claim_provider_for_submission(request, "vertex_ai")
    assert claim.can_submit
    assert store.get(request.request_id)["submitted_at"]
    store.save_result(
        request,
        VideoGenerationResult(
            request_id=request.request_id,
            status="submitted",
            provider_operation_id="operations/video-scene-01",
        ),
    )
    provider = _ResumeProvider("vertex_ai")

    result = SceneRenderRuntime(store=store, provider=provider).run(request)

    assert result.status == "running"
    assert provider.submit_calls == []
    assert provider.poll_calls == ["operations/video-scene-01"]


def test_download_failure_repolls_same_scene_operation_without_new_generation(tmp_path: Path) -> None:
    keyframe = tmp_path / "generated-keyframe.png"
    keyframe.write_bytes(b"generated-keyframe")
    request = _request(keyframe)
    output = tmp_path / "scene.mp4"
    output.write_bytes(b"downloaded video bytes")
    store = SceneRenderOperationStore(sqlite3.connect(":memory:"))

    class Provider:
        provider_kind = "google_flow"

        def __init__(self) -> None:
            self.submit_calls = 0
            self.status_calls = 0

        def submit(self, current: VideoGenerationRequest) -> VideoGenerationResult:
            self.submit_calls += 1
            return VideoGenerationResult(
                request_id=current.request_id, status="submitted",
                provider_operation_id="https://flow.google.com/project/00000000-0000-0000-0000-000000000001",
            )

        def get_status(self, provider_operation_id: str) -> VideoGenerationResult:
            self.status_calls += 1
            if self.status_calls == 1:
                raise RuntimeError("GOOGLE_FLOW_DOWNLOAD_FAILED")
            return VideoGenerationResult(
                request_id=request.request_id, status="completed",
                provider_operation_id=provider_operation_id, video_path=str(output),
            )

    provider = Provider()
    runtime = SceneRenderRuntime(store=store, provider=provider)
    assert runtime.run(request).status == "submitted"
    with pytest.raises(RuntimeError, match="GOOGLE_FLOW_DOWNLOAD_FAILED"):
        runtime.run(request)
    assert store.get(request.request_id)["status"] == "submitted"

    completed = runtime.run(request)
    assert completed.status == "completed"
    assert store.get(request.request_id)["output_path"] == str(output)
    assert provider.submit_calls == 1
    assert provider.status_calls == 2


def test_checkpoint_without_provider_id_refuses_ambiguous_resubmit(
    tmp_path: Path,
) -> None:
    keyframe = tmp_path / "generated-keyframe.png"
    keyframe.write_bytes(b"generated-keyframe")
    request = _request(keyframe)
    store = SceneRenderOperationStore(sqlite3.connect(":memory:"))
    store.mark_submitting(request)
    provider = _ResumeProvider()

    result = SceneRenderRuntime(store=store, provider=provider).run(request)

    assert result.status == "failed"
    assert result.error_code == "RECONCILIATION_REQUIRED"
    assert provider.submit_calls == []


def test_vertex_unknown_outcome_keeps_provider_pin_and_never_falls_back(
    tmp_path: Path,
) -> None:
    keyframe = tmp_path / "generated-keyframe.png"
    keyframe.write_bytes(b"generated-keyframe")
    request = _request(keyframe)
    store = SceneRenderOperationStore(sqlite3.connect(":memory:"))

    class UnknownVertex:
        provider_kind = "vertex_ai"
        supports_end_keyframe = True

        def __init__(self) -> None:
            self.submit_calls = 0

        def is_available(self) -> bool:
            return True

        def submit(self, req: VideoGenerationRequest) -> VideoGenerationResult:
            self.submit_calls += 1
            return VideoGenerationResult(
                request_id=req.request_id,
                status="unknown",
                error_code="NETWORK_UNKNOWN_OUTCOME",
                error_message="POST outcome is ambiguous",
            )

        def get_status(self, _: str) -> VideoGenerationResult:
            raise AssertionError("ambiguous operation must be reconciled before polling")

    class MustNotFallback:
        provider_kind = "google_flow"
        supports_end_keyframe = True
        submit_calls = 0

        def is_available(self) -> bool:
            return True

        def submit(self, _: VideoGenerationRequest) -> VideoGenerationResult:
            self.submit_calls += 1
            raise AssertionError("Flow fallback after Vertex submit is forbidden")

        def get_status(self, _: str) -> VideoGenerationResult:
            raise AssertionError("Flow must not poll a Vertex operation")

    vertex = UnknownVertex()
    flow = MustNotFallback()
    runtime = SceneRenderRuntime(
        store=store,
        provider={"vertex_ai": vertex, "google_flow": flow},
        preferred_provider_kinds=("vertex_ai", "google_flow"),
    )

    first = runtime.run(request)
    second = runtime.run(request)

    assert first.status == "unknown"
    assert first.error_code == "NETWORK_UNKNOWN_OUTCOME"
    assert second.error_code == "RECONCILIATION_REQUIRED"
    assert vertex.submit_calls == 1
    assert flow.submit_calls == 0
    saved = store.get(request.request_id)
    assert saved is not None
    assert saved["provider_kind"] == "vertex_ai"
    assert saved["submitted_at"]


def test_vertex_submission_limit_counts_unknown_sends_and_blocks_sixth(
    tmp_path: Path,
) -> None:
    keyframe = tmp_path / "approved-keyframe.png"
    keyframe.write_bytes(b"approved-frame")
    base = _request(keyframe)
    store = SceneRenderOperationStore(sqlite3.connect(":memory:"))

    for index in range(5):
        request = replace(base, request_id=f"vertex-send-{index}")
        store.mark_planned(request)
        claim = store.claim_provider_for_submission(request, "vertex_ai")
        assert claim.status == "claimed"

    sixth = replace(base, request_id="vertex-send-6")
    store.mark_planned(sixth)
    claim = store.claim_provider_for_submission(sixth, "vertex_ai")

    assert claim.status == "send_limit_exceeded"
    assert store.get(sixth.request_id)["status"] == "planned"
    assert store.get(sixth.request_id)["submitted_at"] is None


def test_creative_scene_cap_does_not_group_distinct_unordered_scenes(tmp_path: Path) -> None:
    keyframe = tmp_path / "approved-keyframe.png"
    keyframe.write_bytes(b"approved-frame")
    base = _request(keyframe)
    store = SceneRenderOperationStore(sqlite3.connect(":memory:"))

    for index in range(4):
        request = replace(base, request_id=f"scene-request-{index}", scene_id=f"scene-{index}",
            provider_options={**base.provider_options, "creative_mode":"four_agent"})
        store.mark_planned(request)
        claim = store.claim_provider_for_submission(request, "google_flow")
        assert claim.status == "claimed", claim.message


def _render_fixture(tmp_path: Path):
    from video_factory.integrations.domain import (
        AssetReference,
        FramePrompt,
        ProjectStatus,
        ResourcePack,
        Storyboard,
        StoryboardApprovalStatus,
        StoryboardFrame,
        VideoFactoryProject,
    )

    source = tmp_path / "raw-listing.jpg"
    source.write_bytes(b"raw-listing-image")
    keyframe = tmp_path / "generated-keyframe.png"
    keyframe.write_bytes(b"generated-keyframe")
    keyframe_sha = hashlib.sha256(keyframe.read_bytes()).hexdigest()
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    frame = StoryboardFrame(
        frame_id="scene-01-start",
        scene_id="scene-01",
        order=1,
        label="Real use",
        purpose="Show use",
        visual_state="Earbuds in case",
        subject_action="Take one earbud",
        product_state="Open case",
        character_state="Hand ready",
        context="Morning desk",
        camera_intention="Medium close-up",
        prompt=FramePrompt(
            positive_prompt="Legacy prompt that must not be used",
            reference_asset_ids=("source-1",),
        ),
    )
    project = VideoFactoryProject(
        id="project-1",
        owner_user_id="operator-1",
        status=ProjectStatus.STORYBOARD_APPROVED,
        resource_pack=ResourcePack(
            id="pack-1",
            owner_user_id="operator-1",
            product_references=(
                AssetReference("source-1", str(source), {"qc_eligible": True, "qc_sha256": source_sha}),
            ),
            primary_product_asset_id="source-1",
            product_identity_description="Baseus Bowie WM02 wireless earbuds",
        ),
        storyboard=Storyboard(
            storyboard_id="storyboard-1",
            project_id="project-1",
            frames=(frame,),
            approval_status=StoryboardApprovalStatus.APPROVED,
        ),
    )
    scene = {
        "scene_id": "scene-01",
        "duration_seconds": 4,
        "subject_action": "A commuter takes one earbud from the charging case",
        "product_interaction": "The hand lifts the right earbud naturally",
        "start_state": "Both earbuds rest inside the open charging case",
        "end_state": "One earbud is held beside the open case",
        "visual_change": "The right earbud moves from the case into the commuter's hand",
        "environment": "A clean morning commute desk",
        "camera_framing": "Medium close-up",
        "camera_movement": "Subtle handheld tracking",
        "overlay_text": "Pin 25 giờ",
    }
    keyframe_item = {
        "frame_id": "scene-01-start",
        "scene_id": "scene-01",
        "image_path": str(keyframe),
        "sha256": keyframe_sha,
        "reference_sha256s": [source_sha],
    }
    receipt = {
        "report_id": "qc-keyframe-1",
        "stage": "keyframe",
        "verdict": "pass",
        "score": 92,
        "artifact_digest": "sha256:approved-package",
        "findings": {
            "items": [
                {
                    "frame_id": "scene-01-start",
                    "sha256": keyframe_sha,
                    "reference_digests": [source_sha],
                    "score": 92,
                    "verdict": "pass",
                    "hard_failure": False,
                }
            ]
        },
    }
    package = {
        "schema_version": "storyboard-package-v2",
        "workflow_id": "workflow-1",
        "project_id": "project-1",
        "package_digest": "sha256:approved-package",
        "script": {"scenes": [scene]},
        "generated_keyframes": [keyframe_item],
        "keyframe_qc_receipt": receipt,
    }
    package["package_digest"] = storyboard_package_digest(package)
    receipt["artifact_digest"] = package["package_digest"]
    return project, package, source, keyframe


class _FakeRepository:
    def __init__(self, project) -> None:
        self.project = project
        self.batches = {}
        self.artifacts = {}

    def save(self, project):
        self.project = project
        return project

    def save_render_batch(self, batch):
        self.batches[batch.render_batch_id] = batch
        return batch

    def get_render_batch(self, batch_id: str):
        return self.batches.get(batch_id)

    def save_artifact(self, artifact):
        self.artifacts[artifact.artifact_id] = artifact
        return artifact

    def get_artifact(self, artifact_id: str):
        return self.artifacts.get(artifact_id)


class _FakeService:
    def __init__(self, project) -> None:
        self.repository = _FakeRepository(project)

    def get_project(self, _owner: str, _project: str):
        return self.repository.project


class _SubmitThenPollProvider:
    model = "veo-3.1-generate-001"
    provider_kind = "google_flow"

    def __init__(self) -> None:
        self.provider_kind = "google_flow"
        self.submit_calls: list[VideoGenerationRequest] = []
        self.poll_calls: list[str] = []

    def submit(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        self.submit_calls.append(request)
        return VideoGenerationResult(
            request_id=request.request_id,
            status="submitted",
            provider_operation_id="operations/video-1",
        )

    def get_status(self, operation_id: str) -> VideoGenerationResult:
        self.poll_calls.append(operation_id)
        return VideoGenerationResult(
            request_id=operation_id,
            status="running",
            provider_operation_id=operation_id,
        )


@pytest.mark.parametrize("bound_low_qc", [False, True])
def test_server_render_checkpoints_each_scene_and_resume_polls_without_resubmit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound_low_qc: bool,
) -> None:
    from video_factory.integrations import server
    from video_factory.workflows.budgets import (
        BudgetPolicy,
        init_default_engine,
        reserve_budget,
    )

    project, package, source, keyframe = _render_fixture(tmp_path)
    db_path = tmp_path / "factory.sqlite"
    if bound_low_qc:
        from video_factory.storage_infra.sqlite import connect
        from video_factory.workflows.repository import WorkflowRepository

        Image.new("RGB", (108, 192), "red").save(keyframe)
        package["generated_keyframes"][0]["sha256"] = hashlib.sha256(keyframe.read_bytes()).hexdigest()
        package["package_digest"] = storyboard_package_digest(package)
        package["keyframe_qc_receipt"].update(
            verdict="unknown", artifact_digest=package["package_digest"], findings={},
        )
        connection = connect(db_path)
        try:
            WorkflowRepository(connection).commit_workflow(
                workflow_id="workflow-1",
                aggregate_json=json.dumps({"creative_policy": {"qc_level": "low"}, "storyboard": package}),
                events=[], expected_version=None,
            )
        finally:
            connection.close()
        monkeypatch.setenv("HERMES_APPROVAL_DB_PATH", str(db_path))
    service = _FakeService(project)
    server.set_service(service)
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_path))
    init_default_engine(BudgetPolicy(max_total=1000, per_channel={"video_generation": 1000}))
    reservation = reserve_budget(
        "video_generation", 100, owner_user_id="operator-1", scope_id="project-1"
    )
    provider = _SubmitThenPollProvider()
    try:
        first = server.render(
            storyboard_result=package,
            owner_user_id="operator-1",
            project_id="project-1",
            admission_context={"reservation_id": reservation.reservation_id},
            provider=provider,
        )
        service.repository.project = replace(
            service.repository.project,
            draft_video_asset_id=None,
        )
        second = server.render(
            storyboard_result=package,
            owner_user_id="operator-1",
            project_id="project-1",
            admission_context={"reservation_id": reservation.reservation_id},
            provider=provider,
        )
    finally:
        server.set_service(None)

    assert first["ok"] is True
    assert second["ok"] is True
    assert len(provider.submit_calls) == 1
    assert provider.poll_calls == ["operations/video-1"]
    assert provider.submit_calls[0].reference_image_paths == (str(keyframe.resolve()),)
    assert str(source.resolve()) not in provider.submit_calls[0].reference_image_paths


def test_server_render_acceptance_unknown_requires_reconciliation_and_preserves_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from video_factory.integrations import server
    from video_factory.workflows.budgets import (
        BudgetPolicy,
        init_default_engine,
        reserve_budget,
    )
    from video_factory.job_contracts import TerminalJobError

    project, package, source, keyframe = _render_fixture(tmp_path)
    service = _FakeService(project)
    server.set_service(service)
    db_file = tmp_path / "factory.sqlite"
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_file))
    init_default_engine(BudgetPolicy(max_total=1000, per_channel={"video_generation": 1000}))
    reservation = reserve_budget(
        "video_generation", 100, owner_user_id="operator-1", scope_id="project-1"
    )

    class _UnknownAcceptanceProvider:
        model = "veo-3.1-generate-001"
        provider_kind = "google_flow"

        def __init__(self) -> None:
            self.submit_calls: list[VideoGenerationRequest] = []

        def submit(self, request: VideoGenerationRequest) -> VideoGenerationResult:
            self.submit_calls.append(request)
            err = TerminalJobError(
                "Flow paid Send has no confirmed video tile; reconcile the existing project before retrying",
                code="GOOGLE_FLOW_VIDEO_ACCEPTANCE_UNKNOWN",
            )
            err.provider_operation_id = "https://flow.google.com/project/00000000-0000-0000-0000-000000000001"
            raise err

        def get_status(self, provider_operation_id: str) -> VideoGenerationResult:
            return VideoGenerationResult(
                request_id="",
                status="failed",
                error_code="RECONCILIATION_REQUIRED",
                provider_operation_id=provider_operation_id,
            )

    provider = _UnknownAcceptanceProvider()
    try:
        result = server.render(
            storyboard_result=package,
            owner_user_id="operator-1",
            project_id="project-1",
            admission_context={"reservation_id": reservation.reservation_id},
            provider=provider,
        )
    finally:
        server.set_service(None)

    assert result["ok"] is False
    assert result["status"] == "reconciliation_required"
    assert result["code"] in ("RECONCILIATION_REQUIRED", "GOOGLE_FLOW_VIDEO_ACCEPTANCE_UNKNOWN")
    assert len(provider.submit_calls) == 1

    # Operation must be preserved in submitting with provider_operation_id bound
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM video_factory_scene_operation").fetchone()
    assert row is not None
    assert row["status"] == "submitting"
    assert row["provider_operation_id"] == "https://flow.google.com/project/00000000-0000-0000-0000-000000000001"
    assert row["submitted_at"] is not None
    conn.close()


def test_server_render_resume_after_acceptance_unknown_does_not_send_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from video_factory.integrations import server
    from video_factory.workflows.budgets import (
        BudgetPolicy,
        init_default_engine,
        reserve_budget,
    )
    from video_factory.job_contracts import TerminalJobError

    project, package, source, keyframe = _render_fixture(tmp_path)
    service = _FakeService(project)
    server.set_service(service)
    db_file = tmp_path / "factory.sqlite"
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_file))
    init_default_engine(BudgetPolicy(max_total=1000, per_channel={"video_generation": 1000}))
    reservation = reserve_budget(
        "video_generation", 100, owner_user_id="operator-1", scope_id="project-1"
    )

    class _ResumeProvider:
        model = "veo-3.1-generate-001"
        provider_kind = "google_flow"

        def __init__(self) -> None:
            self.submit_calls: list[VideoGenerationRequest] = []
            self.get_status_calls: list[str] = []

        def submit(self, request: VideoGenerationRequest) -> VideoGenerationResult:
            self.submit_calls.append(request)
            err = TerminalJobError(
                "Flow paid Send has no confirmed video tile",
                code="GOOGLE_FLOW_VIDEO_ACCEPTANCE_UNKNOWN",
            )
            err.provider_operation_id = "https://flow.google.com/project/00000000-0000-0000-0000-000000000001"
            raise err

        def get_status(self, provider_operation_id: str) -> VideoGenerationResult:
            self.get_status_calls.append(provider_operation_id)
            return VideoGenerationResult(
                request_id="",
                status="failed",
                error_code="RECONCILIATION_REQUIRED",
                provider_operation_id=provider_operation_id,
            )

    provider = _ResumeProvider()
    try:
        # First call fails acceptance after Send
        first = server.render(
            storyboard_result=package,
            owner_user_id="operator-1",
            project_id="project-1",
            admission_context={"reservation_id": reservation.reservation_id},
            provider=provider,
        )
        assert first["status"] == "reconciliation_required"
        assert len(provider.submit_calls) == 1

        # Second call with the same request resumes and must NOT send a second time
        second = server.render(
            storyboard_result=package,
            owner_user_id="operator-1",
            project_id="project-1",
            admission_context={"reservation_id": reservation.reservation_id},
            provider=provider,
        )
    finally:
        server.set_service(None)

    assert second["status"] == "reconciliation_required"
    assert len(provider.submit_calls) == 1  # Never sent twice!
    assert len(provider.get_status_calls) == 1
    assert provider.get_status_calls[0] == "https://flow.google.com/project/00000000-0000-0000-0000-000000000001"


def test_server_render_v2_fails_closed_without_immutable_keyframe_qc_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from video_factory.integrations import server
    from video_factory.workflows.budgets import (
        BudgetPolicy,
        init_default_engine,
        reserve_budget,
    )

    project, package, _source, _keyframe = _render_fixture(tmp_path)
    package.pop("keyframe_qc_receipt")
    server.set_service(_FakeService(project))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(tmp_path / "factory.sqlite"))
    init_default_engine(BudgetPolicy(max_total=1000, per_channel={"video_generation": 1000}))
    reservation = reserve_budget(
        "video_generation", 100, owner_user_id="operator-1", scope_id="project-1"
    )
    provider = _SubmitThenPollProvider()
    try:
        result = server.render(
            storyboard_result=package,
            owner_user_id="operator-1",
            project_id="project-1",
            admission_context={"reservation_id": reservation.reservation_id},
            provider=provider,
        )
    finally:
        server.set_service(None)

    assert result["ok"] is False
    assert result["code"] == "KEYFRAME_QC_REQUIRED"
    assert provider.submit_calls == []


def test_server_render_v2_rejects_scene_mutated_after_keyframe_qc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from video_factory.integrations import server
    from video_factory.workflows.budgets import (
        BudgetPolicy,
        init_default_engine,
        reserve_budget,
    )

    project, package, _source, _keyframe = _render_fixture(tmp_path)
    package["script"]["scenes"][0]["subject_action"] = "A different unapproved action"
    server.set_service(_FakeService(project))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(tmp_path / "factory.sqlite"))
    init_default_engine(BudgetPolicy(max_total=1000, per_channel={"video_generation": 1000}))
    reservation = reserve_budget(
        "video_generation", 100, owner_user_id="operator-1", scope_id="project-1"
    )
    provider = _SubmitThenPollProvider()
    try:
        result = server.render(
            storyboard_result=package,
            owner_user_id="operator-1",
            project_id="project-1",
            admission_context={"reservation_id": reservation.reservation_id},
            provider=provider,
        )
    finally:
        server.set_service(None)

    assert result["ok"] is False
    assert result["code"] == "STORYBOARD_PACKAGE_CHANGED"
    assert provider.submit_calls == []


def test_server_render_never_uses_local_listing_image_as_video_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from video_factory.integrations import server
    from video_factory.workflows.budgets import (
        BudgetPolicy,
        init_default_engine,
        reserve_budget,
    )

    project, _package, source, _keyframe = _render_fixture(tmp_path)
    server.set_service(_FakeService(project))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(tmp_path / "factory.sqlite"))
    init_default_engine(BudgetPolicy(max_total=1000, per_channel={"video_generation": 1000}))
    reservation = reserve_budget(
        "video_generation", 100, owner_user_id="operator-1", scope_id="project-1"
    )
    provider = _SubmitThenPollProvider()
    try:
        result = server.render(
            storyboard_result=None,
            owner_user_id="operator-1",
            project_id="project-1",
            admission_context={"reservation_id": reservation.reservation_id},
            render_params={"reference_image_paths": [str(source)]},
            provider=provider,
        )
    finally:
        server.set_service(None)

    assert result["ok"] is False
    assert result["code"] == "KEYFRAME_PACKAGE_REQUIRED"
    assert provider.submit_calls == []
