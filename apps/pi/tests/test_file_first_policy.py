from datetime import date

import pytest

from product_scout.workflow_policy import require_product_discovery


def test_discovery_is_paused_without_external_calls():
    with pytest.raises(RuntimeError, match="discovery is paused"):
        require_product_discovery()


def test_runner_returns_paused_before_accessing_storage():
    from product_scout.daily_runner import DailyScoutRunner

    result = object.__new__(DailyScoutRunner).run_daily()
    assert result["status"] == "paused"
    assert result["failure_code"] == "product_discovery_paused"


def test_trend_service_stops_before_clock_repository_or_providers():
    from product_scout.trend_discovery.service import TrendDiscoveryService

    with pytest.raises(RuntimeError, match="discovery is paused"):
        object.__new__(TrendDiscoveryService).discover(None, run_date=date(2026, 10, 30))


def test_emergency_catalog_cannot_bypass_pause(monkeypatch):
    from product_scout.daily_parser import DailyPromptParser

    monkeypatch.setenv("PRODUCT_SCOUT_ENABLE_CURATED_EMERGENCY", "true")
    with pytest.raises(RuntimeError, match="discovery is paused"):
        DailyPromptParser.discover_candidates(None)


@pytest.mark.parametrize("command", ["run", "daemon", "test-prompt"])
def test_daily_cli_stops_before_constructing_runner(command, monkeypatch):
    from scripts import daily_scout

    def unexpected(*args, **kwargs):
        pytest.fail("Paused command constructed the runner")

    monkeypatch.setattr(daily_scout, "DailyScoutRunner", unexpected)
    assert daily_scout.main([command]) == 2
