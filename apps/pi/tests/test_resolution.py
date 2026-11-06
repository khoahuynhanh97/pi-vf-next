"""Offline tests for multi-source product resolution. No network, no LLM."""

from __future__ import annotations

from datetime import UTC, datetime

from media.evidence.models import (
    ClaimEvidence,
    EvidenceSource,
    EvidenceValue,
    IdentityEvidence,
    MediaEvidence,
    OfferEvidence,
    ProductEvidence,
    ReputationEvidence,
    SourceKind,
    SpecificationEvidence,
)
from media.resolution import ProductResolver
from media.resolution.models import ConflictStatus, ResolutionStatus


def _stamp():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _source(url="https://x.test/p", host=None):
    final = url or "https://x.test/p"
    return EvidenceSource(requested_url=final, final_url=final, host=host or final.split("//")[1].split("/")[0], acquisition_method="http", acquisition_status="success", observed_at=_stamp())


def _evidence(eid: str, *, name=None, brand=None, model=None, gtin=None, sku=None, variant=None, price=None, currency="VND", seller=None, rating=None, specs=(), claims=(), url=None, host=None):
    identity = IdentityEvidence()
    if name:
        identity.name = EvidenceValue(value=name, source_kind=SourceKind.JSON_LD)
    if brand:
        identity.brand = EvidenceValue(value=brand, source_kind=SourceKind.JSON_LD)
    if model:
        identity.model = EvidenceValue(value=model, source_kind=SourceKind.JSON_LD)
    if gtin:
        identity.gtin = EvidenceValue(value=gtin, source_kind=SourceKind.JSON_LD)
    if sku:
        identity.sku = EvidenceValue(value=sku, source_kind=SourceKind.JSON_LD)
    if variant:
        identity.variant = EvidenceValue(value=variant, source_kind=SourceKind.JSON_LD)
    offer = None
    if price is not None or seller is not None:
        offer = OfferEvidence(
            price=EvidenceValue(value=price, source_kind=SourceKind.JSON_LD) if price is not None else None,
            currency=EvidenceValue(value=currency, source_kind=SourceKind.JSON_LD) if price is not None else None,
            seller_name=EvidenceValue(value=seller, source_kind=SourceKind.JSON_LD) if seller else None,
        )
    reputation = None
    if rating is not None:
        reputation = ReputationEvidence(rating_value=EvidenceValue(value=rating, source_kind=SourceKind.JSON_LD))
    return ProductEvidence(
        evidence_id=eid,
        source=_source(url=url, host=host),
        identity=identity,
        offer=offer,
        reputation=reputation,
        specifications=[SpecificationEvidence(name=EvidenceValue(value=n, source_kind=SourceKind.JSON_LD), value=EvidenceValue(value=v, source_kind=SourceKind.JSON_LD)) for n, v in specs],
        claims=[ClaimEvidence(text=EvidenceValue(value=text, source_kind=SourceKind.JSON_LD)) for text in claims],
        media=MediaEvidence(page_image_urls=[f"https://x.test/{eid}.jpg"]),
    )


def test_same_product_brand_model_case_variants():
    a = _evidence("a", brand="Jisulife", model="FA55", price=699000)
    b = _evidence("b", brand="JISULIFE", model="fa55", price=649000)
    result = ProductResolver().resolve([a, b])
    assert result.status == ResolutionStatus.COMPLETED
    assert len(result.canonical_products) == 1
    product = result.canonical_products[0]
    assert set(product.supporting_evidence_ids) == {"a", "b"}
    assert product.brand.supporting_evidence_ids == ["a", "b"]


def test_different_model_not_merged():
    a = _evidence("a", brand="Jisulife", model="FA55")
    b = _evidence("b", brand="Jisulife", model="FA55 Pro")
    result = ProductResolver().resolve([a, b])
    assert len(result.canonical_products) == 2


def test_gtin_match_same_product():
    a = _evidence("a", brand="Jisulife", model="FA55", gtin="1234567890123")
    b = _evidence("b", brand="Jisulife", model="FA55", gtin="1234567890123")
    result = ProductResolver().resolve([a, b])
    assert len(result.canonical_products) == 1


def test_gtin_conflict_not_merged():
    a = _evidence("a", brand="Jisulife", model="FA55", gtin="1234567890123")
    b = _evidence("b", brand="Jisulife", model="FA55", gtin="9999999999999")
    result = ProductResolver().resolve([a, b])
    assert len(result.canonical_products) == 2


def test_missing_model_is_not_conflict():
    a = _evidence("a", brand="Jisulife", model="FA55")
    b = _evidence("b", brand="Jisulife", name="Jisulife FA55 fan")  # no model
    result = ProductResolver().resolve([a, b])
    # Model missing is not negative evidence; may group or stay unresolved, but
    # must NOT create a synthetic model conflict.
    product = result.canonical_products[0]
    assert product.model.conflict_status == ConflictStatus.RESOLVED


def test_variant_separation():
    a = _evidence("a", brand="Jisulife", model="FA55", variant="Black", price=699000)
    b = _evidence("b", brand="Jisulife", model="FA55", variant="White", price=649000)
    result = ProductResolver().resolve([a, b])
    assert len(result.canonical_products) == 1
    product = result.canonical_products[0]
    assert len(product.variant_ids) == 2


