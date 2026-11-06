from __future__ import annotations

import json
import os
import shutil
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
import pytest


@pytest.fixture(autouse=True)
def enable_legacy_discovery_for_regression_tests(monkeypatch):
    # Exercise retained legacy behavior without changing the production pause.
    monkeypatch.setattr("product_scout.workflow_policy.PRODUCT_DISCOVERY_PAUSED", False)

from product_scout.daily_parser import DailyPromptParser, DiscoveredProduct
from product_scout.daily_runner import DailyScoutRunner, slugify
from product_scout.trend_discovery.backfill import backfill_daily_runs
from product_scout.trend_discovery.config import TrendDiscoveryConfig
from product_scout.trend_discovery.models import (
    AggregatedCandidate,
    CandidateObservation,
    DiscoveryResult,
    ScoreBreakdown,
    ScoredCandidate,
    SelectionResult,
)
from product_scout.trend_discovery.repository import TrendHistoryRepository

FIXED_NOW = datetime(2026, 9, 18, 7, 0, 0, tzinfo=timezone.utc)


def test_slugify():
    assert slugify("Quạt mini Jisulife Life 7!") == "quat-mini-jisulife-life-7"
    assert slugify("Tai nghe Baseus Bowie WM02") == "tai-nghe-baseus-bowie-wm02"


def test_parse_criteria_standard_prompt():
    prompt = "tìm 10 sản phẩm ngành công nghệ hot trend tiktok/shopee trong phạm vi giá từ 200k-1tr vnd"
    criteria = DailyPromptParser.parse_criteria(prompt)
    assert criteria.limit == 10
    assert criteria.price_min == 200_000
    assert criteria.price_max == 1_000_000
    assert "tiktok" in criteria.platforms
    assert "shopee" in criteria.platforms
    assert "công nghệ" in criteria.category


def test_parse_criteria_under_budget():
    prompt = "top 5 đồ gia dụng thông minh dưới 500k"
    criteria = DailyPromptParser.parse_criteria(prompt)
    assert criteria.limit == 5
    assert criteria.price_min == 0
    assert criteria.price_max == 500_000
    assert "gia dụng" in criteria.category


def test_discover_candidates_heuristic():
    os.environ["PRODUCT_SCOUT_ENABLE_CURATED_EMERGENCY"] = "true"
    try:
        prompt = "tìm 3 sản phẩm công nghệ giá 300k - 600k"
        criteria = DailyPromptParser.parse_criteria(prompt)
        candidates = DailyPromptParser.discover_candidates(criteria)
        assert len(candidates) >= 3
        for c in candidates:
            assert isinstance(c, DiscoveredProduct)
            assert c.name
            assert c.brand
            assert len(c.keywords) > 0
    finally:
        os.environ.pop("PRODUCT_SCOUT_ENABLE_CURATED_EMERGENCY", None)


def test_daily_runner_idempotency(tmp_path: Path):
    req_file = tmp_path / "request.txt"
    req_file.write_text("tìm 2 sản phẩm công nghệ giá 200k-1tr", encoding="utf-8")
    runs_dir = tmp_path / "runs"

    runner = DailyScoutRunner(
        base_dir=tmp_path,
        request_file=req_file,
        runs_dir=runs_dir,
        history_repository=TrendHistoryRepository(tmp_path / "trend.db"),
    )

    test_date = date(2026, 9, 10)
    day_str = test_date.isoformat()

    # Pre-create completed run
    day_dir = runs_dir / day_str
    day_dir.mkdir(parents=True)
    json_path = day_dir / "products.json"
    json_path.write_text(json.dumps({"status": "success", "products": [{"id": "prod_01"}]}), encoding="utf-8")

    assert runner.is_already_completed(test_date) is True

    # Running without force should return already_completed
    result = runner.run_daily(force=False, target_date=test_date)
    assert result["status"] == "already_completed"


