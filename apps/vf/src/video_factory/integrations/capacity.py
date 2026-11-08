"""Shared SQLite admission for Product Video paid provider operations."""

from __future__ import annotations

import sqlite3
import time
import os
import uuid
from typing import Any


_PROCESS_LEASE_TOKEN = uuid.uuid4().hex


def process_lease_owner() -> str:
    """Keep ownership stable across runtime instances in one worker process."""
    return f"{os.getpid()}:{_PROCESS_LEASE_TOKEN}"


def process_is_alive(pid: int) -> bool:
    """Probe process existence without signaling it; uncertainty retains ownership.

    Windows os.kill(pid, 0) is not a portable existence check: use a query-only
    process handle there. PID reuse is deliberately conservative and may keep a
    stale lease blocked until its owner is reconciled.
    """
    if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() not in {87, 1168}  # invalid PID / not found
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


def process_lease_owner_alive(owner: str) -> bool:
    """Recognize process lease identities without treating expiry as liveness."""
    try:
        pid = int(owner.split(":", 1)[0])
    except (AttributeError, TypeError, ValueError):
        return False
    return process_is_alive(pid)


def configured_capacity(provider_kind: str) -> int:
    """Read a bounded behavioral setting; the safe default is one operation."""
    try:
        from video_factory.config import load_config
    except ImportError:
        from hermes_cli.config import load_config

    config = load_config()
    video = config.get("video_factory", {}) if isinstance(config, dict) else {}
    capacity = video.get("provider_capacity", {}) if isinstance(video, dict) else {}
    raw = capacity.get(provider_kind, 1) if isinstance(capacity, dict) else 1
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ValueError(f"video_factory.provider_capacity.{provider_kind} must be a positive integer")
    return raw


