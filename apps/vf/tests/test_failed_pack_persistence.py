import json

from video_factory.handlers.registry import create_product_video_topic_handlers
from video_factory.job_contracts import JobFailed
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.envelope import build_envelope, parse_envelope
from video_factory.workflows.repository import WorkflowRepository


def test_failed_resource_pack_job_persists_pack_for_operator_reconciliation(tmp_path):
    workflow_id = "wf-pack-unknown"
    connection = connect(tmp_path / "pack.db")
    repository = WorkflowRepository(connection)
    repository.commit_workflow(
        workflow_id,
        json.dumps({"state": "BuildingResourcePack", "owner_user_id": "local-owner"}),
        [],
        expected_version=None,
    )
    built_pack = {
        "resource_pack_id": "rp-pack-unknown",
        "version": 1,
        "assets": [{"asset_id": "asset-1", "sha256": "a" * 64}],
    }

    class _Handlers:
        def get_handlers_map(self):
            return {
                "product_video.resource_pack": lambda *_args, **_kwargs: JobFailed(
                    retryable=False,
                    code="QC_OUTCOME_UNCERTAIN",
                    message="provider response is unknown",
                    artifact_ref=built_pack,
                )
            }

    topics = create_product_video_topic_handlers(
        workflow_repository=repository,
        handlers_service=_Handlers(),
        provider_mode="fake",
    )
    envelope = parse_envelope(build_envelope(
        workflow_id=workflow_id,
        topic="job.dispatch",
        idempotency_key="resource-pack-attempt-1",
        payload={
            "job_name": "product_video.resource_pack",
            "workflow_id": workflow_id,
            "args": [],
        },
    ))

    topics["job.dispatch"](envelope)

    aggregate, _ = repository.get_workflow(workflow_id)
    assert aggregate["state"] == "WaitingForOperator"
    assert aggregate["resource_pack"] == built_pack
    assert aggregate["latest_artifact"] == built_pack
    assert aggregate["operator_action"] == "qc_retry_authorization"
    connection.close()
