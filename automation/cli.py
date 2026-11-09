"""Command-line interface for Unified PI/VF Product Automation Platform."""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from automation.config import (
    BANGKOK_TZ,
    DAILY_VIDEO_LIMIT,
    DEFAULT_PROFILE,
    INBOX_ROOT,
    PI_DIR,
    PI_PYTHON,
    PI_RUNS_DIR,
    ROOT_DIR,
    RUNTIME_DIR,
    VF_DB_PATH,
    VF_DIR,
    VF_EXPORTS_DIR,
    VF_PYTHON,
    VF_RUNTIME_DIR,
    VF_WORKSPACE_DIR,
    ensure_runtime_dirs,
    now_bangkok,
    today_bangkok_str,
)
from automation.daily_report import report_configured_run
from automation.pipelines.spark_pipeline import probe_mp4_file, run_pipeline
from automation.scheduler.daily_trigger import (
    CorruptLedgerError,
    LedgerLockTimeout,
    ScheduleRecord,
    load_ledger,
    record_run,
    run_scheduler_daemon,
    should_run_today,
    trigger_daily_run,
)


def cmd_run(args: argparse.Namespace) -> int:
    """Run full automated pipeline from Spark CSV to Decodable MP4."""
    load_ledger()
    print(f"=== Unified PI/VF Product Automation: Run ===")
    print(f"Input:         {args.csv or 'Two configured Google Sheets'}")
    print(f"Date:          {args.date or today_bangkok_str()} (Asia/Bangkok)")
    print(f"Scenes:        {args.scenes} (target scenes per video)")
    print(f"Limit:         {args.limit}")
    print(f"Provider Mode: {args.provider_mode}")
    print(f"==============================================\n")

    res = run_pipeline(
        spark_csv=args.csv,
        limit=args.limit,
        scenes=args.scenes,
        date=args.date,
        provider_mode=args.provider_mode,
        run_id=args.run_id,
        profile_path=args.profile,
        db_path=args.db_path,
        offline_intake=not args.live_intake,
        qc_level=args.qc_level,
    )

    print(f"\n--- Run Finished ---")
    print(f"Run ID:        {res.run_id}")
    print(f"Status:        {res.status.upper()}")
    print(f"Duration:      {res.duration_seconds}s")
    if res.error:
        print(f"Note/Error:    {res.error}")

    if res.product_outcomes:
        print(f"\nProduct Outcomes ({len(res.product_outcomes)}):")
        for item in res.product_outcomes:
            st = item.get("status", "unknown").upper()
            print(f"  [{st}] {item.get('product_name')}")
            print(f"         Stage:     {item.get('stage')}")
            print(f"         Reason:    {item.get('reason')}")
            print(f"         Next Step: {item.get('next_step')}")
            if item.get("file_path"):
                print(f"         MP4:       {item.get('file_path')}")

    print(f"\nExported Videos ({len(res.exported_videos)}):")
    for probe in res.exported_videos:
        status_str = "VALID DEC" if probe.get("decodable") else "CORRUPTED"
        print(f"  [{status_str}] {probe.get('file_path')}")
        print(f"           Resolution: {probe.get('width')}x{probe.get('height')} @ {probe.get('fps')}fps, Frames: {probe.get('frame_count')}")
        print(f"           SHA256:     {probe.get('sha256')}")

    record = ScheduleRecord(
        date=args.date or today_bangkok_str(),
        csv_sha256=res.spark_csv_sha256,
        run_id=res.run_id,
        status=res.status,
        timestamp=now_bangkok().isoformat(),
        mp4_count=len(res.exported_videos),
        duration_seconds=res.duration_seconds,
        error=res.error,
        provider_mode=res.provider_mode,
    )
    record_run(record)
    report_configured_run(res, db_path=args.db_path or VF_DB_PATH)

    if res.status not in ("success", "skipped"):
        try:
            from automation.doctor_agent import DoctorAgent
            DoctorAgent().handle_pipeline_incident(res)
        except Exception as doctor_exc:
            pass

    if args.json:
        print("\n" + json.dumps(asdict(res), indent=2, ensure_ascii=False))

    return 0 if res.status == "success" else 1


