"""M10: Hermes-facing MCP surface acceptance tests. Offline."""

from __future__ import annotations

from pathlib import Path

from product_scout import mcp_server

PRIMARY_TOOLS = {"research_product", "get_product_research", "build_resource_pack", "get_resource_pack"}
KNOWN_TOOLS = {
    "research_product",
    "get_product_research",
    "build_resource_pack",
    "get_resource_pack",
    "research_product_images",
    "validate_product_image",
    "inspect_product_source",
    "extract_product_evidence",
    "get_product_media",
}
FORBIDDEN_LOW_LEVEL = {
    "ddg_search",
    "http_download",
    "browser_open",
    "playwright_navigate",
    "parse_json_ld",
    "calculate_phash",
    "resolve_identity",
    "resolve_variant",
    "sqlite_query",
    "lock_store_insert",
}


def _tool_names() -> set[str]:
    if hasattr(mcp_server.mcp, "_tool_manager"):
        return {tool.name for tool in mcp_server.mcp._tool_manager._tools.values()}
    return set()


def test_tool_inventory_has_primary_and_no_low_level():
    names = _tool_names()
    assert names, "expected MCP tool manager to be discoverable"
    assert PRIMARY_TOOLS <= names
    assert not (names & FORBIDDEN_LOW_LEVEL)


def test_domain_does_not_import_mcp_sdk():
    root = Path(mcp_server.__file__).resolve().parent.parent / "media"
    violations = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "from mcp" in text or "import mcp" in text:
            violations.append(str(path))
    assert violations == []


def test_mcp_handler_has_no_low_level_imports():
    source = Path(mcp_server.__file__).read_text(encoding="utf-8")
    # Handlers must not import acquisition/media primitives directly at module
    # level; they delegate through _service()/_acquisition_service() helpers.
    for token in ("import httpx", "from ddgs", "import sqlite3", "from PIL", "import playwright"):
        assert token not in source, f"low-level import leaked into MCP module: {token}"


def test_research_product_serialization_mock(monkeypatch):
    from media.research.models import (
        ProductResearchResult,
        ResearchProductRequest,
        ResearchStatus,
        ResearchSummary,
    )

    result = ProductResearchResult(
        status=ResearchStatus.COMPLETED,
        request=ResearchProductRequest(url="https://x.test/p"),
        research_id="research_abc",
        snapshot_id="snap_123",
        persistence_status="saved",
        summary=ResearchSummary(usable_sources=2),
    )
    captured = {}

    def fake_research(self, request, **kwargs):
        captured["request"] = request
        return result

    monkeypatch.setattr("media.research.ProductResearchService.research", fake_research)
    out = mcp_server.research_product(url="https://x.test/p")
    assert out["status"] == "completed"
    assert out["research_id"] == "research_abc"
    assert out["snapshot_id"] == "snap_123"
    assert out["persistence_status"] == "saved"
    assert out["source_summary"] == {"attempted": 0, "usable": 2, "challenged": 0, "blocked": 0, "failed": 0}


def test_research_product_challenged_source_not_error(monkeypatch):
    from media.research.models import (
        ProductResearchResult,
        ResearchProductRequest,
        ResearchStatus,
        ResearchSummary,
        SourceResearchResult,
    )

    source = SourceResearchResult(url="https://tiktok.test/x", acquisition_status="challenged", acquisition_method="browser", evidence_status="no_product_evidence", host="tiktok.test", signals=["captcha_visible"], usable=False)
    result = ProductResearchResult(
        status=ResearchStatus.PARTIAL,
        request=ResearchProductRequest(url="https://tiktok.test/x"),
        source_results=[source],
        summary=ResearchSummary(usable_sources=0),
    )

    def fake_research(self, request, **kwargs):
        return result

    monkeypatch.setattr("media.research.ProductResearchService.research", fake_research)
    out = mcp_server.research_product(url="https://tiktok.test/x")
    assert out["status"] == "partial"
    assert out["source_summary"]["challenged"] == 1
    assert out["error"] is None


