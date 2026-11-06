from __future__ import annotations

import json
from pathlib import Path
import openpyxl
import pytest

from product_scout.master_excel import MasterExcelExporter


@pytest.fixture
def sample_runs_dir(tmp_path: Path) -> Path:
    runs_dir = tmp_path / "daily_runs"
    runs_dir.mkdir(parents=True)

    # 1. Day 1: V1 Schema
    day1 = runs_dir / "2026-09-10"
    day1.mkdir()
    d1_payload = {
        "version": "1.0",
        "date": "2026-09-10",
        "status": "success",
        "request_prompt": "tìm 10 sản phẩm công nghệ",
        "products": [
            {
                "id": "prod_01",
                "name": "Quạt cầm tay mini Jisulife Life 7",
                "brand": "Jisulife",
                "model": "Life 7",
                "category": "Gia dụng & Tiện ích thông minh",
                "price_display": "349,000đ - 450,000đ",
                "price_min": 349000,
                "price_max": 450000,
                "currency": "VND",
                "hot_reason": "Pin khủng 5000mAh",
                "key_features": ["Pin 5000mAh", "Động cơ không chổi than"],
                "keywords": ["quạt mini", "jisulife"],
                "image_count": 2,
                "images": [
                    {"local_path": "data/daily_runs/2026-09-10/images/quat/img1.jpg", "original_url": "https://img.test/1.jpg"}
                ],
            }
        ],
    }
    (day1 / "products.json").write_text(json.dumps(d1_payload, ensure_ascii=False), encoding="utf-8")

    # 2. Day 2: V2 Schema
    day2 = runs_dir / "2026-09-19"
    day2.mkdir()
    d2_payload = {
        "version": "2.0",
        "date": "2026-09-19",
        "status": "success_with_warnings",
        "warnings": ["insufficient_fresh_candidates"],
        "request_prompt": "tìm 10 sản phẩm công nghệ",
        "products": [
            {
                "id": "prod_01",
                "canonical_key": "samsung|t7",
                "name": "Ổ cứng di động SSD Samsung T7 tốc độ...",
                "brand": "Samsung",
                "model": "T7",
                "category": "công nghệ",
                "price_display": "1,500,000đ - 2,000,000đ",
                "price_min": 1500000,
                "price_max": 2000000,
                "currency": "VND",
                "selection_status": "new",
                "trend_score": 75.0,
                "freshness_score": 100.0,
                "confidence_score": 85.0,
                "final_score": 83.25,
                "hot_reason": "Tốc độ cao 1050MB/s",
                "key_features": ["NVMe USB 3.2", "Chống sốc"],
                "keywords": ["samsung t7", "ssd"],
                "trend_evidence": [
                    {"provider": "public_commerce", "host": "shopee.vn", "url": "https://shopee.vn/t7"}
                ],
                "image_count": 1,
                "images": [
                    {"local_path": "data/daily_runs/2026-09-19/images/samsung-t7/img1.jpg", "original_url": "https://img.test/t7.jpg"}
                ],
            }
        ],
    }
    (day2 / "products.json").write_text(json.dumps(d2_payload, ensure_ascii=False), encoding="utf-8")

    # 3. Day 3: Empty products run
    day3 = runs_dir / "2026-09-20"
    day3.mkdir()
    d3_payload = {
        "version": "2.0",
        "date": "2026-09-20",
        "status": "success_with_warnings",
        "warnings": ["insufficient_fresh_candidates"],
        "request_prompt": "tìm 10 sản phẩm",
        "products": [],
    }
    (day3 / "products.json").write_text(json.dumps(d3_payload, ensure_ascii=False), encoding="utf-8")

    return runs_dir


def test_master_excel_exporter_build_records(sample_runs_dir: Path):
    exporter = MasterExcelExporter(runs_dir=sample_runs_dir)
    records, daily_summaries, unique_catalog = exporter.collect_all_data()

    assert len(records) == 2
    assert len(daily_summaries) == 3
    assert len(unique_catalog) == 2

    # Verify record fields
    r1 = records[0]
    assert r1["crawl_date"] == "2026-09-10"
    assert r1["name"] == "Quạt cầm tay mini Jisulife Life 7"
    assert r1["brand"] == "Jisulife"
    assert r1["version"] == "1.0"
    assert r1["selection_status"] == "Curated (v1.0)"

    r2 = records[1]
    assert r2["crawl_date"] == "2026-09-19"
    assert r2["canonical_key"] == "samsung|t7"
    assert r2["final_score"] == 83.25
    assert r2["selection_status"] == "Mới (Fresh)"
    assert "shopee.vn" in r2["evidence_sources"]


def test_master_excel_export_file(sample_runs_dir: Path, tmp_path: Path):
    excel_path = tmp_path / "master_products.xlsx"
    csv_path = tmp_path / "master_products.csv"

    exporter = MasterExcelExporter(runs_dir=sample_runs_dir)
    res = exporter.export(excel_path=excel_path, csv_path=csv_path)

    assert excel_path.exists()
    assert csv_path.exists()
    assert res["total_products"] == 2
    assert res["total_days"] == 3

    # Verify openpyxl workbook structure
    wb = openpyxl.load_workbook(excel_path)
    assert "Toàn Bộ Sản Phẩm" in wb.sheetnames
    assert "Tổng Hợp Ngày" in wb.sheetnames
    assert "Sản Phẩm Độc Nhất" in wb.sheetnames

    ws_all = wb["Toàn Bộ Sản Phẩm"]
    assert ws_all.max_row == 3  # 1 header + 2 products
    assert ws_all.cell(row=1, column=1).value == "STT"
    assert ws_all.cell(row=2, column=2).value == "2026-09-10"
    assert ws_all.cell(row=3, column=2).value == "2026-09-19"
