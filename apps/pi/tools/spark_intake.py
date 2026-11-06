"""Spark CSV intake and resolution for Product Intelligence."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sys
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PIL import Image

from tools.selection_handoff import SELECTED_FIELDS
from tools.spaiauto.intake import finish_request, intake_summary, resolve_request, search_attempt, start_intake
from tools.spaiauto.intake_parser import parse_input
from tools.spaiauto.links import direct_product_url, link_kind, normalize_market_url
from tools.spaiauto.storage import SPAIAUTO_URL, atomic_json, digest, item_key, now, read_json
from tools.spaiauto.workflow import attach, begin_attempt, publish, review_item


def _listing_info(url: str) -> tuple[str, str, str] | None:
    try:
        norm = normalize_market_url(url)
    except ValueError:
        return None
    p = urlsplit(norm)
    host = p.hostname or ""
    path = p.path.rstrip("/")
    m = re.search(r"/product/(\d+)/(\d+)$", path) or re.search(r"-i\.(\d+)\.(\d+)$", path)
    if host.endswith("shopee.vn") and m:
        shop_id = m.group(1) or m.group(3)
        item_id = m.group(2) or m.group(4)
        return f"shopee:{shop_id}:{item_id}", "shopee", norm
    if host.endswith("tiktok.com") and link_kind(norm) == "direct":
        m = re.search(r"/(?:pdp|product)/(?:[^/]+/)*(\d+)$", path)
        if m:
            return f"tiktok:{m.group(1)}", "tiktok", norm
    return None


def _clean_text(val: Any) -> str:
    return str(val or "").strip()


def _slugify(val: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(val or "")).strip()
    ascii_str = raw.encode("ascii", "ignore").decode("ascii").lower()
    return "-".join(re.findall(r"[a-z0-9]+", ascii_str))


def _extract_variant(name: str) -> str:
    m = re.search(r"(\d+[\.,]?\d*\s*(?:mah|w|gb|tb|v|hz|dpi|g|kg|ml|l)|màu\s+\w+|đen|trắng|xám|black|white|grey|hộp\s+\d+\w*)", name, re.IGNORECASE)
    if m:
        return m.group(1).strip().title()
    return "Tiêu chuẩn"


def _extract_brand(name: str) -> str:
    words = name.split()
    if words:
        candidate = words[0]
        if len(candidate) > 1 and candidate.isalnum():
            return candidate.title()
    return "Generic"


def _make_dummy_image(color: str = "blue", text: str = "PI Asset") -> bytes:
    img = Image.new("RGB", (720, 1280), color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def is_verified_real_batch(batch_dir: Path | str) -> tuple[bool, str]:
    """Verify that an Exchange batch contains authentic operator media, not fixtures."""
    b_path = Path(batch_dir).resolve()
    if not b_path.is_dir():
        return False, f"Thư mục batch không tồn tại: {b_path}"
    if not (b_path / "READY").is_file():
        return False, f"Cờ READY chưa có trong {b_path}"
    manifest_file = b_path / "manifest.json"
    if not manifest_file.is_file():
        return False, f"manifest.json không tồn tại trong {b_path}"

    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"Không đọc được manifest.json: {exc}"

    # Check fixture flags and batch naming prefix
    if manifest.get("fixture") is True or manifest.get("is_fixture") is True:
        return False, "Batch được đánh dấu rõ là fixture/dữ liệu giả lập."
    if b_path.name.startswith("run_fix_") or str(manifest.get("source_selection_batch_id", "")).startswith("fix_") or str(manifest.get("batch_id", "")).startswith("fix_"):
        return False, "Mã batch thuộc luồng fixture/offline test, không phải tư liệu thật."

    products = manifest.get("products", [])
    if not products:
        return False, "Batch manifest không chứa sản phẩm nào."

    for prod in products:
        images = prod.get("images", [])
        if not images:
            return False, f"Sản phẩm '{prod.get('name')}' không có ảnh tham chiếu."

        prov_rel = prod.get("provenance_path")
        if not prov_rel:
            return False, f"Sản phẩm '{prod.get('name')}' thiếu provenance_path (nguồn gốc chưa xác minh)."
        prov_file = b_path / prov_rel
        if not prov_file.is_file():
            return False, f"File provenance của sản phẩm '{prod.get('name')}' không tồn tại: {prov_file}"
        try:
            prov = json.loads(prov_file.read_text(encoding="utf-8"))
            rev = prov.get("review") or {}
            rev_by = str(rev.get("reviewed_by") or "").strip()
            if rev_by in ("fixture_operator", "synthetic_operator", ""):
                return False, f"Sản phẩm '{prod.get('name')}' chỉ được duyệt bởi fixture operator hoặc chưa có reviewer thật."
            if not rev.get("identity_verified"):
                return False, f"Sản phẩm '{prod.get('name')}' chưa qua xác minh identity."
        except Exception as exc:
            return False, f"Không đọc được provenance của sản phẩm '{prod.get('name')}': {exc}"

        for img_info in images:
            orig_url = str(img_info.get("original_url") or "")
            if not orig_url or "example.com" in orig_url or orig_url.startswith("file://") or "dummy" in orig_url.lower():
                return False, f"Ảnh {img_info.get('asset_id')} có URL giả lập hoặc không rõ nguồn: {orig_url}"
            rel_path = img_info.get("relative_path")
            if not rel_path:
                return False, f"Ảnh {img_info.get('asset_id')} thiếu relative_path."
            img_file = b_path / rel_path
            if not img_file.is_file():
                return False, f"File ảnh không tồn tại trên đĩa: {img_file}"

            # Media check: detect single solid dummy image
            try:
                with Image.open(img_file) as im:
                    import numpy as np
                    arr = np.array(im.convert("RGB"))
                    if float(arr.std()) < 1.0:
                        return False, f"Ảnh {img_file.name} là ảnh màu đơn sắc giả lập, không phải ảnh chụp sản phẩm thật."
            except Exception as exc:
                return False, f"Lỗi đọc ảnh {img_file}: {exc}"

    return True, "Batch hợp lệ: tư liệu thật đã được PI xác minh và duyệt."


def run_spark_intake(
    spark_file: Path,
    run_dir: Path,
    inbox_root: Path,
    limit: int = 15,
    offline_adapter: bool = False,
    batch_date: str | None = None,
) -> dict[str, Any]:
    spark_file = spark_file.resolve()
    if not spark_file.is_file():
        raise FileNotFoundError(f"Spark file not found: {spark_file}")

    run_dir = run_dir.resolve()
    inbox_root = inbox_root.resolve()
    today = batch_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    batch_date_compact = today.replace("-", "")

    # Step 1: Parse Spark CSV using PI's existing intake parser
    parsed_rows = parse_input(spark_file)
    source_sha = digest(spark_file)

    # Step 2: Separate direct PDP links vs search/shop links
    direct_candidates: list[dict[str, Any]] = []
    unresolved_rows: list[dict[str, Any]] = []
    seen_candidates: set[str] = set()

    for row in parsed_rows:
        row_id = row["row_id"]
        source_row = row["source_row"]
        links = row.get("links", [])
        
        direct_link = next((link["url"] for link in links if link.get("kind") == "direct"), None)
        if direct_link:
            info = _listing_info(direct_link)
            if info:
                cid, platform, norm_url = info
                if cid not in seen_candidates:
                    seen_candidates.add(cid)
                    brand = _extract_brand(row["name"])
                    variant = _extract_variant(row["name"])
                    price_hint = str(row.get("price_min") or row.get("price_max") or "")
                    direct_candidates.append({
                        "row_id": row_id,
                        "source_row": source_row,
                        "name": row["name"],
                        "url": norm_url,
                        "candidate_id": cid,
                        "platform": platform,
                        "category": row.get("category") or "general",
                        "brand": brand,
                        "variant": variant,
                        "price_hint": price_hint,
                        "price_min": row.get("price_min"),
                        "price_max": row.get("price_max"),
                        "currency": row.get("currency") or "VND",
                        "raw_cells": row.get("raw_cells", []),
                    })
                continue

        # If not direct, record unresolvable search/shop reason
        first_kind = links[0]["kind"] if links else "unsupported"
        reason = f"Unresolved {first_kind} link; requires browser operator to select exact product listing"
        unresolved_rows.append({
            "row_id": row_id,
            "source_row": source_row,
            "name": row["name"],
            "reason": reason,
            "kind": first_kind,
        })

    # Limit to maximum eligible products (up to limit, max 15)
    selected_candidates = direct_candidates[:limit]
    if not selected_candidates:
        raise ValueError("No direct eligible product listings found in Spark sheet.")

    # Step 3: Create selection handoff directory and selected_products.csv
    handoff_dir = run_dir / "handoff"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    sources_dir = handoff_dir / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)

    # Copy source file
    source_copy_name = f"sources/{spark_file.name}"
    staged_source = handoff_dir / source_copy_name
    shutil.copy2(spark_file, staged_source)

    csv_rows: list[dict[str, Any]] = []
    for rank, c in enumerate(selected_candidates, 1):
        refs_json = json.dumps([{
            "sheet_id": "spark",
            "file": source_copy_name,
            "tab": "CSV",
            "row": int(c["source_row"]),
            "snapshot_sha256": source_sha,
        }], ensure_ascii=False, separators=(",", ":"))
        observed_ts = f"{today}T00:00:00Z"
        csv_rows.append({
            "product_name": c["name"],
            "source_url": c["url"],
            "category": c["category"],
            "candidate_id": c["candidate_id"],
            "platform": c["platform"],
            "brand_hint": c["brand"],
            "requested_variant": c["variant"],
            "price_hint_vnd": c["price_hint"],
            "selection_rank": rank,
            "selection_reason": "Spark product selected for unified automation",
            "video_concept": "POV unboxing & demonstration",
            "script_draft": f"Mở hộp và trải nghiệm thực tế {c['name']}.",
            "claims_to_verify": "",
            "source_refs_json": refs_json,
            "observed_at": observed_ts,
        })

    csv_path = handoff_dir / "selected_products.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SELECTED_FIELDS, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(csv_rows)

    csv_sha = digest(csv_path)
    prefix = "fix" if offline_adapter else "sel"
    batch_id = f"{prefix}_{batch_date_compact}_{csv_sha[:12]}"
    expected_exchange_batch = inbox_root / today / f"run_{batch_id}"

    # Record unresolved search/shop rows for traceability
    unresolved_file = run_dir / "unresolved_links.json"
    atomic_json(unresolved_file, {
        "batch_date": today,
        "source_sha256": source_sha,
        "total_unresolved": len(unresolved_rows),
        "unresolved_rows": unresolved_rows,
    })

    # Idempotency: if already published with READY
    if (expected_exchange_batch / "READY").is_file() and (expected_exchange_batch / "manifest.json").is_file():
        if not offline_adapter:
            is_real, reason = is_verified_real_batch(expected_exchange_batch)
            if not is_real:
                # Existing batch is not verified real
                expected_exchange_batch = None
        if expected_exchange_batch:
            summary_file = run_dir / "intake" / "summary.json"
            summary_counts = read_json(summary_file).get("counts", {}) if summary_file.is_file() else {"completed": len(selected_candidates)}
            if (run_dir / "intake" / "resources.json").is_file() and not (run_dir / "resources.json").is_file():
                shutil.copy2(run_dir / "intake" / "resources.json", run_dir / "resources.json")
            return {
                "status": "success",
                "is_fixture": offline_adapter,
                "intake_run_dir": str(run_dir / "intake"),
                "exchange_batch_dir": str(expected_exchange_batch),
                "total_spark_rows": len(parsed_rows),
                "direct_candidates": len(direct_candidates),
                "unresolved_search_shop": len(unresolved_rows),
                "published_products": len(selected_candidates),
                "summary": summary_counts,
                "resumed": True,
            }

    handoff_data = {
        "schema_version": "1.0.0",
        "kind": "pi_selection",
        "batch_id": batch_id,
        "batch_date": batch_date_compact,
        "created_at": f"{today}T00:00:00Z",
        "selection_limit": limit,
        "selected_count": len(csv_rows),
        "csv_file": "selected_products.csv",
        "csv_sha256": csv_sha,
        "sources": [{
            "sheet_id": "spark",
            "file": source_copy_name,
            "sha256": source_sha,
        }],
    }
    handoff_json_path = handoff_dir / "handoff.json"
    atomic_json(handoff_json_path, handoff_data)
    (handoff_dir / "READY").write_text("READY\n", encoding="utf-8")

    # Step 4: Run PI Intake bound to this handoff
    intake_dir = run_dir / "intake"
    fixture_dir = run_dir / "fixtures"
    if (intake_dir / "intake.json").is_file():
        try:
            old_state = read_json(intake_dir / "intake.json")
            if (
                old_state.get("input_sha256") != csv_sha
                or old_state.get("batch_id") != batch_id
                or old_state.get("selection_handoff_sha256") != digest(handoff_json_path)
            ):
                shutil.rmtree(intake_dir, ignore_errors=True)
                shutil.rmtree(fixture_dir, ignore_errors=True)
        except Exception:
            pass
    intake_state = start_intake(csv_path, intake_dir, handoff=handoff_json_path)
    if intake_state.get("date") != today:
        intake_state["date"] = today
        atomic_json(intake_dir / "intake.json", intake_state)

    # Step 5: Acquire and visually review each selected item
    if not offline_adapter:
        # Live intake requires authentic captures and reviews from browser operator
        pending_items = []
        for idx, c in enumerate(selected_candidates, 1):
            row_id = f"row_{idx:04d}"
            child_dir = intake_dir / "rows" / row_id / "acquisition"
            k = item_key(c["url"])
            item_state_file = child_dir / "state.json"
            item_done = False
            if item_state_file.is_file():
                try:
                    c_state = read_json(item_state_file)
                    item_info = c_state.get("items", {}).get(k, {})
                    if item_info.get("status") == "reviewed":
                        item_done = True
                except Exception:
                    pass
            if not item_done:
                pending_items.append({
                    "row_id": row_id,
                    "name": c["name"],
                    "url": c["url"],
                    "candidate_id": c["candidate_id"],
                })

        if pending_items:
            # Do not generate fake dummy images or synthetic captures.
            # Return waiting status with explicit next steps for browser operator.
            return {
                "status": "waiting",
                "is_fixture": False,
                "reason": f"Tư liệu thật chưa sẵn sàng cho {len(pending_items)} sản phẩm. Đang chờ browser operator quét listing, tải ZIP/capture và duyệt ảnh qua Spaiauto.",
                "next_step": "Chạy browser operator với Spaiauto (tools.spaiauto) cho các sản phẩm đã chọn, sau đó duyệt ảnh (review) để tạo Exchange READY batch.",
                "handoff_dir": str(handoff_dir),
                "intake_run_dir": str(intake_dir),
                "total_spark_rows": len(parsed_rows),
                "direct_candidates": len(direct_candidates),
                "unresolved_search_shop": len(unresolved_rows),
                "pending_items": [p["name"] for p in pending_items],
            }
    else:
        # In offline adapter mode, generate synthetic captures, images, and review records.
        fixture_dir.mkdir(parents=True, exist_ok=True)

        for idx, c in enumerate(selected_candidates, 1):
            row_id = f"row_{idx:04d}"
            url = c["url"]
            cid = c["candidate_id"]

            row_state = next((r for r in intake_state.get("rows", []) if r.get("row_id") == row_id), None)
            if row_state and row_state.get("status") == "completed":
                continue

            search_attempt(intake_dir, row_id)

            # Selection evidence file
            sel_evidence = {
                "url": url,
                "observed_title": c["name"],
                "selected_by": "fixture_operator",
                "reason": "Direct product match from Spark sheet",
                "criteria_checks": {
                    "requested_features_match": True,
                    "variant_checked": True,
                },
                "observed_price": float(c["price_min"]) if c.get("price_min") is not None else 100000.0,
                "currency": c.get("currency") or "VND",
                "brand": c["brand"],
                "model": c["name"][:30],
                "variant": c["variant"],
                "candidates": [{
                    "url": url,
                    "title": c["name"],
                    "reason": "Selected match",
                    "decision": "selected",
                }],
            }
            sel_file = fixture_dir / f"selection_{row_id}.json"
            atomic_json(sel_file, sel_evidence)

            resolve_request(intake_dir, row_id, sel_file)

            child_dir = intake_dir / "rows" / row_id / "acquisition"
            k = item_key(url)

            begin_attempt(child_dir, k, "scan")
            begin_attempt(child_dir, k, "download")

            # Generate images and ZIP archive
            img_bytes_1 = _make_dummy_image(color="darkblue", text=f"{c['name']} 1")
            img_bytes_2 = _make_dummy_image(color="darkgreen", text=f"{c['name']} 2")

            capture_json = fixture_dir / f"capture_{row_id}.json"
            atomic_json(capture_json, {
                "schema_version": 1,
                "acquisition_tool": "spaiauto",
                "extractor_url": SPAIAUTO_URL,
                "source_url": url,
                "captured_at": now(),
                "title": c["name"],
                "description": f"Chất lượng cao, chính hãng {c['brand']}. Quy cách {c['variant']}.",
                "price_display": f"{c['price_hint']} VND",
                "variations_text": c["variant"],
                "fixture": True,
                "asset_sources": [
                    {"archive_member": "item_1.jpg", "original_url": "https://example.com/item_1.jpg"},
                    {"archive_member": "item_2.jpg", "original_url": "https://example.com/item_2.jpg"},
                ],
            })

            screenshot = fixture_dir / f"page_{row_id}.png"
            screenshot.write_bytes(img_bytes_1)

            archive = fixture_dir / f"download_{row_id}.zip"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("item_1.jpg", img_bytes_1)
                z.writestr("item_2.jpg", img_bytes_2)

            destination = attach(child_dir, k, capture_json, archive, screenshot)
            imp_rec = read_json(destination / "import.json")

            # Review record
            asset_ids = [a["asset_id"] for a in imp_rec["assets"]]
            review_data = {
                "capture_digest": digest(destination / "raw" / "capture.json"),
                "reviewed_by": "fixture_operator",
                "note": "Visual review passed. Identity and packaging match. (Fixture mode)",
                "identity_verified": True,
                "identity": {
                    "brand": c["brand"],
                    "model": c["name"][:30],
                    "variant": c["variant"],
                    "category": c["category"],
                },
                "selected_asset_ids": asset_ids,
            }
            review_file = fixture_dir / f"review_{row_id}.json"
            atomic_json(review_file, review_data)

            review_item(child_dir, k, review_file)
            finish_request(intake_dir, row_id, "completed", "Verified import and visual review complete")

    # Step 6: Generate intake summary and publish to Exchange
    summary = intake_summary(intake_dir)
    exchange_batch_dir = publish(intake_dir, inbox_root, eligible_only=True)

    # Tag manifest with fixture vs verified real flag
    manifest_path = exchange_batch_dir / "manifest.json"
    if manifest_path.is_file():
        m_data = read_json(manifest_path)
        m_data["fixture"] = offline_adapter
        m_data["verified_real"] = not offline_adapter
        atomic_json(manifest_path, m_data)

    if not offline_adapter:
        is_real, reason = is_verified_real_batch(exchange_batch_dir)
        if not is_real:
            return {
                "status": "waiting",
                "is_fixture": False,
                "reason": f"Batch vừa publish chưa đủ điều kiện tư liệu thật: {reason}",
                "next_step": "Kiểm tra lại hình ảnh và review trong Spaiauto để đảm bảo tư liệu thật hợp lệ.",
                "exchange_batch_dir": str(exchange_batch_dir),
            }

    # Ensure canonical runtime/pi/runs/<run>/resources.json is present
    if (intake_dir / "resources.json").is_file():
        shutil.copy2(intake_dir / "resources.json", run_dir / "resources.json")

    return {
        "status": "success",
        "is_fixture": offline_adapter,
        "intake_run_dir": str(intake_dir),
        "exchange_batch_dir": str(exchange_batch_dir),
        "total_spark_rows": len(parsed_rows),
        "direct_candidates": len(direct_candidates),
        "unresolved_search_shop": len(unresolved_rows),
        "published_products": len(selected_candidates),
        "summary": summary.get("counts", {}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PI Spark CSV Intake and Exchange Publisher")
    parser.add_argument("--spark-file", type=Path, required=True,
                        help="Path to Spark sheet CSV")
    parser.add_argument("--run-dir", type=Path, required=True, help="Directory for PI intake runs")
    parser.add_argument("--inbox-root", type=Path, required=True, help="Exchange inbox destination")
    parser.add_argument("--limit", type=int, default=15, help="Maximum products to intake and publish (max 15)")
    parser.add_argument("--date", help="Production date YYYY-MM-DD")
    parser.add_argument("--offline", action="store_true", default=False, help="Use offline fixture adapter")
    parser.add_argument("--live-intake", action="store_true", default=False, help="Use live crawl rather than offline fixture adapter")

    args = parser.parse_args(argv)
    is_offline = args.offline and not args.live_intake
    try:
        res = run_spark_intake(
            spark_file=args.spark_file,
            run_dir=args.run_dir,
            inbox_root=args.inbox_root,
            limit=args.limit,
            offline_adapter=is_offline,
            batch_date=args.date,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
