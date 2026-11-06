"""Immutable, versioned Resource Pack lock storage."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import ResourcePack, ResourcePackLock

_SCHEMA = """
CREATE TABLE IF NOT EXISTS resource_pack_locks (
    resource_pack_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    research_snapshot_id TEXT NOT NULL,
    canonical_product_id TEXT NOT NULL,
    variant_id TEXT,
    media_asset_json TEXT NOT NULL,
    resolved_values_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    PRIMARY KEY (resource_pack_id, version)
);
CREATE INDEX IF NOT EXISTS idx_pack_locks_product ON resource_pack_locks(canonical_product_id);
"""


class ResourcePackLockStore:
    """Append-only lock store: each lock version is immutable."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None
        self.initialize()

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(self.path, isolation_level=None)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=DELETE")
            self._connection.execute("PRAGMA foreign_keys=ON")
        return self._connection

    def initialize(self) -> None:
        self._db().executescript(_SCHEMA)

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def lock(self, pack: ResourcePack) -> ResourcePackLock:
        if pack.pack_status.value != "ready_to_lock":
            raise ValueError("cannot lock a pack that is not ready_to_lock")
        next_version = self._next_version(pack.resource_pack_id)
        lock = ResourcePackLock(
            resource_pack_id=pack.resource_pack_id,
            version=next_version,
            created_at=pack.created_at,
            research_snapshot_id=pack.research_snapshot_id,
            canonical_product_id=pack.canonical_product_id,
            variant_id=pack.variant_id,
            media_asset_ids=[item.asset_id for item in pack.media],
            resolved_values=pack.product_identity,
            provenance=pack.provenance,
        )
        db = self._db()
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                "INSERT INTO resource_pack_locks(resource_pack_id,version,created_at,research_snapshot_id,canonical_product_id,variant_id,media_asset_json,resolved_values_json,provenance_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    lock.resource_pack_id,
                    lock.version,
                    lock.created_at,
                    lock.research_snapshot_id,
                    lock.canonical_product_id,
                    lock.variant_id,
                    json.dumps(lock.media_asset_ids, separators=(",", ":")),
                    json.dumps(lock.resolved_values, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                    json.dumps(lock.provenance, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                ),
            )
            db.execute("COMMIT")
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        return lock

    def get_latest_lock(self, resource_pack_id: str) -> ResourcePackLock | None:
        row = self._db().execute(
            "SELECT * FROM resource_pack_locks WHERE resource_pack_id=? ORDER BY version DESC LIMIT 1",
            (resource_pack_id,),
        ).fetchone()
        return self._to_lock(row) if row else None

    def list_locks(self, resource_pack_id: str, limit: int = 20) -> list[ResourcePackLock]:
        rows = self._db().execute(
            "SELECT * FROM resource_pack_locks WHERE resource_pack_id=? ORDER BY version DESC LIMIT ?",
            (resource_pack_id, min(limit, 100)),
        ).fetchall()
        return [self._to_lock(row) for row in rows]

    def _next_version(self, resource_pack_id: str) -> int:
        row = self._db().execute("SELECT MAX(version) AS v FROM resource_pack_locks WHERE resource_pack_id=?", (resource_pack_id,)).fetchone()
        return int(row["v"] or 0) + 1

    def _to_lock(self, row) -> ResourcePackLock:
        return ResourcePackLock(
            resource_pack_id=row["resource_pack_id"],
            version=row["version"],
            created_at=row["created_at"],
            research_snapshot_id=row["research_snapshot_id"],
            canonical_product_id=row["canonical_product_id"],
            variant_id=row["variant_id"],
            media_asset_ids=json.loads(row["media_asset_json"]),
            resolved_values=json.loads(row["resolved_values_json"]),
            provenance=json.loads(row["provenance_json"]),
        )
