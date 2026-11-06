"""Process Hot Trend TikTok & Shopee docx into qualified video materials.

Reads all products from the Word document, uses Product-Intelligence's
media pipeline to acquire, validate, and deduplicate product images,
filters products through the Gatekeeper admission rules, and packages
qualified products as immutable exchange materials.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sys
import time
import unicodedata
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

# Ensure Product-Intelligence root is in sys.path
PI_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PI_ROOT))

from media import MediaConfig
from media.discovery import DuckDuckGoImageDiscoveryProvider
from media.models import ImageSearchQuery
from media.dedup import phash_distance
from media.validation.technical import _hash_image

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("process_hot_trend")

# Comprehensive brand list across 330 products and consumer electronics
COMPREHENSIVE_BRANDS = [
    # Top tech & audio
    "Xiaomi", "Redmi", "Baseus", "Ugreen", "QCY", "Jisulife", "DUKA", "ATuMan", "INPHIC",
    "CUKTECH", "Tuya", "Awei", "Remax", "Hoco", "Joyroom", "Essager", "Toocki", "Robot",
    "Deerma", "Bear", "Lock&Lock", "Philips", "SoundPEATS", "Sony", "Anker", "Tribit",
    "Mcdodo", "Kuulaa", "Tiankeplus", "Gorilla", "KK", "MC256", "B-Gem", "SY-T25",
    "HY300", "Bcase", "ShowSee", "Enchen", "Mijia", "Haylou", "Moft", "Sanag", "Lenovo",
    "Huawei", "Honor", "Oppo", "Realme", "Samsung", "Apple", "Logitech", "Razer", "Rapoo",
    "Dareu", "Keychron", "Akko", "UGREEN", "BASEUS", "ANKER", "XIAOMI", "Sandisk", "Kingston",
    "JBL", "Marshall", "Bose", "Sennheiser", "Edifier", "Belkin", "ZMI", "Vention", "ANENG",
    "AOHi", "APECH", "APEXEL", "AULA", "Havit", "Monster", "Tronsmart", "Orico", "Hagibis",
    "Wanbo", "SmartDevil", "Nillkin", "Ringke", "Spigen", "ESR", "Torras", "Pisen", "Yoobao",
    "Romoss", "USAMS", "Choetech", "Aukey", "Darmoshark", "Attack Shark", "Ajazz", "Aula",
    "RK", "Royal Kludge", "Hoto", "Wiha", "Bosch", "Nitecore", "Olight", "Fenix", "Sofirn",
    "Wurkkos", "Ledlenser", "Wuben", "RovyVon", "Smiling Shark", "Supfire", "Warsun",
    "K9", "K8", "M9", "T500", "V8", "S4", "X9", "T13", "Uno", "HY300", "G10S", "G10",
    "C50", "Q2", "Y2", "K10", "P20", "P30", "F5", "F6", "T2", "T3", "M10", "M11",
    "TPS", "ZK", "DSP", "Kemei", "Enchen", "Chigo", "Simplus", "Gaabor", "Tyeso",
    "ThermoPro", "Meater", "Inateck", "UGREEN", "BASEUS", "Deli", "M&G"
]

_PRICE_LIKE_MODEL = re.compile(r"^\d+([kK]|tr|tri[eệ]u|[.,]\d{3})", re.IGNORECASE)
_AMBIGUOUS_VARIANT = re.compile(r"\b\d+\s*[a-zA-Z]*\s*/\s*\d+", re.IGNORECASE)

COMMON_VIETNAMESE_PREFIXES = {
    "tai", "cáp", "bàn", "gậy", "giá", "máy", "kính", "quạt", "thước", "chuột",
    "vợt", "đèn", "bao", "hộp", "bình", "củ", "nồi", "bộ", "túi", "khay", "móc",
    "miếng", "dây", "đồng", "gối", "kẹp", "khăn", "chổi", "áo", "ví", "thùng",
    "thảm", "dụng", "thiết", "ổ", "bút", "cân", "loa", "sạc", "pin", "mũ", "ghế",
    "thanh", "vỏ", "chân", "tấm", "đầu", "khóa", "chuông", "ổ cắm"
}


def slugify(val: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(val or "")).strip()
    ascii_str = raw.encode("ascii", "ignore").decode("ascii").lower()
    return "-".join(re.findall(r"[a-z0-9]+", ascii_str))


def compute_canonical_product_key(brand: str, model: str, variant: str | None = None) -> str:
    b_slug = slugify(brand)
    m_slug = slugify(model)
    if not b_slug or not m_slug:
        raise ValueError(f"Brand ({brand!r}) and model ({model!r}) must be non-empty.")
    if m_slug.startswith(f"{b_slug}-"):
        m_slug = m_slug[len(b_slug) + 1 :]
    parts = [b_slug, m_slug]
    if variant:
        v_slug = slugify(variant)
        if v_slug:
            parts.append(v_slug)
    return ":".join(parts)


def parse_docx(docx_path: Path) -> list[dict[str, Any]]:
    with zipfile.ZipFile(docx_path) as z:
        rels_content = z.read("word/_rels/document.xml.rels")
        rels_tree = ET.fromstring(rels_content)
        rels = {r.attrib.get("Id"): r.attrib.get("Target") for r in rels_tree if "hyperlink" in r.attrib.get("Type", "")}

        xml_content = z.read("word/document.xml")
        tree = ET.fromstring(xml_content)

        products: list[dict[str, Any]] = []
        current_prod: dict[str, Any] | None = None

        for p in tree.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
            full_text = "".join([t.text for t in p.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t") if t.text]).strip()
            links = []
            for hl in p.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}hyperlink"):
                r_id = hl.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                link_text = "".join([t.text for t in hl.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t") if t.text]).strip()
                if r_id in rels:
                    links.append((link_text, rels[r_id]))

            if not full_text:
                continue

            m = re.match(r"^(\d+)\.\s+(.*)", full_text)
            if m:
                if current_prod:
                    products.append(current_prod)
                current_prod = {
                    "idx": int(m.group(1)),
                    "name": m.group(2).strip(),
                    "fields": {},
                    "links": [],
                }
            elif current_prod:
                if ":" in full_text:
                    k, v = full_text.split(":", 1)
                    current_prod["fields"][k.strip()] = v.strip()
                if links:
                    current_prod["links"].extend(links)

        if current_prod:
            products.append(current_prod)

    return products


def extract_brand_and_model(name: str) -> tuple[str | None, str | None]:
    name_clean = name.strip()

    # 1. Match known brands
    for b in COMPREHENSIVE_BRANDS:
        m = re.search(rf"(?i)\b{re.escape(b)}\b", name_clean)
        if m:
            actual_brand = m.group(0)
            rest = name_clean[m.end():].strip()
            # Find model token right after brand or model code in name
            model_match = re.search(r"^([A-Za-z0-9\-\.\_]+(?:\s+[A-Za-z0-9\-\.\_]+){0,2})", rest)
            if model_match and len(model_match.group(1)) > 1:
                model = model_match.group(1).strip()
            else:
                codes = re.findall(r"\b([A-Z0-9]+(?:[- ][A-Z0-9]+)*)\b", name_clean)
                codes = [c for c in codes if c.lower() != actual_brand.lower() and len(c) > 1]
                model = codes[0].strip() if codes else "Standard"
            return actual_brand, model

    # 2. Extract leading capitalized brand tokens / model codes (skipping common Vietnamese noun words)
    codes = re.findall(r"\b([A-Za-z0-9\-\_]{2,15})\b", name_clean)
    stopwords = {
        "bluetooth", "usb", "type-c", "led", "rgb", "lcd", "full", "hd", "wifi", "ai",
        "anc", "pro", "plus", "max", "mini", "ultra", "nano", "vnd", "app", "gan", "silent",
        "laser", "tu", "thong", "da", "khong", "day", "sac", "pin", "nhanh", "thong minh",
        "cam tay", "gap gon", "chong nuoc", "chong on", "da nang"
    }
    candidates = []
    for c in codes:
        low = c.lower()
        if low not in stopwords and low not in COMMON_VIETNAMESE_PREFIXES and re.search(r"[A-Za-z]", c):
            candidates.append(c)

    if candidates:
        brand = candidates[0]
        rem = [c for c in candidates if c.lower() != brand.lower()]
        model = rem[0] if rem else "Standard"
        return brand, model

    return None, None


def download_image(url: str, timeout: int = 12) -> tuple[bytes | None, str | None]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Referer": "https://shopee.vn/",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            return data, None
    except Exception as exc:
        return None, str(exc)


def validate_image_bytes(payload: bytes, min_dim: int = 400) -> dict[str, Any] | None:
    if not payload or len(payload) > 15 * 1024 * 1024:
        return None
    try:
        im = Image.open(io.BytesIO(payload))
        im.verify()
        im = Image.open(io.BytesIO(payload))
        im.load()
    except Exception:
        return None

    w, h = im.size
    if min(w, h) < min_dim:
        return None
    if max(w, h) / min(w, h) > 4.0:
        return None

    sha256 = hashlib.sha256(payload).hexdigest()
    phash = _hash_image(im)
    fmt = (im.format or "jpg").lower()
    return {
        "width": w,
        "height": h,
        "format": fmt,
        "sha256": sha256,
        "phash": phash,
        "bytes": payload,
    }


def acquire_product_images(
    prod: dict[str, Any],
    ddg_provider: DuckDuckGoImageDiscoveryProvider,
    min_dim: int = 400,
) -> list[dict[str, Any]]:
    valid_images: list[dict[str, Any]] = []

    # Direct links from docx
    direct_urls = [
        u for t, u in prod["links"]
        if "Ảnh" in t and ("susercontent" in u or "cf.shopee" in u or "wikimedia" in u or ("http" in u and "shopee.vn/list" not in u and "shopee.vn/totorovn" not in u))
    ]

    # Try downloading direct URLs first
    for url in direct_urls:
        if not url.startswith("http"):
            continue
        data, err = download_image(url)
        if data:
            meta = validate_image_bytes(data, min_dim=min_dim)
            if meta:
                # Check deduplication with existing accepted images
                is_dup = False
                for existing in valid_images:
                    if meta["sha256"] == existing["sha256"]:
                        is_dup = True
                        break
                    if phash_distance(meta["phash"], existing["phash"]) <= 8:
                        is_dup = True
                        break
                if not is_dup:
                    meta["original_url"] = url
                    valid_images.append(meta)
                    if len(valid_images) >= 3:
                        break

    # If fewer than 2 valid images, use DuckDuckGo search fallback
    if len(valid_images) < 2:
        query_text = f"{prod.get('extracted_brand', '')} {prod.get('extracted_model', '')} {prod['name']}".strip()
        try:
            candidates = ddg_provider.search(ImageSearchQuery(query=query_text[:80]))
            for cand in candidates:
                if len(valid_images) >= 3:
                    break
                data, err = download_image(cand.image_url)
                if data:
                    meta = validate_image_bytes(data, min_dim=min_dim)
                    if meta:
                        is_dup = False
                        for existing in valid_images:
                            if meta["sha256"] == existing["sha256"]:
                                is_dup = True
                                break
                            if phash_distance(meta["phash"], existing["phash"]) <= 8:
                                is_dup = True
                                break
                        if not is_dup:
                            meta["original_url"] = cand.image_url
                            valid_images.append(meta)
        except Exception as exc:
            logger.warning(f"DDG fallback error for {prod['name']}: {exc}")

    return valid_images


def _identity_words(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(re.findall(r"[a-z0-9]+", normalized))


def evaluate_admission(prod: dict[str, Any], valid_images: list[dict[str, Any]]) -> tuple[bool, str | None]:
    name = prod["name"].strip()
    brand = prod.get("extracted_brand")
    model = prod.get("extracted_model")

    if not name:
        return False, "MISSING_NAME"
    if not brand:
        return False, "MISSING_BRAND"

    # Gatekeeper check: normalized brand MUST appear in normalized product name
    norm_name = f" {_identity_words(name)} "
    brand_words = _identity_words(brand)
    if not brand_words or f" {brand_words} " not in norm_name:
        return False, "BRAND_NOT_IN_NAME"

    model_words = _identity_words(model or "")
    if not model_words or _PRICE_LIKE_MODEL.match(str(model).strip()):
        return False, "PRICE_LIKE_MODEL"
    if _AMBIGUOUS_VARIANT.search(name):
        return False, "AMBIGUOUS_VARIANT"
    if len(valid_images) < 2:
        return False, "INSUFFICIENT_IMAGES"

    return True, None


def _default_exchange_inbox() -> str:
    env_inbox = os.environ.get("HERMES_EXCHANGE_INBOX")
    if env_inbox:
        return env_inbox
    repo_root = PI_ROOT.parent.parent
    runtime_inbox = repo_root / "runtime" / "exchange" / "inbox"
    if runtime_inbox.is_dir():
        return str(runtime_inbox)
    fallback = Path(r"D:\work\hermes-data\product-intelligence-data\inbox")
    if fallback.is_dir():
        return str(fallback)
    return str(runtime_inbox)


def main() -> int:
    parser = argparse.ArgumentParser(description="Process Hot Trend docx into raw materials")
    parser.add_argument("--docx-path", default=r"C:\Users\ninak\Downloads\Sản phẩm Hot Trend TikTok & Shopee.docx")
    parser.add_argument("--output-dir", default=str(PI_ROOT / "data" / "hot_trend_materials"))
    parser.add_argument("--exchange-inbox", default=_default_exchange_inbox())
    parser.add_argument("--limit", type=int, default=None, help="Process first N products for testing")
    parser.add_argument("--max-workers", type=int, default=16, help="Parallel download threads")
    args = parser.parse_args()

    docx_path = Path(args.docx_path)
    output_dir = Path(args.output_dir)
    exchange_inbox = Path(args.exchange_inbox)

    if not docx_path.is_file():
        logger.error(f"Docx file not found: {docx_path}")
        return 1

    logger.info(f"Parsing docx: {docx_path}")
    raw_products = parse_docx(docx_path)
    logger.info(f"Loaded {len(raw_products)} products from docx.")

    if args.limit:
        raw_products = raw_products[: args.limit]
        logger.info(f"Limited processing to first {len(raw_products)} products.")

    # Extract brands and models
    for p in raw_products:
        b, m = extract_brand_and_model(p["name"])
        p["extracted_brand"] = b
        p["extracted_model"] = m

    ddg_provider = DuckDuckGoImageDiscoveryProvider(max_results=5)

    logger.info(f"Acquiring and validating images with {args.max_workers} worker threads...")
    start_time = time.time()

    processed_results: list[dict[str, Any]] = []

    def process_single(p):
        imgs = acquire_product_images(p, ddg_provider=ddg_provider)
        admitted, reason = evaluate_admission(p, imgs)
        return {
            "product": p,
            "images": imgs,
            "admitted": admitted,
            "rejection_reason": reason,
        }

    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        futures = {executor.submit(process_single, p): p for p in raw_products}
        count = 0
        for f in as_completed(futures):
            res = f.result()
            processed_results.append(res)
            count += 1
            if count % 25 == 0 or count == len(raw_products):
                logger.info(f"Progress: {count}/{len(raw_products)} products processed...")

    elapsed = time.time() - start_time
    logger.info(f"Image acquisition & validation finished in {elapsed:.1f}s.")

    # Sort results back by product index
    processed_results.sort(key=lambda r: r["product"]["idx"])

    admitted_results = [r for r in processed_results if r["admitted"]]
    rejected_results = [r for r in processed_results if not r["admitted"]]

    logger.info(f"=== SUMMARY ===")
    logger.info(f"Total products: {len(processed_results)}")
    logger.info(f"Admitted (Đạt tiêu chuẩn làm nguyên liệu): {len(admitted_results)}")
    logger.info(f"Rejected: {len(rejected_results)}")

    # Breakdown of rejections
    rejection_counts: dict[str, int] = {}
    for r in rejected_results:
        reason = r["rejection_reason"] or "UNKNOWN"
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
    logger.info(f"Rejection breakdown: {rejection_counts}")

    # Build materials directory
    today_str = datetime.now().strftime("%Y-%m-%d")
    now_utc = datetime.now(timezone.utc)
    batch_id = f"run_{now_utc.strftime('%Y%m%d_%H%M%S')}"

    materials_batch_dir = output_dir / today_str / batch_id
    materials_batch_dir.mkdir(parents=True, exist_ok=True)

    products_manifest_list: list[dict[str, Any]] = []

    for idx, r in enumerate(admitted_results, start=1):
        p = r["product"]
        imgs = r["images"]
        brand = p["extracted_brand"] or "Generic"
        model = p["extracted_model"] or "Model"
        try:
            canonical_key = compute_canonical_product_key(brand, model)
            pkey = hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()
        except Exception:
            canonical_key = f"prod_{p['idx']}"
            pkey = hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()

        folder_slug = canonical_key.replace(":", "_")
        prod_img_dir = materials_batch_dir / "products" / folder_slug / "images"
        prod_img_dir.mkdir(parents=True, exist_ok=True)

        images_manifest: list[dict[str, Any]] = []
        for img_idx, img in enumerate(imgs, start=1):
            fmt = img["format"]
            filename = f"image_{img_idx}.{fmt}"
            dest_file = prod_img_dir / filename
            dest_file.write_bytes(img["bytes"])

            rel_path = f"products/{folder_slug}/images/{filename}"
            images_manifest.append({
                "asset_id": img["sha256"],
                "relative_path": rel_path,
                "original_url": img.get("original_url"),
                "match_score": 1.0,
                "width": img["width"],
                "height": img["height"],
                "format": fmt,
            })

        pdp_url = next((u for t, u in p["links"] if "Xem trên" in t), None)
        products_manifest_list.append({
            "priority": idx,
            "source_index": p["idx"],
            "canonical_product_key": canonical_key,
            "product_key": pkey,
            "name": p["name"],
            "brand": brand,
            "model": model,
            "category": p["fields"].get("Ngành hàng", ""),
            "usp": p["fields"].get("Điểm nhấn USP", ""),
            "script_idea": p["fields"].get("Ý tưởng kịch bản video", ""),
            "source_url": pdp_url,
            "images": images_manifest,
        })

    manifest_payload = {
        "schema_version": "1.0.0",
        "batch_id": batch_id,
        "production_date": today_str,
        "created_at": now_utc.isoformat(),
        "total_source_products": len(processed_results),
        "admitted_products_count": len(admitted_results),
        "rejected_products_count": len(rejected_results),
        "products": products_manifest_list,
    }

    manifest_file = materials_batch_dir / "manifest.json"
    manifest_file.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    ready_file = materials_batch_dir / "READY"
    ready_file.write_text(f"READY at {now_utc.isoformat()}\n", encoding="utf-8")

    logger.info(f"Materials successfully written to: {materials_batch_dir}")

    # Generate Markdown Report
    report_lines = [
        f"# Báo Cáo Nguyên Liệu Sản Phẩm Hot Trend TikTok & Shopee",
        f"- **Ngày xử lý**: {today_str}",
        f"- **Batch ID**: `{batch_id}`",
        f"- **Tổng số sản phẩm trong file docx**: {len(processed_results)}",
        f"- **Số sản phẩm ĐẠT chuẩn (làm nguyên liệu)**: {len(admitted_results)}",
        f"- **Số sản phẩm CHƯA ĐẠT (loại bỏ)**: {len(rejected_results)}",
        f"- **Thư mục lưu trữ nguyên liệu**: `{materials_batch_dir}`",
        "",
        "## Thống kê lý do không đạt:",
    ]
    for reason, c in sorted(rejection_counts.items(), key=lambda x: -x[1]):
        report_lines.append(f"- `{reason}`: {c} sản phẩm")

    report_lines.extend([
        "",
        "## Danh sách 30 sản phẩm đạt chuẩn tiêu biểu làm nguyên liệu:",
        "| STT | Tên sản phẩm | Thương hiệu | Model | Ngành hàng | Số ảnh hợp lệ |",
        "|---|---|---|---|---|---|",
    ])
    for item in products_manifest_list[:30]:
        report_lines.append(f"| {item['priority']} | {item['name']} | {item['brand']} | {item['model']} | {item['category']} | {len(item['images'])} |")

    report_content = "\n".join(report_lines)
    (materials_batch_dir / "report.md").write_text(report_content, encoding="utf-8")

    # Also copy / publish to exchange inbox if configured
    if exchange_inbox.exists():
        exchange_dest = exchange_inbox / today_str / batch_id
        logger.info(f"Publishing to exchange inbox: {exchange_dest}")
        shutil.copytree(materials_batch_dir, exchange_dest, dirs_exist_ok=True)
        logger.info("Exchange publish complete.")

    logger.info("Pipeline completed successfully!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
