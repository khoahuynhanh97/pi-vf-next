"""Master Excel & CSV Exporter aggregating all daily product intelligence crawl runs."""

from __future__ import annotations

import csv
import json
import logging
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

logger = logging.getLogger(__name__)

# Color Palette & Styles
HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")  # Deep Navy
HEADER_FONT = Font(name="Segoe UI", size=10, bold=True, color="FFFFFF")

ALT_ROW_FILL = PatternFill(start_color="F9FAFB", end_color="F9FAFB", fill_type="solid")  # Very Light Gray
WHITE_FILL = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")

STATUS_NEW_FILL = PatternFill(start_color="E6F4EA", end_color="E6F4EA", fill_type="solid")  # Soft Green
STATUS_NEW_FONT = Font(name="Segoe UI", size=9, bold=True, color="137333")

STATUS_RE_FILL = PatternFill(start_color="FEF7E0", end_color="FEF7E0", fill_type="solid")  # Soft Amber
STATUS_RE_FONT = Font(name="Segoe UI", size=9, bold=True, color="B06000")

STATUS_CUR_FILL = PatternFill(start_color="E8EAED", end_color="E8EAED", fill_type="solid")  # Soft Gray
STATUS_CUR_FONT = Font(name="Segoe UI", size=9, bold=False, color="5F6368")

BASE_FONT = Font(name="Segoe UI", size=9)
BOLD_FONT = Font(name="Segoe UI", size=9, bold=True)
MUTED_FONT = Font(name="Segoe UI", size=8, color="70757A")

THIN_SIDE = Side(border_style="thin", color="E0E0E0")
CELL_BORDER = Border(left=THIN_SIDE, right=THIN_SIDE, top=THIN_SIDE, bottom=THIN_SIDE)

ALIGN_LEFT = Alignment(horizontal="left", vertical="center", wrap_text=False)
ALIGN_CENTER = Alignment(horizontal="center", vertical="center")
ALIGN_RIGHT = Alignment(horizontal="right", vertical="center")


