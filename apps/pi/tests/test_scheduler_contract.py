from pathlib import Path
import re


def test_scheduler_batch_is_producer_only():
    repo_root = Path(__file__).resolve().parents[1]
    batch_file = repo_root / "scripts" / "run_daily.bat"
    assert batch_file.is_file(), f"run_daily.bat not found at {batch_file}"

    content = batch_file.read_text(encoding="utf-8")

    # Assert that PI is strictly producer-only and does NOT start Hermes
    assert not re.search(r'hermes_cli', content, re.IGNORECASE), (
        "run_daily.bat must be producer-only and must not invoke hermes_cli directly."
    )
    assert "HERMES_REPO" not in content, "run_daily.bat must not depend on HERMES_REPO"

    # Assert that publish_exchange exit code is propagated
    assert "EXCHANGE_PUBLISH_EXIT" in content
    assert "exit /b %EXCHANGE_PUBLISH_EXIT%" in content or "exit /b 1" in content


def test_scheduler_batch_has_distinct_preflight_checks():
    repo_root = Path(__file__).resolve().parents[1]
    batch_file = repo_root / "scripts" / "run_daily.bat"
    content = batch_file.read_text(encoding="utf-8")

    # Verify preflight boundaries and distinct non-zero exit codes
    assert "PREFLIGHT_ERROR_10" in content, "Missing Product-Intelligence Python preflight"
    assert "PREFLIGHT_ERROR_40" in content, "Missing Exchange inbox parent preflight"



def test_scheduler_setup_script_configures_retries_and_observable_verification():
    repo_root = Path(__file__).resolve().parents[1]
    ps_file = repo_root / "scripts" / "setup_windows_task.ps1"
    assert ps_file.is_file(), f"setup_windows_task.ps1 not found at {ps_file}"

    content = ps_file.read_text(encoding="utf-8")

    # Verify retry count and interval
    assert "-RestartCount 3" in content
    assert "-RestartInterval (New-TimeSpan -Minutes 15)" in content
    assert "-StartWhenAvailable" in content
    assert "-MultipleInstances IgnoreNew" in content

    # Verify logging redirection under logs/
    assert "logs" in content
    assert "daily_task_run.log" in content

    # Verify observable post-registration verification
    assert "Get-ScheduledTask" in content
    assert "RestartCount" in content
    assert "exit 1" in content
