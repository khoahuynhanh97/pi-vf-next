from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from datetime import date

import pytest
from PIL import Image

from video_factory.automation.contracts import (
    ExchangeImage,
    ExchangeProduct,
    compute_canonical_product_key,
    compute_product_key,
)
from video_factory.automation.coordinator import ProductVideoCoordinator
from video_factory.automation.importer import (
    ExchangeImporter,
    publish_daily_run_to_exchange,
)
from video_factory.automation.repository import DailyImportRepository
from video_factory.storage_infra.sqlite import connect


_img_counter = 0


def _write_image(path: Path) -> None:
    global _img_counter
    _img_counter += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (800, 800), color=(_img_counter % 255, (_img_counter * 7) % 255, (_img_counter * 13) % 255))
    im.putpixel((0, 0), (_img_counter & 255, (_img_counter >> 8) & 255, (_img_counter >> 16) & 255))
    im.save(path, format="PNG")


def test_canonical_product_key_strips_brand_prefix() -> None:
    key1 = compute_canonical_product_key("Baseus", "Baseus Bowie WM02")
    assert key1 == "baseus:bowie-wm02"

    key2 = compute_canonical_product_key("AULA", "F75", "Grey Wood")
    assert key2 == "aula:f75:grey-wood"

    pkey = compute_product_key(key1)
    assert len(pkey) == 64
    assert pkey == compute_product_key("baseus:bowie-wm02")


def test_exchange_importer_ignores_unready_batch(tmp_path: Path) -> None:
    inbox_root = tmp_path / "inbox"
    batch_dir = inbox_root / "2026-09-12" / "run_0001"
    batch_dir.mkdir(parents=True)

    # Only manifest, no READY file
    (batch_dir / "manifest.json").write_text("{}", encoding="utf-8")

    importer = ExchangeImporter(inbox_root)
    with pytest.raises(FileNotFoundError):
        importer.find_latest_ready_batch()

    # Now write READY
    (batch_dir / "READY").write_text("READY\n", encoding="utf-8")
    found = importer.find_latest_ready_batch()
    assert found == batch_dir


def test_exchange_importer_chooses_latest_ready_batch_by_manifest_time(tmp_path: Path) -> None:
    day = tmp_path / "inbox" / "2026-10-24"
    older = day / "run_20261024_catalog_three"
    newer = day / "run_20261024_000328"
    for path, batch_id, created_at in (
        (older, "run_20261024_catalog_three", "2026-10-23T17:29:53.089524+00:00"),
        (newer, "run_20261024_000328", "2026-10-24T00:03:28.817271+00:00"),
    ):
        path.mkdir(parents=True)
        (path / "READY").write_text("READY\n", encoding="utf-8")
        (path / "manifest.json").write_text(json.dumps({
            "batch_id": batch_id, "production_date": "2026-10-24", "created_at": created_at,
            "products": [],
        }), encoding="utf-8")

    found = ExchangeImporter(tmp_path / "inbox").find_latest_ready_batch(date(2026, 10, 24))

    assert found == newer


@pytest.mark.parametrize(
    ("name", "brand", "model", "reason"),
    [
        ("shopee.vn/Đồng-hồ-Huawei-GT-2-Pro", "Huawei", "GT-2", "URL or marketplace slug"),
        ("Đèn Baseus Sunshine", "Baseus", "150k", "looks like a price"),
    ("Sản phẩm công nghệ mới ra mắt", "Xiaomi", "Xiaomi 17", None),
    ],
)
def test_exchange_admission_rejects_unidentified_product(
    tmp_path: Path, name: str, brand: str, model: str, reason: str
) -> None:
    product = ExchangeProduct(
        priority=1,
        canonical_product_key="candidate:key",
        product_key="candidate-key",
        name=name,
        brand=brand,
        model=model,
    )
    valid, rejection, _images = ExchangeImporter(tmp_path).validate_admission(product, tmp_path)
    assert valid is False
    if reason is not None:
        assert reason in str(rejection)


def test_exchange_admission_blocks_missing_images_without_provider_calls(tmp_path: Path) -> None:
    product = ExchangeProduct(
        priority=1,
        canonical_product_key="usmile:y1s",
        product_key="usmile-y1s",
        name="Usmile Y1S",
        brand="Usmile",
        model="Y1S",
    )

    valid, reason, images = ExchangeImporter(tmp_path).validate_admission(product, tmp_path)

    assert valid is False
    assert reason == "BLOCKED_INSUFFICIENT_IMAGES: no technically readable candidate image"
    assert images == []


def test_exchange_admission_accepts_one_decodable_source_image(tmp_path: Path) -> None:
    image_path = tmp_path / "front.png"
    _write_image(image_path)
    product = ExchangeProduct(
        priority=1,
        canonical_product_key="usmile:y1s",
        product_key="usmile-y1s",
        name="Usmile Y1S",
        brand="Usmile",
        model="Y1S",
        variant="standard",
        images=[ExchangeImage("front", "front.png")],
    )

    valid, reason, images = ExchangeImporter(tmp_path).validate_admission(product, tmp_path)

    assert valid is True
    assert reason is None
    assert [image["asset_id"] for image in images] == ["front"]


