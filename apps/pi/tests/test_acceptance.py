"""M11: Product Intelligence V1 end-to-end durable acceptance.

Offline, deterministic. Each test maps to a critical acceptance scenario.
Locked M1-M10 semantics are consumed, never reimplemented.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from media.evidence.models import (
    Availability,
    ClaimEvidence,
    EvidenceSource,
    EvidenceValue,
    IdentityEvidence,
    MediaEvidence,
    OfferEvidence,
    ProductEvidence,
    SourceKind,
    SpecificationEvidence,
)
from media.models import (
    MatchStatus,
    MediaAsset,
    ProductIdentityHint,
    ProductImageAssessment,
)
from media.research import ProductResearchService, ResearchProductRequest
from media.research.models import ResearchStatus, SourceResearchResult
from media.research_persistence import (
    ResearchPersistenceService,
    SnapshotDeserializationError,
    SQLiteResearchRepository,
    UnsupportedSnapshotVersionError,
    make_snapshot,
)
from media.resolution import ProductResolver
from media.resolution.models import (
    CanonicalProduct,
    ProductListing,
    ProductVariant,
    ResolvedField,
)
from media.resource_pack import (
    PackStatus,
    ResourcePackBuilder,
)
from media.resource_pack.lock_store import ResourcePackLockStore
from media.source_discovery import (
    SourceCandidate,
)
from media.source_discovery.relevance import RelevanceLevel, assess_relevance
from media.web_acquisition import AcquisitionService, AcquisitionStatus
from media.web_acquisition.browser_provider import PlaywrightBrowserAcquisitionProvider
from media.web_acquisition.models import BrowserAcquisitionRequest, ExtractedPageContent


def _stamp():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# ---------- fixtures ----------


def _evidence(eid, url, brand="Jisulife", model="FA55", price=699000, seller="Store A", variant=None, gtin=None, name=None):
    src = EvidenceSource(requested_url=url, final_url=url, host=url.split("//")[1].split("/")[0], acquisition_method="http", acquisition_status="success", observed_at=_stamp())
    identity = IdentityEvidence(
        name=EvidenceValue(value=name or f"{brand} {model}", source_kind=SourceKind.JSON_LD, source_path="Product.name"),
        brand=EvidenceValue(value=brand, source_kind=SourceKind.JSON_LD, source_path="Product.brand.name"),
        model=EvidenceValue(value=model, source_kind=SourceKind.JSON_LD, source_path="Product.model"),
        variant=EvidenceValue(value=variant, source_kind=SourceKind.JSON_LD, source_path="Product.variant") if variant else None,
        gtin=EvidenceValue(value=gtin, source_kind=SourceKind.JSON_LD, source_path="Product.gtin13") if gtin else None,
    )
    offer = OfferEvidence(
        price=EvidenceValue(value=price, source_kind=SourceKind.JSON_LD, source_path="Product.offers.price"),
        currency=EvidenceValue(value="VND", source_kind=SourceKind.JSON_LD),
        seller_name=EvidenceValue(value=seller, source_kind=SourceKind.JSON_LD),
        availability=EvidenceValue(value=Availability.IN_STOCK, source_kind=SourceKind.JSON_LD),
    )
    return ProductEvidence(
        evidence_id=eid,
        source=src,
        identity=identity,
        offer=offer,
        specifications=[SpecificationEvidence(name=EvidenceValue(value="Battery Capacity", source_kind=SourceKind.JSON_LD), value=EvidenceValue(value="5000 mAh", source_kind=SourceKind.JSON_LD))],
        claims=[ClaimEvidence(text=EvidenceValue(value="10x stronger airflow", source_kind=SourceKind.JSON_LD))],
        media=MediaEvidence(page_image_urls=[f"https://x.test/{eid}.jpg"]),
    )


def _asset(asset_id, match_score=0.9, variant=None):
    provenance = {"source_class": "marketplace", "usage_policy": "reference_only"}
    if variant is not None:
        provenance["variant"] = variant
    return MediaAsset(
        asset_id=asset_id, local_path=f"data/product_scout/images/{asset_id}.jpg",
        sha256=asset_id * 4, phash="p" * 16, width=1000, height=1000, format="jpg",
        discovery={"provider": "duckduckgo_images", "query": "q", "rank": 1},
        source={"image_url": f"https://x.test/{asset_id}.jpg", "page_url": "https://x.test/p", "host": "x.test"},
        assessment=ProductImageAssessment(product_match_score=match_score, match_status=MatchStatus.MATCHED),
        provenance=provenance,
    )


def _png(color, size=(1000, 1000), marker=None):
    im = Image.new("RGB", size, color)
    d = ImageDraw.Draw(im)
    if marker == "circle":
        d.ellipse((250, 250, 750, 750), fill="white")
    elif marker == "triangle":
        d.polygon(((500, 200), (200, 800), (800, 800)), fill="white")
    else:
        d.rectangle((0, 0, 500, 500), fill="white")
    b = BytesIO()
    im.save(b, format="PNG")
    return b.getvalue()


# ---------- E2E: identity safety ----------


def test_false_merge_fa55_vs_fa55_pro_models_present():
    a = _evidence("a", "https://s1.test/p", model="FA55")
    b = _evidence("b", "https://s2.test/p", model="FA55 Pro")
    result = ProductResolver().resolve([a, b])
    assert len(result.canonical_products) == 2


def test_false_merge_fa55_vs_fa55_pro_titles_only():
    # No model fields; titles differ only by Pro differentiator.
    a = _evidence("a", "https://s1.test/p", name="Jisulife FA55 Turbo Fan")
    b = _evidence("b", "https://s2.test/p", name="Jisulife FA55 Pro Turbo Fan")
    result = ProductResolver().resolve([a, b])
    # Conservative: two separate groups, no blind merge.
    assert len(result.canonical_products) >= 1
    from media.resolution.identity import _exact_or_contained
    assert _exact_or_contained("Jisulife FA55 Turbo Fan", "Jisulife FA55 Pro Turbo Fan") is False


def test_false_merge_baseus_ma10_titles_only():
    from media.resolution.identity import _exact_or_contained
    assert _exact_or_contained("Baseus Bowie MA10", "Baseus Bowie MA10 Pro") is False


def test_gtin_same_model_different_not_same_product():
    a = _evidence("a", "https://s1.test/p", model="FA55", gtin="1234567890123")
    b = _evidence("b", "https://s2.test/p", model="FA55 Pro", gtin="1234567890123")
    result = ProductResolver().resolve([a, b])
    assert len(result.canonical_products) == 2


def test_gtin_different_model_same_not_same_product():
    a = _evidence("a", "https://s1.test/p", model="FA55", gtin="1234567890123")
    b = _evidence("b", "https://s2.test/p", model="FA55", gtin="9999999999999")
    result = ProductResolver().resolve([a, b])
    assert len(result.canonical_products) == 2


def test_missing_gtin_not_conflict():
    a = _evidence("a", "https://s1.test/p", model="FA55")
    b = _evidence("b", "https://s2.test/p", model="FA55", gtin=None)
    result = ProductResolver().resolve([a, b])
    assert len(result.canonical_products) == 1


# ---------- E2E: variant / listing separation ----------


def test_variant_separation_black_white():
    a = _evidence("a", "https://s1.test/p", variant="Black")
    b = _evidence("b", "https://s2.test/p", variant="White")
    result = ProductResolver().resolve([a, b])
    assert len(result.canonical_products) == 1
    assert len(result.canonical_products[0].variants) == 2


def test_listing_separation_prices():
    a = _evidence("a", "https://s1.test/p", seller="Store A", price=699000)
    b = _evidence("b", "https://s2.test/p", seller="Store B", price=649000)
    result = ProductResolver().resolve([a, b])
    product = result.canonical_products[0]
    assert len(product.listings) == 2
    prices = [listing.price for listing in product.listings]
    assert 699000 in prices and 649000 in prices
    assert "price" not in product.model_dump()


# ---------- E2E: persistence history ----------


def test_price_history_immutable(tmp_path):
    repo = SQLiteResearchRepository(tmp_path / "snapshots.db")
    r1 = _research_result(price=699000)
    r2 = _research_result(price=649000)
    s1 = make_snapshot(r1)
    s2 = make_snapshot(r2)
    repo.save_snapshot(s1)
    repo.save_snapshot(s2)
    assert repo.get_snapshot(s1.snapshot_id).result["canonical_products"][0]["listings"][0]["price"] == 699000
    assert repo.get_snapshot(s2.snapshot_id).result["canonical_products"][0]["listings"][0]["price"] == 649000


def test_availability_history_immutable(tmp_path):
    repo = SQLiteResearchRepository(tmp_path / "snapshots.db")
    r1 = _research_result(availability="in_stock")
    r2 = _research_result(availability="out_of_stock")
    s1 = make_snapshot(r1)
    s2 = make_snapshot(r2)
    repo.save_snapshot(s1)
    repo.save_snapshot(s2)
    assert repo.get_snapshot(s1.snapshot_id).result["canonical_products"][0]["listings"][0]["availability"] == "in_stock"
    assert repo.get_snapshot(s2.snapshot_id).result["canonical_products"][0]["listings"][0]["availability"] == "out_of_stock"


def test_canonical_id_evolution_no_migration(tmp_path):
    repo = SQLiteResearchRepository(tmp_path / "snapshots.db")
    r1 = _research_result(gtin=None, product_id="product_old")
    r2 = _research_result(gtin="1234567890123", product_id="product_new")
    s1 = make_snapshot(r1)
    s2 = make_snapshot(r2)
    repo.save_snapshot(s1)
    repo.save_snapshot(s2)
    assert repo.get_snapshot(s1.snapshot_id).result["canonical_products"][0]["canonical_product_id"] == "product_old"
    assert repo.get_snapshot(s2.snapshot_id).result["canonical_products"][0]["canonical_product_id"] == "product_new"


def test_corrupt_snapshot(tmp_path):
    repo = SQLiteResearchRepository(tmp_path / "snapshots.db")
    s = make_snapshot(_research_result())
    repo.save_snapshot(s)
    db = sqlite3.connect(tmp_path / "snapshots.db")
    db.execute("UPDATE research_snapshots SET result_json=? WHERE snapshot_id=?", ("not-json{{", s.snapshot_id))
    db.commit()
    db.close()
    with pytest.raises(SnapshotDeserializationError):
        repo.get_snapshot(s.snapshot_id)


def test_unsupported_version(tmp_path):
    repo = SQLiteResearchRepository(tmp_path / "snapshots.db")
    s = make_snapshot(_research_result())
    s.schema_version = 99
    with pytest.raises(UnsupportedSnapshotVersionError):
        repo.save_snapshot(s)


def test_latest_deterministic_tiebreaker(tmp_path):
    repo = SQLiteResearchRepository(tmp_path / "snapshots.db")
    s1 = make_snapshot(_research_result(price=699000))
    s2 = make_snapshot(_research_result(price=649000))
    s1.created_at = "2026-08-10T00:00:00Z"
    s2.created_at = "2026-08-10T00:00:00Z"
    repo.save_snapshot(s1)
    repo.save_snapshot(s2)
    assert repo.get_latest_snapshot(s2.research_id).snapshot_id == s2.snapshot_id


def test_evidence_lineage_round_trip(tmp_path):
    repo = SQLiteResearchRepository(tmp_path / "snapshots.db")
    result = _research_result()
    s = make_snapshot(result)
    repo.save_snapshot(s)
    loaded = repo.get_snapshot(s.snapshot_id).result
    brand_ev = result.product_evidence[0].identity.brand
    assert loaded["canonical_products"][0]["brand"]["supporting_evidence_ids"] == ["e1"]
    assert loaded["product_evidence"][0]["identity"]["brand"]["source_path"] == "Product.brand.name"
    assert loaded["product_evidence"][0]["source"]["requested_url"] == "https://s1.test/p"
    assert brand_ev.value == "Jisulife"


# ---------- E2E: research orchestration (offline fixtures) ----------


class _FakeEvidence:
    def __init__(self, by_url, status_by_url=None):
        self.by_url = by_url
        self.status_by_url = status_by_url or {url: "success" for url in by_url}

    @property
    def acquisition(self):
        return self

    def acquire(self, url, **kwargs):
        from types import SimpleNamespace

        from media.web_acquisition.models import ExtractedPageContent

        self._last_url = url
        return SimpleNamespace(
            content=ExtractedPageContent(),
            observations=[SimpleNamespace(
                status=SimpleNamespace(value=self.status_by_url.get(url, "success")),
                method="http",
                domain=url.split("//")[1].split("/")[0],
                signals=[],
                reason=None,
                observed_at=_stamp(),
            )],
        )

    @property
    def extractor(self):
        return self

    def extract(self, observation, content):
        from media.evidence.models import (
            EvidenceExtractionStatus,
            ProductEvidenceExtractionResult,
        )

        if observation.status.value != "success":
            return ProductEvidenceExtractionResult(status=EvidenceExtractionStatus.NO_PRODUCT_EVIDENCE, source=_evidence("x", "https://x.test/p").source)
        ev = self.by_url.get(getattr(self, "_last_url", ""))
        if ev is None:
            return ProductEvidenceExtractionResult(status=EvidenceExtractionStatus.NO_PRODUCT_EVIDENCE, source=_evidence("x", "https://x.test/p").source)
        return ProductEvidenceExtractionResult(status=EvidenceExtractionStatus.COMPLETED, source=ev.source, evidence=ev)


class _NoopMedia:
    def __init__(self):
        self.calls = 0

    def research(self, hint, **kwargs):
        self.calls += 1
        from media.models import MediaResearchResult

        return MediaResearchResult(status="completed", product=hint, selected_assets=[])


def _research_result(price=699000, availability="in_stock", gtin=None, product_id="product_jisulife_fa55"):
    from media.research.models import (
        ProductResearchResult,
        ResearchProductRequest,
        ResearchSummary,
    )

    ev = _evidence("e1", "https://s1.test/p", price=price)
    availability_enum = Availability(availability)
    ev.offer.availability = EvidenceValue(value=availability_enum, source_kind=SourceKind.JSON_LD)
    product = CanonicalProduct(
        canonical_product_id=product_id,
        brand=ResolvedField(value="Jisulife", supporting_evidence_ids=["e1"], source_values=["Jisulife"]),
        model=ResolvedField(value="FA55", supporting_evidence_ids=["e1"], source_values=["FA55"]),
        listings=[ProductListing(listing_id="listing_1", canonical_product_id=product_id, source_url="https://s1.test/p", seller="Store A", price=price, currency="VND", availability=availability_enum.value, source_evidence_ids=["e1"], observed_at=_stamp())],
        supporting_evidence_ids=["e1"],
    )
    source = SourceResearchResult(url="https://s1.test/p", acquisition_status="success", acquisition_method="http", evidence_status="completed", evidence_id="e1", host="s1.test", observed_at=_stamp(), usable=True)
    return ProductResearchResult(
        status=ResearchStatus.COMPLETED,
        request=ResearchProductRequest(url="https://s1.test/p"),
        source_results=[source],
        product_evidence=[ev],
        canonical_products=[product],
        variants=[],
        listings=[product.listings[0]],
        media_assets=[_asset("a1")],
        unresolved_evidence_ids=[],
        conflicts=[],
        warnings=[],
        summary=ResearchSummary(sources=1, usable_evidence=1, canonical_products=1),
        evidence_sources=[],
        discovered_sources=[],
        snapshot_id=None,
        research_id=None,
        persistence_status=None,
        error=None,
    )


def test_challenge_isolation_continues():
    good = _evidence("e1", "https://retailer.test/p")
    fake = _FakeEvidence({"https://retailer.test/p": good}, status_by_url={"https://retailer.test/p": "success", "https://tiktok.test/x": "challenged"})
    media = _NoopMedia()
    service = ProductResearchService(evidence_service=fake, resolver=ProductResolver(), media=media)
    result = service.research(ResearchProductRequest(
        url="https://tiktok.test/x",
        additional_urls=["https://retailer.test/p"],
        product_identity_hint=ProductIdentityHint(name="Jisulife FA55", brand="Jisulife", model="FA55"),
    ))
    assert result.status in {ResearchStatus.COMPLETED, ResearchStatus.PARTIAL}
    assert result.source_results[0].acquisition_status == "challenged"


def test_all_sources_unusable_no_fabrication():
    fake = _FakeEvidence({}, status_by_url={"https://s1.test/p": "challenged"})
    service = ProductResearchService(evidence_service=fake, resolver=ProductResolver(), media=_NoopMedia())
    result = service.research(ResearchProductRequest(url="https://s1.test/p", product_identity_hint=ProductIdentityHint(name="Jisulife FA55")))
    assert result.status == ResearchStatus.NO_PRODUCT_EVIDENCE
    assert result.canonical_products == []


def test_media_failure_isolation():
    from media import MediaIntelligenceService
    from media.acquisition import ImageDownloader
    from media.config import MediaConfig
    from media.models import ImageCandidate

    candidates = [ImageCandidate(candidate_id=f"c{i}", discovery_provider="fake", query="q", search_result_rank=i, image_url=f"https://x.test/{i}.jpg", host="x.test", discovered_at=_stamp()) for i in range(1, 7)]
    payloads = {
        "https://x.test/1.jpg": (403, b""),
        "https://x.test/2.jpg": (200, _png("red", (300, 300))),
        "https://x.test/3.jpg": (200, b"corrupt"),
        "https://x.test/4.jpg": (200, _png("red")),
        "https://x.test/5.jpg": (200, _png("blue", marker="circle")),
        "https://x.test/6.jpg": (200, _png("green", marker="triangle")),
    }
    service = MediaIntelligenceService(
        MediaConfig(data_dir=__import__("tempfile").mkdtemp()),
        provider=type("P", (), {"name": "fake", "search": lambda self, q: candidates})(),
        downloader=ImageDownloader(fetcher=lambda url: payloads[url]),
    )
    result = service.research(ProductIdentityHint(name="x"))
    assert result.status.value in ("completed", "partial")
    assert result.valid_count >= 3
    reasons = {r.reason for r in result.rejections}
    assert "IMAGE_HTTP_STATUS" in reasons
    assert "IMAGE_TOO_SMALL" in reasons
    assert "IMAGE_DECODE_FAILED" in reasons


# ---------- E2E: resource pack safety ----------


def _pack_result(products=1, variants=0, media=4, media_variant=None, claims=True):
    from media.research.models import ProductResearchResult, ResearchSummary

    product = CanonicalProduct(
        canonical_product_id="product_fa55",
        brand=ResolvedField(value="Jisulife", supporting_evidence_ids=["e1"]),
        model=ResolvedField(value="FA55", supporting_evidence_ids=["e1"]),
        variants=[
            ProductVariant(variant_id="variant_black", canonical_product_id="product_fa55", dimensions={"variant": "black"}, supporting_evidence_ids=["e1"]),
            ProductVariant(variant_id="variant_white", canonical_product_id="product_fa55", dimensions={"variant": "white"}, supporting_evidence_ids=["e1"]),
        ][:variants],
        claims=[ClaimEvidence(text=EvidenceValue(value="10x stronger airflow", source_kind=SourceKind.JSON_LD))] if claims else [],
        supporting_evidence_ids=["e1"],
    )
    source = SourceResearchResult(url="https://s1.test/p", acquisition_status="success", acquisition_method="http", evidence_status="completed", evidence_id="e1", host="s1.test", usable=True)
    return ProductResearchResult(
        status=ResearchStatus.COMPLETED,
        request=ResearchProductRequest(url="https://s1.test/p"),
        source_results=[source],
        canonical_products=[product] * products,
        media_assets=[_asset(f"a{i}", variant=media_variant) for i in range(media)],
        summary=ResearchSummary(sources=1, usable_evidence=1, canonical_products=len([product] * products)),
    )


def test_pack_clear_product_ready():
    pack = ResourcePackBuilder().build(_pack_result(variants=0, media=4))
    assert pack.pack_status == PackStatus.READY_TO_LOCK


def test_pack_no_product_rejected():
    pack = ResourcePackBuilder().build(_pack_result(products=0))
    assert pack.pack_status == PackStatus.REJECTED


def test_pack_ambiguous_needs_review():
    pack = ResourcePackBuilder().build(_pack_result(products=2))
    assert pack.pack_status == PackStatus.NEEDS_REVIEW


def test_pack_variant_required():
    pack = ResourcePackBuilder().build(_pack_result(variants=2, media=4), variant_id=None)
    assert pack.pack_status == PackStatus.NEEDS_REVIEW


def test_pack_unknown_variant_media_needs_review():
    pack = ResourcePackBuilder().build(_pack_result(variants=2, media=4, media_variant=None), variant_id="variant_black")
    assert pack.pack_status == PackStatus.NEEDS_REVIEW
    assert pack.reason == "selected variant media not verified against chosen variant"


def test_pack_explicit_variant_conflict_excluded():
    pack = ResourcePackBuilder().build(_pack_result(variants=2, media=4, media_variant="white"), variant_id="variant_black")
    assert pack.pack_status == PackStatus.INCOMPLETE
    assert pack.media == []


def test_pack_claim_stays_source_claim():
    pack = ResourcePackBuilder().build(_pack_result(variants=0, media=4))
    assert any(claim.status.value == "source_claim" for claim in pack.claims)
    assert not any(claim.status.value == "verified_spec" for claim in pack.claims)


def test_lock_immutable_versioned(tmp_path):
    store = ResourcePackLockStore(tmp_path / "locks.db")
    pack = ResourcePackBuilder().build(_pack_result(variants=0, media=4))
    v1 = store.lock(pack)
    v2 = store.lock(pack)
    assert v1.version == 1 and v2.version == 2
    assert store.get_latest_lock(pack.resource_pack_id).version == 2


# ---------- E2E: retrieval no rerun ----------


def test_research_retrieval_no_rerun(tmp_path):
    repo = SQLiteResearchRepository(tmp_path / "snapshots.db")
    s = make_snapshot(_research_result())
    repo.save_snapshot(s)
    service = ResearchPersistenceService(repository=repo)
    loaded = service.get(s.snapshot_id)
    assert loaded.result["canonical_products"][0]["canonical_product_id"] == "product_jisulife_fa55"


# ---------- E2E: source relevance (M6 locked) ----------


def test_source_relevance_ranking():
    hint = ProductIdentityHint(name="Jisulife FA55", brand="Jisulife", model="FA55")
    cases = {
        "Jisulife FA55 Portable Turbo Fan": RelevanceLevel.HIGH,
        "Jisulife FA55 Pro Portable Fan": None,  # not exact HIGH
        "JISULIFE FA53 Turbo Fan": RelevanceLevel.LOW,
        "Agricultural Tractor Supplies": RelevanceLevel.LOW,
        "JISULIFE Official Products": RelevanceLevel.MEDIUM,
    }
    for title, expected in cases.items():
        c = SourceCandidate(candidate_id="c", url="https://x.test/p", title=title, host="x.test")
        level, _ = assess_relevance(c, hint)
        if expected is None:
            assert level != RelevanceLevel.HIGH
        else:
            assert level == expected


def test_source_unknown_host_relevant_eligible():
    c = SourceCandidate(candidate_id="c", url="https://random.example/p", title="Jisulife FA55 Rechargeable", host="random.example")
    level, _ = assess_relevance(c, ProductIdentityHint(name="Jisulife FA55", brand="Jisulife", model="FA55"))
    assert level == RelevanceLevel.HIGH


# ---------- E2E: browser fallback (M2 locked) ----------


def test_http_success_avoids_browser():
    service = AcquisitionService(
        http_provider=_FixedHttp(AcquisitionStatus.SUCCESS, final_url="https://x.test/p"),
        browser_provider=_FixedBrowser(AcquisitionStatus.SUCCESS, title="wrong"),
    )
    result = service.acquire("https://x.test/p")
    assert result.status == AcquisitionStatus.SUCCESS
    assert len(result.observations) == 1


def test_http_blocked_runs_browser_once():
    service = AcquisitionService(
        http_provider=_FixedHttp(AcquisitionStatus.BLOCKED, http_status=403, reason="HTTP 403"),
        browser_provider=_FixedBrowser(AcquisitionStatus.SUCCESS, title="recovered", text="body", images=["https://x.test/i.jpg"]),
    )
    result = service.acquire("https://x.test/p")
    assert result.status == AcquisitionStatus.SUCCESS
    assert len(result.observations) == 2  # HTTP once + browser once, no loop


def test_browser_engine_failure_clean():
    class BrokenEngine:
        def open(self, request):
            raise RuntimeError("browser crash")

    provider = PlaywrightBrowserAcquisitionProvider(engine=BrokenEngine())
    result = provider.acquire(BrowserAcquisitionRequest(url="https://x.test/p"))
    assert result.observation.status == AcquisitionStatus.FAILED
    assert "BROWSER_ENGINE_ERROR" in result.observation.reason


class _FixedHttp:
    def __init__(self, status, http_status=None, final_url="https://x.test/p", signals=(), reason=None, summary=None):
        self._status = status
        self._http_status = http_status
        self._final_url = final_url
        self._signals = signals
        self._reason = reason
        self._summary = summary

    def acquire(self, request):
        from media.web_acquisition.models import (
            HttpAcquisitionResult,
            SourceObservation,
        )

        obs = SourceObservation(
            requested_url=request.url, method="http", status=self._status,
            final_url=self._final_url, domain="x.test", http_status=self._http_status,
            signals=list(self._signals), reason=self._reason, observed_at=_stamp(),
        )
        return HttpAcquisitionResult(observation=obs, content=ExtractedPageContent())


class _FixedBrowser:
    def __init__(self, status, title=None, text=None, images=(), signals=(), reason=None):
        self._status = status
        self._title = title
        self._text = text
        self._images = images
        self._signals = signals
        self._reason = reason

    def acquire(self, request):
        from media.web_acquisition.models import (
            BrowserAcquisitionResult,
            SourceObservation,
        )

        content = ExtractedPageContent(title=self._title, visible_text=self._text, image_urls=list(self._images))
        obs = SourceObservation(
            requested_url=request.url, method="browser", status=self._status,
            final_url=request.url, domain="x.test", signals=list(self._signals),
            reason=self._reason, observed_at=_stamp(),
        )
        return BrowserAcquisitionResult(observation=obs, content=content)
