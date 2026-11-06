"""Offline admission tests for workflow-wide keyframe/video quality cycles."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from PIL import Image

from video_factory.qc.models import QCReport, QCStage, QCVerdict
from video_factory.qc.repository import QCReportRepository
from video_factory.qc.service import ProductVideoQCService
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.repository import WorkflowRepository


def _seed(tmp_path, stage: QCStage, *, current_digest: str):
    conn = connect(tmp_path / "cycles.db")
    aggregate = {
        "workflow_id": "wf-cycles", "owner_user_id": "owner", "project_id": "project",
        "source_context": {"brand": "AULA", "model": "F75"},
        "resource_pack": {"resource_pack_id": "pack", "brand": "AULA", "model": "F75"},
        "storyboard": {"storyboard_id": "board", "generated_keyframes": [{"frame_id": "frame", "sha256": current_digest}],
                       "package_digest": current_digest},
        "render_result": {"sha256": current_digest},
    }
    WorkflowRepository(conn).commit_workflow("wf-cycles", json.dumps(aggregate), [], expected_version=None)
    service = ProductVideoQCService(conn, evaluator=MagicMock())
    return conn, service


def _prior(conn, stage: QCStage, number: int, digest: str, status: str = "semantic_fail"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO product_video_operation_attempt (
           attempt_id, authorization_id, campaign_id, owner_user_id, workflow_id,
           capability_id, attempt_number, input_fingerprint, estimated_cost,
           reservation_id, status, created_at, updated_at
        ) VALUES (?, ?, 'campaign', 'owner', 'wf-cycles', ?, ?, ?, 10, ?, ?, ?, ?)""",
        (f"old-{stage.value}-{number}", f"old-grant-{number}",
         f"product_video.qc.{stage.value}", number, f"fingerprint-{number}",
         f"reservation-{number}", status, now, now),
    )
    QCReportRepository(conn).save_report(QCReport(
        report_id=f"report-{stage.value}-{number}", workflow_id="wf-cycles", stage=stage,
        artifact_id="artifact", artifact_version=number, artifact_digest=digest,
        reference_digests=[], evaluator_name="offline", evaluator_version="1",
        rubric_version="1", created_at=now, score=20, verdict=QCVerdict.FAIL,
        findings={"frame_sha256s": {"frame": digest.removeprefix("sha256:")}},
    ))


def _run(service, stage: QCStage, *, explicit_id=None):
    return service.run(
        "wf-cycles", stage, provider_mode="configured", authorize_cost=True,
        max_cost_usd=0.10, attempt_id=explicit_id,
        authorization={
            "workflow_id": "wf-cycles", "authorization_id": "new-grant",
            "scope": [stage.value], "max_cost_usd": 1.0,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        },
    )


@pytest.mark.parametrize("stage", [QCStage.KEYFRAME, QCStage.VIDEO])
def test_same_artifact_cannot_consume_another_quality_cycle(tmp_path, stage):
    conn, service = _seed(tmp_path, stage, current_digest="sha256:same")
    _prior(conn, stage, 1, "sha256:same")
    result = _run(service, stage)
    assert result["status"] == "pending"
    assert "new artifact" in result["reason"].lower()
    service.evaluator.evaluate_multiple_images.assert_not_called()
    conn.close()


@pytest.mark.parametrize("stage", [QCStage.KEYFRAME, QCStage.VIDEO])
def test_three_cycles_cap_survives_new_grant_and_explicit_attempt_id(tmp_path, stage):
    conn, service = _seed(tmp_path, stage, current_digest="sha256:new")
    for number in (1, 2, 3):
        _prior(conn, stage, number, f"sha256:old-{number}")
    result = _run(service, stage, explicit_id="manual-fourth")
    assert result["status"] == "pending"
    assert "3 total" in result["reason"]
    service.evaluator.evaluate_multiple_images.assert_not_called()
    conn.close()


def test_changed_keyframe_package_gets_one_governed_qc_cycle(tmp_path, monkeypatch):
    conn, service = _seed(tmp_path, QCStage.KEYFRAME, current_digest="sha256:new")
    _prior(conn, QCStage.KEYFRAME, 1, "sha256:old")
    calls = []

    def evaluate_once(**kwargs):
        calls.append(kwargs["attempt_id"])
        return QCReport(
            report_id="new-keyframe-pass", workflow_id="wf-cycles", stage=QCStage.KEYFRAME,
            artifact_id="board", artifact_version=2, artifact_digest="sha256:new",
            reference_digests=[], evaluator_name="offline", evaluator_version="1",
            rubric_version="1", created_at=datetime.now(timezone.utc).isoformat(),
            score=95, verdict=QCVerdict.PASS, findings={},
        )

    monkeypatch.setattr("video_factory.qc.service.run_keyframe_qc", evaluate_once)
    first = _run(service, QCStage.KEYFRAME)
    assert first["status"] == "completed"
    assert first["report"].verdict == QCVerdict.PASS
    assert len(calls) == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM product_video_operation_attempt WHERE workflow_id='wf-cycles'"
    ).fetchone()[0] == 2
    second = _run(service, QCStage.KEYFRAME)
    assert second["status"] == "pending"
    assert len(calls) == 1
    conn.close()


def test_technical_unknown_blocks_new_keyframe_cycle_across_grants(tmp_path):
    conn, service = _seed(tmp_path, QCStage.KEYFRAME, current_digest="sha256:new")
    _prior(conn, QCStage.KEYFRAME, 1, "sha256:old", status="technical_unknown")
    result = _run(service, QCStage.KEYFRAME)
    assert result["code"] == "QC_OUTCOME_UNCERTAIN"
    assert "reconciliation" in result["reason"]
    service.evaluator.evaluate_multiple_images.assert_not_called()
    conn.close()


