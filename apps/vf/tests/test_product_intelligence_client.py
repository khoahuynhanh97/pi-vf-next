from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from video_factory.integrations.product_intelligence.client import ProductIntelligenceClient


def test_research_returns_correlated_result_without_external_io():
    client = ProductIntelligenceClient(fake=True)
    report = json.dumps({"product_name": "TestProduct", "keywords": ["kw1", "kw2"]})
    client._pipeline = MagicMock()
    client._pipeline.run.return_value = SimpleNamespace(summary="offline research", report=report)

    result = client.research(
        "TestProduct", ["kw1", "kw2"], workflow_id="wf-1", job_id="job-1", idempotency_key="research-1"
    )

    assert result["summary"] == "offline research"
    assert json.loads(result["report"]) == {"product_name": "TestProduct", "keywords": ["kw1", "kw2"]}
    assert (result["workflow_id"], result["job_id"], result["idempotency_key"]) == (
        "wf-1", "job-1", "research-1"
    )
    spec = client._pipeline.run.call_args.args[0]
    assert (spec.product_name, spec.keywords) == ("TestProduct", ["kw1", "kw2"])


def test_build_resource_pack_preserves_local_pi_images_and_correlation():
    client = ProductIntelligenceClient(fake=True)
    client._builder = MagicMock()
    client._builder.build.return_value = SimpleNamespace(
        summary="offline pack",
        assets_mapping={"kw1_asset": "placeholder"},
        scene_plan=SimpleNamespace(steps=["intro", "outro"]),
    )
    image = {"asset_id": "pi-image-1", "absolute_path": "D:/pi/image.png", "sha256": "a" * 64}

    pack = client.build_resource_pack(
        {"report": json.dumps({"product_name": "TestProduct", "keywords": ["kw1"]})},
        workflow_id="wf-1",
        job_id="job-2",
        idempotency_key="pack-1",
        images=[image],
    )

    assert pack["assets"][0]["asset_id"] == "pi-image-1"
    assert pack["assets"][0]["local_path"] == "D:/pi/image.png"
    assert pack["assets_mapping"] == {"pi-image-1": "D:/pi/image.png"}
    assert (pack["workflow_id"], pack["job_id"], pack["idempotency_key"]) == (
        "wf-1", "job-2", "pack-1"
    )


def test_lock_resource_pack_invokes_service_with_owner():
    client = ProductIntelligenceClient()
    service = MagicMock()
    service.lock_resource_pack.return_value = {
        "resource_pack_id": "rp_locked",
        "resource_pack_version": 1,
        "status": "locked",
        "assets": [],
        "manifest_digest": "sha256:" + "a" * 64,
    }
    client.service = service

    locked = client.lock_resource_pack(
        {"summary": "TestProduct", "assets_mapping": {"image": "D:/pi/image.png"}},
        owner_user_id="tester",
    )

    service.lock_resource_pack.assert_called_once()
    payload, owner = service.lock_resource_pack.call_args.args
    assert payload["resource_pack_id"].startswith("rp_")
    assert payload["status"] == "unlocked"
    assert payload["assets"] == [{"asset_id": "image", "local_path": "D:/pi/image.png"}]
    assert owner == "tester"
    assert locked["status"] == "locked"
    assert locked["manifest_digest"] == "sha256:" + "a" * 64


def test_lock_resource_pack_id_is_stable_across_process_hash_seeds():
    repo_root = Path(__file__).resolve().parents[1]
    script = """
from video_factory.integrations.product_intelligence.client import ProductIntelligenceClient
class LocalLock:
    def lock_resource_pack(self, payload, owner):
        return payload
client = ProductIntelligenceClient()
client.service = LocalLock()
print(client.lock_resource_pack({'summary': 'same-input'})['resource_pack_id'])
"""
    ids = []
    for seed in ("1", "2"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        env["PYTHONPATH"] = str(repo_root / "src") + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=repo_root, env=env,
            check=True, capture_output=True, text=True,
        )
        ids.append(result.stdout.strip())

    assert ids[0] == ids[1]


def test_malformed_report_raises():
    client = ProductIntelligenceClient()

    try:
        client.build_resource_pack({"summary": "x", "report": "not json"})
    except ValueError as exc:
        assert "Malformed research report" in str(exc)
    else:
        raise AssertionError("Expected malformed research report to fail")