def cmd_resume(args: argparse.Namespace) -> int:
    """Resume an existing or interrupted run."""
    print(f"=== Resuming Workflow: {args.run_id} ===")
    res = run_pipeline(
        spark_csv=args.csv,
        limit=args.limit,
        scenes=args.scenes,
        date=args.date,
        provider_mode=args.provider_mode,
        run_id=args.run_id,
        profile_path=args.profile,
        db_path=args.db_path,
        offline_intake=not getattr(args, "live_intake", False),
        qc_level=args.qc_level,
    )
    report_configured_run(res, db_path=args.db_path or VF_DB_PATH)

    if res.status not in ("success", "skipped"):
        try:
            from automation.doctor_agent import DoctorAgent
            DoctorAgent().handle_pipeline_incident(res)
        except Exception as doctor_exc:
            pass
    print(f"Status: {res.status}")
    if res.error:
        print(f"Note/Error: {res.error}")

    if res.product_outcomes:
        print(f"\nProduct Outcomes ({len(res.product_outcomes)}):")
        for item in res.product_outcomes:
            st = item.get("status", "unknown").upper()
            print(f"  [{st}] {item.get('product_name')}")
            print(f"         Stage:     {item.get('stage')}")
            print(f"         Reason:    {item.get('reason')}")
            print(f"         Next Step: {item.get('next_step')}")
            if item.get("file_path"):
                print(f"         MP4:       {item.get('file_path')}")

    print(f"\nExported Videos ({len(res.exported_videos)}):")
    for probe in res.exported_videos:
        status_str = "VALID DEC" if probe.get("decodable") else "CORRUPTED"
        print(f"  [{status_str}] {probe.get('file_path')}")
        print(f"           Resolution: {probe.get('width')}x{probe.get('height')} @ {probe.get('fps')}fps, Frames: {probe.get('frame_count')}")
        print(f"           SHA256:     {probe.get('sha256')}")

    return 0 if res.status == "success" else 1


def cmd_status(args: argparse.Namespace) -> int:
    """Display overall status of automation pipeline, ledger, and database."""
    print("=== PI/VF Automation Status ===")
    print(f"Current Time:  {now_bangkok().isoformat()} (Asia/Bangkok)")
    print(f"Inbox Root:    {INBOX_ROOT}")
    print(f"Database:      {VF_DB_PATH}")

    # Ledger history
    ledger = load_ledger()
    print(f"\nScheduler Ledger ({len(ledger)} entries):")
    for r in ledger[-5:]:
        print(f"  [{r.get('date')}] Run: {r.get('run_id')} | Status: {r.get('status')} | MP4s: {r.get('mp4_count')} | Duration: {r.get('duration_seconds')}s")

    # Inbox batches
    print("\nExchange Inbox Batches:")
    if INBOX_ROOT.is_dir():
        for date_dir in sorted(INBOX_ROOT.iterdir(), reverse=True)[:3]:
            if date_dir.is_dir():
                batches = [b.name for b in date_dir.iterdir() if b.is_dir()]
                print(f"  Date {date_dir.name}: {len(batches)} batch(es) -> {', '.join(batches[:3])}")
    else:
        print("  (Inbox directory not found)")

    # SQLite workflows
    if VF_DB_PATH.is_file():
        try:
            conn = sqlite3.connect(str(VF_DB_PATH))
            rows = conn.execute(
                "SELECT workflow_id, aggregate_json, version FROM workflow LIMIT 10"
            ).fetchall()
            print(f"\nWorkflows in {VF_DB_PATH.name} ({len(rows)}):")
            for wid, agg_str, ver in rows:
                try:
                    state = json.loads(agg_str).get("state", "unknown")
                except Exception:
                    state = "raw"
                print(f"  {wid[:45]}... | State: {state} | Version: {ver}")
            conn.close()
        except Exception as exc:
            print(f"  DB inspect error: {exc}")

    return 0


