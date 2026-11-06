"""Auto-Retry Image QC helper functions for cleanable failure detection and recovery."""

from __future__ import annotations

from typing import Any

from video_factory.qc.models import QCReport, QCVerdict


def is_cleanable_failure(qc_report: QCReport | dict[str, Any] | Any) -> bool:
    """Determine if Image QC failure is recoverable via image cleaning (inpainting)."""
    if qc_report is None:
        return False

    if isinstance(qc_report, dict):
        verdict = qc_report.get("verdict")
        score = qc_report.get("score", 0)
        findings = qc_report.get("findings", {})
    else:
        verdict = getattr(qc_report, "verdict", None)
        score = getattr(qc_report, "score", 0)
        findings = getattr(qc_report, "findings", {})

    verdict_str = verdict.value if hasattr(verdict, "value") else str(verdict)
    if verdict_str.lower() != "fail":
        return False

    # Score too low indicates fundamental issues (missing files, corrupted, etc.)
    if score < 20:
        return False

    if not isinstance(findings, dict):
        findings = {}

    # Check if any failed items have cleanable risk flags
    items = findings.get("items", [])
    failed_items = [it for it in items if isinstance(it, dict) and not it.get("eligible")]
    cleanable_risks = {"watermark", "logo", "text_overlay", "banner", "badge", "shop_domain"}

    cleanable_count = 0
    for item in failed_items:
        if not isinstance(item, dict):
            continue
        findings_item = item.get("findings", {})
        if not isinstance(findings_item, dict):
            continue
        risk_flags = findings_item.get("risk_flags", [])
        if not isinstance(risk_flags, list):
            continue
        if set(risk_flags) & cleanable_risks:
            cleanable_count += 1

    # Check recovery actions - if reacquire_references is present without any cleanable items, it's non-cleanable
    recovery_actions = findings.get("recovery_actions", [])
    if "reacquire_references" in recovery_actions:
        eligible_count = len([it for it in items if isinstance(it, dict) and it.get("eligible")])
        if eligible_count >= 1 or cleanable_count == 0:
            return False

    return cleanable_count > 0


def extract_artifacts_from_qc(qc_report: QCReport) -> list[dict[str, Any]]:
    """Extract detected artifact categories from QC findings for targeted inpainting.
    
    Maps QC risk flags to ProductImageCleaner ArtifactCategory enum values.
    
    Args:
        qc_report: Image QC report with detected issues
        
    Returns:
        List of artifact descriptors for image cleaner:
        [
            {
                "asset_id": "img_001",
                "category": "GENERIC_WATERMARK",
                "confidence": 0.95,
                "bounding_boxes": [...]  # Optional, if available
            },
            ...
        ]
    """
    items = qc_report.findings.get("items", [])
    
    # Map QC risk flags to ProductImageCleaner categories
    category_map = {
        "watermark": "GENERIC_WATERMARK",
        "logo": "SELLER_BRANDING",
        "text_overlay": "SPEC_CALLOUT_OVERLAY",
        "banner": "PROMOTIONAL_TAG",
        "badge": "MARKETPLACE_BADGE",
        "shop_domain": "SHOP_DOMAIN",
        "qr_code": "QR_OR_BARCODE",
        "guarantee": "GUARANTEE_STAMP",
    }
    
    artifacts: list[dict[str, Any]] = []
    
    for item in items:
        if not isinstance(item, dict):
            continue
        
        # Only process ineligible items (failed QC)
        if item.get("eligible", False):
            continue
        
        asset_id = item.get("asset_id", "")
        findings = item.get("findings", {})
        
        if not isinstance(findings, dict):
            continue
        
        risk_flags = findings.get("risk_flags", [])
        if not isinstance(risk_flags, list):
            continue
        
        # Extract detected categories
        detected_categories = []
        for flag in risk_flags:
            if flag in category_map:
                detected_categories.append(category_map[flag])
        
        if detected_categories:
            artifacts.append({
                "asset_id": asset_id,
                "categories": detected_categories,
                "confidence": item.get("score", 0) / 100.0,
                "reason": item.get("reason", ""),
                "findings": findings,
            })
    
    return artifacts


def replace_images_in_resource_pack(
    resource_pack: dict[str, Any],
    cleaned_images: list[dict[str, Any]],
) -> dict[str, Any]:
    """Replace original images with cleaned versions in resource pack.
    
    Preserves original image paths for audit trail.
    
    Args:
        resource_pack: Original resource pack with media_assets
        cleaned_images: List of cleaned image results from ProductImageCleaner
        
    Returns:
        Updated resource pack with cleaned image paths
    """
    import copy
    
    cleaned_pack = copy.deepcopy(resource_pack)
    cleaned_by_id = {img["asset_id"]: img for img in cleaned_images}
    
    media_assets = cleaned_pack.get("media_assets", [])
    updated_assets = []
    
    for asset in media_assets:
        if not isinstance(asset, dict):
            updated_assets.append(asset)
            continue
        
        asset_id = asset.get("asset_id")
        
        if asset_id in cleaned_by_id:
            # Replace with cleaned version
            cleaned = cleaned_by_id[asset_id]
            updated_asset = dict(asset)
            updated_asset["local_path"] = cleaned["cleaned_path"]
            updated_asset["sha256"] = cleaned["clean_sha256"]
            updated_asset["original_path"] = asset.get("local_path")  # Preserve original
            updated_asset["cleaning_applied"] = True
            updated_asset["detected_artifacts"] = cleaned.get("detected_watermarks", [])
            updated_asset["detected_categories"] = cleaned.get("detected_categories", [])
            updated_assets.append(updated_asset)
        else:
            # Keep original (not in cleaned set)
            updated_assets.append(asset)
    
    cleaned_pack["media_assets"] = updated_assets
    return cleaned_pack


def extract_product_from_resource_pack(resource_pack: dict[str, Any]) -> dict[str, Any]:
    """Extract normalized product metadata from resource pack for QC.
    
    Args:
        resource_pack: Resource pack containing product metadata
        
    Returns:
        Normalized product dict with brand, model, variant, display_name
    """
    # Try multiple locations for product metadata
    product = resource_pack.get("product", {})
    if not product:
        product = resource_pack.get("product_metadata", {})
    if not product:
        # Fallback: extract from top-level fields
        product = {
            "brand": resource_pack.get("brand", ""),
            "model": resource_pack.get("model", ""),
            "variant": resource_pack.get("variant"),
            "display_name": resource_pack.get("product_name") or resource_pack.get("name", ""),
            "verified_claims": resource_pack.get("key_features") or resource_pack.get("verified_claims", []),
        }
    return product


SAFE_ZONE_POLICY = {
    "right_action_bar": "Shift action leftward, maintain subject within X∈[0.08,0.78]",
    "bottom_overlay": "Raise subject framing upward, keep critical motion above Y=0.76",
    "top_header": "Lower camera framing downward, keep critical motion below Y=0.12",
}


def inject_safe_zone_constraint(prompt: str, collision_zones: list[str]) -> str:
    """Inject spatial camera/framing constraints into prompt based on collision zones."""
    if not collision_zones:
        return prompt

    constraints = [
        SAFE_ZONE_POLICY[zone]
        for zone in collision_zones
        if zone in SAFE_ZONE_POLICY
    ]
    if not constraints:
        return prompt

    prefix = f"[SPATIAL CONSTRAINT: {'; '.join(constraints)}] "
    return prefix + prompt