def test_empty_result_is_failure_and_does_not_replace_previous_report(tmp_path: Path):
    req_file = tmp_path / "request.txt"
    req_file.write_text("tìm sản phẩm công nghệ", encoding="utf-8")
    runs_dir = tmp_path / "runs"
    day_dir = runs_dir / "2026-09-18"
    day_dir.mkdir(parents=True)
    prior = day_dir / "products.json"
    prior.write_text('{"status":"success","products":[{"id":"old"}]}', encoding="utf-8")
    repo = TrendHistoryRepository(tmp_path / "trend.db")
    run_id = repo.begin_run(date(2026, 9, 18), {"limit": 10})
    mock_discovery = MagicMock()
    mock_discovery.config = TrendDiscoveryConfig()
    mock_discovery.discover.return_value = DiscoveryResult(
        run_id=run_id,
        status="completed",
        selection=SelectionResult(selected=[]),
    )
    runner = DailyScoutRunner(
        base_dir=tmp_path,
        request_file=req_file,
        runs_dir=runs_dir,
        discovery_service=mock_discovery,
        history_repository=repo,
        media_service=MagicMock(),
    )
    result = runner.run_daily(force=True, target_date=date(2026, 9, 18))
    assert result["status"] == "failed"
    assert result["failure_code"] == "no_eligible_products"
    assert json.loads(prior.read_text(encoding="utf-8"))["products"][0]["id"] == "old"
    assert repo.get_current_run(date(2026, 9, 18)) is None


def test_empty_report_is_not_considered_completed(tmp_path: Path):
    day_dir = tmp_path / "runs" / "2026-09-18"
    day_dir.mkdir(parents=True)
    (day_dir / "products.json").write_text(
        '{"status":"success_with_warnings","products":[]}', encoding="utf-8"
    )
    runner = DailyScoutRunner(
        base_dir=tmp_path,
        request_file=tmp_path / "request.txt",
        runs_dir=tmp_path / "runs",
        history_repository=TrendHistoryRepository(tmp_path / "trend.db"),
        discovery_service=MagicMock(),
        media_service=MagicMock(),
    )
    assert runner.is_already_completed(date(2026, 9, 18)) is False


def make_mock_scored_candidate(name: str, brand: str, model: str, score: float = 85.0) -> ScoredCandidate:
    obs = CandidateObservation(
        provider="web_search",
        candidate_name=name,
        brand=brand,
        model=model,
        source_url=f"https://source.test/{brand.lower()}",
        source_host="source.test",
        source_rank=1,
        observed_at=FIXED_NOW,
        published_at=FIXED_NOW,
        signal_type="web_mention",
        title=name,
    )
    cand = AggregatedCandidate(
        canonical_key=f"{brand.lower()}|{model.lower()}",
        name=name,
        brand=brand,
        model=model,
        category="công nghệ",
        price_min=300_000,
        price_max=500_000,
        observations=[obs],
    )
    return ScoredCandidate(
        candidate=cand,
        trend_score=80.0,
        freshness_score=100.0,
        confidence_score=90.0,
        final_score=score,
        independent_source_count=1,
        score_breakdown=ScoreBreakdown(),
    )


def test_runner_exports_selection_audit(tmp_path: Path):
    req_file = tmp_path / "request.txt"
    req_file.write_text("tìm 10 sản phẩm công nghệ", encoding="utf-8")
    runs_dir = tmp_path / "runs"
    repo = TrendHistoryRepository(tmp_path / "trend.db")

    run_date = date(2026, 9, 18)
    run_id = repo.begin_run(run_date, {"limit": 10})
    scored_items = [
        make_mock_scored_candidate(f"Product {i}", f"Brand{i}", f"Model{i}", 80.0 + i)
        for i in range(7)
    ]
    mock_discovery = MagicMock()
    mock_discovery.config = TrendDiscoveryConfig()
    mock_discovery.discover.return_value = DiscoveryResult(
        run_id=run_id,
        status="completed",
        candidates=[s.candidate for s in scored_items],
        selection=SelectionResult(
            selected=scored_items,
            statuses={s.candidate.canonical_key: "new" for s in scored_items},
            reason_codes={s.candidate.canonical_key: ["fresh_top_rank"] for s in scored_items},
            warnings=[],
        ),
        provider_summary=[],
        raw_candidate_count=7,
        deduplicated_candidate_count=7,
        enriched_candidate_count=7,
    )

    mock_media = MagicMock()
    mock_media.research.return_value = MagicMock(selected_assets=[])

    runner = DailyScoutRunner(
        base_dir=tmp_path,
        request_file=req_file,
        runs_dir=runs_dir,
        discovery_service=mock_discovery,
        history_repository=repo,
        media_service=mock_media,
    )

    result = runner.run_daily(target_date=date(2026, 9, 18))
    assert result["status"] == "success"
    payload = json.loads(Path(result["output_json"]).read_text(encoding="utf-8"))
    assert payload["version"] == "2.0"
    assert payload["selection_policy"]["cooldown_days"] == 30
    assert payload["products"][0]["selection_status"] == "new"
    assert payload["products"][0]["trend_evidence"]
    assert payload["products"][0]["key_features"] == ""


