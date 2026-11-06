"""Product Intelligence MCP server.

Thin MCP adapter over the Media Intelligence service. No acquisition or
validation logic lives here; Hermes-facing tools only.
"""

from __future__ import annotations

import os
import sys
from typing import Any

# Prevent OpenMP/NumPy/PyWavelets deadlocks in background threads
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"

# imagehash.phash is impractically slow inside the stdio server process
# (48s per image observed) and can wedge a research call past the MCP timeout.
# Disable it by default so any client is safe; an explicit opt-in overrides.
# The exporter already sets DISABLE_IMAGEHASH=true, so workflow behavior is
# unchanged; this only hardens direct MCP calls.
if os.environ.get("PRODUCT_RESEARCH_ENABLE_IMAGEHASH") != "true":
    os.environ.setdefault("PRODUCT_RESEARCH_DISABLE_IMAGEHASH", "true")

print(f"Server Startup: env DISABLE_IMAGEHASH={os.environ.get('PRODUCT_RESEARCH_DISABLE_IMAGEHASH')}", file=sys.stderr, flush=True)
try:
    if os.environ.get("PRODUCT_RESEARCH_DISABLE_IMAGEHASH") != "true":
        import imagehash  # noqa: F401
        print("Server Startup: imagehash imported successfully", file=sys.stderr, flush=True)
    else:
        print("Server Startup: imagehash import skipped", file=sys.stderr, flush=True)
except ImportError:
    print("Server Startup: imagehash import failed (ImportError)", file=sys.stderr, flush=True)

from mcp.server.fastmcp import FastMCP  # noqa: E402
from media import MediaConfig, MediaIntelligenceService, ProductIdentityHint  # noqa: E402


def _service(data_dir=None) -> MediaIntelligenceService:
    return MediaIntelligenceService(MediaConfig(data_dir=data_dir) if data_dir else MediaConfig())


def _acquisition_service():
    from media.web_acquisition import AcquisitionService

    return AcquisitionService()


def _research_persistence():
    from media.research_persistence import ResearchPersistenceService

    return ResearchPersistenceService()


def _resource_pack():
    from media.resource_pack import ResourcePackService

    return ResourcePackService()


mcp = FastMCP("product-intelligence")


@mcp.tool()
def inspect_product_source(url: str, browser_fallback: bool = True) -> dict:
    """[advanced/diagnostic] Inspect a single source URL via HTTP with optional
    browser fallback.

    For debugging or targeted source checks. Returns structured source
    observations (success/blocked/challenged/partial/failed), extracted
    metadata, and discovered image URLs. Not required for normal research.
    """
    result = _acquisition_service().acquire(url, browser_fallback=browser_fallback)
    content = result.content
    return {
        "requested_url": result.requested_url,
        "status": result.status.value,
        "reason": result.reason,
        "observations": [observation.model_dump(mode="json") for observation in result.observations],
        "content": content.model_dump(mode="json") if content else None,
        "image_candidates": [
            {
                "image_url": candidate.image_url,
                "source_page_url": candidate.source_page_url,
                "host": candidate.host,
                "discovery_provider": candidate.discovery_provider,
            }
            for candidate in result.image_candidates
        ],
    }



@mcp.tool()
def extract_product_evidence(url: str, browser_fallback: bool = True) -> dict:
    """[advanced/diagnostic] Acquire a single URL and extract typed product
    evidence with field-level provenance.

    For debugging a specific source. Evidence is what one source claims, not
    canonical truth; use research_product for the full workflow.
    """
    from media.evidence import ProductEvidenceService

    acquisition_result, evidence_result = ProductEvidenceService().research_url(url, browser_fallback=browser_fallback)
    return {
        "acquisition": {
            "requested_url": acquisition_result.requested_url,
            "status": acquisition_result.status.value,
            "observations": [observation.model_dump(mode="json") for observation in acquisition_result.observations],
        },
        "evidence_status": evidence_result.status.value,
        "source": evidence_result.source.model_dump(mode="json"),
        "evidence": evidence_result.evidence.model_dump(mode="json") if evidence_result.evidence else None,
        "warnings": evidence_result.warnings,
        "errors": evidence_result.errors,
        "conflicts": [conflict.model_dump(mode="json") for conflict in evidence_result.conflicts],
    }


