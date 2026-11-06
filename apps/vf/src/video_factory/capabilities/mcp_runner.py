"""Controlled MCP discovery with durable catalog and process-bound readiness."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import threading

from video_factory.capabilities.manifest import INITIAL_CAPABILITIES
from video_factory.capabilities.service import CapabilityCatalogService
from video_factory.capabilities.snapshots import CapabilitySnapshotRepository, compute_snapshot_digest
from hermes_personal.storage.sqlite import connect

logger = logging.getLogger(__name__)


def run_discovery(home: Path, *, registry=None, pid: int | None = None):
    home.mkdir(parents=True, exist_ok=True)
    marker = home / "mcp-discovery-ready.json"
    marker.unlink(missing_ok=True)
    if registry is None:
        from tools.mcp_tool import discover_mcp_tools
        from tools.registry import registry
        discover_mcp_tools()
    connection = connect(home / "hermes.db")
    try:
        snapshot = CapabilityCatalogService(
            CapabilitySnapshotRepository(connection), registry=registry
        ).reload()
        pending = marker.with_suffix(".tmp")
        pending.write_text(json.dumps({
            "pid": os.getpid() if pid is None else pid,
            "snapshot_id": snapshot.snapshot_id,
            "digest": snapshot.digest,
        }), encoding="utf-8")
        pending.replace(marker)
        return snapshot
    finally:
        connection.close()


def check_snapshot(home: Path, *, expected_pid: int) -> bool:
    """Read durable readiness without starting servers, migrations or paid tools."""
    try:
        marker = json.loads((home / "mcp-discovery-ready.json").read_text(encoding="utf-8"))
        if marker["pid"] != expected_pid:
            return False
        connection = sqlite3.connect((home / "hermes.db").resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            snapshot = CapabilitySnapshotRepository(connection).get_active()
            if snapshot is None:
                return False
            digest, bindings = compute_snapshot_digest(snapshot.bindings)
            return (
                snapshot.snapshot_id == marker["snapshot_id"]
                and digest == snapshot.digest == marker["digest"]
                and {b.capability_id for b in bindings} == set(INITIAL_CAPABILITIES)
                and all(b.health == "healthy" for b in bindings)
            )
        finally:
            connection.close()
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return False


def main(argv: list[str] | None = None) -> int:
    from video_factory.constants import get_hermes_home
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", default="discover", choices=["discover", "check"])
    parser.add_argument("--expected-pid", type=int)
    args = parser.parse_args(argv)
    home = get_hermes_home()
    if args.action == "check":
        return 0 if args.expected_pid is not None and check_snapshot(home, expected_pid=args.expected_pid) else 1
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    try:
        run_discovery(home)
        # Discovery is controlled at startup; do not mutate a live conversation's catalog.
        while not stop.wait(1):
            if not check_snapshot(home, expected_pid=os.getpid()):
                return 1
        return 0
    except Exception:
        logger.error("MCP discovery failed; catalog readiness is unavailable")
        return 1
    finally:
        (home / "mcp-discovery-ready.json").unlink(missing_ok=True)
        from tools.mcp_tool import shutdown_mcp_servers
        shutdown_mcp_servers()


if __name__ == "__main__":
    raise SystemExit(main())
