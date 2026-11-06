"""ProductEvidenceExtractor: deterministic extraction with field provenance."""

from __future__ import annotations

import re
import uuid
from urllib.parse import urlsplit

from media.web_acquisition.models import ExtractedPageContent, SourceObservation

from . import json_ld
from .models import (
    ClaimEvidence,
    EvidenceConflict,
    EvidenceExtractionStatus,
    EvidenceSource,
    EvidenceValue,
    IdentityEvidence,
    MediaEvidence,
    OfferEvidence,
    ProductEvidence,
    ProductEvidenceExtractionResult,
    ReputationEvidence,
    SourceKind,
    SpecificationEvidence,
)
from .normalizers import normalize_whitespace, parse_currency, parse_price

CLAIM_MARKERS = (
    "turbo", "ultra", "extreme", "pro", "max", "best", "fastest", "powerful",
    "premium", "premium quality", "long-lasting", "durable", "quiet",
    "10x", "super", "hi-end", "cao cấp", "siêu", "nhanh gấp", "bền bỉ",
    "chất lượng cao", "vượt trội", "tốt nhất",
)

VISIBLE_FACT_CUE = re.compile(
    r"\b(?:illumination|brightness|adjustment|folds?|colour temperature|"
    r"color temperature|operating temperature|rated input|cri)\b|\bRa\d+\b",
    re.IGNORECASE,
)