def test_short_report_succeeds_with_warning(tmp_path: Path):
    req_file = tmp_path / "request.txt"
    req_file.write_text("tìm 10 sản phẩm công nghệ", encoding="utf-8")
    runs_dir = tmp_path / "runs"
    repo = TrendHistoryRepository(tmp_path / "trend.db")

    run_date = date(2026, 9, 18)
    run_id = repo.begin_run(run_date, {"limit": 10})
    scored_items = [
        make_mock_scored_candidate(f"Product {i}", f"Brand{i}", f"Model{i}", 80.0 + i)
        for i in range(5)
    ]
    mock_discovery = MagicMock()
    mock_discovery.config = TrendDiscoveryConfig()
    mock_discovery.discover.return_value = DiscoveryResult(
        run_id=run_id,
        status="completed",
        candidates=[s.candidate for s in scored_items],
        selection=SelectionResult(
            selected=scored_items,
            statuses={s.candidate.canonical_key: "new" for s in scored_items},
            reason_codes={s.candidate.canonical_key: ["fresh_selected"] for s in scored_items},
            warnings=["insufficient_fresh_candidates"],
        ),
        provider_summary=[],
    )

    mock_media = MagicMock()
    mock_media.research.return_value = MagicMock(selected_assets=[])

    runner = DailyScoutRunner(
        base_dir=tmp_path,
        request_file=req_file,
        runs_dir=runs_dir,
        discovery_service=mock_discovery,
        history_repository=repo,
        media_service=mock_media,
    )

    result = runner.run_daily(target_date=date(2026, 9, 18))
    assert result["status"] == "success_with_warnings"
    assert result["total_products"] == 5
    assert result["warnings"] == ["insufficient_fresh_candidates"]


def test_export_failure_preserves_existing_report(tmp_path: Path):
    req_file = tmp_path / "request.txt"
    req_file.write_text("tìm 10 sản phẩm", encoding="utf-8")
    runs_dir = tmp_path / "runs"
    repo = TrendHistoryRepository(tmp_path / "trend.db")

    # Pre-existing report
    report_file = runs_dir / "2026-09-18" / "products.json"
    report_file.parent.mkdir(parents=True)
    report_file.write_text('{"version":"1.0"}', encoding="utf-8")

    mock_discovery = MagicMock()
    mock_discovery.config = TrendDiscoveryConfig()
    mock_discovery.discover.return_value = DiscoveryResult(
        run_id="test_run_fail",
        status="completed",
        candidates=[],
        selection=SelectionResult(
            selected=[make_mock_scored_candidate("Jisulife FA55", "Jisulife", "FA55")],
            statuses={},
            reason_codes={},
            warnings=[],
        ),
    )
    mock_media = MagicMock()

    runner = DailyScoutRunner(
        base_dir=tmp_path,
        request_file=req_file,
        runs_dir=runs_dir,
        discovery_service=mock_discovery,
        history_repository=repo,
        media_service=mock_media,
    )

    # Force an error during atomic replacement
    runner._generate_markdown_report = MagicMock(side_effect=OSError("Disk simulation failure"))

    with pytest.raises(OSError):
        runner.run_daily(force=True, target_date=date(2026, 9, 18))

    assert report_file.read_text(encoding="utf-8") == '{"version":"1.0"}'


