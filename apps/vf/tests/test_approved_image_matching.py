from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image

from video_factory.cli import main as cli_main
from video_factory.handlers import (
    JobContext,
    JobFailed,
    JobSucceeded,
    ProductVideoJobHandlers,
    ProductVideoWorkflowState,
    start_product_video_workflow,
)
from video_factory.handlers.common import (
    _assert_approved_images_in_resource_pack,
    _resolve_image_bytes_hash,
)
from video_factory.job_contracts import TerminalJobError
from video_factory.qc.normalization import _media_manifest
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.engine import WorkflowEngine
from video_factory.workflows.outbox_dispatcher import OutboxDispatcher
from video_factory.workflows.outbox_repository import (
    OutboxRepository,
    _add_seconds,
    _now_iso,
)
from video_factory.workflows.repository import WorkflowRepository
from video_factory.automation.coordinator import ProductVideoCoordinator


def _create_image(path: Path, color: tuple[int, int, int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (100, 100), color=color).save(path, format="PNG")
    return path


def test_exchange_uses_source_images_without_rebuilding_them_through_pi(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test_gate.db")
    repo = WorkflowRepository(conn)
    engine = WorkflowEngine(repo)

    wf_id = "wf_mismatch_test"
    app1 = _create_image(tmp_path / "approved1.png", (255, 255, 255))
    app2 = _create_image(tmp_path / "approved2.png", (200, 200, 200))
    wrong1 = _create_image(tmp_path / "wrong1.png", (10, 10, 10))
    wrong2 = _create_image(tmp_path / "wrong2.png", (20, 20, 20))

    source_context = {
        "source": "hermes_product_video_exchange",
        "images": [
            {"asset_id": "app_1", "absolute_path": str(app1)},
            {"asset_id": "app_2", "absolute_path": str(app2)},
        ],
    }

    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf_id,
        product_name="Test Product",
        keywords=["test"],
        pdp_url="https://www.ugreen.com/products/10335",
        project_id="proj_1",
        owner_user_id="user_1",
        source_context=source_context,
    )

    mock_pi = MagicMock()
    mock_pi.build_resource_pack.return_value = {
        "resource_pack_id": "rp_test",
        "version": 1,
        "assets": [
            {"asset_id": "p1", "local_path": str(wrong1)},
            {"asset_id": "p2", "local_path": str(wrong2)},
        ],
    }

    handlers = ProductVideoJobHandlers(
        workflow_engine=engine,
        workflow_repository=repo,
        pi_client=mock_pi,
        provider_mode="fake",
    )

    ctx = JobContext(
        workflow_id=wf_id,
        job_id="test_job_1",
        owner_user_id="user_1",
        expected_version=1,
    )

    res = handlers.resource_pack_job(
        ctx,
        research_result={"snapshot_id": "snap_1", "persistence_status": "saved"},
        source_context=source_context,
    )

    assert isinstance(res, JobSucceeded)
    assert {item["local_path"] for item in res.artifact_ref["assets"]} == {str(app1), str(app2)}
    mock_pi.build_resource_pack.assert_not_called()


def test_exchange_marker_without_usable_reference_fails_closed_at_start(tmp_path: Path) -> None:
    conn = connect(tmp_path / "exchange_missing_images.db")
    repo = WorkflowRepository(conn)
    with pytest.raises(TerminalJobError, match="EXCHANGE_INPUT_REVIEW_REQUIRED"):
        start_product_video_workflow(
            workflow_repository=repo,
            workflow_id="wf_exchange_missing_images",
            product_name="Exchange product",
            keywords=["product"],
            project_id="proj_exchange",
            source_context={"source": "hermes_product_video_exchange", "images": []},
        )


def test_configured_non_exchange_pack_uses_normal_pi_and_qc_contract(tmp_path: Path) -> None:
    conn = connect(tmp_path / "configured_non_exchange.db")
    repo = WorkflowRepository(conn)
    engine = WorkflowEngine(repo)
    approved = _create_image(tmp_path / "approved.png", (1, 2, 3))
    pack_a = _create_image(tmp_path / "pack_a.png", (4, 5, 6))
    pack_b = _create_image(tmp_path / "pack_b.png", (7, 8, 9))
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id="wf_configured_non_exchange",
        product_name="PI product",
        keywords=["product"],
        project_id="proj_pi",
        source_context={"images": [{"asset_id": "pi_hint", "absolute_path": str(approved)}]},
        qc_authorization={
            "authorization_id": "auth_pi",
            "workflow_id": "wf_configured_non_exchange",
            "max_cost_usd": 10.0,
            "workflow_max_cost_usd": 10.0,
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "scope": ["image", "product_video"],
        },
        qc_max_cost_usd=10.0,
    )
    pi = MagicMock()
    pi.build_resource_pack.return_value = {
        "resource_pack_id": "rp_pi",
        "version": 1,
        "assets": [
            {"asset_id": "pack_a", "local_path": str(pack_a)},
            {"asset_id": "pack_b", "local_path": str(pack_b)},
        ],
    }
    class MockEval:
        def evaluate_image(self, *args, **kwargs):
            return {
                "verdict": "pass",
                "total_score": 90,
                "identity_score": 50,
                "clarity_score": 25,
                "usability_score": 15,
                "hard_failure": False,
                "source_branding_detected": False,
                "identity_mismatch": False,
                "eligible": True,
                "reason": "OK",
                "view_angle": "front",
                "shot_type": "product",
                "visible_details": [],
                "scene_suitability": ["hero"],
                "risk_flags": [],
                "text_coverage_percent": 0,
            }

    handlers = ProductVideoJobHandlers(
        workflow_engine=engine,
        workflow_repository=repo,
        pi_client=pi,
        provider_mode="configured",
        evaluator=MockEval(),
    )
    result = handlers.resource_pack_job(
        JobContext(workflow_id="wf_configured_non_exchange", job_id="job_pi", owner_user_id="user", expected_version=1),
        research_result={"snapshot_id": "snap_pi", "persistence_status": "saved"},
        source_context={"images": [{"asset_id": "pi_hint", "absolute_path": str(approved)}]},
        project_id="proj_pi",
    )
    assert not isinstance(result, JobFailed)
    pi.build_resource_pack.assert_called_once()


