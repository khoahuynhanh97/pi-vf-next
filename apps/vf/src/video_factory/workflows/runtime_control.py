"""Durable checkout-wide pause state stored in the existing Video Factory SQLite DB."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from video_factory.automation.failure_classification import FailureCategory, classify_failure

PROVIDER_AVAILABILITY_PAUSE_CODES = frozenset({
    "PROVIDER_CAPACITY_WAIT", "RATE_LIMITED", "HTTP_429", "RESOURCE_EXHAUSTED",
})

SHARED_INFRA_PAUSE_CODES = frozenset({
    *PROVIDER_AVAILABILITY_PAUSE_CODES,
    "QC_PROVIDER_UNAVAILABLE",
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


def verified_render_finalization_checkpoint(
    conn: sqlite3.Connection, workflow_id: str, batch_id: str, provider_kind: str,
) -> dict[str, Any] | None:
    """Prove only polling/finalization remains for the exact render batch."""
    try:
        batch = conn.execute(
            "SELECT project_id, workflow_id, scene_count FROM video_factory_render_batches "
            "WHERE render_batch_id=?", (batch_id,),
        ).fetchone()
        if batch is None or batch['workflow_id'] != workflow_id or int(batch['scene_count']) < 1:
            return None
        rows = conn.execute(
            "SELECT operation_id, scene_id, status, provider_operation_id, output_path, "
            "output_sha256, provider_kind, render_batch_id FROM video_factory_scene_operation "
            "WHERE render_batch_id=? ORDER BY scene_order", (batch_id,),
        ).fetchall()
        if len(rows) != int(batch['scene_count']):
            return None
        scene_evidence = []
        active = []
        for index, row in enumerate(rows):
            if (not row['provider_operation_id'] or row['provider_kind'] != provider_kind
                    or row['render_batch_id'] != batch_id):
                return None
            if row['status'] == 'completed':
                output = Path(str(row['output_path'] or ''))
                expected = str(row['output_sha256'] or '').removeprefix('sha256:').lower()
                if (not output.is_file() or not expected
                        or hashlib.sha256(output.read_bytes()).hexdigest() != expected):
                    return None
            elif row['status'] in {'submitted', 'running'} and index == len(rows) - 1:
                active.append(row)
            else:
                return None
            scene_evidence.append({
                'operation_id': row['operation_id'], 'scene_id': row['scene_id'],
                'provider_operation_id': row['provider_operation_id'], 'status': row['status'],
                'sha256': expected if row['status'] == 'completed' else None,
            })
        if len(active) > 1:
            return None
        return {'project_id': batch['project_id'], 'scenes': scene_evidence}
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None


def workflow_can_advance_during_capacity_wait(connection: sqlite3.Connection, workflow_id: str) -> bool:
    if workflow_has_inflight_operation(connection, workflow_id):
        return True
    row = connection.execute("SELECT aggregate_json FROM workflow WHERE workflow_id=?", (workflow_id,)).fetchone()
    if not row:
        return False
    aggregate = json.loads(row[0])
    if aggregate.get("state") in {"AwaitingFinalApproval", "exporting"}:
        return True
    batch_id = str(aggregate.get("active_render_job_id") or "")
    if aggregate.get("state") != "rendering" or not batch_id.startswith("batch_"):
        return False
    storyboard = aggregate.get("latest_artifact") or aggregate.get("storyboard") or {}
    provider = str((storyboard.get("backend_profile") or {}).get("provider_kind") or "")
    if not provider:
        return False
    evidence = verified_render_finalization_checkpoint(connection, workflow_id, batch_id, provider)
    return bool(evidence and evidence["project_id"] == aggregate.get("project_id"))


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


def _resume_capacity_wait(connection: sqlite3.Connection, pause: dict[str, Any]) -> dict[str, Any] | None:
    """Clear a transient pause once persisted provider work has settled."""
    if pause["code"] != "PROVIDER_CAPACITY_WAIT" or connection.in_transaction:
        return pause
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "video_factory_provider_capacity" not in tables:
        return pause
    from video_factory.integrations.capacity import ProviderCapacityStore, process_lease_owner_alive
    import time

    connection.execute("BEGIN IMMEDIATE")
    with connection:
        current = get_pipeline_pause(connection)
        if current != pause:
            return current
        ProviderCapacityStore.cleanup_orphaned_slots(connection)
        if connection.execute("SELECT 1 FROM video_factory_provider_capacity LIMIT 1").fetchone():
            return pause
        for table in ("video_factory_keyframe_operation", "video_factory_scene_operation"):
            if table in tables and connection.execute(
                f"SELECT 1 FROM {table} WHERE status IN "
                "('unknown','submitting','project_created','submitted','running') LIMIT 1"
            ).fetchone():
                return pause
        if "flow_ui_lease" in tables and any(
            lease[1] > time.time() or process_lease_owner_alive(lease[0])
            for lease in connection.execute("SELECT owner, expires_at FROM flow_ui_lease")
        ):
            return pause
        connection.execute(
            "UPDATE product_video_runtime_control SET paused=0, resumed_at=?, resume_note=? WHERE state_id=1",
            (datetime.now(timezone.utc).isoformat(), "Provider capacity is available."),
        )
        return None


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
        return _resume_capacity_wait(connection, pause)
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
