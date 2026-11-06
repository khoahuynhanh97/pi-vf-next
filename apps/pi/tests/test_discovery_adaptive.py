"""Tests for adaptive usable-source stopping and relevance assessment."""

from __future__ import annotations

from datetime import UTC, datetime

from media.evidence.models import (
    EvidenceSource,
    EvidenceValue,
    IdentityEvidence,
    OfferEvidence,
    ProductEvidence,
    SourceKind,
)
from media.models import ProductIdentityHint
from media.research import ProductResearchService
from media.research.models import ResearchProductRequest, is_usable_source_result
from media.resolution import ProductResolver
from media.source_discovery import SourceCandidate, SourceClass
from media.source_discovery.relevance import RelevanceLevel, assess_relevance


def _stamp():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _hint():
    return ProductIdentityHint(name="Jisulife FA55", brand="Jisulife", model="FA55")


def _candidate(url, host, title, source_class=SourceClass.UNKNOWN, rank=1):
    return SourceCandidate(
        candidate_id=f"c{rank}", url=url, title=title, snippet="", host=host, query="q",
        discovery_provider="test", rank=rank, source_class=source_class, discovered_at=_stamp(),
    )


def _evidence(url, eid="e1"):
    i = IdentityEvidence()
    i.brand = EvidenceValue(value="Jisulife", source_kind=SourceKind.JSON_LD)
    i.model = EvidenceValue(value="FA55", source_kind=SourceKind.JSON_LD)
    offer = OfferEvidence(price=EvidenceValue(value=699000, source_kind=SourceKind.JSON_LD))
    src = EvidenceSource(requested_url=url, final_url=url, host=url.split("//")[1].split("/")[0], acquisition_method="http", acquisition_status="success", observed_at=_stamp())
    return ProductEvidence(evidence_id=eid, source=src, identity=i, offer=offer)


# --- relevance ---


def test_relevance_exact_model_high():
    c = _candidate("https://x.test/p", "x.test", "JISULIFE FA55 Portable Turbo Fan")
    level, _reason = assess_relevance(c, _hint())
    assert level == RelevanceLevel.HIGH


def test_relevance_pro_suffix_not_exact():
    c = _candidate("https://x.test/p", "x.test", "JISULIFE FA55 Pro Portable Fan")
    level, _reason = assess_relevance(c, _hint())
    assert level != RelevanceLevel.HIGH


def test_relevance_different_model_low():
    c = _candidate("https://x.test/p", "x.test", "JISULIFE FA53 Turbo Fan")
    level, _reason = assess_relevance(c, _hint())
    assert level == RelevanceLevel.LOW


def test_relevance_brand_only_medium():
    c = _candidate("https://x.test/p", "x.test", "JISULIFE Official Products")
    level, _reason = assess_relevance(c, _hint())
    assert level == RelevanceLevel.MEDIUM


def test_relevance_irrelevant_low():
    c = _candidate("https://tractorsupply.com/p", "tractorsupply.com", "Agricultural Tractor Supplies")
    level, _reason = assess_relevance(c, _hint())
    assert level == RelevanceLevel.LOW


def test_relevance_unknown_host_relevant_high():
    c = _candidate("https://random-store.example/p", "random-store.example", "Jisulife FA55 Rechargeable Fan")
    level, _reason = assess_relevance(c, _hint())
    assert level == RelevanceLevel.HIGH


def test_relevance_name_only_identity_matches_name_tokens():
    hint = ProductIdentityHint(name="Jisulife FA55")  # no brand/model in hint
    c = _candidate("https://x.test/p", "x.test", "Jisulife FA55 Portable Turbo Fan")
    level, _reason = assess_relevance(c, hint)
    assert level == RelevanceLevel.MEDIUM  # name token overlap


def test_relevance_name_only_identity_irrelevant_still_low():
    hint = ProductIdentityHint(name="Jisulife FA55")
    c = _candidate("https://tractorsupply.com/p", "tractorsupply.com", "Agricultural Tractor Supplies")
    level, _reason = assess_relevance(c, hint)
    assert level == RelevanceLevel.LOW


# --- adaptive stop ---


class _FakeAcquisition:
    def __init__(self, status_by_url: dict[str, str]):
        self.status_by_url = status_by_url

    def acquire(self, url, **kwargs):
        from types import SimpleNamespace

        from media.web_acquisition.models import ExtractedPageContent

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


