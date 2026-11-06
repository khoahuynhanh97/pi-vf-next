"""Repository for atomic claim, idempotency protection, and import state persistence."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .contracts import compute_product_key


_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_product_video_import (
    product_key TEXT PRIMARY KEY,
    canonical_product_key TEXT NOT NULL,
    source_date TEXT NOT NULL,
    source_batch_id TEXT NOT NULL,
    source_product_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    workflow_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('discovered', 'imported', 'claimed', 'started', 'error', 'reserved')),
    worker_id TEXT,
    claimed_at TEXT,
    lease_expires_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_master_catalog (
    canonical_product_key TEXT PRIMARY KEY,
    product_key TEXT NOT NULL,
    brand TEXT NOT NULL,
    model TEXT NOT NULL,
    variant TEXT NOT NULL DEFAULT '',
    first_seen_date TEXT NOT NULL,
    last_produced_date TEXT,
    production_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('scouted', 'admitted', 'produced', 'cooldown', 'rejected')),
    cooldown_until TEXT,
    rejection_reason TEXT,
    source_url TEXT,
    raw_metadata_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_master_catalog_status ON product_master_catalog(status);
CREATE INDEX IF NOT EXISTS idx_master_catalog_cooldown ON product_master_catalog(status, cooldown_until);
"""


@dataclass(frozen=True)
class ClaimResult:
    status: str  # 'claimed' | 'already_started' | 'already_claimed' | 'error' | 'started'
    product_key: str
    workflow_id: str
    claimed_by: str | None = None
    error: str | None = None


