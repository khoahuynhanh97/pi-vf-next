"""Offline tests for Product Evidence Extraction & Normalization. No network."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from media.evidence import ProductEvidenceExtractor
from media.evidence.models import (
    Availability,
    EvidenceExtractionStatus,
    SourceKind,
)
from media.evidence.normalizers import (
    parse_count,
    parse_currency,
    parse_price,
    parse_rating,
    split_unit,
)
from media.web_acquisition.models import ExtractedPageContent, SourceObservation


def _stamp():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _observation(url="https://x.test/p", method="http", status="success", title=None):
    return SourceObservation(
        requested_url=url,
        method=method,
        status=status,
        final_url=url,
        domain="x.test",
        http_status=200,
        page_title=title,
        observed_at=_stamp(),
    )


def _content(**changes):
    values = {"title": None, "meta_description": None, "canonical_url": None, "og": {}, "json_ld": [], "visible_text": None, "image_urls": []}
    values.update(changes)
    return ExtractedPageContent(**values)


def test_visible_product_facts_keep_source_fragment_provenance():
    content = _content(
        json_ld=[{"@type": "Product", "name": "Xiaomi Desk Lamp Lite"}],
        og={"description": "Get the key specs, reviews, best price of Xiaomi Desk Lamp Lite: Ra90 high CRI"},
        visible_text=(
            "Overview\nWide-area illumination\nRa90 high CRI\n"
            "Three-level brightness adjustment\nShop now"
        ),
    )

    result = ProductEvidenceExtractor().extract(_observation(), content)

    claims = result.evidence.claims
    assert [claim.text.value for claim in claims] == [
        "Wide-area illumination", "Ra90 high CRI", "Three-level brightness adjustment",
    ]
    assert all(claim.text.source_kind == SourceKind.VISIBLE_TEXT for claim in claims)
    assert [claim.text.source_path for claim in claims] == [
        "visible_text.fragment[2]", "visible_text.fragment[3]", "visible_text.fragment[4]",
    ]


# --- Normalizers ---


@pytest.mark.parametrize(
    "raw,amount,currency",
    [
        ("499000", 499000, None),
        ("499.000", None, None),
        ("499.000 ₫", 499000, "VND"),
        ("₫499.000", 499000, "VND"),
        ("499,000 đ", 499000, "VND"),
        ("499000 VND", 499000, "VND"),
        ("1.299.000₫", 1299000, "VND"),
        ("$499.00", None, None),  # $ ambiguous without currency context
        ("USD 49.99", 49.99, "USD"),
        ("n/a", None, None),
    ],
)
def test_parse_price(raw, amount, currency):
    assert parse_price(raw) == (amount, currency, raw)


def test_parse_currency_symbols():
    assert parse_currency("499.000 ₫") == "VND"
    assert parse_currency("499000 VND") == "VND"
    assert parse_currency("¥1200") == "JPY"
    assert parse_currency("$5") is None  # ambiguous


def test_parse_rating():
    assert parse_rating("4.8") == 4.8
    assert parse_rating("4,8/5") == 4.8
    assert parse_rating("Rất tốt") is None


def test_parse_count():
    assert parse_count("1.2k") == (1200, "1.2k")
    assert parse_count("1,2k sold") == (1200, "1,2k sold")
    assert parse_count("10K+") == (10000, "10K+")
    assert parse_count("3200") == (3200, "3200")
    assert parse_count("1.200")[0] is None  # ambiguous thousands vs decimal


def test_split_unit():
    assert split_unit("5000 mAh") == ("5000", "mAh", "5000 mAh")
    assert split_unit("USB-C") == (None, None, "USB-C")


# --- JSON-LD Product ---


def test_json_ld_product_with_provenance():
    content = _content(
        json_ld=[{
            "@type": "Product",
            "name": "Jisulife FA55",
            "brand": {"@type": "Brand", "name": "Jisulife"},
            "model": "FA55",
            "sku": "FA55-BLK",
            "mpn": "MPN-FA55",
            "gtin13": "1234567890123",
            "description": "Turbo cooling handheld fan with long-lasting battery.",
            "image": ["https://x.test/a.jpg", "https://x.test/b.jpg"],
        }]
    )
    result = ProductEvidenceExtractor().extract(_observation(), content)
    assert result.status == EvidenceExtractionStatus.COMPLETED
    ev = result.evidence
    assert ev.identity.name.value == "Jisulife FA55"
    assert ev.identity.name.source_kind == SourceKind.JSON_LD
    assert ev.identity.name.source_path == "Product.name"
    assert ev.identity.brand.value == "Jisulife"
    assert ev.identity.brand.source_path == "Product.brand.name"
    assert ev.identity.model.value == "FA55"
    assert ev.identity.sku.value == "FA55-BLK"
    assert ev.identity.gtin.value == "1234567890123"
    assert ev.description.source_kind == SourceKind.JSON_LD
    assert "https://x.test/a.jpg" in ev.media.page_image_urls


def test_json_ld_missing_fields_still_succeeds():
    content = _content(json_ld=[{"@type": "Product", "name": "Only Name"}])
    result = ProductEvidenceExtractor().extract(_observation(), content)
    assert result.status == EvidenceExtractionStatus.COMPLETED
    assert result.evidence.identity.brand is None
    assert result.evidence.identity.model is None
    assert result.evidence.offer is None


# --- Offer ---


def test_json_ld_offer():
    content = _content(json_ld=[{
        "@type": "Product",
        "name": "Fan",
        "offers": {
            "@type": "Offer",
            "price": "499000",
            "priceCurrency": "VND",
            "availability": "https://schema.org/InStock",
            "url": "https://x.test/buy",
            "seller": {"@type": "Organization", "name": "Shop X"},
        },
    }])
    result = ProductEvidenceExtractor().extract(_observation(), content)
    offer = result.evidence.offer
    assert offer.price.value == 499000
    assert offer.currency.value == "VND"
    assert offer.availability.value == Availability.IN_STOCK
    assert offer.url.value == "https://x.test/buy"
    assert offer.seller_name.value == "Shop X"


def test_aggregate_offer_preserves_range_without_single_price():
    content = _content(json_ld=[{
        "@type": "Product",
        "name": "Bundle",
        "offers": {"@type": "AggregateOffer", "lowPrice": "100000", "highPrice": "200000", "offerCount": 3, "priceCurrency": "VND"},
    }])
    result = ProductEvidenceExtractor().extract(_observation(), content)
    offer = result.evidence.offer
    assert offer.price is None
    assert offer.low_price.value == 100000
    assert offer.high_price.value == 200000
    assert offer.offer_count.value == 3


def test_aggregate_rating():
    content = _content(json_ld=[{
        "@type": "Product",
        "name": "Fan",
        "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.8", "ratingCount": "1200", "reviewCount": "300"},
    }])
    result = ProductEvidenceExtractor().extract(_observation(), content)
    reputation = result.evidence.reputation
    assert reputation.rating_value.value == 4.8
    assert reputation.rating_count.value == 1200
    assert reputation.review_count.value == 300


# --- @graph ---


def test_json_ld_graph_finds_product():
    content = _content(json_ld=[{
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "BreadcrumbList", "itemListElement": []},
            {"@type": "Product", "name": "Graph Product", "offers": {"price": "99000", "priceCurrency": "VND"}},
        ],
    }])
    result = ProductEvidenceExtractor().extract(_observation(), content)
    assert result.status == EvidenceExtractionStatus.COMPLETED
    assert result.evidence.identity.name.value == "Graph Product"
    assert result.evidence.offer.price.value == 99000


# --- Malformed JSON-LD isolation ---


def test_malformed_json_ld_is_isolated():
    content = _content(
        json_ld=[{"@type": "Product", "name": "Good Product"}],
        og={"title": "OG Fallback", "image": "https://x.test/og.jpg"},
    )
    # Simulate one malformed block alongside valid metadata.
    content.json_ld.append("not-a-dict")
    content.json_ld.append(42)
    result = ProductEvidenceExtractor().extract(_observation(), content)
    assert result.status == EvidenceExtractionStatus.COMPLETED
    assert result.evidence.identity.name.value == "Good Product"
    assert result.evidence.media.og_image_url == "https://x.test/og.jpg"


# --- Same-page conflict ---


def test_same_page_price_conflict_preserved():
    content = _content(
        json_ld=[{"@type": "Product", "name": "Fan", "offers": {"price": "499000", "priceCurrency": "VND"}}],
        og={"product:price:amount": "529000", "product:price:currency": "VND"},
    )
    result = ProductEvidenceExtractor().extract(_observation(), content)
    # JSON-LD price wins (priority), OG price retained as conflict evidence.
    assert result.evidence.offer.price.value == 499000
    price_conflicts = [c for c in result.conflicts if c.field == "price"]
    assert price_conflicts
    values = [v.value for v in price_conflicts[0].values]
    assert 499000 in values
    assert 529000 in values


# --- No product evidence ---


def test_no_product_evidence_for_generic_page():
    content = _content(title="News Homepage", visible_text="Navigation links and unrelated content.")
    result = ProductEvidenceExtractor().extract(_observation("https://news.test/"), content)
    assert result.status == EvidenceExtractionStatus.NO_PRODUCT_EVIDENCE
    assert result.evidence is None


# --- Provenance: acquisition method preserved ---


def test_evidence_source_preserves_browser_method():
    content = _content(title="T", json_ld=[{"@type": "Product", "name": "P"}])
    obs = _observation(method="browser", status="success", title="T")
    result = ProductEvidenceExtractor().extract(obs, content)
    assert result.source.acquisition_method == "browser"
    assert result.source.acquisition_status == "success"
    assert result.source.host == "x.test"


# --- Media references ---


def test_media_references_page_and_og_images():
    content = _content(
        json_ld=[{"@type": "Product", "name": "P", "image": ["https://x.test/j1.jpg", "https://x.test/j2.jpg"]}],
        og={"image": "https://x.test/og.jpg"},
        image_urls=["https://x.test/page1.jpg"],
    )
    result = ProductEvidenceExtractor().extract(_observation(), content)
    media = result.evidence.media
    assert "https://x.test/j1.jpg" in media.page_image_urls
    assert media.og_image_url == "https://x.test/og.jpg"


# --- Specifications ---


def test_specifications_extracted():
    content = _content(json_ld=[{
        "@type": "Product",
        "name": "Fan",
        "additionalProperty": [
            {"@type": "PropertyValue", "name": "Battery Capacity", "value": "5000 mAh"},
            {"@type": "PropertyValue", "name": "Charging Port", "value": "USB-C"},
        ],
    }])
    result = ProductEvidenceExtractor().extract(_observation(), content)
    specs = result.evidence.specifications
    assert len(specs) == 2
    assert specs[0].name.value == "Battery Capacity"
    assert specs[0].value.source_kind == SourceKind.JSON_LD


# --- Claims ---


def test_claims_extracted_from_description():
    content = _content(json_ld=[{
        "@type": "Product",
        "name": "Fan",
        "description": "Turbo cooling performance. Ultra quiet operation. Runs all day.",
    }])
    result = ProductEvidenceExtractor().extract(_observation(), content)
    claims = result.evidence.claims
    texts = [claim.text.value.casefold() for claim in claims]
    assert any("turbo" in t for t in texts)
    assert any("ultra quiet" in t for t in texts)
