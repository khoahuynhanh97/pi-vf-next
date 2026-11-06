"""Durable retry policy, tracking, and candidate substitution for QC quality gates."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from video_factory.qc.models import QCReport

logger = logging.getLogger(__name__)

DEFAULT_MAX_QC_RETRIES = 3
MIN_QC_RETRIES = 0
MAX_QC_RETRIES = 3


class QCQuotaExhaustedError(RuntimeError):
    """Raised when QC recovery attempts exceed durable quota allowance."""

    def __init__(
        self,
        message: str,
        *,
        workflow_id: str = "",
        retry_type: str = "",
        item_id: str = "",
        attempts_used: int = 0,
        max_allowed: int = 0,
    ) -> None:
        super().__init__(message)
        self.workflow_id = workflow_id
        self.retry_type = retry_type
        self.item_id = item_id
        self.attempts_used = attempts_used
        self.max_allowed = max_allowed


def clamp_max_retries(value: int | None) -> int:
    """Return the configured retry count, capped at three after the initial attempt."""
    if value is None:
        return DEFAULT_MAX_QC_RETRIES
    try:
        val = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid max_qc_retries: {value!r}. Must be an integer between 0 and 3.")
    if val < MIN_QC_RETRIES:
        raise ValueError(
            f"Invalid max_qc_retries: {val}. Allowed recovery count is between 0 and 3."
        )
    return min(val, MAX_QC_RETRIES)


def ensure_qc_retry_schema(conn: sqlite3.Connection) -> None:
    """Ensure the durable retry tracking table exists in SQLite."""
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_qc_retries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id TEXT NOT NULL,
                retry_type TEXT NOT NULL,
                item_id TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                verdict TEXT NOT NULL,
                input_hash TEXT,
                qc_report_id TEXT,
                cost_usd REAL DEFAULT NULL,
                input_url TEXT,
                action_taken TEXT,
                reason_text TEXT,
                retry_category TEXT DEFAULT 'quality',
                operation_id TEXT DEFAULT '',
                status TEXT DEFAULT 'claimed',
                output_hash TEXT DEFAULT '',
                budget_reference TEXT DEFAULT '',
                updated_at TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(workflow_qc_retries)").fetchall()]
            if "input_url" not in cols:
                conn.execute("ALTER TABLE workflow_qc_retries ADD COLUMN input_url TEXT")
            if "action_taken" not in cols:
                conn.execute("ALTER TABLE workflow_qc_retries ADD COLUMN action_taken TEXT")
            if "reason_text" not in cols:
                conn.execute("ALTER TABLE workflow_qc_retries ADD COLUMN reason_text TEXT")
            if "retry_category" not in cols:
                conn.execute("ALTER TABLE workflow_qc_retries ADD COLUMN retry_category TEXT DEFAULT 'quality'")
            if "operation_id" not in cols:
                conn.execute("ALTER TABLE workflow_qc_retries ADD COLUMN operation_id TEXT DEFAULT ''")
            if "status" not in cols:
                conn.execute("ALTER TABLE workflow_qc_retries ADD COLUMN status TEXT DEFAULT 'claimed'")
            if "output_hash" not in cols:
                conn.execute("ALTER TABLE workflow_qc_retries ADD COLUMN output_hash TEXT DEFAULT ''")
            if "budget_reference" not in cols:
                conn.execute("ALTER TABLE workflow_qc_retries ADD COLUMN budget_reference TEXT DEFAULT ''")
            if "updated_at" not in cols:
                conn.execute("ALTER TABLE workflow_qc_retries ADD COLUMN updated_at TEXT DEFAULT ''")
        except Exception:
            pass
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_qc_retries_wf
            ON workflow_qc_retries (workflow_id, retry_type)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_qc_retries_item
            ON workflow_qc_retries (workflow_id, retry_type, item_id, attempt_number)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_qc_retries_op
            ON workflow_qc_retries (workflow_id, retry_type, item_id, operation_id)
            """
        )