def test_identical_id_url_with_differing_bytes_rejected(tmp_path: Path) -> None:
    """Mục tiêu 2: Cùng ID hoặc URL nhưng bytes khác phải bị từ chối."""
    app1 = _create_image(tmp_path / "app1.png", (255, 255, 255))
    app2 = _create_image(tmp_path / "app2.png", (200, 200, 200))
    wrong1 = _create_image(tmp_path / "wrong1.png", (50, 50, 50))
    wrong2 = _create_image(tmp_path / "wrong2.png", (60, 60, 60))

    source_context = {
        "images": [
            {"asset_id": "asset_same_1", "original_url": "https://img.example/same1.png", "absolute_path": str(app1)},
            {"asset_id": "asset_same_2", "original_url": "https://img.example/same2.png", "absolute_path": str(app2)},
        ]
    }

    resource_pack = {
        "assets": [
            {"asset_id": "asset_same_1", "original_url": "https://img.example/same1.png", "local_path": str(wrong1)},
            {"asset_id": "asset_same_2", "original_url": "https://img.example/same2.png", "local_path": str(wrong2)},
        ]
    }

    with pytest.raises(TerminalJobError, match="APPROVED_IMAGE_MISMATCH"):
        _assert_approved_images_in_resource_pack(
            source_context=source_context,
            resource_pack=resource_pack,
        )


def test_copied_or_renamed_id_with_identical_bytes_accepted(tmp_path: Path) -> None:
    """Mục tiêu 2: PI copy file hoặc đổi ID nhưng bytes giống nhau phải được chấp nhận."""
    app1 = _create_image(tmp_path / "original_1.png", (120, 130, 140))
    app2 = _create_image(tmp_path / "original_2.png", (210, 220, 230))

    pi_dir = tmp_path / "pi_download_cache"
    pi_dir.mkdir(parents=True, exist_ok=True)
    copied1 = pi_dir / "copied_asset_999.png"
    copied2 = pi_dir / "renamed_asset_888.png"
    copied1.write_bytes(app1.read_bytes())
    copied2.write_bytes(app2.read_bytes())

    source_context = {
        "images": [
            {"asset_id": "orig_id_1", "absolute_path": str(app1), "original_url": "https://cdn.example/1.png"},
            {"asset_id": "orig_id_2", "absolute_path": str(app2), "original_url": "https://cdn.example/2.png"},
        ]
    }

    resource_pack = {
        "assets": [
            {"asset_id": "copied_asset_999", "local_path": str(copied1)},
            {"asset_id": "renamed_asset_888", "local_path": str(copied2)},
        ]
    }

    # Should not raise any error because byte hashes match
    _assert_approved_images_in_resource_pack(
        source_context=source_context,
        resource_pack=resource_pack,
    )


