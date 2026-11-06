from pathlib import Path
import sqlite3
import pytest

from video_factory.integrations.ports import VideoGenerationResult
from video_factory.qc.cv_integrity_checker import VideoFrameIntegrityChecker
from video_factory.qc.models import QCReport, QCStage, QCVerdict
from video_factory.qc.service import ProductVideoQCService


def test_config_externalization_yaml(tmp_path: Path):
    """Verify that VideoFrameIntegrityChecker dynamically loads custom YAML dead zones."""
    custom_yaml = tmp_path / "custom_safe_zone.yaml"
    custom_yaml.write_text(
        """
platforms:
  tiktok:
    aspect_ratio: "9:16"
    safe_zone:
      x_min: 0.10
      x_max: 0.80
      y_min: 0.15
      y_max: 0.70
    dead_zones:
      custom_banner:
        x_min: 0.0
        x_max: 1.0
        y_min: 0.0
        y_max: 0.20
""",
        encoding="utf-8",
    )

    checker = VideoFrameIntegrityChecker(config_path=custom_yaml)
    assert "custom_banner" in checker._safe_zone_config["dead_zones"]
    assert checker._safe_zone_config["safe_zone"]["x_min"] == 0.10


def test_cost_tracking_in_models():
    """Verify cost_usd tracking in VideoGenerationResult and QCReport."""
    result = VideoGenerationResult(
        request_id="req-123",
        status="completed",
        video_path="/path/to/vid.mp4",
        cost_usd=0.045,
    )
    assert result.cost_usd == 0.045

    report = QCReport(
        report_id="rep-123",
        workflow_id="wf-1",
        stage=QCStage.VIDEO,
        artifact_id="art-1",
        artifact_version=1,
        artifact_digest="sha256:abc",
        reference_digests=[],
        evaluator_name="test-eval",
        evaluator_version="1.0",
        rubric_version="1.0",
        created_at="2026-10-10T00:00:00Z",
        score=95,
        verdict=QCVerdict.PASS,
        findings={},
        cost_usd=0.015,
    )
    d = report.to_dict()
    assert d["cost_usd"] == 0.015

    restored = QCReport.from_dict(d)
    assert restored.cost_usd == 0.015


def test_manual_review_queue_lifecycle(tmp_path: Path):
    """Verify queue logging, listing, and resolution."""
    queue_file = tmp_path / "review_queue.jsonl"
    conn = sqlite3.connect(":memory:")
    service = ProductVideoQCService(conn)

    # 1. Log an item
    record = service.log_manual_review(
        scene_id="scene_03",
        video_path=Path("/tmp/scene_03.mp4"),
        collision_zones=["right_action_bar"],
        action_centroid=(0.88, 0.50),
        review_log_path=queue_file,
    )
    assert record["status"] == "pending_manual_review"

    # 2. List items
    pending_items = service.list_manual_reviews(queue_file, status_filter="pending_manual_review")
    assert len(pending_items) == 1
    assert pending_items[0]["scene_id"] == "scene_03"

    # 3. Resolve item
    success = service.resolve_manual_review(
        scene_id="scene_03",
        resolution="rerender_requested",
        notes="Subject hand collided with share button",
        review_log_path=queue_file,
    )
    assert success is True

    # 4. Verify status updated
    all_items = service.list_manual_reviews(queue_file)
    assert len(all_items) == 1
    assert all_items[0]["status"] == "rerender_requested"
    assert all_items[0]["resolution_notes"] == "Subject hand collided with share button"
