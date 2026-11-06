"""Final video QC must retain ambiguous provider submissions for reconciliation."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from test_video_qc import _create_synthetic_mp4
from video_factory.qc.image_qc import compute_artifact_digest
from video_factory.qc.models import NormalizedProduct, QCReport, QCStage, QCVerdict
from video_factory.qc.repository import QCReportRepository
from video_factory.qc.service import ProductVideoQCService
from video_factory.qc.video_qc import compute_file_sha256, run_video_qc
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.repository import WorkflowRepository


class TimeoutAfterSend:
    evaluator_name = "offline-test"
    evaluator_version = "1"
    model = "offline/vision"
    fake_mode = False

    def __init__(self):
        self.calls = 0

    def evaluate_multiple_images(self, **_kwargs):
        self.calls += 1
        raise TimeoutError("request timed out after send")


def _assets(tmp_path: Path, *, scenes: int = 1):
    video = tmp_path / "video.mp4"
    _create_synthetic_mp4(video, width=720, height=1280, duration_sec=float(scenes), fps=24)
    references = [tmp_path / "ref-a.png", tmp_path / "ref-b.png"]
    for image, color in zip(references, ("red", "blue")):
        Image.new("RGB", (16, 16), color=color).save(image)
    storyboard = {"frames": [
        {"description": f"Product view {number}", "duration_seconds": 1.0}
        for number in range(1, scenes + 1)
    ]}
    return video, references, storyboard


def test_video_qc_timeout_after_send_stops_before_next_scene(tmp_path):
    video, references, storyboard = _assets(tmp_path, scenes=2)
    evaluator = TimeoutAfterSend()

    report = run_video_qc(
        "wf-timeout", video, storyboard,
        NormalizedProduct(brand="AULA", model="F75"), references,
        expected_duration=2.0, evaluator=evaluator,
        owner_user_id="owner", project_id="project", artifact_id="render",
    )

    assert evaluator.calls == 1
    assert report.verdict is QCVerdict.UNKNOWN
    assert len(report.findings["visual_scenes"]) == 1
    scene = report.findings["visual_scenes"][0]
    assert scene["verdict"] == "unknown"
    assert scene["hard_failure"] is False
    assert scene["diagnostics"]["reconcile_required"] is True
    assert "retry_scene_1" not in report.findings["recovery_actions"]


def test_governed_video_qc_timeout_keeps_cost_unknown_and_blocks_second_request(tmp_path, monkeypatch):
    video, references, storyboard = _assets(tmp_path)
    workflow_id = "wf-video-timeout"
    pack = {
        "resource_pack_id": "pack-video-timeout", "version": 1,
        "owner_user_id": "owner", "project_id": "project",
        "brand": "AULA", "model": "F75",
        "assets_mapping": {f"a{number}": str(path) for number, path in enumerate(references)},
    }
    conn = connect(tmp_path / "qc.db")
    aggregate = {
        "workflow_id": workflow_id, "owner_user_id": "owner", "project_id": "project",
        "state": "Rendering", "source_context": {"brand": "AULA", "model": "F75"},
        "resource_pack": pack, "storyboard": storyboard,
        "render_result": {
            "owner_user_id": "owner", "project_id": "project", "workflow_id": workflow_id,
            "artifact_id": "render-video", "artifact_version": 1,
            "sha256": compute_file_sha256(video), "file_path": str(video),
            "duration_seconds": 1.0,
        },
    }
    WorkflowRepository(conn).commit_workflow(workflow_id, json.dumps(aggregate), [], expected_version=None)
    repo = QCReportRepository(conn)
    repo.save_report(QCReport(
        report_id="qc-approved-images", workflow_id=workflow_id, stage=QCStage.IMAGE,
        artifact_id="pack-video-timeout", artifact_version=1,
        artifact_digest=compute_artifact_digest(pack),
        reference_digests=[compute_file_sha256(path) for path in references],
        evaluator_name="offline", evaluator_version="1", rubric_version="1",
        created_at=datetime.now(timezone.utc).isoformat(), score=90, verdict=QCVerdict.PASS,
        findings={"eligible_reference_manifest": [
            {"eligible": True, "local_path": str(path)} for path in references
        ]}, owner_user_id="owner", project_id="project",
    ))
    monkeypatch.setattr("video_factory.qc.service._file_url_to_path", lambda value: Path(value))
    monkeypatch.setattr("video_factory.qc.service._reference_url_to_path", lambda value: Path(value))
    evaluator = TimeoutAfterSend()
    service = ProductVideoQCService(conn, evaluator=evaluator)
    auth = {
        "workflow_id": workflow_id, "run_id": "video-timeout-authorization",
        "scope": ["video"], "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "max_cost_usd": 0.10,
    }
    kwargs = dict(provider_mode="configured", authorize_cost=True,
                  max_cost_usd=0.10, authorization=auth)

    first = service.run(workflow_id, QCStage.VIDEO, **kwargs)
    assert first["report"].verdict is QCVerdict.UNKNOWN
    assert first["report"].findings["usage_status"] == "uncertain"
    assert first["report"].findings["reconcile_required"] is True
    assert evaluator.calls == 1
    attempt = conn.execute("SELECT status, reservation_id, cost_status FROM product_video_operation_attempt").fetchone()
    assert attempt["status"] == "technical_unknown"
    assert attempt["cost_status"] == "unknown"
    assert attempt["reservation_id"] is None

    again = service.run(workflow_id, QCStage.VIDEO, **kwargs)
    assert again["code"] == "QC_OUTCOME_UNCERTAIN"
    assert evaluator.calls == 1
    conn.close()