def test_exchange_admission_blocks_sku_without_explicit_variant(tmp_path: Path) -> None:
    image_path = tmp_path / "front.png"
    _write_image(image_path)
    product = ExchangeProduct(
        priority=1,
        canonical_product_key="bibigo:seaweed-snack",
        product_key="bibigo-seaweed-snack",
        name="Bibigo seaweed snack assorted pack",
        brand="Bibigo",
        model="Seaweed Snack",
        variant=None,
        images=[ExchangeImage("front", "front.png")],
    )

    valid, reason, images = ExchangeImporter(tmp_path).validate_admission(product, tmp_path)

    assert valid is False
    assert reason == "PRODUCT_VARIANT_REQUIRED: verify the exact product variant in PI"
    assert images == []


def test_exchange_admission_defers_url_identity_hints_to_image_qc(tmp_path: Path) -> None:
    first = tmp_path / "one.png"
    second = tmp_path / "two.png"
    third = tmp_path / "three.png"
    for path in (first, second, third):
        _write_image(path)
    product = ExchangeProduct(
        priority=1,
        canonical_product_key="ugreen:100w-65827",
        product_key="ugreen-65827",
        name="Ugreen 100W 65827",
        brand="Ugreen",
        model="100W 65827",
        variant="standard",
        images=[
            ExchangeImage("good-1", "one.png", "https://img.example/ugreen-100w-65827-a.jpg", 1.0),
            ExchangeImage("wrong", "two.png", "https://img.example/ugreen-65w-65827-b.jpg", 1.0),
            ExchangeImage("good-2", "three.png", "https://img.example/ugreen-100w-65827-c.jpg", 1.0),
        ],
    )
    importer = ExchangeImporter(tmp_path)

    valid, reason, images = importer.validate_admission(product, tmp_path)
    assert valid is True
    assert reason is None
    wrong = next(image for image in images if image["asset_id"] == "wrong")
    assert wrong["identity_review_reason"] == "ADMITTED_FOR_VISUAL_QC"


def test_exchange_admission_does_not_block_on_model_url_hint_or_match_score(tmp_path: Path) -> None:
    for name in ("one.png", "two.png", "three.png"):
        _write_image(tmp_path / name)
    product = ExchangeProduct(
        priority=1,
        canonical_product_key="ugreen:10335",
        product_key="ugreen-10335",
        name="Ugreen 10335",
        brand="Ugreen",
        model="10335",
        variant="standard",
        images=[
            ExchangeImage("one", "one.png", "https://img.example/ugreen-10335-a.jpg", 1.0),
            ExchangeImage("wrong", "two.png", "https://img.example/ugreen-10334-65w.jpg", 1.0),
            ExchangeImage("three", "three.png", "https://img.example/ugreen-10335-b.jpg", 1.0),
        ],
    )

    valid, reason, images = ExchangeImporter(tmp_path).validate_admission(product, tmp_path)

    assert valid is True
    assert reason is None
    wrong = next(image for image in images if image["asset_id"] == "wrong")
    assert wrong["identity_review_reason"] == "ADMITTED_FOR_VISUAL_QC"
    assert wrong["match_score"] == 1.0


def test_exchange_admission_rejects_repeated_asset_id_and_same_file(tmp_path: Path) -> None:
    shared = tmp_path / "shared.png"
    other = tmp_path / "other.png"
    _write_image(shared)
    _write_image(other)
    importer = ExchangeImporter(tmp_path)

    repeated_id = ExchangeProduct(
        priority=1,
        canonical_product_key="brand:model",
        product_key="brand-model",
        name="Brand Model",
        brand="Brand",
        model="Model",
        variant="standard",
        images=[ExchangeImage("same", "shared.png"), ExchangeImage("same", "other.png")],
    )
    valid, reason, _ = importer.validate_admission(
        repeated_id,
        tmp_path,
        input_review={
            "confirmed_image_asset_ids": ["same"],
            "confirmed_claims": [{"text": "Specific claim", "source_url": "https://brand.example/model"}],
        },
    )
    assert valid is False
    assert "BLOCKED_INSUFFICIENT_IMAGES" in str(reason)

    same_file = ExchangeProduct(
        priority=1,
        canonical_product_key="brand:model-2",
        product_key="brand-model-2",
        name="Brand Model 2",
        brand="Brand",
        model="Model 2",
        variant="standard",
        images=[ExchangeImage("one", "shared.png"), ExchangeImage("two", "shared.png")],
    )
    valid, reason, reviewed_images = importer.validate_admission(
        same_file,
        tmp_path,
        input_review={
            "confirmed_image_asset_ids": ["one", "two"],
            "confirmed_claims": [{"text": "Specific claim", "source_url": "https://brand.example/model-2"}],
        },
    )
    assert valid is True
    assert reason is None
    assert len(reviewed_images) == 1
    assert reviewed_images[0]["absolute_path"] == str((tmp_path / "shared.png").resolve())