def test_keyframe_metadata_change_without_new_image_bytes_is_not_rework(tmp_path):
    conn, service = _seed(tmp_path, QCStage.KEYFRAME, current_digest="sha256:changed-package")
    _prior(conn, QCStage.KEYFRAME, 1, "sha256:old-package")
    aggregate, version = WorkflowRepository(conn).get_workflow("wf-cycles")
    aggregate["storyboard"]["generated_keyframes"][0]["sha256"] = "sha256:same-image"
    WorkflowRepository(conn).commit_workflow("wf-cycles", json.dumps(aggregate), [], expected_version=version)
    conn.execute(
        "UPDATE product_video_qc_reports SET findings_json=? WHERE report_id=?",
        (json.dumps({"frame_sha256s": {"frame": "same-image"}}), "report-keyframe-1"),
    )
    result = _run(service, QCStage.KEYFRAME)
    assert result["status"] == "pending"
    assert "new keyframe image" in result["reason"].lower()
    service.evaluator.evaluate_multiple_images.assert_not_called()
    conn.close()


def test_keyframe_timeout_stops_before_second_frame_and_holds_reservation(tmp_path, monkeypatch):
    import hashlib
    from pathlib import Path

    conn, service = _seed(tmp_path, QCStage.KEYFRAME, current_digest="sha256:initial")
    reference = tmp_path / "reference.png"
    Image.new("RGB", (8, 8), color="green").save(reference)
    frames = []
    for index in (1, 2):
        image = tmp_path / f"frame-{index}.png"
        Image.new("RGB", (8, 8), color="red" if index == 1 else "blue").save(image)
        frames.append({
            "frame_id": f"frame-{index}", "scene_id": f"scene-{index}",
            "image_path": str(image), "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
            "reference_image_paths": [str(reference)], "prompt": "offline",
        })
    aggregate, version = WorkflowRepository(conn).get_workflow("wf-cycles")
    aggregate["storyboard"] = {
        "storyboard_id": "board", "version": 1, "package_digest": "sha256:initial",
        "generated_keyframes": frames,
    }
    WorkflowRepository(conn).commit_workflow("wf-cycles", json.dumps(aggregate), [], expected_version=version)
    monkeypatch.setattr("video_factory.qc.service._reference_url_to_path", lambda value: Path(value))

    class TimeoutEvaluator:
        evaluator_name = "offline-vision"
        evaluator_version = "1"
        model = "offline-model"
        fake_mode = False
        calls = 0

        def evaluate_multiple_images(self, *_args, **_kwargs):
            self.calls += 1
            raise TimeoutError("request may have reached provider")

    service.evaluator = TimeoutEvaluator()
    result = _run(service, QCStage.KEYFRAME)
    assert result["report"].verdict == QCVerdict.UNKNOWN
    assert service.evaluator.calls == 1
    assert result["report"].findings["reconcile_required"] is True
    assert conn.execute(
        "SELECT state FROM budget_reservation WHERE scope_id='wf-cycles'"
    ).fetchone()["state"] == "reserved"
    conn.close()


def test_governed_image_qc_respects_unresolved_selector_attempt(tmp_path):
    from video_factory.qc.retry_policy import claim_qc_attempt, complete_qc_attempt

    conn, service = _seed(tmp_path, QCStage.IMAGE, current_digest="sha256:initial")
    claimed = claim_qc_attempt(
        conn, workflow_id="wf-cycles", retry_type="source_image_qc",
        item_id="aula:f75", operation_id="selector-uncertain",
        input_hash="sha256:image-1",
    )
    complete_qc_attempt(
        conn, attempt_id=claimed["attempt_id"], status="technical_unknown",
        verdict="unknown", reason_text="sent outcome unknown",
    )
    result = _run(service, QCStage.IMAGE)
    assert result["code"] == "QC_OUTCOME_UNCERTAIN"
    assert "selector-uncertain" in result["reason"]
    service.evaluator.evaluate_image.assert_not_called()
    conn.close()


def test_keyframe_presend_invalid_asset_releases_budget(tmp_path, monkeypatch):
    from pathlib import Path

    conn, service = _seed(tmp_path, QCStage.KEYFRAME, current_digest="sha256:initial")
    reference = tmp_path / "reference.png"
    Image.new("RGB", (8, 8), color="green").save(reference)
    monkeypatch.setattr("video_factory.qc.service._reference_url_to_path", lambda value: Path(value))
    aggregate, version = WorkflowRepository(conn).get_workflow("wf-cycles")
    aggregate["storyboard"] = {
        "storyboard_id": "board", "version": 1, "package_digest": "sha256:initial",
        "generated_keyframes": [{
            "frame_id": "frame-1", "image_path": str(tmp_path / "missing.png"),
            "sha256": "0" * 64, "reference_image_paths": [str(reference)],
        }],
    }
    WorkflowRepository(conn).commit_workflow("wf-cycles", json.dumps(aggregate), [], expected_version=version)
    result = _run(service, QCStage.KEYFRAME)
    assert result["report"].verdict == QCVerdict.FAIL
    service.evaluator.evaluate_multiple_images.assert_not_called()
    assert result["report"].findings["usage_status"] == "not_sent"
    assert conn.execute(
        "SELECT state FROM budget_reservation WHERE scope_id='wf-cycles'"
    ).fetchone()["state"] == "released"
    conn.close()
