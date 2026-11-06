from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import urllib.error

import pytest

from video_factory.integrations.product_intelligence.client import ProductIntelligenceClient
from video_factory.integrations.nine_router import _route_from_config
from video_factory.qc.evaluator import _urlopen_with_backoff


def test_build_resource_pack_with_source_context_images():
    client = ProductIntelligenceClient(fake=True)
    research_result = {
        "report": json.dumps({"product_name": "Test Product", "keywords": ["test"]}),
        "media_assets": [
            {
                "asset_id": "asset_media_1",
                "local_path": "/path/to/media1.jpg",
                "sha256": "hash1",
            }
        ],
    }
    source_context = {
        "images": [
            {
                "asset_id": "asset_sc_1",
                "local_path": "/path/to/sc1.jpg",
                "sha256": "hash_sc1",
            }
        ]
    }
    # When source_context is provided, its images are preferred
    pack = client.build_resource_pack(
        research_result,
        workflow_id="wf_test_1",
        source_context=source_context,
    )
    assert "product_references" in pack
    assert len(pack["product_references"]) == 1
    assert pack["product_references"][0]["asset_id"] == "asset_sc_1"

    # When source_context is omitted, media_assets in research_result are used as fallback
    pack_fallback = client.build_resource_pack(
        research_result,
        workflow_id="wf_test_2",
    )
    assert "product_references" in pack_fallback
    assert len(pack_fallback["product_references"]) == 1
    assert pack_fallback["product_references"][0]["asset_id"] == "asset_media_1"


def test_route_from_config_disabled():
    cfg = {
        "product_video": {
            "nine_router": {
                "enabled": True,
                "endpoint": "http://127.0.0.1:20128/v1",
                "routes": {
                    "vision_qc": {
                        "enabled": False,
                        "combo_id": "some_combo",
                        "combo_name": "SomeCombo",
                    },
                    "creative": {
                        "enabled": True,
                        "combo_id": "creative_combo",
                        "combo_name": "CreativeCombo",
                        "model_id": "gemini-3.8-flash",
                        "execution_mode": "combo",
                    },
                },
            }
        }
    }
    # Disabled route returns None
    assert _route_from_config("vision_qc", config=cfg) is None
    # Enabled route returns NineRouterRoute
    creative_route = _route_from_config("creative", config=cfg)
    assert creative_route is not None
    assert creative_route.combo_name == "CreativeCombo"


def test_urlopen_with_backoff_retries_429():
    req = MagicMock()
    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{"success": true}'
    mock_resp.__enter__.return_value = mock_resp

    # First call 429, second call succeeds
    err_429 = urllib.error.HTTPError(
        url="http://example.com",
        code=429,
        msg="Rate limit",
        hdrs={},
        fp=MagicMock(read=lambda *a, **k: b"Rate limit"),
    )

    with patch("urllib.request.urlopen", side_effect=[err_429, mock_resp]) as mock_open:
        with patch("time.sleep") as mock_sleep:
            res = _urlopen_with_backoff(req, timeout=10.0, max_retries=2, backoff=0.01)
            assert res == {"success": True}
            assert mock_open.call_count == 2
            mock_sleep.assert_called_once_with(0.01)
