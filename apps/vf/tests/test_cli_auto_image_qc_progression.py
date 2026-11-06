from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw

from video_factory.automation.repository import DailyImportRepository
from video_factory.cli import main
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.repository import WorkflowRepository
import video_factory.automation.coordinator as coordinator_module
from video_factory.handlers import ProductVideoJobHandlers, create_product_video_topic_handlers
from video_factory.workflows.worker import run_worker as real_run_worker
from video_factory.qc.evaluator import QCTechnicalError


class _OfflineEvaluator:
    evaluator_name = "offline-sequence"
    evaluator_version = "1"
    model = "fake"

    def __init__(self, outcomes: dict[str, str]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    def evaluate_image(self, image_path: Path | str, prompt: str, system_instruction: str | None = None) -> dict:
        path = Path(image_path)
        self.calls.append(path.name)
        verdict = self.outcomes.get(path.name.split("_")[0], "pass")
        if verdict == "technical_unknown":
            raise QCTechnicalError("offline transport timeout after send")
        return {
            "verdict": verdict,
            "total_score": 90 if verdict == "pass" else 20 if verdict == "fail" else 0,
            "identity_score": 55 if verdict == "pass" else 0 if verdict == "unknown" else 5,
            "clarity_score": 10 if verdict == "fail" else 0 if verdict == "unknown" else 23,
            "usability_score": 5 if verdict == "fail" else 0 if verdict == "unknown" else 12,
            "hard_failure": verdict == "fail",
            "source_branding_detected": False,
            "identity_mismatch": verdict == "fail",
            "eligible": verdict == "pass",
            "reason": f"offline {verdict}",
            "view_angle": "front",
            "shot_type": "product",
            "visible_details": [],
            "scene_suitability": [],
            "risk_flags": ["identity_mismatch"] if verdict == "fail" else [],
            "text_coverage_percent": 0,
        }


class _OfflinePI:
    def __init__(self, repo: WorkflowRepository, *, mismatched_pack_model: str | None = None) -> None:
        self.repo = repo
        self.mismatched_pack_model = mismatched_pack_model

    def research(self, product_name, keywords, pdp_url=None, **kwargs):
        workflow = self.repo.get_workflow(kwargs.get("workflow_id", ""))
        source_context = workflow[0].get("source_context") if workflow else None
        return {
            "snapshot_id": f"offline-{kwargs.get('workflow_id')}",
            "persistence_status": "saved",
            "source_context": source_context,
            "product": {"brand": "brand", "model": product_name.split()[-1]},
        }

    def build_resource_pack(self, research_result, **kwargs):
        workflow = self.repo.get_workflow(kwargs.get("workflow_id", ""))
        source = workflow[0].get("source_context") if workflow else {}
        images = list(source.get("images") or [])[:2]
        assets = [{**image, "local_path": image.get("absolute_path")} for image in images]
        if source.get("model") == self.mismatched_pack_model:
            assets = []
            for index in range(2):
                wrong_path = Path(images[0]["absolute_path"]).parent / f"wrong_pack_{index}.png"
                Image.new("RGB", (128, 128), (20 + index * 70, 40, 60)).save(wrong_path)
                assets.append({"asset_id": f"wrong-pack-{index}", "local_path": str(wrong_path)})
        return {
            "resource_pack_id": f"pack-{kwargs.get('workflow_id')}",
            "version": 1,
            "brand": source.get("brand"),
            "model": source.get("model"),
            "assets": assets,
            "product_references": assets,
        }


def _make_batch(root: Path, products: list[tuple[str, str, int]]) -> tuple[Path, Path]:
    date_text = date.today().isoformat()
    inbox = root / "inbox"
    batch = inbox / date_text / "run_pipeline_test"
    batch.mkdir(parents=True)
    manifest_products = []
    catalog = root / "AUTO_QC_CANDIDATES.csv"
    with catalog.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["canonical_product_key", "identity_status", "pipeline_status"])
        for index, (key, model, image_count) in enumerate(products, 1):
            folder = f"p{index}"
            images = []
            for image_index in range(image_count):
                rel = f"products/{folder}/images/{folder}_{image_index}.png"
                image_path = batch / rel
                image_path.parent.mkdir(parents=True, exist_ok=True)
                background = (250, 250, 250) if image_index == 0 else (8, 8, 8)
                image = Image.new("RGB", (128, 128), background)
                draw = ImageDraw.Draw(image)
                if image_index == 0:
                    draw.ellipse([18, 18, 108, 108], fill=(25 + index * 20, 80, 210))
                else:
                    for y in range(0, 128, 16):
                        draw.rectangle([0, y, 127, y + 7], fill=(240, 180 + index * 10, 15))
                image.save(image_path)
                images.append({"asset_id": f"{folder}_asset_{image_index}", "relative_path": rel, "match_score": 1.0})
            brand, canonical_model = key.split(":", 1)
            manifest_products.append({
                "priority": index,
                "source_product_id": folder,
                "name": f"{brand} {model}",
                "brand": brand,
                "model": model,
                "canonical_product_key": key,
                "product_key": f"product-{folder}",
                "price_display": "1",
                "images": images,
            })
            writer.writerow([key, "VALID", "READY_FOR_IMAGE_QC"])
    manifest = {
        "schema_version": "1.0.0",
        "batch_id": "run_pipeline_test",
        "target_date": date_text,
        "production_date": date_text,
        "producer_version": "test",
        "products": manifest_products,
    }
    (batch / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (batch / "READY").write_text("READY\n", encoding="utf-8")
    return inbox, catalog


def _start_with_offline_worker(
    monkeypatch, db_path: Path, inbox: Path, catalog: Path, outcomes: dict[str, str],
    *, mismatched_pack_model: str | None = None, limit: int = 1, no_auto_approve: bool = True,
) -> tuple[int, _OfflineEvaluator]:
    evaluator = _OfflineEvaluator(outcomes)

    def worker_with_fake_provider(**kwargs):
        conn = connect(db_path)
        repo = WorkflowRepository(conn)
        handlers_service = ProductVideoJobHandlers(
            workflow_repository=repo,
            pi_client=_OfflinePI(repo, mismatched_pack_model=mismatched_pack_model),
            provider_mode="configured",
            evaluator=evaluator,
        )
        handlers = create_product_video_topic_handlers(
            workflow_repository=repo,
            handlers_service=handlers_service,
            provider_mode="configured",
        )
        # The real durable worker/outbox runs; only its evaluator is local and deterministic.
        try:
            return real_run_worker(
                db_path=db_path,
                provider_mode="fake",
                topic_handlers=handlers,
                once=True,
                batch_size=kwargs.get("batch_size", 8),
                workflow_ids=kwargs.get("workflow_ids"),
            )
        finally:
            conn.close()

    monkeypatch.setattr(coordinator_module, "run_worker", worker_with_fake_provider)
    expiry = (date.today() + timedelta(days=1)).isoformat() + "T23:59:00+00:00"
    cli_args = [
        "--db-path", str(db_path), "--inbox-root", str(inbox),
        "start", "--auto", "--limit", str(limit), "--date", date.today().isoformat(),
        "--provider-mode", "configured",
        "--catalog-v2-candidates", str(catalog),
        "--authorize-cost", "--qc-max-cost-usd", "2",
        "--qc-authorization-run-id", "offline-only",
        "--qc-authorization-scope", "image",
        "--qc-authorization-expires-at", expiry,
        "--qc-authorization-max-cost-usd", "2",
    ]
    if no_auto_approve:
        cli_args.append("--no-auto-approve")
    result = main(cli_args)
    return result, evaluator


def test_auto_approval_failure_stops_before_approving_next_product(tmp_path, monkeypatch, capsys) -> None:
    import video_factory.cli as cli_module

    inbox, catalog = _make_batch(tmp_path, [("brand:a", "A", 2), ("brand:b", "B", 2)])
    calls: list[str] = []

    def fail_approval(*args, **kwargs):
        calls.append(str(kwargs.get("target_id") or args[1]))
        raise RuntimeError("approval persistence failed")

    monkeypatch.setattr(cli_module, "approve_and_advance_workflow", fail_approval)
    result, _evaluator = _start_with_offline_worker(
        monkeypatch, tmp_path / "approval-stop.db", inbox, catalog, {}, limit=2, no_auto_approve=False,
    )

    assert result == 1
    assert len(calls) == 1
    captured = capsys.readouterr()
    assert "DỪNG BATCH" in (captured.out + captured.err)
    conn = connect(tmp_path / "approval-stop.db")
    from video_factory.workflows.steps import read_pipeline_steps

    failed_step = next(
        row for row in read_pipeline_steps(conn, calls[0])
        if row["step_name"] == "PRODUCT_LOCK"
    )
    assert failed_step["status"] == "pending_operator"
    assert failed_step["error_code"] == "AUTO_APPROVAL_FAILED"
    conn.close()


def test_cli_auto_admits_one_image_and_continues_after_semantic_fail(tmp_path, monkeypatch, capsys) -> None:
    inbox, catalog = _make_batch(tmp_path, [
        ("brand:a", "A", 1),
        ("brand:b", "B", 2),
        ("brand:c", "C", 2),
    ])
    db_path = tmp_path / "pipeline.db"
    result, evaluator = _start_with_offline_worker(
        monkeypatch, db_path, inbox, catalog, {"p2": "fail", "p3": "pass"}, limit=3
    )
    assert result == 0
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "APPROVED_IMAGE_MISMATCH" not in output

    conn = connect(db_path)
    imports = DailyImportRepository(conn).get_all_for_date(date.today().isoformat())
    products = {row["canonical_product_key"]: row for row in imports.values()}
    wf_repo = WorkflowRepository(conn)
    states = {}
    for canonical, row in products.items():
        workflow = wf_repo.get_workflow(row["workflow_id"])
        states[canonical] = workflow[0]["state"] if workflow else None
    assert products["brand:a"]["status"] == "started"
    assert states["brand:a"] == "AwaitingProductLock"
    assert products["brand:b"]["status"] == "error"
    assert states["brand:b"] == "skipped"
    failed_aggregate = wf_repo.get_workflow(products["brand:b"]["workflow_id"])[0]
    assert failed_aggregate.get("failure_code") == "EXCHANGE_IMAGE_QC_FAILED"
    assert products["brand:c"]["status"] == "started"
    assert states["brand:c"] == "AwaitingProductLock"

    qc_count = conn.execute("SELECT COUNT(*) FROM workflow_qc_retries").fetchone()[0]
    workflow_count = conn.execute("SELECT COUNT(*) FROM workflow").fetchone()[0]
    outbox_count = conn.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0]
    assert len(evaluator.calls) == 4
    assert [name.split("_")[0] for name in evaluator.calls] == ["p1", "p2", "p2", "p3"]
    conn.close()

    # A fresh worker process-equivalent poll and start replay must not duplicate workflow or QC work.
    restarted = real_run_worker(db_path=db_path, provider_mode="fake", topic_handlers={}, once=True, batch_size=20)
    replay_result = main([
        "--db-path", str(db_path), "--inbox-root", str(inbox),
        "start", "--auto", "--limit", "3", "--date", date.today().isoformat(),
        "--provider-mode", "fake", "--no-auto-approve", "--catalog-v2-candidates", str(catalog),
    ])
    assert replay_result == 0
    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM workflow").fetchone()[0] == workflow_count
    assert conn.execute("SELECT COUNT(*) FROM workflow_qc_retries").fetchone()[0] == qc_count
    assert conn.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0] == outbox_count
    assert len(evaluator.calls) == 4
    assert restarted is not None and restarted.claimed == 0
    conn.close()