def test_research_product_ambiguous_multiple_products(monkeypatch):
    from media.research.models import (
        ProductResearchResult,
        ResearchProductRequest,
        ResearchStatus,
        ResearchSummary,
    )
    from media.resolution.models import CanonicalProduct

    p1 = CanonicalProduct(canonical_product_id="product_fa55")
    p2 = CanonicalProduct(canonical_product_id="product_fa55_pro")
    result = ProductResearchResult(
        status=ResearchStatus.PARTIAL,
        request=ResearchProductRequest(url="https://x.test/p"),
        canonical_products=[p1, p2],
        summary=ResearchSummary(usable_sources=2, canonical_products=2),
    )

    def fake_research(self, request, **kwargs):
        return result

    monkeypatch.setattr("media.research.ProductResearchService.research", fake_research)
    out = mcp_server.research_product(url="https://x.test/p")
    assert len(out["canonical_products"]) == 2
    assert {p["canonical_product_id"] for p in out["canonical_products"]} == {"product_fa55", "product_fa55_pro"}


def test_get_product_research_missing_snapshot():
    out = mcp_server.get_product_research("snap_nonexistent")
    assert out["status"] == "not_found"


def test_get_product_research_does_not_rerun(monkeypatch):
    from media.research_persistence import ResearchPersistenceService, ResearchSnapshot

    snapshot = ResearchSnapshot(snapshot_id="snap_x", research_id="research_x", created_at="2026-08-10T00:00:00Z", status="completed", result={"status": "completed"}, request={"url": "https://x.test/p"})

    def fake_get(self, snapshot_id):
        return snapshot

    monkeypatch.setattr(ResearchPersistenceService, "get", fake_get)
    out = mcp_server.get_product_research("snap_x")
    assert out["status"] == "found"
    assert out["result"]["status"] == "completed"


def test_build_resource_pack_variant_review_gate(monkeypatch):
    from media.research_persistence import ResearchPersistenceService, ResearchSnapshot

    # Snapshot with multi-variant product; media variant unknown -> NEEDS_REVIEW.
    result = {
        "status": "completed",
        "request": {"url": "https://x.test/p"},
        "canonical_products": [{
            "canonical_product_id": "product_fa55",
            "variants": [
                {"variant_id": "variant_black", "canonical_product_id": "product_fa55", "dimensions": {"variant": "black"}},
                {"variant_id": "variant_white", "canonical_product_id": "product_fa55", "dimensions": {"variant": "white"}},
            ],
            "supporting_evidence_ids": [],
        }],
        "media_assets": [{
            "asset_id": f"a{i}", "local_path": f"x{i}.jpg", "sha256": ("a" + str(i)) * 32, "phash": "p" * 16, "width": 1000, "height": 1000, "format": "jpg",
            "assessment": {"product_match_score": 0.9, "match_status": "matched"},
            "provenance": {"source_class": "marketplace", "usage_policy": "reference_only"},
        } for i in range(4)],
        "source_results": [],
        "summary": {"usable_sources": 1},
    }
    snapshot = ResearchSnapshot(snapshot_id="snap_y", research_id="research_y", created_at="2026-08-10T00:00:00Z", status="completed", result=result, request={"url": "https://x.test/p"})

    def fake_get(self, snapshot_id):
        return snapshot

    monkeypatch.setattr(ResearchPersistenceService, "get", fake_get)
    out = mcp_server.build_resource_pack("snap_y", variant_id="variant_black")
    assert out["status"] == "built"
    # High product_match_score must NOT upgrade unknown media variant to READY.
    assert out["pack_status"] == "needs_review"
    assert "variant" in out["reason"]


def test_get_resource_pack_missing():
    out = mcp_server.get_resource_pack("pack_nonexistent")
    assert out["status"] == "not_found"


def test_no_research_seed_returns_structured_error(monkeypatch):
    from media.research.models import ProductResearchResult, ResearchStatus

    def fake_research(self, request, **kwargs):
        return ProductResearchResult(status=ResearchStatus.ERROR, request=request, error="requires a url or usable hint")

    monkeypatch.setattr("media.research.ProductResearchService.research", fake_research)
    out = mcp_server.research_product()
    assert out["status"] == "error"
    assert out["error"]