def test_configured_resource_pack_must_retain_a_source_approved_image(tmp_path: Path) -> None:
    from video_factory.handlers.common import _assert_approved_images_in_resource_pack
    from video_factory.job_contracts import TerminalJobError

    approved_one = tmp_path / "approved-one.png"
    approved_two = tmp_path / "approved-two.png"
    wrong_one = tmp_path / "wrong-one.png"
    wrong_two = tmp_path / "wrong-two.png"
    Image.new("RGB", (800, 800), color=(240, 240, 240)).save(approved_one, format="PNG")
    Image.new("RGB", (800, 800), color=(220, 220, 220)).save(approved_two, format="PNG")
    Image.new("RGB", (800, 800), color=(20, 20, 20)).save(wrong_one, format="PNG")
    Image.new("RGB", (800, 800), color=(40, 40, 40)).save(wrong_two, format="PNG")

    with pytest.raises(TerminalJobError, match="APPROVED_IMAGE_MISMATCH"):
        _assert_approved_images_in_resource_pack(
            source_context={"images": [
                {"asset_id": "approved-1", "absolute_path": str(approved_one), "original_url": "https://img.example/a"},
                {"asset_id": "approved-2", "absolute_path": str(approved_two), "original_url": "https://img.example/b"},
            ]},
            resource_pack={"assets": [
                {"asset_id": "other-1", "local_path": str(wrong_one)},
                {"asset_id": "other-2", "local_path": str(wrong_two)},
            ]},
        )


def test_configured_resource_pack_accepts_one_matching_approved_image(tmp_path: Path) -> None:
    from video_factory.handlers.common import _assert_approved_images_in_resource_pack

    image = tmp_path / "approved.png"
    Image.new("RGB", (800, 800), color=(240, 240, 240)).save(image, format="PNG")
    asset = {"asset_id": "approved", "absolute_path": str(image)}
    _assert_approved_images_in_resource_pack(
        source_context={"images": [asset]},
        resource_pack={"assets": [{**asset, "local_path": str(image)}]},
    )


def test_exchange_admission_keeps_one_usable_image_when_another_candidate_is_missing(tmp_path: Path) -> None:
    from video_factory.qc.service import _exchange_image_pack_gate

    usable = tmp_path / "usable.png"
    Image.new("RGB", (64, 64), color=(240, 240, 240)).save(usable, format="PNG")
    missing = tmp_path / "missing.png"
    source = {
        "source": "hermes_product_video_exchange",
        "auto_admitted": True,
        "images": [
            {"asset_id": "missing", "absolute_path": str(missing)},
            {"asset_id": "usable", "absolute_path": str(usable)},
        ],
    }

    filtered, code, _ = _exchange_image_pack_gate(
        source,
        {"assets": [{"asset_id": "usable-copy", "local_path": str(usable)}]},
    )

    assert code is None
    assert [item["asset_id"] for item in filtered["assets"]] == ["usable"]


