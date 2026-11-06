from __future__ import annotations

import concurrent.futures
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from PIL import Image

from video_factory.application.operator_handler import approve_and_advance_workflow, _current_qc_digest
from video_factory.automation.contracts import (
    compute_product_key,
)
from video_factory.automation.coordinator import ProductVideoCoordinator
from video_factory.automation.importer import (
    publish_daily_run_to_exchange,
)
from video_factory.automation.repository import DailyImportRepository
from video_factory.handlers import (
    ProductVideoWorkflowState,
    start_product_video_workflow,
)
from video_factory.qc.models import QCReport, QCStage, QCVerdict
from video_factory.qc.repository import QCReportRepository
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.approvals import ApprovalService
from video_factory.workflows.envelope import parse_envelope
from video_factory.workflows.repository import WorkflowRepository


def _write_image(path: Path, color: tuple[int, int, int] = (200, 200, 200)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 400), color=color).save(path, format="PNG")


def _seed_pass_image_qc(conn, workflow_id: str, artifact: dict) -> None:
    QCReportRepository(conn).save_report(
        QCReport(
            report_id=f"qc_image_{workflow_id}",
            workflow_id=workflow_id,
            stage=QCStage.IMAGE,
            artifact_id=str(artifact.get("artifact_id") or artifact.get("product_key") or workflow_id),
            artifact_version=1,
            artifact_digest=_current_qc_digest(QCStage.IMAGE, artifact),
            reference_digests=[],
            evaluator_name="test_seeded_qc",
            evaluator_version="1",
            rubric_version="1",
            created_at="2026-09-15T00:00:00+00:00",
            score=90,
            verdict=QCVerdict.PASS,
            findings={"seeded_for": "crash recovery approval guard"},
        )
    )


def test_window_a_claim_crash_and_lease_expiry_reclaim(tmp_path: Path) -> None:
    """Window A: Worker A crashes after claiming; lease expires; Worker B safely reclaims."""
    db_path = tmp_path / "hermes.db"
    conn = connect(db_path)
    repo = DailyImportRepository(conn)

    canon_key = "baseus:bowie-wm02"
    pkey = compute_product_key(canon_key)

    # 1. Worker A claims product with 1-second lease
    claim_a = repo.claim_product(
        canonical_product_key=canon_key,
        source_date="2026-09-12",
        source_batch_id="batch-001",
        source_product_id="prod-001",
        source_path="manifest.json",
        worker_id="worker-a",
        lease_duration_seconds=1.0,
    )
    assert claim_a.status == "claimed"
    assert claim_a.claimed_by == "worker-a"

    # Worker B tries to claim immediately while active -> denied
    claim_b_denied = repo.claim_product(
        canonical_product_key=canon_key,
        source_date="2026-09-12",
        source_batch_id="batch-001",
        source_product_id="prod-001",
        source_path="manifest.json",
        worker_id="worker-b",
    )
    assert claim_b_denied.status == "already_claimed"
    assert claim_b_denied.claimed_by == "worker-a"

    # 2. Simulate crash of Worker A & lease expiration by setting lease_expires_at in the past
    past_iso = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    with conn:
        conn.execute(
            "UPDATE daily_product_video_import SET lease_expires_at = ? WHERE product_key = ?",
            (past_iso, pkey),
        )

    # 3. Worker B retries claim -> lease is expired, Worker B successfully reclaims
    claim_b_success = repo.claim_product(
        canonical_product_key=canon_key,
        source_date="2026-09-12",
        source_batch_id="batch-001",
        source_product_id="prod-001",
        source_path="manifest.json",
        worker_id="worker-b",
    )
    assert claim_b_success.status == "claimed"
    assert claim_b_success.claimed_by == "worker-b"


