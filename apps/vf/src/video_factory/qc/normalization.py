"""Truthful Product Identity and Grounded Claims Normalization."""

from __future__ import annotations

import re
import hashlib
import json
from pathlib import Path
from typing import Any

from video_factory.qc.models import NormalizedProduct
from video_factory.creative.source_backed import is_source_backed_claim

_ID_LIKE_PATTERNS = re.compile(
    r"^(pack_snap_|rp_|cand_|snap_|proj-|wf-|pv-|[0-9a-f]{16,64})",
    re.IGNORECASE,
)

_FORBIDDEN_GENERIC_NAMES = {
    "sản phẩm công nghệ tiện ích",
    "sản phẩm công nghệ",
    "thiết bị tiện ích",
    "product identity",
    "unknown product",
    "generic product",
}
_SELLER_NAME_MARKERS = (
    "minh gear",
    "shop",
    "store",
    "seller",
    "official mall",
    "tiktok shop",
)


def _asset_file(path_value: Any) -> Path | None:
    if not path_value:
        return None
    path = Path(str(path_value)).expanduser()
    if not path.is_file() or path.stat().st_size <= 0:
        return None
    return path.resolve()


def _is_decodable_image(path: Path) -> bool:
    """Check bytes at the PI boundary before they can become eligible media."""
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
        return True
    except Exception:
        return False


def _identity_aliases(value: str, *extra: Any) -> set[str]:
    """Return canonical exact-match keys for an identity field and declared aliases."""
    values = [value]
    for item in extra:
        if isinstance(item, (list, tuple, set)):
            values.extend(str(v) for v in item)
        elif item:
            values.append(str(item))
    return {re.sub(r"[^a-z0-9]+", "", item.lower()) for item in values if str(item).strip()}


def _unwrap_resource_pack(value: dict[str, Any]) -> dict[str, Any]:
    """Unwrap the PI envelope variants observed at the Hermes boundary."""
    current = value
    for _ in range(4):
        nested = current.get("resource_pack")
        if isinstance(nested, dict):
            current = nested
            continue
        nested = current.get("raw_result")
        if isinstance(nested, dict):
            current = nested
            continue
        break
    return current


def _media_manifest(raw_rp: dict[str, Any]) -> list[dict[str, Any]]:
    assets = (
        raw_rp.get("assets")
        or raw_rp.get("assets_mapping")
        or raw_rp.get("media_assets")
        or raw_rp.get("product_references")
        or []
    )
    if isinstance(assets, dict):
        mapped_assets = []
        for asset_id, value in assets.items():
            if isinstance(value, dict):
                item = dict(value)
                item.setdefault("asset_id", asset_id)
                mapped_assets.append(item)
            else:
                mapped_assets.append({"asset_id": asset_id, "local_path": value})
        assets = mapped_assets
    if not isinstance(assets, list):
        return []

    manifest: list[dict[str, Any]] = []
    for index, raw_asset in enumerate(assets, start=1):
        asset = {"asset_id": f"asset_{index}"} if isinstance(raw_asset, str) else dict(raw_asset or {})
        asset_id = str(asset.get("asset_id") or asset.get("id") or f"asset_{index}")
        local_path = asset.get("local_path") or asset.get("absolute_path") or asset.get("path")
        digest = ""
        eligible = False
        if local_path:
            file_path = _asset_file(local_path)
            if file_path is not None:
                digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
                local_path = str(file_path)
                eligible = _is_decodable_image(file_path)
        declared_status = str(asset.get("status") or "").lower()
        if declared_status in {"rejected", "quarantined", "invalid", "unsupported"}:
            eligible = False
        manifest.append({
            **asset,
            "asset_id": asset_id,
            "local_path": local_path,
            "sha256": digest,
            "eligible": eligible,
            "provenance": asset.get("provenance") or asset.get("source") or "product_intelligence",
        })
    return manifest


def canonical_resource_pack_for_fingerprint(value: dict[str, Any] | None) -> dict[str, Any]:
    """Return the shared, envelope-independent artifact view used by QC and gates.

    Paths and declared hashes are deliberately replaced with the bytes currently
    present on disk. This makes a same-path replacement stale immediately while
    keeping a PI envelope and its unwrapped resource pack equivalent.
    """
    raw = _unwrap_resource_pack(dict(value or {}))
    canonical = dict(raw)
    for key in ("assets", "assets_mapping", "media_assets", "product_references", "_initial_qc_preview"):
        canonical.pop(key, None)
    manifest = _media_manifest(raw)
    canonical["media_manifest"] = [
        {
            "asset_id": item["asset_id"],
            "sha256": item["sha256"],
            "eligible": item["eligible"],
            "provenance": item["provenance"],
        }
        for item in manifest
    ]
    return canonical


