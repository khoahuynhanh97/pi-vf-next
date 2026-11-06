"""Configuration loader for Video Factory with fallback to ~/.hermes/config.yaml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def load_config() -> dict[str, Any]:
    """Load configuration from config.yaml or return empty dict."""
    candidates = [
        Path.cwd() / "config.yaml",
        Path.cwd() / "apps" / "vf" / "config.yaml",
        Path(__file__).resolve().parents[2] / "config.yaml",
        Path(__file__).resolve().parents[3] / "config.yaml",
        Path.home() / ".hermes" / "config.yaml",
    ]
    for p in candidates:
        if p.is_file():
            try:
                content = p.read_text(encoding="utf-8")
                data = yaml.safe_load(content)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return {}


def load_config_readonly() -> dict[str, Any]:
    return load_config()
