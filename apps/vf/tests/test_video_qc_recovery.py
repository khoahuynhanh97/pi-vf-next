from __future__ import annotations

import sqlite3
import json
import pytest

from video_factory.integrations.ports import VideoGenerationRequest
from video_factory.integrations.scene_renderer import SceneRenderOperationStore
from video_factory.storage_infra.sqlite import connect
from video_factory.handlers.registry import _prepare_video_qc_scene_retries
from video_factory.handlers import ProductVideoJobHandlers, JobSucceeded, create_product_video_topic_handlers
from video_factory.qc.validation import QCValidationError
from video_factory.workflows.envelope import OutboxEnvelope
from video_factory.workflows.repository import WorkflowRepository


def test_video_qc_scene_retry_creates_new_request_with_recovery_feedback(tmp_path):
    connection = sqlite3.connect(tmp_path / "video-recovery.db")
    try:
        store = SceneRenderOperationStore(connection)
        request = VideoGenerationRequest(
            request_id="scene-op-1",
            owner_user_id="owner-1",
            project_id="project-1",
            scene_id="scene-1",
            prompt="Show the approved product in the scripted scene.",
            duration_seconds=4,
        )
        store.mark_planned(request, render_batch_id="batch-1", scene_order=1)
        with connection:
            connection.execute(
                "UPDATE video_factory_scene_operation SET status='completed', "
                "provider_operation_id='provider-op-1', output_path='clip.mp4', "
                "output_sha256=?, provider_kind='vertex_ai' WHERE operation_id=?",
                ("a" * 64, request.request_id),
            )

        retry = store.retry_qc_failed_scene(
            request.request_id,
            qc_report_id="qc-video-fail-1",
            feedback="Regenerate this scene to match the approved storyboard.",
        )
        retry_request = store.request_from_record(retry)

        assert retry["status"] == "planned"
        assert retry["render_batch_id"] == "batch-1"
        assert retry["scene_order"] == 1
        assert retry_request.request_id != request.request_id
        assert "Regenerate this scene to match the approved storyboard." in retry_request.prompt
        assert retry["request_digest"] != store.get(request.request_id)["request_digest"]
    finally:
        connection.close()


