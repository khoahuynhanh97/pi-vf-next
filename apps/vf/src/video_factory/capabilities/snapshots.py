from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

from video_factory.capabilities.bindings import ImplementationBinding
from video_factory.capabilities.manifest import INITIAL_CAPABILITIES


@dataclass(frozen=True)
class CatalogSnapshot:
    snapshot_id: str
    digest: str
    created_at: str
    bindings: tuple[ImplementationBinding, ...]

    def __post_init__(self) -> None:
        for field_name in ("snapshot_id", "digest", "created_at"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be blank")


def _binding_to_dict(binding: ImplementationBinding) -> dict[str, str]:
    return {
        "capability_id": binding.capability_id,
        "wire_name": binding.wire_name,
        "source": binding.source,
        "provider": binding.provider,
        "implementation_version": binding.implementation_version,
        "input_schema_version": binding.input_schema_version,
        "output_schema_version": binding.output_schema_version,
        "schema_digest": binding.schema_digest,
        "health": binding.health,
    }


def _dict_to_binding(data: dict[str, str]) -> ImplementationBinding:
    return ImplementationBinding(
        capability_id=data["capability_id"],
        wire_name=data["wire_name"],
        source=data["source"],  # type: ignore[arg-type]
        provider=data["provider"],
        implementation_version=data["implementation_version"],
        input_schema_version=data["input_schema_version"],
        output_schema_version=data["output_schema_version"],
        schema_digest=data["schema_digest"],
        health=data["health"],  # type: ignore[arg-type]
    )


def compute_snapshot_digest(
    bindings: Iterable[ImplementationBinding],
) -> tuple[str, tuple[ImplementationBinding, ...]]:
    binding_list = list(bindings)
    seen_ids: set[str] = set()
    sorted_bindings = sorted(binding_list, key=lambda b: b.capability_id)

    serialized_items: list[dict[str, str]] = []
    for b in sorted_bindings:
        if b.capability_id in seen_ids:
            raise ValueError(f"duplicate capability_id in bindings: {b.capability_id}")
        if b.capability_id not in INITIAL_CAPABILITIES:
            raise ValueError(
                f"unknown capability_id outside INITIAL_CAPABILITIES: {b.capability_id}"
            )
        seen_ids.add(b.capability_id)
        serialized_items.append(_binding_to_dict(b))

    canonical_json = json.dumps(
        serialized_items, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return f"sha256:{digest}", tuple(sorted_bindings)


def _validate_completeness(bindings: Sequence[ImplementationBinding]) -> None:
    found_ids = {b.capability_id for b in bindings}
    missing_ids = set(INITIAL_CAPABILITIES.keys()) - found_ids
    if missing_ids:
        raise ValueError(
            f"snapshot incomplete, missing capabilities: {sorted(missing_ids)}"
        )


class CapabilitySnapshotRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def save(self, bindings: Iterable[ImplementationBinding]) -> CatalogSnapshot:
        with self._connection:
            return self._save_internal(bindings)

    def _save_internal(
        self, bindings: Iterable[ImplementationBinding]
    ) -> CatalogSnapshot:
        digest, sorted_bindings = compute_snapshot_digest(bindings)

        row = self._connection.execute(
            "SELECT snapshot_id, created_at FROM capability_snapshot WHERE digest = ?",
            (digest,),
        ).fetchone()

        if row is not None:
            return CatalogSnapshot(
                snapshot_id=row["snapshot_id"],
                digest=digest,
                created_at=row["created_at"],
                bindings=sorted_bindings,
            )

        snapshot_id = f"snap_{digest[7:23]}"
        created_at = datetime.now(timezone.utc).isoformat()

        self._connection.execute(
            "INSERT INTO capability_snapshot (snapshot_id, digest, created_at) VALUES (?, ?, ?)",
            (snapshot_id, digest, created_at),
        )

        for binding in sorted_bindings:
            binding_json = json.dumps(
                _binding_to_dict(binding), sort_keys=True, separators=(",", ":")
            )
            self._connection.execute(
                """
                INSERT INTO capability_snapshot_binding (snapshot_id, capability_id, binding_json)
                VALUES (?, ?, ?)
                """,
                (snapshot_id, binding.capability_id, binding_json),
            )

        return CatalogSnapshot(
            snapshot_id=snapshot_id,
            digest=digest,
            created_at=created_at,
            bindings=sorted_bindings,
        )

    def get(self, snapshot_id: str) -> CatalogSnapshot | None:
        row = self._connection.execute(
            "SELECT snapshot_id, digest, created_at FROM capability_snapshot WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()

        if row is None:
            return None

        binding_rows = self._connection.execute(
            """
            SELECT binding_json FROM capability_snapshot_binding
            WHERE snapshot_id = ?
            ORDER BY capability_id ASC
            """,
            (snapshot_id,),
        ).fetchall()

        bindings = tuple(
            _dict_to_binding(json.loads(r["binding_json"])) for r in binding_rows
        )

        return CatalogSnapshot(
            snapshot_id=row["snapshot_id"],
            digest=row["digest"],
            created_at=row["created_at"],
            bindings=bindings,
        )

    def get_active(self) -> CatalogSnapshot | None:
        row = self._connection.execute(
            "SELECT active_snapshot_id FROM capability_catalog_state WHERE state_id = 1"
        ).fetchone()

        if row is None:
            return None

        return self.get(row["active_snapshot_id"])

    def activate(self, snapshot_id: str) -> CatalogSnapshot:
        with self._connection:
            return self._activate_internal(snapshot_id)

    def _activate_internal(self, snapshot_id: str) -> CatalogSnapshot:
        snapshot = self.get(snapshot_id)
        if snapshot is None:
            raise ValueError(f"unknown snapshot_id: {snapshot_id}")

        _validate_completeness(snapshot.bindings)

        activated_at = datetime.now(timezone.utc).isoformat()
        self._connection.execute(
            """
            INSERT INTO capability_catalog_state (state_id, active_snapshot_id, activated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(state_id) DO UPDATE SET
              active_snapshot_id = excluded.active_snapshot_id,
              activated_at = excluded.activated_at
            """,
            (snapshot_id, activated_at),
        )

        return snapshot

    def save_and_activate(
        self, bindings: Iterable[ImplementationBinding]
    ) -> CatalogSnapshot:
        with self._connection:
            snapshot = self._save_internal(bindings)
            _validate_completeness(snapshot.bindings)
            activated_at = datetime.now(timezone.utc).isoformat()
            self._connection.execute(
                """
                INSERT INTO capability_catalog_state (state_id, active_snapshot_id, activated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(state_id) DO UPDATE SET
                  active_snapshot_id = excluded.active_snapshot_id,
                  activated_at = excluded.activated_at
                """,
                (snapshot.snapshot_id, activated_at),
            )
            return snapshot