def test_auto_reconsiders_legacy_catalog_rejection_with_current_exchange_admission(tmp_path, monkeypatch) -> None:
    inbox, catalog = _make_batch(tmp_path, [("brand:new", "New SKU", 2)])
    # Simulate the old static Catalog gate's persisted rejection for this batch.
    catalog.write_text("canonical_product_key,identity_status,pipeline_status\n", encoding="utf-8")
    db_path = tmp_path / "legacy-catalog-rejection.db"
    conn = connect(db_path)
    product_key = "product-p1"
    imported = DailyImportRepository(conn)
    claim = imported.claim_product(
        canonical_product_key="brand:new",
        source_date=date.today().isoformat(),
        source_batch_id="run_pipeline_test",
        source_product_id="p1",
        source_path=str(inbox / date.today().isoformat() / "run_pipeline_test" / "manifest.json"),
        worker_id="old-static-catalog-gate",
    )
    imported.mark_error(claim.product_key, "NOT_LISTED_IN_CATALOG_V2")
    conn.close()

    result, evaluator = _start_with_offline_worker(
        monkeypatch, db_path, inbox, catalog, {}, limit=1,
    )

    assert result == 0
    conn = connect(db_path)
    row = conn.execute(
        "SELECT status, last_error, workflow_id FROM daily_product_video_import WHERE canonical_product_key='brand:new'"
    ).fetchone()
    assert row["status"] == "started"
    assert row["last_error"] is None
    assert row["workflow_id"] == f"pv-daily-{claim.product_key[:16]}"
    assert len(evaluator.calls) == 1
    conn.close()


