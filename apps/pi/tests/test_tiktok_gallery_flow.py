from datetime import datetime, timezone

from PIL import Image

from media import MediaConfig, MediaIntelligenceService, ProductIdentityHint
from media.models import ImageCandidate
from media.web_acquisition.tiktok_gallery import extract_tiktok_gallery_candidates
from product_scout.daily_runner import gallery_candidates_for_selected
from product_scout.trend_discovery.models import AggregatedCandidate, CandidateObservation


PDP_URL = "https://shop.tiktok.com/vn/pdp/1736093592588027649"
NAME = "Bánh Gấu Nhân Kem - Thơm Ngon Giòn Rụm - Ngon Mêly"


def test_pdp_gallery_excludes_reviews_and_deduplicates_thumbnail_urls():
    page = f"""
    <img class="cursor-zoom-in" src="https://img.test/a.webp?x=1&amp;y=2" alt="{NAME}">
    <img class="cursor-zoom-in" src="https://img.test/b.webp" alt="{NAME}">
    <img class="thumbnail" src="https://img.test/a.webp?x=1&amp;y=2">
    <img alt="review image 0" src="https://img.test/review.webp">
    """

    rows = extract_tiktok_gallery_candidates(PDP_URL, NAME, fetcher=lambda url: page)

    assert [row.image_url for row in rows] == [
        "https://img.test/a.webp?x=1&y=2",
        "https://img.test/b.webp",
    ]
    assert all(row.source_page_url == PDP_URL for row in rows)
    assert all(row.title == NAME for row in rows)


def test_media_research_uses_page_gallery_without_searching_elsewhere(tmp_path):
    image_path = tmp_path / "picture.png"
    Image.effect_noise((800, 800), 80).convert("RGB").save(image_path)
    payload = image_path.read_bytes()

    class FailingSearch:
        def search(self, query):
            raise AssertionError("Image search must not run when gallery candidates exist")

    candidate = ImageCandidate(
        candidate_id="gallery_1",
        discovery_provider="tiktok_pdp_gallery",
        query=NAME,
        image_url="https://img.test/product.png",
        source_page_url=PDP_URL,
        host="img.test",
        title=NAME,
        discovered_at=datetime(2026, 10, 20, tzinfo=timezone.utc).isoformat(),
    )
    service = MediaIntelligenceService(
        MediaConfig(data_dir=tmp_path / "media", target_selected_assets=1),
        provider=FailingSearch(),
    )
    service.downloader.fetcher = lambda url: (200, payload)

    result = service.research(ProductIdentityHint(name=NAME), target_assets=1, seed_candidates=[candidate])

    assert result.status.value == "completed"
    assert len(result.selected_assets) == 1
    assert result.selected_assets[0].source["page_url"] == PDP_URL


def test_selected_category_listing_passes_its_pdp_gallery_to_media():
    observation = CandidateObservation(
        provider="tiktok_snacks_category",
        candidate_name=NAME,
        source_url=PDP_URL,
        source_host="shop.tiktok.com",
        observed_at=datetime(2026, 10, 20, tzinfo=timezone.utc),
        signal_type="commerce_listing",
    )
    product = AggregatedCandidate(canonical_key="banh gau", name=NAME, observations=[observation])
    page = f'<img class="cursor-zoom-in" src="https://img.test/product.webp" alt="{NAME}">'

    rows = gallery_candidates_for_selected(product, fetcher=lambda url: page)

    assert len(rows) == 1
    assert rows[0].source_page_url == PDP_URL
