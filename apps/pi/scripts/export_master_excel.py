"""Command-line utility to generate or refresh the Master Excel and CSV product catalog."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Fix console encoding
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from product_scout.master_excel import MasterExcelExporter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("export_master_excel")


def main() -> int:
    parser = argparse.ArgumentParser(description="Export consolidated Master Excel and CSV product catalogue.")
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "daily_runs",
        help="Path to directory containing daily runs (default: data/daily_runs)",
    )
    parser.add_argument(
        "--out-excel",
        type=Path,
        default=PROJECT_ROOT / "data" / "master_products.xlsx",
        help="Path for output Excel workbook (default: data/master_products.xlsx)",
    )
    parser.add_argument(
        "--out-csv",
        type=Path,
        default=PROJECT_ROOT / "data" / "master_products.csv",
        help="Path for output CSV file (default: data/master_products.csv)",
    )

    args = parser.parse_args()

    print("==========================================================")
    print("   📊 XUẤT MASTER EXCEL & CSV TỔNG HỢP DAILY PRODUCT SCOUT")
    print("==========================================================")
    print(f"Thư mục nguồn daily runs: {args.runs_dir}")
    print(f"File Excel đích:          {args.out_excel}")
    print(f"File CSV đích:            {args.out_csv}")
    print("Đang tổng hợp dữ liệu...")

    exporter = MasterExcelExporter(runs_dir=args.runs_dir, output_dir=args.out_excel.parent)
    result = exporter.export(excel_path=args.out_excel, csv_path=args.out_csv)

    print("\n✅ XUẤT DỮ LIỆU THÀNH CÔNG!")
    print(f"- Tổng số phiên crawl đã quét: {result['total_days']} ngày")
    print(f"- Tổng số lượt ghi nhận sản phẩm: {result['total_products']} lượt")
    print(f"- Tổng số sản phẩm độc nhất (Unique Catalog): {result['unique_products']} sản phẩm")
    print(f"- File Excel Master: {result['excel_path']}")
    print(f"- File CSV Master:   {result['csv_path']}")
    print("==========================================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
