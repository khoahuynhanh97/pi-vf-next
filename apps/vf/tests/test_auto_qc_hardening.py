"""Targeted tests for Auto Image QC hardening:
1. Replay & Unknown Paid Outcome (technical_unknown vs semantic unknown, retry cap <= 3, safe replay).
2. Budget Admission lifecycle (reserve -> commit on pass, release on reject/timeout/error).
3. Byte-based image binding (invalidating cache on disk byte change).
4. Caller hardening in handlers/_base.py (clean JobFailed, no worker crashes).
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from video_factory.qc.evaluator import (
    MultimodalEvaluator,
    QCTechnicalError,
    is_technical_error,
)
from video_factory.qc.image_qc import (
    compute_artifact_digest,
    compute_file_sha256,
    evaluate_image_item,
    run_image_qc,
)
from video_factory.qc.models import NormalizedProduct, QCStage, QCVerdict
from video_factory.qc.repository import QCReportRepository
from video_factory.qc.service import ProductVideoQCService
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.budgets import BudgetPolicy, BudgetRepository
from video_factory.workflows.repository import WorkflowRepository
from video_factory.storage_infra.sqlite import connect
from video_factory.job_contracts import JobContext, JobFailed


def _make_dummy_image(path: Path, color: str = "blue") -> None:
    from PIL import Image
    img = Image.new("RGB", (100, 100), color=color)
    img.save(path)


def _seed_image_workflow(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "qc_hardening.db")
    WorkflowRepository(conn)
    repo = QCReportRepository(conn)
    p1 = tmp_path / "asset1.png"
    p2 = tmp_path / "asset2.png"
    _make_dummy_image(p1, "blue")
    _make_dummy_image(p2, "green")
    resource_pack = {
        "resource_pack_id": "rp-hardened-1",
        "version": 1,
        "owner_user_id": "owner-hardened",
        "project_id": "proj-hardened",
        "assets": [
            {"asset_id": "a1", "local_path": str(p1)},
            {"asset_id": "a2", "local_path": str(p2)},
        ],
    }
    wf_repo = WorkflowRepository(conn)
    wf_repo.commit_workflow(
        "wf-hardened",
        json.dumps({
            "workflow_id": "wf-hardened",
            "project_id": "proj-hardened",
            "owner_user_id": "owner-hardened",
            "state": "building_resource_pack",
            "resource_pack": resource_pack,
            "source_context": {
                "owner_user_id": "owner-hardened",
                "project_id": "proj-hardened",
                "brand": "Baseus",
                "model": "Bowie WM02",
                "verified_claims": ["bluetooth 5.3"],
            },
        }),
        [],
        expected_version=None,
    )
    return conn


def _valid_passing_result() -> dict[str, object]:
    return {
        "view_angle": "front three-quarter",
        "shot_type": "clean product",
        "visible_details": ["earbud body"],
        "scene_suitability": ["hero", "context"],
        "risk_flags": [],
        "text_coverage_percent": 0,
        "identity_score": 55,
        "clarity_score": 23,
        "usability_score": 12,
        "total_score": 90,
        "verdict": "pass",
        "eligible": True,
        "hard_failure": False,
        "source_branding_detected": False,
        "identity_mismatch": False,
        "reason": "Passing clean image",
    }


def test_differentiate_technical_unknown_vs_semantic_unknown(tmp_path):
    """Test 1: Distinguish technical_unknown (429, timeout, network drop) from semantic unknown."""
    # 1. Technical error classification helper
    assert is_technical_error(QCTechnicalError("rate limit", status_code=429)) is True
    assert is_technical_error(TimeoutError("request timed out")) is True
    assert is_technical_error(ConnectionResetError("Connection reset by peer")) is True
    assert is_technical_error(RuntimeError("MultimodalEvaluator HTTP 429: RESOURCE_EXHAUSTED")) is True
    assert is_technical_error(ValueError("Invalid JSON schema")) is False

    # 2. In evaluate_image_item:
    p = tmp_path / "img_test.png"
    _make_dummy_image(p)
    prod = NormalizedProduct(brand="Baseus", model="Bowie WM02")

    # A technical failure (e.g. 429) sets technical_unknown: True
    evaluator_tech = MagicMock()
    evaluator_tech.evaluate_image.side_effect = RuntimeError("MultimodalEvaluator HTTP 429: rate limited")
    item_tech = evaluate_image_item(asset_id="a1", local_path=p, product=prod, evaluator=evaluator_tech)
    assert item_tech.verdict == QCVerdict.UNKNOWN
    assert item_tech.findings.get("technical_unknown") is True

    # A semantic unknown returned by model sets technical_unknown: False / None
    evaluator_sem = MagicMock()
    evaluator_sem.evaluate_image.return_value = {
        **_valid_passing_result(),
        "total_score": 50,
        "identity_score": 25,
        "clarity_score": 15,
        "usability_score": 10,
        "verdict": "unknown",
        "eligible": False,
        "reason": "Subject identity is ambiguous",
    }
    item_sem = evaluate_image_item(asset_id="a1", local_path=p, product=prod, evaluator=evaluator_sem)
    assert item_sem.verdict == QCVerdict.UNKNOWN
    assert item_sem.findings.get("technical_unknown") is not True


def test_technical_unknown_requires_reconciliation_before_any_retry(tmp_path):
    """A possible post-send failure is held for reconciliation, not spent as a retry."""
    conn = _seed_image_workflow(tmp_path)
    evaluator = MagicMock()
    evaluator.evaluator_name = "configured_test_vision"
    evaluator.evaluator_version = "1.0"
    evaluator.model = "test/vision"
    evaluator.fake_mode = False
    evaluator.evaluate_image.side_effect = RuntimeError("MultimodalEvaluator HTTP 429: rate limit")

    auth = {
        "workflow_id": "wf-hardened",
        "run_id": "grant-retry-cap",
        "scope": ["image"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "max_cost_usd": 1.0,
    }
    service = ProductVideoQCService(conn, evaluator=evaluator)

    result = service.run("wf-hardened", QCStage.IMAGE, provider_mode="configured",
                         authorize_cost=True, max_cost_usd=0.10, authorization=auth)
    assert result["status"] == "pending"
    assert result["report"].verdict == QCVerdict.UNKNOWN
    blocked = service.run("wf-hardened", QCStage.IMAGE, provider_mode="configured",
                          authorize_cost=True, max_cost_usd=0.10, authorization=auth)
    assert blocked["status"] == "pending"
    assert "reconciliation" in blocked["reason"].lower()
    assert evaluator.evaluate_image.call_count == 1
    attempt = conn.execute(
        "SELECT status FROM product_video_operation_attempt WHERE capability_id='product_video.qc.image'"
    ).fetchone()
    assert attempt["status"] == "technical_unknown"
    assert conn.execute("SELECT COUNT(*) FROM budget_reservation").fetchone()[0] == 0
    conn.close()


def test_configured_qc_technical_unknown_records_no_fake_budget_or_cost(tmp_path):
    """Configured QC needs no money grant and keeps uncertain usage fail-closed."""
    conn = _seed_image_workflow(tmp_path)
    evaluator = MagicMock()
    evaluator.evaluator_name = "configured_test_vision"
    evaluator.evaluator_version = "1.0"
    evaluator.model = "test/vision"
    evaluator.fake_mode = False

    service = ProductVideoQCService(conn, evaluator=evaluator)

    # The evaluator call may have reached the provider, so cost and usage remain unknown.
    evaluator.evaluate_image.side_effect = RuntimeError("MultimodalEvaluator HTTP 429: rate limited")
    res1 = service.run("wf-hardened", QCStage.IMAGE, provider_mode="configured")
    assert res1["status"] == "pending"
    assert res1["report"].verdict == QCVerdict.UNKNOWN
    assert res1["report"].findings["usage_status"] == "uncertain"
    assert res1["report"].findings["cost_status"] == "unknown"
    assert res1["report"].cost_usd is None
    assert conn.execute("SELECT COUNT(*) FROM budget_reservation").fetchone()[0] == 0
    row = conn.execute(
        "SELECT status, reservation_id, actual_cost_usd, cost_status FROM product_video_operation_attempt WHERE workflow_id=?",
        ("wf-hardened",),
    ).fetchone()
    assert row["status"] == "technical_unknown"
    assert row["reservation_id"] is None
    assert row["actual_cost_usd"] is None
    assert row["cost_status"] == "unknown"
    replay = service.run("wf-hardened", QCStage.IMAGE, provider_mode="configured")
    assert replay["status"] == "pending"
    assert replay["code"] == "QC_OUTCOME_UNCERTAIN"
    assert evaluator.evaluate_image.call_count == 1
    conn.close()


def test_byte_tampering_invalidates_cache_and_reevaluates(tmp_path):
    """Test 4: Cache is bound to image byte SHA-256; modifying file content on disk

    (even with identical path/filename) invalidates cached report and triggers re-evaluation.
    """
    p1 = tmp_path / "img1.png"
    p2 = tmp_path / "img2.png"
    _make_dummy_image(p1, "blue")
    _make_dummy_image(p2, "green")
    rp = {
        "resource_pack": {
            "resource_pack_id": "rp-tamper-test",
            "version": 1,
            "owner_user_id": "owner-tamper",
            "project_id": "proj-tamper",
            "assets": [
                {"asset_id": "a1", "local_path": str(p1)},
                {"asset_id": "a2", "local_path": str(p2)},
            ],
        }
    }
    prod = NormalizedProduct(brand="Jisulife", model="Life 7", display_name="Jisulife Life 7")

    conn = sqlite3.connect(":memory:")
    repo = QCReportRepository(conn)
    evaluator = MagicMock()
    evaluator.evaluator_name = "test_vision"
    evaluator.evaluator_version = "1.0"
    evaluator.evaluate_image.return_value = _valid_passing_result()

    # Initial evaluation: runs evaluator for both images
    rep1, eligible1 = run_image_qc("wf-tamper", rp, prod, evaluator=evaluator, repo=repo)
    assert rep1.verdict == QCVerdict.PASS
    assert len(eligible1) == 2
    assert evaluator.evaluate_image.call_count == 2

    # Second run without changes: reuses cache (0 extra calls)
    evaluator.evaluate_image.reset_mock()
    rep_cached, _ = run_image_qc("wf-tamper", rp, prod, evaluator=evaluator, repo=repo)
    assert rep_cached.report_id == rep1.report_id
    assert evaluator.evaluate_image.call_count == 0

    # Modify p1 content on disk (same filename and path, but bytes change)
    _make_dummy_image(p1, "red")
    new_sha = compute_file_sha256(p1)
    assert new_sha != eligible1[0].digest

    # Run image QC again: cached report MUST be invalidated, triggering fresh evaluation
    evaluator.evaluate_image.reset_mock()
    rep2, eligible2 = run_image_qc("wf-tamper", rp, prod, evaluator=evaluator, repo=repo)
    assert rep2.report_id != rep1.report_id
    assert evaluator.evaluate_image.call_count == 2
    # The new digest must match the modified image bytes
    digests = {item.digest for item in eligible2}
    assert new_sha in digests
    conn.close()


def test_handlers_base_catches_all_errors_cleanly(tmp_path):
    """Test 5: Caller in handlers/_base.py catches all codes cleanly without unhandled exceptions."""
    from video_factory.handlers._base import ProductVideoJobHandlers

    pi_client = MagicMock()
    pi_client.build_resource_pack.return_value = {
        "resource_pack_id": "rp-1",
        "version": 1,
        "owner_user_id": "owner-test",
        "project_id": "proj-test",
        "assets": [],
    }
    wf_conn = connect(":memory:")
    wf_repo = WorkflowRepository(wf_conn)
    wf_repo.commit_workflow(
        "wf-handler-test",
        json.dumps({
            "state": "building_resource_pack",
            "source_context": {"brand": "Baseus", "model": "Bowie WM02"},
            "qc_authorization": {
                "workflow_id": "wf-handler-test",
                "run_id": "grant-handler",
                "scope": ["image"],
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "max_cost_usd": 0.50,
            },
            "qc_max_cost_usd": 0.50,
        }),
        [],
        expected_version=None,
    )
    from video_factory.qc.retry_policy import ensure_qc_retry_schema

    ensure_qc_retry_schema(wf_conn)
    handler = ProductVideoJobHandlers(
        pi_client=pi_client,
        video_client=MagicMock(),
        workflow_repository=wf_repo,
        provider_mode="configured",
    )

    ctx = JobContext(
        workflow_id="wf-handler-test",
        job_id="job-1",
        owner_user_id="owner-test",
    )

    # When qc_svc returns error status (e.g. INSUFFICIENT_BUDGET)
    evaluator = MagicMock()
    handler.evaluator = evaluator

    # Case A: Service returns INSUFFICIENT_BUDGET
    mock_svc = MagicMock()
    mock_svc.select_image_references.return_value = {
        "status": "pending",
        "code": "INSUFFICIENT_BUDGET",
        "reason": "Exceeds authorized ceiling",
    }

    import video_factory.qc.service
    orig_svc_class = video_factory.qc.service.ProductVideoQCService
    video_factory.qc.service.ProductVideoQCService = lambda *args, **kwargs: mock_svc

    try:
        res = handler.resource_pack_job(
            ctx,
            research_result={
                "brand": "Baseus",
                "model": "Bowie WM02",
                "owner_user_id": "owner-test",
                "project_id": "proj-test",
                "snapshot_id": "snap-test-1",
                "persistence_status": "persisted",
            },
            qc_authorization={
                "workflow_id": "wf-handler-test",
                "run_id": "grant-handler",
                "scope": ["image"],
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "max_cost_usd": 0.50,
            },
            qc_max_cost_usd=0.50,
            project_id="proj-test",
        )
        assert isinstance(res, JobFailed)
        assert res.code == "INSUFFICIENT_BUDGET", res.message
        assert res.retryable is False

        # Case B: Service raises unexpected Exception (e.g. SQLite connection dropped)
        mock_svc.select_image_references.side_effect = RuntimeError("Database disk I/O error")
        res_exc = handler.resource_pack_job(
            ctx,
            research_result={
                "brand": "Baseus",
                "model": "Bowie WM02",
                "owner_user_id": "owner-test",
                "project_id": "proj-test",
                "snapshot_id": "snap-test-1",
                "persistence_status": "persisted",
            },
            qc_authorization={
                "workflow_id": "wf-handler-test",
                "run_id": "grant-handler",
                "scope": ["image"],
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "max_cost_usd": 0.50,
            },
            qc_max_cost_usd=0.50,
            project_id="proj-test",
        )
        assert isinstance(res_exc, JobFailed)
        assert res_exc.code == "IMAGE_QC_ERROR"
        assert "Database disk I/O error" in res_exc.message
    finally:
        video_factory.qc.service.ProductVideoQCService = orig_svc_class
