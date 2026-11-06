"""Offline tests for ProductResearchService. No network, no LLM."""

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
    SourceKind,
)
from media.models import ProductIdentityHint
from media.research import ProductResearchService, ResearchProductRequest
from media.research.models import ResearchStatus
from media.resolution import ProductResolver


def _stamp():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _source(url):
    if "//" not in url:
        url = f"https://{url}/p"
    host = url.split("//")[1].split("/")[0]
    return EvidenceSource(requested_url=url, final_url=url, host=host, acquisition_method="http", acquisition_status="success", observed_at=_stamp())


def _evidence(eid, url, brand=None, model=None, price=None, seller=None, name=None):
    i = IdentityEvidence()
    if brand:
        i.brand = EvidenceValue(value=brand, source_kind=SourceKind.JSON_LD)
    if model:
        i.model = EvidenceValue(value=model, source_kind=SourceKind.JSON_LD)
    if name:
        i.name = EvidenceValue(value=name, source_kind=SourceKind.JSON_LD)
    offer = None
    if price is not None:
        offer = OfferEvidence(
            price=EvidenceValue(value=price, source_kind=SourceKind.JSON_LD),
            currency=EvidenceValue(value="VND", source_kind=SourceKind.JSON_LD),
            seller_name=EvidenceValue(value=seller, source_kind=SourceKind.JSON_LD) if seller else None,
        )
    return ProductEvidence(
        evidence_id=eid,
        source=_source(url),
        identity=i,
        offer=offer,
        claims=[ClaimEvidence(text=EvidenceValue(value="Turbo cooling", source_kind=SourceKind.JSON_LD))],
        media=MediaEvidence(page_image_urls=[f"https://x.test/{eid}.jpg"]),
    )


class FakeEvidenceService:
    """Returns fixture ProductEvidence per URL without acquisition."""

    def __init__(self, by_url: dict[str, ProductEvidence], challenged: set[str] | None = None):
        self.by_url = by_url
        self.challenged = challenged or set()

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
                status=SimpleNamespace(value="challenged" if url in self.challenged else "success"),
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

        if observation.status.value == "challenged":
            return ProductEvidenceExtractionResult(status=EvidenceExtractionStatus.NO_PRODUCT_EVIDENCE, source=_source(observation.domain or "https://x.test"))
        url = getattr(self, "_last_url", "")
        evidence = self.by_url.get(url)
        if evidence is None:
            return ProductEvidenceExtractionResult(status=EvidenceExtractionStatus.NO_PRODUCT_EVIDENCE, source=_source(url or "https://x.test/p"))
        return ProductEvidenceExtractionResult(status=EvidenceExtractionStatus.COMPLETED, source=evidence.source, evidence=evidence)


def _research_service(fake_evidence, media_result=None):
    from media.models import MediaResearchResult

    class FakeMedia:
        def __init__(self):
            self.calls = []

        def research(self, hint, **kwargs):
            self.calls.append((hint, kwargs))
            if media_result is not None:
                return media_result
            return MediaResearchResult(status="completed", product=hint, selected_assets=[])

    media = FakeMedia()
    service = ProductResearchService(evidence_service=fake_evidence, resolver=ProductResolver(), media=media)
    return service, media


def test_research_01_single_usable_page():
    ev = _evidence("e1", "https://shop1.test/p", brand="Jisulife", model="FA55", price=699000, seller="Store A")
    service, _media = _research_service(FakeEvidenceService({"https://shop1.test/p": ev}))
    result = service.research(ResearchProductRequest(url="https://shop1.test/p", product_identity_hint=ProductIdentityHint(name="Jisulife FA55", brand="Jisulife", model="FA55")))
    assert result.status in {ResearchStatus.COMPLETED, ResearchStatus.PARTIAL}
    assert result.source_results[0].acquisition_status == "success"
    assert len(result.canonical_products) == 1
    assert _media.calls  # media researched with resolved identity


def test_resolved_identity_and_page_gallery_reach_media_without_url_query():
    url = "https://shop1.test/product"
    ev = _evidence("gallery", url, brand="Acme", model="M1", name="Acme M1")
    ev.media.page_image_urls = ["https://cdn.test/front.jpg", "https://cdn.test/back.jpg"]
    ev.media.og_image_url = ev.media.page_image_urls[0]
    service, media = _research_service(FakeEvidenceService({url: ev}))
    service.research(ResearchProductRequest(url=url, query=url, discover_sources=False))
    identity, kwargs = media.calls[0]
    assert identity.name == "Acme M1"
    assert identity.model == "M1"
    seeds = kwargs["seed_candidates"]
    assert [c.image_url for c in seeds] == ev.media.page_image_urls
    assert all(c.source_page_url == url and c.discovery_provider == "product_evidence" for c in seeds)


def test_explicit_official_url_keeps_source_class_without_discovery():
    url = "https://www.mi.com/global/product/xiaomi-desk-lamp-lite/"
    ev = _evidence("e-mi", url, brand="Xiaomi", model="Desk Lamp Lite")
    service, _ = _research_service(FakeEvidenceService({url: ev}))
    result = service.research(ResearchProductRequest(
        url=url,
        product_identity_hint=ProductIdentityHint(name="Xiaomi Desk Lamp Lite"),
        discover_sources=False,
    ))
    assert result.discovered_sources == []
    assert result.source_results[0].source_class == "official"
    assert result.source_results[0].usable is True