def test_auto_reclassifies_legacy_catalog_rejection_as_missing_images(tmp_path, monkeypatch) -> None:
    inbox, catalog = _make_batch(tmp_path, [("brand:no-images", "No Images", 0)])
    catalog.write_text("canonical_product_key,identity_status,pipeline_status\n", encoding="utf-8")
    db_path = tmp_path / "legacy-catalog-missing-images.db"
    conn = connect(db_path)
    imported = DailyImportRepository(conn)
    claim = imported.claim_product(
        canonical_product_key="brand:no-images",
        source_date=date.today().isoformat(),
        source_batch_id="run_pipeline_test",
        source_product_id="p1",
        source_path=str(inbox / date.today().isoformat() / "run_pipeline_test" / "manifest.json"),
        worker_id="old-static-catalog-gate",
    )
    imported.mark_error(claim.product_key, "NOT_LISTED_IN_CATALOG_V2")
    conn.close()

    result, evaluator = _start_with_offline_worker(
        monkeypatch, db_path, inbox, catalog, {}, limit=1,
    )

    assert result == 1  # CLI reports that every candidate was skipped.
    conn = connect(db_path)
    row = conn.execute(
        "SELECT status, last_error FROM daily_product_video_import WHERE canonical_product_key='brand:no-images'"
    ).fetchone()
    assert row["status"] == "error"
    assert "BLOCKED_INSUFFICIENT_IMAGES" in row["last_error"]
    assert len(evaluator.calls) == 0
    assert conn.execute("SELECT COUNT(*) FROM workflow").fetchone()[0] == 0
    conn.close()