def claim_qc_attempt(
    conn: sqlite3.Connection,
    *,
    workflow_id: str,
    retry_type: str,
    item_id: str,
    input_hash: str = "",
    operation_id: str = "",
    max_recoveries: int | None = None,
) -> dict[str, Any]:
    """Claim a unique QC attempt before action.

    Item ID is stable product key for source images, or logical frame ID for keyframes.
    Quota is bounded to 1 initial + N recoveries (total attempts = 1 + N), where N is capped at 3 (default 3).
    Idempotent: Replaying with the same operation_id returns the existing attempt without advancing quota.
    Raises QCQuotaExhaustedError when quota is exceeded.
    """
    ensure_qc_retry_schema(conn)
    n_recoveries = clamp_max_retries(max_recoveries)
    max_total_attempts = 1 + n_recoveries

    now = datetime.now(timezone.utc).isoformat()
    clean_item = str(item_id).strip()

    with conn:
        # Check for idempotent reuse with operation_id
        if operation_id:
            row = conn.execute(
                """
                SELECT id, workflow_id, retry_type, item_id, attempt_number,
                       status, verdict, input_hash, qc_report_id, operation_id, created_at
                FROM workflow_qc_retries
                WHERE workflow_id = ? AND retry_type = ? AND item_id = ? AND operation_id = ?
                ORDER BY attempt_number DESC LIMIT 1
                """,
                (workflow_id, retry_type, clean_item, operation_id),
            ).fetchone()
            if row:
                prior_hash = row[7] if isinstance(row, tuple) else row["input_hash"]
                if input_hash and str(prior_hash or "").strip() != str(input_hash).strip():
                    raise ValueError(
                        f"Operation ID '{operation_id}' replay rejected: input_hash changed from '{prior_hash}' to '{input_hash}' (SPEC S8)"
                    )
                return {
                    "attempt_id": row[0] if isinstance(row, tuple) else row["id"],
                    "workflow_id": row[1] if isinstance(row, tuple) else row["workflow_id"],
                    "retry_type": row[2] if isinstance(row, tuple) else row["retry_type"],
                    "item_id": row[3] if isinstance(row, tuple) else row["item_id"],
                    "attempt_number": row[4] if isinstance(row, tuple) else row["attempt_number"],
                    "status": row[5] if isinstance(row, tuple) else row["status"],
                    "verdict": row[6] if isinstance(row, tuple) else row["verdict"],
                    "input_hash": prior_hash,
                    "qc_report_id": row[8] if isinstance(row, tuple) else row["qc_report_id"],
                    "operation_id": operation_id,
                    "reused": True,
                    "allowed": True,
                }

        # Check durable attempt count
        cur_row = conn.execute(
            """
            SELECT MAX(attempt_number) FROM workflow_qc_retries
            WHERE workflow_id = ? AND retry_type = ? AND item_id = ?
            """,
            (workflow_id, retry_type, clean_item),
        ).fetchone()
        current_count = int(cur_row[0]) if (cur_row and cur_row[0] is not None) else 0

        if current_count >= max_total_attempts:
            raise QCQuotaExhaustedError(
                f"QC quota exhausted for {retry_type} item '{clean_item}': "
                f"{current_count} attempts used (max allowed: {max_total_attempts})",
                workflow_id=workflow_id,
                retry_type=retry_type,
                item_id=clean_item,
                attempts_used=current_count,
                max_allowed=max_total_attempts,
            )

        next_attempt = current_count + 1
        cursor = conn.execute(
            """
            INSERT INTO workflow_qc_retries (
                workflow_id, retry_type, item_id, attempt_number,
                operation_id, status, verdict, input_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'claimed', 'pending', ?, ?, ?)
            """,
            (
                workflow_id,
                retry_type,
                clean_item,
                next_attempt,
                operation_id,
                input_hash,
                now,
                now,
            ),
        )
        attempt_id = cursor.lastrowid
        return {
            "attempt_id": attempt_id,
            "workflow_id": workflow_id,
            "retry_type": retry_type,
            "item_id": clean_item,
            "attempt_number": next_attempt,
            "operation_id": operation_id,
            "status": "claimed",
            "verdict": "pending",
            "reused": False,
            "allowed": True,
        }