class MasterExcelExporter:
    """Consolidates all historical daily runs into a multi-sheet master Excel workbook and CSV."""

    def __init__(self, runs_dir: Path | str | None = None, output_dir: Path | str | None = None):
        self.runs_dir = Path(runs_dir or Path("data/daily_runs")).resolve()
        self.output_dir = Path(output_dir or Path("data")).resolve()

    def collect_all_data(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Read and parse all daily runs, producing flat records, daily summaries, and unique catalog."""
        if not self.runs_dir.exists():
            return [], [], []

        flat_records: list[dict[str, Any]] = []
        daily_summaries: list[dict[str, Any]] = []
        unique_map: dict[str, dict[str, Any]] = {}

        # Scan day directories in chronological order
        day_dirs = sorted([d for d in self.runs_dir.iterdir() if d.is_dir()])

        for day_dir in day_dirs:
            prod_json = day_dir / "products.json"
            if not prod_json.exists():
                continue

            try:
                data = json.loads(prod_json.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Failed to parse %s: %s", prod_json, exc)
                continue

            day_str = data.get("date", day_dir.name)
            version = data.get("version", "1.0")
            status = data.get("status", "unknown")
            warnings = data.get("warnings", [])
            prompt = data.get("request_prompt", "")
            products = data.get("products", [])

            fresh_cnt = 0
            re_cnt = 0
            trend_scores = []
            final_scores = []

            for p in products:
                # Resolve selection status human readable
                raw_status = p.get("selection_status")
                if raw_status == "new":
                    status_text = "Mới (Fresh)"
                    fresh_cnt += 1
                elif raw_status == "re_emerging":
                    status_text = "Quay lại (Re-emerging)"
                    re_cnt += 1
                else:
                    status_text = f"Curated (v{version})"

                # Resolve key features
                features = p.get("key_features") or []
                features_str = "; ".join(features) if isinstance(features, list) else str(features)

                # Resolve trend evidence
                evidence = p.get("trend_evidence") or []
                if isinstance(evidence, list):
                    evidence_str = "; ".join(
                        f"[{e.get('host', e.get('provider', 'source'))}] {e.get('url', '')}"
                        for e in evidence if isinstance(e, dict)
                    )
                else:
                    evidence_str = str(evidence)

                # Resolve primary image
                images = p.get("images") or []
                primary_local = ""
                primary_url = ""
                if images and isinstance(images, list) and isinstance(images[0], dict):
                    primary_local = images[0].get("local_path", "")
                    primary_url = images[0].get("original_url", "")

                t_score = p.get("trend_score")
                f_score = p.get("final_score")
                if t_score is not None:
                    trend_scores.append(float(t_score))
                if f_score is not None:
                    final_scores.append(float(f_score))

                record = {
                    "stt": len(flat_records) + 1,
                    "crawl_date": day_str,
                    "version": version,
                    "product_id": p.get("id", ""),
                    "canonical_key": p.get("canonical_key") or p.get("name", ""),
                    "name": p.get("name", ""),
                    "brand": p.get("brand", "") or "Unknown",
                    "model": p.get("model", ""),
                    "category": p.get("category", ""),
                    "selection_status": status_text,
                    "price_display": p.get("price_display", ""),
                    "price_min": p.get("price_min"),
                    "price_max": p.get("price_max"),
                    "currency": p.get("currency", "VND"),
                    "trend_score": t_score,
                    "freshness_score": p.get("freshness_score"),
                    "confidence_score": p.get("confidence_score"),
                    "final_score": f_score,
                    "hot_reason": p.get("hot_reason", ""),
                    "key_features": features_str,
                    "evidence_sources": evidence_str,
                    "image_count": p.get("image_count", len(images)),
                    "primary_image_path": primary_local,
                    "primary_image_url": primary_url,
                }
                flat_records.append(record)

                # Update unique catalog
                ckey = record["canonical_key"]
                if ckey not in unique_map:
                    unique_map[ckey] = {
                        "canonical_key": ckey,
                        "name": record["name"],
                        "brand": record["brand"],
                        "model": record["model"],
                        "category": record["category"],
                        "days_appeared": 1,
                        "first_seen_date": day_str,
                        "last_seen_date": day_str,
                        "latest_price_display": record["price_display"],
                        "max_trend_score": t_score,
                        "first_version": version,
                    }
                else:
                    item = unique_map[ckey]
                    item["days_appeared"] += 1
                    item["last_seen_date"] = day_str
                    item["latest_price_display"] = record["price_display"] or item["latest_price_display"]
                    if t_score is not None:
                        current_max = item.get("max_trend_score")
                        item["max_trend_score"] = max(current_max or 0.0, float(t_score))

            # Daily summary
            avg_trend = (sum(trend_scores) / len(trend_scores)) if trend_scores else None
            avg_final = (sum(final_scores) / len(final_scores)) if final_scores else None

            daily_summaries.append({
                "date": day_str,
                "version": f"v{version}",
                "status": status,
                "total_products": len(products),
                "fresh_products": fresh_cnt,
                "reemerging_products": re_cnt,
                "with_images": sum(1 for p in products if p.get("image_count", 0) > 0 or p.get("images")),
                "avg_trend_score": avg_trend,
                "avg_final_score": avg_final,
                "warnings": ", ".join(warnings) if warnings else "",
                "request_prompt": prompt,
            })

        unique_catalog = list(unique_map.values())
        unique_catalog.sort(key=lambda x: (-x["days_appeared"], x["name"]))
        for idx, u in enumerate(unique_catalog, start=1):
            u["stt"] = idx

        return flat_records, daily_summaries, unique_catalog

    def export(
        self,
        excel_path: Path | str | None = None,
        csv_path: Path | str | None = None,
    ) -> dict[str, Any]:
        """Generate master Excel and CSV files with professional layout, borders, and auto-filters."""
        out_excel = Path(excel_path or self.output_dir / "master_products.xlsx").resolve()
        out_csv = Path(csv_path or self.output_dir / "master_products.csv").resolve()

        out_excel.parent.mkdir(parents=True, exist_ok=True)
        out_csv.parent.mkdir(parents=True, exist_ok=True)

        records, daily_summaries, unique_catalog = self.collect_all_data()

        # Build Excel Workbook
        wb = openpyxl.Workbook()
        # Remove default sheet
        wb.remove(wb.active)

        self._build_products_sheet(wb, records)
        self._build_summary_sheet(wb, daily_summaries)
        self._build_unique_sheet(wb, unique_catalog)

        # Atomic replacement for Excel
        tmp_excel = out_excel.with_suffix(".xlsx.tmp")
        wb.save(tmp_excel)
        tmp_excel.replace(out_excel)

        # Write CSV
        self._export_csv(out_csv, records)

        logger.info("Successfully exported Master Excel to %s and CSV to %s", out_excel, out_csv)

        return {
            "excel_path": str(out_excel),
            "csv_path": str(out_csv),
            "total_products": len(records),
            "total_days": len(daily_summaries),
            "unique_products": len(unique_catalog),
        }

    def _build_products_sheet(self, wb: openpyxl.Workbook, records: list[dict[str, Any]]) -> None:
        ws = wb.create_sheet(title="Toàn Bộ Sản Phẩm")
        ws.views.sheetView[0].showGridLines = True

        headers = [
            ("STT", ALIGN_CENTER, 6),
            ("Ngày Crawl", ALIGN_CENTER, 12),
            ("Phiên Bản", ALIGN_CENTER, 10),
            ("Mã SP", ALIGN_CENTER, 10),
            ("Mã Định Danh (Canonical Key)", ALIGN_LEFT, 24),
            ("Tên Sản Phẩm", ALIGN_LEFT, 36),
            ("Thương Hiệu", ALIGN_LEFT, 16),
            ("Model", ALIGN_LEFT, 14),
            ("Ngành Hàng", ALIGN_LEFT, 22),
            ("Trạng Thái Lựa Chọn", ALIGN_CENTER, 20),
            ("Khoảng Giá Hiển Thị", ALIGN_RIGHT, 22),
            ("Giá Min (VND)", ALIGN_RIGHT, 15),
            ("Giá Max (VND)", ALIGN_RIGHT, 15),
            ("Trend Score", ALIGN_RIGHT, 12),
            ("Freshness", ALIGN_RIGHT, 12),
            ("Confidence", ALIGN_RIGHT, 12),
            ("Final Score", ALIGN_RIGHT, 12),
            ("Lý Do Hot / Chọn", ALIGN_LEFT, 30),
            ("Tính Năng Nổi Bật", ALIGN_LEFT, 35),
            ("Bằng Chứng Nguồn (Evidence)", ALIGN_LEFT, 35),
            ("Số Ảnh", ALIGN_CENTER, 8),
            ("Đường Dẫn Ảnh Cục Bộ", ALIGN_LEFT, 35),
            ("Link Ảnh Gốc", ALIGN_LEFT, 35),
        ]

        # Header row
        for col_idx, (h_name, align, _) in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col_idx, value=h_name)
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
            cell.alignment = align
            cell.border = CELL_BORDER
        ws.row_dimensions[1].height = 26

        # Data rows
        for row_idx, r in enumerate(records, start=2):
            fill = ALT_ROW_FILL if row_idx % 2 == 0 else WHITE_FILL
            ws.row_dimensions[row_idx].height = 20

            values = [
                r["stt"],
                r["crawl_date"],
                f"v{r['version']}",
                r["product_id"],
                r["canonical_key"],
                r["name"],
                r["brand"],
                r["model"],
                r["category"],
                r["selection_status"],
                r["price_display"],
                r["price_min"],
                r["price_max"],
                r["trend_score"],
                r["freshness_score"],
                r["confidence_score"],
                r["final_score"],
                r["hot_reason"],
                r["key_features"],
                r["evidence_sources"],
                r["image_count"],
                r["primary_image_path"],
                r["primary_image_url"],
            ]

            for col_idx, (val, (_, align, _)) in enumerate(zip(values, headers), start=1):
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                cell.fill = fill
                cell.font = BASE_FONT
                cell.alignment = align
                cell.border = CELL_BORDER

                # Specific cell formatting
                if col_idx in (12, 13) and isinstance(val, (int, float)):
                    cell.number_format = '#,##0 "₫"'
                elif col_idx in (14, 15, 16, 17) and isinstance(val, (int, float)):
                    cell.number_format = '0.0'

                # Status highlight
                if col_idx == 10:
                    if "Mới" in str(val):
                        cell.fill = STATUS_NEW_FILL
                        cell.font = STATUS_NEW_FONT
                    elif "Quay lại" in str(val):
                        cell.fill = STATUS_RE_FILL
                        cell.font = STATUS_RE_FONT
                    else:
                        cell.fill = STATUS_CUR_FILL
                        cell.font = STATUS_CUR_FONT

        # Set column widths & Freeze Panes & Auto filter
        ws.freeze_panes = "A2"
        if records:
            ws.auto_filter.ref = ws.dimensions
        for col_idx, (_, _, width) in enumerate(headers, start=1):
            ws.column_dimensions[get_column_letter(col_idx)].width = width

    def _build_summary_sheet(self, wb: openpyxl.Workbook, summaries: list[dict[str, Any]]) -> None:
        ws = wb.create_sheet(title="Tổng Hợp Ngày")
        ws.views.sheetView[0].showGridLines = True

        headers = [
            ("Ngày", ALIGN_CENTER, 14),
            ("Phiên Bản", ALIGN_CENTER, 10),
            ("Trạng Thái Phiên", ALIGN_CENTER, 22),
            ("Tổng SP Crawl", ALIGN_RIGHT, 14),
            ("SP Mới (Fresh)", ALIGN_RIGHT, 14),
            ("SP Quay Lại", ALIGN_RIGHT, 14),
            ("Có Hình Ảnh", ALIGN_RIGHT, 14),
            ("Trend Score TB", ALIGN_RIGHT, 14),
            ("Final Score TB", ALIGN_RIGHT, 14),
            ("Cảnh Báo Vận Hành", ALIGN_LEFT, 30),
            ("Yêu Cầu (Prompt)", ALIGN_LEFT, 50),
        ]

        # Header
        for col_idx, (h_name, align, _) in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col_idx, value=h_name)
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
            cell.alignment = align
            cell.border = CELL_BORDER
        ws.row_dimensions[1].height = 26

        # Data rows
        for row_idx, s in enumerate(summaries, start=2):
            fill = ALT_ROW_FILL if row_idx % 2 == 0 else WHITE_FILL
            ws.row_dimensions[row_idx].height = 20

            values = [
                s["date"],
                s["version"],
                s["status"],
                s["total_products"],
                s["fresh_products"],
                s["reemerging_products"],
                s["with_images"],
                s["avg_trend_score"],
                s["avg_final_score"],
                s["warnings"],
                s["request_prompt"],
            ]

            for col_idx, (val, (_, align, _)) in enumerate(zip(values, headers), start=1):
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                cell.fill = fill
                cell.font = BASE_FONT
                cell.alignment = align
                cell.border = CELL_BORDER

                if col_idx in (8, 9) and isinstance(val, (int, float)):
                    cell.number_format = '0.0'

        ws.freeze_panes = "A2"
        if summaries:
            ws.auto_filter.ref = ws.dimensions
        for col_idx, (_, _, width) in enumerate(headers, start=1):
            ws.column_dimensions[get_column_letter(col_idx)].width = width

    def _build_unique_sheet(self, wb: openpyxl.Workbook, catalog: list[dict[str, Any]]) -> None:
        ws = wb.create_sheet(title="Sản Phẩm Độc Nhất")
        ws.views.sheetView[0].showGridLines = True

        headers = [
            ("STT", ALIGN_CENTER, 6),
            ("Tên Sản Phẩm", ALIGN_LEFT, 36),
            ("Thương Hiệu", ALIGN_LEFT, 16),
            ("Model", ALIGN_LEFT, 14),
            ("Ngành Hàng", ALIGN_LEFT, 22),
            ("Số Ngày Xuất Hiện", ALIGN_RIGHT, 18),
            ("Lần Đầu Xuất Hiện", ALIGN_CENTER, 16),
            ("Lần Cuối Xuất Hiện", ALIGN_CENTER, 16),
            ("Giá Tham Khảo Gần Nhất", ALIGN_RIGHT, 22),
            ("Trend Score Cao Nhất", ALIGN_RIGHT, 20),
            ("Mã Canonical Key", ALIGN_LEFT, 26),
        ]

        # Header
        for col_idx, (h_name, align, _) in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col_idx, value=h_name)
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
            cell.alignment = align
            cell.border = CELL_BORDER
        ws.row_dimensions[1].height = 26

        # Data rows
        for row_idx, u in enumerate(catalog, start=2):
            fill = ALT_ROW_FILL if row_idx % 2 == 0 else WHITE_FILL
            ws.row_dimensions[row_idx].height = 20

            values = [
                u["stt"],
                u["name"],
                u["brand"],
                u["model"],
                u["category"],
                u["days_appeared"],
                u["first_seen_date"],
                u["last_seen_date"],
                u["latest_price_display"],
                u["max_trend_score"],
                u["canonical_key"],
            ]

            for col_idx, (val, (_, align, _)) in enumerate(zip(values, headers), start=1):
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                cell.fill = fill
                cell.font = BASE_FONT
                cell.alignment = align
                cell.border = CELL_BORDER

                if col_idx == 6:
                    cell.font = BOLD_FONT
                elif col_idx == 10 and isinstance(val, (int, float)):
                    cell.number_format = '0.0'

        ws.freeze_panes = "A2"
        if catalog:
            ws.auto_filter.ref = ws.dimensions
        for col_idx, (_, _, width) in enumerate(headers, start=1):
            ws.column_dimensions[get_column_letter(col_idx)].width = width

    def _export_csv(self, csv_path: Path, records: list[dict[str, Any]]) -> None:
        """Export flat records to CSV encoded with UTF-8 with BOM for Excel compatibility."""
        if not records:
            csv_path.write_text("", encoding="utf-8-sig")
            return

        fieldnames = list(records[0].keys())
        tmp_csv = csv_path.with_suffix(".csv.tmp")
        with open(tmp_csv, mode="w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in records:
                writer.writerow(r)

        tmp_csv.replace(csv_path)
