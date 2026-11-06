"""Focused regression test suite for the Two-Category Dispatcher and Runner."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def enable_legacy_discovery_for_regression_tests(monkeypatch):
    monkeypatch.setattr("product_scout.workflow_policy.PRODUCT_DISCOVERY_PAUSED", False)

from product_scout.category_dispatcher import (
    CategoryDispatcherAgent,
    extract_counts,
    extract_non_count_spans,
    extract_price_and_spans,
)
from product_scout.daily_parser import ParsedCriteria
from product_scout.daily_runner import DailyScoutRunner
from product_scout.trend_discovery.backfill import backfill_daily_runs
from product_scout.trend_discovery.config import TrendDiscoveryConfig
from product_scout.trend_discovery.models import (
    AggregatedCandidate,
    DiscoveryResult,
    ProviderRunSummary,
    ScoreBreakdown,
    ScoredCandidate,
    SelectionResult,
)
from product_scout.trend_discovery.repository import TrendHistoryRepository


def _make_scored_candidate(name: str, brand: str, model: str, category: str = "công nghệ") -> ScoredCandidate:
    cand = AggregatedCandidate(
        canonical_key=f"{brand.lower()}::{model.lower()}",
        name=name,
        brand=brand,
        model=model,
        category=category,
    )
    return ScoredCandidate(
        candidate=cand,
        trend_score=85.0,
        freshness_score=90.0,
        confidence_score=95.0,
        final_score=88.0,
        score_breakdown=ScoreBreakdown(),
    )


# 1. Single Food Category
def test_single_food_category_dispatch():
    dispatcher = CategoryDispatcherAgent(enable_llm=False)
    res = dispatcher.analyze_and_dispatch("tìm 10 loại snack và đồ ăn vặt đóng gói")
    assert res.status == "dispatched"
    assert not res.is_multi_category
    assert len(res.tasks) == 1
    task = res.tasks[0]
    assert task.category_id == "food_snack"
    assert task.target_count == 10
    assert task.criteria.price_min == 0
    assert task.criteria.price_max == 0  # food defaults to 0-0 VND


# 2. Single Tech Category
def test_single_tech_category_dispatch():
    dispatcher = CategoryDispatcherAgent(enable_llm=False)
    res = dispatcher.analyze_and_dispatch("tìm 10 sản phẩm công nghệ tai nghe bluetooth")
    assert res.status == "dispatched"
    assert not res.is_multi_category
    assert len(res.tasks) == 1
    task = res.tasks[0]
    assert task.category_id == "tech"
    assert task.target_count == 10
    assert task.criteria.price_min == 200_000
    assert task.criteria.price_max == 1_000_000  # tech default budget


# 3. Mixed categories require the operator to choose one before discovery.
def test_mixed_prompt_explicit_counts_needs_review():
    dispatcher = CategoryDispatcherAgent(enable_llm=False)
    res = dispatcher.analyze_and_dispatch("tìm 20 snack ăn vặt và 5 tai nghe bluetooth")
    assert res.status == "needs_review"
    assert res.is_multi_category
    assert res.tasks == []
    assert "một ngành hàng" in res.message


# 4. The runner must halt before calling discovery for a mixed request.
def test_mixed_prompt_halts_before_discovery(tmp_path: Path):
    dispatcher = CategoryDispatcherAgent(enable_llm=False)
    res = dispatcher.analyze_and_dispatch("tìm 20 sản phẩm đồ ăn và công nghệ hot trend")
    assert res.status == "needs_review"
    assert res.is_multi_category
    assert res.tasks == []

    discovery = MagicMock()
    runner = DailyScoutRunner(
        base_dir=tmp_path,
        request_file=tmp_path / "request.txt",
        runs_dir=tmp_path / "runs",
        discovery_service=discovery,
        history_repository=TrendHistoryRepository(tmp_path / "trend.db"),
        media_service=MagicMock(),
    )
    result = runner.run_daily(
        force=True,
        target_date=date(2026, 10, 20),
        prompt="tìm 20 sản phẩm đồ ăn và công nghệ hot trend",
    )
    assert result["status"] == "needs_review"
    discovery.discover.assert_not_called()


# 5. Unsupported Category Detection
@pytest.mark.parametrize("prompt", [
    "tìm 10 cái áo thun thời trang hot trend",
    "tìm 5 cuốn sách hay nhất",
    "tìm 10 bộ mỹ phẩm dưỡng da skincare",
    "tìm 10 cái áo thun và 5 tai nghe",
])
def test_unsupported_category_rejection(prompt: str):
    dispatcher = CategoryDispatcherAgent(enable_llm=False)
    res = dispatcher.analyze_and_dispatch(prompt)
    assert res.status == "unsupported_category"
    assert len(res.tasks) == 0
    assert res.original_prompt == prompt
    assert res.metadata.classification_method == "heuristic"


# 6. Price Tokens Not Confused With Product Counts
def test_prices_and_units_not_mistaken_for_counts():
    dispatcher = CategoryDispatcherAgent(enable_llm=False)

    # Under 50k should not yield count=50
    res1 = dispatcher.analyze_and_dispatch("tìm đồ ăn vặt dưới 50k")
    assert res1.tasks[0].target_count == 10  # default count
    assert res1.tasks[0].criteria.price_max == 50_000

    # Under 2 million should not yield count=2
    res2 = dispatcher.analyze_and_dispatch("tìm 10 sản phẩm công nghệ under 2 million VND")
    assert res2.tasks[0].target_count == 10
    assert res2.tasks[0].criteria.price_max == 2_000_000

    # Range 200-500k
    res3 = dispatcher.analyze_and_dispatch("tìm 10 sản phẩm công nghệ giá từ 200k đến 500k")
    assert res3.tasks[0].target_count == 10
    assert res3.tasks[0].criteria.price_min == 200_000
    assert res3.tasks[0].criteria.price_max == 500_000


# 7. Units, Weights, Years, Battery Specs Ignored as Counts
def test_non_count_spans_masked():
    text = "tìm 5 snack bánh quy 100g trà xanh 350ml củ sạc 65w pin 10000mah smartwatch 2026 đánh giá 5 sao"
    _, _, _, price_spans = extract_price_and_spans(text)
    non_count_spans = extract_non_count_spans(text, price_spans)
    total_count, _ = extract_counts(text, non_count_spans)
    assert total_count == 5


# 8. Budget Preservation Across Categories
def test_budget_preservation():
    dispatcher = CategoryDispatcherAgent(enable_llm=False)

    # Explicit food budget
    res_food = dispatcher.analyze_and_dispatch("tìm 10 loại bánh tráng giá dưới 50k")
    assert res_food.tasks[0].criteria.price_min == 0
    assert res_food.tasks[0].criteria.price_max == 50_000

    # Default food budget (0 - 0)
    res_food_default = dispatcher.analyze_and_dispatch("tìm 10 loại bánh tráng")
    assert res_food_default.tasks[0].criteria.price_min == 0
    assert res_food_default.tasks[0].criteria.price_max == 0

    # Explicit tech budget
    res_tech = dispatcher.analyze_and_dispatch("tìm 10 tai nghe bluetooth giá từ 300k đến 600k")
    assert res_tech.tasks[0].criteria.price_min == 300_000
    assert res_tech.tasks[0].criteria.price_max == 600_000

    # Default tech budget (200k - 1M)
    res_tech_default = dispatcher.analyze_and_dispatch("tìm 10 tai nghe bluetooth")
    assert res_tech_default.tasks[0].criteria.price_min == 200_000
    assert res_tech_default.tasks[0].criteria.price_max == 1_000_000


# 9. LLM Disambiguation and Fallback
def test_llm_cannot_split_a_multi_category_prompt():
    def mock_llm(system: str, user: str) -> dict:
        return {
            "status": "supported",
            "categories": [
                {"id": "food_snack", "limit": 7},
                {"id": "tech", "limit": 3},
            ],
        }

    dispatcher = CategoryDispatcherAgent(enable_llm=True, llm_caller=mock_llm)
    res = dispatcher.analyze_and_dispatch("tìm 10 món quà tặng công nghệ và đồ ăn vặt")
    assert res.status == "needs_review"
    assert res.is_multi_category
    assert res.tasks == []


def test_llm_failure_does_not_enable_a_multi_category_fallback():
    def mock_failing_llm(system: str, user: str) -> dict | None:
        return None  # simulates timeout or network failure

    dispatcher = CategoryDispatcherAgent(enable_llm=True, llm_caller=mock_failing_llm)
    res = dispatcher.analyze_and_dispatch("tìm 20 sản phẩm đồ ăn và công nghệ")
    assert res.status == "needs_review"
    assert res.is_multi_category
    assert res.tasks == []


def test_llm_invalid_category_fallback():
    def mock_invalid_llm(system: str, user: str) -> dict:
        return {
            "status": "supported",
            "categories": [
                {"id": "unknown_future_category", "limit": 10},
            ],
        }

    dispatcher = CategoryDispatcherAgent(enable_llm=True, llm_caller=mock_invalid_llm)
    res = dispatcher.analyze_and_dispatch("tìm 10 sản phẩm hot trend")
    assert res.status == "dispatched"
    assert res.metadata.classification_method == "fallback"
    assert "invalid_category_returned" in (res.metadata.fallback_reason or "")
    # Falls back to deterministic tech default
    assert res.tasks[0].category_id == "tech"
    assert res.tasks[0].target_count == 10


# 10. Category-Partitioned Output & Metadata Integrity
def test_runner_category_isolated_output(tmp_path: Path):
    req_file = tmp_path / "request.txt"
    req_file.write_text("tìm 2 snack ăn vặt", encoding="utf-8")
    runs_dir = tmp_path / "runs"
    repo = TrendHistoryRepository(tmp_path / "trend.db")

    mock_discovery = MagicMock()
    mock_discovery.config = TrendDiscoveryConfig()

    def mock_discover(criteria: ParsedCriteria, run_date: date):
        cat = criteria.category
        if "ăn vặt" in cat:
            cand = _make_scored_candidate("Bánh tráng trộn Sài Gòn", "Cô Ba", "BTT-50G", category="food_snack")
            cid = "food_snack"
        else:
            cand = _make_scored_candidate("Tai nghe Baseus Bowie WM02", "Baseus", "WM02", category="tech")
            cid = "tech"

        run_id = repo.begin_run(run_date, {"category": cid})
        return DiscoveryResult(
            run_id=run_id,
            status="completed",
            candidates=[],
            raw_candidate_count=5,
            deduplicated_candidate_count=4,
            enriched_candidate_count=3,
            selection=SelectionResult(selected=[cand], statuses={}, reason_codes={}, warnings=[]),
            provider_summary=[ProviderRunSummary(provider="mock_p", status="ok", observation_count=5)],
        )

    mock_discovery.discover.side_effect = mock_discover
    mock_media = MagicMock()

    runner = DailyScoutRunner(
        base_dir=tmp_path,
        request_file=req_file,
        runs_dir=runs_dir,
        discovery_service=mock_discovery,
        history_repository=repo,
        media_service=mock_media,
    )

    target_d = date(2026, 10, 20)
    res = runner.run_daily(force=True, target_date=target_d)
    assert res["status"] == "success"
    assert res["total_products"] == 1

    day_dir = runs_dir / "2026-10-20"

    # Category partitions exist
    food_json = day_dir / "food_snack" / "products.json"
    assert food_json.exists()

    # Category partition payload checks
    food_data = json.loads(food_json.read_text(encoding="utf-8"))
    assert food_data["is_canonical"] is True
    assert food_data["category_id"] == "food_snack"
    assert food_data["products"][0]["category_id"] == "food_snack"

    # Root compatibility export checks
    root_json = day_dir / "products.json"
    assert root_json.exists()
    root_data = json.loads(root_json.read_text(encoding="utf-8"))
    assert root_data["is_compatibility_export"] is True
    assert root_data["canonical_source"] == "category_partitions"

    # Discovery summary belongs to the one selected category.
    assert root_data["discovery_summary"]["raw_candidates"] == 5
    assert root_data["discovery_summary"]["deduplicated_candidates"] == 4
    assert root_data["discovery_summary"]["enriched_candidates"] == 3
    assert len(root_data["provider_summary"]) == 1
    assert root_data["provider_summary"][0]["observation_count"] == 5


# 11. A mixed request is rejected before either category can fail or succeed.
def test_runner_mixed_prompt_does_not_start_partial_category_runs(tmp_path: Path):
    discovery = MagicMock()
    runner = DailyScoutRunner(
        base_dir=tmp_path,
        request_file=tmp_path / "request.txt",
        runs_dir=tmp_path / "runs",
        discovery_service=discovery,
        history_repository=TrendHistoryRepository(tmp_path / "trend.db"),
        media_service=MagicMock(),
    )

    result = runner.run_daily(
        force=True,
        target_date=date(2026, 10, 20),
        prompt="tìm 2 snack ăn vặt và 2 tai nghe",
    )

    assert result["status"] == "needs_review"
    discovery.discover.assert_not_called()


# 12. Explicit counts do not re-enable the removed split behavior.
def test_runner_mixed_prompt_with_explicit_counts_needs_review(tmp_path: Path):
    discovery = MagicMock()
    runner = DailyScoutRunner(
        base_dir=tmp_path,
        request_file=tmp_path / "request.txt",
        runs_dir=tmp_path / "runs",
        discovery_service=discovery,
        history_repository=TrendHistoryRepository(tmp_path / "trend.db"),
        media_service=MagicMock(),
    )

    result = runner.run_daily(
        force=True,
        target_date=date(2026, 10, 20),
        prompt="tìm 20 snack ăn vặt và 5 tai nghe bluetooth",
    )

    assert result["status"] == "needs_review"
    discovery.discover.assert_not_called()


# 13. Backfill Idempotency & No Double Counting with Category Partitions
def test_backfill_prioritizes_partitions_and_prevents_double_counting(tmp_path: Path):
    runs_dir = tmp_path / "daily_runs"
    day_dir = runs_dir / "2026-10-20"

    # Category partition 1: food_snack (1 product)
    food_dir = day_dir / "food_snack"
    food_dir.mkdir(parents=True)
    food_payload = {
        "is_canonical": True,
        "date": "2026-10-20",
        "products": [{"name": "Bánh tráng Cô Ba", "brand": "Cô Ba", "model": "BTT-50G", "category": "food_snack"}],
    }
    (food_dir / "products.json").write_text(json.dumps(food_payload), encoding="utf-8")

    # Category partition 2: tech (1 product)
    tech_dir = day_dir / "tech"
    tech_dir.mkdir(parents=True)
    tech_payload = {
        "is_canonical": True,
        "date": "2026-10-20",
        "products": [{"name": "Tai nghe Baseus", "brand": "Baseus", "model": "WM02", "category": "tech"}],
    }
    (tech_dir / "products.json").write_text(json.dumps(tech_payload), encoding="utf-8")

    # Compatibility root export: contains BOTH products
    compat_payload = {
        "is_compatibility_export": True,
        "canonical_source": "category_partitions",
        "date": "2026-10-20",
        "products": [
            {"name": "Bánh tráng Cô Ba", "brand": "Cô Ba", "model": "BTT-50G", "category": "food_snack"},
            {"name": "Tai nghe Baseus", "brand": "Baseus", "model": "WM02", "category": "tech"},
        ],
    }
    (day_dir / "products.json").write_text(json.dumps(compat_payload), encoding="utf-8")

    repo = TrendHistoryRepository(tmp_path / "trend.db")
    summary = backfill_daily_runs(repo, runs_dir)

    # Must import exactly 2 selections (from food_snack and tech), NOT 4!
    assert summary.selections_imported == 2
    assert summary.files_read == 2  # Only read the 2 canonical partitions, skipped root compatibility export!


# 14. A rejected mixed request writes no SQLite selection or cooldown history.
def test_multi_category_prompt_does_not_write_sqlite_history(tmp_path: Path):
    repo = TrendHistoryRepository(tmp_path / "trend.db")
    discovery = MagicMock()
    runner = DailyScoutRunner(
        base_dir=tmp_path,
        request_file=tmp_path / "request.txt",
        runs_dir=tmp_path / "runs",
        discovery_service=discovery,
        history_repository=repo,
        media_service=MagicMock(),
    )

    result = runner.run_daily(
        force=True,
        target_date=date(2026, 10, 20),
        prompt="tìm 2 snack ăn vặt và 2 tai nghe",
    )

    assert result["status"] == "needs_review"
    discovery.discover.assert_not_called()
    with repo._get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM discovery_runs;").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM daily_selections;").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM product_history;").fetchone()[0] == 0


# 15. Partition Cleanup on Re-Run
def test_partition_cleanup_on_rerun(tmp_path: Path):
    req_file = tmp_path / "request.txt"
    runs_dir = tmp_path / "runs"
    repo = TrendHistoryRepository(tmp_path / "trend.db")

    mock_discovery = MagicMock()
    mock_discovery.config = TrendDiscoveryConfig()

    def mock_discover(criteria: ParsedCriteria, run_date: date):
        cat = criteria.category
        if "ăn vặt" in cat:
            cand = _make_scored_candidate("Bánh tráng trộn", "Cô Ba", "BTT-50G", category="food_snack")
            cid = "food_snack"
        else:
            cand = _make_scored_candidate("Tai nghe Baseus", "Baseus", "WM02", category="tech")
            cid = "tech"

        run_id = repo.begin_run(run_date, {"category": cid})
        return DiscoveryResult(
            run_id=run_id,
            status="completed",
            candidates=[],
            raw_candidate_count=5,
            deduplicated_candidate_count=4,
            enriched_candidate_count=3,
            selection=SelectionResult(selected=[cand], statuses={}, reason_codes={}, warnings=[]),
            provider_summary=[ProviderRunSummary(provider="mock_p", status="ok", observation_count=5)],
        )

    mock_discovery.discover.side_effect = mock_discover
    mock_media = MagicMock()

    runner = DailyScoutRunner(
        base_dir=tmp_path,
        request_file=req_file,
        runs_dir=runs_dir,
        discovery_service=mock_discovery,
        history_repository=repo,
        media_service=mock_media,
    )

    target_d = date(2026, 10, 20)
    # First run: multi-category
    runner.run_daily(force=True, target_date=target_d, prompt="tìm 2 snack")
    day_dir = runs_dir / "2026-10-20"
    assert (day_dir / "food_snack" / "products.json").exists()

    # Second run: single category tech only (force re-run)
    res2 = runner.run_daily(force=True, target_date=target_d, prompt="tìm 2 sản phẩm công nghệ tai nghe")
    assert res2["status"] == "success"

    # The obsolete food_snack partition directory must have been cleanly removed!
    assert not (day_dir / "food_snack").exists()
    assert (day_dir / "tech" / "products.json").exists()
    assert (day_dir / "products.json").exists()


# 16. Backfill Skips Failed Runs and Ignores Orphan Partitions
def test_backfill_skips_failed_runs_and_orphan_partitions(tmp_path: Path):
    runs_dir = tmp_path / "runs"
    repo = TrendHistoryRepository(tmp_path / "trend.db")

    # Day 1: Failed session with orphan partition
    day1 = runs_dir / "2026-10-18"
    day1.mkdir(parents=True)
    (day1 / "products.json").write_text(json.dumps({"status": "failed", "products": []}), encoding="utf-8")
    orphan_cat = day1 / "food_snack"
    orphan_cat.mkdir()
    (orphan_cat / "products.json").write_text(
        json.dumps({"products": [{"name": "Orphan Food", "brand": "BrandX", "model": "ModelY"}]}),
        encoding="utf-8",
    )

    # Day 2: Successful session with legitimate tech partition and an unreferenced orphan food dir
    day2 = runs_dir / "2026-10-19"
    day2.mkdir(parents=True)
    tech_dir = day2 / "tech"
    tech_dir.mkdir()
    (tech_dir / "products.json").write_text(
        json.dumps({"products": [{"name": "Tech Gadget", "brand": "Baseus", "model": "WM02"}]}),
        encoding="utf-8",
    )
    orphan2 = day2 / "food_snack"
    orphan2.mkdir()
    (orphan2 / "products.json").write_text(
        json.dumps({"products": [{"name": "Stale Snack", "brand": "Old", "model": "Snack"}]}),
        encoding="utf-8",
    )
    (day2 / "products.json").write_text(
        json.dumps({
            "status": "success",
            "category_summaries": {
                "tech": {"status": "success", "count": 1},
            },
            "products": [{"name": "Tech Gadget", "brand": "Baseus", "model": "WM02"}],
        }),
        encoding="utf-8",
    )

    summary = backfill_daily_runs(repo, runs_dir)
    # Day 1 was failed -> 0 imported
    # Day 2 only tech was in category_summaries with status 'success' -> 1 imported (orphan Stale Snack ignored)
    assert summary.selections_imported == 1

    with repo._get_connection() as conn:
        rows = conn.execute("SELECT canonical_key FROM product_history;").fetchall()
        keys = [r["canonical_key"] for r in rows]
        assert keys == ["baseus|wm02"]


# 17. Budget Parser Regression Cases
@pytest.mark.parametrize("prompt,expected_min,expected_max", [
    ("tìm 10 tai nghe giá 1.5tr-2tr", 1_500_000, 2_000_000),
    ("tìm 10 tai nghe dưới 2 triệu 500k", 0, 2_500_000),
    ("tìm 10 tai nghe giá 500k", 0, 500_000),
    ("tìm 10 tai nghe khoảng 350k", 0, 350_000),
    ("tìm 10 tai nghe từ 500k", 500_000, 0),  # unconstrained ceiling
])
def test_budget_parser_regression_cases(prompt: str, expected_min: int, expected_max: int):
    dispatcher = CategoryDispatcherAgent(enable_llm=False)
    res = dispatcher.analyze_and_dispatch(prompt)
    assert res.status == "dispatched"
    task = res.tasks[0]
    assert task.criteria.price_min == expected_min
    assert task.criteria.price_max == expected_max


def test_invalid_price_range_rejected():
    dispatcher = CategoryDispatcherAgent(enable_llm=False)
    res = dispatcher.analyze_and_dispatch("tìm 10 tai nghe giá 2tr - 1tr")
    assert res.status == "needs_review"
    assert "giá tối thiểu" in res.message and "lớn hơn giá tối đa" in res.message


# 18. Water-Resistant Specification vs Category Classification
def test_chong_nuoc_not_classified_as_food():
    dispatcher = CategoryDispatcherAgent(enable_llm=False)
    # "tai nghe chống nước" must be 100% tech, NOT split into 5 food + 5 tech!
    res = dispatcher.analyze_and_dispatch("tìm 10 tai nghe chống nước")
    assert res.status == "dispatched"
    assert not res.is_multi_category
    assert len(res.tasks) == 1
    assert res.tasks[0].category_id == "tech"
    assert res.tasks[0].target_count == 10


def test_attribute_alone_without_noun_needs_review():
    dispatcher = CategoryDispatcherAgent(enable_llm=False)
    # "chống nước" alone is not proof of tech, so generic request needs review
    res = dispatcher.analyze_and_dispatch("tìm 10 sản phẩm chống nước")
    assert res.status == "needs_review"


# 19. Quantity Constraint Contradictions and Deductions
def test_quantity_constraint_deduction():
    dispatcher = CategoryDispatcherAgent(enable_llm=False)
    res = dispatcher.analyze_and_dispatch("tìm 20 sản phẩm gồm 15 snack và tai nghe")
    assert res.status == "needs_review"
    assert res.is_multi_category
    assert res.tasks == []


def test_quantity_constraint_contradiction_exceeds_total():
    dispatcher = CategoryDispatcherAgent(enable_llm=False)
    res = dispatcher.analyze_and_dispatch("tìm 20 sản phẩm gồm 25 snack và tai nghe")
    assert res.status == "needs_review"
    assert "một ngành hàng" in res.message


def test_quantity_constraint_contradiction_sum_mismatch():
    dispatcher = CategoryDispatcherAgent(enable_llm=False)
    res = dispatcher.analyze_and_dispatch("tìm 10 sản phẩm gồm 7 snack và 5 tai nghe")
    assert res.status == "needs_review"
    assert "một ngành hàng" in res.message


def test_quantity_constraint_one_product_two_categories():
    dispatcher = CategoryDispatcherAgent(enable_llm=False)
    res = dispatcher.analyze_and_dispatch("tìm 1 sản phẩm đồ ăn và công nghệ")
    assert res.status == "needs_review"
    assert "một ngành hàng" in res.message


# 20. CLI Rejection Handling (No KeyError)
def test_cli_rejection_no_keyerror():
    from scripts.daily_scout import cmd_run
    import argparse

    mock_runner = MagicMock()
    mock_runner.run_daily.return_value = {
        "status": "unsupported_category",
        "date": "2026-10-20",
        "failure_code": "unsupported_category",
        "message": "Chỉ hỗ trợ 2 ngành hàng food_snack và tech.",
        "prompt": "tìm 10 cái áo thun",
    }

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("scripts.daily_scout.DailyScoutRunner", lambda base_dir: mock_runner)
        args = argparse.Namespace(date=None, force=False, max_images=3, prompt="tìm 10 cái áo thun")
        exit_code = cmd_run(args)
        assert exit_code == 1  # must exit cleanly with code 1, no KeyError!