def cmd_find_mp4(args: argparse.Namespace) -> int:
    """Find, probe, and verify all generated MP4 files."""
    print(f"=== Searching for MP4 artifacts ===")
    roots = [
        VF_EXPORTS_DIR,
        RUNTIME_DIR,
    ]
    if args.dir:
        roots = [Path(args.dir)]

    found = []
    seen = set()
    for root in roots:
        if root.is_dir():
            for p in root.glob("**/*.mp4"):
                if str(p) not in seen:
                    seen.add(str(p))
                    probe = probe_mp4_file(p)
                    found.append(probe)

    print(f"Total MP4 files discovered: {len(found)}\n")
    for p in found:
        dec_tag = "VALID DEC" if p.decodable else f"ERROR: {p.error}"
        print(f"File:       {p.file_path}")
        print(f"Size:       {p.size_bytes:,} bytes")
        print(f"Specs:      {p.width}x{p.height} @ {p.fps}fps, {p.frame_count} frames probed")
        print(f"Integrity:  [{dec_tag}]")
        print(f"SHA256:     {p.sha256}")
        print("-" * 50)

    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Run diagnostics on virtual environments, dependencies, and storage."""
    print("=== PI/VF Platform Doctor ===")
    issues = 0

    # 1. Check Python Venvs
    print("\n[1] Python Environments:")
    for name, py_path in [("PI VirtualEnv", PI_PYTHON), ("VF VirtualEnv", VF_PYTHON)]:
        if py_path.is_file():
            print(f"  [OK] {name}: {py_path}")
        else:
            print(f"  [FAIL] {name} NOT found at: {py_path}")
            issues += 1

    # 2. Check Key Dependencies
    print("\n[2] Key Dependencies Check:")
    import subprocess
    for name, py_path, module in [
        ("PI PIL", PI_PYTHON, "PIL"),
        ("PI Pydantic", PI_PYTHON, "pydantic"),
        ("VF PyAV", VF_PYTHON, "av"),
        ("VF NumPy", VF_PYTHON, "numpy"),
        ("VF VideoFactory", VF_PYTHON, "video_factory"),
    ]:
        proc = subprocess.run([str(py_path), "-c", f"import {module}; print({module}.__name__)"], capture_output=True, text=True)
        if proc.returncode == 0:
            print(f"  [OK] {name} available")
        else:
            print(f"  [FAIL] {name} import failed: {proc.stderr.strip() or proc.stdout.strip()}")
            issues += 1

    # 3. Check Files and Folders
    print("\n[3] Storage & Required Files:")
    print("  Input source: two configured Google Sheets")

    prof_p = Path(DEFAULT_PROFILE)
    if prof_p.is_file():
        print(f"  [OK] Creative Profile: {prof_p}")
    else:
        print(f"  [FAIL] Creative Profile missing: {prof_p}")
        issues += 1

    # 4. Check Disk Space
    print("\n[4] Disk Space:")
    total, used, free = shutil.disk_usage("D:\\")
    free_gb = free // (2**30)
    print(f"  [OK] Drive D: {free_gb} GB free")

    # 5. Check Timezone
    print("\n[5] Timezone:")
    print(f"  [OK] Asia/Bangkok Current Time: {now_bangkok().strftime('%Y-%m-%d %H:%M:%S %Z')}")

    # 6. Pipeline Health & Workflows
    from automation.doctor_agent import DoctorAgent
    doctor = DoctorAgent()
    target_date = getattr(args, 'date', None)
    diag = doctor.diagnose(day=target_date)

    print("\n[6] Pipeline Health & Workflows:")
    print(f"  Target Date:     {diag.date}")
    print(f"  Status:          {'HEALTHY' if diag.healthy else 'ATTENTION REQUIRED'}")
    print(f"  MP4s Completed:  {diag.mp4_count_today}")
    if diag.findings:
        print(f"  Findings:        {len(diag.findings)}")
        for f in diag.findings:
            icon = {'CRITICAL': '⛔', 'ERROR': '❌', 'WARNING': '⚠️', 'INFO': 'ℹ️'}.get(f.severity, '•')
            print(f"    {icon} [{f.severity}] [{f.component}] {f.title}")
            if f.action_suggested:
                print(f"        -> Action: {f.action_suggested}")

    if getattr(args, 'fix', False):
        print("\n=== Executing Doctor Auto-Repair ===")
        fixes = doctor.auto_repair(diag)
        if fixes:
            for fix in fixes:
                print(f"  [FIXED] {fix}")
        else:
            print("  Không có lỗi tự động sửa nào cần can thiệp.")

    if getattr(args, 'notify', False):
        print("\n=== Sending Telegram Alert ===")
        doctor.send_telegram_alert(
            diag,
            custom_message=getattr(args, 'message', None),
            attach_report=getattr(args, 'attach_report', False),
        )

    print("\n" + "=" * 30)
    if issues == 0 and diag.healthy:
        print("Doctor Verdict: ALL CHECKS PASSED (Ready for production & offline verification)")
        return 0
    elif issues == 0 and not diag.healthy:
        print(f"Doctor Verdict: ENVIRONMENT OK, BUT PIPELINE HAS {len([f for f in diag.findings if f.severity in ('ERROR', 'CRITICAL')])} BLOCKING ISSUE(S)")
        return 1
    else:
        print(f"Doctor Verdict: {issues} ENVIRONMENT ISSUE(S) DETECTED")
        return 1


def cmd_trigger(args: argparse.Namespace) -> int:
    """Trigger daily scheduler check or daemon."""
    limit = args.limit if args.limit is not None else (DAILY_VIDEO_LIMIT if args.csv is None else 1)
    if args.daemon:
        run_scheduler_daemon(
            spark_csv=args.csv,
            limit=limit,
            scenes=args.scenes,
            provider_mode=args.provider_mode,
            qc_level=args.qc_level,
            acquire=args.acquire,
            on_result=report_configured_run,
        )
        return 0

    res = trigger_daily_run(
        spark_csv=args.csv,
        limit=limit,
        scenes=args.scenes,
        provider_mode=args.provider_mode,
        force=args.force,
        date=args.date,
        offline_intake=not args.live_intake if getattr(args, "live_intake", False) else None,
        qc_level=args.qc_level,
        acquire=args.acquire,
    )
    if isinstance(res, dict) and res.get("status") == "skipped":
        print(f"[SKIPPED] {res.get('reason')}")
        return 0
    report_configured_run(res)
    status = getattr(res, "status", "unknown")
    print(f"Daily Run Result: {status}")
    if getattr(res, "error", None):
        print(res.error)
    return 0 if status in ("success", "skipped") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pi-vf-automation",
        description="Unified PI/VF Product Automation Platform CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # run
    p_run = subparsers.add_parser("run", help="Run end-to-end pipeline from Spark CSV to MP4")
    p_run.add_argument("--csv", type=Path, help="Explicit CSV override for offline verification")
    p_run.add_argument("--limit", type=int, default=1, help="Number of products to intake and produce")
    p_run.add_argument("--scenes", type=int, choices=(3, 4, 5), default=4, help="Target scene count (3, 4, 5; default 4)")
    p_run.add_argument("--date", help="Production date YYYY-MM-DD (defaults to Bangkok today)")
    p_run.add_argument("--provider-mode", choices=("fake", "configured"), help="Defaults to configured for Google Sheets")
    p_run.add_argument("--run-id", help="Explicit run ID")
    p_run.add_argument("--profile", type=Path, help="Custom creative profile JSON")
    p_run.add_argument("--qc-level", choices=("low", "middle", "high"), default="high")
    p_run.add_argument("--db-path", type=Path, help="Hermes SQLite DB path")
    p_run.add_argument("--live-intake", action="store_true", help="Use live crawl rather than offline intake adapter")
    p_run.add_argument("--json", action="store_true", help="Output result as JSON")
    p_run.set_defaults(func=cmd_run)

    # resume
    p_resume = subparsers.add_parser("resume", help="Resume an existing or interrupted run")
    p_resume.add_argument("--run-id", required=True, help="Run ID to resume")
    p_resume.add_argument("--csv", type=Path, help="Explicit CSV override for offline verification")
    p_resume.add_argument("--limit", type=int, default=1)
    p_resume.add_argument("--scenes", type=int, choices=(3, 4, 5), default=4)
    p_resume.add_argument("--date")
    p_resume.add_argument("--provider-mode", choices=("fake", "configured"))
    p_resume.add_argument("--profile", type=Path)
    p_resume.add_argument("--qc-level", choices=("low", "middle", "high"),
        help="Defaults to the saved run policy")
    p_resume.add_argument("--db-path", type=Path)
    p_resume.add_argument("--live-intake", action="store_true", help="Use live crawl rather than offline fixture adapter")
    p_resume.set_defaults(func=cmd_resume)

    # status
    p_status = subparsers.add_parser("status", help="Show system status and recent runs")
    p_status.set_defaults(func=cmd_status)

    # find-mp4
    p_find = subparsers.add_parser("find-mp4", help="Find and probe generated MP4 files")
    p_find.add_argument("--dir", help="Directory to scan")
    p_find.set_defaults(func=cmd_find_mp4)

    # doctor
    p_doctor = subparsers.add_parser("doctor", help="Run environment and system diagnostics, auto-repair and notify")
    p_doctor.add_argument("--fix", action="store_true", help="Auto-repair fixable pipeline issues")
    p_doctor.add_argument("--notify", action="store_true", help="Send doctor report to Telegram")
    p_doctor.add_argument("--attach-report", action="store_true", help="Attach daily report JSON to Telegram")
    p_doctor.add_argument("--message", help="Custom note to include in Telegram alert")
    p_doctor.add_argument("--date", help="Target date YYYY-MM-DD (defaults to today)")
    p_doctor.set_defaults(func=cmd_doctor)

    # trigger
    p_trigger = subparsers.add_parser("trigger", help="Trigger daily scheduled run or run scheduler daemon")
    p_trigger.add_argument("--csv", type=Path, help="Explicit CSV override for offline verification")
    p_trigger.add_argument("--limit", type=int,
                           help=f"Maximum products for this run (Sheets default {DAILY_VIDEO_LIMIT}, CSV default 1)")
    p_trigger.add_argument("--scenes", type=int, choices=(3, 4, 5), default=4)
    p_trigger.add_argument("--date")
    p_trigger.add_argument("--provider-mode", choices=("fake", "configured"))
    p_trigger.add_argument("--qc-level", choices=("low", "middle", "high"), default="low")
    p_trigger.add_argument("--acquire", action="store_true", help="Use Spaiauto to collect and review the selected Sheet product")
    p_trigger.add_argument("--force", action="store_true", help="Force execution even if already run today")
    p_trigger.add_argument("--daemon", action="store_true", help="Run foreground scheduler daemon")
    p_trigger.add_argument("--live-intake", action="store_true", help="Use live crawl rather than offline intake adapter")
    p_trigger.set_defaults(func=cmd_trigger)

    from automation.human_review import VERDICT_CAUSES, cmd_compare_qc, cmd_human_verdict

    p_verdict = subparsers.add_parser("human-verdict", help="Record a human review beside an export")
    p_verdict.add_argument("export_dir", type=Path)
    p_verdict.add_argument("--verdict", required=True, choices=("approve", "reject"))
    p_verdict.add_argument("--cause", required=True, choices=VERDICT_CAUSES)
    p_verdict.add_argument("--reason", required=True)
    p_verdict.set_defaults(func=cmd_human_verdict)

    p_compare = subparsers.add_parser("compare-qc", help="Compare export QC and human verdicts")
    p_compare.add_argument("--exports-root", type=Path, default=VF_EXPORTS_DIR)
    p_compare.add_argument("--db-path", type=Path, default=VF_DB_PATH)
    p_compare.add_argument("--threshold", type=int, default=80, help="Preproduction QC threshold (0–100, default 80)")
    p_compare.add_argument("--rubric-version", choices=("v1", "v2", "unknown"), help="Filter rows by rubric version")
    p_compare.set_defaults(func=cmd_compare_qc)

    args = parser.parse_args(argv)
    if args.func is cmd_compare_qc and not 0 <= args.threshold <= 100:
        p_compare.error("--threshold must be between 0 and 100")
    if hasattr(args, "provider_mode") and args.provider_mode is None:
        args.provider_mode = "configured" if args.csv is None else "fake"
    try:
        return args.func(args)
    except (CorruptLedgerError, LedgerLockTimeout) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