def complete_qc_attempt(
    conn: sqlite3.Connection,
    *,
    attempt_id: int,
    status: str = "completed",
    verdict: str = "pass",
    qc_report_id: str = "",
    output_hash: str = "",
    budget_reference: str = "",
    cost_usd: float | None = None,
    action_taken: str = "",
    reason_text: str = "",
) -> None:
    """Complete a claimed QC attempt with result and accounting reference."""
    ensure_qc_retry_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    val_cost = float(cost_usd) if cost_usd is not None else None
    with conn:
        conn.execute(
            """
            UPDATE workflow_qc_retries
            SET status = ?,
                verdict = ?,
                qc_report_id = COALESCE(NULLIF(?, ''), qc_report_id),
                output_hash = COALESCE(NULLIF(?, ''), output_hash),
                budget_reference = COALESCE(NULLIF(?, ''), budget_reference),
                cost_usd = COALESCE(?, cost_usd),
                action_taken = COALESCE(NULLIF(?, ''), action_taken),
                reason_text = COALESCE(NULLIF(?, ''), reason_text),
                updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                str(verdict),
                qc_report_id,
                output_hash,
                budget_reference,
                val_cost,
                action_taken,
                reason_text,
                now,
                attempt_id,
            ),
        )


def get_item_remaining_recoveries(
    conn: sqlite3.Connection | None,
    *,
    workflow_id: str,
    retry_type: str,
    item_id: str,
    max_recoveries: int | None = None,
) -> int:
    """Calculate remaining recovery attempts allowed for an item.

    Total allowed attempts = 1 (initial) + N (recoveries).
    If used <= 1, remaining recoveries = N.
    If used > 1, remaining recoveries = max(0, (1 + N) - used).
    """
    n = clamp_max_retries(max_recoveries)
    used = get_item_retry_count(conn, workflow_id, retry_type, item_id)
    max_total = 1 + n
    return max(0, max_total - used)


def record_qc_retry(
    conn: sqlite3.Connection | None,
    workflow_id: str = "",
    retry_type: str = "",
    *,
    item_id: str = "",
    attempt_number: int | None = None,
    verdict: str = "fail",
    input_hash: str = "",
    qc_report_id: str = "",
    cost_usd: float | None = None,
    input_url: str = "",
    action_taken: str = "",
    reason_text: str = "",
    retry_category: str = "quality",
    candidate_asset_id: str | None = None,
    **kwargs: Any,
) -> None:
    """Record a QC attempt/retry persistently in SQLite (deduplicating existing attempt records)."""
    if conn is None:
        return
    ensure_qc_retry_schema(conn)
    if candidate_asset_id and not item_id:
        item_id = str(candidate_asset_id)
    if not item_id:
        item_id = "default"
    if attempt_number is None:
        attempt_number = get_qc_retry_count(conn, workflow_id, retry_type) + 1
    now = datetime.now(timezone.utc).isoformat()
    val_cost = float(cost_usd) if cost_usd is not None else None
    with conn:
        existing = conn.execute(
            """
            SELECT id FROM workflow_qc_retries
            WHERE workflow_id = ? AND retry_type = ? AND item_id = ? AND attempt_number = ?
            """,
            (workflow_id, retry_type, str(item_id), attempt_number),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE workflow_qc_retries
                SET verdict = ?,
                    input_hash = ?,
                    qc_report_id = ?,
                    cost_usd = ?,
                    input_url = ?,
                    action_taken = ?,
                    reason_text = ?,
                    retry_category = ?,
                    status = 'completed',
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    str(verdict),
                    input_hash,
                    qc_report_id,
                    val_cost,
                    input_url,
                    action_taken,
                    reason_text,
                    retry_category,
                    now,
                    existing[0] if isinstance(existing, tuple) else existing["id"],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO workflow_qc_retries (
                    workflow_id, retry_type, item_id, attempt_number,
                    verdict, input_hash, qc_report_id, cost_usd,
                    input_url, action_taken, reason_text, retry_category,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?)
                """,
                (
                    workflow_id,
                    retry_type,
                    str(item_id),
                    attempt_number,
                    str(verdict),
                    input_hash,
                    qc_report_id,
                    val_cost,
                    input_url,
                    action_taken,
                    reason_text,
                    retry_category,
                    now,
                    now,
                ),
            )


def get_qc_retry_count(
    conn: sqlite3.Connection | None,
    workflow_id: str,
    retry_type: str,
) -> int:
    """Get durable attempt count for a workflow and retry type."""
    if conn is None:
        return 0
    ensure_qc_retry_schema(conn)
    row = conn.execute(
        """
        SELECT MAX(attempt_number) FROM workflow_qc_retries
        WHERE workflow_id = ? AND retry_type = ?
        """,
        (workflow_id, retry_type),
    ).fetchone()
    if row and row[0] is not None:
        return int(row[0])
    return 0


def get_item_retry_count(
    conn: sqlite3.Connection | None,
    workflow_id: str,
    retry_type: str,
    item_id: str,
) -> int:
    """Get durable attempt count for a specific item (e.g. frame_id or asset_id)."""
    if conn is None:
        return 0
    ensure_qc_retry_schema(conn)
    row = conn.execute(
        """
        SELECT MAX(attempt_number) FROM workflow_qc_retries
        WHERE workflow_id = ? AND retry_type = ? AND item_id = ?
        """,
        (workflow_id, retry_type, str(item_id)),
    ).fetchone()
    if row and row[0] is not None:
        return int(row[0])
    return 0


