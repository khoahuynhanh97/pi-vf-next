from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import pytest

from video_factory.cli import _quarantined_job_error, _qc_authorization_from_args, build_parser, main
from video_factory.storage_infra.sqlite import connect


def _write_image(path: Path, color: tuple[int, int, int] = (255, 255, 255)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if color == (255, 255, 255):
        h = sum(path.name.encode('utf-8')) % 250
        color = (h, (h * 2) % 250, (h * 3) % 250)
    Image.new('RGB', (800, 800), color=color).save(path, format='PNG')


def _product(batch_dir: Path, index: int, *, name: str, brand: str, model: str) -> dict:
    images = []
    for image_index in range(2):
        image_path = batch_dir / "images" / f"prod-{index}" / f"img-{image_index}.png"
        _write_image(image_path)
        images.append(
            {
                "asset_id": f"asset-{index}-{image_index}",
                "absolute_path": str(image_path),
                "match_score": 1.0,
                "width": 800,
                "height": 800,
            }
        )
    return {
        "id": f"prod_{index:02d}",
        "name": name,
        "brand": brand,
        "model": model,
        "price_display": "200.000đ",
        "price_min": 200000,
        "price_max": 200000,
        "hot_reason": "Hot",
        "key_features": "Features",
        "keywords": [name],
        "image_count": len(images),
        "images": images,
    }


def test_qc_cli_preserves_campaign_id_for_budget_admission() -> None:
    args = build_parser().parse_args([
        "qc", "pv-1", "--stage", "image",
        "--authorization-run-id", "run-1",
        "--authorization-scope", "product_video",
        "--authorization-expires-at", "2026-09-26T00:00:00Z",
        "--authorization-max-cost-usd", "50",
        "--authorization-campaign-id", "campaign-1",
    ])
    assert _qc_authorization_from_args(args, workflow_id="pv-1")["campaign_id"] == "campaign-1"


def test_watch_accepts_batch_qc_authorization() -> None:
    args = build_parser().parse_args([
        "watch", "--once", "--qc-max-cost-usd", "10",
        "--qc-authorization-run-id", "pilot-run-1",
        "--qc-authorization-scope", "product_video",
        "--qc-authorization-max-cost-usd", "10",
    ])
    assert _qc_authorization_from_args(args) == {
        "run_id": "pilot-run-1", "scope": ["product_video"],
        "expires_at": None, "max_cost_usd": 10.0, "campaign_max_cost_usd": 100.0,
    }


@pytest.mark.parametrize("global_position", ["before", "after"])
def test_replace_source_images_uses_global_db_binding(tmp_path: Path, monkeypatch, global_position: str) -> None:
    from types import SimpleNamespace
    import video_factory.cli as cli
    from video_factory.application import operator_handler

    db_path = tmp_path / "chosen.db"
    image = tmp_path / "replacement.png"
    image.write_bytes(b"fixture")
    opened: list[Path] = []
    received: list[tuple[object, dict]] = []
    connection = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(cli, "connect", lambda path: (opened.append(Path(path)) or connection))
    monkeypatch.setattr(cli, "_resolve_db_path", lambda path: Path(path))
    monkeypatch.setattr(cli, "DailyImportRepository", lambda _conn: SimpleNamespace(find_by_target=lambda _target: None))
    monkeypatch.setattr(operator_handler, "replace_blocked_source_images",
        lambda conn, **kwargs: (received.append((conn, kwargs)) or {"state": "building_resource_pack"}))
    command = ["replace-source-images", "fixture-product", "--image", str(image), "--reason", "corrected"]
    argv = ["--db-path", str(db_path), *command] if global_position == "before" else [*command, "--db-path", str(db_path)]

    assert cli.main(argv) == 0
    assert opened == [db_path]
    assert received[0][0] is connection
    assert received[0][1]["workflow_id"] == "fixture-product"


def test_qc_advance_reuses_current_saved_image_pass_without_provider_call(tmp_path: Path, capsys, monkeypatch) -> None:
    import hashlib
    from datetime import datetime, timezone

    from video_factory.qc.fingerprint import IMAGE_POLICY_VERSION, IMAGE_RUBRIC_VERSION
    from video_factory.qc.models import QCReport, QCStage, QCVerdict
    from video_factory.qc.normalization import resource_pack_fingerprint
    from video_factory.qc.repository import QCReportRepository
    from video_factory.workflow import ProductVideoWorkflowState
    from video_factory.workflows.repository import WorkflowRepository

    db_path = tmp_path / "reuse_image_qc.db"
    workflow_id = "pv-reuse-saved-image-pass"
    image_paths = [tmp_path / "front.png", tmp_path / "rear.png", tmp_path / "failed.png"]
    for index, path in enumerate(image_paths):
        Image.new("RGB", (64, 64), (index * 50, 30, 100)).save(path)
    assets = [
        {
            "asset_id": f"asset-{index}",
            "local_path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for index, path in enumerate(image_paths)
    ]
    pack = {
        "resource_pack_id": f"rp_{workflow_id}",
        "version": 1,
        "brand": "Example",
        "model": "Snack 48g",
        "variant": "Lemon Lime",
        "assets": assets,
        "product_references": assets,
        "media_assets": assets,
    }
    selected = [
        {**assets[0], "eligible": True, "score": 95, "verdict": "pass"},
        {**assets[1], "eligible": True, "score": 90, "verdict": "pass"},
    ]
    report_id = f"qc_img_{workflow_id}_saved"
    now = datetime.now(timezone.utc).isoformat()
    report = QCReport(
        report_id=report_id,
        workflow_id=workflow_id,
        stage=QCStage.IMAGE,
        artifact_id=pack["resource_pack_id"],
        artifact_version=1,
        artifact_digest=resource_pack_fingerprint(pack),
        reference_digests=[asset["sha256"] for asset in assets],
        evaluator_name="nine_router_vision_qc",
        evaluator_version="1.0",
        model_identifier="Vision-QC-Check",
        rubric_version=IMAGE_RUBRIC_VERSION,
        policy_version=IMAGE_POLICY_VERSION,
        created_at=now,
        score=92,
        verdict=QCVerdict.PASS,
        findings={
            "hard_failure": False,
            "eligible_reference_manifest": selected,
            "items": [
                *[
                    {
                        "asset_id": asset["asset_id"], "sha256": asset["sha256"],
                        "score": 90, "verdict": "pass", "eligible": True,
                        "hard_failure": False,
                        "findings": {
                            "eligible": True, "verdict": "pass", "hard_failure": False,
                            "source_branding_detected": False, "identity_mismatch": False,
                            "risk_flags": [],
                        },
                    }
                    for asset in selected
                ],
                {"asset_id": assets[2]["asset_id"], "sha256": assets[2]["sha256"], "score": 0, "verdict": "fail", "eligible": False, "hard_failure": True},
            ],
        },
        provider_mode="configured",
        owner_user_id="local-owner",
        project_id=f"proj-{workflow_id}",
    )
    conn = connect(db_path)
    aggregate = {
        "workflow_id": workflow_id,
        "project_id": f"proj-{workflow_id}",
        "owner_user_id": "local-owner",
        "state": ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value,
        "operator_action": "qc_reconciliation",
        "operator_stage": "image",
        "resource_pack": pack,
        "source_context": {"brand": "Example", "model": "Snack 48g", "variant": "Lemon Lime"},
    }
    WorkflowRepository(conn).commit_workflow(workflow_id, json.dumps(aggregate), [], expected_version=None)
    QCReportRepository(conn).save_report(report)
    conn.close()

    def unexpected_provider_call(*args, **kwargs):
        raise AssertionError("saved PASS advance must not invoke QC evaluator")

    monkeypatch.setattr("video_factory.qc.service.ProductVideoQCService.run", unexpected_provider_call)

    result = main([
        "--db-path", str(db_path), "qc", workflow_id,
        "--stage", "image", "--provider-mode", "configured", "--advance",
    ])

    assert result == 0
    output = capsys.readouterr().out
    assert report_id in output
    assert "QC reused" in output
    with closing(connect(db_path)) as check_conn:
        updated, _ = WorkflowRepository(check_conn).get_workflow(workflow_id)
        assert updated["state"] == ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value


def test_quarantined_worker_error_is_shown_only_for_current_blocked_step() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE workflow_outbox (outbox_id INTEGER PRIMARY KEY, workflow_id TEXT, "
        "state TEXT, last_error TEXT)"
    )
    conn.execute(
        "INSERT INTO workflow_outbox (workflow_id, state, last_error) VALUES (?, ?, ?)",
        ("pv-1", "quarantined", "NO_PRODUCT_EVIDENCE"),
    )
    assert _quarantined_job_error(conn, "pv-1", "researching") == "NO_PRODUCT_EVIDENCE"
    assert _quarantined_job_error(conn, "pv-1", "AwaitingProductLock") is None
    conn.execute(
        "INSERT INTO workflow_outbox (workflow_id, state, last_error) VALUES (?, ?, ?)",
        ("pv-1", "completed", None),
    )
    assert _quarantined_job_error(conn, "pv-1", "researching") is None


def test_cli_list_and_start_auto(tmp_path: Path, capsys) -> None:
    source_root = tmp_path / "daily_runs"
    batch_dir = source_root / "2026-09-12"
    batch_dir.mkdir(parents=True)

    products = [
        _product(batch_dir, 1, name="Quạt Jisulife Life 7", brand="Jisulife", model="Life 7"),
        _product(batch_dir, 2, name="Bàn phím AULA F75", brand="AULA", model="F75"),
    ]
    (batch_dir / "products.json").write_text(
        json.dumps({"version": "1.0", "date": "2026-09-12", "products": products}),
        encoding="utf-8",
    )
    db_path = tmp_path / "hermes.db"

    # Test list command
    exit_code = main(["--db-path", str(db_path), "--source-root", str(source_root), "list"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Quạt Jisulife Life 7" in out
    assert "Bàn phím AULA F75" in out
    assert "[CHƯA TẠO VIDEO]" in out

    # Test start auto command without research
    exit_code = main([
        "--db-path", str(db_path),
        "--source-root", str(source_root),
        "start", "--auto", "--limit", "1",
        "--no-research"
    ])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Đã tạo 1 workflow mới" in out

    # Verify state in list again
    exit_code = main(["--db-path", str(db_path), "--source-root", str(source_root), "list"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "[ĐANG SẢN XUẤT]" in out

    # A best-effort day reports a shortfall without undoing valid workflows.
    exit_code = main([
        "--db-path", str(db_path), "--source-root", str(source_root),
        "start", "--auto", "--limit", "5", "--minimum-daily", "3",
        "--date", "2026-09-12", "--no-research",
    ])
    output = capsys.readouterr()
    assert exit_code == 1
    assert "2/3" in output.err
    with closing(connect(db_path)) as connection:
        assert len(connection.execute(
            "SELECT workflow_id FROM daily_product_video_import WHERE source_date = '2026-09-12' AND status = 'started'"
        ).fetchall()) == 2

    status_code = main([
        "--db-path", str(db_path), "daily-status",
        "--date", "2026-09-12", "--minimum", "3", "--json",
    ])
    daily_status = json.loads(capsys.readouterr().out)
    assert status_code == 1
    assert daily_status["started"] == 2
    assert daily_status["completed"] == 0


def test_cli_start_fails_when_all_claims_error(tmp_path: Path, capsys, monkeypatch) -> None:
    inbox_root = tmp_path / "exchange_inbox"
    batch_dir = inbox_root / "2026-09-13" / "run_01"
    batch_dir.mkdir(parents=True)

    img_path = batch_dir / "products" / "test_p" / "images" / "img_1.png"
    _write_image(img_path)
    img_path2 = batch_dir / "products" / "test_p" / "images" / "img_2.png"
    _write_image(img_path2)

    manifest = {
        "schema_version": "1.0.0",
        "batch_id": "run_01",
        "target_date": "2026-09-13",
        "production_date": "2026-09-13",
        "producer_version": "1.0.0",
        "products": [
            {
                "priority": 1,
                "source_product_id": "p01",
                "name": "Jisulife Life 7",
                "brand": "Jisulife",
                "model": "Life 7",
                "canonical_product_key": "jisulife:life-7",
                "product_key": "1e96ac740ec2f41f35a92194ca77c69fa3249fd103fc4e792321eca63eafd38b",
                "price_display": "350.000đ",
                "images": [
                    {
                        "asset_id": "a01",
                        "relative_path": "products/test_p/images/img_1.png",
                        "match_score": 1.0,
                    },
                    {
                        "asset_id": "a02",
                        "relative_path": "products/test_p/images/img_2.png",
                        "match_score": 1.0,
                    }
                ],
            }
        ],
    }
    (batch_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (batch_dir / "READY").write_text("READY\n", encoding="utf-8")

    db_path = tmp_path / "hermes_claim_err.db"

    # Simulate claim failure on repository
    from video_factory.automation.repository import ClaimResult, DailyImportRepository
    def mock_claim(*args, **kwargs):
        return ClaimResult(status="error", product_key="test_key", workflow_id="test_wf", error="Simulated lock failure")

    monkeypatch.setattr(DailyImportRepository, "claim_product", mock_claim)

    pkey = "1e96ac740ec2f41f35a92194ca77c69fa3249fd103fc4e792321eca63eafd38b"
    exit_code = main([
        "--db-path", str(db_path),
        "--inbox-root", str(inbox_root),
        "start", "--product-keys", pkey,
        "--confirmed-image-asset-id", "a01",
        "--confirmed-image-asset-id", "a02",
        "--confirmed-claim", "Life 7 Claim|https://jisulife.com/life-7",
        "--no-research"
    ])
    # Must fail with non-zero when claims error out
    assert exit_code != 0
    err_out = capsys.readouterr()
    assert "Tất cả sản phẩm đã được xử lý" not in err_out.out
    assert "LỖI KHỞI ĐỘNG" in err_out.err or "LỖI" in err_out.err


def test_cli_start_fails_when_advance_to_product_lock_fails(tmp_path: Path, capsys, monkeypatch) -> None:
    inbox_root = tmp_path / "exchange_inbox"
    batch_dir = inbox_root / "2026-09-13" / "run_01"
    batch_dir.mkdir(parents=True)

    img_path = batch_dir / "products" / "test_p" / "images" / "img_1.png"
    _write_image(img_path)
    img_path2 = batch_dir / "products" / "test_p" / "images" / "img_2.png"
    _write_image(img_path2)

    manifest = {
        "schema_version": "1.0.0",
        "batch_id": "run_01",
        "target_date": "2026-09-13",
        "production_date": "2026-09-13",
        "producer_version": "1.0.0",
        "products": [
            {
                "priority": 1,
                "source_product_id": "p01",
                "name": "Jisulife Life 7",
                "brand": "Jisulife",
                "model": "Life 7",
                "canonical_product_key": "jisulife:life-7",
                "product_key": "1e96ac740ec2f41f35a92194ca77c69fa3249fd103fc4e792321eca63eafd38b",
                "price_display": "350.000đ",
                "images": [
                    {
                        "asset_id": "a01",
                        "relative_path": "products/test_p/images/img_1.png",
                        "match_score": 1.0,
                    },
                    {
                        "asset_id": "a02",
                        "relative_path": "products/test_p/images/img_2.png",
                        "match_score": 1.0,
                    }
                ],
            }
        ],
    }
    (batch_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (batch_dir / "READY").write_text("READY\n", encoding="utf-8")

    db_path = tmp_path / "hermes_adv_fail.db"

    from video_factory.automation.coordinator import ProductVideoCoordinator
    def mock_advance(*args, **kwargs):
        return {"succeeded": False, "error": "Simulated advance failure"}

    monkeypatch.setattr(ProductVideoCoordinator, "advance_to_product_lock", mock_advance)

    pkey = "1e96ac740ec2f41f35a92194ca77c69fa3249fd103fc4e792321eca63eafd38b"
    exit_code = main([
        "--db-path", str(db_path),
        "--inbox-root", str(inbox_root),
        "start", "--product-keys", pkey,
        "--confirmed-image-asset-id", "a01",
        "--confirmed-image-asset-id", "a02",
        "--confirmed-claim", "Life 7 Claim|https://jisulife.com/life-7",
    ])
    assert exit_code != 0
    err_out = capsys.readouterr()
    assert "HOÀN TẤT" not in err_out.out
    assert "LỖI ADVANCE TO PRODUCT LOCK" in err_out.err


def test_cli_approve_cost_guard_gate4(tmp_path: Path, capsys) -> None:
    from video_factory.workflow import ProductVideoWorkflowState
    from video_factory.workflows.approvals import ApprovalService
    from video_factory.workflows.repository import WorkflowRepository

    db_path = tmp_path / "cost_guard.db"
    conn = connect(db_path)

    repo = WorkflowRepository(conn)
    approval_svc = ApprovalService(conn)

    wf_id = "pv-daily-costguard"
    project_id = f"proj-{wf_id}"
    owner = "local-owner"

    # Set up workflow at Gate 4 (AwaitingStoryboardApproval)
    agg = {
        "workflow_id": wf_id,
        "project_id": project_id,
        "owner_user_id": owner,
        "state": ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL.value,
        "source_context": {"brand": "Jisulife", "model": "Life 7", "name": "Jisulife Life 7", "price_display": "350.000đ"},
        "latest_artifact": {
            "schema": "storyboard",
            "artifact_id": "sb_001",
            "version": 1,
        },
    }
    repo.commit_workflow(wf_id, json.dumps(agg), [], expected_version=None)

    req_id = approval_svc.submit_request({
        "workflow_id": wf_id,
        "project_id": project_id,
        "gate": "AwaitingStoryboardApproval",
        "workflow_version": 2,
        "artifact_id": "sb_001",
        "artifact_version": 1,
        "owner_user_id": owner,
    })

    agg["pending_approval_id"] = req_id
    agg["pending_approval_gate"] = "AwaitingStoryboardApproval"
    repo.commit_workflow(wf_id, json.dumps(agg), [], expected_version=1)

    from video_factory.application.operator_handler import _current_qc_digest
    from video_factory.qc.models import QCReport, QCStage, QCVerdict
    from video_factory.qc.repository import QCReportRepository

    QCReportRepository(conn).save_report(
        QCReport(
            report_id=f"qc_keyframe_{wf_id}",
            workflow_id=wf_id,
            stage=QCStage.KEYFRAME,
            artifact_id="sb_001",
            artifact_version=1,
            artifact_digest=_current_qc_digest(QCStage.KEYFRAME, agg["latest_artifact"]),
            reference_digests=[],
            evaluator_name="test_seeded_qc",
            evaluator_version="1",
            rubric_version="1",
            created_at="2026-09-15T00:00:00+00:00",
            score=90,
            verdict=QCVerdict.PASS,
            findings={"seeded_for": "test_cli_approve_cost_guard_gate4"},
        )
    )
    conn.close()

    # 1. Attempt approve without --authorize-cost (non-interactive) -> MUST FAIL with code 1
    code_no_auth = main([
        "--db-path", str(db_path),
        "approve", req_id,
    ])
    assert code_no_auth != 0
    err_out = capsys.readouterr()
    assert "LỖI XÁC NHẬN CHI PHÍ" in err_out.err
    assert "--authorize-cost" in err_out.err

    # 1b. Interactive approve prompt answered 'y' -> Advances successfully
    with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="y"):
        code_interactive = main([
            "--db-path", str(db_path),
            "approve", req_id,
            "--provider-mode", "fake",
        ])
    assert code_interactive == 0
    out_interactive = capsys.readouterr().out
    assert "PHÊ DUYỆT THÀNH CÔNG" in out_interactive


def test_cli_status_and_caption_completed(tmp_path: Path, capsys) -> None:
    from video_factory.workflow import ProductVideoWorkflowState
    from video_factory.workflows.repository import WorkflowRepository

    db_path = tmp_path / "status_test.db"
    conn = connect(db_path)
    repo = WorkflowRepository(conn)

    wf_id = "pv-daily-completed1"
    project_id = f"proj-{wf_id}"
    video_file = tmp_path / "export_final.mp4"
    video_file.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"video data here")
    caption_file = tmp_path / "caption.txt"
    caption_file.write_text("Baseus Bowie WM02\n\n#baseus #bowiewm02", encoding="utf-8")

    agg = {
        "workflow_id": wf_id,
        "project_id": project_id,
        "owner_user_id": "local-owner",
        "state": ProductVideoWorkflowState.COMPLETED.value,
        "source_context": {
            "brand": "Baseus",
            "model": "Bowie WM02",
            "name": "Tai nghe Baseus Bowie WM02",
            "price_display": "299.000đ",
            "selling_points": ["Pin 25 giờ", "Độ trễ thấp 0.06s", "Thiết kế capsule trong suốt"],
            "images": ["img1.png", "img2.png"],
        },
        "resource_pack": {
            "resource_pack_id": "rp_test",
            "status": "locked",
            "digest": "sha256:abc1234567890123456",
        },
        "latest_artifact": {
            "schema": "video_export",
            "export_id": "exp_001",
                "version": 1,
                "file_path": str(video_file),
                "caption_path": str(caption_file),
                "final_video_url": f"file://{video_file.resolve()}",
        },
    }
    repo.commit_workflow(wf_id, json.dumps(agg), [], expected_version=None)
    conn.close()

    code = main([
        "--db-path", str(db_path),
        "status", wf_id,
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "Baseus Bowie WM02" in out
    assert "299.000đ" in out
    assert "Resource Pack ID:   rp_test" in out
    assert "THÔNG TIN VIDEO HOÀN THÀNH" in out
    assert str(video_file.resolve()) in out
    assert "TIKTOK CAPTION GỢI Ý" in out
    assert "#baseus" in out
    assert "#bowiewm02" in out


def test_cli_status_reads_canonical_export_caption_and_never_rebuilds_template(tmp_path: Path, capsys) -> None:
    from video_factory.workflow import ProductVideoWorkflowState
    from video_factory.workflows.repository import WorkflowRepository

    db_path = tmp_path / "status_caption_truth.db"
    video_file = tmp_path / "export_final.mp4"
    video_file.write_bytes(b"mp4")
    caption_file = tmp_path / "caption.txt"
    caption_file.write_text("AULA F75\n\nXem thêm thông tin sản phẩm.", encoding="utf-8")

    conn = connect(db_path)
    WorkflowRepository(conn).commit_workflow(
        "wf-caption-truth",
        json.dumps({
            "workflow_id": "wf-caption-truth",
            "project_id": "proj-wf-caption-truth",
            "owner_user_id": "local-owner",
            "state": ProductVideoWorkflowState.COMPLETED.value,
            "source_context": {
                "brand": "AULA",
                "model": "F75",
                "name": "pack_snap_016c6cfddd3641e6a9bf7384fb78079d",
                "price_display": "350.000đ",
            },
            "latest_artifact": {
                "schema": "video_export",
                "file_path": str(video_file),
                "caption_path": str(caption_file),
                "metadata_path": str(tmp_path / "metadata.json"),
            },
        }),
        [],
        expected_version=None,
    )
    conn.close()

    assert main(["--db-path", str(db_path), "status", "wf-caption-truth"]) == 0
    out = capsys.readouterr().out
    assert "AULA F75" in out
    assert "Xem thêm thông tin sản phẩm." in out
    assert "Review chân thực" not in out
    assert "chính hãng" not in out.lower()
    assert "giỏ hàng" not in out.lower()
    assert "#review" not in out


def test_cli_status_does_not_generate_caption_before_export(tmp_path: Path, capsys) -> None:
    from video_factory.workflow import ProductVideoWorkflowState
    from video_factory.workflows.repository import WorkflowRepository

    db_path = tmp_path / "status_no_caption.db"
    conn = connect(db_path)
    WorkflowRepository(conn).commit_workflow(
        "wf-no-caption",
        json.dumps({
            "workflow_id": "wf-no-caption",
            "project_id": "proj-wf-no-caption",
            "owner_user_id": "local-owner",
            "state": ProductVideoWorkflowState.COMPLETED.value,
            "source_context": {"brand": "AULA", "model": "F75", "name": "AULA F75"},
            "latest_artifact": {"schema": "video_export", "file_path": str(tmp_path / "missing.mp4")},
        }),
        [],
        expected_version=None,
    )
    conn.close()

    assert main(["--db-path", str(db_path), "status", "wf-no-caption"]) == 0
    out = capsys.readouterr().out
    assert "Chưa có caption export chính thức" in out
    assert "Review chân thực" not in out


@pytest.mark.parametrize(
    "failure_code,should_roll_over",
    [("PI_RESEARCH_FAILED", False), ("EXCHANGE_IMAGE_QC_FAILED", True)],
)
def test_cli_start_auto_rolls_over_only_for_classified_product_rejection(
    tmp_path: Path, capsys, monkeypatch, failure_code: str, should_roll_over: bool
) -> None:
    from video_factory.automation.importer import publish_daily_run_to_exchange
    from video_factory.automation.coordinator import ProductVideoCoordinator

    src_run = tmp_path / "src_run"
    src_run.mkdir()
    p1 = _product(src_run, 1, name="Gorilla KK", brand="Gorilla", model="KK")
    p2 = _product(src_run, 2, name="Máy chiếu Magcubic HY300 Pro 4K", brand="Magcubic", model="HY300 Pro 4K")
    (src_run / "products.json").write_text(
        json.dumps({"version": "1.0", "date": "2026-10-07", "products": [p1, p2]}),
        encoding="utf-8",
    )
    inbox_root = tmp_path / "exchange_inbox"
    publish_daily_run_to_exchange(src_run, inbox_root, batch_id="run_01")

    catalog_path = tmp_path / "AUTO_QC_CANDIDATES.csv"
    catalog_path.write_text(
        "canonical_product_key,identity_status,pipeline_status\n"
        "gorilla:kk,VALID,READY_FOR_IMAGE_QC\n"
        "magcubic:hy300-pro-4k,VALID,READY_FOR_IMAGE_QC\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "rollover.db"

    calls: list[list[str]] = []

    def mock_advance(self, workflow_ids: list[str], provider_mode: str = "configured", max_cycles: int = 10):
        calls.append(list(workflow_ids))
        if len(calls) == 1:
            wf_id = workflow_ids[0]
            failure_reason = f"{failure_code}: candidate rejected"
            self.import_repo.mark_error_by_workflow_id(wf_id, failure_reason)
            if should_roll_over:
                from video_factory.automation.failure_classification import FailureCategory
                aggregate, version = self.workflow_repo.get_workflow(wf_id)
                aggregate.update({
                    "state": "skipped",
                    "failure_category": FailureCategory.PRODUCT_REJECTED.value,
                    "failure_code": failure_code,
                    "skip_reason": failure_reason,
                })
                self.workflow_repo.commit_workflow(
                    workflow_id=wf_id,
                    aggregate_json=json.dumps(aggregate),
                    events=[],
                    expected_version=version,
                )
                return {
                    "succeeded": True,
                    "workflows": {wf_id: {"state": "skipped", "failure_category": "PRODUCT_REJECTED"}},
                    "locked_workflows": [],
                    "skipped_workflows": [wf_id],
                }
            return {
                "succeeded": False,
                "error": f"No further outbox messages eligible ({failure_reason})",
                "workflows": {wf_id: {"state": "building_resource_pack", "error": failure_reason, "quarantined": True}},
                "locked_workflows": [],
                "quarantined_workflows": {wf_id: failure_reason},
            }
        wf_id = workflow_ids[0]
        return {
            "succeeded": True,
            "cycles": 1,
            "workflows": {wf_id: {"state": "AwaitingProductLock", "approval_id": "app-1", "gate": "AwaitingProductLock"}},
            "locked_workflows": [wf_id],
            "quarantined_workflows": {},
        }

    monkeypatch.setattr(ProductVideoCoordinator, "advance_to_product_lock", mock_advance)
    from video_factory.automation.importer import ExchangeImporter
    monkeypatch.setattr(
        ExchangeImporter,
        "validate_admission",
        lambda self, p, b, input_review=None: (True, None, [
            {"asset_id": p.images[0].asset_id, "absolute_path": str(b / p.images[0].relative_path)},
            {"asset_id": p.images[1].asset_id, "absolute_path": str(b / p.images[1].relative_path)},
        ])
    )

    exit_code = main([
        "--db-path", str(db_path),
        "--inbox-root", str(inbox_root),
        "start", "--auto", "--limit", "1", "--no-auto-approve",
        "--catalog-v2-candidates", str(catalog_path),
    ])
    captured = capsys.readouterr()
    if not should_roll_over:
        assert exit_code == 1
        assert len(calls) == 1
        assert "DỪNG BATCH" in captured.err
        return
    assert exit_code == 0, f"stdout={captured.out}\nstderr={captured.err}"
    assert len(calls) == 2
    assert "HOÀN TẤT" in captured.out
    with closing(connect(db_path)) as mode_conn:
        from video_factory.workflows.repository import WorkflowRepository
        for row in mode_conn.execute("SELECT workflow_id FROM daily_product_video_import WHERE status='started'"):
            assert WorkflowRepository(mode_conn).get_workflow(row[0])[0]["execution_mode"] == "manual"


def test_cli_reset_unblocks_stalled_workflow_for_retry_or_skip(tmp_path: Path, capsys) -> None:
    from video_factory.automation.importer import compute_canonical_product_key, compute_product_key, publish_daily_run_to_exchange

    src_run = tmp_path / "src_run"
    src_run.mkdir()
    p1 = _product(src_run, 1, name="Gorilla KK", brand="Gorilla", model="KK")
    (src_run / "products.json").write_text(
        json.dumps({"version": "1.0", "date": "2026-10-07", "products": [p1]}),
        encoding="utf-8",
    )
    inbox_root = tmp_path / "exchange_inbox"
    publish_daily_run_to_exchange(src_run, inbox_root, batch_id="run_01")
    db_path = tmp_path / "reset_test.db"
    catalog_path = tmp_path / "AUTO_QC_CANDIDATES.csv"
    catalog_path.write_text(
        "canonical_product_key,identity_status,pipeline_status\n"
        "gorilla:kk,VALID,READY_FOR_IMAGE_QC\n",
        encoding="utf-8",
    )

    canon = compute_canonical_product_key(brand="Gorilla", model="KK")
    pkey = compute_product_key(canon)

    # 1. Start workflow without research using confirmed input
    assert main([
        "--db-path", str(db_path),
        "--inbox-root", str(inbox_root),
        "start", "--product-keys", pkey,
        "--confirmed-image-asset-id", "asset-1-0",
        "--confirmed-image-asset-id", "asset-1-1",
        "--confirmed-claim", "Gorilla Claim|https://example.com/source",
        "--catalog-v2-candidates", str(catalog_path),
        "--no-research",
    ]) == 0
    capsys.readouterr()

    # Simulate partial state delete where workflow row is gone but processed_command remains
    conn = connect(db_path)
    wf_id = conn.execute("SELECT workflow_id FROM daily_product_video_import").fetchone()[0]
    conn.execute("DELETE FROM workflow WHERE workflow_id = ?", (wf_id,))
    conn.commit()
    conn.close()

    # 2. Run reset --all-blocked to return product to unprocessed
    assert main(["--db-path", str(db_path), "--inbox-root", str(inbox_root), "reset", "--all-blocked"]) == 0
    out = capsys.readouterr().out
    assert "Đã giải phóng" in out

    # 3. Verify restarting the same product succeeds cleanly without DuplicateCommandError
    assert main([
        "--db-path", str(db_path),
        "--inbox-root", str(inbox_root),
        "start", "--product-keys", pkey,
        "--confirmed-image-asset-id", "asset-1-0",
        "--confirmed-image-asset-id", "asset-1-1",
        "--confirmed-claim", "Gorilla Claim|https://example.com/source",
        "--catalog-v2-candidates", str(catalog_path),
        "--no-research",
    ]) == 0
    out_restart = capsys.readouterr().out
    assert "Đã tạo 1 workflow mới" in out_restart

    with closing(connect(db_path)) as manual_conn:
        from video_factory.workflows.repository import WorkflowRepository
        assert WorkflowRepository(manual_conn).get_workflow(wf_id)[0]["execution_mode"] == "manual"
    with patch("video_factory.cli.run_worker") as worker:
        assert main([
            "--db-path", str(db_path), "--inbox-root", str(inbox_root),
            "watch", "--once", "--provider-mode", "fake",
        ]) == 0
        worker.assert_not_called()

    # 4. Reset with --skip marks the product as skipped/invalid so --auto skips it
    assert main(["--db-path", str(db_path), "--inbox-root", str(inbox_root), "reset", wf_id, "--skip", "--reason", "Bad URL"]) == 0
    capsys.readouterr()
    assert main(["--db-path", str(db_path), "--inbox-root", str(inbox_root), "list"]) == 0
    list_out = capsys.readouterr().out
    assert "[KHÔNG ĐẠT TIÊU CHUẨN]" in list_out
    assert "Bad URL" in list_out


def test_cli_watch_once_processes_new_batch(tmp_path: Path, capsys) -> None:
    from video_factory.automation.importer import publish_daily_run_to_exchange

    src_run = tmp_path / "src_run_watch"
    src_run.mkdir()
    p1 = _product(src_run, 1, name="Tai nghe V8 Pro ANC", brand="V8", model="Pro ANC")
    (src_run / "products.json").write_text(
        json.dumps({"version": "1.0", "date": "2026-10-07", "products": [p1]}),
        encoding="utf-8",
    )
    inbox_root = tmp_path / "exchange_inbox_watch"
    publish_daily_run_to_exchange(src_run, inbox_root, batch_id="run_01")
    catalog_path = tmp_path / "AUTO_QC_CANDIDATES.csv"
    catalog_path.write_text(
        "canonical_product_key,identity_status,pipeline_status\n"
        "v8:pro-anc,VALID,READY_FOR_IMAGE_QC\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "watch_test.db"

    # Under fail-closed admission: raw unconfirmed product without operator review
    # must NOT be automatically started by the watcher daemon
    exit_code = main([
        "--db-path", str(db_path),
        "--inbox-root", str(inbox_root),
        "watch", "--once", "--limit", "1", "--provider-mode", "fake",
        "--catalog-v2-candidates", str(catalog_path),
    ])
    assert exit_code == 0
    captured = capsys.readouterr().out
    assert "AUTONOMOUS INBOX WATCHER" in captured
    assert "Phát hiện 1 sản phẩm mới! Bắt đầu tạo video tự động" in captured
    assert "Đã tạo 1 workflow mới" in captured
    conn = connect(db_path)
    started_count = conn.execute("SELECT COUNT(*) FROM daily_product_video_import WHERE status = 'started'").fetchone()[0]
    assert started_count == 1
    from video_factory.workflows.repository import WorkflowRepository
    auto_workflow_id = conn.execute("SELECT workflow_id FROM daily_product_video_import").fetchone()[0]
    assert WorkflowRepository(conn).get_workflow(auto_workflow_id)[0]["execution_mode"] == "auto"

    # A later watcher tick must service an existing workflow even when the
    # inbox contains no newly unprocessed product.
    with patch("video_factory.cli.run_worker") as worker:
        assert main([
            "--db-path", str(db_path), "--inbox-root", str(inbox_root),
            "watch", "--once", "--limit", "1", "--provider-mode", "fake",
            "--no-auto-approve", "--catalog-v2-candidates", str(catalog_path),
        ]) == 0
        assert worker.call_count == 1
        assert worker.call_args.kwargs["workflow_ids"]
