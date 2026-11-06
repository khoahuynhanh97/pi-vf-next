"""Constants and workspace paths for standalone video-factory."""

from __future__ import annotations

import os
from pathlib import Path


def get_video_factory_home() -> Path:
    """Return root directory for video factory data/workspaces."""
    env_home = os.environ.get("VIDEO_FACTORY_HOME")
    if env_home:
        return Path(env_home).resolve()
    hermes_env = os.environ.get("HERMES_HOME")
    if hermes_env and not hermes_env.lower().startswith("c:"):
        return Path(hermes_env).resolve()
    repo_data = Path(__file__).resolve().parents[2] / "data"
    if repo_data.is_dir():
        return repo_data.resolve()
    return repo_data.resolve()



# Backward compatibility helper
def get_hermes_home() -> Path:
    return get_video_factory_home()


def get_hermes_db_path(override: str | Path | None = None) -> Path:
    """Resolve the shared workflow DB path used by CLI and Studio."""
    if override is not None and str(override).strip():
        return Path(override).expanduser().resolve()
    return get_hermes_home() / "hermes.db"
