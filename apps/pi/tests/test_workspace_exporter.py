from __future__ import annotations

import asyncio
import json
from pathlib import Path

from tools.workspace_exporter.exporter import WorkspaceExporter, slugify
from tools.workspace_exporter.run_research import run_workflow


def _research_payload(media_path: Path | None = None) -> dict:
    asset = {
        "asset_id": "asset-front",
        "match_status": "unknown",
        "product_match_score": 0.73,
        "usage_policy": "reference_only",
        "view": "front",
    }
    if media_path:
        asset["local_path"] = str(media_path)
    return {
        "status": "partial",
        "research_id": "research-1",
        "snapshot_id": "snapshot-1",
        "source_summary": {"attempted": 2, "usable": 1},
        "canonical_products": [{
            "canonical_product_id": "product-jisulife-fa55",
            "name": "Jisulife FA55",
            "brand": "Jisulife",
            "model": "FA55",
            "variants": [{"variant_id": "black", "dimensions": {"color": "black"}}],
            "listings": [{"listing_id": "listing-1", "host": "shop.example", "price": "39.99", "currency": "USD", "availability": "in_stock"}],
        }],
        "media_assets": [asset],
        "conflicts": [{"field": "battery", "status": "conflict"}],
        "unresolved_evidence_ids": ["evidence-unknown"],
    }


def test_slugify_is_stable_and_keeps_model_identifier():
    assert slugify("Jisulife FA55") == "jisulife-fa55"
    assert slugify("Baseus Bowie MA10 Pro") == "baseus-bowie-ma10-pro"


def test_export_preserves_raw_payload_before_human_projections(tmp_path: Path):
    payload = _research_payload()
    result = WorkspaceExporter(tmp_path).export(payload, query="Research Jisulife FA55", tool_calls=[])

    raw = json.loads((result.product_dir / "machine" / "research.json").read_text(encoding="utf-8"))
    assert raw == payload
    assert "Research: partial" in (result.product_dir / "README.md").read_text(encoding="utf-8")
    assert "unknown" in (result.product_dir / "reports" / "media.md").read_text(encoding="utf-8")
    assert "conflict" in (result.product_dir / "reports" / "evidence.md").read_text(encoding="utf-8")


def test_export_projects_media_and_preserves_prior_runs(tmp_path: Path):
    source = tmp_path / "source.jpg"
    source.write_bytes(b"media-content")
    exporter = WorkspaceExporter(tmp_path / "workspace")
    first = exporter.export(_research_payload(source), query="Jisulife FA55", tool_calls=[])
    second = exporter.export(_research_payload(source), query="Jisulife FA55", tool_calls=[])

    manifest = json.loads((first.product_dir / "machine" / "media-manifest.json").read_text(encoding="utf-8"))
    assert manifest["assets"][0]["projection"] in {"hardlinked", "copied"}
    assert (first.product_dir / manifest["assets"][0]["workspace_path"]).is_file()
    assert len(list((first.product_dir / "runs").iterdir())) == 2
    assert second.canonical_product_id == "product-jisulife-fa55"


def test_export_replaces_stale_current_media_projection(tmp_path: Path):
    source = tmp_path / "source.jpg"
    source.write_bytes(b"media-content")
    exporter = WorkspaceExporter(tmp_path / "workspace")
    first = exporter.export(_research_payload(source), query="Jisulife FA55", tool_calls=[])

    current = _research_payload()
    current["media_assets"] = []
    second = exporter.export(current, query="Jisulife FA55", tool_calls=[])

    assert not list((second.product_dir / "media" / "selected").iterdir())
    manifest = json.loads((second.product_dir / "machine" / "media-manifest.json").read_text(encoding="utf-8"))
    assert manifest["assets"] == []
    assert len(list((first.product_dir / "runs").iterdir())) == 2


def test_index_upserts_using_canonical_product_identity(tmp_path: Path):
    exporter = WorkspaceExporter(tmp_path)
    first = _research_payload()
    second = _research_payload()
    second["status"] = "completed"
    exporter.export(first, query="Jisulife FA55", tool_calls=[])
    exporter.export(second, query="Jisulife FA55 new query", tool_calls=[])

    index = (tmp_path / "_index.md").read_text(encoding="utf-8")
    assert index.count("product-key:product-jisulife-fa55") == 1
    assert "completed" in index


def test_same_name_different_canonical_identity_uses_a_different_folder(tmp_path: Path):
    exporter = WorkspaceExporter(tmp_path)
    first = exporter.export(_research_payload(), query="Jisulife FA55", tool_calls=[])
    second_payload = _research_payload()
    second_payload["canonical_products"][0]["canonical_product_id"] = "product-jisulife-fa55-pro"
    second = exporter.export(second_payload, query="Jisulife FA55", tool_calls=[])

    assert first.product_dir != second.product_dir
    assert second.product_dir.name == "jisulife-fa55-product-jisulife-fa55-pro"


