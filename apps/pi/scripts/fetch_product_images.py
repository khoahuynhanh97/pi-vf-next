"""Fetch 2-3 sample product images from a CSV idea table.

Thin CLI over Media Intelligence: reads the Google-composed product table,
researches each product, saves images under the media store, prints a JSON
report. DDG and download logic live in media/; this script only parses rows
and renders results.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media import MediaIntelligenceService, ProductIdentityHint
from media.config import MediaConfig


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="fetch-product-images")
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--max-images", type=int, default=5, help="DDG results per product")
    parser.add_argument("--min-per-product", type=int, default=2, help="target accepted images per product")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    config = MediaConfig(max_search_results=args.max_images, target_selected_assets=args.min_per_product)
    service = MediaIntelligenceService(config)
    report = []

    with open(args.csv_path, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if args.limit is not None:
        rows = rows[: args.limit]

    for row in rows:
        name = str(row.get("Tên sản phẩm") or "").strip()
        if not name:
            continue
        keyword = str(row.get("Từ khóa ảnh") or name).strip()
        result = service.research(ProductIdentityHint(name=name, keywords=[keyword]), target_assets=args.min_per_product)
        report.append({
            "product": name,
            "keyword": keyword,
            "status": result.status.value,
            "assets": [
                {"url": asset.source.get("image_url"), "path": asset.local_path, "host": asset.source.get("host"), "match": asset.assessment.match_status.value}
                for asset in result.selected_assets
            ],
            "rejected": [rejection.reason for rejection in result.rejections],
            "coverage": result.coverage,
            "ok": len(result.selected_assets) >= args.min_per_product,
        })

    ok = sum(1 for item in report if item["ok"])
    print(json.dumps({"total": len(report), "ok": ok, "items": report}, ensure_ascii=False, indent=2))
    return 0 if ok == len(report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
