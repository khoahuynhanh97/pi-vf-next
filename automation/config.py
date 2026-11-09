"""Configuration and path resolution for PI/VF automation platform."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Timezone definition for daily triggers
BANGKOK_TZ = timezone(timedelta(hours=7), name="Asia/Bangkok")
DAILY_VIDEO_LIMIT = 20

# Repo root: D:\work\pi-vf-next
ROOT_DIR = Path(__file__).resolve().parent.parent

# Apps roots
PI_DIR = ROOT_DIR / "apps" / "pi"
VF_DIR = ROOT_DIR / "apps" / "vf"

# Virtual environment python executables
def _resolve_venv_python(app_dir: Path) -> Path:
    candidates = [
        app_dir / ".venv" / "Scripts" / "python.exe",
        app_dir / ".venv" / "bin" / "python",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return Path(sys.executable)

PI_PYTHON = _resolve_venv_python(PI_DIR)
VF_PYTHON = _resolve_venv_python(VF_DIR)

# Runtime paths
RUNTIME_DIR = ROOT_DIR / "runtime"
EXCHANGE_DIR = RUNTIME_DIR / "exchange"
INBOX_ROOT = EXCHANGE_DIR / "inbox"
PI_RUNS_DIR = RUNTIME_DIR / "pi" / "runs"
PI_SHEET_ROOT = RUNTIME_DIR / "pi" / "sheet_daily"
VF_RUNTIME_DIR = RUNTIME_DIR / "vf"
VF_DB_PATH = VF_RUNTIME_DIR / "hermes.db"
VF_WORKSPACE_DIR = VF_RUNTIME_DIR / "workspace"
VF_PROJECTS_DIR = VF_WORKSPACE_DIR / "projects"
VF_EXPORTS_DIR = VF_WORKSPACE_DIR / "exports"

# Default profile and input sheet
DEFAULT_PROFILE = VF_DIR / "config" / "creative_run_profile.json"
DEFAULT_FOOD_PROFILE = VF_DIR / "data" / "run_profiles" / "bibigo-food-pov-20261025-v2.json"


def ensure_runtime_dirs() -> None:
    """Ensure all required runtime directories exist."""
    INBOX_ROOT.mkdir(parents=True, exist_ok=True)
    PI_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    VF_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    VF_WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    VF_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    VF_EXPORTS_DIR.mkdir(parents=True, exist_ok=True)


def now_bangkok() -> datetime:
    """Get current datetime in Asia/Bangkok (UTC+7)."""
    return datetime.now(BANGKOK_TZ)


def today_bangkok_str() -> str:
    """Get current date formatted as YYYY-MM-DD in Asia/Bangkok."""
    return now_bangkok().strftime("%Y-%m-%d")