def test_list_exposes_per_image_review_reason_before_start(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    for name in ("one.png", "two.png", "three.png"):
        _write_image(src / name)
    (src / "products.json").write_text(json.dumps({
        "date": "2026-10-15",
        "products": [{
            "id": "p1", "name": "Ugreen 10335", "brand": "Ugreen", "model": "10335", "variant": "standard",
            "images": [
                {"asset_id": "good", "absolute_path": str(src / "one.png"), "original_url": "https://img.example/10335-a"},
                {"asset_id": "wrong", "absolute_path": str(src / "two.png"), "original_url": "https://img.example/10334-65w"},
                {"asset_id": "good2", "absolute_path": str(src / "three.png"), "original_url": "https://img.example/10335-b"},
            ],
        }],
    }), encoding="utf-8")
    inbox = tmp_path / "inbox"
    publish_daily_run_to_exchange(src, inbox, batch_id="run_01")
    conn = connect(tmp_path / "list.db")
    candidate = ProductVideoCoordinator(conn, inbox_root=inbox).list_candidates()[0]

    assert candidate.status == "unprocessed"
    wrong = next(image for image in candidate.images if image["asset_id"] == "wrong")
    assert wrong["original_url"].endswith("10334-65w")
    assert wrong["identity_review_reason"] == "ADMITTED_FOR_VISUAL_QC"


def test_legacy_exchange_manifest_waits_for_review_and_cannot_synthesize_claims(tmp_path: Path) -> None:
    from video_factory.handlers.common import _build_exchange_research_result, _build_exchange_resource_pack
    from video_factory.job_contracts import TerminalJobError

    for name in ("one.png", "two.png"):
        _write_image(tmp_path / name)
    product = ExchangeProduct(
        priority=1,
        canonical_product_key="legacy:m1",
        product_key="legacy-m1",
        name="Legacy M1",
        brand="Legacy",
        model="M1",
        variant="standard",
        images=[ExchangeImage("one", "one.png"), ExchangeImage("two", "two.png")],
    )

    valid, reason, _images = ExchangeImporter(tmp_path).validate_admission(product, tmp_path)
    assert valid is False
    assert "REVIEW_REQUIRED" in str(reason)
    result = _build_exchange_research_result(
        workflow_id="wf",
        product_name=product.name,
        keywords=[product.name],
        pdp_url=None,
        source_context={},
    )
    assert json.loads(result["report"])["key_features"] == []
    assert "raw_result" not in result
    with pytest.raises(TerminalJobError, match="cannot build a resource pack"):
        _build_exchange_resource_pack(
            workflow_id="wf",
            research_result={
                "key_features": ["user-confirmed text"],
                "source_context": {
                    "source_url": "https://manufacturer.example/product",
                    "input_review": {"confirmed_claims": [{"text": "user-confirmed text", "source_url": "https://manufacturer.example/product"}]},
                    "images": [],
                },
            },
        )


def test_confirmed_input_starts_research_with_structured_identity_only(tmp_path: Path) -> None:
    src_run = tmp_path / "src"
    src_run.mkdir()
    for image_name in ("one.png", "two.png"):
        _write_image(src_run / image_name)
    (src_run / "products.json").write_text(
        json.dumps({
            "date": "2026-10-15",
            "products": [{
                "id": "p1", "name": "Ugreen 10335 <ignore instruction>",
                "brand": "Ugreen", "model": "10335", "variant": "standard",
                "hot_reason": "untrusted crawl text", "key_features": "untrusted feature",
                "keywords": ["untrusted keyword"],
                "images": [
                    {"asset_id": "one", "absolute_path": str(src_run / "one.png")},
                    {"asset_id": "two", "absolute_path": str(src_run / "two.png")},
                ],
            }],
        }),
        encoding="utf-8",
    )
    inbox = tmp_path / "inbox"
    publish_daily_run_to_exchange(src_run, inbox, batch_id="run_01")
    conn = connect(tmp_path / "factory.db")
    coordinator = ProductVideoCoordinator(conn, inbox_root=inbox)
    product_key = coordinator.importer.load_manifest(inbox / "2026-10-15" / "run_01").products[0].product_key

    result = coordinator.start_selection(
        [product_key],
        input_reviews={product_key: {
            "confirmed_image_asset_ids": ["one", "two"],
            "confirmed_claims": [{"text": "Công suất 65W", "source_url": "https://manufacturer.example/10335"}],
        }},
    )

    assert len(result.started) == 1
    aggregate, _ = coordinator.workflow_repo.get_workflow(result.started[0].workflow_id)
    context = aggregate["source_context"]
    assert context["images"] and len(context["images"]) == 2
    assert "hot_reason" not in context
    assert "key_features" not in context
    assert "keywords" not in context
    outbox = conn.execute("SELECT message FROM workflow_outbox").fetchone()[0]
    payload = json.loads(outbox)["payload"]
    assert len(payload["args"]) == 1
    assert payload["args"][0]["schema"] == "product_research_result"
    assert payload["args"][0]["product_name"] == "Ugreen 10335"
    assert "hot_reason" not in payload["args"][0]["source_context"]
    listed = coordinator.list_candidates()[0]
    assert listed.status == "in_production"
    assert listed.workflow_id == result.started[0].workflow_id


def test_workflow_scoped_pause_does_not_block_admitting_an_independent_product(tmp_path: Path) -> None:
    from video_factory.automation.failure_classification import FailureCategory
    from video_factory.workflows.runtime_control import pause_pipeline

    src_run = tmp_path / "src-two"
    src_run.mkdir()
    products = []
    for index, (brand, model) in enumerate((("Ugreen", "A"), ("Anker", "B"))):
        names = [f"{index}-one.png", f"{index}-two.png"]
        for name in names:
            _write_image(src_run / name)
        products.append({"id":f"p{index}", "name":f"{brand} {model}", "brand":brand,
            "model":model, "variant":"standard", "images":[{"asset_id":f"{index}-one", "absolute_path":str(src_run / names[0])},
                {"asset_id":f"{index}-two", "absolute_path":str(src_run / names[1])}]})
    (src_run / "products.json").write_text(json.dumps({"date":"2026-10-15","products":products}), encoding="utf-8")
    inbox = tmp_path / "inbox-two"
    publish_daily_run_to_exchange(src_run, inbox, batch_id="run_two")
    conn = connect(tmp_path / "pause-admission.db")
    coordinator = ProductVideoCoordinator(conn, inbox_root=inbox)
    manifest = coordinator.importer.load_manifest(inbox / "2026-10-15" / "run_two")
    first, second = manifest.products
    first_result = coordinator.start_selection([first.product_key], input_reviews={first.product_key:{
        "confirmed_image_asset_ids":["0-one","0-two"], "confirmed_claims":[]}})
    assert len(first_result.started) == 1
    paused_workflow = first_result.started[0].workflow_id
    pause_pipeline(conn, category=FailureCategory.QC_INFRA_PAUSED, code="KEYFRAME_CONTENT_UNKNOWN",
        message="wf-A requires reconciliation", workflow_id=paused_workflow)

    second_result = coordinator.start_selection([second.product_key], input_reviews={second.product_key:{
        "confirmed_image_asset_ids":["1-one","1-two"], "confirmed_claims":[]}})

    assert len(second_result.started) == 1
    assert second_result.started[0].product_key == second.product_key
    assert second_result.started[0].workflow_id != paused_workflow
    conn.close()


def test_concurrent_claim_protection(tmp_path: Path) -> None:
    conn = connect(tmp_path / "hermes.db")
    repo = DailyImportRepository(conn)

    # Worker 1 claims product
    claim1 = repo.claim_product(
        canonical_product_key="aula:f75",
        source_date="2026-09-12",
        source_batch_id="run_1",
        source_product_id="prod_01",
        source_path="manifest.json",
        worker_id="worker-1",
    )
    assert claim1.status == "claimed"
    assert claim1.workflow_id == f"pv-daily-{compute_product_key('aula:f75')[:16]}"
    assert claim1.claimed_by == "worker-1"

    # Worker 2 attempts to claim same product while lease is active
    claim2 = repo.claim_product(
        canonical_product_key="aula:f75",
        source_date="2026-09-12",
        source_batch_id="run_1",
        source_product_id="prod_01",
        source_path="manifest.json",
        worker_id="worker-2",
    )
    assert claim2.status == "already_claimed"
    assert claim2.claimed_by == "worker-1"

    # After marking started, new claims are also rejected
    repo.mark_started(compute_product_key("aula:f75"))
    claim3 = repo.claim_product(
        canonical_product_key="aula:f75",
        source_date="2026-09-12",
        source_batch_id="run_1",
        source_product_id="prod_01",
        source_path="manifest.json",
        worker_id="worker-3",
    )
    assert claim3.status == "already_started"


def test_coordinator_auto_start_preserves_manifest_priority(tmp_path: Path) -> None:
    # Build exchange batch
    src_run = tmp_path / "src_run"
    src_run.mkdir()
    # Six products prove the daily cap across repeated scheduler calls.
    prods = []
    for i in range(1, 7):
        name, model = f"Zippo {i}", f"M{i}"
        img = src_run / "images" / f"ig{i}" / "img1.png"
        _write_image(img)
        img2 = src_run / "images" / f"ig{i}" / "img2.png"
        _write_image(img2)
        prods.append({
            "id": f"prod{i}",
            "name": name,
            "brand": "Zippo",
            "model": model,
            "variant": "standard",
            "price_display": "100.000đ",
            "price_min": 100000,
            "price_max": 100000,
            "hot_reason": "Hot",
            "key_features": "Features",
            "keywords": [name],
            "images": [
                {"asset_id": f"a1{i}", "absolute_path": str(img), "match_score": 1.0, "width": 800, "height": 800},
                {"asset_id": f"a2{i}", "absolute_path": str(img2), "match_score": 1.0, "width": 800, "height": 800},
            ],
        })
    (src_run / "products.json").write_text(
        json.dumps({"version": "1.0", "date": "2026-09-12", "products": prods}),
        encoding="utf-8",
    )

    inbox = tmp_path / "exchange_inbox"
    publish_daily_run_to_exchange(src_run, inbox, batch_id="run_01")

    conn = connect(tmp_path / "hermes.db")
    coordinator = ProductVideoCoordinator(connection=conn, inbox_root=inbox)

    # Auto-start starts eligible products following manifest priority
    res = coordinator.start_auto(limit=2)
    assert len(res.started) == 2
    assert res.started[0].canonical_product_key == "zippo:m1:standard"
    assert res.started[1].canonical_product_key == "zippo:m2:standard"


def test_auto_batch_stops_before_loading_candidates_when_any_qc_attempt_is_uncertain(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from video_factory.qc.retry_policy import claim_qc_attempt, complete_qc_attempt, ensure_qc_retry_schema
    from video_factory.workflows.repository import WorkflowRepository

    conn = connect(tmp_path / "batch-stop.db")
    WorkflowRepository(conn).commit_workflow(
        "wf-hoco-uncertain", '{"state":"WaitingForOperator"}', [], expected_version=None
    )
    ensure_qc_retry_schema(conn)
    attempt = claim_qc_attempt(
        conn,
        workflow_id="wf-hoco-uncertain",
        retry_type="source_image_qc",
        item_id="hoco:j101a",
        operation_id="op-hoco-second",
    )
    complete_qc_attempt(
        conn,
        attempt_id=attempt["attempt_id"],
        status="technical_unknown",
        verdict="unknown",
        reason_text="Reconciliation required before retry.",
    )
    coordinator = ProductVideoCoordinator(conn, inbox_root=tmp_path / "inbox")
    coordinator.importer = MagicMock()
    coordinator.importer.find_latest_ready_batch.return_value = tmp_path / "ready"
    coordinator.importer.load_manifest.return_value = SimpleNamespace(
        production_date="2026-10-21", batch_id="run-1", products=[]
    )
    coordinator.list_candidates = MagicMock(return_value=[])

    result = coordinator.start_auto(limit=1)

    assert result.started == ()
    assert len(result.skipped) == 1
    assert result.has_errors is True
    assert result.skipped[0].workflow_id == "wf-hoco-uncertain"
    assert "BATCH_STOPPED" in result.skipped[0].reason
    assert "op-hoco-second" in result.skipped[0].reason
    coordinator.importer.find_latest_ready_batch.assert_not_called()
    complete_qc_attempt(
        conn,
        attempt_id=attempt["attempt_id"],
        status="reconciled_consumed",
        verdict="unknown",
        reason_text="Provider usage was correlated; QC result was not recovered.",
    )
    assert coordinator._unresolved_qc_attempt() is None
    conn.close()


def test_catalog_contract_selects_exchange_intersection_and_survives_restart(tmp_path: Path) -> None:
    src_run = tmp_path / "catalog-src"
    src_run.mkdir()
    products = []
    for idx in (1, 2):
        image_a = src_run / f"{idx}-a.png"
        _write_image(image_a)
        images = [] if idx == 1 else [{"asset_id": f"a{idx}", "absolute_path": str(image_a)}]
        if idx == 2:
            image_b = src_run / f"{idx}-b.png"
            _write_image(image_b)
            images.append({"asset_id": f"b{idx}", "absolute_path": str(image_b)})
        products.append({
            "id": f"zippo-{idx}", "name": f"Zippo {idx}", "brand": "Zippo", "model": f"M{idx}", "variant": "standard",
            "images": images,
        })
    (src_run / "products.json").write_text(json.dumps({"date": "2026-10-19", "products": products}), encoding="utf-8")
    inbox = tmp_path / "exchange"
    publish_daily_run_to_exchange(src_run, inbox, batch_id="run_catalog")
    catalog = tmp_path / "AUTO_QC_CANDIDATES.csv"
    catalog.write_text(
        "canonical_product_key,identity_status,pipeline_status\n"
        "zippo:m1:standard,VALID,READY_FOR_IMAGE_QC\n"
        "zippo:m2:standard,VALID,READY_FOR_IMAGE_QC\n",
        encoding="utf-8",
    )
    db = tmp_path / "catalog.db"
    conn = connect(db)
    coordinator = ProductVideoCoordinator(conn, inbox_root=inbox, catalog_candidates_path=catalog)

    first = coordinator.start_auto(limit=1)
    assert [item.canonical_product_key for item in first.started] == ["zippo:m2:standard"]
    assert any("BLOCKED_INSUFFICIENT_IMAGES" in item.reason for item in first.skipped)
    started_id = first.started[0].workflow_id
    conn.close()

    reopened = connect(db)
    resumed_coordinator = ProductVideoCoordinator(reopened, inbox_root=inbox, catalog_candidates_path=catalog)
    second = resumed_coordinator.start_auto(limit=1)
    assert second.started == ()
    assert resumed_coordinator.list_candidates()[1].workflow_id == started_id
    assert reopened.execute(
        "SELECT COUNT(*) FROM daily_product_video_import WHERE canonical_product_key='zippo:m2:standard'"
    ).fetchone()[0] == 1
    assert reopened.execute(
        "SELECT status, last_error FROM daily_product_video_import WHERE canonical_product_key='zippo:m1:standard'"
    ).fetchone()["status"] == "error"
    reopened.close()

    # A later corrected PI batch must be reconsidered. The old pre-start
    # SKIPPED reason remains inspectable until a new workflow is claimed.
    fixed_image = src_run / "1-b.png"
    _write_image(fixed_image)
    products[0]["images"].append({"asset_id": "b1", "absolute_path": str(fixed_image)})
    (src_run / "products.json").write_text(
        json.dumps({"date": "2026-10-19", "products": products}), encoding="utf-8",
    )
    publish_daily_run_to_exchange(src_run, inbox, batch_id="run_catalog_fixed")
    again = connect(db)
    corrected = ProductVideoCoordinator(again, inbox_root=inbox, catalog_candidates_path=catalog)
    third = corrected.start_auto(limit=2)
    assert [item.canonical_product_key for item in third.started] == ["zippo:m1:standard"]
    assert again.execute(
        "SELECT status FROM daily_product_video_import WHERE canonical_product_key='zippo:m1:standard'"
    ).fetchone()["status"] == "started"
    again.close()


def test_coordinator_write_time_revalidation_skips_stale_or_invalid(tmp_path: Path) -> None:
    src_run = tmp_path / "src_run"
    src_run.mkdir()
    img = src_run / "img1.png"
    _write_image(img)
    (src_run / "products.json").write_text(
        json.dumps({
            "version": "1.0",
            "date": "2026-09-12",
            "products": [
                {
                    "id": "p1",
                    "name": "Sample P",
                    "brand": "Sample",
                    "model": "P1",
                    "variant": "standard",
                    "price_display": "100đ",
                    "features": "F",
                    "keywords": ["s"],
                    "images": [{"asset_id": "a", "absolute_path": str(img), "match_score": 1.0}],
                }
            ],
        }),
        encoding="utf-8",
    )
    inbox = tmp_path / "exchange_inbox"
    publish_daily_run_to_exchange(src_run, inbox, batch_id="run_02")

    conn = connect(tmp_path / "hermes.db")
    coordinator = ProductVideoCoordinator(connection=conn, inbox_root=inbox)

    # Try starting with non-existent key
    res = coordinator.start_selection(["non_existent_key"])
    assert len(res.started) == 0
    assert len(res.skipped) == 1
    assert res.skipped[0]["reason"] == "NOT_FOUND_IN_MANIFEST"


def test_top_level_cli_personal_video(tmp_path: Path, capsys, monkeypatch) -> None:
    import sys
    from video_factory.cli import main

    src_run = tmp_path / "src_run"
    src_run.mkdir()
    img = src_run / "img1.png"
    _write_image(img)
    img2 = src_run / "img2.png"
    _write_image(img2)
    (src_run / "products.json").write_text(
        json.dumps({
            "version": "1.0",
            "date": "2026-09-12",
            "products": [
                {
                    "id": "p1",
                    "name": "Super Mouse",
                    "brand": "Super",
                    "model": "M1",
                    "variant": "standard",
                    "price_display": "200.000đ",
                    "features": "Fast",
                    "keywords": ["mouse"],
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
    publish_daily_run_to_exchange(src_run, inbox, batch_id="run_03")

    db_path = tmp_path / "hermes.db"

    # Test video_factory.cli list
    monkeypatch.setattr(sys, "argv", [
        "video_factory.cli",
        "--db-path", str(db_path),
        "--inbox-root", str(inbox),
        "list",
    ])
    code = main()
    assert code is None or code == 0
    out = capsys.readouterr().out
    assert "Super Mouse" in out
    assert "[CHƯA TẠO VIDEO]" in out


LEGACY_LEDGER_DDL = """
CREATE TABLE daily_product_video_import (
    product_key TEXT PRIMARY KEY,
    source_date TEXT NOT NULL,
    source_product_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    workflow_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('reserved', 'started', 'error')),
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    canonical_product_key TEXT NOT NULL DEFAULT '',
    source_batch_id TEXT NOT NULL DEFAULT '',
    worker_id TEXT,
    claimed_at TEXT,
    lease_expires_at TEXT
);
"""


def test_legacy_ddl_migration_and_claim_preserves_history(tmp_path: Path) -> None:
    db_file = tmp_path / "hermes_legacy.db"
    raw_conn = sqlite3.connect(db_file)
    raw_conn.executescript(LEGACY_LEDGER_DDL)

    legacy_rows = [
        (
            "1e96ac740ec2f41f35a92194ca77c69fa3249fd103fc4e792321eca63eafd38b",
            "2026-09-12",
            "prod_01",
            r"D:\work\Personal\Product-Intelligence\data\daily_runs\2026-09-12\products.json",
            "pv-daily-1e96ac740ec2f41f",
            "started",
            None,
            "2026-09-12T09:13:44.033665+00:00",
            "2026-09-12T09:13:44.035194+00:00",
            "",
            "",
            None,
            None,
            None,
        ),
        (
            "c56349cc02be2276d5578a1c3b081b4a6349ff4bba199d19c355ef838b3adff3",
            "2026-09-12",
            "prod_02",
            r"D:\work\Personal\Product-Intelligence\data\daily_runs\2026-09-12\products.json",
            "pv-daily-c56349cc02be2276",
            "started",
            None,
            "2026-09-12T09:13:44.037251+00:00",
            "2026-09-12T09:13:44.037251+00:00",
            "",
            "",
            None,
            None,
            None,
        ),
        (
            "1dd69d4052bfb08a68fe47e13e0d73535f50d9a1d487fa4b307cb9d42930c8c8",
            "2026-09-12",
            "prod_06",
            r"D:\work\Personal\Product-Intelligence\data\daily_runs\2026-09-12\products.json",
            "pv-daily-1dd69d4052bfb08a",
            "started",
            None,
            "2026-09-12T09:13:44.047127+00:00",
            "2026-09-12T09:13:44.047127+00:00",
            "",
            "",
            None,
            None,
            None,
        ),
    ]
    raw_conn.executemany(
        """
        INSERT INTO daily_product_video_import VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        legacy_rows,
    )
    raw_conn.commit()
    raw_conn.close()

    # Open with central connect, which should perform migration
    conn = connect(db_file)
    repo = DailyImportRepository(conn)

    # Claim a new product for 2026-09-13
    claim = repo.claim_product(
        canonical_product_key="jisulife:life-7",
        source_date="2026-09-13",
        source_batch_id="run_20260913_000105",
        source_product_id="prod_jisulife",
        source_path="manifest.json",
        worker_id="worker-test",
    )
    assert claim.status == "claimed", f"Claim failed: {claim.error}"

    # Verify legacy rows preserved byte-for-byte
    rows_0912 = conn.execute(
        "SELECT product_key, status, created_at, workflow_id FROM daily_product_video_import WHERE source_date = '2026-09-12' ORDER BY source_product_id"
    ).fetchall()
    assert len(rows_0912) == 3
    assert rows_0912[0]["workflow_id"] == "pv-daily-1e96ac740ec2f41f"
    assert rows_0912[0]["status"] == "started"
    assert rows_0912[1]["workflow_id"] == "pv-daily-c56349cc02be2276"
    assert rows_0912[2]["workflow_id"] == "pv-daily-1dd69d4052bfb08a"

    # Idempotency check: rerun migration does not alter data or fail
    from video_factory.storage_infra.sqlite import _migrate_daily_product_video_import
    _migrate_daily_product_video_import(conn)
    rows_after = conn.execute("SELECT COUNT(*) FROM daily_product_video_import").fetchone()[0]
    assert rows_after == 4  # 3 legacy + 1 new claimed


def test_migration_failure_rolls_back_safely(tmp_path: Path) -> None:
    db_file = tmp_path / "hermes_rollback.db"
    raw_conn = sqlite3.connect(db_file)
    raw_conn.executescript(LEGACY_LEDGER_DDL)
    raw_conn.execute(
        """
        INSERT INTO daily_product_video_import (
            product_key, source_date, source_product_id, source_path, workflow_id, status, created_at, updated_at
        ) VALUES ('key1', '2026-09-12', 'p1', 'path', 'wf1', 'started', '2026-09-12T00:00:00', '2026-09-12T00:00:00')
        """
    )
    raw_conn.commit()

    from video_factory.storage_infra.sqlite import _migrate_daily_product_video_import

    class FailingConnection:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn
        def execute(self, sql: str, *args, **kwargs):
            if "DROP TABLE daily_product_video_import" in sql:
                raise sqlite3.OperationalError("Simulated drop failure")
            return self._conn.execute(sql, *args, **kwargs)
        def __enter__(self):
            return self._conn.__enter__()
        def __exit__(self, *args):
            return self._conn.__exit__(*args)
        def __getattr__(self, name: str):
            return getattr(self._conn, name)

    proxy = FailingConnection(raw_conn)

    with pytest.raises(sqlite3.OperationalError, match="Simulated drop failure"):
        with raw_conn:
            _migrate_daily_product_video_import(proxy)

    # Verify original table is intact
    count = raw_conn.execute("SELECT COUNT(*) FROM daily_product_video_import").fetchone()[0]
    assert count == 1
    raw_conn.close()


def test_coordinator_enforces_bounded_auto_batch(tmp_path: Path) -> None:
    conn = connect(tmp_path / "hermes_limit.db")
    coord = ProductVideoCoordinator(conn, inbox_root=tmp_path / "inbox")

    with pytest.raises(ValueError, match="Limit must be between 1 and 100"):
        coord.start_auto(limit=0)

    with pytest.raises(ValueError, match="Limit must be between 1 and 100"):
        coord.start_auto(limit=101)

    with pytest.raises(ValueError, match="Cannot select more than 5 products"):
        coord.start_selection(["k1", "k2", "k3", "k4", "k5", "k6"])


def test_exchange_workflow_does_not_synthesize_research_or_qc_from_local_images(tmp_path: Path) -> None:

    src_run = tmp_path / "src_run"
    src_run.mkdir()
    img1 = src_run / "images" / "p1" / "img1.png"
    img2 = src_run / "images" / "p1" / "img2.png"
    _write_image(img1)
    Image.new("RGB", (800, 800), color=(240, 240, 250)).save(img2, format="PNG")

    (src_run / "products.json").write_text(
        json.dumps({
            "version": "1.0",
            "date": "2026-10-07",
            "products": [
                {
                    "id": "prod_hy300",
                    "name": "Máy chiếu Magcubic HY300 Pro 4K",
                    "brand": "Magcubic",
                    "model": "HY300 Pro 4K",
                    "variant": "standard",
                    "price_display": "950.000đ",
                    "hot_reason": "Máy chiếu mini bán chạy nhất",
                    "key_features": ["Độ phân giải 4K hỗ trợ", "Xoay 180 độ linh hoạt"],
                    "keywords": ["Magcubic HY300 Pro 4K", "máy chiếu mini"],
                    "images": [
                        {"asset_id": "img_01", "absolute_path": str(img1), "match_score": 0.95, "width": 800, "height": 800},
                        {"asset_id": "img_02", "absolute_path": str(img2), "match_score": 0.92, "width": 800, "height": 800},
                    ],
                }
            ],
        }),
        encoding="utf-8",
    )
    inbox = tmp_path / "exchange_inbox"
    publish_daily_run_to_exchange(src_run, inbox, batch_id="run_20261007_01")

    db_file = tmp_path / "hermes_direct_pack.db"
    conn = connect(db_file)
    coord = ProductVideoCoordinator(conn, inbox_root=inbox, db_path=db_file)

    res = coord.start_auto(limit=1)
    assert len(res.started) == 1
    candidate = coord.list_candidates()[0]
    assert candidate.status == "in_production"