def test_export_failure_restores_files_history_and_current_run(tmp_path: Path, monkeypatch):
    run_date = date(2026, 9, 18)
    runs_dir = tmp_path / "runs"
    day_dir = runs_dir / run_date.isoformat()
    day_dir.mkdir(parents=True)
    prior_payload = {
        "status": "success",
        "products": [{"name": "Old Product", "brand": "Old", "model": "One"}],
    }
    prior_json = day_dir / "products.json"
    prior_json.write_text(json.dumps(prior_payload), encoding="utf-8")
    prior_report = day_dir / "report.md"
    prior_report.write_text("# Previous published report\n", encoding="utf-8")

    repo = TrendHistoryRepository(tmp_path / "trend.db")
    old_item = make_mock_scored_candidate("Old Product", "Old", "One")
    old_run_id = repo.begin_run(run_date, {"category": "công nghệ"})
    repo.complete_run(
        old_run_id,
        SelectionResult(selected=[old_item], statuses={old_item.candidate.canonical_key: "new"}),
        [],
    )

    new_item = make_mock_scored_candidate("New Product", "New", "Two")
    new_run_id = repo.begin_run(run_date, {"category": "công nghệ"})
    mock_discovery = MagicMock()
    mock_discovery.config = TrendDiscoveryConfig()
    mock_discovery.discover.return_value = DiscoveryResult(
        run_id=new_run_id,
        status="completed",
        selection=SelectionResult(selected=[new_item], statuses={new_item.candidate.canonical_key: "new"}),
        provider_summary=[],
        raw_candidate_count=1,
        deduplicated_candidate_count=1,
        enriched_candidate_count=1,
    )
    mock_media = MagicMock()
    mock_media.research.return_value = MagicMock(selected_assets=[])
    runner = DailyScoutRunner(
        base_dir=tmp_path,
        request_file=tmp_path / "request.txt",
        runs_dir=runs_dir,
        discovery_service=mock_discovery,
        history_repository=repo,
        media_service=mock_media,
    )
    runner.request_file.write_text("tìm 1 sản phẩm công nghệ", encoding="utf-8")

    original_move = shutil.move

    def fail_root_json_publish(source, destination, *args, **kwargs):
        if Path(source).name == "products.json.tmp":
            raise OSError("simulated root publish failure")
        return original_move(source, destination, *args, **kwargs)

    monkeypatch.setattr("product_scout.daily_runner.shutil.move", fail_root_json_publish)

    with pytest.raises(OSError, match="simulated root publish failure"):
        runner.run_daily(force=True, target_date=run_date)

    assert json.loads(prior_json.read_text(encoding="utf-8")) == prior_payload
    assert prior_report.read_text(encoding="utf-8") == "# Previous published report\n"
    assert repo.get_current_run(run_date)["run_id"] == old_run_id
    assert repo.get_product_history(new_item.candidate.canonical_key) is None
    with repo._get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_selections;").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM discovery_runs WHERE run_id = ?;", (new_run_id,)).fetchone()[0] == 1
    backfill_daily_runs(repo, runs_dir)
    assert repo.get_product_history(new_item.candidate.canonical_key) is None
    assert not list(runs_dir.glob(".*.rollback-*"))


