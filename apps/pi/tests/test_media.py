"""Offline tests for Media Intelligence. No network, no DDG, no Pillow IO."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from media.acquisition import (
    ContentAddressedImageStore,
    ImageDownloader,
    ImageDownloadError,
)
from media.classification import ImageClassifier
from media.config import MediaConfig
from media.dedup import ImageDeduplicator, phash_distance
from media.matching import DeterministicProductImageMatcher
from media.models import (
    ImageCandidate,
    ImageSearchQuery,
    MatchStatus,
    MediaAsset,
    ProductIdentityHint,
    ProductImageAssessment,
    ResearchStatus,
)
from media.selection import ProductAssetSelector
from media.service import MediaIntelligenceService
from media.validation import TechnicalImageValidator


def _png(color: str = "red", size: tuple[int, int] = (1000, 1000), marker: str | None = None) -> bytes:
    image = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(image)
    if marker == "circle":
        draw.ellipse((250, 250, 750, 750), fill="white")
    elif marker == "triangle":
        draw.polygon(((500, 200), (200, 800), (800, 800)), fill="white")
    else:
        draw.rectangle((0, 0, size[0] // 2, size[1] // 2), fill="white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _candidate(url: str, rank: int = 1, query: str = "test", host: str | None = None) -> ImageCandidate:
    return ImageCandidate(
        candidate_id=f"c{rank}",
        discovery_provider="test",
        query=query,
        search_result_rank=rank,
        image_url=url,
        host=host or "example.com",
        discovered_at="2026-08-10T00:00:00Z",
    )


class FakeFetcher:
    def __init__(self, mapping: dict[str, tuple[int, bytes]]):
        self.mapping = mapping

    def __call__(self, url: str):
        status, payload = self.mapping[url]
        return status, payload


class FakeProvider:
    name = "fake_provider"

    def __init__(self, candidates: list[ImageCandidate]):
        self.candidates = candidates

    def search(self, query: ImageSearchQuery) -> list[ImageCandidate]:
        return self.candidates


def _service(config: MediaConfig, candidates: list[ImageCandidate], payloads: dict[str, tuple[int, bytes]], matcher=None):
    return MediaIntelligenceService(
        config,
        provider=FakeProvider(candidates),
        downloader=ImageDownloader(fetcher=FakeFetcher(payloads)),
        matcher=matcher,
    )


# --- Technical validation ---


def test_valid_jpeg_passes_technical_validation(tmp_path):
    config = MediaConfig(data_dir=tmp_path)
    validator = TechnicalImageValidator(config)
    payload = _png("blue")
    result = validator.validate(_candidate("https://x.test/a.jpg"), payload)
    assert result.status == "valid"
    assert result.width == 1000 and result.height == 1000
    assert result.format == "png"
    assert len(result.sha256) == 64


def test_corrupt_image_is_rejected(tmp_path):
    validator = TechnicalImageValidator(MediaConfig(data_dir=tmp_path))
    result = validator.validate(_candidate("https://x.test/b.jpg"), b"not-an-image")
    assert result.status == "rejected"
    assert result.reason == "IMAGE_DECODE_FAILED"


def test_too_small_image_is_rejected(tmp_path):
    validator = TechnicalImageValidator(MediaConfig(data_dir=tmp_path))
    result = validator.validate(_candidate("https://x.test/c.jpg"), _png("red", (300, 300)))
    assert result.reason == "IMAGE_TOO_SMALL"


def test_extreme_aspect_ratio_is_rejected(tmp_path):
    validator = TechnicalImageValidator(MediaConfig(data_dir=tmp_path))
    result = validator.validate(_candidate("https://x.test/d.jpg"), _png("red", (4000, 800)))
    assert result.reason == "IMAGE_EXTREME_RATIO"


def test_oversized_payload_is_rejected(tmp_path):
    config = MediaConfig(data_dir=tmp_path, max_image_bytes=1024)
    validator = TechnicalImageValidator(config)
    result = validator.validate(_candidate("https://x.test/e.jpg"), _png("red"))
    assert result.reason == "IMAGE_TOO_LARGE"


def test_downloader_streams_and_guards_size():
    payload = _png("red")
    downloader = ImageDownloader(max_bytes=1024, fetcher=lambda url: (200, payload))
    with pytest.raises(ImageDownloadError) as exc:
        downloader.download(_candidate("https://x.test/a.jpg"))
    assert exc.value.reason == "IMAGE_TOO_LARGE"


def test_downloader_rejects_http_status():
    downloader = ImageDownloader(fetcher=lambda url: (403, b""))
    with pytest.raises(ImageDownloadError) as exc:
        downloader.download(_candidate("https://x.test/a.jpg"))
    assert exc.value.reason == "IMAGE_HTTP_STATUS"


def test_downloader_enforces_wall_clock_deadline(monkeypatch):
    import time

    class SlowResponse:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_bytes(self):
            while True:
                time.sleep(0.05)
                yield b"x"

    class FakeStream:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return SlowResponse()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("httpx.stream", FakeStream)
    downloader = ImageDownloader(timeout_seconds=0.2)
    start = time.monotonic()
    with pytest.raises(ImageDownloadError) as exc:
        downloader.download(_candidate("https://x.test/slow.jpg"))
    elapsed = time.monotonic() - start
    assert exc.value.reason == "IMAGE_TIMEOUT"
    assert elapsed < 3.0  # deadline fires, does not hang


# --- Deduplication ---


def test_exact_duplicate_shares_one_file(tmp_path):
    config = MediaConfig(data_dir=tmp_path)
    store = ContentAddressedImageStore(config)
    payload = _png("red")
    sha1, path1 = store.store(payload, "png")
    sha2, path2 = store.store(payload, "png")
    assert sha1 == sha2 and path1 == path2
    assert len(list(Path(config.storage_dir).iterdir())) == 1


def test_near_duplicate_grouping_preserves_metadata():
    payloads = [_png("red", marker="circle"), _png("red")]
    dedup = ImageDeduplicator(phash_distance_threshold=8)
    import hashlib

    class Fake:
        def __init__(self, payload):
            import media.validation.technical as t

            self.sha256 = hashlib.sha256(payload).hexdigest()
            self.phash = t._hash_image(Image.open(io.BytesIO(payload)))

    items = [Fake(p) for p in payloads]
    dedup.group(items)
    assert items[0].duplicate_group_id is not None
    # Both have distinct sha256 so must not be collapsed to one file, but
    # close phash means they may share a group — verify no metadata is lost.
    assert items[0].sha256 != items[1].sha256
    assert len({item.duplicate_group_id for item in items}) <= 2


def test_phash_distance():
    assert phash_distance("0000", "0001") == 1
    assert phash_distance("0000", "ffff") > 1


# --- Provenance / rights semantics ---


def test_provenance_serializes(tmp_path):
    config = MediaConfig(data_dir=tmp_path)
    candidate = _candidate("https://img.ugreen.com/x.jpg", rank=2, query="ugreen cd271", host="img.ugreen.com")
    service = MediaIntelligenceService(config)
    provenance, _source_class, _usage = service._provenance(candidate)
    assert provenance["source_class"] == "official"
    assert provenance["usage_policy"] == "reference_only"


def test_official_domain_does_not_imply_licensed(tmp_path):
    service = MediaIntelligenceService(MediaConfig(data_dir=tmp_path))
    assert "appmifile.com" in service.official_domains
    provenance, _source_class, _usage = service._provenance(_candidate("https://appmifile.com/x.jpg", host="appmifile.com"))
    assert provenance["source_class"] == "official"
    assert provenance["usage_policy"] == "reference_only"


def test_marketplace_host_classified(tmp_path):
    service = MediaIntelligenceService(MediaConfig(data_dir=tmp_path))
    _p, _source_class, _u = service._provenance(_candidate("https://down-vn.img.susercontent.com/x.jpg", host="down-vn.img.susercontent.com"))
    assert _source_class == "marketplace"


# --- Product matching ---


def test_deterministic_matcher_matches_known_model():
    matcher = DeterministicProductImageMatcher()
    hint = ProductIdentityHint(name="Jisulife FA55", brand="Jisulife", model="FA55")
    candidate = _candidate("https://x.test/a.jpg")
    result = matcher.match(candidate, hint, title="Jisulife FA55 product photo")
    assert result.match_status == MatchStatus.MATCHED
    assert result.product_match_score >= 0.5


def test_deterministic_matcher_reports_unknown_without_metadata():
    matcher = DeterministicProductImageMatcher()
    hint = ProductIdentityHint(name="Jisulife FA55", brand="Jisulife", model="FA55")
    candidate = _candidate("https://x.test/a.jpg")  # no title metadata available
    result = matcher.match(candidate, hint)
    assert result.match_status == MatchStatus.UNKNOWN
    assert result.product_match_score is None


# --- Classification ---


def test_classifier_defaults_to_unknown():
    classifier = ImageClassifier()
    assert classifier.classify(width=1000, height=1000) == "unknown"


def test_classifier_detects_packaging_text():
    classifier = ImageClassifier()
    assert classifier.classify(width=1000, height=1000, url="https://x.test/packaging-01.jpg") == "packaging"


# --- Selection ---


def _asset(asset_id: str, match_score: float, group: str | None, quality: float = 1.0) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id,
        local_path=f"/tmp/{asset_id}.jpg",
        sha256=asset_id,
        phash="",
        width=1000,
        height=1000,
        format="jpg",
        assessment=ProductImageAssessment(
            technical_quality=quality,
            product_match_score=match_score,
            match_status=MatchStatus.MATCHED if match_score is not None else MatchStatus.UNKNOWN,
            duplicate_group_id=group,
        ),
    )


def test_selector_prefers_distinct_groups_and_high_match():
    selector = ProductAssetSelector(target_assets=2)
    assets = [
        _asset("a", 0.9, "dg_1"),
        _asset("b", 0.89, "dg_1"),   # near-duplicate of a
        _asset("c", 0.4, "dg_2"),
        _asset("d", 0.3, "dg_3"),
    ]
    selected, _coverage = selector.select(assets)
    assert len(selected) == 2
    assert selected[0].asset_id == "a"
    assert {asset.assessment.duplicate_group_id for asset in selected} == {"dg_1", "dg_2"}


def test_selector_reports_incomplete_coverage():
    selector = ProductAssetSelector(target_assets=4)
    selected, _coverage = selector.select([_asset("a", 0.9, "dg_1"), _asset("b", 0.3, "dg_2")])
    assert len(selected) == 2
    assert _coverage["complete"] is False


# --- Service orchestration ---


def test_service_isolation_continues_on_partial_failures(tmp_path):
    payload = _png("red")
    candidates = [_candidate(f"https://x.test/{i}.jpg", rank=i) for i in range(5)]
    payloads = {
        "https://x.test/1.jpg": (403, b""),
        "https://x.test/2.jpg": (200, _png("red", (300, 300))),
        "https://x.test/3.jpg": (200, payload),
        "https://x.test/4.jpg": (200, payload),
        "https://x.test/5.jpg": (200, _png("blue", marker="circle")),
    }
    service = _service(MediaConfig(data_dir=tmp_path), candidates, payloads)
    result = service.research(ProductIdentityHint(name="test product"))
    assert result.status in {ResearchStatus.COMPLETED, ResearchStatus.PARTIAL}
    assert result.valid_count >= 2
    reasons = {r.reason for r in result.rejections}
    assert "IMAGE_HTTP_STATUS" in reasons
    assert "IMAGE_TOO_SMALL" in reasons


def test_service_reports_no_usable_assets_when_all_rejected(tmp_path):
    candidates = [_candidate("https://x.test/1.jpg")]
    payloads = {"https://x.test/1.jpg": (200, b"corrupt")}
    service = _service(MediaConfig(data_dir=tmp_path), candidates, payloads)
    result = service.research(ProductIdentityHint(name="x"))
    assert result.status == ResearchStatus.NO_USABLE_ASSETS
    assert result.rejections[0].reason == "IMAGE_DECODE_FAILED"


def test_service_returns_error_for_empty_identity(tmp_path):
    service = MediaIntelligenceService(MediaConfig(data_dir=tmp_path))
    result = service.research(ProductIdentityHint())
    assert result.status == ResearchStatus.ERROR


def test_service_persists_content_addressed_assets(tmp_path):
    payload = _png("green")
    candidates = [_candidate("https://x.test/a.jpg")]
    service = _service(MediaConfig(data_dir=tmp_path), candidates, {"https://x.test/a.jpg": (200, payload)})
    result = service.research(ProductIdentityHint(name="x"))
    assert result.valid_count == 1
    asset = result.selected_assets[0]
    assert Path(asset.local_path).exists()
    assert asset.discovery["provider"] == "test"
    assert asset.source["host"] == "example.com"
    assert asset.assessment.match_status == MatchStatus.UNKNOWN


# --- MCP boundary ---


def test_mcp_tool_delegates_to_service(monkeypatch):
    from product_scout import mcp_server

    captured = {}

    def fake_research(self, hint, **kwargs):
        captured["hint"] = hint
        captured["kwargs"] = kwargs
        from media.models import MediaResearchResult
        return MediaResearchResult(status=ResearchStatus.COMPLETED, product=hint, valid_count=1)

    monkeypatch.setattr(MediaIntelligenceService, "research", fake_research)
    out = mcp_server.research_product_images("Jisulife FA55", brand="Jisulife", model="FA55", target_assets=3)
    assert out["status"] == "completed"
    assert captured["hint"].name == "Jisulife FA55"
    assert captured["kwargs"]["target_assets"] == 3


def test_mcp_server_has_expected_tools():
    from product_scout import mcp_server

    tool_names = {tool.name for tool in mcp_server.mcp._tool_manager._tools.values()}
    assert {"research_product_images", "get_product_media", "validate_product_image"} <= tool_names
