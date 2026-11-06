"""Run Product Intelligence research through the official MCP stdio client."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

# Avoid C-extension deadlocks on Windows in spawned MCP server subprocesses
os.environ["PRODUCT_RESEARCH_DISABLE_IMAGEHASH"] = "true"

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from .exporter import WorkspaceExport, WorkspaceExporter, workspace_root

ToolCaller = Callable[[str, Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


async def run_workflow(
    query: str,
    tool_call: ToolCaller,
    exporter: WorkspaceExporter,
    mcp_timeout_seconds: float | None = None,
) -> WorkspaceExport:
    """Call primary MCP tools and export their unmodified response payloads."""
    if mcp_timeout_seconds is None:
        val = os.environ.get("PRODUCT_RESEARCH_MCP_TIMEOUT_SECONDS")
        mcp_timeout_seconds = float(val) if val else 300.0

    audit: list[dict[str, Any]] = []
    started_at = _now()

    async def invoke(name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = await tool_call(name, arguments)
        audit.append({
            "tool": name,
            "timestamp": _now(),
            "input": dict(arguments),
            "returned_identifier": _identifier(payload),
            "returned_status": payload.get("pack_status") or payload.get("status"),
        })
        return payload

    try:
        research = await asyncio.wait_for(
            invoke("research_product", {"product_name": query, "query": query, "discover_sources": True}),
            timeout=mcp_timeout_seconds
        )
    except asyncio.TimeoutError:
        audit.append({
            "tool": "research_product",
            "timestamp": _now(),
            "input": {"product_name": query, "query": query, "discover_sources": True},
            "returned_status": "MCP_CALL_TIMEOUT",
        })
        return exporter.export(
            None,
            query=query,
            tool_calls=audit,
            started_at=started_at,
            completed_at=_now(),
            operator_status="MCP_CALL_TIMEOUT",
        )

    projection: Mapping[str, Any] = research
    if research.get('status')=='error':
        return exporter.export(research,query=query,tool_calls=audit,started_at=started_at,
                               completed_at=_now(),operator_status='MCP_TOOL_ERROR')
    retrieval: Mapping[str, Any] | None = None
    snapshot_id = research.get("snapshot_id")
    if _requires_retrieval(research) and isinstance(snapshot_id, str) and snapshot_id:
        try:
            retrieved = await asyncio.wait_for(
                invoke("get_product_research", {"snapshot_id": snapshot_id}),
                timeout=mcp_timeout_seconds
            )
            if retrieved.get("status") == "found" and isinstance(retrieved.get("result"), Mapping):
                retrieval = retrieved
                projection = retrieved["result"]
        except asyncio.TimeoutError:
            pass

    resource: Mapping[str, Any] | None = None
    resource_retrieval: Mapping[str, Any] | None = None
    if _can_build_resource_pack(projection) and isinstance(snapshot_id, str) and snapshot_id:
        try:
            resource = await asyncio.wait_for(
                invoke("build_resource_pack", {"snapshot_id": snapshot_id}),
                timeout=mcp_timeout_seconds
            )
            pack_id = resource.get("resource_pack_id")
            if resource.get("status") == "built" and isinstance(pack_id, str) and pack_id:
                resource_retrieval = await asyncio.wait_for(
                    invoke("get_resource_pack", {"resource_pack_id": pack_id}),
                    timeout=mcp_timeout_seconds
                )
        except asyncio.TimeoutError:
            pass

    return exporter.export(
        research,
        projection_payload=projection,
        research_retrieval_payload=retrieval,
        resource_pack_payload=resource,
        resource_pack_retrieval_payload=resource_retrieval,
        query=query,
        tool_calls=audit,
        started_at=started_at,
        completed_at=_now(),
        operator_status="completed",
    )


async def stdio_tool_caller(command: str, args: list[str], cwd: Path) -> ToolCaller:
    """Create an official MCP stdio client-backed tool caller."""
    parameters = StdioServerParameters(command=command, args=args, env=dict(os.environ), cwd=cwd)
    streams = stdio_client(parameters)
    read, write = await streams.__aenter__()
    session = ClientSession(read, write)
    await session.__aenter__()
    await session.initialize()

    async def call(name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        result = await session.call_tool(name, arguments=dict(arguments))
        if getattr(result, "isError", False):
            return {"status": "error", "error": _tool_text(result)}
        return _tool_payload(result)

    async def close() -> None:
        await session.__aexit__(None, None, None)
        await streams.__aexit__(None, None, None)

    setattr(call, "close", close)
    return call


async def _main_async(namespace: argparse.Namespace) -> WorkspaceExport:
    caller = await stdio_tool_caller(namespace.server_command, namespace.server_arg, Path(namespace.server_cwd))
    try:
        return await run_workflow(namespace.query, caller, WorkspaceExporter(namespace.workspace_root))
    finally:
        await getattr(caller, "close")()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export Product Intelligence research via real MCP stdio transport.")
    parser.add_argument("query")
    parser.add_argument("--workspace-root", type=Path, default=workspace_root())
    parser.add_argument("--server-command", default=sys.executable)
    parser.add_argument("--server-arg", action="append", default=["-m", "product_scout.mcp_server"])
    parser.add_argument("--server-cwd", type=Path, default=Path.cwd())
    namespace = parser.parse_args(argv)
    result = asyncio.run(_main_async(namespace))
    print(result.product_dir)
    return 0


def _tool_payload(result: Any) -> Mapping[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, Mapping):
        return structured
    for item in getattr(result, "content", []):
        text = getattr(item, "text", None)
        if isinstance(text, str):
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, Mapping):
                return parsed
    return {'status':'error','error_code':'MCP_NON_JSON_RESPONSE',
            'error':'MCP tool returned no JSON object payload','raw_response':_tool_text(result)}


def _tool_text(result: Any) -> str:
    return "\n".join(str(getattr(item, "text", item)) for item in getattr(result, "content", []))


def _requires_retrieval(research: Mapping[str, Any]) -> bool:
    return not research.get("canonical_products") and bool(research.get("snapshot_id"))


def _can_build_resource_pack(research: Mapping[str, Any]) -> bool:
    products = research.get("canonical_products") or []
    return len(products) == 1 and isinstance(products[0], Mapping) and bool(products[0].get("canonical_product_id"))


def _identifier(payload: Mapping[str, Any]) -> str | None:
    for key in ("resource_pack_id", "snapshot_id", "research_id", "canonical_product_id"):
        if payload.get(key):
            return str(payload[key])
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
