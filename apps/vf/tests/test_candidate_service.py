from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from video_factory.candidate_service import DailyCandidateService
from video_factory.daily_importer import import_daily_products
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
        "price_display": "300.000đ - 500.000đ",
        "price_min": 300000,
        "price_max": 500000,
        "hot_reason": "Hot trend",
        "key_features": "Feature 1, Feature 2",
        "keywords": [name, f"{brand} {model}"],
        "image_count": len(images),
        "images": images,
    }


def test_candidate_service_classifies_and_selects(tmp_path: Path) -> None:
    source_root = tmp_path / "daily_runs"
    batch_dir = source_root / "2026-09-12"
    batch_dir.mkdir(parents=True)

    products = [
        _product(batch_dir, 1, name="Quạt Jisulife Life 7", brand="Jisulife", model="Life 7"),
        _product(batch_dir, 2, name="Loa thiếu ảnh", brand="Tribit", model="XSound Go", image_count=1),
        _product(batch_dir, 3, name="Bàn phím AULA F75", brand="AULA", model="F75"),
    ]
    (batch_dir / "products.json").write_text(
        json.dumps({"version": "1.0", "date": "2026-09-12", "products": products}),
        encoding="utf-8",
    )

    connection = connect(tmp_path / "hermes.db")
    service = DailyCandidateService(connection, source_root=source_root)

    # Initial state: no workflow started yet
    candidates = service.get_candidates()
    assert len(candidates) == 3
    status_map = {c.name: c.status for c in candidates}
    assert status_map["Quạt Jisulife Life 7"] == "unprocessed"
    assert status_map["Loa thiếu ảnh"] == "invalid"
    assert status_map["Bàn phím AULA F75"] == "unprocessed"

    # Start specific selection: only AULA F75
    f75_candidate = next(c for c in candidates if c.model == "F75")
    batch_res = service.start_selection([f75_candidate.product_key])
    assert len(batch_res.started) == 1
    started_wf_id = batch_res.started[0].workflow_id
    assert started_wf_id.startswith("pv-daily-")

    # Verify state updated: F75 is now in_production (researching)
    updated_candidates = service.get_candidates()
    f75_updated = next(c for c in updated_candidates if c.model == "F75")
    assert f75_updated.status == "in_production"
    assert f75_updated.workflow_id == started_wf_id

    # Test start_auto picks remaining unprocessed: Jisulife
    auto_res = service.start_auto(limit=2)
    assert len(auto_res.started) == 1  # only Jisulife remained unprocessed

    # Verify all valid are now started
    final_candidates = service.get_candidates()
    final_unprocessed = [c for c in final_candidates if c.status == "unprocessed"]
    assert len(final_unprocessed) == 0