@mcp.tool()
def research_product(
    url: str | None = None,
    product_name: str | None = None,
    brand: str | None = None,
    model: str | None = None,
    variant: str | None = None,
    canonical_product_key: str | None = None,
    query: str | None = None,
    additional_urls: list[str] | None = None,
    discover_sources: bool = False,
    target_assets: int | None = None,
    browser_fallback: bool = True,
) -> dict:
    """Research a product from a URL or product identity and return resolved
    product, listing, evidence, and media intelligence.

    Primary Hermes-facing capability. Accepts a URL, additional URLs, or a
    name/brand/model/variant/query. When discover_sources is true, sources are found
    automatically. Challenged or blocked sources are tolerated and reported in
    source_summary; they do not fail the research. Multiple products may be
    resolved if identities differ. The result is persisted when persistence is
    configured (see snapshot_id / persistence_status).
    """
    from media.models import ProductIdentityHint
    from media.research import ProductResearchService, ResearchProductRequest

    hint = None
    if product_name or brand or model or variant:
        hint = ProductIdentityHint(name=product_name or "", brand=brand, model=model, variant=variant)
    request = ResearchProductRequest(
        url=url,
        additional_urls=additional_urls or [],
        product_identity_hint=hint,
        query=query,
        discover_sources=discover_sources,
        target_assets=target_assets,
        browser_fallback=browser_fallback,
    )
    result = ProductResearchService(persistence=_research_persistence()).research(request)
    return _research_payload(result)


def _research_payload(result) -> dict:
    challenged = [s for s in result.source_results if s.acquisition_status == "challenged"]
    blocked = [s for s in result.source_results if s.acquisition_status == "blocked"]
    failed = [s for s in result.source_results if s.acquisition_status == "failed"]
    return {
        "status": result.status.value,
        "error": result.error,
        "research_id": result.research_id,
        "snapshot_id": result.snapshot_id,
        "persistence_status": result.persistence_status,
        "source_summary": {
            "attempted": len(result.source_results),
            "usable": result.summary.usable_sources,
            "challenged": len(challenged),
            "blocked": len(blocked),
            "failed": len(failed),
        },
        "source_results": [source.model_dump(mode="json") for source in result.source_results],
        "canonical_products": [
            {
                "canonical_product_id": product.canonical_product_id,
                "brand": product.brand.value if product.brand else None,
                "model": product.model.value if product.model else None,
                "name": product.name.value if product.name else None,
                "variants": [variant.model_dump(mode="json") for variant in product.variants],
                "listings": [
                    {
                        "listing_id": listing.listing_id,
                        "host": listing.host,
                        "seller": listing.seller,
                        "price": listing.price,
                        "currency": listing.currency,
                        "availability": listing.availability,
                    }
                    for listing in product.listings
                ],
            }
            for product in result.canonical_products
        ],
        "media_assets": [
            {
                "asset_id": asset.asset_id,
                "local_path": asset.local_path,
                "match_status": asset.assessment.match_status.value,
                "product_match_score": asset.assessment.product_match_score,
                "usage_policy": asset.provenance.get("usage_policy"),
            }
            for asset in result.media_assets
        ],
        "unresolved_evidence_ids": result.unresolved_evidence_ids,
        "conflicts": [conflict.model_dump(mode="json") for conflict in result.conflicts],
        "warnings": result.warnings,
        "discovered_sources": [
            {"url": source.url, "host": source.host, "source_class": source.source_class.value, "rank": source.rank}
            for source in result.discovered_sources
        ],
        "summary": result.summary.model_dump(mode="json"),
    }


@mcp.tool()
def research_product_images(
    product_name: str,
    brand: str | None = None,
    model: str | None = None,
    variant: str | None = None,
    keywords: list[str] | None = None,
    target_assets: int | None = None,
    minimum_short_edge: int | None = None,
) -> dict:
    """[advanced/diagnostic] Research product images for a product identity.

    Returns selected MediaAsset metadata plus rejection and coverage summaries.
    Not required for normal research (research_product includes media).
    """
    hint = ProductIdentityHint(
        name=product_name,
        brand=brand,
        model=model,
        variant=variant,
        keywords=keywords or [],
    )
    result = _service().research(hint, target_assets=target_assets, minimum_short_edge=minimum_short_edge)
    return _result_payload(result)


@mcp.tool()
def validate_product_image(product_name: str, image_url: str, brand: str | None = None, model: str | None = None) -> dict:
    """[advanced/diagnostic] Validate a single image URL against a product
    identity (technical + match + classification).

    For debugging a specific image. Not a generic HTTP downloader; not required
    for normal research.
    """
    service = _service()
    from media.models import ImageCandidate, ProductIdentityHint

    candidate = ImageCandidate(
        candidate_id="manual_1",
        discovery_provider="user_provided",
        query=product_name,
        image_url=image_url,
        host=_host(image_url),
    )
    hint = ProductIdentityHint(name=product_name, brand=brand, model=model)
    try:
        _status, payload = service.downloader.download(candidate)
    except Exception as exc:  # noqa: BLE001 - per-image failure isolation
        return {"status": "rejected", "reason": getattr(exc, "reason", "IMAGE_HTTP_STATUS"), "detail": getattr(exc, "detail", None)}
    technical = service.validator.validate(candidate, payload)
    if technical.status == "rejected":
        return {"status": "rejected", "reason": technical.reason}
    match = service.matcher.match(candidate, hint, title=product_name)
    view = service.classifier.classify(width=technical.width or 0, height=technical.height or 0, url=image_url)
    return {
        "status": "valid",
        "technical": technical.model_dump(exclude={"payload_bytes"}),
        "match": match.model_dump(),
        "view": view.value,
    }


