import json

import pytest

from video_factory.automation.coordinator import ProductVideoCoordinator
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.repository import WorkflowRepository


@pytest.mark.parametrize("existing_pack", [None, {"resource_pack_id": "rp-prior", "version": 1}])
@pytest.mark.parametrize("failure_code", ["QC_OUTCOME_UNCERTAIN", "QC_QUOTA_EXHAUSTED"])
@pytest.mark.parametrize("already_scheduled", [False, True])
def test_reconciled_image_qc_can_resume_resource_pack_through_outbox(tmp_path, existing_pack, failure_code, already_scheduled):
    workflow_id = "pv-recovery-pack"
    connection = connect(tmp_path / "recovery.db")
    repository = WorkflowRepository(connection)
    old_images = [
        {"asset_id": "asset-one", "absolute_path": str(tmp_path / "one.png"), "sha256": "1" * 64},
        {"asset_id": "asset-two", "absolute_path": str(tmp_path / "two.png"), "sha256": "2" * 64},
    ]
    repository.commit_workflow(
        workflow_id,
        json.dumps({
            "workflow_id": workflow_id,
            "state": "WaitingForOperator",
            "project_id": "proj-pv-recovery-pack",
            "owner_user_id": "local-owner",
            "operator_action": "qc_retry_authorization",
            "operator_stage": "image",
            "failure_code": failure_code,
            "max_qc_retries": 3,
            "qc_authorization": {"run_id": "previous-grant"},
            "resource_pack": existing_pack,
            "qc_reconciliations": [{
                "attempt_id": "qc-attempt-1", "outcome": "consumed",
                "resource_pack_resume_scheduled": already_scheduled,
            }],
            "research_result": {"snapshot_id": "snap-saved", "persistence_status": "saved"},
            "research_snapshot_id": "snap-saved",
            "source_context": {
                "source": "hermes_product_video_exchange",
                "images": old_images,
                "input_review": {"confirmed_image_asset_ids": ["asset-one", "asset-two"]},
            },
        }),
        [],
        expected_version=None,
    )
    result = ProductVideoCoordinator(connection, db_path=tmp_path / "recovery.db")._resume_reconciled_image_qc(
        workflow_id,
        owner_user_id="local-owner",
        qc_authorization=None,
        qc_max_cost_usd=None,
        max_qc_retries=3,
    )

    assert result is (failure_code == "QC_OUTCOME_UNCERTAIN" and not already_scheduled)
    if not result:
        connection.close()
        return
    aggregate, _ = repository.get_workflow(workflow_id)
    assert aggregate["state"] == "building_resource_pack"
    assert aggregate["qc_authorization"]["run_id"] == "previous-grant"
    assert aggregate["source_context"]["images"] == old_images
    row = connection.execute(
        "SELECT state, message FROM workflow_outbox WHERE workflow_id = ?",
        (workflow_id,),
    ).fetchone()
    envelope = json.loads(row["message"])
    assert row["state"] == "pending"
    assert envelope["payload"]["job_name"] == "product_video.resource_pack"
    assert envelope["payload"]["kwargs"]["research_snapshot_id"] == "snap-saved"
    assert "qc_authorization" not in envelope["payload"]["kwargs"]
    assert ProductVideoCoordinator(connection, db_path=tmp_path / "recovery.db")._resume_reconciled_image_qc(
        workflow_id,
        owner_user_id="local-owner",
        qc_authorization=None,
        qc_max_cost_usd=None,
        max_qc_retries=3,
    ) is False
    assert connection.execute(
        "SELECT COUNT(*) FROM workflow_outbox WHERE workflow_id = ?", (workflow_id,)
    ).fetchone()[0] == 1
    connection.close()
