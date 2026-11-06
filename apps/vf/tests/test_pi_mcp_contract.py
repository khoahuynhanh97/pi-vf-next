from __future__ import annotations

import hashlib
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image

from video_factory.handlers import (
    JobContext,
    JobFailed,
    JobSucceeded,
    MCPProductIntelligenceAdapter,
    ProductVideoJobHandlers,
    ProductVideoWorkflowState,
    TerminalJobError,
)
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.engine import WorkflowEngine
from video_factory.workflows.repository import WorkflowRepository


class StrictPIMCPToolRegistry:
    """Strict mock registry simulating the exact signatures and schemas of the located
    product_scout/mcp_server.py tools, rejecting unsupported keywords or invalid types.
    """

    def __init__(self) -> None:
        self.dispatched_calls: list[tuple[str, dict[str, Any]]] = []
        self.research_store: dict[str, dict[str, Any]] = {}
        self.pack_store: dict[str, dict[str, Any]] = {}
        self.lock_store: dict[str, dict[str, Any]] = {}

    def get_all_tool_names(self) -> set[str]:
        return {
            "mcp__product_intelligence__research_product",
            "mcp__product_intelligence__get_product_research",
            "mcp__product_intelligence__build_resource_pack",
            "mcp__product_intelligence__lock_resource_pack",
        }

    def dispatch(self, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.dispatched_calls.append((tool_name, payload))

        if tool_name == "mcp__product_intelligence__research_product":
            # Strict signature check: must NOT receive unsupported 'keywords' or 'pdp_url' keys
            forbidden_keys = {"keywords", "pdp_url", "research_result", "pack_dict"}
            unexpected = forbidden_keys.intersection(payload.keys())
            if unexpected:
                raise TypeError(f"research_product received unexpected arguments: {unexpected}")

            p_name = payload.get("product_name") or "Unknown"
            snap_id = f"snap_{len(self.research_store) + 1:03d}"
            res = {
                "status": "success",
                "error": None,
                "research_id": f"res_{snap_id}",
                "snapshot_id": snap_id,
                "persistence_status": "saved",  # exact media/research_persistence/service.py contract
                "source_summary": {"attempted": 2, "usable": 2, "challenged": 0, "blocked": 0, "failed": 0},
                "canonical_products": [
                    {
                        "canonical_product_id": f"prod_{snap_id}",
                        "name": p_name,
                        "brand": "Acme",
                        "model": "Model-X",
                        "variants": [],
                        "listings": [{"listing_id": "list_01", "host": "example.com", "seller": "AcmeStore", "price": 99.99, "currency": "USD", "availability": "in_stock"}],
                    }
                ],
                "media_assets": [
                    {
                        "asset_id": f"asset_{snap_id}_01",
                        "local_path": f"/data/media/{snap_id}_01.jpg",
                        "match_status": "matched",
                        "product_match_score": 0.95,
                        "usage_policy": "approved",
                    }
                ],
                "summary": {"usable_sources": 2, "total_assets": 1},
                "report": {"product_name": p_name, "query": payload.get("query"), "url": payload.get("url")},
            }
            self.research_store[snap_id] = res
            return res

        elif tool_name == "mcp__product_intelligence__get_product_research":
            snap_id = payload.get("snapshot_id")
            result = self.research_store.get(snap_id)
            if result is None:
                return {"status": "not_found", "snapshot_id": snap_id}
            return {
                "status": "found",
                "snapshot_id": snap_id,
                "research_id": result.get("research_id"),
                "result": result,
            }

        elif tool_name == "mcp__product_intelligence__build_resource_pack":
            # Strict signature check: must receive 'snapshot_id', and optionally 'variant_id'
            forbidden_keys = {"research_result", "pack_dict", "product_name", "keywords", "url"}
            unexpected = forbidden_keys.intersection(payload.keys())
            if unexpected:
                raise TypeError(f"build_resource_pack received unexpected arguments: {unexpected}")

            snap_id = payload.get("snapshot_id")
            if not snap_id:
                raise ValueError("snapshot_id is required")

            if snap_id not in self.research_store:
                return {"status": "not_found", "snapshot_id": snap_id}

            pack_id = f"rp_{snap_id}"
            pack_res = {
                "status": "built",
                "resource_pack_id": pack_id,
                "pack_status": "ready_to_lock",
                "reason": None,
                "canonical_product_id": f"prod_{snap_id}",
                "variant_id": payload.get("variant_id"),
                "media_count": 2,
                "claims": [{"text": "High quality product", "status": "verified"}],
                "restrictions": [],
                "completeness": {"has_images": True, "has_specs": True},
                "assets_mapping": {
                    f"asset_{snap_id}_01": f"/data/media/{snap_id}_01.jpg",
                    f"asset_{snap_id}_02": f"/data/media/{snap_id}_02.jpg",
                },
                "scene_plan": ["intro", "features", "outro"],
            }
            self.pack_store[pack_id] = pack_res
            return pack_res

        elif tool_name == "mcp__product_intelligence__lock_resource_pack":
            # Strict signature check: must receive 'resource_pack_id', and optionally 'owner_user_id'
            forbidden_keys = {"pack_dict", "research_result", "snapshot_id"}
            unexpected = forbidden_keys.intersection(payload.keys())
            if unexpected:
                raise TypeError(f"lock_resource_pack received unexpected arguments: {unexpected}")

            pack_id = payload.get("resource_pack_id")
            owner_user_id = payload.get("owner_user_id")

            if not pack_id or pack_id not in self.pack_store:
                return {"status": "not_found", "resource_pack_id": pack_id}

            snap_id = pack_id.removeprefix("rp_")
            lock_id = f"lock_{pack_id}"
            lock_res = {
                "status": "locked",
                "lock_id": lock_id,
                "resource_pack_id": pack_id,
                "snapshot_id": snap_id,
                "canonical_product_id": f"prod_{snap_id}",
                "owner_user_id": owner_user_id,
                "version": 1,
                "manifest_digest": f"sha256:manifest_digest_{pack_id}",
                "created_at": "2026-08-28T12:00:00Z",
                "assets": [
                    {
                        "asset_id": f"asset_{snap_id}_01",
                        "local_path": f"/data/media/{snap_id}_01.jpg",
                        "media_role": "original",
                        "source_url": "https://example.com/image.jpg",
                        "match_confidence": 0.98,
                    },
                    {
                        "asset_id": f"asset_{snap_id}_02",
                        "local_path": f"/data/media/{snap_id}_02.jpg",
                        "media_role": "original",
                        "source_url": "https://example.com/image2.jpg",
                        "match_confidence": 0.95,
                    },
                ],
            }
            self.lock_store[lock_id] = lock_res
            return lock_res

        raise ValueError(f"Unknown tool: {tool_name}")


def test_pi_adapter_outgoing_parameter_mapping():
    """Test that MCPProductIntelligenceAdapter maps pdp_url to url, keywords to query, and avoids unexpected keys."""
    registry = StrictPIMCPToolRegistry()
    wire_tools = {
        "research": "mcp__product_intelligence__research_product",
        "build_resource_pack": "mcp__product_intelligence__build_resource_pack",
        "lock_resource_pack": "mcp__product_intelligence__lock_resource_pack",
    }
    adapter = MCPProductIntelligenceAdapter(registry, wire_tools)

    # 1. Research mapping
    research_res = adapter.research(
        product_name="Drone 4K",
        keywords=["gimbal", "camera", "drone"],
        pdp_url="https://example.com/pdp/drone-4k",
    )
    assert len(registry.dispatched_calls) == 1
    tool, payload = registry.dispatched_calls[0]
    assert tool == "mcp__product_intelligence__research_product"
    assert payload == {
        "product_name": "Drone 4K",
        "url": "https://example.com/pdp/drone-4k",
        "query": "gimbal camera drone",
        "discover_sources": False,
    }
    assert "keywords" not in payload
    assert "pdp_url" not in payload
    assert research_res["snapshot_id"] == "snap_001"
    assert research_res["persistence_status"] == "saved"
    assert research_res["product_name"] == "Drone 4K"
    assert research_res["pdp_url"] == "https://example.com/pdp/drone-4k"

    # 2. Build pack mapping
    pack_res = adapter.build_resource_pack(research_res, variant_id="var_v1")
    assert len(registry.dispatched_calls) == 2
    tool2, payload2 = registry.dispatched_calls[1]
    assert tool2 == "mcp__product_intelligence__build_resource_pack"
    assert payload2 == {
        "snapshot_id": "snap_001",
        "variant_id": "var_v1",
    }
    assert "research_result" not in payload2
    assert pack_res["resource_pack_id"] == "rp_snap_001"
    assert pack_res["raw_result"]["version"] == pack_res["version"] == 1
    assert pack_res["pack_status"] == "ready_to_lock"
    assert pack_res["canonical_product_id"] == "prod_snap_001"
    assert pack_res["variant_id"] == "var_v1"

    # 3. Lock pack mapping
    lock_res = adapter.lock_resource_pack(pack_res, owner_user_id="user_admin")
    assert len(registry.dispatched_calls) == 3
    tool3, payload3 = registry.dispatched_calls[2]
    assert tool3 == "mcp__product_intelligence__lock_resource_pack"
    assert payload3 == {
        "resource_pack_id": "rp_snap_001",
        "owner_user_id": "user_admin",
    }
    assert "pack_dict" not in payload3
    assert lock_res["lock_id"] == "lock_rp_snap_001"
    assert lock_res["owner_user_id"] == "user_admin"
    assert lock_res["manifest_digest"] == "sha256:manifest_digest_rp_snap_001"
    assert lock_res["snapshot_id"] == "snap_001"
    assert lock_res["canonical_product_id"] == "prod_snap_001"
    assert lock_res["variant_id"] == "var_v1"
    assert len(lock_res["assets"]) == 2
    assert lock_res["assets"][0]["asset_id"] == "asset_snap_001_01"


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("canonical_product_id", "prod_another_snapshot", "PRODUCT_MISMATCH"),
        ("canonical_product_id", None, "MISSING_CANONICAL_PRODUCT_ID"),
        ("variant_id", "var_other", "VARIANT_MISMATCH"),
        ("variant_id", None, "MISSING_VARIANT_ID"),
    ],
)
def test_pi_resource_pack_must_match_researched_product_and_requested_variant(
    monkeypatch, field: str, value: str, expected_code: str
) -> None:
    registry = StrictPIMCPToolRegistry()
    adapter = MCPProductIntelligenceAdapter(
        registry,
        {
            "research": "mcp__product_intelligence__research_product",
            "build_resource_pack": "mcp__product_intelligence__build_resource_pack",
        },
    )
    research_result = adapter.research("Acme Model-X", ["Model-X"])
    dispatch = registry.dispatch

    def mismatched_pack(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = dispatch(tool_name, payload)
        if tool_name == "mcp__product_intelligence__build_resource_pack":
            result[field] = value
        return result

    monkeypatch.setattr(registry, "dispatch", mismatched_pack)
    with pytest.raises(TerminalJobError) as exc_info:
        adapter.build_resource_pack(research_result, variant_id="var_v1")
    assert exc_info.value.code == expected_code


def test_pi_lock_rejects_provider_variant_mismatch(monkeypatch) -> None:
    registry = StrictPIMCPToolRegistry()
    adapter = MCPProductIntelligenceAdapter(
        registry,
        {
            "research": "mcp__product_intelligence__research_product",
            "build_resource_pack": "mcp__product_intelligence__build_resource_pack",
            "lock_resource_pack": "mcp__product_intelligence__lock_resource_pack",
        },
    )
    research_result = adapter.research("Acme Model-X", ["Model-X"])
    pack = adapter.build_resource_pack(research_result, variant_id="var_v1")
    dispatch = registry.dispatch

    def mismatched_lock(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = dispatch(tool_name, payload)
        if tool_name == "mcp__product_intelligence__lock_resource_pack":
            result["variant_id"] = "var_other"
        return result

    monkeypatch.setattr(registry, "dispatch", mismatched_lock)
    with pytest.raises(TerminalJobError) as exc_info:
        adapter.lock_resource_pack(pack, owner_user_id="user_admin")
    assert exc_info.value.code == "VARIANT_MISMATCH"


def test_pi_adapter_discovers_sources_from_manifest_identity_when_pdp_is_missing():
    registry = StrictPIMCPToolRegistry()
    adapter = MCPProductIntelligenceAdapter(
        registry,
        {"research": "mcp__product_intelligence__research_product"},
    )

    with pytest.raises(TerminalJobError):
        adapter.research(
            product_name="Đèn bàn thông minh Xiaomi Mijia Desk Lamp Lite",
            keywords=["Xiaomi Mijia Desk Lamp Lite"],
            pdp_url=None,
            brand="Xiaomi",
            model="Mijia Desk Lamp Lite",
            canonical_product_key="xiaomi:mijia-desk-lamp-lite",
        )

    _tool, payload = registry.dispatched_calls[0]
    assert payload["brand"] == "Xiaomi"
    assert payload["model"] == "Mijia Desk Lamp Lite"
    assert payload["canonical_product_key"] == "xiaomi:mijia-desk-lamp-lite"
    assert payload["discover_sources"] is True


def test_pi_adapter_reuses_persisted_research_snapshot_without_rerunning_research():
    registry = StrictPIMCPToolRegistry()
    wire_tools = {
        "research": "mcp__product_intelligence__research_product",
        "get_research": "mcp__product_intelligence__get_product_research",
    }
    adapter = MCPProductIntelligenceAdapter(registry, wire_tools)

    first = adapter.research("Snapshot Lamp", ["lamp"])
    registry.dispatched_calls.clear()
    reused = adapter.research(
        "Snapshot Lamp",
        ["lamp"],
        research_snapshot_id=first["snapshot_id"],
        brand="Acme",
        model="Model-X",
    )

    assert registry.dispatched_calls == [
        ("mcp__product_intelligence__get_product_research", {"snapshot_id": first["snapshot_id"]})
    ]
    assert reused["snapshot_id"] == first["snapshot_id"]
    assert reused["persistence_status"] == "saved"


def test_build_resource_pack_missing_or_failed_snapshot_fails_closed():
    """Test that build_resource_pack fails closed with TerminalJobError if snapshot_id is missing or non-persisted."""
    registry = StrictPIMCPToolRegistry()
    wire_tools = {
        "build_resource_pack": "mcp__product_intelligence__build_resource_pack",
    }
    adapter = MCPProductIntelligenceAdapter(registry, wire_tools)

    # Missing snapshot_id
    with pytest.raises(TerminalJobError) as exc1:
        adapter.build_resource_pack({"report": "{}"})
    assert exc1.value.code == "MISSING_SNAPSHOT_ID"
    assert len(registry.dispatched_calls) == 0

    # Failed or skipped persistence status
    for bad_status in ("failed", "unpersisted", "missing", "skipped", None):
        with pytest.raises(TerminalJobError) as exc2:
            adapter.build_resource_pack({"snapshot_id": "snap_bad", "persistence_status": bad_status})
        assert exc2.value.code == "PERSISTENCE_FAILED"
    assert len(registry.dispatched_calls) == 0


def test_build_resource_pack_snapshot_not_found_raises_terminal_error():
    """Test that provider returning not_found for snapshot raises TerminalJobError."""
    registry = StrictPIMCPToolRegistry()
    wire_tools = {
        "build_resource_pack": "mcp__product_intelligence__build_resource_pack",
    }
    adapter = MCPProductIntelligenceAdapter(registry, wire_tools)

    with pytest.raises(TerminalJobError) as exc_info:
        adapter.build_resource_pack({"snapshot_id": "snap_999", "persistence_status": "saved"})
    assert exc_info.value.code == "SNAPSHOT_NOT_FOUND"


def test_lock_resource_pack_non_lockable_status_and_missing_owner_fails_closed():
    """Test that lock_resource_pack fails closed if pack_status is not ready_to_lock or owner is missing."""
    registry = StrictPIMCPToolRegistry()
    wire_tools = {
        "lock_resource_pack": "mcp__product_intelligence__lock_resource_pack",
    }
    adapter = MCPProductIntelligenceAdapter(registry, wire_tools)

    # Missing owner
    with pytest.raises(TerminalJobError) as exc_owner:
        adapter.lock_resource_pack({"resource_pack_id": "rp_001", "pack_status": "ready_to_lock"}, owner_user_id="")
    assert exc_owner.value.code == "MISSING_OWNER"

    # Non ready_to_lock statuses (denies needs_review, incomplete, rejected, built, empty, unknown)
    for bad_status in ("needs_review", "incomplete", "rejected", "built", "", None, "unknown"):
        with pytest.raises(TerminalJobError) as exc_status:
            adapter.lock_resource_pack({"resource_pack_id": "rp_001", "pack_status": bad_status}, owner_user_id="user1")
        assert exc_status.value.code == "NON_LOCKABLE_STATUS"
    assert len(registry.dispatched_calls) == 0


def test_lock_resource_pack_mismatches_raise_terminal_error():
    """Test that contradictory owner, pack_id, snapshot_id, or product_id raises TerminalJobError."""
    registry = StrictPIMCPToolRegistry()
    # Seed a pack
    registry.pack_store["rp_test"] = {"resource_pack_id": "rp_test"}
    wire_tools = {
        "lock_resource_pack": "mcp__product_intelligence__lock_resource_pack",
    }
    adapter = MCPProductIntelligenceAdapter(registry, wire_tools)

    orig_dispatch = registry.dispatch

    # 1. Owner mismatch
    def dispatch_owner_mismatch(tool_name, payload):
        res = orig_dispatch(tool_name, payload)
        res["owner_user_id"] = "different_user_attacker"
        return res

    registry.dispatch = dispatch_owner_mismatch  # type: ignore
    with pytest.raises(TerminalJobError) as exc1:
        adapter.lock_resource_pack({"resource_pack_id": "rp_test", "pack_status": "ready_to_lock"}, owner_user_id="legit_user")
    assert exc1.value.code == "OWNER_MISMATCH"

    # 2. Snapshot mismatch
    def dispatch_snap_mismatch(tool_name, payload):
        res = orig_dispatch(tool_name, payload)
        res["snapshot_id"] = "contradictory_snap_999"
        return res

    registry.dispatch = dispatch_snap_mismatch  # type: ignore
    with pytest.raises(TerminalJobError) as exc2:
        adapter.lock_resource_pack({"resource_pack_id": "rp_test", "pack_status": "ready_to_lock", "snapshot_id": "snap_orig"}, owner_user_id="system")
    assert exc2.value.code == "SNAPSHOT_MISMATCH"


def test_product_video_job_handlers_roundtrip_identifiers_through_sqlite(tmp_path):
    """Test end-to-end round-trip of PI identifiers (snapshot_id, resource_pack_id, lock_id, variant_id, assets, manifest)
    through ProductVideoJobHandlers and real SQLite workflow persistence.
    """
    db_file = tmp_path / "pi_contract_roundtrip.db"
    conn = connect(db_file)
    repo = WorkflowRepository(conn)
    wf_engine = WorkflowEngine(repo)

    source_image = tmp_path / "pi-source.png"
    Image.new("RGB", (32, 32), "silver").save(source_image)
    registry = StrictPIMCPToolRegistry()
    wire_tools = {
        "research": "mcp__product_intelligence__research_product",
        "build_resource_pack": "mcp__product_intelligence__build_resource_pack",
        "lock_resource_pack": "mcp__product_intelligence__lock_resource_pack",
    }
    pi_adapter = MCPProductIntelligenceAdapter(registry, wire_tools)
    original_dispatch = registry.dispatch

    def dispatch_with_local_pi_media(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = original_dispatch(tool_name, payload)
        asset = {"asset_id": "asset_snap_001_01", "local_path": str(source_image)}
        if tool_name == "mcp__product_intelligence__research_product":
            result["media_assets"] = [asset]
        elif tool_name == "mcp__product_intelligence__build_resource_pack":
            result["assets_mapping"] = {asset["asset_id"]: asset["local_path"]}
            result["media_count"] = 1
        elif tool_name == "mcp__product_intelligence__lock_resource_pack":
            result["assets"] = [asset]
        return result

    registry.dispatch = dispatch_with_local_pi_media  # type: ignore[method-assign]

    from video_factory.integrations.client import VideoFactoryClient

    handlers = ProductVideoJobHandlers(
        pi_client=pi_adapter,
        video_client=VideoFactoryClient(),
        workflow_repository=repo,
        provider_mode="configured",
        evaluator=MagicMock(
            evaluator_name="offline-pi-contract",
            evaluator_version="1",
            model="offline",
            evaluate_image=MagicMock(return_value={
                "identity_score": 55,
                "clarity_score": 23,
                "usability_score": 12,
                "total_score": 90,
                "verdict": "pass",
                "eligible": True,
                "hard_failure": False,
                "source_branding_detected": False,
                "identity_mismatch": False,
                "view_angle": "front",
                "shot_type": "product",
                "visible_details": ["product body"],
                "scene_suitability": ["hero"],
                "risk_flags": [],
                "text_coverage_percent": 0,
                "reason": "Offline contract fixture is a usable reference",
            }),
        ),
    )

    wf_id = "wf_pi_contract_01"

    # Step 1: Initial research job
    repo.commit_workflow(
        workflow_id=wf_id,
        aggregate_json=json.dumps({"state": ProductVideoWorkflowState.RESEARCHING.value}),
        events=[{"event_id": "e_init", "command_id": "c_init", "event_type": "WorkflowStarted", "timestamp": "2026-08-28T00:00:00Z", "payload": {}}],
        expected_version=0,
    )

    ctx_res = JobContext(workflow_id=wf_id, job_id="job_res_01", owner_user_id="operator_user")
    res_out = handlers.research_job(ctx_res, product_name="Wireless ANC Earbuds", keywords=["bluetooth", "anc", "audio"], pdp_url="https://example.com/pdp/earbuds")
    assert isinstance(res_out, JobSucceeded)
    research_art = res_out.artifact_ref
    assert research_art["snapshot_id"] == "snap_001"
    assert research_art["persistence_status"] == "saved"

    # Persist research result and advance workflow to BUILDING_RESOURCE_PACK
    repo.commit_workflow(
        workflow_id=wf_id,
        aggregate_json=json.dumps({"state": ProductVideoWorkflowState.BUILDING_RESOURCE_PACK.value, "research_result": research_art}),
        events=[{"event_id": "e_res", "command_id": "c_res", "event_type": "ResearchCompleted", "timestamp": "2026-08-28T00:01:00Z", "payload": {"artifact": research_art}}],
        expected_version=1,
    )

    # Step 2: Build resource pack job
    ctx_pack = JobContext(workflow_id=wf_id, job_id="job_pack_01", owner_user_id="operator_user")
    pack_out = handlers.resource_pack_job(
        ctx_pack,
        research_result=research_art,
    )
    assert isinstance(pack_out, JobSucceeded)
    pack_art = pack_out.artifact_ref
    assert pack_art["resource_pack_id"] == "rp_snap_001"
    assert pack_art["snapshot_id"] == "snap_001"
    assert pack_art["canonical_product_id"] == "prod_snap_001"
    assert pack_art["pack_status"] == "ready_to_lock"
    assert pack_art["assets"][0]["asset_id"] == "asset_snap_001_01"
    assert pack_art["assets"][0]["local_path"] == str(source_image)
    assert pack_art["assets"][0]["sha256"] == hashlib.sha256(source_image.read_bytes()).hexdigest()

    # Persist pack result
    repo.commit_workflow(
        workflow_id=wf_id,
        aggregate_json=json.dumps({"state": ProductVideoWorkflowState.LOCKING_RESOURCE_PACK.value, "resource_pack": pack_art}),
        events=[{"event_id": "e_pack", "command_id": "c_pack", "event_type": "ResourcePackBuilt", "timestamp": "2026-08-28T00:02:00Z", "payload": {"artifact": pack_art}}],
        expected_version=2,
    )

    # Step 3: Lock resource pack job
    ctx_lock = JobContext(workflow_id=wf_id, job_id="job_lock_01", owner_user_id="operator_user")
    lock_out = handlers.lock_job(ctx_lock, pack_dict=pack_art)
    assert isinstance(lock_out, JobSucceeded)
    lock_art = lock_out.artifact_ref
    assert lock_art["lock_id"] == "lock_rp_snap_001"
    assert lock_art["resource_pack_id"] == "rp_snap_001"
    assert lock_art["snapshot_id"] == "snap_001"
    assert lock_art["canonical_product_id"] == "prod_snap_001"
    assert lock_art["owner_user_id"] == "operator_user"
    assert lock_art["manifest_digest"] == "sha256:manifest_digest_rp_snap_001"
    assert len(lock_art["assets"]) == 1
    assert lock_art["assets"][0]["asset_id"] == "asset_snap_001_01"
    assert lock_art["assets"][0]["local_path"] == str(source_image)

    # Persist locked artifact
    repo.commit_workflow(
        workflow_id=wf_id,
        aggregate_json=json.dumps({"state": ProductVideoWorkflowState.CREATING_BRIEF.value, "locked_pack": lock_art}),
        events=[{"event_id": "e_lock", "command_id": "c_lock", "event_type": "ResourcePackLocked", "timestamp": "2026-08-28T00:03:00Z", "payload": {"artifact": lock_art}}],
        expected_version=3,
    )
    conn.close()
    # Step 4: Reopen SQLite connection from scratch to verify durability across restart
    conn2 = connect(db_file)
    repo2 = WorkflowRepository(conn2)
    loaded_wf, ver = repo2.get_workflow(wf_id)
    assert ver == 4
    assert loaded_wf["state"] == ProductVideoWorkflowState.CREATING_BRIEF.value
    assert loaded_wf["locked_pack"]["lock_id"] == "lock_rp_snap_001"
    assert loaded_wf["locked_pack"]["resource_pack_id"] == "rp_snap_001"
    assert loaded_wf["locked_pack"]["snapshot_id"] == "snap_001"
    assert loaded_wf["locked_pack"]["canonical_product_id"] == "prod_snap_001"
    assert loaded_wf["locked_pack"]["owner_user_id"] == "operator_user"
    assert loaded_wf["locked_pack"]["manifest_digest"] == "sha256:manifest_digest_rp_snap_001"
    assert loaded_wf["locked_pack"]["assets"][0]["asset_id"] == "asset_snap_001_01"
    conn2.close()


def test_handlers_missing_prerequisites_return_non_retryable_failed(tmp_path):
    """Test that handlers return non-retryable JobFailed on missing snapshot or non-lockable status."""
    db_file = tmp_path / "prereq_test.db"
    conn = connect(db_file)
    repo = WorkflowRepository(conn)
    wf_engine = WorkflowEngine(repo)

    registry = StrictPIMCPToolRegistry()
    wire_tools = {
        "research": "mcp__product_intelligence__research_product",
        "build_resource_pack": "mcp__product_intelligence__build_resource_pack",
        "lock_resource_pack": "mcp__product_intelligence__lock_resource_pack",
    }
    pi_adapter = MCPProductIntelligenceAdapter(registry, wire_tools)

    from video_factory.integrations.client import VideoFactoryClient

    handlers = ProductVideoJobHandlers(
        pi_client=pi_adapter,
        video_client=VideoFactoryClient(),
        workflow_repository=repo,
        provider_mode="configured",
    )

    wf_id = "wf_prereq_01"
    repo.commit_workflow(
        workflow_id=wf_id,
        aggregate_json=json.dumps({"state": ProductVideoWorkflowState.BUILDING_RESOURCE_PACK.value}),
        events=[{"event_id": "e_init", "command_id": "c_init", "event_type": "WorkflowStarted", "timestamp": "2026-08-28T00:00:00Z", "payload": {}}],
        expected_version=0,
    )

    ctx = JobContext(workflow_id=wf_id, job_id="job_01", owner_user_id="user1")

    # 1. resource_pack_job with missing snapshot_id -> JobFailed(retryable=False)
    fail_res1 = handlers.resource_pack_job(ctx, research_result={"persistence_status": "unpersisted"})
    assert isinstance(fail_res1, JobFailed)
    assert fail_res1.retryable is False
    assert fail_res1.code == "PREREQUISITE_FAILED"

    # 2. lock_job with non-lockable status -> JobFailed(retryable=False)
    repo.commit_workflow(
        workflow_id=wf_id,
        aggregate_json=json.dumps({"state": ProductVideoWorkflowState.LOCKING_RESOURCE_PACK.value}),
        events=[{"event_id": "e_lock_init", "command_id": "c_lock_init", "event_type": "ReadyToLock", "timestamp": "2026-08-28T00:01:00Z", "payload": {}}],
        expected_version=1,
    )
    fail_res2 = handlers.lock_job(ctx, pack_dict={"resource_pack_id": "rp_123", "pack_status": "needs_review"})
    assert isinstance(fail_res2, JobFailed)
    assert fail_res2.retryable is False
    assert fail_res2.code == "NON_LOCKABLE_STATUS"

    conn.close()


def test_fake_mode_compatibility_retained(tmp_path):
    """Test that explicit fake provider mode continues to progress without configured MCP prerequisites."""
    from video_factory.integrations.product_intelligence.client import ProductIntelligenceClient
    from video_factory.integrations.client import VideoFactoryClient

    db_file = tmp_path / "fake_mode.db"
    conn = connect(db_file)
    repo = WorkflowRepository(conn)

    fake_pi = ProductIntelligenceClient()
    fake_vf = VideoFactoryClient()

    handlers = ProductVideoJobHandlers(
        pi_client=fake_pi,
        video_client=fake_vf,
        workflow_repository=repo,
        provider_mode="fake",
    )

    wf_id = "wf_fake_01"
    repo.commit_workflow(
        workflow_id=wf_id,
        aggregate_json=json.dumps({"state": ProductVideoWorkflowState.RESEARCHING.value}),
        events=[{"event_id": "e_init", "command_id": "c_init", "event_type": "WorkflowStarted", "timestamp": "2026-08-28T00:00:00Z", "payload": {}}],
        expected_version=0,
    )

    ctx = JobContext(workflow_id=wf_id, job_id="job_fake_res", owner_user_id="user_fake")

    # Research
    res = handlers.research_job(ctx, product_name="Fake Drone", keywords=["camera"])
    assert isinstance(res, JobSucceeded)

    # Transition to BUILDING_RESOURCE_PACK
    repo.commit_workflow(
        workflow_id=wf_id,
        aggregate_json=json.dumps({"state": ProductVideoWorkflowState.BUILDING_RESOURCE_PACK.value, "research_result": res.artifact_ref}),
        events=[{"event_id": "e_res", "command_id": "c_res", "event_type": "ResearchCompleted", "timestamp": "2026-08-28T00:01:00Z", "payload": {}}],
        expected_version=1,
    )

    # Build pack with fake output (which has no snapshot_id/persistence_status)
    res_pack = handlers.resource_pack_job(ctx, research_result=res.artifact_ref)
    assert isinstance(res_pack, JobSucceeded)

    # Transition to LOCKING_RESOURCE_PACK
    repo.commit_workflow(
        workflow_id=wf_id,
        aggregate_json=json.dumps({"state": ProductVideoWorkflowState.LOCKING_RESOURCE_PACK.value, "resource_pack": res_pack.artifact_ref}),
        events=[{"event_id": "e_pack", "command_id": "c_pack", "event_type": "ResourcePackBuilt", "timestamp": "2026-08-28T00:02:00Z", "payload": {}}],
        expected_version=2,
    )

    # Lock pack with fake output
    res_lock = handlers.lock_job(ctx, pack_dict=res_pack.artifact_ref)
    assert isinstance(res_lock, JobSucceeded)
    conn.close()


def test_pi_adapter_fails_closed_on_semantic_evidence_failures():
    """Test that MCPProductIntelligenceAdapter fails closed on no evidence, empty products, or identity mismatch."""
    class CustomMockRegistry:
        def __init__(self, response: dict[str, Any]) -> None:
            self.response = response

        def get_all_tool_names(self) -> set[str]:
            return {"mcp__product_intelligence__research_product"}

        def dispatch(self, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
            return self.response

    wire_tools = {"research": "mcp__product_intelligence__research_product"}

    # 1. status == "no_product_evidence"
    reg1 = CustomMockRegistry({
        "status": "no_product_evidence",
        "snapshot_id": "snap_err_1",
        "persistence_status": "saved",
        "report": {"text": "nothing found"},
    })
    adapter1 = MCPProductIntelligenceAdapter(reg1, wire_tools)
    with pytest.raises(TerminalJobError) as exc1:
        adapter1.research(product_name="Unknown Device", keywords=["unknown"])
    assert exc1.value.code == "PI_RESEARCH_FAILED"

    # 2. empty canonical_products
    reg2 = CustomMockRegistry({
        "status": "success",
        "snapshot_id": "snap_err_2",
        "persistence_status": "saved",
        "canonical_products": [],
        "report": {"text": "scraped but no canonical identity"},
    })
    adapter2 = MCPProductIntelligenceAdapter(reg2, wire_tools)
    with pytest.raises(TerminalJobError) as exc2:
        adapter2.research(product_name="Unknown Device", keywords=["unknown"])
    assert exc2.value.code == "NO_PRODUCT_EVIDENCE"

    # A summary identity is advisory only; it must not be promoted into
    # canonical evidence when the persisted PI result is empty.
    reg2b = CustomMockRegistry({
        "status": "success",
        "snapshot_id": "snap_err_2b",
        "persistence_status": "saved",
        "canonical_products": [],
        "media_assets": [{"asset_id": "a1", "local_path": "/tmp/a1.jpg"}],
        "summary": {"research_identity": {"name": "Looks like a product", "brand": "Example"}},
        "report": {"text": "identity was only a query summary"},
    })
    adapter2b = MCPProductIntelligenceAdapter(reg2b, wire_tools)
    with pytest.raises(TerminalJobError) as exc2b:
        adapter2b.research(product_name="Looks like a product", keywords=["example"])
    assert exc2b.value.code == "NO_PRODUCT_EVIDENCE"

    # 3. identity mismatch: requested Apple AirPods Max, received Sony WH-1000XM5
    reg3 = CustomMockRegistry({
        "status": "success",
        "snapshot_id": "snap_err_3",
        "persistence_status": "saved",
        "canonical_products": [{"brand": "Sony", "model": "WH-1000XM5"}],
        "report": {"text": "found Sony headphones instead"},
    })
    adapter3 = MCPProductIntelligenceAdapter(reg3, wire_tools)
    with pytest.raises(TerminalJobError) as exc3:
        adapter3.research(
            product_name="AirPods Max",
            keywords=["apple", "headphones"],
            brand="Apple",
            model="AirPods Max",
        )
    assert exc3.value.code == "IDENTITY_MISMATCH"


def test_pi_adapter_fails_closed_on_missing_media_or_non_lockable_status():
    """A single image is sufficient; missing media and non-lockable status still fail closed."""
    class CustomMockRegistry:
        def __init__(self, response: dict[str, Any]) -> None:
            self.response = response

        def get_all_tool_names(self) -> set[str]:
            return {"mcp__product_intelligence__build_resource_pack"}

        def dispatch(self, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
            return self.response

    wire_tools = {"build_resource_pack": "mcp__product_intelligence__build_resource_pack"}

    # 1. No usable media
    reg1 = CustomMockRegistry({
        "status": "built",
        "resource_pack_id": "rp_few_media",
        "pack_status": "ready_to_lock",
        "media_count": 0,
        "assets_mapping": {},
    })
    adapter1 = MCPProductIntelligenceAdapter(reg1, wire_tools)
    with pytest.raises(TerminalJobError) as exc1:
        adapter1.build_resource_pack({"snapshot_id": "snap_01", "persistence_status": "saved"})
    assert exc1.value.code == "INSUFFICIENT_MEDIA_ASSETS"

    # 2. pack_status != "ready_to_lock"
    reg2 = CustomMockRegistry({
        "status": "built",
        "resource_pack_id": "rp_not_ready",
        "pack_status": "needs_review",
        "media_count": 3,
        "assets_mapping": {"asset_01": "/1.jpg", "asset_02": "/2.jpg", "asset_03": "/3.jpg"},
    })
    adapter2 = MCPProductIntelligenceAdapter(reg2, wire_tools)
    with pytest.raises(TerminalJobError) as exc2:
        adapter2.build_resource_pack({"snapshot_id": "snap_01", "persistence_status": "saved"})
    assert exc2.value.code == "NON_LOCKABLE_STATUS"

    # Ambiguous identity produces an empty draft; report that upstream reason
    # instead of falsely diagnosing the empty media list as the root cause.
    reg3 = CustomMockRegistry({
        "status": "built",
        "resource_pack_id": "rp_ambiguous",
        "pack_status": "needs_review",
        "reason": "ambiguous: 2 resolved products",
        "media_count": 0,
    })
    adapter3 = MCPProductIntelligenceAdapter(reg3, wire_tools)
    with pytest.raises(TerminalJobError) as exc3:
        adapter3.build_resource_pack({"snapshot_id": "snap_01", "persistence_status": "saved"})
    assert exc3.value.code == "NON_LOCKABLE_STATUS"
    assert "ambiguous: 2 resolved products" in str(exc3.value)


def test_idempotency_cache_lookup_fails_on_illegal_state_or_version(tmp_path):
    """Test that in-memory cache lookup validates state and version first, never bypassing guards."""
    from video_factory.integrations.product_intelligence.client import ProductIntelligenceClient
    from video_factory.integrations.client import VideoFactoryClient

    db_file = tmp_path / "cache_guard_test.db"
    conn = connect(db_file)
    repo = WorkflowRepository(conn)

    handlers = ProductVideoJobHandlers(
        pi_client=ProductIntelligenceClient(),
        video_client=VideoFactoryClient(),
        workflow_repository=repo,
        provider_mode="fake",
    )

    wf_id = "wf_cache_guard_01"
    repo.commit_workflow(
        workflow_id=wf_id,
        aggregate_json=json.dumps({"state": ProductVideoWorkflowState.RESEARCHING.value}),
        events=[{"event_id": "e_init", "command_id": "c_init", "event_type": "WorkflowStarted", "timestamp": "2026-08-28T00:00:00Z", "payload": {}}],
        expected_version=0,
    )

    # Inject cached result for research
    fake_succeeded = JobSucceeded(artifact_ref={"report": "cached_report"})
    handlers._idempotency_cache[f"{wf_id}:research:v1:idemp_cache_1"] = fake_succeeded

    # 1. When state is RESEARCHING and expected_version matches, cache hit succeeds
    ctx_valid = JobContext(workflow_id=wf_id, job_id="job_c1", expected_version=1, idempotency_key="idemp_cache_1")
    res1 = handlers.research_job(ctx_valid, product_name="Any", keywords=["any"])
    assert isinstance(res1, JobSucceeded)
    assert res1.artifact_ref["report"] == "cached_report"

    # 2. When expected_version is stale/conflicting, guard triggers VERSION_CONFLICT without returning cached result
    ctx_stale = JobContext(workflow_id=wf_id, job_id="job_c2", expected_version=99, idempotency_key="idemp_cache_1")
    res2 = handlers.research_job(ctx_stale, product_name="Any", keywords=["any"])
    assert isinstance(res2, JobFailed)
    assert res2.code == "VERSION_CONFLICT"

    # 3. When workflow state is advanced/illegal (e.g. COMPLETED), guard triggers ILLEGAL_WORKFLOW_STATE
    repo.commit_workflow(
        workflow_id=wf_id,
        aggregate_json=json.dumps({"state": ProductVideoWorkflowState.COMPLETED.value}),
        events=[{"event_id": "e_done", "command_id": "c_done", "event_type": "WorkflowCompleted", "timestamp": "2026-08-28T00:01:00Z", "payload": {}}],
        expected_version=1,
    )
    ctx_illegal = JobContext(workflow_id=wf_id, job_id="job_c3", expected_version=2, idempotency_key="idemp_cache_1")
    res3 = handlers.research_job(ctx_illegal, product_name="Any", keywords=["any"])
    assert isinstance(res3, JobFailed)
    assert res3.code == "ILLEGAL_WORKFLOW_STATE"

    conn.close()
