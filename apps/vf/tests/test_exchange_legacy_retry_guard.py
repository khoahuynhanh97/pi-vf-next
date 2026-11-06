"""Exchange images must never use the legacy unbudgeted cleaner retry."""

import json
from unittest.mock import patch

from video_factory.handlers import ProductVideoJobHandlers
from video_factory.job_contracts import JobContext, JobFailed
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.engine import WorkflowEngine
from video_factory.workflows.repository import WorkflowRepository


def test_exchange_retry_rejected_before_image_cleaner_or_evaluator(tmp_path):
    conn = connect(tmp_path / "exchange-retry.db")
    repo = WorkflowRepository(conn)
    repo.commit_workflow(
        "wf-exchange-retry",
        json.dumps({
            "state": "retrying_image_qc",
            "source_context": {"source": "hermes_product_video_exchange"},
        }), [], expected_version=None,
    )
    handlers = ProductVideoJobHandlers(
        workflow_engine=WorkflowEngine(repo), workflow_repository=repo,
    )
    with patch("video_factory.preprocessing.image_cleaner.ProductImageCleaner") as cleaner:
        result = handlers.retry_image_qc_job(
            JobContext(workflow_id="wf-exchange-retry", job_id="retry-job"),
            {"initial_qc_report": {"verdict": "FAIL"}, "image_qc_retry_count": 0},
        )
    assert isinstance(result, JobFailed)
    assert result.code == "QC_AUTHORIZATION_REQUIRED"
    assert "governed" in result.message.lower()
    cleaner.assert_not_called()
    conn.close()
