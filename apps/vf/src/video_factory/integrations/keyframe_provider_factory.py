"""Single resolver for the keyframe (image) provider.

Product Video keyframes are generated through Google Flow (Playwright). Vertex AI
is not an eligible keyframe fallback for this workflow; an unverified or
unavailable Flow session waits for operator action instead of changing the
conditioning provider.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from video_factory.integrations.ports import KeyframeGenerationPort

_FLOW = "google_flow"


class KeyframeProviderUnavailable(RuntimeError):
    """Google Flow cannot run for the requested keyframe."""


def _video_factory_config() -> dict[str, Any]:
    try:
        from video_factory.config import load_config
        cfg = load_config()
    except ImportError:
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
        except ImportError:
            cfg = {}
    vf_cfg = cfg.get("video_factory", {}) if isinstance(cfg, dict) else {}
    return vf_cfg if isinstance(vf_cfg, dict) else {}


def resolve_keyframe_provider(output_dir: Path | str, *, image_provider_kind='google_flow',
                              creative_mode='legacy', muse_adapter=None, provider_mode: str | None = None) -> tuple[KeyframeGenerationPort, str]:
    """Return the Google Flow image provider.

    Provider selection belongs to video rendering. Image/keyframe generation
    has one provider by contract, so render-provider settings are deliberately
    ignored here.
    """
    if image_provider_kind=='muse':
        if creative_mode!='four_agent' or muse_adapter is None:
            raise KeyframeProviderUnavailable('Muse image operations require an explicit four_agent adapter/profile')
        from .muse_keyframe_adapter import MuseKeyframeAdapter
        return MuseKeyframeAdapter(muse_adapter), 'muse'
    if image_provider_kind!='google_flow':
        raise KeyframeProviderUnavailable('unsupported image provider; no automatic fallback')
    from video_factory.integrations.google_flow_keyframe_provider import (
        GoogleFlowKeyframeProvider,
    )

    # Configured mode or non-fake mode must strictly use the real Google Flow provider
    # Mock is only allowed when fake mode is explicitly passed:
    if provider_mode == "fake" and os.environ.get("VF_TEST_MOCK_FLOW") == "1":
        from video_factory.integrations.test_flow_mock import TestMockFlowKeyframeProvider
        return TestMockFlowKeyframeProvider(output_dir=output_dir), _FLOW
    flow = GoogleFlowKeyframeProvider.from_config(output_dir=output_dir)
    if flow.is_available():
        return flow, _FLOW
    raise KeyframeProviderUnavailable(
        "Google Flow keyframe session is unavailable (profile missing, not logged in, "
        "or its UI/upload/cost contracts are unverified). Log in to the configured "
        "Google Flow profile and complete operator verification; Vertex is not an allowed fallback."
    )
