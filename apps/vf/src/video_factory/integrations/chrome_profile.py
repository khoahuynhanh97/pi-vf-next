"""Isolated Chrome profile manager and session cloner for Google Flow automation.

Ensures Video Factory runs with its own independent Chrome profile, preventing
SingletonLock conflicts and session corruption with the user's primary browser.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from video_factory.constants import get_video_factory_home

logger = logging.getLogger(__name__)

# Files/directories critical for authentication and session persistence
ESSENTIAL_ROOT_FILES = (
    "Local State",  # Contains DPAPI master key needed to decrypt Cookies on Windows
)

ESSENTIAL_PROFILE_ITEMS = (
    "Network",          # Contains Network/Cookies in modern Chromium
    "Cookies",          # Legacy cookies file
    "Preferences",
    "Secure Preferences",
    "Local Storage",    # Web tokens and local state
    "Session Storage",
    "IndexedDB",        # Some Google auth flows store tokens in IndexedDB
)

# Files and patterns to strictly avoid during cloning (locks, caches, sockets)
IGNORED_PATTERNS = {
    "SingletonLock",
    "SingletonCookie",
    "SingletonSocket",
    "lockfile",
    "Cache",
    "Code Cache",
    "GPUCache",
    "DawnCache",
    "GrShaderCache",
    "Service Worker",
    "Crashpad",
    "BrowserMetrics",
    "OptimizationGuidePredictionModels",
}


def get_default_system_chrome_user_data_dir() -> Path:
    """Detect default Google Chrome user data directory on Windows/macOS/Linux."""
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            p = Path(local_app_data) / "Google" / "Chrome" / "User Data"
            if p.is_dir():
                return p
        app_data = os.environ.get("APPDATA")
        if app_data:
            p = Path(app_data) / "Google" / "Chrome" / "User Data"
            if p.is_dir():
                return p
    return Path.home() / ".config" / "google-chrome"


def get_isolated_flow_profile_dir() -> Path:
    """Return the dedicated, independent Chrome profile directory for Video Factory."""
    custom = os.environ.get("FLOW_PROFILE_PATH") or os.environ.get("HERMES_FLOW_PROFILE_PATH")
    if custom:
        return Path(custom).expanduser().resolve()
    return (get_video_factory_home() / "chrome_flow_profile").resolve()


def _safe_copy_file(src_file: Path, dst_file: Path) -> bool:
    """Copy a single file safely, ignoring active read locks."""
    if not src_file.is_file() or src_file.name in IGNORED_PATTERNS:
        return False
    try:
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst_file)
        return True
    except (PermissionError, OSError) as exc:
        logger.debug("Skipped copying %s due to lock: %s", src_file.name, exc)
        return False


def _safe_copy_tree(src_dir: Path, dst_dir: Path) -> int:
    """Recursively copy directory contents skipping locks and caches."""
    copied = 0
    if not src_dir.is_dir():
        return copied

    for root, dirs, files in os.walk(src_dir):
        # Prune ignored directory patterns
        dirs[:] = [d for d in dirs if d not in IGNORED_PATTERNS]

        rel_root = Path(root).relative_to(src_dir)
        target_root = dst_dir / rel_root

        for file_name in files:
            if file_name in IGNORED_PATTERNS:
                continue
            src_file = Path(root) / file_name
            dst_file = target_root / file_name
            if _safe_copy_file(src_file, dst_file):
                copied += 1

    return copied


def clone_chrome_profile(
    source_user_data_dir: Path | str | None = None,
    target_user_data_dir: Path | str | None = None,
    profile_name: str = "Default",
) -> dict[str, Any]:
    """Clone session and auth state from system Chrome to the isolated bot profile.

    Safe to execute even while system Chrome is running.
    """
    source_dir = (
        Path(source_user_data_dir).expanduser().resolve()
        if source_user_data_dir
        else get_default_system_chrome_user_data_dir()
    )
    target_dir = (
        Path(target_user_data_dir).expanduser().resolve()
        if target_user_data_dir
        else get_isolated_flow_profile_dir()
    )

    if not source_dir.is_dir():
        raise FileNotFoundError(f"Source Chrome User Data dir not found: {source_dir}")

    target_dir.mkdir(parents=True, exist_ok=True)
    target_profile_dir = target_dir / profile_name
    source_profile_dir = source_dir / profile_name

    if not source_profile_dir.is_dir():
        raise FileNotFoundError(f"Source Chrome profile '{profile_name}' not found in {source_dir}")

    copied_files = 0

    # 1. Copy essential root files (Local State is required for decrypting DPAPI cookies on Windows)
    for root_item in ESSENTIAL_ROOT_FILES:
        src_file = source_dir / root_item
        dst_file = target_dir / root_item
        if _safe_copy_file(src_file, dst_file):
            copied_files += 1

    # 2. Copy essential profile items (Cookies, Network, Local Storage)
    for item_name in ESSENTIAL_PROFILE_ITEMS:
        src_item = source_profile_dir / item_name
        dst_item = target_profile_dir / item_name
        if src_item.is_file():
            if _safe_copy_file(src_item, dst_item):
                copied_files += 1
        elif src_item.is_dir():
            copied_files += _safe_copy_tree(src_item, dst_item)

    # 3. Clean any stale lock files from target to ensure Playwright launches cleanly
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock_file = target_dir / lock_name
        if lock_file.exists():
            try:
                if lock_file.is_file() or lock_file.is_symlink():
                    lock_file.unlink()
                elif lock_file.is_dir():
                    shutil.rmtree(lock_file, ignore_errors=True)
            except Exception:
                pass

    logger.info("Successfully cloned Chrome session from %s to %s (%d items copied)", source_dir, target_dir, copied_files)
    return {
        "status": "success",
        "source": str(source_dir),
        "target": str(target_dir),
        "profile": profile_name,
        "copied_files": copied_files,
    }


def find_system_chrome_executable() -> str | None:
    """Find installed Google Chrome binary path."""
    if os.name == "nt":
        candidates = [
            Path(os.environ.get("PROGRAMFILES", "C:\\Program Files")) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)")) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]
        for p in candidates:
            if p.is_file():
                return str(p)
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            which = shutil.which(name)
            if which:
                return which
    return None


def launch_isolated_browser(
    target_user_data_dir: Path | str | None = None,
    profile_name: str = "Default",
    url: str = "https://flow.google.com/",
) -> None:
    """Launch isolated Chrome profile in an interactive window for manual login/verification."""
    target_dir = (
        Path(target_user_data_dir).expanduser().resolve()
        if target_user_data_dir
        else get_isolated_flow_profile_dir()
    )
    target_dir.mkdir(parents=True, exist_ok=True)

    chrome_exe = find_system_chrome_executable()
    if not chrome_exe:
        raise RuntimeError("Google Chrome executable not found on system.")

    args = [
        chrome_exe,
        f"--user-data-dir={target_dir}",
        f"--profile-directory={profile_name}",
        "--new-window",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]
    logger.info("Launching isolated Chrome: %s", " ".join(args))
    if os.name == "nt":
        # First try normal Popen with desktop specification
        try:
            si = subprocess.STARTUPINFO()
            si.lpDesktop = r"WinSta0\default"
            subprocess.Popen(args, startupinfo=si)
            return
        except Exception:
            pass
    subprocess.Popen(args)

