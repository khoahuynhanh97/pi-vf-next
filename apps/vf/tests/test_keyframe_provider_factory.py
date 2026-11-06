"""Behavior contract: keyframes always use Google Flow."""

from __future__ import annotations

import pytest

from video_factory.integrations import keyframe_provider_factory as factory


class _Flow:
    def __init__(self, available: bool) -> None:
        self._available = available

    def is_available(self) -> bool:
        return self._available


def _patch(monkeypatch, tmp_path, cfg, flow_available):
    monkeypatch.setattr(factory, "_video_factory_config", lambda: cfg)
    from video_factory.integrations import google_flow_keyframe_provider as gf

    monkeypatch.setattr(
        gf.GoogleFlowKeyframeProvider, "from_config", classmethod(lambda cls, output_dir=None: _Flow(flow_available))
    )


def test_default_uses_google_flow(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, {}, True)
    provider, kind = factory.resolve_keyframe_provider(tmp_path)
    assert kind == "google_flow"
    assert isinstance(provider, _Flow)


def test_flow_unavailable_fails_closed_without_opt_in(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, {}, False)
    with pytest.raises(factory.KeyframeProviderUnavailable):
        factory.resolve_keyframe_provider(tmp_path)


def test_vertex_keyframe_fallback_is_rejected(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, {"keyframe_fallback": "vertex_ai"}, False)
    with pytest.raises(factory.KeyframeProviderUnavailable, match="require Google Flow"):
        factory.resolve_keyframe_provider(tmp_path)


def test_unknown_provider_rejected(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, {"keyframe_provider": "kling"}, True)
    with pytest.raises(factory.KeyframeProviderUnavailable):
        factory.resolve_keyframe_provider(tmp_path)
