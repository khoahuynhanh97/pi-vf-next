from __future__ import annotations

import sys
import types

import pytest

from video_factory.integrations import keyframe_provider_factory as factory


class _Flow:
    provider_kind = "google_flow"

    def __init__(self, available: bool) -> None:
        self._available = available

    def is_available(self) -> bool:
        return self._available


def _install_flow(monkeypatch: pytest.MonkeyPatch, flow: _Flow) -> None:
    class _Provider:
        @classmethod
        def from_config(cls, output_dir=None):
            return flow

    monkeypatch.setitem(
        sys.modules,
        "video_factory.integrations.google_flow_keyframe_provider",
        types.SimpleNamespace(GoogleFlowKeyframeProvider=_Provider),
    )


def test_keyframe_resolver_ignores_video_provider_settings(monkeypatch, tmp_path):
    flow = _Flow(available=True)
    _install_flow(monkeypatch, flow)
    monkeypatch.setattr(
        factory,
        "_video_factory_config",
        lambda: {"keyframe_provider": "vertex_ai", "keyframe_fallback": "vertex_ai"},
    )

    provider, kind = factory.resolve_keyframe_provider(tmp_path)

    assert provider is flow
    assert kind == "google_flow"


def test_keyframe_resolver_fails_closed_when_flow_is_unavailable(monkeypatch, tmp_path):
    _install_flow(monkeypatch, _Flow(available=False))
    monkeypatch.setattr(
        factory,
        "_video_factory_config",
        lambda: {"keyframe_provider": "vertex_ai", "keyframe_fallback": "vertex_ai"},
    )

    with pytest.raises(factory.KeyframeProviderUnavailable, match="Google Flow"):
        factory.resolve_keyframe_provider(tmp_path)
