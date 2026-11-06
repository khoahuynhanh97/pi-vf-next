"""ResearchRepository abstraction and SQLite implementation.

Append-only snapshot storage. No resolution logic lives here; persistence only
stores what the research pipeline resolved at a point in time.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Protocol

from .models import SCHEMA_VERSION, ResearchSnapshot

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    research_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    request_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    research_metadata_json TEXT NOT NULL DEFAULT '{}',
    schema_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS resource_pack_locks (
    lock_id TEXT PRIMARY KEY,
    resource_pack_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    canonical_product_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    manifest_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    lock_json TEXT NOT NULL,
    UNIQUE(resource_pack_id, owner_user_id, manifest_digest)
);
CREATE INDEX IF NOT EXISTS idx_research_snapshots_research_id ON research_snapshots(research_id);
CREATE INDEX IF NOT EXISTS idx_research_snapshots_created_at ON research_snapshots(created_at);
"""

MAX_LIST_LIMIT = 100


class ResearchRepository(Protocol):
    def save_snapshot(self, snapshot: ResearchSnapshot) -> str: ...

    def save_intake_batch(self, snapshots: list[ResearchSnapshot]) -> list[str]: ...

    def get_snapshot(self, snapshot_id: str) -> ResearchSnapshot: ...

    def get_latest_snapshot(self, research_id: str) -> ResearchSnapshot | None: ...

    def list_snapshots(self, research_id: str, limit: int = 20) -> list[ResearchSnapshot]: ...


class UnsupportedSnapshotVersionError(RuntimeError):
    def __init__(self, version: int, supported: int):
        self.version = version
        self.supported = supported
        super().__init__(f"UNSUPPORTED_SNAPSHOT_VERSION: snapshot version {version}, supported {supported}")


class SnapshotDeserializationError(RuntimeError):
    def __init__(self, snapshot_id: str, detail: str = ""):
        self.snapshot_id = snapshot_id
        super().__init__(f"SNAPSHOT_DESERIALIZATION_FAILED: {snapshot_id} {detail}")