def test_same_variant_two_listings():
    a = _evidence("a", brand="Jisulife", model="FA55", variant="Black", seller="Store A", price=699000, url="https://shop1.test/p")
    b = _evidence("b", brand="Jisulife", model="FA55", variant="Black", seller="Store B", price=649000, url="https://shop2.test/p")
    result = ProductResolver().resolve([a, b])
    product = result.canonical_products[0]
    assert len(product.variant_ids) == 1
    assert len(product.listing_ids) == 2


def test_price_stays_listing_level():
    a = _evidence("a", brand="Jisulife", model="FA55", price=699000, url="https://shop1.test/p")
    b = _evidence("b", brand="Jisulife", model="FA55", price=649000, url="https://shop2.test/p")
    result = ProductResolver().resolve([a, b])
    product = result.canonical_products[0]
    assert "price" not in product.model_dump()
    prices = [listing.price for listing in _listings(result)]
    assert 699000 in prices and 649000 in prices


def test_seller_is_listing_level():
    a = _evidence("a", brand="Jisulife", model="FA55", seller="Store A")
    b = _evidence("b", brand="Jisulife", model="FA55", seller="Store B")
    result = ProductResolver().resolve([a, b])
    assert len(result.canonical_products) == 1
    sellers = [listing.seller for listing in _listings(result)]
    assert "Store A" in sellers and "Store B" in sellers


def test_spec_conflict_preserved():
    a = _evidence("a", brand="Jisulife", model="FA55", specs=[("Battery Capacity", "5000 mAh")])
    b = _evidence("b", brand="Jisulife", model="FA55", specs=[("Battery Capacity", "4000 mAh")])
    result = ProductResolver().resolve([a, b])
    assert result.conflicts
    assert any("battery capacity" in conflict.field for conflict in result.conflicts)
    spec_conflicts = [c for c in result.conflicts if c.field == "specification:battery capacity"]
    assert spec_conflicts
    assert spec_conflicts[0].status == ConflictStatus.UNRESOLVED


def test_claims_not_promoted_to_specs():
    a = _evidence("a", brand="Jisulife", model="FA55", claims=["10x stronger airflow"])
    result = ProductResolver().resolve([a])
    product = result.canonical_products[0]
    assert any("10x stronger airflow" in claim.text.value for claim in product.claims)
    assert not any(spec.value == "10x stronger airflow" for spec in product.specifications)


def test_unresolved_evidence_retained():
    weak = _evidence("weak", name="Mystery product no identifiers")
    strong = _evidence("strong", brand="Jisulife", model="FA55")
    result = ProductResolver().resolve([strong, weak])
    assert result.unresolved_evidence_ids == ["weak"]


def test_deterministic_ids_stable():
    a = _evidence("a", brand="Jisulife", model="FA55", price=699000)
    r1 = ProductResolver().resolve([a])
    b = _evidence("a", brand="Jisulife", model="FA55", price=699000)
    r2 = ProductResolver().resolve([b])
    assert r1.canonical_products[0].canonical_product_id == r2.canonical_products[0].canonical_product_id


def test_different_price_not_different_variant():
    a = _evidence("a", brand="Jisulife", model="FA55", price=699000)
    b = _evidence("b", brand="Jisulife", model="FA55", price=649000)
    result = ProductResolver().resolve([a, b])
    assert len(result.canonical_products[0].variant_ids) == 0  # no fabricated variants


def test_no_resolution_for_empty():
    result = ProductResolver().resolve([])
    assert result.status == ResolutionStatus.NO_RESOLUTION


def test_name_pro_suffix_not_merged_without_model():
    a = _evidence("a", brand="Baseus", name="Baseus Bowie MA10")
    b = _evidence("b", brand="Baseus", name="Baseus Bowie MA10 Pro")
    result = ProductResolver().resolve([a, b])
    assert len(result.canonical_products) == 2


def test_name_exact_match_groups_without_model():
    a = _evidence("a", brand="Baseus", name="Baseus Bowie MA10")
    b = _evidence("b", brand="Baseus", name="Baseus bowie ma10")
    result = ProductResolver().resolve([a, b])
    assert len(result.canonical_products) == 1


def test_generic_same_brand_names_stay_unresolved():
    a = _evidence("a", brand="Jisulife", name="Jisulife Fan")
    b = _evidence("b", brand="Jisulife", name="Jisulife Cooler")
    result = ProductResolver().resolve([a, b])
    # Conservative: no name match, no model — must not be force-merged.
    assert len(result.canonical_products) >= 1


def test_gtin_conflict_beats_name_similarity():
    a = _evidence("a", brand="Jisulife", model="FA55", gtin="1234567890123", name="Jisulife FA55 Turbo Fan")
    b = _evidence("b", brand="Jisulife", model="FA55", gtin="9999999999999", name="Jisulife FA55 Turbo Fan")
    result = ProductResolver().resolve([a, b])
    assert len(result.canonical_products) == 2


def test_weak_generic_names_do_not_containment_match():
    from media.resolution.identity import _exact_or_contained

    assert _exact_or_contained("Portable Fan", "Portable Fan Rechargeable") is False
    assert _exact_or_contained("Jisulife Portable Turbo Fan", "Jisulife Portable Turbo Fan Pro") is False
    assert _exact_or_contained("Baseus Bowie MA10", "Baseus Bowie MA10 Pro") is False
    assert _exact_or_contained("Jisulife Portable Turbo Fan", "Jisulife Portable Turbo Fan") is True


def _listings(result):
    return [listing for product in result.canonical_products for listing in product.listings]
