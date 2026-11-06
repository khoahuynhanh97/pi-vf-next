"""Provider-mode and polling configuration resolution for Product Video."""

from __future__ import annotations

from typing import Any


def validate_provider_mode(mode: Any) -> str:
    if isinstance(mode, bool) or not isinstance(mode, str):
        raise ValueError(f"Invalid provider_mode type {type(mode).__name__}: {mode!r}. Must be 'configured' or 'fake'.")
    normalized = mode.strip().lower()
    if not normalized:
        raise ValueError("Invalid provider_mode: empty string. Must be strictly 'configured' or 'fake'.")
    if normalized not in ("configured", "fake"):
        raise ValueError(f"Invalid provider_mode '{mode}'. Must be strictly 'configured' or 'fake'.")
    return normalized


def resolve_provider_mode(
    cli_arg: Any = None,
    config: dict[str, Any] | None = None,
    env_val: Any = None,
) -> str:
    """Resolve provider_mode explicitly across sources without truthiness fallback."""
    cfg = config or {}
    wf_cfg = cfg.get("workflows") if isinstance(cfg.get("workflows"), dict) else {}

    if cli_arg is not None:
        return validate_provider_mode(cli_arg)
    if "provider_mode" in cfg and cfg["provider_mode"] is not None:
        return validate_provider_mode(cfg["provider_mode"])
    if "provider_mode" in wf_cfg and wf_cfg["provider_mode"] is not None:
        return validate_provider_mode(wf_cfg["provider_mode"])
    if env_val is not None:
        return validate_provider_mode(env_val)
    return "configured"


def validate_polling_setting(name: str, value: Any, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"Invalid {name} type bool: {value!r}. Must be a positive number.")
    try:
        val = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid {name} value: {value!r}. Must be a positive number.")
    import math
    if math.isnan(val) or math.isinf(val) or val <= 0:
        raise ValueError(f"Invalid {name} value {val}. Must be finite and > 0.")
    return val


def resolve_polling_setting(
    name: str,
    cli_val: Any = None,
    cfg_val: Any = None,
    default: float = 60.0,
) -> float:
    """Resolve polling setting explicitly across sources without truthiness fallback."""
    if cli_val is not None:
        return validate_polling_setting(name, cli_val, default)
    if cfg_val is not None:
        return validate_polling_setting(name, cfg_val, default)
    return default


__all__ = [
    "validate_provider_mode",
    "resolve_provider_mode",
    "validate_polling_setting",
    "resolve_polling_setting",
]