class SQLiteResearchRepository:
    def __init__(self, path: str | Path, *, max_list_limit: int = MAX_LIST_LIMIT):
        self.path = Path(path)
        self.max_list_limit = max_list_limit
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

    def save_snapshot(self, snapshot: ResearchSnapshot) -> str:
        if snapshot.schema_version != SCHEMA_VERSION:
            raise UnsupportedSnapshotVersionError(snapshot.schema_version, SCHEMA_VERSION)
        db = self._db()
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                "INSERT INTO research_snapshots(snapshot_id,research_id,created_at,status,request_json,result_json,research_metadata_json,schema_version) VALUES(?,?,?,?,?,?,?,?)",
                (
                    snapshot.snapshot_id,
                    snapshot.research_id,
                    snapshot.created_at,
                    snapshot.status,
                    _dump(snapshot.request),
                    _dump(snapshot.result),
                    _dump(snapshot.research_metadata),
                    snapshot.schema_version,
                ),
            )
            db.execute("COMMIT")
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        return snapshot.snapshot_id

    def save_intake_batch(self, snapshots: list[ResearchSnapshot]) -> list[str]:
        """Atomically insert or reuse identical immutable intake snapshots."""
        if not snapshots:
            return []
        db = self._db()
        db.execute("BEGIN IMMEDIATE")
        try:
            for snapshot in snapshots:
                if snapshot.schema_version != SCHEMA_VERSION:
                    raise UnsupportedSnapshotVersionError(snapshot.schema_version, SCHEMA_VERSION)
                existing = db.execute("SELECT * FROM research_snapshots WHERE snapshot_id=?", (snapshot.snapshot_id,)).fetchone()
                if existing:
                    if self._to_snapshot(existing).model_dump(mode="json") != snapshot.model_dump(mode="json"):
                        raise ValueError(f"Intake snapshot identity conflict: {snapshot.snapshot_id}")
                    continue
                db.execute(
                    "INSERT INTO research_snapshots(snapshot_id,research_id,created_at,status,request_json,result_json,research_metadata_json,schema_version) VALUES(?,?,?,?,?,?,?,?)",
                    (snapshot.snapshot_id, snapshot.research_id, snapshot.created_at, snapshot.status,
                     _dump(snapshot.request), _dump(snapshot.result), _dump(snapshot.research_metadata), snapshot.schema_version),
                )
            db.execute("COMMIT")
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        return [snapshot.snapshot_id for snapshot in snapshots]

    def get_snapshot(self, snapshot_id: str) -> ResearchSnapshot:
        row = self._db().execute("SELECT * FROM research_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        if not row:
            raise KeyError(snapshot_id)
        return self._to_snapshot(row)

    def get_latest_snapshot(self, research_id: str) -> ResearchSnapshot | None:
        # created_at DESC with rowid tiebreaker: deterministic under timestamp
        # collisions (Windows SQLite resolution).
        row = self._db().execute(
            "SELECT * FROM research_snapshots WHERE research_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (research_id,),
        ).fetchone()
        return self._to_snapshot(row) if row else None

    def list_snapshots(self, research_id: str, limit: int = 20) -> list[ResearchSnapshot]:
        bounded = min(max(1, int(limit)), self.max_list_limit)
        rows = self._db().execute(
            "SELECT * FROM research_snapshots WHERE research_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (research_id, bounded),
        ).fetchall()
        return [self._to_snapshot(row) for row in rows]

    def save_lock(self, lock_payload: dict) -> dict:
        lock_id = lock_payload["lock_id"]
        resource_pack_id = lock_payload["resource_pack_id"]
        owner_user_id = lock_payload["owner_user_id"]
        manifest_digest = lock_payload["manifest_digest"]

        db = self._db()
        # Lookup existing by lock_id or identity tuple
        existing = db.execute(
            "SELECT lock_json FROM resource_pack_locks WHERE lock_id=? OR (resource_pack_id=? AND owner_user_id=? AND manifest_digest=?)",
            (lock_id, resource_pack_id, owner_user_id, manifest_digest),
        ).fetchone()
        if existing:
            return _load(existing["lock_json"])

        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                """
                INSERT INTO resource_pack_locks (
                    lock_id, resource_pack_id, snapshot_id, canonical_product_id,
                    owner_user_id, version, manifest_digest, created_at, lock_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lock_id,
                    resource_pack_id,
                    lock_payload["snapshot_id"],
                    lock_payload["canonical_product_id"],
                    owner_user_id,
                    lock_payload["version"],
                    manifest_digest,
                    lock_payload["created_at"],
                    _dump(lock_payload),
                ),
            )
            db.execute("COMMIT")
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        return lock_payload

    def get_lock(self, lock_id: str) -> dict | None:
        row = self._db().execute("SELECT lock_json FROM resource_pack_locks WHERE lock_id=?", (lock_id,)).fetchone()
        if not row:
            return None
        return _load(row["lock_json"])

    def get_lock_by_identity(self, resource_pack_id: str, owner_user_id: str, manifest_digest: str) -> dict | None:
        row = self._db().execute(
            "SELECT lock_json FROM resource_pack_locks WHERE resource_pack_id=? AND owner_user_id=? AND manifest_digest=?",
            (resource_pack_id, owner_user_id, manifest_digest),
        ).fetchone()
        if not row:
            return None
        return _load(row["lock_json"])

    def _to_snapshot(self, row) -> ResearchSnapshot:
        version = row["schema_version"]
        if version != SCHEMA_VERSION:
            raise UnsupportedSnapshotVersionError(version, SCHEMA_VERSION)
        try:
            return ResearchSnapshot(
                snapshot_id=row["snapshot_id"],
                research_id=row["research_id"],
                created_at=row["created_at"],
                status=row["status"],
                request=_load(row["request_json"]),
                result=_load(row["result_json"]),
                research_metadata=_load(row["research_metadata_json"]),
                schema_version=version,
            )
        except (ValueError, TypeError) as exc:
            raise SnapshotDeserializationError(row["snapshot_id"], str(exc)) from exc


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load(payload: str) -> dict:
    if not payload:
        return {}
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise TypeError("snapshot payload is not an object")
    return value
