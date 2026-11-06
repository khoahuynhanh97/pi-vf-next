"""ProductResolver: multi-source evidence → Canonical Product / Variant / Listing.

Deterministic and conservative. False merges are worse than under-merging.
"""

from __future__ import annotations

import re

from media.evidence.models import ProductEvidence

from .identity import EvidenceIdentityKey, match_identity
from .ids import (
    canonical_product_id,
    host_of,
    listing_id,
    variant_dimensions,
    variant_id,
)
from .models import (
    CanonicalProduct,
    ConflictStatus,
    ProductListing,
    ProductResolutionResult,
    ProductVariant,
    ResolutionConflict,
    ResolutionDecision,
    ResolutionStatus,
    ResolvedField,
)

VARIANT_DIMENSION_KEYS = ("color", "size", "capacity", "memory", "configuration", "region", "material", "edition", "variant")


class _Group:
    def __init__(self):
        self.evidence: list[ProductEvidence] = []
        self.key: EvidenceIdentityKey | None = None
        self.product_id: str | None = None


class ProductResolver:
    def resolve(self, evidence: list[ProductEvidence]) -> ProductResolutionResult:
        decisions: list[ResolutionDecision] = []
        warnings: list[str] = []

        groups, unresolved, conflicts = self._group_evidence(evidence, decisions, warnings)

        products: list[CanonicalProduct] = []
        for group in groups:
            products.append(self._build_product(group, decisions, conflicts))

        status = self._overall_status(groups, unresolved)
        return ProductResolutionResult(
            status=status,
            canonical_products=products,
            unresolved_evidence_ids=[item.evidence_id for item in unresolved],
            conflicts=conflicts,
            warnings=warnings,
            decisions=decisions,
        )

    # --- grouping -----------------------------------------------------------

    def _group_evidence(self, evidence, decisions, warnings):
        groups: list[_Group] = []
        unresolved: list[ProductEvidence] = []
        conflicts: list[ResolutionConflict] = []

        for item in evidence:
            key = EvidenceIdentityKey.from_evidence(item)
            if not key.has_model and not key.has_gtin and not key.brand:
                unresolved.append(item)
                decisions.append(ResolutionDecision("IDENTITY_UNRESOLVED", "identity", reason="no brand, model, or strong identifier"))
                continue
            assigned = False
            for group in groups:
                if group.key is None:
                    continue
                result = match_identity(item, group.evidence[0])
                if result.status.value.startswith("same_product") or result.status.value.startswith("likely_same"):
                    group.evidence.append(item)
                    group.key = self._merged_key(group.key, key)
                    decisions.append(ResolutionDecision("IDENTITY_GROUPED", "identity", evidence_ids=[item.evidence_id], reason=result.reason))
                    assigned = True
                    break
                if result.status == "different_product":
                    continue
            if not assigned:
                if groups and not key.has_model and not key.has_gtin and key.brand and self._brand_only_compatible(key, groups):
                    # Brand-only evidence: only group if it does not conflict
                    # with any existing group and no explicit model exists.
                    group = _Group()
                    group.evidence = [item]
                    group.key = key
                    groups.append(group)
                    decisions.append(ResolutionDecision("IDENTITY_GROUPED", "identity", evidence_ids=[item.evidence_id], reason="brand-only evidence in isolated group"))
                    continue
                new_group = _Group()
                new_group.evidence = [item]
                new_group.key = key
                groups.append(new_group)
                decisions.append(ResolutionDecision("IDENTITY_GROUPED", "identity", evidence_ids=[item.evidence_id], reason="new identity group"))

        for group in groups:
            keys = [EvidenceIdentityKey.from_evidence(item) for item in group.evidence]
            explicit_models = {key.model for key in keys if key.has_model}
            if len(explicit_models) > 1:
                conflicts.append(ResolutionConflict(
                    field="model",
                    candidate_values=sorted(explicit_models),
                    evidence_ids=[item.evidence_id for item in group.evidence],
                    severity="error",
                    status=ConflictStatus.REQUIRES_REVIEW,
                ))
                warnings.append("MODEL_CONFLICT_WITHIN_GROUP")
        return groups, unresolved, conflicts

    def _merged_key(self, *keys) -> EvidenceIdentityKey:
        brand = next((key.brand for key in keys if key.brand), "")
        model = next((key.model for key in keys if key.model), "")
        gtin = next((key.gtin for key in keys if key.gtin), "")
        return EvidenceIdentityKey(brand=brand, model=model, gtin=gtin)

    def _brand_only_compatible(self, key, groups) -> bool:
        for group in groups:
            if group.key and group.key.brand and group.key.brand != key.brand:
                return False
        return True

    # --- product / variant / listing ----------------------------------------

    def _build_product(self, group: _Group, decisions, conflicts) -> CanonicalProduct:
        evidence = group.evidence
        key = self._merged_key(*(EvidenceIdentityKey.from_evidence(item) for item in evidence))
        product_id = self._product_id_for_group(key, evidence)
        for item in evidence:
            item_id = canonical_product_id(item)
            if item_id and not product_id:
                product_id = item_id
        product_id = product_id or _fallback_group_id(key, evidence)

        name = self._resolve_text(evidence, "name", "Product name", decisions)
        brand = self._resolve_text(evidence, "brand", "Product brand", decisions)
        model = self._resolve_text(evidence, "model", "Product model", decisions)
        category = self._resolve_text(evidence, "category", "Product category", decisions)
        description = self._resolve_description(evidence, decisions)
        specifications, spec_conflicts = self._resolve_specifications(evidence, decisions)
        conflicts.extend(spec_conflicts)

        claims = [claim for item in evidence for claim in item.claims]

        # Variants
        variant_groups: dict[str, list[ProductEvidence]] = {}
        for item in evidence:
            dims = variant_dimensions(item)
            if dims:
                vid = variant_id(product_id, dims)
                if vid:
                    variant_groups.setdefault(vid, []).append(item)
        variants = []
        for vid, items in variant_groups.items():
            variants.append(ProductVariant(
                variant_id=vid,
                canonical_product_id=product_id,
                dimensions=variant_dimensions(items[0]),
                supporting_evidence_ids=[item.evidence_id for item in items],
            ))

        # Listings: one per evidence item (each evidence = one source listing).
        listings = [self._build_listing(item, product_id, variants, decisions) for item in evidence]

        supporting = [item.evidence_id for item in evidence]
        product = CanonicalProduct(
            canonical_product_id=product_id,
            name=name,
            brand=brand,
            model=model,
            category=category,
            description=description,
            specifications=specifications,
            specification_evidence=[spec for item in evidence for spec in item.specifications],
            claims=claims,
            media_refs=sorted({url for item in evidence for url in item.media.page_image_urls if url}),
            variants=variants,
            listings=listings,
            variant_ids=[variant.variant_id for variant in variants],
            listing_ids=[listing.listing_id for listing in listings],
            supporting_evidence_ids=supporting,
        )
        return product

    def _product_id_for_group(self, key: EvidenceIdentityKey, evidence) -> str | None:
        if key.has_model:
            return _digest(f"{key.brand}|{key.model}|{key.gtin}", "product")
        if key.has_gtin:
            return _digest(f"gtin:{key.gtin}", "product")
        return None

    def _build_listing(self, item: ProductEvidence, product_id: str, variants: list[ProductVariant], decisions) -> ProductListing:
        offer = item.offer
        reputation = item.reputation
        listing_variant_id = None
        dims = variant_dimensions(item)
        if dims:
            listing_variant_id = variant_id(product_id, dims)
        listing = ProductListing(
            listing_id=listing_id(item),
            canonical_product_id=product_id,
            variant_id=listing_variant_id,
            source_url=item.source.canonical_url or item.source.final_url or item.source.requested_url or "",
            host=item.source.host or host_of(item.source.final_url or item.source.requested_url),
            seller=offer.seller_name.value if offer and offer.seller_name else None,
            price=offer.price.value if offer and offer.price is not None else None,
            currency=offer.currency.value if offer and offer.currency else None,
            availability=offer.availability.value if offer and offer.availability else None,
            rating=reputation.rating_value.value if reputation and reputation.rating_value is not None else None,
            review_count=reputation.review_count.value if reputation and reputation.review_count is not None else None,
            sold_count=reputation.sold_count.value if reputation and reputation.sold_count is not None else None,
            source_evidence_ids=[item.evidence_id],
            observed_at=item.source.observed_at,
            low_price=offer.low_price.value if offer and offer.low_price is not None else None,
            high_price=offer.high_price.value if offer and offer.high_price is not None else None,
            offer_count=offer.offer_count.value if offer and offer.offer_count is not None else None,
        )
        decisions.append(ResolutionDecision("LISTING_CREATED", "listing", selected_value=listing.listing_id, evidence_ids=[item.evidence_id]))
        return listing

    # --- field resolution ----------------------------------------------------

    def _resolve_text(self, evidence: list[ProductEvidence], field: str, label: str, decisions) -> ResolvedField | None:
        values = []
        for item in evidence:
            identity = item.identity
            value = getattr(identity, field, None)
            if value is None or not value.value:
                continue
            values.append((str(value.value).strip(), item.evidence_id))
        if not values:
            return None
        unique: list[tuple[str, str]] = []
        seen: set[str] = set()
        for raw, eid in values:
            key = _normalize_key(raw)
            if key in seen:
                continue
            seen.add(key)
            unique.append((raw, eid))
        if not unique:
            return None
        # Deterministic: prefer JSON-LD/OG-sourced evidence, else repeated value,
        # else first. Conflicts are surfaced, never silently dropped.
        selected, source_ids = self._select(evidence, field, unique)
        status = ConflictStatus.RESOLVED if len(unique) == 1 else ConflictStatus.REQUIRES_REVIEW
        decisions.append(ResolutionDecision(
            decision_type="FIELD_RESOLVED" if status == ConflictStatus.RESOLVED else "FIELD_CONFLICT",
            field=label,
            selected_value=selected,
            evidence_ids=source_ids,
            reason=f"{len(unique)} distinct value(s) for {label}",
        ))
        return ResolvedField(value=selected, supporting_evidence_ids=source_ids, source_values=[raw for raw, _ in unique], decision=label, conflict_status=status)

    def _select(self, evidence, field, unique):
        # Prefer a value whose raw form appears in JSON-LD/OG-sourced evidence.
        for item in evidence:
            identity = item.identity
            value = getattr(identity, field, None)
            if value and value.value and value.source_kind.value in ("json_ld", "open_graph"):
                raw = str(value.value).strip()
                for candidate, eid in unique:
                    if candidate == raw:
                        return raw, self._supporting(evidence, field, raw)
        # repeat-compatible: pick the value appearing most often
        from collections import Counter

        counts = Counter(_normalize_key(raw) for raw, _ in unique)
        best_key = counts.most_common(1)[0][0]
        for raw, eid in unique:
            if _normalize_key(raw) == best_key:
                return raw, self._supporting(evidence, field, raw)
        return unique[0][0], [unique[0][1]]

    def _supporting(self, evidence, field, selected_raw):
        ids = []
        selected_key = _normalize_key(selected_raw)
        for item in evidence:
            identity = item.identity
            value = getattr(identity, field, None)
            if value and value.value and _normalize_key(str(value.value)) == selected_key:
                ids.append(item.evidence_id)
        return ids or []

    def _resolve_description(self, evidence, decisions) -> ResolvedField | None:
        candidates = []
        for item in evidence:
            if item.description and item.description.value:
                candidates.append((str(item.description.value).strip(), item.evidence_id))
        if not candidates:
            return None
        best = max(candidates, key=lambda pair: len(pair[0]))
        decisions.append(ResolutionDecision("DESCRIPTION_SELECTED", "description", selected_value=best[0][:80], evidence_ids=[best[1]], reason="longest structured description"))
        return ResolvedField(value=best[0], supporting_evidence_ids=[best[1]], source_values=[raw for raw, _ in candidates], decision="description", conflict_status=ConflictStatus.RESOLVED)

    def _resolve_specifications(self, evidence, decisions):
        resolved: dict[str, list] = {}
        for item in evidence:
            for spec in item.specifications:
                name = _normalize_key(spec.name.value)
                value = str(spec.value.value).strip()
                resolved.setdefault(name, []).append((value, item.evidence_id))
        fields: list[ResolvedField] = []
        conflicts: list[ResolutionConflict] = []
        for name, values in resolved.items():
            unique = list(dict.fromkeys(values))
            if len({value for value, _ in unique}) > 1:
                conflicts.append(ResolutionConflict(
                    field=f"specification:{name}",
                    candidate_values=[value for value, _ in unique],
                    evidence_ids=[eid for _, eid in unique],
                    severity="warning",
                    status=ConflictStatus.UNRESOLVED,
                ))
                decisions.append(ResolutionDecision("SPEC_CONFLICT", f"specification:{name}", reason=f"{len(unique)} distinct values"))
            fields.append(ResolvedField(
                value=unique[0][0],
                supporting_evidence_ids=[eid for _, eid in unique],
                source_values=[value for value, _ in unique],
                decision="specification",
                conflict_status=ConflictStatus.REQUIRES_REVIEW if len(unique) > 1 else ConflictStatus.RESOLVED,
            ))
        return fields, conflicts

    # --- status ---------------------------------------------------------------

    def _overall_status(self, groups, unresolved) -> ResolutionStatus:
        if not groups and not unresolved:
            return ResolutionStatus.NO_RESOLUTION
        if not groups and unresolved:
            return ResolutionStatus.NO_RESOLUTION
        if unresolved:
            return ResolutionStatus.PARTIAL
        return ResolutionStatus.COMPLETED


def _normalize_key(value: str) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _digest(value: str, prefix: str, length: int = 12) -> str:
    import hashlib

    return f"{prefix}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:length]}"


def _fallback_group_id(key: EvidenceIdentityKey, evidence) -> str:
    import hashlib

    names = [normalize_identity(item.identity.name.value) for item in evidence if item.identity.name]
    payload = "|".join(sorted(n for n in names if n))
    return f"product_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:12]}"


def normalize_identity(value: str) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()
