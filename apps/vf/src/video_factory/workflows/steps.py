"""Append-only, structured pipeline progress stored in the existing workflow_event table."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

PIPELINE_STEP_STATUSES = {
    "pending", "running", "completed", "failed", "uncertain", "pending_operator", "skipped",
}
PIPELINE_STEP_NAMES = {
    "INPUT_ADMISSION", "IMAGE_QC", "PRODUCT_LOCK", "CREATIVE_BRIEF", "SCENE_PLAN",
    "KEYFRAME_GENERATION", "KEYFRAME_QC", "VIDEO_RENDER", "VIDEO_QC", "EXPORT",
}
_BEARER_SECRET = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_NAMED_SECRET = re.compile(
    r"(?i)\b(authorization|x-api-key|api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password)\s*[:=]\s*[^\s,;]+"
)
_PROMPT_CONTENT = re.compile(r"(?is)\b(prompt|system_instruction|messages)\s*[:=]\s*.*")


def _safe_log_text(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    text = _PROMPT_CONTENT.sub(r"\1=[redacted]", value)
    text = _BEARER_SECRET.sub("Bearer [redacted]", text)
    text = _NAMED_SECRET.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    return text[:limit]


def record_pipeline_step(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    product_key: str,
    step_name: str,
    status: str,
    attempt_number: int | None = None,
    operation_id: str | None = None,
    input_digest: str | None = None,
    provider: str | None = None,
    requested_model: str | None = None,
    actual_model: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    duration_ms: int | None = None,
    cost_status: str = "not_applicable",
    cost_usd: float | None = None,
    error_code: str | None = None,
    message: str | None = None,
    next_action: str | None = None,
    retry_reason: str | None = None,
) -> None:
    """Persist one sanitized step transition without mutating workflow history/state."""
    if step_name not in PIPELINE_STEP_NAMES:
        raise ValueError(f"unknown pipeline step: {step_name}")
    if status not in PIPELINE_STEP_STATUSES:
        raise ValueError(f"unknown pipeline step status: {status}")
    if cost_status not in {"recorded", "estimated", "unknown", "not_sent", "not_applicable"}:
        raise ValueError(f"unknown cost status: {cost_status}")
    now = datetime.now(timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "schema": "pipeline_step.v1",
        "workflow_id": workflow_id,
        "product_key": product_key,
        "step_name": step_name,
        "status": status,
        "attempt_number": attempt_number,
        "operation_id": operation_id,
        "input_digest": input_digest,
        "provider": provider,
        "requested_model": requested_model,
        "actual_model": actual_model,
        "started_at": started_at or (now if status == "running" else None),
        "finished_at": finished_at or (now if status != "running" else None),
        "duration_ms": duration_ms,
        "cost_status": cost_status,
        "cost_usd": cost_usd,
        "error_code": _safe_log_text(error_code, 128),
        "message": _safe_log_text(message, 500),
        "next_action": _safe_log_text(next_action, 1000),
        "retry_reason": _safe_log_text(retry_reason, 500),
    }
    with connection:
        connection.execute(
            "INSERT INTO workflow_event(event_id,workflow_id,event_type,timestamp,payload_json) VALUES(?,?,?,?,?)",
            (f"evt_step_{uuid4().hex}", workflow_id, "PipelineStep", now, json.dumps(payload, ensure_ascii=False)),
        )


def read_pipeline_steps(connection: sqlite3.Connection, workflow_id: str) -> list[dict[str, Any]]:
    """Return the latest persisted state for each step in event order."""
    latest: dict[str, dict[str, Any]] = {}
    for row in connection.execute(
        "SELECT payload_json FROM workflow_event WHERE workflow_id=? AND event_type='PipelineStep' ORDER BY rowid",
        (workflow_id,),
    ):
        try:
            payload = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("step_name") in PIPELINE_STEP_NAMES:
            latest[str(payload["step_name"])] = payload
    return list(latest.values())