def test_render_batch_poll_consumes_planned_video_qc_retry(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from video_factory.integrations import server

    db_path = tmp_path / "video-qc-poll.db"
    connection = sqlite3.connect(db_path)
    try:
        store = SceneRenderOperationStore(connection)
        original = VideoGenerationRequest(
            request_id="scene-poll-op-1",
            owner_user_id="owner-1",
            project_id="project-1",
            scene_id="scene-1",
            prompt="Match the approved storyboard.",
            duration_seconds=4,
        )
        store.mark_planned(original, render_batch_id="batch_poll_1", scene_order=1)
        with connection:
            connection.execute(
                "UPDATE video_factory_scene_operation SET status='completed', "
                "provider_operation_id='provider-scene-1', output_path='clip.mp4', "
                "output_sha256=?, provider_kind='vertex_ai' WHERE operation_id=?",
                ("d" * 64, original.request_id),
            )
        retry = store.retry_qc_failed_scene(
            original.request_id,
            qc_report_id="qc-video-poll-fail-1",
            feedback="Regenerate this scene to match the approved storyboard.",
        )
        retry_request = store.request_from_record(retry)
        assert retry["status"] == "planned"

        class _Repository:
            @staticmethod
            def get_render_batch(_job_id):
                return SimpleNamespace(
                    render_batch_id="batch_poll_1",
                    project_id="project-1",
                    owner_user_id="owner-1",
                    workflow_id="wf-1",
                    version=1,
                    status="running",
                    scene_count=1,
                    completed_scene_count=0,
                    composite_artifact_id=None,
                )

        class _Runtime:
            def __init__(self, **kwargs):
                self.store = kwargs["store"]

            def run(self, request):
                assert request.request_id == retry_request.request_id
                assert "Video QC recovery guidance:" in request.prompt
                return SimpleNamespace(
                    status="submitted",
                    provider_operation_id="provider-scene-retry-1",
                    error_code="",
                    error_message="",
                )

        monkeypatch.setattr(server, "_db_path", lambda: db_path)
        monkeypatch.setattr(server, "_service", lambda: SimpleNamespace(repository=_Repository()))
        monkeypatch.setattr(server, "_video_provider", lambda **_kwargs: object())
        monkeypatch.setattr(server, "SceneRenderRuntime", _Runtime)
        monkeypatch.setattr(
            "video_factory.storage.layout.get_workspace_layout",
            lambda: SimpleNamespace(root=tmp_path),
        )

        result = server.get_job_status("batch_poll_1")
        assert result["ok"] is True, result
        assert result["status"] == "running"
        assert result["job_id"] == "batch_poll_1"

        saved = store.get(retry_request.request_id)
        assert saved["status"] == "planned"
    finally:
        connection.close()


def test_video_qc_recovery_retries_only_failed_scene_three_times(tmp_path):
    connection = sqlite3.connect(tmp_path / "video-recovery-quota.db")
    try:
        store = SceneRenderOperationStore(connection)
        for scene_id, scene_order in (("scene-1", 1), ("scene-2", 2)):
            request = VideoGenerationRequest(
                request_id=f"{scene_id}-op-1",
                owner_user_id="owner-1",
                project_id="project-1",
                scene_id=scene_id,
                prompt=f"Show approved {scene_id}.",
                duration_seconds=4,
            )
            store.mark_planned(request, render_batch_id="batch-1", scene_order=scene_order)
            with connection:
                connection.execute(
                    "UPDATE video_factory_scene_operation SET status='completed', "
                    "provider_operation_id=?, output_path=?, output_sha256=?, "
                    "provider_kind='vertex_ai' WHERE operation_id=?",
                    (f"provider-{scene_id}", f"{scene_id}.mp4", "a" * 64, request.request_id),
                )

        for cycle in range(1, 5):
            digest = f"{cycle:064x}"
            report = {
                "report_id": f"qc-video-{cycle}",
                "stage": "video",
                "verdict": "fail",
                "artifact_digest": digest,
                "findings": {"visual_scenes": [{"scene_number": 1, "verdict": "fail"}]},
            }
            result = _prepare_video_qc_scene_retries(
                connection,
                workflow_id="wf-video-retry",
                render_artifact={"render_id": "batch-1", "sha256": digest},
                report=report,
                max_retries=3,
            )

            if cycle < 4:
                assert result["status"] == "retry"
                assert result["scene_ids"] == ["scene-1"]
                retry_op = store.get(result["operation_ids"][0])
                assert retry_op["attempt"] == cycle + 1
                retry_request = store.request_from_record(retry_op)
                assert "Video QC recovery guidance:" in retry_request.prompt
                if cycle == 1:
                    replay = _prepare_video_qc_scene_retries(
                        connection,
                        workflow_id="wf-video-retry",
                        render_artifact={"render_id": "batch-1", "sha256": digest},
                        report=report,
                        max_retries=3,
                    )
                    assert replay["operation_ids"] == result["operation_ids"]
                    assert connection.execute(
                        "SELECT COUNT(*) FROM video_factory_scene_operation WHERE scene_id='scene-1'"
                    ).fetchone()[0] == 2
                with connection:
                    connection.execute(
                        "UPDATE video_factory_scene_operation SET status='completed', "
                        "provider_operation_id=?, output_path=?, output_sha256=? WHERE operation_id=?",
                        (f"provider-retry-{cycle}", f"retry-{cycle}.mp4", "b" * 64, retry_op["operation_id"]),
                    )
            else:
                assert result["status"] == "exhausted"
                assert result["scene_ids"] == ["scene-1"]
                assert result["operation_ids"] == []

        attempts = connection.execute(
            "SELECT item_id, MAX(attempt_number) FROM workflow_qc_retries "
            "WHERE workflow_id=? AND retry_type='video_qc' GROUP BY item_id",
            ("wf-video-retry",),
        ).fetchall()
        assert [(row[0], row[1]) for row in attempts] == [("scene-1", 4)]
        assert connection.execute(
            "SELECT COUNT(*) FROM video_factory_scene_operation WHERE scene_id='scene-1'"
        ).fetchone()[0] == 4
        assert connection.execute(
            "SELECT COUNT(*) FROM video_factory_scene_operation WHERE scene_id='scene-2'"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_video_qc_classifies_technical_only_failure_for_local_recompose(tmp_path):
    connection = sqlite3.connect(tmp_path / "video-technical-only.db")
    try:
        result = _prepare_video_qc_scene_retries(
            connection, workflow_id="wf-technical-only",
            render_artifact={"render_id":"batch-technical", "sha256":"a" * 64},
            report={"report_id":"qc-technical-only", "stage":"video", "verdict":"fail",
                "artifact_digest":"a" * 64, "findings":{"technical_specs":{"technical_pass":False},
                    "sample_coverage":{"coverage_valid":True},
                    "visual_scenes":[{"scene_number":1,"verdict":"pass","score":100}]}},
            max_retries=2,
        )
        assert result["status"] == "technical_only"
        assert result["batch_id"] == "batch-technical"
        assert result["scene_ids"] == []
    finally:
        connection.close()


def test_video_qc_fail_handler_enqueues_render_of_new_scene_attempt(tmp_path, monkeypatch):
    from video_factory.handlers import registry

    connection = connect(tmp_path / "video-recovery-handler.db")
    try:
        repo = WorkflowRepository(connection)
        workflow_id = "wf-video-recovery-handler"
        digest = "c" * 64
        storyboard = {"schema_version": "storyboard-package-v2", "storyboard_id": "sb-1"}
        repo.commit_workflow(
            workflow_id,
            json.dumps({
                "state": "Rendering",
                "owner_user_id": "owner-1",
                "project_id": "project-1",
                "product_key": "product-1",
                "max_qc_retries": 3,
                "storyboard": storyboard,
                "qc_authorization": {"authorization_id": "auth-1"},
            }),
            [],
            expected_version=None,
        )
        store = SceneRenderOperationStore(connection)
        request = VideoGenerationRequest(
            request_id="scene-handler-op-1",
            owner_user_id="owner-1",
            project_id="project-1",
            scene_id="scene-1",
            prompt="Match the approved storyboard.",
            duration_seconds=4,
        )
        store.mark_planned(request, render_batch_id="batch-handler-1", scene_order=1)
        with connection:
            connection.execute(
                "UPDATE video_factory_scene_operation SET status='completed', "
                "provider_operation_id='provider-scene-1', output_path='clip.mp4', "
                "output_sha256=?, provider_kind='vertex_ai' WHERE operation_id=?",
                ("d" * 64, request.request_id),
            )

        artifact = {
            "render_id": "batch-handler-1",
            "sha256": digest,
            "artifact_id": "render-artifact-1",
            "artifact_version": 1,
            "owner_user_id": "owner-1",
            "project_id": "project-1",
        }
        report = {
            "report_id": "qc-video-handler-1",
            "stage": "video",
            "verdict": "fail",
            "artifact_digest": digest,
            "findings": {"visual_scenes": [{"scene_number": 1, "verdict": "fail"}]},
        }
        qc_result = {"status": "completed", "report": report, "reason": "Scene did not match visual intent"}
        monkeypatch.setattr(registry, "_run_qc_with_step_log", lambda *_args, **_kwargs: qc_result)
        monkeypatch.setattr(
            registry,
            "require_current_pass_report",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(QCValidationError("QC_NOT_PASS", "fail")),
        )

        service = ProductVideoJobHandlers(provider_mode="fake", workflow_repository=repo)
        service.render_job = lambda *_args, **_kwargs: JobSucceeded(artifact_ref=artifact)
        topic_handler = create_product_video_topic_handlers(
            workflow_repository=repo,
            handlers_service=service,
            provider_mode="configured",
            qc_service_factory=lambda _connection: object(),
        )["job.dispatch"]
        topic_handler(OutboxEnvelope(
            message_id="message-video-render-1",
            workflow_id=workflow_id,
            topic="job.dispatch",
            idempotency_key="render-attempt-1",
            schema_version="1",
            payload={"job_name": "product_video.render", "workflow_id": workflow_id, "owner_user_id": "owner-1"},
        ))

        aggregate, _version = repo.get_workflow(workflow_id)
        assert aggregate["state"] == "rendering"
        assert aggregate["active_render_job_id"] == "batch-handler-1"
        assert aggregate["video_qc_recovery"]["scene_ids"] == ["scene-1"]
        assert "operator_action" not in aggregate
        queued = connection.execute("SELECT message FROM workflow_outbox").fetchone()
        envelope = json.loads(queued[0])
        assert envelope["payload"]["job_name"] == "product_video.render"
        assert envelope["payload"]["kwargs"]["render_params"]["active_job_id"] == "batch-handler-1"
        assert "operator_action" not in aggregate
    finally:
        connection.close()


@pytest.mark.parametrize("checkpoint_available", [True, False])
def test_video_qc_technical_only_recomposes_only_with_scene_checkpoint(tmp_path, monkeypatch, checkpoint_available):
    from video_factory.handlers import registry

    connection = connect(tmp_path / "video-technical-recompose-handler.db")
    try:
        repo = WorkflowRepository(connection)
        workflow_id = "wf-video-technical-recompose"
        digest = "e" * 64
        repo.commit_workflow(workflow_id, json.dumps({
            "state":"Rendering", "owner_user_id":"owner-1", "project_id":"project-1",
            "product_key":"product-1", "max_qc_retries":2,
            "scene_plan":{"scenes":[{"scene_id":"scene-1"}]},
            "storyboard":{"schema_version":"storyboard-package-v2"},
        }), [], expected_version=0)
        artifact = {"render_id":"batch-technical", "sha256":digest, "artifact_id":"render-artifact",
                    "artifact_version":1, "owner_user_id":"owner-1", "project_id":"project-1"}
        report = {"report_id":"qc-technical-recompose", "stage":"video", "verdict":"fail",
                  "artifact_digest":digest, "findings":{"technical_specs":{"technical_pass":False},
                      "sample_coverage":{"coverage_valid":True},
                    "visual_scenes":[{"scene_number":1,"verdict":"pass","score":100}]}}
        monkeypatch.setattr(registry, "_run_qc_with_step_log",
            lambda *_args, **_kwargs:{"status":"completed", "report":report, "reason":"technical profile mismatch"})
        monkeypatch.setattr(registry, "require_current_pass_report",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(QCValidationError("QC_NOT_PASS", "fail")))
        monkeypatch.setattr(registry, "_prepare_video_qc_scene_retries", lambda *_args, **_kwargs:{
            "status":"technical_only", "batch_id":"batch-technical", "scene_ids":[], "operation_ids":[]})
        monkeypatch.setattr(registry, "_has_recompose_checkpoint", lambda *_args, **_kwargs: checkpoint_available)
        service = ProductVideoJobHandlers(provider_mode="fake", workflow_repository=repo)
        service.render_job = lambda *_args, **_kwargs: JobSucceeded(artifact_ref=artifact)
        handler = create_product_video_topic_handlers(workflow_repository=repo, handlers_service=service,
            provider_mode="configured", qc_service_factory=lambda _connection:object())["job.dispatch"]
        handler(OutboxEnvelope(message_id="message-technical-recompose", workflow_id=workflow_id,
            topic="job.dispatch", idempotency_key="render-technical-recompose", schema_version="1",
            payload={"job_name":"product_video.render", "workflow_id":workflow_id, "owner_user_id":"owner-1"}))
        aggregate, _ = repo.get_workflow(workflow_id)
        if checkpoint_available:
            assert aggregate["state"] == "rendering"
            assert aggregate["video_qc_technical_recompositions"] == [report["report_id"]]
            queued = json.loads(connection.execute("SELECT message FROM workflow_outbox").fetchone()[0])
            assert queued["payload"]["kwargs"]["render_params"] == {
                "active_job_id":"batch-technical", "force_recompose":True}
        else:
            assert aggregate["state"] == "WaitingForOperator"
            assert aggregate["failure_code"] == "VIDEO_QC_TECHNICAL_RECOMPOSE_NO_PROGRESS"
            assert "lineage" in aggregate["operator_reason"]
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='video_factory_scene_operation'").fetchone()[0] == 0
    finally:
        connection.close()
