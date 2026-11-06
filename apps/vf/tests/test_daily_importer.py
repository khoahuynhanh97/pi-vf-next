from __future__ import annotations

import json
import io
import sys
from pathlib import Path

from PIL import Image

from video_factory.daily_importer import import_daily_products, main
from video_factory.storage_infra.sqlite import connect


def _write_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (800, 800), color=(255, 255, 255)).save(path, format="PNG")


def _product(
    batch_dir: Path,
    index: int,
    *,
    name: str,
    brand: str,
    model: str,
    image_count: int = 2,
    match_score: float = 1.0,
) -> dict:
    images = []
    for image_index in range(image_count):
        image_path = batch_dir / "images" / f"product-{index}" / f"image-{image_index}.png"
        _write_image(image_path)
        images.append(
            {
                "asset_id": f"asset-{index}-{image_index}",
                "absolute_path": str(image_path),
                "match_score": match_score,
                "width": 800,
                "height": 800,
            }
        )
    return {
        "id": f"prod_{index:02d}",
        "name": name,
        "brand": brand,
        "model": model,
        "hot_reason": "Có thể trình diễn rõ trong video",
        "key_features": "Tính năng A, tính năng B",
        "keywords": [name, f"{brand} {model}"],
        "image_count": len(images),
        "images": images,
    }


def test_imports_first_eligible_products_in_order_and_replay_is_safe(tmp_path: Path) -> None:
    source_root = tmp_path / "daily_runs"
    batch_dir = source_root / "2026-09-12"
    batch_dir.mkdir(parents=True)

    products = [
        _product(batch_dir, 1, name="Quạt Jisulife Life 7", brand="Jisulife", model="Life 7"),
        _product(batch_dir, 2, name="Loa thiếu ảnh", brand="Tribit", model="XSound Go", image_count=1),
        _product(batch_dir, 3, name="Sạc Ugreen 30W / 65W", brand="Ugreen", model="Uno Robot GaN"),
        _product(batch_dir, 4, name="Tai nghe Baseus Bowie WM02", brand="Baseus", model="Bowie WM02"),
        _product(batch_dir, 5, name="Camera Imou Ranger 2", brand="Imou", model="Ranger 2"),
        _product(batch_dir, 6, name="Bàn phím AULA F75", brand="AULA", model="F75"),
        _product(batch_dir, 7, name="Chuột DareU LM115B", brand="DareU", model="LM115B"),
    ]
    (batch_dir / "products.json").write_text(
        json.dumps({"version": "1.0", "date": "2026-09-12", "products": products}),
        encoding="utf-8",
    )

    connection = connect(tmp_path / "hermes.db")
    first = import_daily_products(
        source_root=source_root,
        connection=connection,
        daily_limit=3,
        owner_user_id="owner-1",
    )

    assert first.created_product_names == (
        "Quạt Jisulife Life 7",
        "Loa thiếu ảnh",
        "Tai nghe Baseus Bowie WM02",
    )
    assert first.skipped_invalid == 1
    assert first.skipped_existing == 0

    workflows = connection.execute(
        "SELECT aggregate_json FROM workflow ORDER BY workflow_id"
    ).fetchall()
    assert len(workflows) == 3
    aggregates = [json.loads(row["aggregate_json"]) for row in workflows]
    assert all(aggregate["source_context"]["source_date"] == "2026-09-12" for aggregate in aggregates)
    assert all(len(aggregate["source_context"]["images"]) >= 1 for aggregate in aggregates)

    second = import_daily_products(
        source_root=source_root,
        connection=connection,
        daily_limit=3,
        owner_user_id="owner-1",
    )

    assert second.created_product_names == ()
    assert second.skipped_existing == 3
    assert connection.execute("SELECT COUNT(*) FROM workflow").fetchone()[0] == 3
    assert connection.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0] == 3


def test_rejects_images_outside_the_daily_batch(tmp_path: Path) -> None:
    source_root = tmp_path / "daily_runs"
    batch_dir = source_root / "2026-09-12"
    batch_dir.mkdir(parents=True)
    outside = tmp_path / "outside.png"
    _write_image(outside)
    product = _product(
        batch_dir,
        1,
        name="Quạt Jisulife Life 7",
        brand="Jisulife",
        model="Life 7",
    )
    for image in product["images"]:
        image["absolute_path"] = str(outside)
    (batch_dir / "products.json").write_text(
        json.dumps({"version": "1.0", "date": "2026-09-12", "products": [product]}),
        encoding="utf-8",
    )

    connection = connect(tmp_path / "hermes.db")
    result = import_daily_products(source_root=source_root, connection=connection, daily_limit=3)

    assert result.created_product_names == ()
    assert result.skipped_invalid == 1
    assert connection.execute("SELECT COUNT(*) FROM workflow").fetchone()[0] == 0


def test_cli_prints_vietnamese_when_windows_stdout_starts_as_cp1252(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "daily_runs"
    batch_dir = source_root / "2026-09-12"
    batch_dir.mkdir(parents=True)
    product = _product(
        batch_dir,
        1,
        name="Quạt cầm tay Jisulife",
        brand="Jisulife",
        model="Life 7",
    )
    (batch_dir / "products.json").write_text(
        json.dumps({"version": "1.0", "date": "2026-09-12", "products": [product]}),
        encoding="utf-8",
    )
    output_bytes = io.BytesIO()
    output = io.TextIOWrapper(output_bytes, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", output)

    result = main(
        [
            "--source-root",
            str(source_root),
            "--db-path",
            str(tmp_path / "cli.db"),
            "--date",
            "2026-09-12",
            "--limit",
            "1",
        ]
    )

    output.flush()
    assert result == 0
    assert "Quạt cầm tay Jisulife" in output_bytes.getvalue().decode("utf-8")
