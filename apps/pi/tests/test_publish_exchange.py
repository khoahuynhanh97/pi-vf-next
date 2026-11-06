from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from scripts.publish_exchange import publish_daily_run


def test_publish_exchange_keeps_trend_link_but_drops_template_feature(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    image_path = run_dir / "images" / "one.png"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (20, 20)).save(image_path)
    (run_dir / "products.json").write_text(
        json.dumps(
            {
                "date": "2026-10-15",
                "products": [
                    {
                        "id": "p1",
                        "name": "Ugreen 10335",
                        "brand": "Ugreen",
                        "model": "10335",
                        "key_features": "Sản phẩm Ugreen 10335 công nghệ đang xu hướng",
                        "trend_evidence": [
                            {"url": "https://news.example/products/ugreen-10335", "title": "Trending"}
                        ],
                        "images": [{"asset_id": "one", "absolute_path": str(image_path)}],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    batch = publish_daily_run(run_dir, tmp_path / "inbox", batch_id="run_test")
    product = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))["products"][0]

    assert product["key_features"] == ""
    assert product["source_url"] == "https://news.example/products/ugreen-10335"
    assert product["trend_evidence"] == [
        {"url": "https://news.example/products/ugreen-10335", "title": "Trending"}
    ]