class _FakeExtractor:
    def __init__(self, usable: set[str]):
        self.usable = usable

    def extract(self, observation, content):
        from media.evidence.models import (
            EvidenceExtractionStatus,
            ProductEvidenceExtractionResult,
        )

        url = "https://" + (observation.domain or "x.test") + "/p"
        if observation.status.value != "success" or url not in self.usable:
            return ProductEvidenceExtractionResult(status=EvidenceExtractionStatus.NO_PRODUCT_EVIDENCE, source=_source(observation.domain or "x.test"))
        evidence = _evidence(url)
        return ProductEvidenceExtractionResult(status=EvidenceExtractionStatus.COMPLETED, source=evidence.source, evidence=evidence)


def _source(url):
    if "//" not in url:
        url = f"https://{url}/p"
    return EvidenceSource(requested_url=url, final_url=url, host=url.split("//")[1].split("/")[0], acquisition_method="http", acquisition_status="success", observed_at=_stamp())


class _FakeEvidenceService:
    def __init__(self, status_by_url, usable):
        self._acq = _FakeAcquisition(status_by_url)
        self._ext = _FakeExtractor(usable)

    @property
    def acquisition(self):
        return self._acq

    @property
    def extractor(self):
        return self._ext


class _FakeDiscovery:
    def __init__(self, max_sources, target_usable, discovered):
        self.config = type("C", (), {"max_sources": max_sources, "target_usable_sources": target_usable, "per_host_limit": 2, "max_results": 15, "diversity_order": ()})()
        self._discovered = discovered

    def discover(self, hint, **kwargs):
        return self._discovered


class _NoopMedia:
    def research(self, hint, **kwargs):
        from media.models import MediaResearchResult

        return MediaResearchResult(status="completed", product=hint, selected_assets=[])


def _service(max_sources=8, target=3, urls=(), usable=(), status_by_url=None):
    status_by_url = status_by_url or {url: "success" for url in urls}
    discovered = [_candidate(url, url.split("//")[1].split("/")[0], "Jisulife FA55", rank=i + 1) for i, url in enumerate(urls)]
    return ProductResearchService(
        evidence_service=_FakeEvidenceService(status_by_url, set(usable)),
        resolver=ProductResolver(),
        media=_NoopMedia(),
        discovery=_FakeDiscovery(max_sources, target, discovered),
    )


def test_adaptive_stops_at_target():
    urls = [f"https://s{i}.test/p" for i in range(1, 5)]
    service = _service(max_sources=8, target=3, urls=urls, usable=urls)
    result = service.research(ResearchProductRequest(query="Jisulife FA55", discover_sources=True))
    assert result.summary.usable_sources == 3
    assert result.summary.attempted_sources == 3
    assert result.summary.stop_reason == "target_reached"


def test_adaptive_skips_unusable_and_stops_at_target():
    urls = [f"https://s{i}.test/p" for i in range(1, 6)]
    status = {urls[0]: "challenged", urls[1]: "success"}  # s2 has no evidence (not in usable)
    service = _service(max_sources=8, target=3, urls=urls, usable=[urls[2], urls[3], urls[4]], status_by_url=status)
    result = service.research(ResearchProductRequest(query="Jisulife FA55", discover_sources=True))
    assert result.summary.usable_sources == 3
    assert result.summary.attempted_sources == 5
    assert result.summary.stop_reason == "target_reached"


def test_adaptive_stops_at_budget():
    urls = [f"https://s{i}.test/p" for i in range(1, 7)]
    service = _service(max_sources=5, target=10, urls=urls, usable=[urls[0]])
    result = service.research(ResearchProductRequest(query="Jisulife FA55", discover_sources=True))
    assert result.summary.usable_sources == 1
    assert result.summary.attempted_sources == 5
    assert result.summary.stop_reason == "budget_exhausted"


def test_usable_source_definition():
    assert is_usable_source_result(type("S", (), {"evidence_id": "e1", "acquisition_status": "success"})()) is True
    assert is_usable_source_result(type("S", (), {"evidence_id": None, "acquisition_status": "success"})()) is False
    assert is_usable_source_result(type("S", (), {"evidence_id": "e1", "acquisition_status": "challenged"})()) is False
    assert is_usable_source_result(type("S", (), {"evidence_id": "e1", "acquisition_status": "blocked"})()) is False
    assert is_usable_source_result(type("S", (), {"evidence_id": "e1", "acquisition_status": "failed"})()) is False