@mcp.tool()
def get_product_media(product_name: str) -> dict:
    """[advanced/diagnostic] Return previously known media for a product.

    Deferred: no media-specific persistence surface yet. Always reports no
    cached state rather than fabricating it.
    """
    return {"status": "deferred", "product": product_name, "assets": [], "note": "persistence not implemented"}


@mcp.tool()
def get_product_research(snapshot_id: str) -> dict:
    """Retrieve a durable historical research snapshot by snapshot id.

    Returns the immutable snapshot recorded at research time; does not re-run
    research. Unknown ids return not_found.
    """
    try:
        snapshot = _research_persistence().get(snapshot_id)
    except KeyError:
        return {"status": "not_found", "snapshot_id": snapshot_id}
    except Exception as exc:  # noqa: BLE001 - structured retrieval failure
        return {"status": "error", "snapshot_id": snapshot_id, "reason": f"{type(exc).__name__}: {exc}"}
    return {
        "status": "found",
        "snapshot_id": snapshot.snapshot_id,
        "research_id": snapshot.research_id,
        "created_at": snapshot.created_at,
        "research_status": snapshot.status,
        "result": snapshot.result,
    }


_built_packs: dict[str, Any] = {}


def _resource_pack_assets(pack) -> list[dict]:
    """Expose the exact local media manifest needed by downstream QC."""
    return [
        {
            "asset_id": item.asset_id,
            "local_path": item.local_path,
            "uri": f"file://{item.local_path}",
            "media_role": item.source_class,
            "view": item.view,
            "match_status": item.match_status,
            "match_confidence": item.product_match_score,
            "usage_policy": item.usage_policy,
            "variant_match": item.variant_match,
        }
        for item in pack.media
    ]


@mcp.tool()
def build_resource_pack(snapshot_id: str, variant_id: str | None = None) -> dict:
    """Build a trusted Resource Pack from a persisted research snapshot.

    Primary downstream bridge toward Video Factory. Returns pack_status:
    ready_to_lock / needs_review / incomplete / rejected. Ambiguous identity or
    variant is never auto-locked; media variant verification is conservative
    (unknown multi-variant media requires review). Delegate-only handler.
    """

    try:
        snapshot = _research_persistence().get(snapshot_id)
        pack = _resource_pack().build_from_snapshot(snapshot, variant_id=variant_id)
        pack_id = f"pack_{snapshot_id}" if not variant_id else f"pack_{snapshot_id}__{variant_id}"
        pack.resource_pack_id = pack_id
        _built_packs[pack_id] = pack
    except KeyError:
        return {"status": "not_found", "snapshot_id": snapshot_id}
    except Exception as exc:  # noqa: BLE001 - structured failure
        return {"status": "error", "snapshot_id": snapshot_id, "reason": f"{type(exc).__name__}: {exc}"}
    return {
        "status": "built",
        "resource_pack_id": pack.resource_pack_id,
        "snapshot_id": snapshot.snapshot_id,
        "pack_status": pack.pack_status.value,
        "reason": pack.reason,
        "canonical_product_id": pack.canonical_product_id,
        "product_identity": pack.product_identity,
        "variant_id": pack.variant_id,
        "media_count": len(pack.media),
        "assets": _resource_pack_assets(pack),
        "claims": [
            {
                "claim_id": claim.claim_id,
                "text": claim.text,
                "status": claim.status.value,
                "evidence_refs": claim.source_evidence_ids,
                "snapshot_id": snapshot.snapshot_id,
                "canonical_product_id": pack.canonical_product_id,
                "variant_id": pack.variant_id,
            }
            for claim in pack.claims
        ],
        "evidence": [
            {
                "evidence_id": item["evidence_id"],
                "snapshot_id": snapshot.snapshot_id,
                "canonical_product_id": pack.canonical_product_id,
                "variant_id": pack.variant_id,
                "source": item.get("source", {}),
            }
            for item in snapshot.result.get("product_evidence", [])
            if item.get("evidence_id") in {
                ref for claim in pack.claims for ref in claim.source_evidence_ids
            }
        ],
        "source_results": snapshot.result.get("source_results", []),
        "discovered_sources": snapshot.result.get("discovered_sources", []),
        "restrictions": pack.restrictions,
        "completeness": pack.completeness,
    }