def resource_pack_fingerprint(value: dict[str, Any] | None) -> str:
    canonical = canonical_resource_pack_for_fingerprint(value)
    encoded = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_product_identity(
    resource_pack: dict[str, Any] | None,
    source_context: dict[str, Any] | None = None,
    product_name_fallback: str = "",
) -> NormalizedProduct:
    """Extract and validate normalized product identity from PI resource pack and source context.

    Enforces:
    1. Truthful Brand & Model.
    2. Identity mismatch between source and pack MUST raise ValueError (blocking live render).
    3. Pack ID is NEVER used as display name.
    4. Generic fallbacks ('Sản phẩm công nghệ tiện ích') are rejected.
    5. Verified claims are extracted only from grounded evidence.
    """
    source_ctx = dict(source_context or {})
    src_brand = str(source_ctx.get("brand") or "").strip()
    src_model = str(source_ctx.get("model") or "").strip()
    src_variant = str(source_ctx.get("variant") or "").strip()

    rp = dict(resource_pack or {})
    raw_rp = _unwrap_resource_pack(rp) if isinstance(rp, dict) else {}
    prod_vals = raw_rp.get("resolved_values", {}) if isinstance(raw_rp.get("resolved_values"), dict) else {}
    prod_identity = raw_rp.get("product_identity", {}) if isinstance(raw_rp.get("product_identity"), dict) else {}

    rp_brand = str(prod_vals.get("brand") or prod_identity.get("brand") or raw_rp.get("brand") or "").strip()
    rp_model = str(prod_vals.get("model") or prod_identity.get("model") or raw_rp.get("model") or "").strip()
    rp_variant = str(prod_vals.get("variant") or prod_identity.get("variant") or raw_rp.get("variant") or "").strip()

    # 1. Check identity mismatch
    if src_brand and rp_brand:
        src_brand_aliases = source_ctx.get("brand_aliases")
        rp_brand_aliases = raw_rp.get("brand_aliases") or prod_identity.get("brand_aliases")
        if not _identity_aliases(src_brand, src_brand_aliases).intersection(_identity_aliases(rp_brand, rp_brand_aliases)):
            raise ValueError(f"Product identity brand mismatch: source brand '{src_brand}' != resource pack brand '{rp_brand}'")

    if src_model and rp_model:
        src_model_aliases = source_ctx.get("model_aliases")
        rp_model_aliases = raw_rp.get("model_aliases") or prod_identity.get("model_aliases")
        if not _identity_aliases(src_model, src_model_aliases).intersection(_identity_aliases(rp_model, rp_model_aliases)):
            raise ValueError(f"Product identity model mismatch: source model '{src_model}' != resource pack model '{rp_model}'")

    effective_brand = src_brand or rp_brand
    effective_model = src_model or rp_model
    effective_variant = src_variant or rp_variant

    if not effective_brand and not effective_model:
        snapshot = source_ctx.get('source_snapshot') or {}
        from video_factory.creative.contracts import canonical_digest
        source_identity = (prod_identity.get('identity_mode') == 'source_product'
            and source_ctx.get('identity_mode') == 'source_product'
            and bool(prod_identity.get('canonical_product_id'))
            and prod_identity['canonical_product_id'] == snapshot.get('canonical_product_id')
            and prod_identity.get('source_snapshot_digest') == canonical_digest(snapshot))
        if not source_identity:
            raise ValueError("Cannot resolve truthful product identity: both brand and model are empty. Refusing generic fallback.")

    # 2. Resolve display_name
    cand_name = str(
        prod_vals.get("name")
        or prod_identity.get("name")
        or raw_rp.get("product_name")
        or raw_rp.get("name")
        or product_name_fallback
        or ""
    ).strip()

    # Check if candidate name is an internal ID
    if (
        not cand_name
        or _ID_LIKE_PATTERNS.match(cand_name)
        or cand_name.lower() in _FORBIDDEN_GENERIC_NAMES
        or any(marker in cand_name.lower() for marker in _SELLER_NAME_MARKERS)
    ):
        if effective_brand and effective_model:
            cand_name = f"{effective_brand} {effective_model}"
        elif effective_brand:
            cand_name = effective_brand
        else:
            cand_name = effective_model

    if cand_name.lower() in _FORBIDDEN_GENERIC_NAMES:
        raise ValueError(f"Refusing generic product display name: '{cand_name}'")

    category = str(
        prod_vals.get("category")
        or prod_identity.get("category")
        or raw_rp.get("category")
        or source_ctx.get("category")
        or "Thiết bị tiện ích"
    ).strip()

    # 3. Grounded verified claims
    claims: list[str] = []
    raw_claims = (
        raw_rp.get("verified_selling_points")
        or raw_rp.get("claims")
        or raw_rp.get("key_features")
        or raw_rp.get("verified_claims")
        or source_ctx.get("key_features")
        or []
    )
    if isinstance(raw_claims, str):
        raw_claims = [raw_claims]
    if isinstance(raw_claims, (list, tuple)):
        for c in raw_claims:
            if isinstance(c, dict):
                if not is_source_backed_claim(c):
                    continue
                s = str(c.get("claim") or c.get("text") or "").strip()
            else:
                s = str(c).strip()
            # filter out marketing buzzwords
            if s and not any(bad in s.lower() for bad in ["review chân thực", "giỏ hàng", "ưu đãi", "giảm giá", "chính hãng 100%"]):
                claims.append(s)

    # 4. Extract approved media
    candidate_media = _media_manifest(raw_rp)

    return NormalizedProduct(
        brand=effective_brand,
        model=effective_model,
        variant=effective_variant,
        display_name=cand_name,
        category=category,
        verified_claims=claims,
        # This field remains the compatibility name used by older callers. It
        # contains candidates only; QC creates the immutable eligible manifest.
        approved_media=candidate_media,
        source_context=source_ctx,
    )
