"""Exchange boundary importer and publisher for the independent inbox contract."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from .contracts import (
    ExchangeImage,
    ExchangeManifest,
    ExchangeProduct,
    compute_canonical_product_key,
    compute_product_key,
)

DEFAULT_EXCHANGE_INBOX = Path(r"D:\work\hermes-data\product-intelligence-data\inbox")
FALLBACK_EXCHANGE_INBOX = Path(r"D:\work\hermes-agent-data\product-intelligence\inbox")
LEGACY_EXCHANGE_INBOX = Path(r"D:\work\Personal\hermes-product-video-exchange\inbox")
_AMBIGUOUS_VARIANT = re.compile(r"\b\d+\s*[a-zA-Z]*\s*/\s*\d+", re.IGNORECASE)
_URL_LIKE_NAME = re.compile(r"^[^\s/]+\.[a-z]{2,}(?:\.[a-z]{2,})?/", re.IGNORECASE)
_PRICE_LIKE_MODEL = re.compile(r"^\d+(?:[.,]\d+)?\s*(?:k|tr|đ|vnd)$", re.IGNORECASE)
_PROMOTIONAL_NOISE_MODEL = re.compile(
    r"^(?:sale|top|giam|giảm|hot|deal|combo|freeship|chinh\s*hang|chính\s*hãng)\b|\b\d+\s*%"
    r"|^[^\w\s]+|\b(?!2k\b|4k\b|8k\b)\d+\s*(?:k|tr|đ|vnd|usd)\b",
    re.IGNORECASE,
)
_RANKING_LIST_PATTERN = re.compile(r"^top\s*\d+\b", re.IGNORECASE)
_TEMPLATE_FEATURE = re.compile(r"^sản phẩm\s+.+\s+công nghệ đang xu hướng$", re.IGNORECASE)


def _identity_words(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(re.findall(r"[a-z0-9]+", normalized))


def _valid_confirmed_claims(input_review: dict[str, Any] | None) -> list[dict[str, str]]:
    claims: list[dict[str, str]] = []
    for raw in (input_review or {}).get("confirmed_claims", []):
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        source_url = str(raw.get("source_url") or "").strip()
        if not text or not re.match(r"^https?://", source_url, re.IGNORECASE):
            continue
        if "exchange.local" in source_url.lower():
            continue
        claims.append({"text": text, "source_url": source_url})
    return claims


def _trend_links(raw: Any) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not re.match(r"^https?://", url, re.IGNORECASE):
            continue
        links.append({key: str(item[key]) for key in ("url", "title", "host", "provider", "signal_type") if item.get(key)})
    return links


def get_default_exchange_inbox() -> Path:
    """Resolve exchange inbox root, preferring the one with the latest READY batch."""
    workspace_parent = Path(__file__).resolve().parents[4]
    relative_inbox = workspace_parent / "hermes-data" / "product-intelligence-data" / "inbox"
    candidates = []
    try:
        repo_root = Path(__file__).resolve().parents[5]
        candidates.append(repo_root / "runtime" / "exchange" / "inbox")
    except IndexError:
        pass
    candidates.extend([
        DEFAULT_EXCHANGE_INBOX,
        Path(r"Z:\work\hermes-data\product-intelligence-data\inbox"),
        relative_inbox,
        FALLBACK_EXCHANGE_INBOX,
        Path(r"Z:\work\hermes-agent-data\product-intelligence\inbox"),
        LEGACY_EXCHANGE_INBOX,
        Path(r"Z:\work\Personal\hermes-product-video-exchange\inbox"),
    ])
    seen: set[str] = set()
    existing: list[Path] = []
    for p in candidates:
        norm = str(p.resolve()).casefold() if p.exists() else ""
        if norm and norm not in seen:
            seen.add(norm)
            existing.append(p)
    if not existing:
        return DEFAULT_EXCHANGE_INBOX

    best_root = existing[0]
    latest_batch_key = ""
    for root in existing:
        for batch in root.glob("????-??-??/run_*"):
            if (batch / "READY").is_file() and (batch / "manifest.json").is_file():
                # Key by date folder + run folder name (e.g. '2026-10-07/run_20261007_094026')
                batch_key = f"{batch.parent.name}/{batch.name}"
                if batch_key > latest_batch_key:
                    latest_batch_key = batch_key
                    best_root = root
    return best_root


class ExchangeImporter:
    """Reads and validates versioned immutable exchange batches from inbox."""

    def __init__(self, inbox_root: Path | None = None) -> None:
        if inbox_root is not None:
            self.inbox_root = Path(inbox_root)
        else:
            env_inbox = os.environ.get("HERMES_EXCHANGE_INBOX")
            self.inbox_root = Path(env_inbox) if env_inbox else get_default_exchange_inbox()

    def find_latest_ready_batch(self, target_date: date | None = None) -> Path:
        """Locate the latest batch directory containing a valid READY signal file."""
        if not self.inbox_root.is_dir():
            raise FileNotFoundError(f"Exchange inbox directory not found: {self.inbox_root}")

        def created_key(candidate: Path) -> tuple[int, float, str]:
            try:
                raw = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
                created_at = datetime.fromisoformat(str(raw.get("created_at") or "").replace("Z", "+00:00"))
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                return (1, created_at.timestamp(), candidate.name)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return (0, candidate.stat().st_mtime, candidate.name)

        if target_date is not None:
            day_dir = self.inbox_root / target_date.isoformat()
            if not day_dir.is_dir():
                raise FileNotFoundError(f"No exchange batches found for date: {target_date.isoformat()}")
            candidates = [candidate for candidate in day_dir.glob("run_*")
                          if (candidate / "READY").is_file() and (candidate / "manifest.json").is_file()]
            if candidates:
                return max(candidates, key=created_key)
        else:
            try:
                day_dirs = sorted([d for d in self.inbox_root.iterdir() if d.is_dir() and len(d.name) == 10], reverse=True)
            except OSError:
                day_dirs = []
            for day_dir in day_dirs:
                candidates = [candidate for candidate in day_dir.glob("run_*")
                              if (candidate / "READY").is_file() and (candidate / "manifest.json").is_file()]
                if candidates:
                    return max(candidates, key=created_key)

        raise FileNotFoundError(f"No ready exchange batch (manifest.json + READY) found beneath {self.inbox_root}")

    def load_manifest(self, batch_dir: Path) -> ExchangeManifest:
        """Parse manifest.json from a confirmed READY batch directory."""
        manifest_file = batch_dir / "manifest.json"
        if not manifest_file.is_file():
            raise FileNotFoundError(f"manifest.json missing in batch: {batch_dir}")

        raw = json.loads(manifest_file.read_text(encoding="utf-8"))
        schema_version = str(raw.get("schema_version") or "1.0.0")
        batch_id = str(raw.get("batch_id") or batch_dir.name)
        production_date = str(raw.get("production_date") or batch_dir.parent.name)
        created_at = str(raw.get("created_at") or "")

        products: list[ExchangeProduct] = []
        for p in raw.get("products") or []:
            brand = str(p.get("brand") or "").strip()
            model = str(p.get("model") or "").strip()
            variant = p.get("variant")
            canonical_key = str(p.get("canonical_product_key") or "")
            if not canonical_key and brand and model:
                canonical_key = compute_canonical_product_key(brand, model, variant)
            pkey = str(p.get("product_key") or "")
            if not pkey and canonical_key:
                pkey = compute_product_key(canonical_key)

            images: list[ExchangeImage] = []
            for img in p.get("images") or []:
                images.append(
                    ExchangeImage(
                        asset_id=str(img.get("asset_id") or ""),
                        relative_path=str(img.get("relative_path") or img.get("local_path") or ""),
                        original_url=img.get("original_url"),
                        match_score=float(img.get("match_score", 1.0)),
                        width=img.get("width"),
                        height=img.get("height"),
                        format=img.get("format"),
                    )
                )

            products.append(
                ExchangeProduct(
                    priority=int(p.get("priority", len(products) + 1)),
                    canonical_product_key=canonical_key,
                    product_key=pkey,
                    name=str(p.get("name") or f"{brand} {model}"),
                    brand=brand,
                    model=model,
                    variant=variant,
                    category=str(p.get("category") or ""),
                    price_display=str(p.get("price_display") or ""),
                    price_min=p.get("price_min"),
                    price_max=p.get("price_max"),
                    currency=str(p.get("currency") or "VND"),
                    hot_reason=str(p.get("hot_reason") or ""),
                    key_features=str(p.get("key_features") or ""),
                    keywords=list(p.get("keywords") or []),
                    images=images,
                    source_product_id=str(p.get("source_product_id") or p.get("id") or ""),
                    source_url=p.get("source_url") or p.get("pdp_url"),
                    selection_reason=p.get("selection_reason"),
                    trend_evidence=[item for item in (p.get("trend_evidence") or []) if isinstance(item, dict)],
                    identity_mode=str(p.get("identity_mode") or "sku"),
                    source_snapshot=dict(p.get("source_snapshot") or {}),
                    creative_input=dict(p.get("creative_input") or {}) or None,
                    seller_description=p.get("seller_description"),
                )
            )

        return ExchangeManifest(
            schema_version=schema_version,
            batch_id=batch_id,
            production_date=production_date,
            created_at=created_at,
            products=products,
        )

    def validate_admission(
        self, product: ExchangeProduct, batch_dir: Path, input_review: dict[str, Any] | None = None,
        qc_level: str = "middle",
    ) -> tuple[bool, str | None, list[dict[str, Any]]]:
        """Fail closed before research/creative work using cheap local checks.

        A readable file and a matcher score prove only file handling. Image
        identity is decided by source Image QC; old per-image confirmation is
        not an admission requirement. Legacy claims still need a source.
        """
        from .creative_policy import normalize_qc_level

        qc_level = normalize_qc_level(qc_level)
        if qc_level == "low":
            readable = []
            root = Path(batch_dir).resolve()
            for image in product.images:
                path = (root / image.relative_path).resolve()
                try:
                    path.relative_to(root)
                    if not path.is_file() or path.is_symlink():
                        continue
                    with Image.open(path) as decoded:
                        decoded.verify()
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    readable.append({
                        "asset_id": image.asset_id, "relative_path": image.relative_path,
                        "absolute_path": str(path), "original_url": image.original_url,
                        "match_score": image.match_score, "sha256": digest,
                        "identity_review_reason": "LOW_QC_ADMITTED_FOR_PRODUCT_LOCK",
                        "identity_conflict": False,
                    })
                except (OSError, ValueError):
                    continue
            if not readable:
                return False, "BLOCKED_INSUFFICIENT_IMAGES: no technically readable candidate image", []
            return True, None, readable

        if not product.name or not product.name.strip():
            return False, "missing product name", []
        if _URL_LIKE_NAME.match(product.name.strip()):
            return False, "product name is a URL or marketplace slug", []
        
        # Check for ranking list pattern in product name
        if _RANKING_LIST_PATTERN.match(product.name.strip()):
            return False, "product name is a ranking article/listicle, not a single product", []
        
        brand = _identity_words(product.brand)
        if product.identity_mode not in {"sku", "source_product"}:
            return False, "unsupported product identity mode", []
        if product.identity_mode == "source_product":
            snapshot = product.source_snapshot
            original = snapshot.get("product") or {}
            if (not product.canonical_product_key or not product.source_product_id
                    or snapshot.get("canonical_product_id") != product.canonical_product_key
                    or original.get("canonical_key") != product.canonical_product_key
                    or original.get("id") != product.source_product_id):
                return False, "source product requires a matching persisted PI identity/snapshot", []
            original_ids = {str(i.get("asset_id") or "") for i in original.get("images", []) if isinstance(i, dict)}
            if any(image.asset_id not in original_ids for image in product.images):
                return False, "source product images are not bound to the PI snapshot", []
        if product.identity_mode == "sku" and product.model.strip() and _PRICE_LIKE_MODEL.match(product.model.strip()):
            return False, "product model looks like a price", []
        
        # Check for promotional noise in model
        if _PROMOTIONAL_NOISE_MODEL.search(product.model.strip()):
            return False, "product model contains promotional or ranking noise (e.g. Sale/Top/Deal/%)", []

        if _AMBIGUOUS_VARIANT.search(product.name):
            return False, "ambiguous product variant", []

        valid_imgs: list[dict[str, Any]] = []
        asset_id_counts: dict[str, int] = {}
        for image in product.images:
            asset_id_counts[str(image.asset_id)] = asset_id_counts.get(str(image.asset_id), 0) + 1
        for img in product.images:
            img_path = (batch_dir / img.relative_path).resolve()
            try:
                img_path.relative_to(batch_dir.resolve())
            except ValueError:
                continue
            if not img_path.is_file() or img_path.is_symlink():
                continue
            try:
                with Image.open(img_path) as im:
                    im.verify()
                # URL tokens and absent visual labels are not proof that the
                # image is a different SKU. Image QC decides visible identity.
                if asset_id_counts.get(str(img.asset_id), 0) > 1:
                    continue
                valid_imgs.append(
                    {
                        "asset_id": img.asset_id,
                        "relative_path": img.relative_path,
                        "absolute_path": str(img_path),
                        "original_url": img.original_url,
                        "match_score": img.match_score,
                        "width": img.width,
                        "height": img.height,
                        "identity_review_reason": "ADMITTED_FOR_VISUAL_QC",
                        "identity_conflict": False,
                    }
                )
            except Exception:  # noqa: BLE001
                continue

        if not valid_imgs:
            return False, "BLOCKED_INSUFFICIENT_IMAGES: no technically readable candidate image", []
        if product.identity_mode == "sku" and not str(product.variant or "").strip():
            return False, "PRODUCT_VARIANT_REQUIRED: verify the exact product variant in PI", []

        # Collapse duplicate catalog rows to one usable reference instead of
        # requiring the source to supply a quota of distinct images.
        seen_paths: set[str] = set()
        for image in valid_imgs:
            path_key = str(Path(str(image["absolute_path"])).resolve()).casefold()
            image["_path_key"] = path_key
            if path_key in seen_paths:
                image["identity_review_reason"] = (
                    f"{image['identity_review_reason']}; DUPLICATE_FILE_REFERENCE: multiple asset IDs point to the same file"
                )
                image["identity_conflict"] = True
            else:
                seen_paths.add(path_key)

        # Check physical bytes duplication as well
        seen_byte_hashes: dict[str, str] = {}
        for image in valid_imgs:
            img_p = Path(str(image["absolute_path"]))
            try:
                bh = hashlib.sha256(img_p.read_bytes()).hexdigest()
                image["sha256"] = bh
                if bh in seen_byte_hashes:
                    prev_id = seen_byte_hashes[bh]
                    image["identity_review_reason"] = (
                        f"{image['identity_review_reason']}; DUPLICATE_BYTE_CONTENT: identical content to asset {prev_id}"
                    )
                    image["identity_conflict"] = True
                elif not image.get("identity_conflict"):
                    seen_byte_hashes[bh] = str(image["asset_id"])
            except OSError:
                image["identity_conflict"] = True
                image["identity_review_reason"] = (
                    f"{image['identity_review_reason']}; UNREADABLE_FILE: cannot read file bytes"
                )

        review = input_review or {}
        confirmed_claims = _valid_confirmed_claims(review)

        # Legacy exchange records lack verified source lineage and require manual review
        if product.canonical_product_key.startswith("legacy:") or product.brand.strip().lower() == "legacy":
            if not confirmed_claims:
                for image in valid_imgs:
                    image.pop("_path_key", None)
                return False, "REVIEW_REQUIRED: legacy exchange manifest requires a claim-specific source", valid_imgs

        # A manual review selects the source assets to use. It does not act as
        # an independent identity anchor; Image QC evaluates each selected view.
        confirmed_ids = {str(asset_id) for asset_id in review.get("confirmed_image_asset_ids", [])}
        if confirmed_ids:
            by_id = {str(image["asset_id"]): image for image in valid_imgs}
            selected = [by_id[asset_id] for asset_id in sorted(confirmed_ids) if asset_id in by_id]
            if len(selected) != len(confirmed_ids) or not selected:
                for image in valid_imgs:
                    image.pop("_path_key", None)
                return False, "REVIEW_REQUIRED: select a distinct source image", valid_imgs
            distinct_selected: list[dict[str, Any]] = []
            seen_selected_hashes: set[str] = set()
            for image in selected:
                digest = str(image.get("sha256") or "")
                if not digest:
                    for item in valid_imgs:
                        item.pop("_path_key", None)
                    return False, "REVIEW_REQUIRED: selected source image is unreadable", valid_imgs
                if digest in seen_selected_hashes:
                    continue
                selected_image = dict(image)
                selected_image["identity_conflict"] = False
                selected_image["identity_review_reason"] = "ADMITTED_FOR_VISUAL_QC"
                distinct_selected.append(selected_image)
                seen_selected_hashes.add(digest)
            for image in selected:
                image.pop("_path_key", None)
            for image in valid_imgs:
                image.pop("_path_key", None)
            return True, None, distinct_selected

        # Keep one copy of each readable image; visual QC decides whether that
        # reference shows the intended product and can support a render.
        clean_candidates = [img for img in valid_imgs if not img.get("identity_conflict")]
        if not clean_candidates:
            for image in valid_imgs:
                image.pop("_path_key", None)
            return False, "BLOCKED_INSUFFICIENT_IMAGES: no non-conflicting candidate image", valid_imgs

        for image in clean_candidates:
            image["identity_review_reason"] = "ADMITTED_FOR_VISUAL_QC"
            image.pop("_path_key", None)
        for image in valid_imgs:
            image.pop("_path_key", None)
        return True, None, clean_candidates


def publish_daily_run_to_exchange(
    source_run_dir: Path,
    inbox_root: Path = DEFAULT_EXCHANGE_INBOX,
    batch_id: str | None = None,
    master_catalog_conn: Any | None = None,
    cooldown_days: int = 30,
    max_products: int = 10,
) -> Path:
    """Atomically publish a Product-Intelligence run to the independent exchange inbox with gatekeeper checks.

    1. Validates each product against admission rules and master catalog cooldown.
    2. Copies products and reference images into an isolated exchange directory.
    3. Generates canonical product keys and versioned manifest.json.
    4. Writes the READY sentinel file ONLY after all files are completely written.
    
    Args:
        source_run_dir: Product-Intelligence daily run directory with products.json
        inbox_root: Exchange inbox root directory
        batch_id: Optional batch ID override
        master_catalog_conn: SQLite connection for deduplication tracking
        cooldown_days: Days to block recently produced products (default: 30)
        max_products: Maximum products to include in batch (default: 10)
    
    Returns:
        Path to published batch directory with READY marker
    """
    source_run_dir = Path(source_run_dir)
    products_file = source_run_dir / "products.json"
    if not products_file.is_file():
        raise FileNotFoundError(f"Source products.json not found in {source_run_dir}")

    raw = json.loads(products_file.read_text(encoding="utf-8"))
    production_date = str(raw.get("date") or source_run_dir.name)
    now = datetime.now(timezone.utc)
    effective_batch_id = batch_id or f"run_{now.strftime('%Y%m%d_%H%M%S')}"

    inbox_root = Path(inbox_root)
    batch_dir = inbox_root / production_date / effective_batch_id
    temp_dir = inbox_root / production_date / f".tmp_{effective_batch_id}"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Initialize master catalog repository if connection provided
    master_repo = None
    if master_catalog_conn is not None:
        from .repository import DailyImportRepository
        master_repo = DailyImportRepository(master_catalog_conn)

    products_out: list[dict[str, Any]] = []
    raw_products = raw.get("products") or []
    admitted_count = 0
    seen_batch_keys: set[str] = set()

    for idx, p in enumerate(raw_products, start=1):
        if admitted_count >= max_products:
            break  # Enforce daily limit
        brand = str(p.get("brand") or "").strip()
        model = str(p.get("model") or "").strip()
        variant = p.get("variant")
        name = str(p.get("name") or f"{brand} {model}").strip()

        # Handle canonical key
        try:
            canonical_key = compute_canonical_product_key(brand, model, variant)
            pkey = compute_product_key(canonical_key)
        except Exception:  # noqa: BLE001
            canonical_key = f"unknown:{idx}"
            pkey = f"invalid_{idx}"

        if canonical_key in seen_batch_keys or pkey in seen_batch_keys:
            continue
        seen_batch_keys.add(canonical_key)
        seen_batch_keys.add(pkey)

        # Gatekeeper checks if master catalog enabled
        if master_repo is not None:
            # Record as scouted
            master_repo.upsert_scouted_product(
                canonical_product_key=canonical_key,
                product_key=pkey,
                brand=brand,
                model=model,
                variant=variant or "",
                source_url=p.get("pdp_url"),
                raw_metadata=p,
            )

            # Check cooldown
            is_available, rejection_reason = master_repo.check_cooldown(canonical_key, cooldown_days)
            if not is_available:
                master_repo.mark_rejected(canonical_key, rejection_reason or "COOLDOWN")
                continue  # Skip this product

            # Basic admission validation
            if not name or not brand or not model:
                master_repo.mark_rejected(canonical_key, "MISSING_REQUIRED_FIELDS")
                continue

            if _AMBIGUOUS_VARIANT.search(name):
                master_repo.mark_rejected(canonical_key, "AMBIGUOUS_VARIANT")
                continue

            if _URL_LIKE_NAME.match(name):
                master_repo.mark_rejected(canonical_key, "URL_LIKE_NAME")
                continue

            if _PRICE_LIKE_MODEL.match(model):
                master_repo.mark_rejected(canonical_key, "PRICE_LIKE_MODEL")
                continue

            # Check for promotional noise in model
            if _PROMOTIONAL_NOISE_MODEL.search(model):
                master_repo.mark_rejected(canonical_key, "PROMOTIONAL_NOISE_MODEL")
                continue

            # Check for ranking list pattern in name
            if _RANKING_LIST_PATTERN.match(name):
                master_repo.mark_rejected(canonical_key, "RANKING_LIST_TITLE")
                continue

            # Verify at least one image exists; Image QC checks whether it is usable.
            valid_img_count = 0
            for img in p.get("images") or []:
                src_path = Path(str(img.get("absolute_path") or source_run_dir / img.get("local_path", "")))
                if src_path.is_file():
                    valid_img_count += 1
                    if valid_img_count >= 1:
                        break

            if valid_img_count < 1:
                master_repo.mark_rejected(canonical_key, "INSUFFICIENT_IMAGES")
                continue

            # Mark as admitted
            master_repo.mark_admitted(canonical_key)

        # Copy images into products/<canonical_key>/images/
        clean_key_folder = canonical_key.replace(":", "_")
        target_img_dir = temp_dir / "products" / clean_key_folder / "images"
        target_img_dir.mkdir(parents=True, exist_ok=True)

        images_out: list[dict[str, Any]] = []
        for img_idx, img in enumerate(p.get("images") or [], start=1):
            src_img_path = Path(str(img.get("absolute_path") or source_run_dir / img.get("local_path", "")))
            rel_dest = Path("products") / clean_key_folder / "images" / f"image_{img_idx}{src_img_path.suffix or '.jpg'}"
            dest_img_path = temp_dir / rel_dest

            if src_img_path.is_file():
                shutil.copy2(src_img_path, dest_img_path)

            images_out.append(
                {
                    "asset_id": img.get("asset_id") or f"asset_{pkey[:8]}_{img_idx}",
                    "relative_path": str(rel_dest).replace("\\", "/"),
                    "original_url": img.get("original_url"),
                    "match_score": float(img.get("match_score", 1.0)),
                    "width": img.get("width"),
                    "height": img.get("height"),
                    "format": img.get("format") or src_img_path.suffix.lstrip("."),
                }
            )

        trend_evidence = _trend_links(p.get("trend_evidence"))
        key_features = str(p.get("key_features") or "").strip()
        if _TEMPLATE_FEATURE.match(key_features):
            key_features = ""
        products_out.append(
            {
                "priority": admitted_count + 1,  # Use admitted count for priority
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
                "key_features": key_features,
                "trend_evidence": trend_evidence,
                "keywords": p.get("keywords") or [],
                "images": images_out,
                "source_product_id": str(p.get("id") or f"prod_{idx:02d}"),
                "source_url": p.get("pdp_url") or (trend_evidence[0]["url"] if trend_evidence else None),
                "selection_reason": p.get("hot_reason"),
            }
        )
        admitted_count += 1

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

    # Write READY file LAST
    (batch_dir / "READY").write_text(f"READY at {now.isoformat()}\n", encoding="utf-8")
    return batch_dir