class DailyImportRepository:
    """Manages atomic leasing, claims, and state tracking for daily product video imports."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.connection:
            self.connection.executescript(_LEDGER_SCHEMA)
            from video_factory.storage_infra.sqlite import _migrate_daily_product_video_import
            _migrate_daily_product_video_import(self.connection)


    def claim_product(
        self,
        *,
        canonical_product_key: str,
        source_date: str,
        source_batch_id: str,
        source_product_id: str,
        source_path: str,
        worker_id: str,
        lease_duration_seconds: float = 120.0,
    ) -> ClaimResult:
        """Atomically claim a product for workflow creation.

        Guarantees that across concurrent Scheduler, CLI, and REST requests,
        only one caller can claim the product and create a workflow.
        """
        product_key = compute_product_key(canonical_product_key)
        workflow_id = f"pv-daily-{product_key[:16]}"
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        lease_exp = (now + timedelta(seconds=lease_duration_seconds)).isoformat()

        # Execute inside immediate transaction
        try:
            with self.connection:
                existing = self.connection.execute(
                    "SELECT status, lease_expires_at, workflow_id, worker_id, product_key FROM daily_product_video_import WHERE product_key = ? OR (canonical_product_key = ? AND canonical_product_key != '')",
                    (product_key, canonical_product_key),
                ).fetchone()

                if existing is not None:
                    curr_status = existing["status"]
                    curr_wf_id = existing["workflow_id"]
                    curr_exp = existing["lease_expires_at"]
                    curr_pkey = existing["product_key"]

                    worker_val = existing["worker_id"] if "worker_id" in existing.keys() else None
                    if curr_status == "started":
                        return ClaimResult(status="already_started", product_key=curr_pkey, workflow_id=curr_wf_id, claimed_by=worker_val)

                    if curr_status == "claimed" and curr_exp and curr_exp > now_iso:
                        return ClaimResult(status="already_claimed", product_key=curr_pkey, workflow_id=curr_wf_id, claimed_by=worker_val)

                    # Expired claim or prior error: reclaim the lease
                    self.connection.execute(
                        """
                        UPDATE daily_product_video_import SET

                            canonical_product_key = ?,
                            source_date = ?,
                            source_batch_id = ?,
                            source_product_id = ?,
                            source_path = ?,
                            workflow_id = ?,
                            status = 'claimed',
                            worker_id = ?,
                            claimed_at = ?,
                            lease_expires_at = ?,
                            last_error = NULL,
                            updated_at = ?
                        WHERE product_key = ?
                        """,
                        (
                            canonical_product_key,
                            source_date,
                            source_batch_id,
                            source_product_id,
                            source_path,
                            workflow_id,
                            worker_id,
                            now_iso,
                            lease_exp,
                            now_iso,
                            product_key,
                        ),
                    )
                    return ClaimResult(status="claimed", product_key=product_key, workflow_id=workflow_id, claimed_by=worker_id)

                # Insert new claim
                self.connection.execute(
                    """
                    INSERT INTO daily_product_video_import (
                        product_key, canonical_product_key, source_date, source_batch_id,
                        source_product_id, source_path, workflow_id, status,
                        worker_id, claimed_at, lease_expires_at, last_error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        product_key,
                        canonical_product_key,
                        source_date,
                        source_batch_id,
                        source_product_id,
                        source_path,
                        workflow_id,
                        worker_id,
                        now_iso,
                        lease_exp,
                        now_iso,
                        now_iso,
                    ),
                )
                return ClaimResult(status="claimed", product_key=product_key, workflow_id=workflow_id, claimed_by=worker_id)
        except sqlite3.IntegrityError as ie:
            # Race condition: another thread/process inserted in parallel
            with self.connection:
                row = self.connection.execute(
                    "SELECT status, workflow_id, worker_id, product_key FROM daily_product_video_import WHERE product_key = ? OR (canonical_product_key = ? AND canonical_product_key != '')",
                    (product_key, canonical_product_key),
                ).fetchone()
                if row:
                    row_pkey = row["product_key"]
                    if row["status"] == "started":
                        return ClaimResult(status="already_started", product_key=row_pkey, workflow_id=row["workflow_id"], claimed_by=row["worker_id"])
                    return ClaimResult(status="already_claimed", product_key=row_pkey, workflow_id=workflow_id, claimed_by=row["worker_id"])
                return ClaimResult(status="error", product_key=product_key, workflow_id=workflow_id, error=str(ie))
        except Exception as exc:  # noqa: BLE001
            return ClaimResult(status="error", product_key=product_key, workflow_id=workflow_id, error=str(exc))

    def mark_started(self, product_key: str) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.execute(
                "UPDATE daily_product_video_import SET status = 'started', last_error = NULL, updated_at = ? WHERE product_key = ?",
                (now_iso, product_key),
            )

    def mark_started_by_workflow_id(self, workflow_id: str) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.execute(
                "UPDATE daily_product_video_import SET status = 'started', last_error = NULL, updated_at = ? WHERE workflow_id = ?",
                (now_iso, workflow_id),
            )

    def mark_error(self, product_key: str, error_message: str) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.execute(
                "UPDATE daily_product_video_import SET status = 'error', last_error = ?, updated_at = ? WHERE product_key = ?",
                (str(error_message), now_iso, product_key),
            )

    def mark_error_by_workflow_id(self, workflow_id: str, error_message: str) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.execute(
                "UPDATE daily_product_video_import SET status = 'error', last_error = ?, updated_at = ? WHERE workflow_id = ?",
                (str(error_message), now_iso, workflow_id),
            )

    def delete_by_product_key(self, product_key: str) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM daily_product_video_import WHERE product_key = ?",
                (product_key,),
            )

    def find_by_target(self, target_id: str) -> dict[str, Any] | None:
        """Find ledger entry by workflow_id, exact product_key, canonical_product_key, or product_key prefix."""
        t = target_id.strip()
        row = self.connection.execute(
            """
            SELECT * FROM daily_product_video_import
            WHERE workflow_id = ?
               OR product_key = ?
               OR canonical_product_key = ?
               OR substr(product_key, 1, ?) = ?
            ORDER BY updated_at DESC LIMIT 1
            """,
            (t, t, t, len(t), t),
        ).fetchone()
        return dict(row) if row else None

    def get_by_product_key(self, product_key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM daily_product_video_import WHERE product_key = ?",
            (product_key,),
        ).fetchone()
        return dict(row) if row else None

    def get_all_for_date(self, source_date: str) -> dict[str, dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM daily_product_video_import WHERE source_date = ?",
            (source_date,),
        ).fetchall()
        return {r["product_key"]: dict(r) for r in rows}

    def get_all_active_by_canonical_key(self) -> dict[str, dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM daily_product_video_import WHERE canonical_product_key != '' AND status IN ('started', 'claimed')"
        ).fetchall()
        return {r["canonical_product_key"]: dict(r) for r in rows}

    # Master Catalog Methods for Gatekeeper

    def upsert_scouted_product(
        self,
        canonical_product_key: str,
        product_key: str,
        brand: str,
        model: str,
        variant: str,
        source_url: str | None = None,
        raw_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record a newly discovered product from Product-Intelligence crawler."""
        import json
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        raw_json = json.dumps(raw_metadata or {}, ensure_ascii=False)

        with self.connection:
            self.connection.execute(
                """
                INSERT INTO product_master_catalog (
                    canonical_product_key, product_key, brand, model, variant,
                    first_seen_date, status, source_url, raw_metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'scouted', ?, ?, ?, ?)
                ON CONFLICT(canonical_product_key) DO UPDATE SET
                    product_key = excluded.product_key,
                    brand = excluded.brand,
                    model = excluded.model,
                    variant = excluded.variant,
                    source_url = excluded.source_url,
                    raw_metadata_json = excluded.raw_metadata_json,
                    updated_at = excluded.updated_at
                """,
                (canonical_product_key, product_key, brand, model, variant or "", now, source_url, raw_json, now, now),
            )

    def check_cooldown(self, canonical_product_key: str, cooldown_days: int = 30) -> tuple[bool, str | None]:
        """Check if product is in cooldown period.
        
        Returns:
            (is_available, rejection_reason)
        """
        from datetime import datetime, timezone

        row = self.connection.execute(
            "SELECT status, last_produced_date, cooldown_until FROM product_master_catalog WHERE canonical_product_key = ?",
            (canonical_product_key,),
        ).fetchone()

        if not row:
            return True, None  # New product, no cooldown

        status = row["status"]
        last_produced = row["last_produced_date"]
        cooldown_until = row["cooldown_until"]

        now = datetime.now(timezone.utc)

        # Explicit cooldown gate
        if cooldown_until and cooldown_until > now.isoformat():
            return False, f"IN_COOLDOWN_UNTIL_{cooldown_until}"

        # Recently produced check (fallback)
        if status == "produced" and last_produced:
            produced_dt = datetime.fromisoformat(last_produced)
            if (now - produced_dt).days < cooldown_days:
                return False, f"RECENTLY_PRODUCED_{last_produced}"

        return True, None

    def mark_admitted(self, canonical_product_key: str) -> None:
        """Mark product as admitted to exchange inbox."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.execute(
                """
                UPDATE product_master_catalog
                SET status = 'admitted', updated_at = ?
                WHERE canonical_product_key = ?
                """,
                (now, canonical_product_key),
            )

    def mark_produced(self, canonical_product_key: str, cooldown_days: int = 30) -> None:
        """Mark product as successfully produced with automatic cooldown."""
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        cooldown_until = (now + timedelta(days=cooldown_days)).isoformat()

        with self.connection:
            self.connection.execute(
                """
                UPDATE product_master_catalog
                SET status = 'cooldown',
                    last_produced_date = ?,
                    cooldown_until = ?,
                    production_count = production_count + 1,
                    updated_at = ?
                WHERE canonical_product_key = ?
                """,
                (now_iso, cooldown_until, now_iso, canonical_product_key),
            )

    def mark_rejected(self, canonical_product_key: str, reason: str) -> None:
        """Mark product as rejected with reason."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.execute(
                """
                UPDATE product_master_catalog
                SET status = 'rejected', rejection_reason = ?, updated_at = ?
                WHERE canonical_product_key = ?
                """,
                (reason, now, canonical_product_key),
            )

    def get_master_catalog_entry(self, canonical_product_key: str) -> dict[str, Any] | None:
        """Get master catalog record for deduplication checks."""
        row = self.connection.execute(
            "SELECT * FROM product_master_catalog WHERE canonical_product_key = ?",
            (canonical_product_key,),
        ).fetchone()
        return dict(row) if row else None