def test_window_b_workflow_created_crash_before_mark_started(tmp_path: Path) -> None:
    """Window B: Workflow aggregate was committed to SQLite, but worker crashed before mark_started.

    A subsequent recovery run through ProductVideoCoordinator detects the existing workflow,
    marks it started, and avoids creating duplicate records or erroring out.
    """
    # 1. Setup Exchange batch with 1 product
    src_run = tmp_path / "src_run"
    src_run.mkdir()
    img = src_run / "img1.png"
    _write_image(img)
    img2 = src_run / "img2.png"
    # The admission contract requires two distinct source byte streams.
    _write_image(img2, color=(180, 190, 200))
    (src_run / "products.json").write_text(
        json.dumps({
            "version": "1.0",
            "date": "2026-09-12",
            "products": [
                {
                    "id": "prod_wm02",
                    "name": "Baseus Bowie WM02",
                    "brand": "Baseus",
                    "model": "Bowie WM02",
                    "price_display": "350.000đ",
                    "features": "TWS Earbuds",
                    "keywords": ["earbuds"],
                    "images": [
                        {"asset_id": "a1", "absolute_path": str(img), "match_score": 1.0},
                        {"asset_id": "a2", "absolute_path": str(img2), "match_score": 1.0},
                    ],
                }
            ],
        }),
        encoding="utf-8",
    )
    inbox = tmp_path / "exchange_inbox"
    publish_daily_run_to_exchange(src_run, inbox, batch_id="run_b")

    db_path = tmp_path / "hermes.db"
    conn = connect(db_path)
    wf_repo = WorkflowRepository(conn)
    import_repo = DailyImportRepository(conn)

    canon_key = "baseus:bowie-wm02"
    pkey = compute_product_key(canon_key)
    wf_id = f"pv-daily-{pkey[:16]}"

    # Simulate Worker 1 claiming product
    claim = import_repo.claim_product(
        canonical_product_key=canon_key,
        source_date="2026-09-12",
        source_batch_id="run_b",
        source_product_id="prod_wm02",
        source_path="manifest.json",
        worker_id="worker-crash",
    )
    assert claim.status == "claimed"

    # Simulate Worker 1 successfully committing workflow
    start_product_video_workflow(
        workflow_repository=wf_repo,
        workflow_id=wf_id,
        product_name="Baseus Bowie WM02",
        keywords=["earbuds"],
        pdp_url=None,
        owner_user_id="local-owner",
        idempotency_key=f"exchange-import:{pkey}",
        project_id=f"proj-{wf_id}",
    )
    assert wf_repo.get_workflow(wf_id) is not None

    # SIMULATE CRASH: Worker 1 crashes BEFORE calling import_repo.mark_started(pkey)
    # The ledger status is still 'claimed'
    st_before = import_repo.get_by_product_key(pkey)
    assert st_before is not None
    assert st_before["status"] == "claimed"

    # Simulate lease expiry so Worker 2 / Coordinator can retry
    past_iso = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    with conn:
        conn.execute("UPDATE daily_product_video_import SET lease_expires_at = ? WHERE product_key = ?", (past_iso, pkey))

    # Worker 2 / Coordinator starts selection for this product
    coordinator = ProductVideoCoordinator(connection=conn, inbox_root=inbox)
    outbox_before = conn.execute("SELECT COUNT(*) FROM workflow_outbox WHERE workflow_id=?", (wf_id,)).fetchone()[0]
    version_before = wf_repo.get_workflow(wf_id)[1]
    res = coordinator.start_selection([canon_key])

    # Existing workflows are reported as ALREADY_STARTED, never started again.
    # Recovery still repairs the import ledger without touching aggregate/outbox.
    assert len(res.started) == 0
    assert len(res.skipped) == 1
    assert res.skipped[0].reason == "ALREADY_STARTED"
    assert res.skipped[0].workflow_id == wf_id
    assert wf_repo.get_workflow(wf_id)[1] == version_before
    assert conn.execute("SELECT COUNT(*) FROM workflow_outbox WHERE workflow_id=?", (wf_id,)).fetchone()[0] == outbox_before

    st_after = import_repo.get_by_product_key(pkey)
    assert st_after is not None
    assert st_after["status"] == "started"


