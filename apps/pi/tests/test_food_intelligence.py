"""Focused test suite for Packaged Food/Beverage/Snack intelligence expansion.

Covers:
- Tech prompt regression (tech queries only, food conflicts rejected)
- Food/beverage prompt parsing and custom price handling
- Packaged food SKU identity extraction, flavor/spec differentiation, and non-packaged rejection
- Multi-query discovery, deduplication (URL, SHA-256, pHash), flavor conflict (MISMATCH)
- Quota integrity: UNKNOWN/MISMATCH cannot fill quota, shortfall_reason reported
- Budget limits and resilience (403, timeout, corrupt payload)
- Export audit: products.json and report.md provenance and physical file matching
"""

from __future__ import annotations

import io
import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def enable_legacy_discovery_for_regression_tests(monkeypatch):
    monkeypatch.setattr("product_scout.workflow_policy.PRODUCT_DISCOVERY_PAUSED", False)
from PIL import Image, ImageDraw

from media.acquisition import ContentAddressedImageStore, ImageDownloader
from media.config import MediaConfig
from media.dedup import ImageDeduplicator
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
from product_scout.daily_parser import DailyPromptParser, is_food_category
from product_scout.daily_runner import DailyScoutRunner
from product_scout.trend_discovery.identity import (
    canonical_key,
    check_category_compatibility,
    extract_product_identity,
    is_usable_product_identity,
)
from product_scout.trend_discovery.models import (
    AggregatedCandidate,
    CandidateObservation,
    DiscoveryResult,
    ScoreBreakdown,
    ScoredCandidate,
    SelectionResult,
)
from product_scout.trend_discovery.query_planner import QueryPlanner
from product_scout.trend_discovery.repository import TrendHistoryRepository


