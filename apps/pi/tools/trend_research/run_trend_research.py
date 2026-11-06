"""CLI orchestrator: collect hot tech keywords and research each via MCP."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.workspace_exporter.exporter import WorkspaceExporter, workspace_root
from tools.workspace_exporter.run_research import run_workflow, stdio_tool_caller

from .trends import TrendCollector


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_manifest(root: Path, keywords: list[str], entries: list[dict[str, Any]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_timestamp": _now(),
        "keyword_count": len(entries),
        "keywords": keywords,
        "products": entries,
    }
    _atomic_json(root / "trend-research.json", payload)
    lines = ["# Hot Tech Trends", "", f"Run: {_now()}", "", "| Product | Research | Operator | Dir |", "|---|---|---|---|"]
    rows = [
        f"| {entry.get('keyword')} | {entry.get('research_status')} | {entry.get('operator_status')} | {entry.get('product_dir')} |"
        for entry in entries
    ]
    _atomic_text(root / "trends.md", "\n".join(lines + rows + [""]))


def _atomic_json(path: Path, payload: Any) -> None:
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as handle:
        handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        temp = Path(handle.name)
    temp.replace(path)


def _atomic_text(path: Path, content: str) -> None:
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as handle:
        handle.write(content)
        temp = Path(handle.name)
    temp.replace(path)


def _fake_tool_caller():
    async def fake_tool(name: str, arguments: dict):
        if name == "research_product":
            return {
                "status": "partial",
                "research_id": "dev-1",
                "snapshot_id": "dev-snap-1",
                "source_summary": {"attempted": 1, "usable": 1},
                "canonical_products": [{
                    "canonical_product_id": "product-dev",
                    "name": arguments.get("product_name") or arguments.get("query", "dev"),
                    "brand": None, "model": None, "variants": [], "listings": [],
                }],
                "media_assets": [],
                "conflicts": [],
                "unresolved_evidence_ids": [],
            }
        if name in ("get_product_research", "build_resource_pack", "get_resource_pack"):
            return {"status": "not_found"}
        raise AssertionError(name)

    async def close():
        pass

    setattr(fake_tool, "close", close)
    return fake_tool


async def _main_async(namespace: argparse.Namespace) -> int:
    collector = TrendCollector()
    keywords = await collector.collect(limit=namespace.limit)
    if not keywords:
        print("no tech trend keywords collected", file=sys.stderr)
        return 1

    exporter = WorkspaceExporter(namespace.workspace_root)
    caller = _fake_tool_caller() if namespace.no_mcp else await stdio_tool_caller(
        namespace.server_command, namespace.server_arg, Path(namespace.server_cwd)
    )
    entries: list[dict[str, Any]] = []
    try:
        for keyword in keywords:
            try:
                export = await run_workflow(keyword, caller, exporter, mcp_timeout_seconds=namespace.timeout)
                entries.append({
                    "keyword": keyword,
                    "product_dir": str(export.product_dir),
                    "research_status": export.operator_status,
                    "operator_status": export.operator_status,
                })
                print(f"{keyword} -> {export.product_dir} ({export.operator_status})")
            except Exception as exc:  # noqa: BLE001 - per-keyword isolation
                entries.append({
                    "keyword": keyword,
                    "product_dir": str(namespace.workspace_root),
                    "research_status": "error",
                    "operator_status": f"error: {type(exc).__name__}: {exc}",
                })
                print(f"{keyword} -> ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        await getattr(caller, "close")()

    write_manifest(namespace.workspace_root, keywords, entries)
    print(json.dumps({"keyword_count": len(entries), "manifest": str(namespace.workspace_root / "trend-research.json")}))
    return 0


def _console_utf8() -> None:
    # Windows consoles default to cp1252 and choke on Vietnamese output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _console_utf8()
    parser = argparse.ArgumentParser(description="Research hot Vietnamese tech trend products via Product Intelligence MCP.")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--workspace-root", type=Path, default=workspace_root())
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument("--no-mcp", action="store_true", help="offline: use a canned fake tool caller")
    parser.add_argument("--server-command", default=sys.executable)
    parser.add_argument("--server-arg", action="append", default=["-m", "product_scout.mcp_server"])
    parser.add_argument("--server-cwd", type=Path, default=Path.cwd())
    namespace = parser.parse_args(argv)
    return asyncio.run(_main_async(namespace))


if __name__ == "__main__":
    raise SystemExit(main())