def test_cli_auto_keeps_technical_unknown_waiting_without_selecting_next_candidate(tmp_path, monkeypatch, capsys) -> None:
    inbox, catalog = _make_batch(tmp_path, [("brand:u", "Unknown", 2), ("brand:n", "Next", 2)])
    db_path = tmp_path / "unknown.db"
    _, evaluator = _start_with_offline_worker(
        monkeypatch, db_path, inbox, catalog, {"p1": "technical_unknown", "p2": "pass"}
    )
    conn = connect(db_path)
    imports = DailyImportRepository(conn).get_all_for_date(date.today().isoformat())
    assert len(imports) == 1
    row = next(iter(imports.values()))
    aggregate = WorkflowRepository(conn).get_workflow(row["workflow_id"])[0]
    assert aggregate["state"].lower() == "waitingforoperator".lower()
    assert "unknown" in aggregate.get("operator_reason", "").lower()
    assert len(evaluator.calls) == 1
    assert conn.execute("SELECT COUNT(*) FROM workflow_outbox WHERE state='quarantined'").fetchone()[0] == 0
    attempt_count = conn.execute("SELECT COUNT(*) FROM workflow_qc_retries").fetchone()[0]
    conn.close()

    restarted = real_run_worker(db_path=db_path, provider_mode="fake", topic_handlers={}, once=True)
    replay_result = main([
        "--db-path", str(db_path), "--inbox-root", str(inbox),
        "start", "--auto", "--limit", "1", "--date", date.today().isoformat(),
        "--provider-mode", "fake", "--no-auto-approve", "--catalog-v2-candidates", str(catalog),
    ])
    conn = connect(db_path)
    assert restarted.claimed == 0
    assert replay_result == 1
    assert len(DailyImportRepository(conn).get_all_for_date(date.today().isoformat())) == 1
    assert conn.execute("SELECT COUNT(*) FROM workflow_qc_retries").fetchone()[0] == attempt_count
    assert len(evaluator.calls) == 1
    conn.close()


