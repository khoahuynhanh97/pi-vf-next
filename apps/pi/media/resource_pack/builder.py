"""ResourcePackBuilder: trusted product → resource pack draft with validation.

Consumes a resolved ProductResearchResult (or a persisted snapshot result) and
produces a draft plus a completeness evaluation. Ambiguity is never auto-locked.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from media.research.models import ProductResearchResult
from media.resolution.models import CanonicalProduct

from .models import (
    PackClaim,
    PackMediaItem,
    PackStatus,
    ResourcePack,
    ResourcePackDraft,
)

MIN_TRUSTED_MEDIA = 1
MAX_TRUSTED_MEDIA = 6


class ResourcePackBuilder:
    def __init__(self, *, min_media: int = MIN_TRUSTED_MEDIA, max_media: int = MAX_TRUSTED_MEDIA):
        self.min_media = min_media
        self.max_media = max_media

    def build(self, result: ProductResearchResult, *, variant_id: str | None = None, research_snapshot_id: str = "") -> ResourcePack:
        draft, status, reason, checks = self.evaluate(result, variant_id=variant_id)
        if status != PackStatus.READY_TO_LOCK:
            return ResourcePack(
                resource_pack_id=draft.resource_pack_id,
                created_at=_now(),
                research_snapshot_id=research_snapshot_id,
                canonical_product_id=draft.canonical_product_id,
                variant_id=draft.variant_id,
                product_identity=draft.product_identity,
                specifications=draft.specifications,
                claims=draft.claims,
                media=draft.media,
                restrictions=draft.restrictions,
                provenance=draft.provenance,
                completeness=checks,
                pack_status=status,
                reason=reason,
            )
        return ResourcePack(
            resource_pack_id=draft.resource_pack_id,
            created_at=_now(),
            research_snapshot_id=research_snapshot_id,
            canonical_product_id=draft.canonical_product_id,
            variant_id=draft.variant_id,
            product_identity=draft.product_identity,
            specifications=draft.specifications,
            claims=draft.claims,
            media=draft.media,
            restrictions=draft.restrictions,
            provenance=draft.provenance,
            completeness=checks,
            pack_status=PackStatus.READY_TO_LOCK,
            reason="",
        )

    def evaluate(self, result: ProductResearchResult, *, variant_id: str | None = None) -> tuple[ResourcePackDraft, PackStatus, str, dict]:
        products = result.canonical_products
        checks: dict = {}

        # 1. Identity lock: exactly one resolved product required.
        if not products:
            return self._empty_draft(), PackStatus.REJECTED, "no resolved canonical product", {"identity": "rejected"}
        if len(products) > 1:
            return self._empty_draft(), PackStatus.NEEDS_REVIEW, f"ambiguous: {len(products)} resolved products", {"identity": "needs_review"}
        product = products[0]
        checks["identity"] = "resolved"

        # 2. Variant lock: explicit variant only; never inferred/mixed.
        target_variant = None
        if variant_id is not None:
            matches = [variant for variant in product.variants if variant.variant_id == variant_id]
            if not matches:
                return self._empty_draft(product), PackStatus.NEEDS_REVIEW, f"variant {variant_id} not found", {**checks, "variant": "needs_review"}
            target_variant = matches[0]
            checks["variant"] = "locked"
        elif product.variants:
            checks["variant"] = "needs_review"  # variants exist but none selected
        else:
            checks["variant"] = "none"

        # 3. Trusted media from Media Intelligence selected assets only.
        media, variant_state = self._select_media(result.media_assets, product, target_variant)
        checks["media_count"] = len(media)
        checks["media"] = "ok" if len(media) >= self.min_media else "insufficient" if media else "none"
        checks["media_variant"] = variant_state
        checks["variant_verified_media"] = variant_state.get("verified", 0)

        # 4. Claims separated by status.
        claims = self._build_claims(product, result)

        # 5. Completeness.
        status, reason = self._completeness(checks, target_variant, len(media))
        draft = ResourcePackDraft(
            resource_pack_id=f"pack_{uuid.uuid4().hex}",
            canonical_product_id=product.canonical_product_id,
            variant_id=target_variant.variant_id if target_variant else variant_id,
            product_identity=self._identity(product),
            specifications=[field.model_dump(mode="json", exclude_none=True) for field in product.specifications],
            claims=claims,
            media=media,
            restrictions=self._restrictions(media),
            provenance={
                "canonical_product_id": product.canonical_product_id,
                "supporting_evidence_ids": product.supporting_evidence_ids,
                "research_sources": len(result.source_results),
            },
        )
        return draft, status, reason, checks

    # --- helpers ------------------------------------------------------------

    def _empty_draft(self, product: CanonicalProduct | None = None) -> ResourcePackDraft:
        return ResourcePackDraft(
            resource_pack_id=f"pack_{uuid.uuid4().hex}",
            canonical_product_id=product.canonical_product_id if product else "",
        )

    def _identity(self, product: CanonicalProduct) -> dict:
        return {
            "name": product.name.value if product.name else None,
            "brand": product.brand.value if product.brand else None,
            "model": product.model.value if product.model else None,
            "category": product.category.value if product.category else None,
        }

    def _select_media(self, assets, product: CanonicalProduct, target_variant) -> tuple[list[PackMediaItem], dict]:
        items = []
        variant_state = {"verified": 0, "unverified": 0, "conflict": 0}
        for asset in assets:
            state = self._asset_variant_state(asset, target_variant)
            variant_state[state] = variant_state.get(state, 0) + 1
            if state == "conflict":
                continue
            items.append(PackMediaItem(
                asset_id=asset.asset_id,
                local_path=asset.local_path,
                view=asset.assessment.view.value if asset.assessment else "unknown",
                product_match_score=asset.assessment.product_match_score if asset.assessment else None,
                match_status=asset.assessment.match_status.value if asset.assessment else "unknown",
                source_class=asset.provenance.get("source_class", "unknown"),
                usage_policy=asset.provenance.get("usage_policy", "reference_only"),
                variant_match=state,
            ))
        # Prefer matched assets, dedup by asset_id, bound to max.
        items.sort(key=lambda item: (item.product_match_score is not None, item.product_match_score or 0), reverse=True)
        seen = set()
        selected = []
        for item in items:
            if item.asset_id in seen:
                continue
            seen.add(item.asset_id)
            selected.append(item)
            if len(selected) >= self.max_media:
                break
        return selected, variant_state

    def _asset_variant_state(self, asset, target_variant) -> str:
        """Conservative per-asset variant verification.

        MediaAsset carries no variant identity in M1-M7 contracts. This reads
        OPTIONAL variant markers (e.g. ``variant``/``color``/``capacity`` keys in
        provenance or discovery) without inventing linkage. A high
        product_match_score is never treated as variant match.
        """
        if target_variant is None:
            return "unknown"
        markers = {**asset.provenance, **asset.discovery}
        marker_value = None
        for key in ("variant", "variant_id", "color", "capacity", "configuration"):
            if markers.get(key):
                marker_value = str(markers[key])
                break
        if not marker_value:
            return "unknown"
        target_values = [str(value) for value in target_variant.dimensions.values()]
        normalized_marker = _normalize(marker_value)
        if any(_normalize(value) == normalized_marker for value in target_values):
            return "verified"
        return "conflict"

    def _build_claims(self, product: CanonicalProduct, result) -> list[PackClaim]:
        # Claims belong to their originating ProductEvidence record.  The
        # canonical product's supporting IDs identify the selected product,
        # but are not a claim-level provenance relationship.
        selected_ids = set(product.supporting_evidence_ids)
        claims: list[PackClaim] = []
        seen_claims: set[tuple[str, str]] = set()
        for evidence in getattr(result, "product_evidence", []):
            if evidence.evidence_id not in selected_ids:
                continue
            for index, claim in enumerate(evidence.claims):
                claim_text = claim.text.value.strip()
                claim_key = (evidence.evidence_id, " ".join(claim_text.casefold().split()))
                if claim_key in seen_claims:
                    continue
                seen_claims.add(claim_key)
                claim_id = "claim_" + hashlib.sha256(
                    f"{evidence.evidence_id}:{index}".encode("utf-8")
                ).hexdigest()[:16]
                claims.append(PackClaim(
                    claim_id=claim_id,
                    text=claim_text,
                    status="source_claim",
                    source_evidence_ids=[evidence.evidence_id],
                ))
        if not claims and getattr(product, "claims", None):
            for index, claim in enumerate(product.claims):
                claim_text = claim.text.value.strip()
                claim_id = "claim_" + hashlib.sha256(
                    f"{product.canonical_product_id}:{index}".encode("utf-8")
                ).hexdigest()[:16]
                claims.append(PackClaim(
                    claim_id=claim_id,
                    text=claim_text,
                    status="source_claim",
                    source_evidence_ids=list(product.supporting_evidence_ids),
                ))
        return claims

    def _restrictions(self, media: list[PackMediaItem]) -> dict:
        policies = {item.usage_policy for item in media}
        return {
            "usage_policies": sorted(policies),
            "all_reference_only": bool(media) and policies == {"reference_only"},
            "advertising_license_confirmed": "licensed" in policies,
        }

    def _completeness(self, checks: dict, target_variant, media_count: int) -> tuple[PackStatus, str]:
        if checks.get("identity") == "needs_review" or checks.get("identity") == "rejected":
            return PackStatus.NEEDS_REVIEW if checks.get("identity") == "needs_review" else PackStatus.REJECTED, "identity ambiguous"
        if checks.get("variant") == "needs_review" and target_variant is None:
            return PackStatus.NEEDS_REVIEW, "variant selection required"
        if media_count < self.min_media:
            return PackStatus.INCOMPLETE, f"insufficient trusted media: {media_count} < {self.min_media}"
        # Variant-media safety: for a multi-variant product with a selected
        # variant, media variant evidence must be verified (or the product has
        # no meaningful variant distinction). Unknown variant media alone must
        # not claim verified status.
        if target_variant is not None and checks.get("media_variant", {}).get("verified", 0) < self.min_media:
            return PackStatus.NEEDS_REVIEW, "selected variant media not verified against chosen variant"
        return PackStatus.READY_TO_LOCK, ""


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _normalize(value: str) -> str:
    import re
    import unicodedata

    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")
