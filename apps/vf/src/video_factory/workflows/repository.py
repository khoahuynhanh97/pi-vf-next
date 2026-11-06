from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import List, Optional, Dict, Any


@dataclass(frozen=True)
class CommitResult:
    workflow_id: str
    new_version: int


class VersionConflict(Exception):
    def __init__(self, message: str = "Workflow version conflict") -> None:
        super().__init__(message)


class DuplicateCommandError(Exception):
    def __init__(self, message: str = "Duplicate command_id already processed for workflow") -> None:
        super().__init__(message)


class WorkflowRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.conn = connection
        self.conn.row_factory = sqlite3.Row

    def commit_workflow(
        self,
        workflow_id: str,
        aggregate_json: str,
        events: List[Dict[str, Any]],
        outbox_message: Optional[str] = None,
        expected_version: Optional[int] = None,
        *,
        in_transaction: bool = False,
    ) -> CommitResult:
        """Commit a workflow aggregate and its events atomically.

        Args:
            workflow_id: Identifier of the workflow.
            aggregate_json: JSON representation of the aggregate.
            events: List of event dicts. Each must contain "event_id" and "command_id" keys.
            outbox_message: Optional message to store in workflow_outbox.
            expected_version: If provided, must match current version for optimistic concurrency.
            in_transaction: If True, do not manage outer transaction (caller manages with connection).
        Returns:
            CommitResult with new version.
        Raises:
            VersionConflict: Stale expected version.
            DuplicateCommandError: Command already processed.
        """
        if in_transaction:
            cur = self.conn.cursor()
            return self._commit_workflow_core(
                cur, workflow_id, aggregate_json, events, outbox_message, expected_version
            )
        with self.conn:
            cur = self.conn.cursor()
            return self._commit_workflow_core(
                cur, workflow_id, aggregate_json, events, outbox_message, expected_version
            )

    def commit_workflow_in_transaction(
        self,
        workflow_id: str,
        aggregate_json: str,
        events: List[Dict[str, Any]],
        outbox_message: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> CommitResult:
        """Commit a workflow aggregate and its events inside a caller-managed transaction."""
        return self.commit_workflow(
            workflow_id=workflow_id,
            aggregate_json=aggregate_json,
            events=events,
            outbox_message=outbox_message,
            expected_version=expected_version,
            in_transaction=True,
        )

    def _commit_workflow_core(
        self,
        cur: sqlite3.Cursor,
        workflow_id: str,
        aggregate_json: str,
        events: List[Dict[str, Any]],
        outbox_message: Optional[str],
        expected_version: Optional[int],
    ) -> CommitResult:
        # Fetch current version
        cur.execute("SELECT version FROM workflow WHERE workflow_id = ?", (workflow_id,))
        row = cur.fetchone()
        if row is None:
            # New workflow
            if expected_version is not None and expected_version != 0:
                raise VersionConflict()
            cur.execute(
                "INSERT INTO workflow (workflow_id, aggregate_json, version) VALUES (?, ?, 0)",
                (workflow_id, aggregate_json),
            )
            current_version = 0
        else:
            current_version = row["version"]
            if expected_version is not None and current_version != expected_version:
                raise VersionConflict()
        # Detect duplicate commands
        command_ids = [e.get("command_id") for e in events if e.get("command_id")]
        if command_ids:
            placeholders = ",".join(["?"] * len(command_ids))
            cur.execute(
                f"SELECT command_id FROM processed_command WHERE workflow_id = ? AND command_id IN ({placeholders})",
                (workflow_id, *command_ids),
            )
            if cur.fetchone():
                raise DuplicateCommandError()
        # Increment version
        new_version = current_version + 1
        # Keep the version check in the write itself so a concurrent
        # commit cannot overwrite an aggregate after the initial read.
        if expected_version is None:
            cur.execute(
                "UPDATE workflow SET aggregate_json = ?, version = ? WHERE workflow_id = ?",
                (aggregate_json, new_version, workflow_id),
            )
        else:
            cur.execute(
                "UPDATE workflow SET aggregate_json = ?, version = ? "
                "WHERE workflow_id = ? AND version = ?",
                (aggregate_json, new_version, workflow_id, expected_version),
            )
            if cur.rowcount != 1:
                raise VersionConflict()
        # Insert processed commands
        for e in events:
            cmd_id = e.get("command_id")
            if cmd_id:
                cur.execute(
                    "INSERT INTO processed_command (workflow_id, command_id) VALUES (?, ?)",
                    (workflow_id, cmd_id),
                )
            # Insert event record
            cur.execute(
                "INSERT INTO workflow_event (event_id, workflow_id, event_type, timestamp, payload_json) VALUES (?, ?, ?, ?, ?)",
                (
                    e["event_id"],
                    workflow_id,
                    e.get("event_type", ""),
                    e.get("timestamp", ""),
                    json.dumps(e.get("payload", {})),
                ),
            )
        # Insert outbox if provided
        if outbox_message is not None:
            cur.execute(
                "INSERT INTO workflow_outbox (workflow_id, message, created_at) VALUES (?, ?, datetime('now'))",
                (workflow_id, outbox_message),
            )
        return CommitResult(workflow_id=workflow_id, new_version=new_version)

    def get_workflow(self, workflow_id: str) -> tuple[dict, int] | None:
        """Fetch current aggregate dict and version for a workflow."""
        cur = self.conn.cursor()
        cur.execute("SELECT aggregate_json, version FROM workflow WHERE workflow_id = ?", (workflow_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return json.loads(row["aggregate_json"]), row["version"]

    def purge_workflow(self, workflow_id: str, *, in_transaction: bool = False) -> None:
        """Cleanly purge all persisted state for a stalled/failed workflow so it can be restarted or skipped."""
        from contextlib import nullcontext

        with nullcontext() if in_transaction else self.conn:
            for table in ("workflow_outbox", "workflow_event", "processed_command", "workflow"):
                try:
                    self.conn.execute(f"DELETE FROM {table} WHERE workflow_id = ?", (workflow_id,))
                except sqlite3.OperationalError:
                    pass