class ProviderCapacityStore:
    DDL = """
    CREATE TABLE IF NOT EXISTS video_factory_provider_capacity (
        provider_kind TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        profile_id TEXT NOT NULL DEFAULT '',
        lease_owner TEXT NOT NULL,
        lease_expires_at REAL NOT NULL,
        provider_operation_id TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(provider_kind, operation_id)
    );
    CREATE INDEX IF NOT EXISTS idx_vf_capacity_provider
    ON video_factory_provider_capacity(provider_kind, profile_id);
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(self.DDL)

    def try_acquire(
        self,
        *,
        provider_kind: str,
        operation_id: str,
        lease_owner: str,
        limit: int,
        profile_id: str = "",
        lease_seconds: float = 300.0,
        reconciled_takeover: bool = False,
        reconciliation_only: bool = False,
    ) -> bool:
        if not provider_kind or not operation_id or not lease_owner:
            raise ValueError("provider, operation and lease owner are required")
        if isinstance(limit, bool) or limit < 1 or lease_seconds <= 0:
            raise ValueError("provider capacity and lease duration must be positive")
        now = time.time()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            # The maintenance owner and provider owner arbitrate in the same
            # SQLite write transaction. Check before renewal/takeover as well
            # as fresh admission. Global Flow exclusion matches RepairLedger's
            # provider check and cannot be bypassed by a different profile ID.
            if provider_kind == "google_flow":
                maintenance_table = self.connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='flow_ui_lease'"
                ).fetchone()
                if maintenance_table:
                    leases = self.connection.execute("SELECT owner, expires_at FROM flow_ui_lease").fetchall()
                    if any(lease["expires_at"] > now or process_lease_owner_alive(lease["owner"])
                           for lease in leases):
                        self.connection.commit()
                        return False
            existing = self.connection.execute(
                "SELECT * FROM video_factory_provider_capacity WHERE provider_kind=? AND operation_id=?",
                (provider_kind, operation_id),
            ).fetchone()
            if existing:
                legacy_profileless_reconciliation = (
                    provider_kind == "google_flow"
                    and not existing["profile_id"]
                    and bool(profile_id)
                    and bool(existing["provider_operation_id"])
                    and (reconciliation_only or reconciled_takeover)
                )
                if existing["profile_id"] != profile_id and not legacy_profileless_reconciliation:
                    raise ValueError("provider capacity profile binding conflict")
                if existing["lease_owner"] == lease_owner:
                    self.connection.execute(
                        "UPDATE video_factory_provider_capacity SET lease_expires_at=? WHERE provider_kind=? AND operation_id=?",
                        (now + lease_seconds, provider_kind, operation_id),
                    )
                    self.connection.commit()
                    return True
                owner_pid = str(existing["lease_owner"] or "").split(":", 1)[0]
                dead_owner_reconciliation = (
                    owner_pid.isdecimal()
                    and not process_lease_owner_alive(existing["lease_owner"])
                    and (
                        reconciliation_only
                        or (reconciled_takeover and bool(existing["provider_operation_id"]))
                    )
                )
                if (existing["lease_expires_at"] > now and not dead_owner_reconciliation) or not (
                    reconciliation_only
                    or (reconciled_takeover and existing["provider_operation_id"])
                ):
                    self.connection.commit()
                    return False
                self.connection.execute(
                    "UPDATE video_factory_provider_capacity SET lease_owner=?, lease_expires_at=? WHERE provider_kind=? AND operation_id=?",
                    (lease_owner, now + lease_seconds, provider_kind, operation_id),
                )
                self.connection.commit()
                return True
            count = self.connection.execute(
                "SELECT COUNT(*) FROM video_factory_provider_capacity WHERE provider_kind=?",
                (provider_kind,),
            ).fetchone()[0]
            if count >= limit:
                self.connection.commit()
                return False
            if profile_id and self.connection.execute(
                "SELECT 1 FROM video_factory_provider_capacity "
                "WHERE profile_id=? OR (?='google_flow' AND provider_kind='google_flow' AND profile_id='') LIMIT 1",
                (profile_id, provider_kind),
            ).fetchone():
                self.connection.commit()
                return False
            self.connection.execute(
                "INSERT INTO video_factory_provider_capacity VALUES (?, ?, ?, ?, ?, '')",
                (provider_kind, operation_id, profile_id, lease_owner, now + lease_seconds),
            )
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise

    def bind_provider_operation(self, provider_kind: str, operation_id: str, provider_operation_id: str) -> None:
        if not provider_operation_id:
            raise ValueError("provider operation ID is required")
        with self.connection:
            row = self.connection.execute(
                "SELECT provider_operation_id FROM video_factory_provider_capacity WHERE provider_kind=? AND operation_id=?",
                (provider_kind, operation_id),
            ).fetchone()
            if row is None or row["provider_operation_id"] not in {"", provider_operation_id}:
                raise ValueError("provider capacity operation binding conflict")
            self.connection.execute(
                "UPDATE video_factory_provider_capacity SET provider_operation_id=? WHERE provider_kind=? AND operation_id=?",
                (provider_operation_id, provider_kind, operation_id),
            )

    def release_terminal(
        self,
        provider_kind: str,
        operation_id: str,
        *,
        in_transaction: bool = False,
    ) -> None:
        if in_transaction:
            self.connection.execute(
                "DELETE FROM video_factory_provider_capacity WHERE provider_kind=? AND operation_id=?",
                (provider_kind, operation_id),
            )
            return
        with self.connection:
            self.connection.execute(
                "DELETE FROM video_factory_provider_capacity WHERE provider_kind=? AND operation_id=?",
                (provider_kind, operation_id),
            )

    def release_terminal_in_transaction(self, provider_kind: str, operation_id: str) -> None:
        """Release terminal capacity slot inside a caller-managed transaction."""
        self.release_terminal(provider_kind, operation_id, in_transaction=True)

    def active(self, provider_kind: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM video_factory_provider_capacity WHERE provider_kind=?",
            (provider_kind,),
        )]

    def reclaim_expired_operations(self, *, timeout_minutes: int = 10) -> int:
        """Reclaim capacity slots from keyframe/scene operations stuck beyond timeout."""
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)).isoformat()
        count = 0
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for table, statuses, code_prefix in [
                ("video_factory_keyframe_operation", ("submitting", "project_created", "unknown"), "KEYFRAME"),
                ("video_factory_scene_operation", ("submitted", "running"), "SCENE"),
            ]:
                exists = self.connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if not exists:
                    continue
                status_list = ",".join(f"'{s}'" for s in statuses)
                expired = self.connection.execute(
                    f"""SELECT operation_id, provider_kind, submitted_at FROM {table}
                        WHERE status IN ({status_list})
                          AND submitted_at IS NOT NULL
                          AND submitted_at < ?""",
                    (cutoff,),
                ).fetchall()
                for row in expired:
                    operation_id = row["operation_id"]
                    provider_kind = row["provider_kind"]
                    submitted_at = row["submitted_at"]
                    try:
                        submitted = datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
                        elapsed = datetime.now(timezone.utc) - submitted
                        self.connection.execute(
                            f"""UPDATE {table}
                               SET status='failed', error_code='{code_prefix}_TIMEOUT_EXPIRED',
                                   error_message=?, updated_at=CURRENT_TIMESTAMP
                               WHERE operation_id=? AND status IN ({status_list})""",
                            (f"{code_prefix} exceeded {timeout_minutes}min timeout (elapsed: {int(elapsed.total_seconds())}s)", operation_id),
                        )
                        if self.connection.execute("SELECT changes()").fetchone()[0] > 0:
                            self.connection.execute(
                                "DELETE FROM video_factory_provider_capacity WHERE provider_kind=? AND operation_id=?",
                                (provider_kind, operation_id),
                            )
                            count += 1
                    except (ValueError, TypeError):
                        pass
            self.connection.commit()
            return count
        except Exception:
            self.connection.rollback()
            raise