def test_workflow_uses_primary_mcp_tool_names_and_raw_result(tmp_path: Path):
    calls: list[tuple[str, dict]] = []

    async def fake_tool(name: str, arguments: dict):
        calls.append((name, arguments))
        if name == "research_product":
            return _research_payload()
        if name == "build_resource_pack":
            assert arguments == {"snapshot_id": "snapshot-1"}
            return {"status": "built", "resource_pack_id": "pack-1", "pack_status": "needs_review", "reason": "Select variant"}
        if name == "get_resource_pack":
            return {"status": "found", "resource_pack_id": "pack-1", "version": 1}
        raise AssertionError(name)

    result = asyncio.run(run_workflow("Jisulife FA55", fake_tool, WorkspaceExporter(tmp_path)))
    assert [name for name, _ in calls] == ["research_product", "build_resource_pack", "get_resource_pack"]
    assert json.loads((result.product_dir / "machine" / "research.json").read_text(encoding="utf-8")) == _research_payload()
    assert json.loads((result.product_dir / "machine" / "resource-pack-retrieval.json").read_text(encoding="utf-8"))["version"] == 1
    assert "needs_review" in (result.product_dir / "README.md").read_text(encoding="utf-8")


def test_workflow_preserves_primary_payload_when_retrieving_snapshot(tmp_path: Path):
    primary = {"status": "partial", "research_id": "research-1", "snapshot_id": "snapshot-1", "canonical_products": []}
    snapshot_result = _research_payload()

    async def fake_tool(name: str, arguments: dict):
        if name == "research_product":
            return primary
        if name == "get_product_research":
            return {"status": "found", "snapshot_id": "snapshot-1", "result": snapshot_result}
        if name == "build_resource_pack":
            return {"status": "not_found", "snapshot_id": "snapshot-1"}
        raise AssertionError(name)

    result = asyncio.run(run_workflow("Jisulife FA55", fake_tool, WorkspaceExporter(tmp_path)))
    assert json.loads((result.product_dir / "machine" / "research.json").read_text(encoding="utf-8")) == primary
    assert json.loads((result.product_dir / "machine" / "research-retrieval.json").read_text(encoding="utf-8"))["result"] == snapshot_result


def test_workflow_timeout_writes_operator_audit_without_research_payload(tmp_path: Path):
    async def hanging_tool(name: str, arguments: dict):
        await asyncio.Event().wait()

    result = asyncio.run(run_workflow("Jisulife FA55", hanging_tool, WorkspaceExporter(tmp_path), mcp_timeout_seconds=0.01))

    assert result.operator_status == "MCP_CALL_TIMEOUT"
    assert result.product_dir == tmp_path / "jisulife-fa55"
    assert not (result.product_dir / "machine" / "research.json").exists()
    audit = json.loads((result.run_dir / "tool-calls.json").read_text(encoding="utf-8"))
    assert audit[0]["tool"] == "research_product"
    assert audit[0]["returned_status"] == "MCP_CALL_TIMEOUT"
    readme = (result.product_dir / "README.md").read_text(encoding="utf-8")
    assert "MCP result returned: No" in readme
    index = (tmp_path / "_index.md").read_text(encoding="utf-8")
    assert "| Jisulife FA55 | not available | MCP_CALL_TIMEOUT | not requested |" in index


def test_timeout_does_not_overwrite_prior_canonical_payload(tmp_path: Path):
    exporter = WorkspaceExporter(tmp_path)
    canonical = _research_payload()
    first = exporter.export(canonical, query="Jisulife FA55", tool_calls=[])

    async def hanging_tool(name: str, arguments: dict):
        await asyncio.Event().wait()

    result = asyncio.run(run_workflow("Jisulife FA55", hanging_tool, exporter, mcp_timeout_seconds=0.01))

    assert result.product_dir == first.product_dir
    assert json.loads((first.product_dir / "machine" / "research.json").read_text(encoding="utf-8")) == canonical
    assert "Research: partial" in (first.product_dir / "README.md").read_text(encoding="utf-8")
    assert "Operator status: MCP_CALL_TIMEOUT" in (first.product_dir / "README.md").read_text(encoding="utf-8")
    index = (tmp_path / "_index.md").read_text(encoding="utf-8")
    assert "| [Jisulife FA55](./jisulife-fa55/README.md) | partial | MCP_CALL_TIMEOUT |" in index