def get_qc_retry_history(
    conn: sqlite3.Connection | None,
    workflow_id: str,
    retry_type: str | None = None,
) -> list[dict[str, Any]]:
    """Retrieve full audit history of QC attempts for a workflow."""
    if conn is None:
        return []
    ensure_qc_retry_schema(conn)
    query = "SELECT * FROM workflow_qc_retries WHERE workflow_id = ?"
    params: list[Any] = [workflow_id]
    if retry_type:
        query += " AND retry_type = ?"
        params.append(retry_type)
    query += " ORDER BY id ASC"
    rows = conn.execute(query, params).fetchall()
    results = []
    for r in rows:
        results.append({
            "id": r[0] if isinstance(r, tuple) else r["id"],
            "workflow_id": r[1] if isinstance(r, tuple) else r["workflow_id"],
            "retry_type": r[2] if isinstance(r, tuple) else r["retry_type"],
            "item_id": r[3] if isinstance(r, tuple) else r["item_id"],
            "attempt_number": r[4] if isinstance(r, tuple) else r["attempt_number"],
            "verdict": r[5] if isinstance(r, tuple) else r["verdict"],
            "input_hash": r[6] if isinstance(r, tuple) else r.get("input_hash", ""),
            "qc_report_id": r[7] if isinstance(r, tuple) else r.get("qc_report_id", ""),
            "cost_usd": r[8] if isinstance(r, tuple) else r.get("cost_usd", 0.0),
            "input_url": r[9] if isinstance(r, tuple) else r.get("input_url", ""),
            "action_taken": r[10] if isinstance(r, tuple) else r.get("action_taken", ""),
            "reason_text": r[11] if isinstance(r, tuple) else r.get("reason_text", ""),
            "created_at": r[12] if isinstance(r, tuple) else r.get("created_at", ""),
        })
    return results


def plan_targeted_keyframe_recovery(
    qc_report: QCReport | dict[str, Any],
    generated_keyframes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build targeted keyframe recovery plan.

    - Re-generates failing frames.
    - Cascades dependency: if START fails, END must also be regenerated.
    - Preserves frames that already passed Keyframe QC.
    - Extracts feedback and violation reasons for prompt injection.
    """
    findings = qc_report.findings if hasattr(qc_report, "findings") else (qc_report.get("findings") or {})
    items = findings.get("items") or []

    failing_frame_ids: set[str] = set()
    feedback_by_frame: dict[str, str] = {}
    passed_frame_ids: set[str] = set()

    for item in items:
        frame_id = str(item.get("frame_id") or "")
        verdict = str(item.get("verdict") or "").lower()
        hard_failure = bool(item.get("hard_failure", False))

        if verdict == "pass" and not hard_failure:
            passed_frame_ids.add(frame_id)
        else:
            failing_frame_ids.add(frame_id)
            action = str(item.get("recommended_action") or "").strip()
            reason = str(item.get("reason") or "Keyframe quality control failed").strip()
            feedback = f"{action}: {reason}" if action else reason
            feedback_by_frame[frame_id] = feedback

    # Cascade dependencies: if a START frame must be regenerated, its dependent END frame must too
    cascade_regenerations: set[str] = set()
    for frame in generated_keyframes:
        frame_id = str(frame.get("frame_id") or "")
        scene_id = str(frame.get("scene_id") or frame_id.removesuffix("-start").removesuffix("-end"))
        frame_role = str(frame.get("frame_role") or ("start" if frame_id.endswith("-start") else "end"))

        if frame_role == "end":
            start_frame_id = f"{scene_id}-start"
            if start_frame_id in failing_frame_ids:
                if frame_id not in failing_frame_ids:
                    cascade_regenerations.add(frame_id)
                    feedback_by_frame[frame_id] = "START frame was regenerated; align scene conditioning with new START frame."

    failing_frame_ids.update(cascade_regenerations)
    # Ensure cascade-affected frames are removed from passed
    passed_frame_ids.difference_update(failing_frame_ids)

    return {
        "failing_frame_ids": sorted(failing_frame_ids),
        "passed_frame_ids": sorted(passed_frame_ids),
        "feedback_by_frame": feedback_by_frame,
        "cascade_regenerations": sorted(cascade_regenerations),
    }