def test_research_02_challenged_and_successful_continue():
    good = _evidence("e1", "https://retailer.test/p", brand="Jisulife", model="FA55", price=699000)
    service, _ = _research_service(FakeEvidenceService({"https://retailer.test/p": good}, challenged={"https://shop.tiktok.com/x"}))
    result = service.research(ResearchProductRequest(
        url="https://shop.tiktok.com/x",
        additional_urls=["https://retailer.test/p"],
        product_identity_hint=ProductIdentityHint(name="Jisulife FA55", brand="Jisulife", model="FA55"),
    ))
    assert result.status in {ResearchStatus.COMPLETED, ResearchStatus.PARTIAL}
    assert result.source_results[0].acquisition_status == "challenged"
    assert len(result.canonical_products) == 1


def test_research_03_two_products_not_forced_together():
    a = _evidence("a", "https://s1.test/p", brand="Jisulife", model="FA55")
    b = _evidence("b", "https://s2.test/p", brand="Jisulife", model="FA55 Pro")
    service, _ = _research_service(FakeEvidenceService({"https://s1.test/p": a, "https://s2.test/p": b}))
    result = service.research(ResearchProductRequest(
        additional_urls=["https://s1.test/p", "https://s2.test/p"],
        product_identity_hint=ProductIdentityHint(name="Jisulife FA55", brand="Jisulife"),
    ))
    assert len(result.canonical_products) == 2


def test_research_04_partial_media_still_ok():
    from media.models import MediaResearchResult
    from media.models import ResearchStatus as MediaStatus

    ev = _evidence("e1", "https://s1.test/p", brand="Jisulife", model="FA55")
    partial = MediaResearchResult(status=MediaStatus.PARTIAL, product=ProductIdentityHint(name="Jisulife FA55"), selected_assets=[])
    service, _media = _research_service(FakeEvidenceService({"https://s1.test/p": ev}), media_result=partial)
    result = service.research(ResearchProductRequest(url="https://s1.test/p", product_identity_hint=ProductIdentityHint(name="Jisulife FA55", brand="Jisulife", model="FA55")))
    assert result.status in {ResearchStatus.COMPLETED, ResearchStatus.PARTIAL}
    assert result.summary.media_status == "partial"


def test_research_05_no_evidence_no_fabrication():
    service, _ = _research_service(FakeEvidenceService({}, challenged={"https://s1.test/p"}))
    result = service.research(ResearchProductRequest(url="https://s1.test/p", product_identity_hint=ProductIdentityHint(name="Jisulife FA55")))
    assert result.status == ResearchStatus.NO_PRODUCT_EVIDENCE
    assert result.canonical_products == []


def test_research_request_requires_seed():
    service, _ = _research_service(FakeEvidenceService({}))
    result = service.research(ResearchProductRequest())
    assert result.status == ResearchStatus.ERROR


def test_mcp_delegates_to_research_service(monkeypatch):
    from product_scout import mcp_server

    captured = {}

    def fake_research(self, request, **kwargs):
        captured["request"] = request
        from media.research.models import ProductResearchResult, ResearchSummary

        return ProductResearchResult(status="completed", request=request, summary=ResearchSummary())

    monkeypatch.setattr("media.research.ProductResearchService.research", fake_research)
    out = mcp_server.research_product(product_name="Jisulife FA55", brand="Jisulife", model="FA55")
    assert out["status"] == "completed"
    assert captured["request"].product_identity_hint.brand == "Jisulife"


def test_research_query_only_discover_sources():
    from media.source_discovery import SourceCandidate, SourceClass

    discovered = [
        SourceCandidate(candidate_id="s1", url="https://mi.com/x", title="t", host="mi.com", query="q", discovery_provider="fake", rank=1, source_class=SourceClass.OFFICIAL),
        SourceCandidate(candidate_id="s2", url="https://shopee.vn/y", title="t", host="shopee.vn", query="q", discovery_provider="fake", rank=2, source_class=SourceClass.MARKETPLACE),
    ]

    class FakeDiscovery:
        config = type("C", (), {"max_sources": 8, "per_host_limit": 2, "target_usable_sources": 3, "max_results": 15, "diversity_order": ()})()

        def discover(self, hint, **kwargs):
            return discovered

    fake_evidence = FakeEvidenceService({"https://mi.com/x": _evidence("e1", "https://mi.com/x", brand="Jisulife", model="FA55", price=699000)})
    service = ProductResearchService(evidence_service=fake_evidence, resolver=ProductResolver(), media=_NoopMedia(), discovery=FakeDiscovery())
    result = service.research(ResearchProductRequest(query="Jisulife FA55", discover_sources=True))
    assert result.summary.discovered_sources == 2
    assert len(result.discovered_sources) == 2


class _NoopMedia:
    def research(self, hint, **kwargs):
        from media.models import MediaResearchResult

        return MediaResearchResult(status="completed", product=hint, selected_assets=[])
