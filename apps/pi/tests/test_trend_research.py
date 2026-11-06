from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace


from tools.workspace_exporter.exporter import WorkspaceExporter
from tools.workspace_exporter.run_research import run_workflow
from tools.trend_research.trends import TrendCollector, filter_tech_titles

FIXTURE_RSS = Path(__file__).parent / "fixtures" / "google_trends_vn.xml"


def _rss_bytes() -> bytes:
    return FIXTURE_RSS.read_bytes()


async def _rss_fetcher() -> bytes:
    return _rss_bytes()


def test_filter_tech_titles_keeps_tech_and_dedupes():
    titles = ["robot hút bụi thông minh", "camera wifi trong nhà", "camera wifi trong nhà", "thời tiết hôm nay", "đường phố Hà Nội"]
    kept = filter_tech_titles(titles)
    assert kept == ["robot hút bụi thông minh", "camera wifi trong nhà"]
    assert len(kept) == len(set(kept))


def test_collect_parses_rss_fixture():
    collector = TrendCollector(fetcher=_rss_fetcher)
    keywords = asyncio.run(collector.collect(limit=5))
    assert "robot hút bụi thông minh" in keywords
    assert "camera wifi trong nhà" in keywords


def test_collect_items_parses_rss_fixture():
    collector = TrendCollector(fetcher=_rss_fetcher)
    items = asyncio.run(collector.collect_items(limit=5))
    assert len(items) >= 2
    assert items[0].title == "robot hút bụi thông minh"
    assert items[0].url == "https://news.test/robot"


def test_collect_respects_limit():
    collector = TrendCollector(fetcher=_rss_fetcher)
    keywords = asyncio.run(collector.collect(limit=1))
    assert len(keywords) == 1


def test_collect_falls_back_when_rss_fails():
    class FakeProvider:
        def __init__(self):
            self.searches = 0

        def search(self, query):
            self.searches += 1
            return [
                SimpleNamespace(title="tai nghe bluetooth giá rẻ", url="https://x.test/1", rank=1),
                SimpleNamespace(title="loa bluetooth mini", url="https://x.test/2", rank=2),
            ]

    provider = FakeProvider()

    async def broken_fetcher():
        raise OSError("rss unavailable")

    collector = TrendCollector(fetcher=broken_fetcher, provider=provider, fallback_queries=["tai nghe", "loa"])
    keywords = asyncio.run(collector.collect(limit=2))
    assert provider.searches >= 1
    assert len(keywords) >= 1


def _research_payload() -> dict:
    return {
        "status": "partial",
        "research_id": "research-1",
        "snapshot_id": "snapshot-1",
        "source_summary": {"attempted": 2, "usable": 1},
        "canonical_products": [{
            "canonical_product_id": "product-x",
            "name": "Sample Product",
            "brand": "Sample",
            "model": "X1",
            "variants": [],
            "listings": [{"listing_id": "l1", "host": "shop.example", "price": "1.0", "currency": "USD", "availability": "in_stock"}],
        }],
        "media_assets": [],
        "conflicts": [],
        "unresolved_evidence_ids": [],
    }


async def _run_offline(keywords, tmp_path):
    from tools.trend_research.run_trend_research import write_manifest

    calls = []

    async def fake_tool(name: str, arguments: dict):
        calls.append((name, arguments))
        if name == "research_product":
            return _research_payload()
        if name in ("build_resource_pack", "get_product_research", "get_resource_pack"):
            return {"status": "not_found"}
        raise AssertionError(name)

    exporter = WorkspaceExporter(tmp_path)
    entries = []
    for keyword in keywords:
        export = await run_workflow(keyword, fake_tool, exporter)
        entries.append({
            "keyword": keyword,
            "product_dir": str(export.product_dir),
            "research_status": export.operator_status,
            "operator_status": export.operator_status,
        })
    write_manifest(tmp_path, keywords, entries)
    return calls, entries


def test_orchestrator_runs_workflow_per_keyword_and_writes_manifest(tmp_path: Path):
    keywords = ["robot hút bụi thông minh", "camera wifi trong nhà"]
    calls, _ = asyncio.run(_run_offline(keywords, tmp_path))

    assert [name for name, _ in calls] == ["research_product", "build_resource_pack"] * 2
    manifest = json.loads((tmp_path / "trend-research.json").read_text(encoding="utf-8"))
    assert [entry["keyword"] for entry in manifest["products"]] == keywords
    assert len(manifest["products"]) == 2
    assert (tmp_path / "trends.md").exists()


def test_manifest_reports_error_entry_without_aborting(tmp_path: Path):
    keywords = ["first", "second"]

    async def fake_tool(name: str, arguments: dict):
        raise RuntimeError("mcp down")

    async def _run():
        from tools.trend_research.run_trend_research import write_manifest

        exporter = WorkspaceExporter(tmp_path)
        entries = []
        for keyword in keywords:
            try:
                export = await run_workflow(keyword, fake_tool, exporter)
                entries.append({"keyword": keyword, "product_dir": str(export.product_dir), "research_status": export.operator_status, "operator_status": export.operator_status})
            except Exception as exc:  # noqa: BLE001 - per-keyword isolation
                entries.append({"keyword": keyword, "product_dir": str(tmp_path), "research_status": "error", "operator_status": f"error: {exc}"})
        write_manifest(tmp_path, keywords, entries)
        return entries

    entries = asyncio.run(_run())
    assert len(entries) == 2
    assert entries[0]["operator_status"].startswith("error")
    manifest = json.loads((tmp_path / "trend-research.json").read_text(encoding="utf-8"))
    assert len(manifest["products"]) == 2