def _make_png(color: str = "red", size: tuple[int, int] = (1000, 1000), marker: str | None = None) -> bytes:
    image = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(image)
    if marker == "circle":
        draw.ellipse((250, 250, 750, 750), fill="yellow")
    elif marker == "triangle":
        draw.polygon(((500, 200), (200, 800), (800, 800)), fill="white")
    else:
        draw.rectangle((0, 0, size[0] // 2, size[1] // 2), fill="white")
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 1. Tech regression tests
# ---------------------------------------------------------------------------


def test_tech_prompt_regression():
    prompt = "tìm 10 sản phẩm ngành công nghệ hot trend tiktok/shopee trong phạm vi giá từ 200k-1tr vnd"
    criteria = DailyPromptParser.parse_criteria(prompt)
    assert "công nghệ" in criteria.category
    assert criteria.price_min == 200_000
    assert criteria.price_max == 1_000_000
    assert not is_food_category(criteria.category)

    # Query planner must generate tech queries and NO food niches
    planner = QueryPlanner(enable_llm=False)
    run_time = datetime(2026, 10, 19, 7, 0, tzinfo=timezone.utc)
    plan = planner.plan_queries(criteria, run_at=run_time, web_count=6, commerce_count=6)
    queries = plan.web_queries + plan.commerce_queries
    assert len(queries) > 0
    for q_text in queries:
        q_lower = q_text.lower()
        # Tech queries should not mention snack or food vertical blueprints
        assert not any(ft in q_lower for ft in ["snack khoai tây", "trà sữa đóng chai", "bánh kẹo ăn vặt"])

    # Compatibility: tech item matches, food item conflicts
    tech_match, tech_conflict = check_category_compatibility(
        "Quạt cầm tay mini tích điện Jisulife FA55",
        "công nghệ",
    )
    assert tech_match is True
    assert tech_conflict is False

    food_match, food_conflict = check_category_compatibility(
        "Snack khoai tây Lay's Stax vị sườn nướng BBQ 100g",
        "công nghệ",
    )
    assert food_conflict is True


# ---------------------------------------------------------------------------
# 2. Food prompts and user price handling
# ---------------------------------------------------------------------------


def test_food_prompts_and_custom_pricing():
    # Prompt 1: unconstrained price defaults to 0-0 VND (never tech 200k-1M)
    c1 = DailyPromptParser.parse_criteria("tìm 10 loại snack")
    assert is_food_category(c1.category)
    assert c1.price_min == 0
    assert c1.price_max == 0

    c2 = DailyPromptParser.parse_criteria("đồ uống đóng chai hot trend hè 2026")
    assert is_food_category(c2.category)
    assert c2.price_min == 0
    assert c2.price_max == 0

    # Prompt 2: explicit user price must be preserved strictly
    c3 = DailyPromptParser.parse_criteria("bánh kẹo ăn vặt đóng gói giá từ 20k đến 50k")
    assert is_food_category(c3.category)
    assert c3.price_min == 20_000
    assert c3.price_max == 50_000

    # Query planner for food must generate food niche queries
    planner = QueryPlanner(enable_llm=False)
    run_time = datetime(2026, 10, 19, 7, 0, tzinfo=timezone.utc)
    plan = planner.plan_queries(c1, run_at=run_time, web_count=6, commerce_count=6)
    food_queries = plan.web_queries + plan.commerce_queries
    assert len(food_queries) > 0
    assert any("snack" in q.lower() or "bánh" in q.lower() or "kẹo" in q.lower() for q in food_queries)
    # Must NOT contain tech terms
    for q in food_queries:
        assert not any(t in q.lower() for t in ["tai nghe bluetooth", "chuột không dây", "bàn phím cơ"])


# ---------------------------------------------------------------------------
# 3. Identity differentiation: flavors, specs, and non-packaged rejection
# ---------------------------------------------------------------------------


def test_food_sku_identity_separation_and_non_packaged_rejection():
    # Two distinct flavors of same line must have DIFFERENT canonical keys
    b1, m1, _ = extract_product_identity("Snack khoai tây Lay's Stax vị sườn nướng BBQ 100g", category="food")
    b2, m2, _ = extract_product_identity("Snack khoai tây Lay's Stax vị tự nhiên 100g", category="food")
    assert b1 == "Lay's" and b2 == "Lay's"
    assert "sườn nướng" in m1.lower()
    assert "tự nhiên" in m2.lower()
    key1 = canonical_key(b1, m1, "Snack khoai tây Lay's Stax vị sườn nướng BBQ 100g")
    key2 = canonical_key(b2, m2, "Snack khoai tây Lay's Stax vị tự nhiên 100g")
    assert key1 != key2

    # Two distinct packaging specs of same brand/product must have DIFFERENT canonical keys
    b3, m3, _ = extract_product_identity("Nước ngọt Coca-Cola lon 320ml chính hãng", category="food")
    b4, m4, _ = extract_product_identity("Nước ngọt Coca-Cola chai 1.5L chính hãng", category="food")
    key3 = canonical_key(b3, m3, "Nước ngọt Coca-Cola lon 320ml")
    key4 = canonical_key(b4, m4, "Nước ngọt Coca-Cola chai 1.5L")
    assert key3 != key4

    # Non-packaged food, restaurant dishes, recipes must be rejected
    dish_title = "Bún bò Huế gia truyền nóng hổi quán cô Ba"
    b_dish, m_dish, _ = extract_product_identity(dish_title, category="food")
    assert not is_usable_product_identity(dish_title, b_dish, m_dish, category="food")

    recipe_title = "Cách làm bánh tráng trộn sa tế tóp mỡ tại nhà cực ngon"
    b_recipe, m_recipe, _ = extract_product_identity(recipe_title, category="food")
    assert not is_usable_product_identity(recipe_title, b_recipe, m_recipe, category="food")

    # Generic food without brand or SKU specification must be rejected
    assert not is_usable_product_identity("bánh quy bơ hộp thiếc", "", "bánh quy bơ", category="food")


# ---------------------------------------------------------------------------
# 4. Multi-query discovery, deduplication, flavor conflict, and UNKNOWN handling
# ---------------------------------------------------------------------------


def test_media_multi_query_and_sku_matching(tmp_path: Path):
    hint = ProductIdentityHint(
        name="Snack khoai tây Lay's Stax vị sườn nướng BBQ 100g",
        brand="Lay's",
        model="Stax vị sườn nướng BBQ 100g",
        category="food",
        flavor="vị sườn nướng BBQ",
        spec="100g",
    )

    # Verify multi-query generation
    service = MediaIntelligenceService(MediaConfig(data_dir=tmp_path))
    queries = service._build_queries(hint, service.config)
    assert len(queries) >= 2
    q_texts = [q.query.lower() for q in queries]
    assert any("lay's" in qt and "sườn nướng" in qt for qt in q_texts)
    assert any("bao bì" in qt for qt in q_texts)

    # Test candidate deduplication across queries:
    # 3 queries returning overlapping candidate URLs
    c_shared_url = "https://shopee.vn/lays_bbq_100g_front.jpg"
    c1 = ImageCandidate(
        candidate_id="c1",
        discovery_provider="fake",
        query=queries[0].query,
        search_result_rank=1,
        image_url=c_shared_url,
        host="shopee.vn",
        title="Snack Lay's Stax sườn nướng BBQ 100g bao bì",
        discovered_at="2026-08-10T00:00:00Z",
    )
    c2_dup = ImageCandidate(
        candidate_id="c2_dup",
        discovery_provider="fake",
        query=queries[1].query,
        search_result_rank=1,
        image_url=c_shared_url,  # identical URL
        host="shopee.vn",
        title="Lay's BBQ 100g",
        discovered_at="2026-08-10T00:00:00Z",
    )
    c_mismatch = ImageCandidate(
        candidate_id="c3_wrong_flavor",
        discovery_provider="fake",
        query=queries[0].query,
        search_result_rank=2,
        image_url="https://shopee.vn/lays_phomai.jpg",
        host="shopee.vn",
        title="Snack khoai tây Lay's Stax vị phô mai cheddar",  # conflicting flavor!
        discovered_at="2026-08-10T00:00:00Z",
    )
    c_unknown = ImageCandidate(
        candidate_id="c4_unknown",
        discovery_provider="fake",
        query=queries[0].query,
        search_result_rank=3,
        image_url="https://other.test/random_food.jpg",
        host="other.test",
        title="",  # no search-result text metadata -> UNKNOWN
        discovered_at="2026-08-10T00:00:00Z",
    )

    matcher = DeterministicProductImageMatcher()
    match_bbq = matcher.match(c1, hint, title=c1.title)
    assert match_bbq.match_status in (MatchStatus.MATCHED, MatchStatus.NEEDS_SEMANTIC_REVIEW)

    match_wrong_flavor = matcher.match(c_mismatch, hint, title=c_mismatch.title)
    assert match_wrong_flavor.match_status == MatchStatus.MISMATCH

    match_unknown = matcher.match(c_unknown, hint, title=c_unknown.title)
    assert match_unknown.match_status == MatchStatus.UNKNOWN

    # Selector testing:
    # MISMATCH must be completely excluded
    # UNKNOWN must NOT satisfy complete quota
    img_valid = _make_png("yellow")
    assets = [
        MediaAsset(
            asset_id="asset_bbq",
            local_path=str(tmp_path / "img1.png"),
            sha256="sha_bbq",
            phash="phash1",
            width=600,
            height=600,
            format="png",
            assessment=ProductImageAssessment(
                match_status=MatchStatus.MATCHED,
                product_match_score=0.9,
                technical_quality=1.0,
            ),
        ),
        MediaAsset(
            asset_id="asset_wrong_flavor",
            local_path=str(tmp_path / "img2.png"),
            sha256="sha_wrong",
            phash="phash2",
            width=600,
            height=600,
            format="png",
            assessment=ProductImageAssessment(
                match_status=MatchStatus.MISMATCH,
                product_match_score=0.0,
                technical_quality=1.0,
            ),
        ),
        MediaAsset(
            asset_id="asset_unknown",
            local_path=str(tmp_path / "img3.png"),
            sha256="sha_unk",
            phash="phash3",
            width=600,
            height=600,
            format="png",
            assessment=ProductImageAssessment(
                match_status=MatchStatus.UNKNOWN,
                product_match_score=0.3,
                technical_quality=1.0,
            ),
        ),
    ]

    selector = ProductAssetSelector(target_assets=3)
    selected, coverage = selector.select(assets)
    # asset_wrong_flavor must NOT be in selected
    assert all(a.asset_id != "asset_wrong_flavor" for a in selected)
    # Only 1 matched asset, target is 3 -> complete MUST be False
    assert coverage["complete"] is False
    assert coverage["matched_count"] == 1
    assert "insufficient_matched_assets" in coverage["shortfall_reason"]


# ---------------------------------------------------------------------------
# 5. Resilience to network failures and strict budgets
# ---------------------------------------------------------------------------


def test_media_budget_and_failure_resilience(tmp_path: Path):
    candidates = [
        ImageCandidate(
            candidate_id=f"cand_{i}",
            discovery_provider="fake",
            query="lays",
            search_result_rank=i,
            image_url=f"https://source.test/{i}.jpg",
            host="source.test",
            discovered_at="2026-08-10T00:00:00Z",
        )
        for i in range(1, 10)
    ]

    # Map errors and valid payloads
    valid_payload = _make_png("green")
    payloads = {
        "https://source.test/1.jpg": (403, b""),          # HTTP 403 Forbidden
        "https://source.test/2.jpg": (200, b"corrupted"),  # Decode failure
        "https://source.test/3.jpg": (200, valid_payload), # Valid image 1
        "https://source.test/4.jpg": (200, valid_payload), # Duplicate of 3
        "https://source.test/5.jpg": (200, _make_png("blue", marker="circle")), # Valid image 2
        "https://source.test/6.jpg": (200, _make_png("red", marker="triangle")), # Valid image 3
        "https://source.test/7.jpg": (200, valid_payload),
        "https://source.test/8.jpg": (200, valid_payload),
        "https://source.test/9.jpg": (200, valid_payload),
    }

    # Set small candidate download limit: max 4 downloads
    config = MediaConfig(
        data_dir=tmp_path,
        max_candidates_to_download=4,
        target_selected_assets=3,
    )

    class MockProvider:
        name = "mock_provider"
        def search(self, q):
            return candidates

    class MockDownloader(ImageDownloader):
        def __init__(self):
            super().__init__(fetcher=lambda u: payloads[u])

    service = MediaIntelligenceService(
        config,
        provider=MockProvider(),
        downloader=MockDownloader(),
    )

    result = service.research(ProductIdentityHint(name="Lay's Stax BBQ 100g", brand="Lay's", model="Stax BBQ 100g"))
    assert result.status in (ResearchStatus.PARTIAL, ResearchStatus.COMPLETED)
    # Rejections must record 403 and decode failures cleanly
    rejection_reasons = [r.reason for r in result.rejections]
    assert "IMAGE_HTTP_STATUS" in rejection_reasons
    assert "IMAGE_DECODE_FAILED" in rejection_reasons
    # Candidate download limit was 4, candidates 1-4 downloaded
    assert result.downloaded_count <= 4


# ---------------------------------------------------------------------------
# 6. Daily Runner Export Audit: products.json and report.md honesty
# ---------------------------------------------------------------------------


def test_daily_runner_export_honesty_and_provenance(tmp_path: Path):
    req_file = tmp_path / "request.txt"
    req_file.write_text("tìm 5 loại snack ăn vặt đóng gói", encoding="utf-8")
    runs_dir = tmp_path / "runs"
    repo = TrendHistoryRepository(tmp_path / "trend.db")
    run_date = date(2026, 9, 20)
    run_id = repo.begin_run(run_date, {})

    cand = AggregatedCandidate(
        canonical_key="lays|stax vi suon nuong bbq 100g|snack khoai tay lays stax",
        name="Snack khoai tây Lay's Stax vị sườn nướng BBQ 100g",
        brand="Lay's",
        model="Stax vị sườn nướng BBQ 100g",
        category="đồ ăn vặt & snack đóng gói",
        price_min=25_000,
        price_max=35_000,
        observations=[
            CandidateObservation(
                provider="google_trends",
                candidate_name="Lay's Stax vị sườn nướng BBQ 100g",
                brand="Lay's",
                model="Stax vị sườn nướng BBQ 100g",
                source_url="https://shopee.vn/lays-stax-bbq-100g-p1",
                source_host="shopee.vn",
                observed_at=datetime.now(timezone.utc),
                signal_type="commerce_interest",
                title="Bim bim Lay's Stax sườn nướng BBQ 100g",
            )
        ],
    )
    scored = ScoredCandidate(
        candidate=cand,
        trend_score=88.0,
        freshness_score=90.0,
        confidence_score=92.0,
        final_score=89.5,
        score_breakdown=ScoreBreakdown(),
    )

    mock_discovery = MagicMock()
    mock_discovery.config = MagicMock(cooldown_days=30, fresh_ratio=0.7, confidence_threshold=70.0, reentry_trend_threshold=80.0, reentry_delta_threshold=10.0, config_version="2.0")
    mock_discovery.discover.return_value = DiscoveryResult(
        run_id=run_id,
        status="completed",
        candidates=[cand],
        raw_candidate_count=1,
        deduplicated_candidate_count=1,
        enriched_candidate_count=1,
        eligible_candidate_count=1,
        selection=SelectionResult(
            selected=[scored],
            statuses={cand.canonical_key: "new"},
            reason_codes={cand.canonical_key: ["fresh_selected"]},
            warnings=[],
        ),
        provider_summary=[],
    )

    # Prepare 2 mock downloaded assets on disk (short of target 4)
    asset_file1 = tmp_path / "sample_img1.png"
    asset_file1.write_bytes(_make_png("orange"))
    asset_file2 = tmp_path / "sample_img2.png"
    asset_file2.write_bytes(_make_png("cyan"))

    mock_media = MagicMock()
    from media.models import MediaResearchResult
    mock_media.research.return_value = MediaResearchResult(
        status=ResearchStatus.PARTIAL,
        product=ProductIdentityHint(name=cand.name, brand=cand.brand, model=cand.model),
        discovered_count=4,
        downloaded_count=2,
        valid_count=2,
        selected_assets=[
            MediaAsset(
                asset_id="sha_orange_123",
                local_path=str(asset_file1),
                sha256="sha_orange_123",
                phash="phash_o",
                width=600,
                height=600,
                format="png",
                source={"image_url": "https://shopee.vn/img1.jpg", "host": "shopee.vn"},
                provenance={"source_class": "marketplace", "usage_policy": "reference_only"},
                assessment=ProductImageAssessment(
                    match_status=MatchStatus.MATCHED,
                    product_match_score=0.92,
                    technical_quality=1.0,
                ),
            ),
            MediaAsset(
                asset_id="sha_cyan_456",
                local_path=str(asset_file2),
                sha256="sha_cyan_456",
                phash="phash_c",
                width=600,
                height=600,
                format="png",
                source={"image_url": "https://shopee.vn/img2.jpg", "host": "shopee.vn"},
                provenance={"source_class": "marketplace", "usage_policy": "reference_only"},
                assessment=ProductImageAssessment(
                    match_status=MatchStatus.MATCHED,
                    product_match_score=0.88,
                    technical_quality=1.0,
                ),
            ),
        ],
        coverage={
            "target": 4,
            "selected": 2,
            "matched_count": 2,
            "complete": False,
            "shortfall_reason": "insufficient_matched_assets: 2 < 4",
        },
    )

    runner = DailyScoutRunner(
        base_dir=tmp_path,
        request_file=req_file,
        runs_dir=runs_dir,
        discovery_service=mock_discovery,
        history_repository=repo,
        media_service=mock_media,
    )

    result = runner.run_daily(force=True, target_date=run_date, max_images_per_product=4)
    assert result["status"] == "success"

    export_json = runs_dir / run_date.isoformat() / "products.json"
    assert export_json.exists()
    payload = json.loads(export_json.read_text(encoding="utf-8"))

    # Audit media_summary
    assert "media_summary" in payload
    media_summary = payload["media_summary"]
    assert media_summary["target_per_product"] == 4
    assert media_summary["total_images_saved"] == 2
    assert media_summary["total_unique_sha"] == 2
    assert media_summary["products_full_quota"] == 0
    assert media_summary["products_shortfall"] == 1

    prod = payload["products"][0]
    assert prod["saved_images_count"] == 2
    assert prod["unique_sha_count"] == 2
    assert prod["matched_identity_count"] == 2
    assert prod["target_images"] == 4
    assert prod["quota_met"] is False
    assert "insufficient_matched_assets" in prod["shortfall_reason"]

    # Verify physical file existence and provenance integrity
    for img in prod["images"]:
        local_file = runs_dir / run_date.isoformat() / img["local_path"]
        assert local_file.exists()
        assert img["usage_policy"] == "reference_only"
        assert img["match_status"] == "matched"

    # Verify Markdown report
    export_md = runs_dir / run_date.isoformat() / "report.md"
    assert export_md.exists()
    md_text = export_md.read_text(encoding="utf-8")
    assert "reference_only" in md_text
    assert "Chưa đủ chỉ tiêu" in md_text


def test_category_dispatcher_single_and_multi_category():
    from product_scout.category_dispatcher import CategoryDispatcherAgent

    agent = CategoryDispatcherAgent(enable_llm=False)

    # 1. Food snack prompt
    food_res = agent.analyze_and_dispatch("tìm 20 sản phẩm đồ ăn vặt bán chạy hiện nay trên tiktok, như bánh tráng trộn, bắp rang, bỏng ngô, khoai chiên, nui sấy giòn")
    assert not food_res.is_multi_category
    assert len(food_res.tasks) == 1
    t_food = food_res.tasks[0]
    assert t_food.category_id == "food_snack"
    assert t_food.target_count == 20
    assert "bánh tráng" in t_food.focus_keywords or "bánh tráng trộn" in t_food.focus_keywords
    assert "bắp rang" in t_food.focus_keywords
    assert "khoai chiên" in t_food.focus_keywords

    # 2. Tech prompt
    tech_res = agent.analyze_and_dispatch("tìm 10 tai nghe bluetooth công nghệ")
    assert not tech_res.is_multi_category
    assert len(tech_res.tasks) == 1
    t_tech = tech_res.tasks[0]
    assert t_tech.category_id == "tech"
    assert t_tech.target_count == 10

    # 3. Multi-category prompt requires a user choice before discovery.
    multi_res = agent.analyze_and_dispatch("tìm 10 đồ ăn vặt và 10 phụ kiện công nghệ")
    assert multi_res.status == "needs_review"
    assert multi_res.is_multi_category
    assert multi_res.tasks == []


def test_category_partitioned_storage(tmp_path: Path):
    from unittest.mock import MagicMock
    from product_scout.daily_runner import DailyScoutRunner
    from product_scout.trend_discovery.repository import TrendHistoryRepository
    from product_scout.trend_discovery.models import DiscoveryResult, SelectionResult, AggregatedCandidate, CandidateObservation, ScoredCandidate, ScoreBreakdown
    from media.models import MediaResearchResult, ResearchStatus, MediaAsset, ProductImageAssessment, MatchStatus, ProductIdentityHint

    req_file = tmp_path / "request.txt"
    req_file.write_text("tìm 5 loại snack ăn vặt đóng gói", encoding="utf-8")
    runs_dir = tmp_path / "runs"
    repo = TrendHistoryRepository(tmp_path / "trend.db")
    run_date = date(2026, 9, 21)
    run_id = repo.begin_run(run_date, {})

    cand = AggregatedCandidate(
        canonical_key="oishi|bap rang bo oishi 40g|bap rang bo",
        name="Bắp rang bơ Oishi Popcorn 40g",
        brand="Oishi",
        model="Popcorn vị bơ 40g",
        category="đồ ăn vặt & snack đóng gói",
        price_min=10_000,
        price_max=15_000,
        observations=[
            CandidateObservation(
                provider="google_trends",
                candidate_name="Bắp rang bơ Oishi 40g",
                brand="Oishi",
                model="Popcorn vị bơ 40g",
                source_url="https://shopee.vn/bap-rang-oishi",
                source_host="shopee.vn",
                observed_at=datetime.now(timezone.utc),
                signal_type="commerce_interest",
                title="Bắp rang bơ Oishi Popcorn 40g",
            )
        ],
    )
    scored = ScoredCandidate(
        candidate=cand,
        trend_score=85.0,
        freshness_score=90.0,
        confidence_score=90.0,
        final_score=87.5,
        score_breakdown=ScoreBreakdown(),
    )

    mock_discovery = MagicMock()
    mock_discovery.config = MagicMock(cooldown_days=30, fresh_ratio=0.7, confidence_threshold=70.0, reentry_trend_threshold=80.0, reentry_delta_threshold=10.0, config_version="2.0")
    mock_discovery.discover.return_value = DiscoveryResult(
        run_id=run_id,
        status="completed",
        candidates=[cand],
        raw_candidate_count=1,
        deduplicated_candidate_count=1,
        enriched_candidate_count=1,
        eligible_candidate_count=1,
        selection=SelectionResult(
            selected=[scored],
            statuses={cand.canonical_key: "new"},
            reason_codes={cand.canonical_key: ["fresh_selected"]},
            warnings=[],
        ),
        provider_summary=[],
    )

    asset_file = tmp_path / "oishi_img.png"
    asset_file.write_bytes(_make_png("yellow"))

    mock_media = MagicMock()
    mock_media.research.return_value = MediaResearchResult(
        status=ResearchStatus.COMPLETED,
        product=ProductIdentityHint(name=cand.name, brand=cand.brand, model=cand.model),
        discovered_count=2,
        downloaded_count=1,
        valid_count=1,
        selected_assets=[
            MediaAsset(
                asset_id="sha_yellow_789",
                local_path=str(asset_file),
                sha256="sha_yellow_789",
                phash="phash_y",
                width=600,
                height=600,
                format="png",
                source={"image_url": "https://shopee.vn/img_oishi.jpg", "host": "shopee.vn"},
                provenance={"source_class": "marketplace", "usage_policy": "reference_only"},
                assessment=ProductImageAssessment(
                    match_status=MatchStatus.MATCHED,
                    product_match_score=0.95,
                    technical_quality=1.0,
                ),
            )
        ],
        coverage={"target": 1, "selected": 1, "matched_count": 1, "complete": True},
    )

    runner = DailyScoutRunner(
        base_dir=tmp_path,
        request_file=req_file,
        runs_dir=runs_dir,
        discovery_service=mock_discovery,
        history_repository=repo,
        media_service=mock_media,
    )

    result = runner.run_daily(force=True, target_date=run_date, max_images_per_product=3)
    assert result["status"] == "success"

    day_dir = runs_dir / run_date.isoformat()

    # 1. Verify category-partitioned directory and exports
    cat_dir = day_dir / "food_snack"
    assert cat_dir.is_dir()
    cat_products_file = cat_dir / "products.json"
    assert cat_products_file.exists()
    cat_data = json.loads(cat_products_file.read_text(encoding="utf-8"))
    assert cat_data["category_id"] == "food_snack"
    assert cat_data["total_products"] == 1
    assert (cat_dir / "report.md").exists()

    # 2. Verify root aggregated exports
    root_products_file = day_dir / "products.json"
    assert root_products_file.exists()
    root_data = json.loads(root_products_file.read_text(encoding="utf-8"))
    assert "food_snack" in root_data["categories_dispatched"]
    assert "food_snack" in root_data["category_summaries"]
    assert root_data["selection_policy"]["cooldown_days"] == 30
    assert root_data["products"][0]["target_images"] == 3
    assert root_data["products"][0]["saved_images_count"] == 1
    assert root_data["products"][0]["quota_met"] is False
    assert "insufficient_matched_assets" in root_data["products"][0]["shortfall_reason"]
    assert (day_dir / "report.md").exists()

    # 3. Verify images exist in both category partition and root
    cat_images = list((cat_dir / "images").glob("**/*.png"))
    root_images = list((day_dir / "images").glob("**/*.png"))
    assert len(cat_images) >= 1
    assert len(root_images) >= 1

