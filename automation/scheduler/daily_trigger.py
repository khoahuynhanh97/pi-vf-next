"""Daily trigger and deduplicated scheduler for PI/VF automation.

Implements Bangkok timezone scheduling, input hash deduplication,
and late-input catch-up execution.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from automation.storage import LedgerLockTimeout, atomic_json, exclusive_file_lock

from automation.config import (
    BANGKOK_TZ,
    DAILY_VIDEO_LIMIT,
    RUNTIME_DIR,
    now_bangkok,
    today_bangkok_str,
)
from automation.pipelines.spark_pipeline import PipelineRunResult, prepare_sheet_intake, run_pipeline

LEDGER_PATH = RUNTIME_DIR / "scheduler_ledger.json"


@dataclass
class ScheduleRecord:
    date: str
    csv_sha256: str
    run_id: str
    status: str
    timestamp: str
    mp4_count: int
    duration_seconds: float
    error: str | None = None
    provider_mode: str = "fake"
    sheet_batches_sha256: str | None = None


class CorruptLedgerError(RuntimeError):
    pass


def _ledger_lock(timeout: float = 5.0):
    return exclusive_file_lock(LEDGER_PATH, timeout, label="scheduler ledger")


def load_ledger() -> list[dict[str, Any]]:
    """Load a consistent schedule ledger snapshot."""
    with _ledger_lock():
        return _load_ledger()


def _load_ledger() -> list[dict[str, Any]]:
    try:
        raw = LEDGER_PATH.read_bytes()
    except FileNotFoundError:
        return []
    try:
        records = json.loads(raw.decode("utf-8"))
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            raise ValueError("expected a list of ledger records")
        return records
    except ValueError as exc:
        backup = LEDGER_PATH.with_name(f"{LEDGER_PATH.name}.corrupt-{uuid4().hex}")
        backup.write_bytes(raw)
        raise CorruptLedgerError(
            f"Corrupt scheduler ledger {LEDGER_PATH}. Backup saved to {backup}: {exc}"
        ) from exc


def save_ledger(records: list[dict[str, Any]]) -> None:
    """Save schedule ledger."""
    with _ledger_lock():
        atomic_json(LEDGER_PATH, records)


def record_run(record: ScheduleRecord) -> None:
    """Append or update a run in the scheduler ledger."""
    with _ledger_lock():
        records = _load_ledger()
        updated = [
            r for r in records
            if (r.get("run_id"), r.get("provider_mode", "fake")) != (record.run_id, record.provider_mode)
        ]
        updated.append(asdict(record))
        atomic_json(LEDGER_PATH, updated)


def should_run_today(
    csv_path: Path, target_date: str | None = None, force: bool = False, *, provider_mode: str = "fake",
    sheet_batches_sha256: str | None = None,
) -> tuple[bool, str]:
    """Check if a pipeline run should be scheduled today or if input changed."""
    if force:
        return True, "Forced execution requested"

    if not csv_path.is_file():
        return False, f"CSV file not found: {csv_path}"

    date_str = target_date or today_bangkok_str()
    csv_bytes = csv_path.read_bytes()
    current_sha = hashlib.sha256(csv_bytes).hexdigest()

    records = load_ledger()
    for rec in reversed(records):
        if (rec.get("date") == date_str and rec.get("csv_sha256") == current_sha
                and rec.get("provider_mode", "fake") == provider_mode
                and rec.get("sheet_batches_sha256") == sheet_batches_sha256):
            if rec.get("status") == "success":
                return False, f"Already completed for {date_str} with CSV digest {current_sha[:8]}"
            elif rec.get("status") == "partial":
                return True, f"Prior run for {date_str} was partial ({rec.get('mp4_count')} video(s)); resuming remaining items"
            elif rec.get("status") == "waiting":
                return True, f"Prior run for {date_str} is waiting for operator or real material; retry needed"
            elif rec.get("status") == "failed":
                return True, f"Prior run for {date_str} failed; retry needed"

    return True, f"New or updated intake needed for {date_str} (CSV digest {current_sha[:8]})"


def trigger_daily_run(
    spark_csv: Path | str | None = None,
    limit: int | None = None,
    scenes: int = 4,
    provider_mode: str = "fake",
    force: bool = False,
    date: str | None = None,
    offline_intake: bool | None = None,
    qc_level: str = "low",
    acquire: bool = False,
) -> PipelineRunResult | dict[str, Any]:
    """Trigger a daily run with deduplication and late input handling."""
    load_ledger()  # Stop before acquisition even when force bypasses deduplication.
    target_date = date or today_bangkok_str()
    if limit is None:
        limit = DAILY_VIDEO_LIMIT if spark_csv is None else 1
    if spark_csv is None and not 1 <= limit <= DAILY_VIDEO_LIMIT:
        raise ValueError(f"Sheet limit must be between 1 and {DAILY_VIDEO_LIMIT}")
    sheet_intake = prepare_sheet_intake(target_date, limit, acquire=acquire) if spark_csv is None else None
    ready = (sheet_intake or {}).get("exchange_batches")
    sheet_batches_sha256 = (hashlib.sha256(json.dumps([limit, ready], sort_keys=True).encode()).hexdigest()
                            if isinstance(ready, list) else None)
    selection_csv = (sheet_intake or {}).get("selection_csv") or spark_csv
    if selection_csv is not None:
        csv_p = Path(selection_csv).resolve()
        needed, reason = should_run_today(csv_p, target_date=target_date, force=force, provider_mode=provider_mode,
                                         sheet_batches_sha256=sheet_batches_sha256)
        if not needed:
            return {"status": "skipped", "reason": reason, "date": target_date, "csv_path": str(csv_p)}

    run_res = run_pipeline(
        spark_csv=spark_csv,
        limit=limit,
        scenes=scenes,
        date=target_date,
        provider_mode=provider_mode,
        offline_intake=offline_intake,
        qc_level=qc_level,
        sheet_intake=sheet_intake,
    )

    record = ScheduleRecord(
        date=target_date,
        csv_sha256=run_res.spark_csv_sha256,
        run_id=run_res.run_id,
        status=run_res.status,
        timestamp=now_bangkok().isoformat(),
        mp4_count=len(run_res.exported_videos),
        duration_seconds=run_res.duration_seconds,
        error=run_res.error,
        provider_mode=provider_mode,
        sheet_batches_sha256=sheet_batches_sha256,
    )
    record_run(record)
    if run_res.status not in ("success", "skipped"):
        try:
            from automation.doctor_agent import DoctorAgent
            DoctorAgent().handle_pipeline_incident(run_res)
        except Exception as doctor_exc:
            logger.warning("Doctor agent auto-incident handler failed: %s", doctor_exc)
    return run_res


def run_scheduler_daemon(
    target_hour: int = 8,
    target_minute: int = 0,
    check_interval_seconds: int = 60,
    spark_csv: Path | str | None = None,
    limit: int | None = None,
    scenes: int = 4,
    provider_mode: str = "fake",
    qc_level: str = "low",
    acquire: bool = False,
    on_result: Callable[[PipelineRunResult], None] | None = None,
) -> None:
    """Run a foreground daemon that checks every interval and triggers daily run."""
    print(f"[*] PI/VF Scheduler Daemon running (Target: {target_hour:02d}:{target_minute:02d} Asia/Bangkok)")
    while True:
        now = now_bangkok()
        if now.time() >= dtime(target_hour, target_minute):
            res = trigger_daily_run(
                spark_csv=spark_csv,
                limit=limit,
                scenes=scenes,
                provider_mode=provider_mode,
                qc_level=qc_level,
                acquire=acquire,
                date=now.date().isoformat(),
            )
            if not isinstance(res, dict):
                print(f"[+] Daily run status: {res.status}")
                if on_result is not None:
                    on_result(res)

        time.sleep(check_interval_seconds)
