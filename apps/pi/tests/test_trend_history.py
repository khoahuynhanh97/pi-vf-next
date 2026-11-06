from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
import pytest

from product_scout.trend_discovery.backfill import backfill_daily_runs
from product_scout.trend_discovery.models import (
    AggregatedCandidate,
    CandidateObservation,
    ProviderRunSummary,
    ScoreBreakdown,
    ScoredCandidate,
    SelectionResult,
)
from product_scout.trend_discovery.repository import TrendHistoryRepository


def test_backfill_is_idempotent(tmp_path: Path):
    day = tmp_path / "runs" / "2026-09-10"
    day.mkdir(parents=True)
    (day / "products.json").write_text(
        json.dumps({
            "date": "2026-09-10",
            "products": [{"name": "Jisulife FA55", "brand": "Jisulife", "model": "FA55"}],
        }),
        encoding="utf-8",
    )
    repo = TrendHistoryRepository(tmp_path / "trend.db")
    summary1 = backfill_daily_runs(repo, tmp_path / "runs")
    assert summary1.selections_imported == 1
    summary2 = backfill_daily_runs(repo, tmp_path / "runs")
    assert summary2.selections_imported == 0

    history = repo.get_product_history("jisulife|fa55")
    assert history is not None
    assert history.selection_count == 1
    assert history.last_selected_at is not None
    assert history.last_selected_at.date() == date(2026, 9, 10)


def test_failed_run_never_becomes_current(tmp_path: Path):
    repo = TrendHistoryRepository(tmp_path / "trend.db")
    run_id = repo.begin_run(date(2026, 9, 18), {"limit": 10})
    repo.fail_run(run_id, "no_discovery_evidence", "no observations")
    assert repo.get_current_run(date(2026, 9, 18)) is None


def test_complete_run_marks_current_and_updates_history(tmp_path: Path):
    repo = TrendHistoryRepository(tmp_path / "trend.db")
    run_date = date(2026, 9, 18)
    run_id = repo.begin_run(run_date, {"limit": 10})

    candidate = AggregatedCandidate(
        canonical_key="jisulife|fa55",
        name="Jisulife FA55",
        brand="Jisulife",
        model="FA55",
    )
    scored = ScoredCandidate(
        candidate=candidate,
        trend_score=85.0,
        freshness_score=100.0,
        confidence_score=90.0,
        final_score=89.75,
        independent_source_count=2,
        score_breakdown=ScoreBreakdown(),
    )
    selection = SelectionResult(
        selected=[scored],
        statuses={"jisulife|fa55": "new"},
        reason_codes={"jisulife|fa55": ["fresh_top_rank"]},
        warnings=[],
    )
    summary = [
        ProviderRunSummary(
            provider="web_search",
            status="success",
            observation_count=5,
            latency_ms=120,
        )
    ]
    repo.complete_run(run_id, selection, summary)

    current = repo.get_current_run(run_date)
    assert current is not None
    assert current["run_id"] == run_id
    assert current["status"] == "completed"

    history = repo.get_product_history("jisulife|fa55")
    assert history is not None
    assert history.selection_count == 1
    assert history.last_selected_at is not None
    assert history.last_selected_at.date() == run_date


def test_mark_export_failed_clears_current(tmp_path: Path):
    repo = TrendHistoryRepository(tmp_path / "trend.db")
    run_date = date(2026, 9, 18)
    run_id = repo.begin_run(run_date, {"limit": 10})
    selection = SelectionResult(selected=[], statuses={}, reason_codes={}, warnings=[])
    repo.complete_run(run_id, selection, [])

    assert repo.get_current_run(run_date) is not None
    repo.mark_export_failed(run_id, "disk full")
    assert repo.get_current_run(run_date) is None


def test_rollback_on_complete_run_failure(tmp_path: Path):
    repo = TrendHistoryRepository(tmp_path / "trend.db")
    run_date = date(2026, 9, 18)
    run_id = repo.begin_run(run_date, {"limit": 10})

    # Break repository SQL temporarily or trigger error in complete_run
    class BrokenRepo(TrendHistoryRepository):
        def _insert_selection(self, *args, **kwargs):
            raise RuntimeError("DB disk error simulation")

    broken_repo = BrokenRepo(tmp_path / "trend.db")
    candidate = AggregatedCandidate(
        canonical_key="bad|prod",
        name="Bad Prod",
        brand="Bad",
        model="Prod",
    )
    scored = ScoredCandidate(
        candidate=candidate,
        trend_score=80.0,
        freshness_score=100.0,
        confidence_score=90.0,
        final_score=85.0,
        independent_source_count=1,
        score_breakdown=ScoreBreakdown(),
    )
    selection = SelectionResult(
        selected=[scored],
        statuses={"bad|prod": "new"},
        reason_codes={"bad|prod": ["fresh"]},
    )

    with pytest.raises(RuntimeError, match="DB disk error simulation"):
        broken_repo.complete_run(run_id, selection, [])

    history = broken_repo.get_product_history("bad|prod")
    assert history is None


def test_backfill_skips_malformed_files(tmp_path: Path):
    runs_dir = tmp_path / "runs"
    bad_dir = runs_dir / "2026-09-01"
    bad_dir.mkdir(parents=True)
    (bad_dir / "products.json").write_text("{not valid json", encoding="utf-8")

    good_dir = runs_dir / "2026-09-02"
    good_dir.mkdir(parents=True)
    (good_dir / "products.json").write_text(
        json.dumps({
            "date": "2026-09-02",
            "products": [{"name": "Good Product", "brand": "Good", "model": "One"}],
        }),
        encoding="utf-8",
    )

    repo = TrendHistoryRepository(tmp_path / "trend.db")
    summary = backfill_daily_runs(repo, runs_dir)
    assert summary.files_read == 1
    assert summary.files_skipped == 1
    assert len(summary.warnings) == 1
    assert repo.get_product_history("good|one") is not None
