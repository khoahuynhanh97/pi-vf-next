import json
from pathlib import Path

import pytest
from PIL import Image

from video_factory.application.operator_handler import replace_blocked_source_images
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.repository import WorkflowRepository


def _skipped_image_workflow(conn, *, code="EXCHANGE_IMAGE_QC_FAILED"):
    aggregate = {
        "state": "skipped",
        "owner_user_id": "local-owner",
        "project_id": "proj-input-fix",
        "product_key": "snack-1",
        "source_context": {
            "source": "hermes_product_video_exchange",
            "product_key": "snack-1",
            "canonical_product_key": "tao-kae-noi:classic:27g",
            "images": [],
        },
        "research_result": {"product_name": "Tao Kae Noi snack"},
        "failure_category": "product_rejected",
        "failure_code": code,
        "failure_reason": "source image failed identity QC",
        "skip_reason": "source image failed identity QC",
        "failure_report": {"old_report": "preserve"},
    }
    WorkflowRepository(conn).commit_workflow("wf-input-fix", json.dumps(aggregate), [], expected_version=0)


def test_replace_blocked_source_images_reuses_resource_pack_qc_and_preserves_failure_audit(tmp_path):
    conn = connect(tmp_path / "input-fix.db")
    _skipped_image_workflow(conn)
    image_path = tmp_path / "replacement.png"
    Image.new("RGB", (100, 160), "red").save(image_path)

    result = replace_blocked_source_images(conn, workflow_id="wf-input-fix", owner_user_id="local-owner",
        image_paths=[image_path], reason="Replaced the unclear product photo")

    assert result["state"] == "building_resource_pack"
    assert result["input_revision"].startswith("sha256:")
    aggregate = WorkflowRepository(conn).get_workflow("wf-input-fix")[0]
    assert aggregate["failure_report"] == {"old_report": "preserve"}
    assert aggregate["source_context"]["images"][0]["sha256"] == result["image_sha256s"][0]
    assert aggregate["source_context"]["source_input_revision"] == result["input_revision"]
    assert conn.execute("SELECT event_type FROM workflow_event WHERE workflow_id='wf-input-fix'").fetchone()[0] == "SourceImagesReplaced"
    envelope = json.loads(conn.execute("SELECT message FROM workflow_outbox WHERE workflow_id='wf-input-fix'").fetchone()[0])
    assert envelope["payload"]["job_name"] == "product_video.resource_pack"
    assert envelope["payload"]["kwargs"]["images"][0]["absolute_path"] == str(image_path.resolve())
    replay = replace_blocked_source_images(conn, workflow_id="wf-input-fix", owner_user_id="local-owner",
        image_paths=[image_path], reason="same correction command replayed")
    assert replay["reused"] is True
    assert conn.execute("SELECT COUNT(*) FROM workflow_event WHERE workflow_id='wf-input-fix'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM workflow_outbox WHERE workflow_id='wf-input-fix'").fetchone()[0] == 1
    conn.close()


def test_replace_blocked_product_info_revalidates_existing_images_and_replays_idempotently(tmp_path):
    import hashlib

    conn = connect(tmp_path / "input-info-fix.db")
    image_path = tmp_path / "known-source.png"
    Image.new("RGB", (100, 160), "blue").save(image_path)
    digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
    _skipped_image_workflow(conn)
    aggregate, version = WorkflowRepository(conn).get_workflow("wf-input-fix")
    aggregate["source_context"].update(brand="Tao Kae Noi", model="Seaweed Snack", variant="original",
        images=[{"asset_id": "source-1", "absolute_path": str(image_path), "sha256": digest}])
    WorkflowRepository(conn).commit_workflow("wf-input-fix", json.dumps(aggregate), [], expected_version=version)

    result = replace_blocked_source_images(conn, workflow_id="wf-input-fix", owner_user_id="local-owner",
        reason="Corrected the package variant from the label", product_info={"variant": "classic 27g"})
    assert result["state"] == "building_resource_pack"
    updated = WorkflowRepository(conn).get_workflow("wf-input-fix")[0]
    assert updated["source_context"]["variant"] == "classic 27g"
    assert updated["research_result"]["source_context"]["canonical_product_key"] == "tao-kae-noi:seaweed-snack:classic-27g"
    assert updated["source_input_recovery_history"][0]["product_info"] == {"variant": "classic 27g"}
    assert conn.execute("SELECT canonical_product_key FROM daily_product_video_import WHERE workflow_id='wf-input-fix'").fetchone() is None
    replay = replace_blocked_source_images(conn, workflow_id="wf-input-fix", owner_user_id="local-owner",
        reason="same corrected variant", product_info={"variant": "classic 27g"})
    assert replay["reused"] is True
    assert conn.execute("SELECT COUNT(*) FROM workflow_event WHERE workflow_id='wf-input-fix'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM workflow_outbox WHERE workflow_id='wf-input-fix'").fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize("code", ["FINAL_CONTENT_FAIL", "KEYFRAME_CONTENT_FAIL", "QC_OUTCOME_UNCERTAIN"])
def test_replace_source_images_rejects_other_terminal_reasons(tmp_path, code):
    conn = connect(tmp_path / f"input-fix-{code}.db")
    _skipped_image_workflow(conn, code=code)
    image_path = tmp_path / f"replacement-{code}.png"
    Image.new("RGB", (100, 160), "red").save(image_path)

    with pytest.raises(ValueError, match="source image QC failure"):
        replace_blocked_source_images(conn, workflow_id="wf-input-fix", owner_user_id="local-owner",
            image_paths=[image_path], reason="corrected input")
    assert WorkflowRepository(conn).get_workflow("wf-input-fix")[0]["state"] == "skipped"
    conn.close()
