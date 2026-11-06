"""Tests for isolated Chrome profile resolution and cloning."""

from pathlib import Path
from unittest.mock import patch

from video_factory.integrations.chrome_profile import (
    clone_chrome_profile,
    get_isolated_flow_profile_dir,
    get_default_system_chrome_user_data_dir,
)


def test_isolated_profile_dir_resolves_sensibly():
    p = get_isolated_flow_profile_dir()
    assert isinstance(p, Path)
    assert "chrome_flow_profile" in str(p)


def test_clone_chrome_profile_skips_locks_and_copies_auth(tmp_path):
    source_dir = tmp_path / "system_chrome"
    target_dir = tmp_path / "isolated_chrome"

    # Setup source structure
    source_dir.mkdir()
    (source_dir / "Local State").write_text('{"os_crypt": {"encrypted_key": "abc"}}', encoding="utf-8")
    (source_dir / "SingletonLock").write_text("lock", encoding="utf-8")

    profile_dir = source_dir / "Default"
    profile_dir.mkdir()
    (profile_dir / "Preferences").write_text('{"profile": {"name": "Test"}}', encoding="utf-8")
    
    network_dir = profile_dir / "Network"
    network_dir.mkdir()
    (network_dir / "Cookies").write_bytes(b"dummy_cookies_db")

    # Run clone
    result = clone_chrome_profile(
        source_user_data_dir=source_dir,
        target_user_data_dir=target_dir,
        profile_name="Default",
    )

    assert result["status"] == "success"
    assert result["copied_files"] >= 3

    # Verify essential files exist in target
    assert (target_dir / "Local State").is_file()
    assert (target_dir / "Default" / "Preferences").is_file()
    assert (target_dir / "Default" / "Network" / "Cookies").is_file()

    # Verify SingletonLock was NOT copied
    assert not (target_dir / "SingletonLock").exists()