def test_cli_auto_first_candidate_reaches_lock_and_restart_is_idempotent(tmp_path, monkeypatch, capsys) -> None:
    inbox, catalog = _make_batch(tmp_path, [
        ("brand:a", "A", 2), ("brand:b", "B", 2), ("brand:c", "C", 2),
    ])
    db_path = tmp_path / "sha_mismatch.db"
    result, evaluator = _start_with_offline_worker(
        monkeypatch, db_path, inbox, catalog, {"p1": "pass", "p2": "fail", "p3": "pass"},
    )
    assert result == 0
    conn = connect(db_path)
    rows = DailyImportRepository(conn).get_all_for_date(date.today().isoformat())
    products = {row["canonical_product_key"]: row for row in rows.values()}
    assert products["brand:a"]["status"] == "started"
    assert WorkflowRepository(conn).get_workflow(products["brand:a"]["workflow_id"])[0]["state"] == "AwaitingProductLock"
    assert len(products) == 1
    assert len(evaluator.calls) == 1
    counts_before = (
        conn.execute("SELECT COUNT(*) FROM workflow").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM workflow_qc_retries").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM product_video_qc_reports").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM workflow_outbox WHERE state='quarantined'").fetchone()[0],
    )
    assert counts_before == (1, 1, 1, 0)
    conn.close()

    restarted = real_run_worker(db_path=db_path, provider_mode="fake", topic_handlers={}, once=True)
    assert main([
        "--db-path", str(db_path), "--inbox-root", str(inbox),
        "start", "--auto", "--limit", "1", "--date", date.today().isoformat(),
        "--provider-mode", "fake", "--no-auto-approve", "--catalog-v2-candidates", str(catalog),
    ]) == 0
    conn = connect(db_path)
    counts_after = (
        conn.execute("SELECT COUNT(*) FROM workflow").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM workflow_qc_retries").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM product_video_qc_reports").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM workflow_outbox WHERE state='quarantined'").fetchone()[0],
    )
    assert counts_after == counts_before
    assert restarted.claimed == 0
    assert len(evaluator.calls) == 1
    conn.close()


def test_manual_start_does_not_duplicate_terminally_rejected_workflow(tmp_path, monkeypatch, capsys) -> None:
    inbox, catalog = _make_batch(tmp_path, [("brand:b", "B", 2)])
    db_path = tmp_path / "quarantined.db"
    _start_with_offline_worker(monkeypatch, db_path, inbox, catalog, {"p1": "fail"})

    conn = connect(db_path)
    row = next(iter(DailyImportRepository(conn).get_all_for_date(date.today().isoformat()).values()))
    workflow_id = row["workflow_id"]
    original = WorkflowRepository(conn).get_workflow(workflow_id)
    assert original is not None
    assert row["status"] == "error"
    assert original[0]["state"] == "skipped"
    assert original[0]["failure_category"] == "PRODUCT_REJECTED"
    assert original[0]["failure_code"] == "EXCHANGE_IMAGE_QC_FAILED"
    original_outbox = conn.execute(
        "SELECT COUNT(*) FROM workflow_outbox WHERE workflow_id=? AND state='quarantined'",
        (workflow_id,),
    ).fetchone()[0]
    original_attempts = conn.execute(
        "SELECT COUNT(*) FROM workflow_qc_retries WHERE workflow_id=?", (workflow_id,),
    ).fetchone()[0]
    assert original_outbox == 0
    assert original_attempts == 1
    conn.close()

    main([
        "--db-path", str(db_path), "--inbox-root", str(inbox),
        "start", "--product-keys", "brand:b", "--date", date.today().isoformat(),
        "--provider-mode", "fake", "--no-research", "--no-auto-approve",
        "--catalog-v2-candidates", str(catalog),
    ])
    output = capsys.readouterr()
    assert "RESET_REQUIRED" not in output.out + output.err

    conn = connect(db_path)
    assert WorkflowRepository(conn).get_workflow(workflow_id) == original
    assert conn.execute(
        "SELECT COUNT(*) FROM workflow_outbox WHERE workflow_id=? AND state='quarantined'",
        (workflow_id,),
    ).fetchone()[0] == original_outbox
    assert conn.execute(
        "SELECT COUNT(*) FROM workflow_qc_retries WHERE workflow_id=?", (workflow_id,),
    ).fetchone()[0] == original_attempts
    conn.close()
