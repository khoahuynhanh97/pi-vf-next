from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict


def _contains_artifact_id(obj: Any, artifact_id: str) -> bool:
    """Recursively check if any string value in the nested structure equals artifact_id.

    Matches only exact string values, not substrings, keys, or prefixes.
    """
    if isinstance(obj, dict):
        for value in obj.values():
            if _contains_artifact_id(value, artifact_id):
                return True
    elif isinstance(obj, list) or isinstance(obj, tuple):
        for item in obj:
            if _contains_artifact_id(item, artifact_id):
                return True
    elif isinstance(obj, str):
        return obj == artifact_id
    # int, float, bool, None don't match
    return False


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

class ApprovalRequest:
    def __init__(self, request_id: str, status: ApprovalStatus, payload: Dict[str, Any], created_at: str, updated_at: str) -> None:
        self.request_id = request_id
        self.status = status
        self.payload = payload
        self.created_at = created_at
        self.updated_at = updated_at

class ApprovalService:
    """Simple SQLite‑backed approval workflow.

    Persistence schema created on first use.
    """
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.conn = connection
        self.conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.conn:
            cur = self.conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS approval_request (
                    request_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def submit_request(self, payload: Dict[str, Any], cursor: sqlite3.Cursor | None = None) -> str:
        request_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat() + "Z"
        if cursor is not None:
            cursor.execute(
                "INSERT INTO approval_request (request_id, status, payload_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (request_id, ApprovalStatus.PENDING.value, json.dumps(payload), now, now),
            )
        else:
            with self.conn:
                cur = self.conn.cursor()
                cur.execute(
                    "INSERT INTO approval_request (request_id, status, payload_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (request_id, ApprovalStatus.PENDING.value, json.dumps(payload), now, now),
                )
        return request_id

    def _update_status(self, request_id: str, new_status: ApprovalStatus) -> None:
        with self.conn:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT status FROM approval_request WHERE request_id = ?",
                (request_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"Approval request {request_id} not found")
            current = ApprovalStatus(row["status"])
            if current != ApprovalStatus.PENDING:
                raise ValueError(f"Cannot transition from {current} to {new_status}")
            now = datetime.utcnow().isoformat() + "Z"
            cur.execute(
                "UPDATE approval_request SET status = ?, updated_at = ? WHERE request_id = ?",
                (new_status.value, now, request_id),
            )

    def approve(self, request_id: str) -> None:
        self._update_status(request_id, ApprovalStatus.APPROVED)

    def reject(self, request_id: str) -> None:
        self._update_status(request_id, ApprovalStatus.REJECTED)

    def get_request(self, request_id: str) -> ApprovalRequest:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT * FROM approval_request WHERE request_id = ?",
            (request_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(f"Approval request {request_id} not found")
        return ApprovalRequest(
            request_id=row["request_id"],
            status=ApprovalStatus(row["status"]),
            payload=json.loads(row["payload_json"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def invalidate_for_artifact(self, artifact_id: str) -> int:
        """Reject all PENDING and APPROVED approval requests whose payload
        contains the given artifact_id under any key.

        Gate 5 contract (line 190): any upstream change in the dependency
        chain ``product_lock -> brief -> scene_plan -> storyboard -> render ->
        export`` clears every descendant reference AND invalidates corresponding
        **pending/approved** approvals.  REJECTED approvals are already terminal
        and are skipped.

        Returns the number of requests invalidated.  Called automatically by
        ProductVideoBindingService when a binding chain replacement clears a
        downstream artifact ref.
        """
        now = datetime.utcnow().isoformat() + "Z"
        invalidated_statuses = (ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value)
        with self.conn:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT request_id, payload_json FROM approval_request"
                " WHERE status IN (?, ?)",
                invalidated_statuses,
            )
            rows = cur.fetchall()
            invalidated = 0
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    payload = {}
                if _contains_artifact_id(payload, artifact_id):
                    cur.execute(
                        "UPDATE approval_request SET status = ?, updated_at = ?"
                        " WHERE request_id = ?",
                        (ApprovalStatus.REJECTED.value, now, row["request_id"]),
                    )
                    invalidated += 1
        return invalidated