@mcp.tool()
def get_resource_pack(resource_pack_id: str) -> dict:
    """Return the latest resource pack metadata."""
    lock = _resource_pack().get_latest(resource_pack_id)
    if lock is None:
        return {"status": "not_found", "resource_pack_id": resource_pack_id}
    return {
        "status": "found",
        "resource_pack_id": lock.resource_pack_id,
        "version": lock.version,
        "created_at": lock.created_at,
        "canonical_product_id": lock.canonical_product_id,
        "variant_id": lock.variant_id,
        "media_asset_ids": lock.media_asset_ids,
        "provenance": lock.provenance,
    }


@mcp.tool()
def lock_resource_pack(resource_pack_id: str, owner_user_id: str = "system") -> dict:
    """Lock a built ResourcePack into an immutable ResourcePackLock with manifest digest."""
    import hashlib
    import json

    repo = _research_persistence().repository
    pack_service = _resource_pack()

    pack = _built_packs.get(resource_pack_id)
    if pack is None:
        # Recover pack from persisted snapshot if resource_pack_id carries deterministic snapshot mapping
        if resource_pack_id.startswith("pack_"):
            remainder = resource_pack_id.removeprefix("pack_")
            if "__" in remainder:
                snap_id, var_id = remainder.split("__", 1)
            else:
                snap_id, var_id = remainder, None
            try:
                snap = _research_persistence().get(snap_id)
                pack = pack_service.build_from_snapshot(snap, variant_id=var_id)
                pack.resource_pack_id = resource_pack_id
                _built_packs[resource_pack_id] = pack
            except KeyError:
                return {"status": "not_found", "resource_pack_id": resource_pack_id}
            except Exception as exc:  # noqa: BLE001 - structured failure
                return {"status": "error", "resource_pack_id": resource_pack_id, "reason": f"{type(exc).__name__}: {exc}"}
        else:
            return {"status": "not_found", "resource_pack_id": resource_pack_id}

    if pack is None:
        return {"status": "not_found", "resource_pack_id": resource_pack_id}

    if pack.pack_status.value != "ready_to_lock":
        return {
            "status": "error",
            "resource_pack_id": resource_pack_id,
            "reason": f"Cannot lock resource pack with status '{pack.pack_status.value}': {pack.reason or 'not ready to lock'}",
        }

    assets = _resource_pack_assets(pack)

    manifest_content = json.dumps({"assets": assets}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    manifest_digest = f"sha256:{hashlib.sha256(manifest_content.encode('utf-8')).hexdigest()}"

    existing_by_identity = repo.get_lock_by_identity(pack.resource_pack_id, owner_user_id, manifest_digest)
    if existing_by_identity:
        return existing_by_identity

    lock = pack_service.lock(pack)

    lock_payload = {
        "status": "locked",
        "lock_id": f"lock_{lock.resource_pack_id}_v{lock.version}",
        "resource_pack_id": lock.resource_pack_id,
        "snapshot_id": lock.research_snapshot_id,
        "canonical_product_id": lock.canonical_product_id,
        "variant_id": lock.variant_id,
        "owner_user_id": owner_user_id,
        "version": lock.version,
        "manifest_digest": manifest_digest,
        "created_at": lock.created_at,
        "media_asset_ids": lock.media_asset_ids,
        "resolved_values": lock.resolved_values,
        "provenance": lock.provenance,
        "assets": assets,
    }
    return repo.save_lock(lock_payload)


@mcp.tool()
def get_resource_pack_lock(lock_id: str) -> dict:
    """Retrieve an immutable ResourcePackLock by lock_id."""
    import re

    lock = _research_persistence().repository.get_lock(lock_id)
    if lock:
        return lock
    pack_id = lock_id.removeprefix("lock_")
    base_pack_id = re.sub(r"_v\d+$", "", pack_id)
    latest = _resource_pack().get_latest(base_pack_id)
    if latest:
        latest_lock_id = f"lock_{latest.resource_pack_id}_v{latest.version}"
        repo_lock = _research_persistence().repository.get_lock(latest_lock_id)
        if repo_lock:
            return repo_lock
    return {"status": "not_found", "lock_id": lock_id}


def _host(url: str) -> str | None:
    from urllib.parse import urlsplit

    return urlsplit(url).netloc.casefold().removeprefix("www.") or None


def _result_payload(result) -> dict:
    return {
        "status": result.status.value,
        "product": result.product.model_dump(exclude_none=True),
        "discovered_count": result.discovered_count,
        "downloaded_count": result.downloaded_count,
        "valid_count": result.valid_count,
        "selected_assets": [asset.model_dump(mode="json") for asset in result.selected_assets],
        "rejections": [rejection.model_dump(mode="json") for rejection in result.rejections],
        "coverage": result.coverage,
        "error": result.error,
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
