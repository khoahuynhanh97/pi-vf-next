"""Durable checkout-wide pause state stored in the existing Video Factory SQLite DB."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from video_factory.automation.failure_classification import FailureCategory, classify_failure

SHARED_INFRA_PAUSE_CODES = frozenset({
    "PROVIDER_CAPACITY_WAIT",
    "QC_PROVIDER_UNAVAILABLE",
    "RATE_LIMITED",
    "HTTP_429",
    "RESOURCE_EXHAUSTED",
    "PROVIDER_TIMEOUT",
    "PROVIDER_NETWORK_ERROR",
    "MISSING_PROVIDER_CONFIG",
    "NINE_ROUTER_CONFIGURATION_ERROR",
    "INVALID_QC_PROVIDER_CONFIG",
    "QC_ROUTE_CONFIG_INVALID",
    "ROUTE_PREFLIGHT_FAILED",
    "ROUTE_NOT_VERIFIED",
    "CONTRACT_BROKEN",
})


def is_shared_infra_blocker(code: str | None) -> bool:
    """Return True only if code represents a global shared infrastructure outage, not a per-product failure."""
    if not code:
        return False
    normalized = str(code).strip().upper().replace("-", "_")
    return any(c in normalized for c in SHARED_INFRA_PAUSE_CODES)


def workflow_has_inflight_operation(connection: sqlite3.Connection, workflow_id: str) -> bool:
    """Return True if workflow_id has an active in-flight operation currently holding or polling provider capacity."""
    if not workflow_id:
        return False
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('video_factory_keyframe_operation','video_factory_scene_operation','video_factory_render_batches')"
    )}
    if "video_factory_keyframe_operation" in tables and connection.execute(
        "SELECT 1 FROM video_factory_keyframe_operation "
        "WHERE workflow_id=? AND status IN ('submitted','running') LIMIT 1",
        (workflow_id,),
    ).fetchone():
        return True
    return {"video_factory_scene_operation", "video_factory_render_batches"} <= tables and bool(connection.execute(
        "SELECT 1 FROM video_factory_scene_operation AS scene "
        "JOIN video_factory_render_batches AS batch ON batch.render_batch_id=scene.render_batch_id "
        "AND batch.owner_user_id=scene.owner_user_id AND batch.project_id=scene.project_id "
        "WHERE batch.workflow_id=? AND scene.status IN ('submitted','running') LIMIT 1",
        (workflow_id,),
    ).fetchone())


_SCHEMA = """
CREATE TABLE IF NOT EXISTS product_video_runtime_control (
    state_id INTEGER PRIMARY KEY CHECK (state_id = 1),
    paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    category TEXT NOT NULL DEFAULT '',
    code TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    workflow_id TEXT NOT NULL DEFAULT '',
    paused_at TEXT NOT NULL DEFAULT '',
    resumed_at TEXT NOT NULL DEFAULT '',
    resume_note TEXT NOT NULL DEFAULT ''
)
"""


def _ensure(connection: sqlite3.Connection) -> None:
    connection.execute(_SCHEMA)
    connection.execute(
        "INSERT OR IGNORE INTO product_video_runtime_control(state_id, paused) VALUES (1, 0)"
    )


def is_workflow_terminally_skipped(connection: sqlite3.Connection, workflow_id: str) -> bool:
    """Require both terminal aggregate state and the durable operator audit event."""
    required_tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('workflow','workflow_event')"
        )
    }
    if required_tables != {"workflow", "workflow_event"}:
        return False
    row = connection.execute(
        "SELECT aggregate_json FROM workflow WHERE workflow_id = ?", (workflow_id,)
    ).fetchone()
    if not row:
        return False
    try:
        aggregate = json.loads(row[0])
    except (TypeError, ValueError):
        return False
    if not isinstance(aggregate, dict) or aggregate.get("state") != "skipped":
        return False
    event_exists = connection.execute(
        "SELECT 1 FROM workflow_event WHERE workflow_id = ? "
        "AND event_type = 'OperatorWorkflowSkipped' LIMIT 1",
        (workflow_id,),
    ).fetchone()
    return event_exists is not None


def skip_unresolved_workflow(connection: sqlite3.Connection, workflow_id: str, reason: str, *,
                             known_product_failure: bool = False) -> None:
    """Terminally skip one workflow while preserving all provider-attempt history."""
    clean_reason = str(reason or "").strip()
    if not clean_reason or len(clean_reason) > 500:
        raise ValueError("A skip reason of 1–500 characters is required.")
    if is_workflow_terminally_skipped(connection, workflow_id):
        return

    unresolved = False
    for table, where in (
        ("workflow_qc_retries", "status IN ('reserved','claimed','sent','technical_unknown')"),
        ("product_video_operation_attempt", "capability_id LIKE 'product_video.qc.%' AND status IN ('reserved','claimed','sent','technical_unknown')"),
    ):
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists and connection.execute(
            f"SELECT 1 FROM {table} WHERE workflow_id=? AND {where} LIMIT 1", (workflow_id,)
        ).fetchone():
            unresolved = True
            break
    known_failure = classify_failure(clean_reason) == FailureCategory.PRODUCT_REJECTED
    known_creative_response = "CREATIVE_RESPONSE_INVALID" in clean_reason.upper()
    if known_product_failure and unresolved:
        raise ValueError("Workflow still has an unresolved attempt; reconcile provider outcome before automatic skip.")
    if not unresolved and not (known_product_failure and (known_failure or known_creative_response)):
        raise ValueError("Workflow has no unresolved QC attempt; refusing to skip it through this recovery command.")

    now = datetime.now(timezone.utc)
    outbox_rows = connection.execute(
        "SELECT outbox_id,state,lease_expires_at FROM workflow_outbox WHERE workflow_id=? AND state IN ('pending','leased')",
        (workflow_id,),
    ).fetchall()
    for row in outbox_rows:
        expiry = str(row["lease_expires_at"] or "")
        if row["state"] == "leased" and expiry:
            try:
                parsed = datetime.fromisoformat(expiry[:-1] + "+00:00" if expiry.endswith("Z") else expiry)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                if parsed > now:
                    raise ValueError(f"Workflow has an active outbox lease until {expiry}; wait for it to expire before skipping.")
            except ValueError as exc:
                if "active outbox lease" in str(exc):
                    raise
                raise ValueError("Workflow has a malformed outbox lease; inspect the worker before skipping.") from exc

    from video_factory.workflows.repository import WorkflowRepository

    repo = WorkflowRepository(connection)
    current = repo.get_workflow(workflow_id)
    if current is None:
        raise ValueError(f"Workflow {workflow_id!r} does not exist.")
    aggregate, version = current
    aggregate = dict(aggregate)
    aggregate.update({"state": "skipped", "skip_reason": clean_reason, "skipped_at": now.isoformat(), "operator_action": "terminal_skip"})
    event = {
        "event_id": f"operator-skip-{workflow_id}-{int(now.timestamp())}",
        "command_id": f"operator-skip-{workflow_id}-{int(now.timestamp())}",
        "event_type": "OperatorWorkflowSkipped",
        "timestamp": now.isoformat(),
        "payload": {"reason": clean_reason, "unresolved_qc_preserved": True},
    }
    with connection:
        repo.commit_workflow(
            workflow_id, json.dumps(aggregate, sort_keys=True), [event], expected_version=version,
            in_transaction=True,
        )
        connection.execute(
            "UPDATE workflow_outbox SET state='quarantined', last_error=?, lease_token=NULL, lease_owner=NULL, lease_expires_at=NULL, updated_at=? "
            "WHERE workflow_id=? AND state IN ('pending','leased')",
            (f"[OPERATOR_SKIPPED] {clean_reason}", now.isoformat(), workflow_id),
        )
        connection.execute(
            "UPDATE daily_product_video_import SET status='error', last_error=?, updated_at=? WHERE workflow_id=?",
            (f"OPERATOR_SKIPPED:{clean_reason}", now.isoformat(), workflow_id),
        )


def get_pipeline_pause(connection: sqlite3.Connection) -> dict[str, Any] | None:
    # This is called from worker/coordinator read paths that may use a second
    # SQLite connection while another connection is committing workflow state.
    # Keep pause inspection read-only; schema/row initialization belongs to
    # pause_pipeline/resume_pipeline.
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_video_runtime_control'"
    ).fetchone()
    if not exists:
        return None
    row = connection.execute(
        "SELECT * FROM product_video_runtime_control WHERE state_id=1 AND paused=1"
    ).fetchone()
    return dict(row) if row else None


def ensure_pipeline_pause(connection: sqlite3.Connection) -> dict[str, Any] | None:
    """Restore a durable pause from legacy quarantined infrastructure/contract failures.

    Older workers quarantined route and contract errors without writing the
    checkout-wide control row. Treat those explicit codes as blockers before
    any new claim; semantic product failures are deliberately excluded.
    """
    pause = get_pipeline_pause(connection)
    if pause:
        return pause
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_outbox'"
    ).fetchone()
    if not exists:
        return None
    control_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_video_runtime_control'"
    ).fetchone()
    resumed = (
        connection.execute(
            "SELECT workflow_id, resumed_at FROM product_video_runtime_control WHERE state_id=1"
        ).fetchone()
        if control_exists
        else None
    )
    resumed_workflow_id = str(resumed["workflow_id"] or "") if resumed else ""
    resumed_at = str(resumed["resumed_at"] or "") if resumed else ""
    rows = connection.execute(
        "SELECT workflow_id, last_error, updated_at FROM workflow_outbox "
        "WHERE state='quarantined' AND COALESCE(last_error,'') != '' "
        "ORDER BY outbox_id DESC"
    ).fetchall()
    for row in rows:
        if is_workflow_terminally_skipped(connection, str(row["workflow_id"] or "")):
            continue
        # A governed pipeline-resume explicitly acknowledges the blocker that
        # caused the pause. Do not reconstruct the same pause from its stale
        # quarantine row; a newer quarantine still pauses the checkout.
        if (
            resumed_at
            and str(row["workflow_id"] or "") == resumed_workflow_id
            and str(row["updated_at"] or "") <= resumed_at
        ):
            continue
        match = re.search(r"\[([A-Z][A-Z0-9_]+)\]", str(row["last_error"] or ""))
        if not match:
            continue
        code = match.group(1)
        category = classify_failure(code)
        if category not in {FailureCategory.QC_INFRA_PAUSED, FailureCategory.CONTRACT_BROKEN}:
            continue
        pause_pipeline(
            connection,
            category=category,
            code=code,
            message=(
                "A quarantined job from an earlier worker reports an unresolved "
                f"{category.value} failure ({code}); inspect and reconcile that workflow before resuming."
            ),
            workflow_id=str(row["workflow_id"] or ""),
        )
        return get_pipeline_pause(connection)
    return None


def pause_pipeline(
    connection: sqlite3.Connection,
    *,
    category: FailureCategory,
    code: str,
    message: str,
    workflow_id: str | None = None,
) -> None:
    _ensure(connection)
    now = datetime.now(timezone.utc).isoformat()
    with connection:
        connection.execute(
            """UPDATE product_video_runtime_control
               SET paused=1, category=?, code=?, message=?, workflow_id=?, paused_at=?,
                   resumed_at='', resume_note=''
               WHERE state_id=1""",
            (category.value, str(code), str(message), str(workflow_id or ""), now),
        )


def resume_pipeline(connection: sqlite3.Connection, *, note: str) -> bool:
    _ensure(connection)
    clean_note = str(note or "").strip()
    if not clean_note:
        raise ValueError("A note describing the repair/reconciliation is required to resume the pipeline.")
    now = datetime.now(timezone.utc).isoformat()
    with connection:
        cursor = connection.execute(
            """UPDATE product_video_runtime_control SET paused=0, resumed_at=?, resume_note=?
               WHERE state_id=1 AND paused=1""",
            (now, clean_note),
        )
    return cursor.rowcount == 1
