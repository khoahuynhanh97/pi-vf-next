"""Publisher script owned by Product-Intelligence.

Converts an internal daily_runs scout batch into an immutable, versioned
exchange batch beneath hermes-product-video-exchange/inbox/.

Contract boundary:
- products/<canonical_product_key>/images/
- manifest.json (schema 1.0.0)
- READY (sentinel marker written LAST)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_INBOX_ROOT = Path(r"D:\work\hermes-data\product-intelligence-data\inbox")
FALLBACK_INBOX_ROOT = Path(r"D:\work\hermes-agent-data\product-intelligence\inbox")
LEGACY_INBOX_ROOT = Path(r"D:\work\Personal\hermes-product-video-exchange\inbox")
DEFAULT_RUNS_ROOT = Path(__file__).resolve().parent.parent / "data" / "daily_runs"
_TEMPLATE_FEATURE = re.compile(r"^sản phẩm\s+.+\s+công nghệ đang xu hướng$", re.IGNORECASE)


def get_default_inbox_root() -> Path:
    env_inbox = os.environ.get("HERMES_EXCHANGE_INBOX")
    if env_inbox:
        return Path(env_inbox).resolve()
    try:
        repo_root = Path(__file__).resolve().parents[3]
        runtime_inbox = repo_root / "runtime" / "exchange" / "inbox"
        if runtime_inbox.is_dir():
            return runtime_inbox.resolve()
    except IndexError:
        pass
    if DEFAULT_INBOX_ROOT.exists():
        return DEFAULT_INBOX_ROOT
    if FALLBACK_INBOX_ROOT.exists():
        return FALLBACK_INBOX_ROOT
    return DEFAULT_INBOX_ROOT


def _slugify(val: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(val or "")).strip()
    ascii_str = raw.encode("ascii", "ignore").decode("ascii").lower()
    return "-".join(re.findall(r"[a-z0-9]+", ascii_str))


def compute_canonical_product_key(brand: str, model: str, variant: str | None = None) -> str:
    brand_slug = _slugify(brand)
    model_slug = _slugify(model)
    if not brand_slug or not model_slug:
        raise ValueError(f"Both brand ({brand!r}) and model ({model!r}) must be non-empty.")

    # Strip redundant brand prefix in model name (e.g. "baseus-bowie-wm02" -> "bowie-wm02")
    if model_slug.startswith(f"{brand_slug}-"):
        model_slug = model_slug[len(brand_slug) + 1:]

    parts = [brand_slug, model_slug]
    if variant:
        v_slug = _slugify(variant)
        if v_slug:
            parts.append(v_slug)
    return ":".join(parts)


def compute_product_key(canonical_product_key: str) -> str:
    return hashlib.sha256(canonical_product_key.encode("utf-8")).hexdigest()


def _safe_trend_evidence(raw: Any) -> list[dict[str, str]]:
    """Keep only displayable trend links; they are never claim verification."""
    evidence: list[dict[str, str]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not re.match(r"^https?://", url, re.IGNORECASE):
            continue
        clean = {key: str(item[key]) for key in ("url", "title", "host", "provider", "signal_type") if item.get(key)}
        evidence.append(clean)
    return evidence


def _feature_text_for_exchange(value: Any) -> str:
    text = str(value or "").strip()
    return "" if _TEMPLATE_FEATURE.match(text) else text


def publish_daily_run(
    source_run_dir: Path,
    inbox_root: Path | None = None,
    batch_id: str | None = None,
) -> Path:
    source_run_dir = Path(source_run_dir)
    products_file = source_run_dir / "products.json"
    if not products_file.is_file():
        raise FileNotFoundError(f"Source products.json not found in {source_run_dir}")

    raw = json.loads(products_file.read_text(encoding="utf-8"))
    production_date = str(raw.get("date") or source_run_dir.name)
    now = datetime.now(timezone.utc)
    effective_batch_id = batch_id or f"run_{now.strftime('%Y%m%d_%H%M%S')}"

    inbox_root = Path(inbox_root) if inbox_root is not None else get_default_inbox_root()
    day_inbox = inbox_root / production_date
    batch_dir = day_inbox / effective_batch_id
    temp_dir = day_inbox / f".tmp_{effective_batch_id}"

    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    products_out: list[dict[str, Any]] = []
    raw_products = raw.get("products") or []

    for idx, p in enumerate(raw_products, start=1):
        brand = str(p.get("brand") or "").strip()
        model = str(p.get("model") or "").strip()
        variant = str(p.get("variant") or "").strip()
        name = str(p.get("name") or f"{brand} {model}").strip()

        if not variant:
            # Detect hardware variant (capacity, power, color) or default to Tiêu chuẩn
            m = re.search(r'(\d+[\.,]?\d*\s*(?:mah|w|gb|tb|v|hz|dpi)|màu\s+\w+|đen|trắng|xám|black|white|grey)', name, re.IGNORECASE)
            variant = m.group(1).strip().title() if m else "Tiêu chuẩn"

        try:
            canonical_key = compute_canonical_product_key(brand, model, variant)
            pkey = compute_product_key(canonical_key)
        except Exception:
            canonical_key = f"unknown:{idx}"
            pkey = f"invalid_{idx}"

        clean_folder = canonical_key.replace(":", "_")
        target_img_dir = temp_dir / "products" / clean_folder / "images"
        target_img_dir.mkdir(parents=True, exist_ok=True)

        images_out: list[dict[str, Any]] = []
        for img_idx, raw_img in enumerate(p.get("images") or [], start=1):
            if isinstance(raw_img, str):
                img: dict[str, Any] = {"local_path": raw_img, "absolute_path": raw_img}
            elif isinstance(raw_img, dict):
                img = raw_img
            else:
                continue
            src_img = Path(str(img.get("absolute_path") or source_run_dir / img.get("local_path", "")))
            rel_dest = Path("products") / clean_folder / "images" / f"image_{img_idx}{src_img.suffix or '.jpg'}"
            dest_img = temp_dir / rel_dest

            if src_img.is_file():
                shutil.copy2(src_img, dest_img)

            images_out.append({
                "asset_id": img.get("asset_id") or f"asset_{pkey[:8]}_{img_idx}",
                "relative_path": str(rel_dest).replace("\\", "/"),
                "original_url": img.get("original_url"),
                "match_score": float(img.get("match_score", 1.0)),
                "width": img.get("width"),
                "height": img.get("height"),
                "format": img.get("format") or src_img.suffix.lstrip("."),
            })

        trend_evidence = _safe_trend_evidence(p.get("trend_evidence"))
        source_url = str(p.get("pdp_url") or "").strip() or (trend_evidence[0]["url"] if trend_evidence else None)
        products_out.append({
            "priority": idx,
            "canonical_product_key": canonical_key,
            "product_key": pkey,
            "name": name,
            "brand": brand,
            "model": model,
            "variant": variant,
            "category": p.get("category", ""),
            "price_display": p.get("price_display", ""),
            "price_min": p.get("price_min"),
            "price_max": p.get("price_max"),
            "currency": p.get("currency", "VND"),
            "hot_reason": p.get("hot_reason", ""),
            "key_features": _feature_text_for_exchange(p.get("key_features")),
            "trend_evidence": trend_evidence,
            "keywords": p.get("keywords") or [],
            "images": images_out,
            "source_product_id": str(p.get("id") or f"prod_{idx:02d}"),
            "source_url": source_url,
            "selection_reason": p.get("hot_reason"),
        })

    manifest_payload = {
        "schema_version": "1.0.0",
        "batch_id": effective_batch_id,
        "production_date": production_date,
        "created_at": now.isoformat(),
        "products": products_out,
    }

    (temp_dir / "manifest.json").write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # Atomic rename to final batch directory
    if batch_dir.exists():
        shutil.rmtree(batch_dir)
    temp_dir.rename(batch_dir)

    # Write READY marker file LAST
    (batch_dir / "READY").write_text(f"READY at {now.isoformat()}\n", encoding="utf-8")
    return batch_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish latest Product-Intelligence run to exchange inbox")
    parser.add_argument("--source-run", type=Path, default=None, help="Path to specific daily run directory")
    parser.add_argument("--inbox-root", type=Path, default=None, help="Exchange inbox destination")
    parser.add_argument("--batch-id", default=None, help="Batch ID override (default: run_YYYYMMDD_HHMMSS)")
    args = parser.parse_args(argv)

    if args.source_run is not None:
        source_run = args.source_run
    else:
        runs = sorted(DEFAULT_RUNS_ROOT.glob("????-??-??"))
        if not runs:
            print(f"[ERROR] No daily runs found in {DEFAULT_RUNS_ROOT}", file=sys.stderr)
            return 1
        source_run = runs[-1]

    try:
        out_batch = publish_daily_run(source_run, inbox_root=args.inbox_root, batch_id=args.batch_id)
        print(f"[SUCCESS] Published exchange batch: {out_batch}")
        return 0
    except Exception as exc:
        print(f"[ERROR] Failed to publish exchange batch: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