def test_window_c_product_lock_crash_recovery_and_approval(tmp_path: Path) -> None:
    """Window C: Workflow commits AWAITING_PRODUCT_LOCK and approval request.

    Process crash occurs while awaiting lock.
    On restart, workflow state is safely intact at AwaitingProductLock, and operator approval
    resumes the workflow to BUILDING_RESOURCE_PACK, enqueueing next outbox task.
    """
    db_path = tmp_path / "hermes.db"
    conn = connect(db_path)
    wf_repo = WorkflowRepository(conn)
    approval_svc = ApprovalService(conn)

    canon_key = "baseus:bowie-wm02"
    pkey = compute_product_key(canon_key)
    wf_id = f"pv-daily-{pkey[:16]}"
    project_id = f"proj-{wf_id}"

    # 1. Setup workflow in AwaitingProductLock state
    artifact = {
        "product_key": pkey,
        "product_name": "Bowie WM02",
        "dossier": {"summary": "Great bass and long battery life"},
    }
    req_id = approval_svc.submit_request({
        "workflow_id": wf_id,
        "project_id": project_id,
        "gate": ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value,
        "workflow_version": 1,
        "artifact_id": "art-lock-1",
        "artifact_version": 1,
        "artifact_ref": artifact,
        "owner_user_id": "local-owner",
    })

    agg = {
        "state": ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value,
        "project_id": project_id,
        "owner_user_id": "local-owner",
        "product_key": pkey,
        "pending_approval_id": req_id,
        "pending_approval_gate": ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value,
        "latest_artifact": artifact,
    }
    wf_repo.commit_workflow(wf_id, json.dumps(agg), [], expected_version=0)
    _seed_pass_image_qc(conn, wf_id, artifact)

    # 2. Simulate Process Crash & Restart: close connection and open new one
    conn.close()

    conn2 = connect(db_path)
    wf_repo2 = WorkflowRepository(conn2)
    approval_svc2 = ApprovalService(conn2)

    # State check after restart
    wf_data = wf_repo2.get_workflow(wf_id)
    assert wf_data is not None
    restored_agg, version = wf_data
    assert restored_agg["state"] == ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value
    assert restored_agg["pending_approval_id"] == req_id
    assert version == 1

    # Check approval request status is pending
    req = approval_svc2.get_request(req_id)
    assert req.status.value == "pending"

    # 3. Operator approves Product Lock
    approve_and_advance_workflow(conn2, req_id, owner_user_id="local-owner", expected_version=1)

    # 4. Verify workflow advanced to LOCKING_RESOURCE_PACK
    wf_after = wf_repo2.get_workflow(wf_id)
    assert wf_after is not None
    agg_after, version_after = wf_after
    assert agg_after["state"] == ProductVideoWorkflowState.LOCKING_RESOURCE_PACK.value
    assert agg_after.get("pending_approval_id") is None
    assert version_after == 2

    # Verify Outbox task was enqueued for next step
    raw_outbox = conn2.execute("SELECT message FROM workflow_outbox WHERE workflow_id = ?", (wf_id,)).fetchone()
    assert raw_outbox is not None
    envelope = parse_envelope(raw_outbox[0])
    assert envelope.payload["workflow_id"] == wf_id
    assert envelope.payload["job_name"] == "product_video.lock"
    assert envelope.payload["approval_request_id"] == req_id
    receipt = envelope.payload["kwargs"]["approval_receipt"]
    assert receipt["assessment_actor"] == "operator"
    assert receipt["human_review"] == "not_recorded"
    conn2.close()


def test_window_d_simultaneous_multi_trigger_concurrency(tmp_path: Path) -> None:
    """Window D: Simultaneous multi-trigger (e.g. Scheduler + CLI + REST).

    Multiple concurrent threads attempt to claim the exact same product candidate.
    SQLite transaction locking guarantees exactly 1 thread claims it; all others get already_claimed.
    """
    db_path = tmp_path / "hermes.db"
    conn = connect(db_path)
    conn.close()

    canon_key = "baseus:bowie-wm02"

    def _attempt_claim(worker_id: str) -> str:
        worker_conn = connect(db_path)
        repo = DailyImportRepository(worker_conn)
        res = repo.claim_product(
            canonical_product_key=canon_key,
            source_date="2026-09-12",
            source_batch_id="run_001",
            source_product_id="prod_001",
            source_path="manifest.json",
            worker_id=worker_id,
            lease_duration_seconds=300.0,
        )
        worker_conn.close()
        return res.status

    num_workers = 8
    worker_ids = [f"worker-{i}" for i in range(num_workers)]

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        results = list(executor.map(_attempt_claim, worker_ids))

    claimed_count = results.count("claimed")
    already_claimed_count = results.count("already_claimed")

    assert claimed_count == 1, f"Expected exactly 1 claim, got {claimed_count}. Results: {results}"
    assert already_claimed_count == num_workers - 1