@pytest.mark.parametrize("image_target", [3, 5, 10])
def test_runner_passes_supported_image_target_to_media(tmp_path: Path, image_target: int):
    run_date = date(2026, 9, 18)
    repo = TrendHistoryRepository(tmp_path / "trend.db")
    run_id = repo.begin_run(run_date, {"category": "công nghệ"})
    item = make_mock_scored_candidate("Jisulife FA55", "Jisulife", "FA55")
    mock_discovery = MagicMock()
    mock_discovery.config = TrendDiscoveryConfig()
    mock_discovery.discover.return_value = DiscoveryResult(
        run_id=run_id,
        status="completed",
        selection=SelectionResult(selected=[item], statuses={item.candidate.canonical_key: "new"}),
        provider_summary=[],
        raw_candidate_count=1,
        deduplicated_candidate_count=1,
        enriched_candidate_count=1,
    )
    mock_media = MagicMock()
    mock_media.research.return_value = MagicMock(selected_assets=[])
    runner = DailyScoutRunner(
        base_dir=tmp_path,
        request_file=tmp_path / "request.txt",
        runs_dir=tmp_path / "runs",
        discovery_service=mock_discovery,
        history_repository=repo,
        media_service=mock_media,
    )
    runner.request_file.write_text("tìm 1 sản phẩm công nghệ", encoding="utf-8")

    kwargs = {} if image_target == 5 else {"max_images_per_product": image_target}
    result = runner.run_daily(force=True, target_date=run_date, **kwargs)

    assert result["total_products"] == 1
    assert mock_media.research.call_args.kwargs["target_assets"] == image_target


@pytest.mark.parametrize("command, expected", [
    (["run"], 5),
    (["run", "--max-images", "3"], 3),
    (["run", "--max-images", "10"], 10),
    (["daemon"], 5),
    (["daemon", "--max-images", "3"], 3),
    (["daemon", "--max-images", "10"], 10),
])
def test_cli_and_daemon_default_to_five_images(command: list[str], expected: int, monkeypatch):
    import scripts.daily_scout as daily_scout

    captured: dict[str, int] = {}
    handler_name = "cmd_run" if command[0] == "run" else "cmd_daemon"

    def capture(args):
        captured["max_images"] = args.max_images
        return 0

    monkeypatch.setattr(daily_scout, handler_name, capture)
    assert daily_scout.main(command) == 0
    assert captured["max_images"] == expected


def test_backfill_failure_aborts_run_before_selection(tmp_path: Path, monkeypatch):
    from unittest.mock import patch
    req_file = tmp_path / "request.txt"
    req_file.write_text("tìm 10 sản phẩm", encoding="utf-8")
    repo = TrendHistoryRepository(tmp_path / "trend.db")

    with patch("product_scout.daily_runner.backfill_daily_runs", side_effect=RuntimeError("Corrupt DB")):
        runner = DailyScoutRunner(
            base_dir=tmp_path,
            request_file=req_file,
            history_repository=repo,
        )
        assert runner._backfill_error is not None
        with pytest.raises(RuntimeError, match="History database backfill failed"):
            runner.run_daily(force=True, target_date=date(2026, 9, 18))


def test_audit_eligible_candidates_accurate(tmp_path: Path):
    req_file = tmp_path / "request.txt"
    req_file.write_text("tìm 10 sản phẩm", encoding="utf-8")
    runs_dir = tmp_path / "runs"
    repo = TrendHistoryRepository(tmp_path / "trend.db")
    run_id = repo.begin_run(date(2026, 9, 18), {})

    mock_discovery = MagicMock()
    mock_discovery.config = TrendDiscoveryConfig()
    # 0 eligible candidates
    mock_discovery.discover.return_value = DiscoveryResult(
        run_id=run_id,
        status="completed",
        candidates=[],
        raw_candidate_count=25,
        deduplicated_candidate_count=20,
        enriched_candidate_count=15,
        eligible_candidate_count=0,
        selection=SelectionResult(selected=[], statuses={}, reason_codes={}, warnings=["insufficient_fresh_candidates"], eligible_count=0),
    )
    mock_media = MagicMock()

    runner = DailyScoutRunner(
        base_dir=tmp_path,
        request_file=req_file,
        runs_dir=runs_dir,
        discovery_service=mock_discovery,
        history_repository=repo,
        media_service=mock_media,
    )

    result = runner.run_daily(force=True, target_date=date(2026, 9, 18))
    assert result["failure_code"] == "no_eligible_products"
    assert not (runs_dir / "2026-09-18" / "products.json").exists()

    # An empty selection is a failed run, never a completed daily report.
    with repo._get_connection() as conn:
        row = conn.execute("SELECT status, failure_code FROM discovery_runs WHERE run_id = ?;", (run_id,)).fetchone()
        assert row["status"] == "failed"
        assert row["failure_code"] == "no_eligible_products"
