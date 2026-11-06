"""Targeted test for post-request technical errors and reservation classification in Video QC."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json

from PIL import Image

from test_video_qc import _create_synthetic_mp4
from video_factory.qc.evaluator import QCResponseParseError
from video_factory.qc.image_qc import compute_artifact_digest
from video_factory.qc.models import NormalizedProduct, QCReport, QCStage, QCVerdict
from video_factory.qc.repository import QCReportRepository
from video_factory.qc.service import ProductVideoQCService
from video_factory.qc.video_qc import compute_file_sha256
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.repository import WorkflowRepository


class PostRequestParseErrorEvaluator:
    evaluator_name = "parse-error-test"
    evaluator_version = "1"
    model = "offline/vision"
    fake_mode = False

    def evaluate_multiple_images(self, **_kwargs):
        raise QCResponseParseError(
            raw_response="502 Bad Gateway: upstream connect error",
            finish_reason="other",
            parse_error="Failed to parse response JSON",
        )


def _setup_video_workflow(tmp_path: Path):
    video = tmp_path / "video.mp4"
    _create_synthetic_mp4(video, width=720, height=1280, duration_sec=1.0, fps=24)
    references = [tmp_path / "ref-1.png", tmp_path / "ref-2.png"]
    for img, color in zip(references, ("green", "yellow")):
        Image.new("RGB", (16, 16), color=color).save(img)
    storyboard = {"frames": [{"description": "Product test scene", "duration_seconds": 1.0}]}

    workflow_id = "wf-technical-unknown-test"
    pack = {
        "resource_pack_id": "pack-tech-test",
        "version": 1,
        "owner_user_id": "owner",
        "project_id": "project",
        "brand": "DAREU",
        "model": "LM115B",
        "assets_mapping": {f"a{i}": str(p) for i, p in enumerate(references)},
    }
    conn = connect(tmp_path / "test.db")
    aggregate = {
        "workflow_id": workflow_id,
        "owner_user_id": "owner",
        "project_id": "project",
        "state": "Rendering",
        "source_context": {"brand": "DAREU", "model": "LM115B"},
        "resource_pack": pack,
        "storyboard": storyboard,
        "render_result": {
            "owner_user_id": "owner",
            "project_id": "project",
            "workflow_id": workflow_id,
            "artifact_id": "render-vid",
            "artifact_version": 1,
            "sha256": compute_file_sha256(video),
            "file_path": str(video),
            "duration_seconds": 1.0,
        },
    }
    WorkflowRepository(conn).commit_workflow(workflow_id, json.dumps(aggregate), [], expected_version=None)
    repo = QCReportRepository(conn)
    repo.save_report(
        QCReport(
            report_id="qc-approved-img",
            workflow_id=workflow_id,
            stage=QCStage.IMAGE,
            artifact_id="pack-tech-test",
            artifact_version=1,
            artifact_digest=compute_artifact_digest(pack),
            reference_digests=[compute_file_sha256(p) for p in references],
            evaluator_name="offline",
            evaluator_version="1",
            rubric_version="1",
            created_at=datetime.now(timezone.utc).isoformat(),
            score=95,
            verdict=QCVerdict.PASS,
            findings={"eligible_reference_manifest": [{"eligible": True, "local_path": str(p)} for p in references]},
            owner_user_id="owner",
            project_id="project",
        )
    )
    return conn, workflow_id, video, references


def test_video_qc_post_request_schema_error_is_consumed_and_retryable(tmp_path, monkeypatch):
    conn, workflow_id, _video, _references = _setup_video_workflow(tmp_path)
    monkeypatch.setattr("video_factory.qc.service._file_url_to_path", lambda value: Path(value))
    monkeypatch.setattr("video_factory.qc.service._reference_url_to_path", lambda value: Path(value))

    evaluator = PostRequestParseErrorEvaluator()
    service = ProductVideoQCService(conn, evaluator=evaluator)
    auth = {
        "workflow_id": workflow_id,
        "run_id": "auth-parse-error-test",
        "scope": ["video"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
        "max_cost_usd": 0.10,
    }

    res = service.run(
        workflow_id,
        QCStage.VIDEO,
        provider_mode="configured",
        authorize_cost=True,
        max_cost_usd=0.10,
        authorization=auth,
    )

    report = res["report"]
    assert report.verdict == QCVerdict.UNKNOWN
    assert report.findings["usage_status"] == "consumed"
    assert report.findings["reconcile_required"] is False
    scene_diagnostics = report.findings["visual_scenes"][0]["diagnostics"]
    assert scene_diagnostics["schema_invalid"] is True
    assert scene_diagnostics["retry_allowed"] is True

    attempt = conn.execute(
        "SELECT status, reservation_id, cost_status FROM product_video_operation_attempt WHERE workflow_id = ?",
        (workflow_id,),
    ).fetchone()
    assert attempt is not None
    assert attempt["status"] == "response_invalid"
    assert attempt["reservation_id"] is None
    assert attempt["cost_status"] == "unknown"

    conn.close()