class ProductEvidenceExtractor:
    """Extracts what one page says about a product, without resolving truth."""

    def extract(self, observation: SourceObservation, content: ExtractedPageContent | None) -> ProductEvidenceExtractionResult:
        warnings: list[str] = []
        conflicts: list[EvidenceConflict] = []
        content = content or ExtractedPageContent()

        source = self._evidence_source(observation, content)

        nodes = json_ld.flatten_json_ld(content.json_ld)
        products = json_ld.find_products(nodes)

        identity = IdentityEvidence()
        offer = OfferEvidence()
        reputation = ReputationEvidence()
        descriptions: list[EvidenceValue] = []
        specifications: list[SpecificationEvidence] = []
        page_images: list[str] = []

        if products:
            product = products[0]
            path = "Product"
            self._map_identity(product, path, identity)
            self._map_offer(product, path, offer, warnings)
            self._map_reputation(product, path, reputation)
            desc = json_ld._text(product, "description", "shortDescription")
            if desc:
                descriptions.append(EvidenceValue(value=desc, source_kind=SourceKind.JSON_LD, source_path=f"{path}.description"))
            specifications.extend(json_ld.extract_specifications(product, path))
            page_images.extend(json_ld._image_urls(product))

        self._map_metadata(content, identity, descriptions, offer, warnings, conflicts)

        visible_text = content.visible_text or ""
        if visible_text:
            self._map_visible_text(visible_text, identity, offer, descriptions, conflicts)

        claims = self._extract_claims(descriptions, visible_text)
        media = MediaEvidence(
            page_image_urls=list(dict.fromkeys(page_images)),
            og_image_url=content.og.get("image"),
        )

        identity_values = [identity.name, identity.brand, identity.model, identity.sku, identity.gtin]
        structured_identity = any(
            value is not None and value.source_kind in (SourceKind.JSON_LD, SourceKind.OPEN_GRAPH, SourceKind.META)
            for value in identity_values
        )
        if not structured_identity and not offer.price and not page_images and not descriptions:
            return ProductEvidenceExtractionResult(
                status=EvidenceExtractionStatus.NO_PRODUCT_EVIDENCE,
                source=source,
                warnings=warnings,
                conflicts=conflicts,
            )

        evidence = ProductEvidence(
            evidence_id=f"ev_{uuid.uuid4().hex[:12]}",
            source=source,
            identity=identity,
            offer=offer if (offer.price or offer.currency or offer.seller_name) else None,
            reputation=reputation if (reputation.rating_value or reputation.rating_count or reputation.review_count) else None,
            description=descriptions[0] if descriptions else None,
            specifications=specifications,
            claims=claims,
            media=media,
        )
        status = EvidenceExtractionStatus.COMPLETED if (identity.name or identity.brand or offer.price) else EvidenceExtractionStatus.PARTIAL
        return ProductEvidenceExtractionResult(status=status, source=source, evidence=evidence, warnings=warnings, conflicts=conflicts)

    def _evidence_source(self, observation: SourceObservation, content: ExtractedPageContent) -> EvidenceSource:
        return EvidenceSource(
            requested_url=observation.requested_url,
            final_url=observation.final_url,
            canonical_url=content.canonical_url,
            host=observation.domain or _host(observation.final_url or observation.requested_url),
            acquisition_method=observation.method,
            acquisition_status=observation.status.value,
            observed_at=observation.observed_at,
            page_title=observation.page_title or content.title,
        )

    @staticmethod
    def _record_conflict(conflicts: list[EvidenceConflict], field: str, preferred: EvidenceValue, alternate: EvidenceValue) -> None:
        existing = next((item for item in conflicts if item.field == field), None)
        if existing is None:
            existing = EvidenceConflict(field=field, values=[preferred])
            conflicts.append(existing)
        existing.values.append(alternate)

    def _map_identity(self, product: dict, path: str, identity: IdentityEvidence) -> None:
        name = json_ld._text(product, "name", "title")
        if name:
            identity.name = EvidenceValue(value=name, source_kind=SourceKind.JSON_LD, source_path=f"{path}.name")
        brand_node = product.get("brand")
        if isinstance(brand_node, dict):
            brand = json_ld._string_value(brand_node.get("name"))
            brand_path = f"{path}.brand.name"
        elif isinstance(brand_node, str):
            brand = brand_node
            brand_path = f"{path}.brand"
        else:
            brand, brand_path = None, None
        if brand:
            identity.brand = EvidenceValue(value=brand, source_kind=SourceKind.JSON_LD, source_path=brand_path)
        model = json_ld._text(product, "model")
        if model:
            identity.model = EvidenceValue(value=model, source_kind=SourceKind.JSON_LD, source_path=f"{path}.model")
        sku = json_ld._text(product, "sku")
        mpn = json_ld._text(product, "mpn")
        if sku:
            identity.sku = EvidenceValue(value=sku, source_kind=SourceKind.JSON_LD, source_path=f"{path}.sku")
        elif mpn:
            identity.mpn = EvidenceValue(value=mpn, source_kind=SourceKind.JSON_LD, source_path=f"{path}.mpn")
        for gtin_key in ("gtin", "gtin13", "gtin14", "gtin8", "gtin12"):
            if product.get(gtin_key):
                identity.gtin = EvidenceValue(value=str(product[gtin_key]), source_kind=SourceKind.JSON_LD, source_path=f"{path}.{gtin_key}")
                break
        variant = json_ld._text(product, "variant", "color")
        if variant:
            identity.variant = EvidenceValue(value=variant, source_kind=SourceKind.JSON_LD, source_path=f"{path}.variant" if "variant" in product else f"{path}.color")
        category = json_ld._text(product, "category")
        if category:
            identity.category = EvidenceValue(value=category, source_kind=SourceKind.JSON_LD, source_path=f"{path}.category")

    def _map_offer(self, product: dict, path: str, offer: OfferEvidence, warnings: list[str]) -> None:
        raw_offer = product.get("offers")
        if raw_offer is None:
            return
        offers = raw_offer if isinstance(raw_offer, list) else [raw_offer]
        for index, item in enumerate(offers):
            if not isinstance(item, dict):
                continue
            values, _, offer_warnings = json_ld.extract_offer(item, f"{path}.offers[{index}]")
            warnings.extend(offer_warnings)
            for key, value in values.items():
                setattr(offer, key, value)
            if offer.price is not None:
                break

    def _map_reputation(self, product: dict, path: str, reputation: ReputationEvidence) -> None:
        rating = product.get("aggregateRating")
        if isinstance(rating, dict):
            values, _ = json_ld.extract_reputation(rating, f"{path}.aggregateRating")
            for key, value in values.items():
                setattr(reputation, key, value)

    def _map_metadata(self, content, identity, descriptions, offer, warnings, conflicts) -> None:
        og_title = content.og.get("title")
        if og_title:
            value = EvidenceValue(value=str(og_title), source_kind=SourceKind.OPEN_GRAPH, source_path="og:title")
            if identity.name and identity.name.value != value.value:
                self._record_conflict(conflicts, "name", identity.name, value)
                warnings.append("CONFLICTING_NAME_EVIDENCE")
            elif not identity.name:
                identity.name = value
        og_desc = content.og.get("description")
        if og_desc:
            descriptions.append(EvidenceValue(value=str(og_desc), source_kind=SourceKind.OPEN_GRAPH, source_path="og:description"))
        price_amount = content.og.get("product:price:amount")
        if price_amount:
            amount, currency, raw = parse_price(str(price_amount))
            if amount is not None:
                value = EvidenceValue(value=amount, source_kind=SourceKind.OPEN_GRAPH, source_path="og:product:price:amount", raw_value=raw)
                if offer.price and offer.price.value != value.value:
                    self._record_conflict(conflicts, "price", offer.price, value)
                    warnings.append("CONFLICTING_PRICE_EVIDENCE")
                elif not offer.price:
                    offer.price = value
                    if not offer.currency and currency:
                        offer.currency = EvidenceValue(value=currency, source_kind=SourceKind.OPEN_GRAPH, source_path="og:product:price:currency")
            elif currency is None and parse_currency(str(price_amount)) is None:
                warnings.append("AMBIGUOUS_OG_PRICE")
        price_currency = content.og.get("product:price:currency")
        if price_currency and not offer.currency:
            offer.currency = EvidenceValue(value=str(price_currency).upper(), source_kind=SourceKind.OPEN_GRAPH, source_path="og:product:price:currency")
        if content.meta_description and not descriptions:
            descriptions.append(EvidenceValue(value=content.meta_description, source_kind=SourceKind.META, source_path="meta:description"))
        if content.title and not identity.name:
            identity.name = EvidenceValue(value=content.title, source_kind=SourceKind.PAGE_TITLE, source_path="title")

    def _map_visible_text(self, text, identity, offer, descriptions, conflicts) -> None:
        if not identity.name:
            first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
            if 3 <= len(first_line) <= 200:
                identity.name = EvidenceValue(value=first_line, source_kind=SourceKind.VISIBLE_TEXT, source_path="visible_text.first_line")
        if not offer.price:
            match = re.search(r"(?:giá|price|₫|đ)\s*[:：]?\s*([\d.,\s]+(?:₫|đ|vnd)?)", text.casefold())
            if match:
                amount, currency, raw = parse_price(match.group(1))
                if amount is not None:
                    value = EvidenceValue(value=amount, source_kind=SourceKind.VISIBLE_TEXT, source_path="visible_text.price", raw_value=raw)
                    if offer.price and offer.price.value != value.value:
                        self._record_conflict(conflicts, "price", offer.price, value)
                    offer.price = value
                    if not offer.currency:
                        offer.currency = EvidenceValue(value=currency or "VND", source_kind=SourceKind.VISIBLE_TEXT, source_path="visible_text.price")

    def _extract_claims(self, descriptions: list[EvidenceValue], visible_text: str = "") -> list[ClaimEvidence]:
        claims: list[ClaimEvidence] = []
        for description in descriptions:
            for sentence in re.split(r"[.!?\n]", description.value):
                sentence = normalize_whitespace(sentence)
                if len(sentence) < 8:
                    continue
                # Search snippets advertising prices/reviews are not product
                # facts even when a feature phrase appears later in the text.
                if "best price" in sentence.casefold() or "reviews" in sentence.casefold():
                    continue
                if any(marker in sentence.casefold() for marker in CLAIM_MARKERS):
                    claims.append(ClaimEvidence(text=EvidenceValue(value=sentence, source_kind=description.source_kind, source_path=description.source_path)))
        for index, fragment in enumerate(visible_text.splitlines(), start=1):
            fact = normalize_whitespace(fragment)
            if not 12 <= len(fact) <= 150 or not VISIBLE_FACT_CUE.search(fact):
                continue
            if any(marker in fact.casefold() for marker in ("best price", "reviews", "shop now", "buy now")):
                continue
            if any(claim.text.value.casefold() == fact.casefold() for claim in claims):
                continue
            claims.append(ClaimEvidence(text=EvidenceValue(
                value=fact,
                source_kind=SourceKind.VISIBLE_TEXT,
                source_path=f"visible_text.fragment[{index}]",
            )))
        return claims[:10]


def _host(url: str) -> str | None:
    return urlsplit(url).netloc.casefold().removeprefix("www.") or None