def test_one_usable_reference_is_enough_even_with_duplicate_pack_entries(tmp_path: Path) -> None:
    """One byte-matched image is enough; duplicate pack rows do not create a quota."""
    img_a = _create_image(tmp_path / "img_a.png", (10, 20, 30))
    img_b = _create_image(tmp_path / "img_b.png", (40, 50, 60))

    _assert_approved_images_in_resource_pack(
        source_context={
            "images": [
                {"asset_id": "app_1", "absolute_path": str(img_a)},
                {"asset_id": "app_2", "absolute_path": str(img_b)},
            ]
        },
        resource_pack={
            "assets": [
                {"asset_id": "p_1", "local_path": str(img_a)},
                {"asset_id": "p_2", "local_path": str(img_a)},
            ]
        },
    )


def test_fail_closed_on_missing_file_or_hash_only(tmp_path: Path) -> None:
    """Mục tiêu 3: Cổng ảnh fail-closed đúng nghĩa với hash-only hoặc file không tồn tại."""
    real_img = _create_image(tmp_path / "real.png", (11, 22, 33))

    # 1. Normalization does NOT mark hash-only asset without physical file as eligible
    manifest = _media_manifest({
        "assets": [
            {"asset_id": "hash_only", "sha256": "abcdef123456", "local_path": str(tmp_path / "non_existent.png")},
            {"asset_id": "real", "local_path": str(real_img)},
        ]
    })
    assert len(manifest) == 2
    assert manifest[0]["eligible"] is False
    assert manifest[0]["sha256"] == ""
    assert manifest[1]["eligible"] is True
    assert manifest[1]["sha256"] != ""

    # 2. _resolve_image_bytes_hash returns None for missing/hash-only
    assert _resolve_image_bytes_hash({"asset_id": "hash_only", "sha256": "abcdef123456"}) is None
    assert _resolve_image_bytes_hash({"asset_id": "missing_path", "local_path": str(tmp_path / "missing.png")}) is None
    assert _resolve_image_bytes_hash({"asset_id": "real", "local_path": str(real_img)}) is not None

    # 3. Hash-only/missing files cannot establish a usable reference.
    with pytest.raises(TerminalJobError, match="APPROVED_IMAGE_MISMATCH"):
        _assert_approved_images_in_resource_pack(
            source_context={
                "images": [{"asset_id": "hash_only", "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}]
            },
            resource_pack={"assets": [{"asset_id": "hash_only", "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}]},
        )


def test_reset_safety_guards_and_realistic_iso8601_lease_comparison(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Mục tiêu 2: Reset an toàn nhưng không làm kẹt người vận hành."""
    db_file = tmp_path / "reset_guards.db"
    conn = connect(db_file)
    repo = WorkflowRepository(conn)

    # 1. Past Product Lock: CREATING_BRIEF
    wf_past = "wf_past_lock"
    repo.commit_workflow(
        workflow_id=wf_past,
        aggregate_json=json.dumps({"state": ProductVideoWorkflowState.CREATING_BRIEF.value}),
        events=[],
        expected_version=0,
    )
    rc = cli_main(["--db-path", str(db_file), "reset", wf_past])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Không thể đặt lại workflow đã vượt qua Product Lock" in err

    # 2. Completed workflow
    wf_comp = "wf_completed"
    repo.commit_workflow(
        workflow_id=wf_comp,
        aggregate_json=json.dumps({"state": ProductVideoWorkflowState.COMPLETED.value}),
        events=[],
        expected_version=0,
    )
    rc = cli_main(["--db-path", str(db_file), "reset", wf_comp])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Không thể đặt lại workflow đã hoàn thành" in err

    # 3. Pre-lock workflow with pending outbox job
    wf_pending = "wf_pending_job"
    repo.commit_workflow(
        workflow_id=wf_pending,
        aggregate_json=json.dumps({"state": ProductVideoWorkflowState.BUILDING_RESOURCE_PACK.value}),
        events=[],
        expected_version=0,
    )
    conn.execute(
        "INSERT INTO workflow_outbox (workflow_id, message, state, created_at, updated_at) "
        "VALUES (?, '{}', 'pending', datetime('now'), datetime('now'))",
        (wf_pending,),
    )
    conn.commit()
    rc = cli_main(["--db-path", str(db_file), "reset", wf_pending])
    assert rc == 1
    err = capsys.readouterr().err
    assert "workflow đang có job chờ thực thi trong outbox" in err

    # 4. Pre-lock workflow with active lease using realistic ISO-8601 format from outbox_repository
    wf_active_lease = "wf_active_lease"
    repo.commit_workflow(
        workflow_id=wf_active_lease,
        aggregate_json=json.dumps({"state": ProductVideoWorkflowState.BUILDING_RESOURCE_PACK.value}),
        events=[],
        expected_version=0,
    )
    active_expires_at = _add_seconds(_now_iso(), 3600)  # 1 hour in the future
    conn.execute(
        "INSERT INTO workflow_outbox (workflow_id, message, state, lease_expires_at, created_at, updated_at) "
        "VALUES (?, '{}', 'leased', ?, datetime('now'), datetime('now'))",
        (wf_active_lease, active_expires_at),
    )
    conn.commit()
    rc = cli_main(["--db-path", str(db_file), "reset", wf_active_lease])
    assert rc == 1
    err = capsys.readouterr().err
    assert "workflow đang trong quá trình thực thi" in err
    assert "active lease" in err

    # 5. Pre-lock workflow with EXPIRED lease using realistic ISO-8601 format
    wf_expired_lease = "wf_expired_lease"
    repo.commit_workflow(
        workflow_id=wf_expired_lease,
        aggregate_json=json.dumps({"state": ProductVideoWorkflowState.BUILDING_RESOURCE_PACK.value}),
        events=[],
        expected_version=0,
    )
    expired_at = _add_seconds(_now_iso(), -30)  # 30 seconds ago
    conn.execute(
        "INSERT INTO workflow_outbox (workflow_id, message, state, lease_expires_at, created_at, updated_at) "
        "VALUES (?, '{}', 'leased', ?, datetime('now'), datetime('now'))",
        (wf_expired_lease, expired_at),
    )
    conn.commit()
    # An expired lease alone is not proof of failure; a worker may reclaim it.
    rc = cli_main(["--db-path", str(db_file), "reset", wf_expired_lease])
    assert rc == 1
    err = capsys.readouterr().err
    assert "chưa có bằng chứng lỗi/quarantine" in err
    assert repo.get_workflow(wf_expired_lease) is not None

    # Explicit quarantine evidence makes the same pre-lock workflow recoverable.
    conn.execute(
        "UPDATE workflow_outbox SET state='quarantined', last_error='PI_RESEARCH_FAILED' WHERE workflow_id=?",
        (wf_expired_lease,),
    )
    conn.commit()
    rc = cli_main(["--db-path", str(db_file), "reset", wf_expired_lease])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"Đã xóa nghẽn cho '{wf_expired_lease}'" in out
    assert repo.get_workflow(wf_expired_lease) is None


@pytest.mark.parametrize(
    "state",
    [
        ProductVideoWorkflowState.RESEARCHING.value,
        ProductVideoWorkflowState.BUILDING_RESOURCE_PACK.value,
        ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value,
    ],
)
def test_reset_rejects_healthy_pre_lock_states(tmp_path: Path, capsys: pytest.CaptureFixture, state: str) -> None:
    db_file = tmp_path / f"healthy_{state}.db"
    conn = connect(db_file)
    repo = WorkflowRepository(conn)
    wf_id = f"wf_healthy_{state}"
    repo.commit_workflow(
        workflow_id=wf_id,
        aggregate_json=json.dumps({"state": state}),
        events=[],
        expected_version=0,
    )
    assert cli_main(["--db-path", str(db_file), "reset", wf_id]) == 1
    assert "chưa có bằng chứng lỗi/quarantine" in capsys.readouterr().err


def test_recovery_cycle_via_real_cli_argv(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Mục tiêu 1: Vòng phục hồi bằng CLI thật (status/list -> reset -> start lại)."""
    db_file = tmp_path / "cli_recovery.db"
    inbox_root = tmp_path / "inbox"
    conn = connect(db_file)
    repo = WorkflowRepository(conn)
    coord = ProductVideoCoordinator(conn, inbox_root=inbox_root, db_path=db_file)

    img1 = _create_image(tmp_path / "product_img1.png", (100, 150, 200))
    img2 = _create_image(tmp_path / "product_img2.png", (200, 150, 100))
    wrong1 = _create_image(tmp_path / "wrong1.png", (0, 0, 0))
    wrong2 = _create_image(tmp_path / "wrong2.png", (10, 10, 10))

    pkey = hashlib.sha256(b"test:product:cli:recovery").hexdigest()
    batch_dir = inbox_root / "2026-10-15" / "run_01"
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "READY").write_text("READY\n", encoding="utf-8")
    (batch_dir / "manifest.json").write_text(
        json.dumps({
            "schema_version": "1.0.0",
            "batch_id": "run_01",
            "production_date": "2026-10-15",
            "products": [
                {
                    "priority": 1,
                    "canonical_product_key": "test:product:cli:recovery",
                    "product_key": pkey,
                    "name": "TestBrand RecoveryModel",
                    "brand": "TestBrand",
                    "model": "RecoveryModel",
                    "source_product_id": "prod_rec_01",
                    "source_url": "https://www.ugreen.com/products/10335",
                    "images": [
                        {"asset_id": "img_01", "relative_path": "img1.png"},
                        {"asset_id": "img_02", "relative_path": "img2.png"},
                    ],
                }
            ],
        }),
        encoding="utf-8",
    )
    (batch_dir / "img1.png").write_bytes(img1.read_bytes())
    (batch_dir / "img2.png").write_bytes(img2.read_bytes())

    claim = coord.import_repo.claim_product(
        canonical_product_key="test:product:cli:recovery",
        source_date="2026-10-15",
        source_batch_id="run_01",
        source_product_id="prod_rec_01",
        source_path=str(batch_dir / "manifest.json"),
        worker_id="worker_test",
    )
    wf_id = claim.workflow_id

    source_context = {
        "source": "hermes_product_video_exchange",
        "product_key": pkey,
        "canonical_product_key": "test:product:cli:recovery",
        "brand": "TestBrand",
        "model": "RecoveryModel",
        "input_review": {"confirmed_image_asset_ids": ["img_01", "img_02"]},
        "images": [
            {"asset_id": "img_01", "absolute_path": str(img1)},
            {"asset_id": "img_02", "absolute_path": str(img2)},
        ],
    }

    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf_id,
        product_name="TestBrand RecoveryModel",
        keywords=["TestBrand RecoveryModel"],
        pdp_url="https://www.ugreen.com/products/10335",
        project_id="proj_recovery",
        owner_user_id="local_operator",
        source_context=source_context,
    )
    coord.import_repo.mark_started(pkey)

    # Simulate PI returning research, then returning mismatched resource pack -> outbox job quarantined with APPROVED_IMAGE_MISMATCH
    mock_pi = MagicMock()
    mock_pi.research.return_value = {
        "snapshot_id": "snap_01",
        "persistence_status": "saved",
        "source_context": source_context,
    }
    mock_pi.build_resource_pack.return_value = {
        "resource_pack_id": "rp_mismatch",
        "version": 1,
        "assets": [
            {"asset_id": "wrong_1", "local_path": str(wrong1)},
            {"asset_id": "wrong_2", "local_path": str(wrong2)},
        ],
    }
    from video_factory.handlers.registry import create_product_video_topic_handlers
    topic_handlers = create_product_video_topic_handlers(
        workflow_repository=repo,
        pi_client=mock_pi,
        provider_mode="configured",
    )

    dispatcher = OutboxDispatcher(
        repository=OutboxRepository(conn),
        topic_handlers=topic_handlers,
    )
    # Remove the only usable references before dispatch; durable admission must quarantine without QC.
    img1.unlink()
    img2.unlink()
    report = dispatcher.process_batch(batch_size=1, workflow_ids=[wf_id])
    assert report.quarantined == 1

    # === STEP 1: OPERATOR VIEWS STATUS VIA CLI (Workflow ID) ===
    capsys.readouterr()  # clear buffer
    rc_status_wf = cli_main([
        "--db-path", str(db_file),
        "--inbox-root", str(inbox_root),
        "status", wf_id,
    ])
    assert rc_status_wf == 0
    out_status_wf = capsys.readouterr().out
    assert "APPROVED_IMAGE_MISMATCH" in out_status_wf
    assert f"Đặt lại workflow: python -m video_factory.cli reset {wf_id}" in out_status_wf
    assert f"Chạy lại: python -m video_factory.cli start --product-keys {pkey} --confirmed-image-asset-id <id> --confirmed-claim \"claim|https://nguồn-thật\"" in out_status_wf

    # === STEP 1b: OPERATOR VIEWS STATUS VIA CLI (Product Key) ===
    rc_status_pk = cli_main([
        "--db-path", str(db_file),
        "--inbox-root", str(inbox_root),
        "status", pkey,
    ])
    assert rc_status_pk == 0
    out_status_pk = capsys.readouterr().out
    assert "APPROVED_IMAGE_MISMATCH" in out_status_pk
    assert f"Đặt lại workflow: python -m video_factory.cli reset {wf_id}" in out_status_pk

    # === STEP 1c: OPERATOR VIEWS LIST VIA CLI ===
    rc_list = cli_main([
        "--db-path", str(db_file),
        "--inbox-root", str(inbox_root),
        "list",
    ])
    assert rc_list == 0
    out_list = capsys.readouterr().out
    assert "APPROVED_IMAGE_MISMATCH" in out_list

    # === STEP 2: OPERATOR RESETS WORKFLOW VIA CLI ===
    rc_reset = cli_main([
        "--db-path", str(db_file),
        "--inbox-root", str(inbox_root),
        "reset", wf_id,
    ])
    assert rc_reset == 0
    out_reset = capsys.readouterr().out
    assert f"Đã xóa nghẽn cho '{wf_id}'" in out_reset

    # === STEP 3: OPERATOR RESTARTS VIA CLI WITH CONFIRMED IMAGES & CLAIM ===
    rc_start = cli_main([
        "--db-path", str(db_file),
        "--inbox-root", str(inbox_root),
        "start",
        "--product-keys", pkey,
        "--confirmed-image-asset-id", "img_01",
        "--confirmed-claim", "TestBrand Claim|https://www.ugreen.com/products/10335",
        "--no-research",
    ])
    assert rc_start == 0
    out_start = capsys.readouterr().out
    assert "Đã tạo 1 workflow mới:" in out_start

    # Verify that the product is now claimed and started with a fresh workflow
    entry = coord.import_repo.find_by_target(pkey)
    assert entry is not None
    assert entry["status"] == "started"
    assert entry["workflow_id"] == wf_id
    wf_after = repo.get_workflow(wf_id)
    assert wf_after is not None
    assert wf_after[0]["state"] == ProductVideoWorkflowState.BUILDING_RESOURCE_PACK.value


def test_reset_all_blocked_recovers_missing_aggregate_when_ledger_is_active(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Kiểm tra reset --all-blocked khi ledger còn started nhưng aggregate đã mất."""
    db_file = tmp_path / "orphan_agg.db"
    conn = connect(db_file)
    repo = WorkflowRepository(conn)
    coord = ProductVideoCoordinator(conn, inbox_root=tmp_path / "inbox", db_path=db_file)

    pkey = hashlib.sha256(b"orphan:product:key").hexdigest()
    wf_id = f"pv-daily-{pkey[:16]}"

    # Setup ledger with 'started' status but no row in workflow aggregate table
    conn.execute(
        "INSERT INTO daily_product_video_import ("
        "  product_key, canonical_product_key, source_date, source_batch_id, "
        "  source_product_id, source_path, workflow_id, status, created_at, updated_at"
        ") VALUES (?, 'orphan:canon', '2026-10-15', 'batch_01', 'p01', '/dummy/path', ?, 'started', datetime('now'), datetime('now'))",
        (pkey, wf_id),
    )
    conn.commit()

    # Aggregate row in 'workflow' table is MISSING
    assert repo.get_workflow(wf_id) is None

    # Run reset --all-blocked via CLI
    rc = cli_main(["--db-path", str(db_file), "reset", "--all-blocked"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Đã giải phóng" in out
    assert wf_id in out

    # Verify that the ledger entry has been cleanly reset to unprocessed (deleted)
    assert coord.import_repo.find_by_target(pkey) is None
