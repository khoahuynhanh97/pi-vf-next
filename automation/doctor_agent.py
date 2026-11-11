"""PI/VF Doctor Agent: Automated pipeline incident response, diagnosis, self-healing, and Telegram reporting with logs."""

from __future__ import annotations

import argparse
import fnmatch
import glob
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import httpx

from automation.config import (
    BANGKOK_TZ,
    DEFAULT_PROFILE,
    INBOX_ROOT,
    PI_DIR,
    PI_PYTHON,
    PI_RUNS_DIR,
    PI_SHEET_ROOT,
    ROOT_DIR,
    RUNTIME_DIR,
    VF_DB_PATH,
    VF_DIR,
    VF_EXPORTS_DIR,
    VF_PYTHON,
    VF_WORKSPACE_DIR,
    now_bangkok,
    today_bangkok_str,
)

logger = logging.getLogger("doctor_agent")


@dataclass
class DiagnosticFinding:
    severity: str  # "INFO", "WARNING", "ERROR", "CRITICAL"
    component: str  # "ENV", "PI", "EXCHANGE", "VF", "PROVIDER", "LOCK"
    title: str
    details: str
    fixable: bool = False
    action_suggested: str = ""
    repaired: bool = False


@dataclass
class DoctorDiagnosisReport:
    timestamp: str
    date: str
    healthy: bool
    mp4_count_today: int
    findings: list[DiagnosticFinding] = field(default_factory=list)
    fixes_applied: list[str] = field(default_factory=list)
    pending_human_actions: list[str] = field(default_factory=list)


@dataclass
class IncidentClassification:
    category: str  # "AUTO_FIXED", "RETRYABLE", "CRITICAL_NEED_HUMAN"
    component: str  # "LOCK", "PROVIDER", "PI", "VF", "ENV"
    title: str
    details: str
    action_taken: str = ""
    suggested_action: str = ""


class DoctorAgent:
    """Intelligent Pipeline Doctor for PI/VF Automation."""

    def __init__(
        self,
        *,
        db_path: Path = VF_DB_PATH,
        inbox_root: Path = INBOX_ROOT,
        reports_dir: Path = RUNTIME_DIR / "reports",
        workspace_dir: Path = VF_WORKSPACE_DIR,
    ):
        self.db_path = Path(db_path).resolve()
        self.inbox_root = Path(inbox_root).resolve()
        self.reports_dir = Path(reports_dir).resolve()
        self.workspace_dir = Path(workspace_dir).resolve()
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    def get_telegram_creds(self) -> tuple[str, str]:
        """Resolve Telegram Bot Token and Chat ID from environment or .env files."""
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

        env_files = [
            ROOT_DIR / ".env",
            PI_DIR / ".env",
            VF_DIR / ".env",
            Path("d:/work/Personal/Automation/docs/config/.env"),
        ]

        if not (token and chat_id):
            for env_path in env_files:
                if env_path.is_file():
                    try:
                        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
                            line = line.strip()
                            if line.startswith("#") or not line:
                                continue
                            if "=" in line:
                                k, v = line.split("=", 1)
                                k = k.strip()
                                v = v.strip().strip("'\"")
                                if not token and k in ("TELEGRAM_BOT_TOKEN", "PRODUCT_SCOUT_TELEGRAM_BOT_TOKEN"):
                                    token = v
                                if not chat_id and k in ("TELEGRAM_CHAT_ID", "PRODUCT_SCOUT_TELEGRAM_OWNER_ID", "TELEGRAM_USER_ID"):
                                    chat_id = v
                    except Exception as e:
                        logger.warning("Could not read %s: %s", env_path, e)

        return token, chat_id

    def clean_stale_locks(self, max_age_seconds: int = 300) -> list[str]:
        """Remove dangling file-existence lock files older than threshold, preserving OS-level locks."""
        removed: list[str] = []
        now_ts = time.time()
        os_lock_patterns = ("*.json.lock", "sheet-*.lock", "scheduler_ledger.json.lock")
        lock_patterns = [
            RUNTIME_DIR / "vf" / "*.lock",
            RUNTIME_DIR / "vf" / ".*.lock",
            PI_RUNS_DIR / "**" / "*.lock",
        ]
        for pat in lock_patterns:
            for lf in glob.glob(str(pat), recursive=True):
                lp = Path(lf)
                name = lp.name
                if any(fnmatch.fnmatch(name, p) for p in os_lock_patterns):
                    # Preserve OS file locks managed by exclusive_file_lock
                    continue
                try:
                    if now_ts - lp.stat().st_mtime > max_age_seconds:
                        lp.unlink(missing_ok=True)
                        removed.append(f"Đã gỡ lock treo: {lp.name}")
                except OSError:
                    pass
        return removed

    def heal_database_leases(self) -> list[str]:
        """Clear expired leases in hermes.db so stalled items can be picked up."""
        actions: list[str] = []
        if not self.db_path.is_file():
            return actions

        try:
            conn = sqlite3.connect(str(self.db_path))
            c = conn.cursor()
            tables = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

            if "workflow_outbox" in tables:
                now_utc = datetime.now(timezone.utc).isoformat()
                c.execute(
                    "UPDATE workflow_outbox SET lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL "
                    "WHERE state = 'pending' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
                    (now_utc,)
                )
                if c.rowcount > 0:
                    actions.append(f"Đã giải phóng {c.rowcount} outbox lease bị hết hạn trong hermes.db")

            if "video_factory_keyframe_operation" in tables:
                c.execute(
                    "UPDATE video_factory_keyframe_operation SET status = 'unknown' "
                    "WHERE status = 'failed' AND error_code IN ('KEYFRAME_TIMEOUT_EXPIRED', 'GOOGLE_FLOW_KEYFRAME_TIMEOUT') "
                    "AND COALESCE(provider_operation_id, '') != ''"
                )
                if c.rowcount > 0:
                    actions.append(f"Đã chuyển {c.rowcount} keyframe timeout về trạng thái 'unknown' để đối soát/cứu tự động")

            conn.commit()
            conn.close()
        except Exception as exc:
            logger.error("Database healing error: %s", exc)

        return actions

    def diagnose(self, day: str | None = None) -> DoctorDiagnosisReport:
        """Perform comprehensive system diagnostics."""
        target_day = day or today_bangkok_str()
        findings: list[DiagnosticFinding] = []

        # 1. Environment Diagnostics
        if not PI_PYTHON.is_file():
            findings.append(DiagnosticFinding("CRITICAL", "ENV", "Missing PI VirtualEnv", f"Path not found: {PI_PYTHON}"))
        if not VF_PYTHON.is_file():
            findings.append(DiagnosticFinding("CRITICAL", "ENV", "Missing VF VirtualEnv", f"Path not found: {VF_PYTHON}"))

        # Disk space
        try:
            _, _, free = shutil.disk_usage(str(ROOT_DIR))
            free_gb = free // (2**30)
            if free_gb < 10:
                findings.append(DiagnosticFinding("WARNING", "ENV", "Low Disk Space", f"Only {free_gb} GB free on workspace drive", False, "Clean old render artifacts"))
        except Exception:
            pass

        # 2. Dangling locks
        lock_patterns = [
            RUNTIME_DIR / "vf" / "*.lock",
            RUNTIME_DIR / "vf" / ".*.lock",
            PI_RUNS_DIR / "**" / "*.lock",
        ]
        now_ts = time.time()
        for pat in lock_patterns:
            for lock_file in glob.glob(str(pat), recursive=True):
                lp = Path(lock_file)
                try:
                    age_seconds = now_ts - lp.stat().st_mtime
                    if age_seconds > 300:
                        findings.append(DiagnosticFinding(
                            "WARNING", "LOCK", f"Stale lock file detected: {lp.name}",
                            f"Lock exists for {int(age_seconds)}s at {lp}",
                            fixable=True,
                            action_suggested=f"Remove dangling lock file {lp.name}"
                        ))
                except OSError:
                    pass

        # 3. Check Today's Run Report & MP4 Count
        report_path = self.reports_dir / f"{target_day}.json"
        mp4_count = 0
        if report_path.is_file():
            try:
                rep_data = json.loads(report_path.read_text(encoding="utf-8"))
                for run_id, rinfo in rep_data.get("runs", {}).items():
                    for p in rinfo.get("products", []):
                        stages = p.get("stages", {})
                        if stages.get("EXPORT", {}).get("status") == "completed":
                            mp4_count += 1

                        kf_stage = stages.get("KEYFRAME_GENERATION", {})
                        if kf_stage.get("status") == "pending_operator" or kf_stage.get("error_code") == "RECONCILIATION_REQUIRED":
                            findings.append(DiagnosticFinding(
                                "ERROR", "PROVIDER",
                                f"Keyframe timeout for {p.get('row_id')}: {p.get('name')}",
                                f"Message: {kf_stage.get('message')}",
                                fixable=True,
                                action_suggested="Reconcile Flow status or reset lease for retry"
                            ))
                        elif kf_stage.get("status") == "failed":
                            findings.append(DiagnosticFinding(
                                "ERROR", "PROVIDER",
                                f"Keyframe failure for {p.get('row_id')}: {p.get('name')}",
                                f"Reason: {kf_stage.get('message') or p.get('reason')}",
                                fixable=True,
                                action_suggested="Retry when provider quota resets"
                            ))

                        if stages.get("PI_ACQUISITION", {}).get("status") == "skipped":
                            findings.append(DiagnosticFinding(
                                "INFO", "PI",
                                f"PI Skipped {p.get('row_id')}: {p.get('name')}",
                                f"Reason: {p.get('reason')}",
                                fixable=False,
                                action_suggested="Operator manual review required for image/pricing"
                            ))
            except Exception as exc:
                findings.append(DiagnosticFinding("ERROR", "REPORT", "Corrupt daily report", str(exc)))

        # 4. Check Database Stuck Workflows in hermes.db
        if self.db_path.is_file():
            try:
                conn = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row
                c = conn.cursor()

                c.execute("SELECT workflow_id, aggregate_json FROM workflow WHERE aggregate_json LIKE '%producing%' OR aggregate_json LIKE '%creating_storyboard%'")
                for r in c.fetchall():
                    w_id = r["workflow_id"]
                    try:
                        agg = json.loads(r["aggregate_json"])
                        state = agg.get("state")
                        prod_name = (agg.get("source_context") or {}).get("name") or w_id
                        findings.append(DiagnosticFinding(
                            "WARNING", "VF",
                            f"Workflow in progress or stalled: {w_id}",
                            f"Product: {prod_name} | State: {state}",
                            fixable=True,
                            action_suggested="Inspect lease or resume workflow"
                        ))
                    except Exception:
                        pass
                conn.close()
            except Exception as exc:
                findings.append(DiagnosticFinding("ERROR", "VF", "Error reading hermes.db", str(exc)))

        healthy = not any(f.severity in ("CRITICAL", "ERROR") for f in findings)
        return DoctorDiagnosisReport(
            timestamp=now_bangkok().isoformat(),
            date=target_day,
            healthy=healthy,
            mp4_count_today=mp4_count,
            findings=findings,
        )

    def auto_repair(self, diagnosis: DoctorDiagnosisReport | None = None) -> list[str]:
        """Apply automatic repairs for fixable findings."""
        fixes = self.clean_stale_locks()
        db_fixes = self.heal_database_leases()
        fixes.extend(db_fixes)
        if diagnosis:
            diagnosis.fixes_applied.extend(fixes)
        return fixes

    def send_telegram_alert(
        self,
        diagnosis: DoctorDiagnosisReport,
        custom_message: str | None = None,
        attach_report: bool = False,
    ) -> bool:
        """Send standard Doctor Report to Telegram."""
        token, chat_id = self.get_telegram_creds()
        if not (token and chat_id):
            print("[Doctor] Chưa cấu hình TELEGRAM_BOT_TOKEN hoặc TELEGRAM_CHAT_ID trong .env")
            return False

        now_str = now_bangkok().strftime("%H:%M:%S %d/%m/%Y")
        status_icon = "✅" if diagnosis.healthy else "⚠️"

        lines = [
            f"{status_icon} <b>PI/VF DOCTOR REPORT | {diagnosis.date}</b>",
            f"🕒 <i>Thời gian: {now_str} (Asia/Bangkok)</i>",
            f"🎬 <b>Video MP4 thành công hôm nay: {diagnosis.mp4_count_today}</b>",
            "",
        ]

        if custom_message:
            lines.append(f"💬 <b>Ghi chú:</b> {custom_message}")
            lines.append("")

        crit_errors = [f for f in diagnosis.findings if f.severity in ("CRITICAL", "ERROR")]
        warnings = [f for f in diagnosis.findings if f.severity == "WARNING"]
        infos = [f for f in diagnosis.findings if f.severity == "INFO"]

        if crit_errors:
            lines.append(f"🚨 <b>Lỗi phát hiện ({len(crit_errors)}):</b>")
            for f in crit_errors[:5]:
                lines.append(f"  • [<b>{f.component}</b>] {f.title}")
                if f.action_suggested:
                    lines.append(f"    ↪ <i>Khắc phục: {f.action_suggested}</i>")
            lines.append("")

        if diagnosis.fixes_applied:
            lines.append(f"🛠 <b>Đã tự động sửa ({len(diagnosis.fixes_applied)}):</b>")
            for fix in diagnosis.fixes_applied:
                lines.append(f"  ✓ {fix}")
            lines.append("")

        if warnings:
            lines.append(f"⚠️ <b>Cảnh báo ({len(warnings)}):</b>")
            for f in warnings[:3]:
                lines.append(f"  • {f.title}")
            lines.append("")

        pi_skips = [f for f in infos if f.component == "PI"]
        if pi_skips:
            lines.append(f"⏸ <b>Cần duyệt thủ công:</b> {len(pi_skips)} sản phẩm")

        lines.append("")
        lines.append("<i>Gõ <code>.\\Run.ps1 doctor --fix</code> để kiểm tra & khắc phục.</i>")

        full_text = "\n".join(lines)
        url = f"https://api.telegram.org/bot{token}/sendMessage"

        try:
            resp = httpx.post(url, json={
                "chat_id": chat_id,
                "text": full_text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=12)
            if resp.status_code == 200:
                print(f"[Doctor] Đã gửi báo cáo Telegram thành công tới Chat ID {chat_id}")
            return True
        except Exception as exc:
            print(f"[Doctor] Lỗi kết nối Telegram API: {exc}")
            return False

    def handle_pipeline_incident(
        self,
        run_res: Any | None = None,
        *,
        error_msg: str | None = None,
        exc: Exception | None = None,
        log_text: str | None = None,
    ) -> Path:
        """Central incident handler called when pipeline blocks or fails."""
        now = now_bangkok()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        date_str = now.strftime("%Y-%m-%d")
        ts_id = now.strftime("%Y%m%d_%H%M%S")

        run_id = getattr(run_res, "run_id", "unknown_run")
        status = getattr(run_res, "status", "failed")
        primary_error = error_msg or getattr(run_res, "error", "") or (str(exc) if exc else "Pipeline blockage")

        incident_log_file = self.reports_dir / f"incident_{run_id}_{ts_id}.log"
        log_lines: list[str] = [
            f"=== PI/VF INCIDENT REPORT ===",
            f"Timestamp:   {now_str} (Asia/Bangkok)",
            f"Run ID:      {run_id}",
            f"Status:      {status}",
            f"Primary Err: {primary_error}",
            f"=============================",
            "",
        ]

        if exc:
            log_lines.append("Traceback:")
            log_lines.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            log_lines.append("")

        classifications: list[IncidentClassification] = []

        # 1. Check and Auto-heal locks
        stale_locks = self.clean_stale_locks()
        for lock_msg in stale_locks:
            classifications.append(IncidentClassification(
                category="AUTO_FIXED",
                component="LOCK",
                title="Stale Lock Cleared",
                details=lock_msg,
                action_taken=lock_msg,
            ))
            log_lines.append(f"[AUTO-FIX] {lock_msg}")

        # 2. Check and Auto-heal DB leases
        db_fixes = self.heal_database_leases()
        for df in db_fixes:
            classifications.append(IncidentClassification(
                category="AUTO_FIXED",
                component="VF",
                title="Database Lease Recovery",
                details=df,
                action_taken=df,
            ))
            log_lines.append(f"[AUTO-FIX] {df}")

        # 3. Analyze products from run_res or today's report
        products = getattr(run_res, "product_outcomes", [])
        if not products:
            today_rep = self.reports_dir / f"{date_str}.json"
            if today_rep.is_file():
                try:
                    rep_json = json.loads(today_rep.read_text(encoding="utf-8"))
                    for r_id, r_info in rep_json.get("runs", {}).items():
                        if r_id.endswith(run_id) or run_id in r_id:
                            products = r_info.get("products", [])
                            break
                except Exception:
                    pass

        mp4_count = len(getattr(run_res, "exported_videos", []))

        for p in products:
            row_id = p.get("row_id") or p.get("product_key") or "product"
            name = p.get("name") or p.get("product_name") or row_id
            stages = p.get("stages", {})
            reason = p.get("reason") or ""
            reason_code = p.get("reason_code") or ""
            kf_stage = stages.get("KEYFRAME_GENERATION", {})
            pi_stage = stages.get("PI_ACQUISITION", {})
            exp_stage = stages.get("EXPORT", {})

            if exp_stage.get("status") == "completed":
                continue

            kf_msg = str(kf_stage.get("message") or "")
            if (
                "TIMEOUT" in kf_msg or "timeout" in kf_msg
                or "capacity is full" in reason or "capacity is full" in kf_msg
                or "RECONCILIATION_REQUIRED" in str(kf_stage.get("error_code"))
                or "429" in reason or "RATE_LIMITED" in reason
            ):
                classifications.append(IncidentClassification(
                    category="RETRYABLE",
                    component="PROVIDER",
                    title=f"Provider nghẽn tải: {name[:35]}...",
                    details=f"[{row_id}] {kf_msg or reason}",
                    suggested_action="Dịch vụ AI bên ngoài (Google Flow/Render) đang timeout hoặc hết quota tạm thời. Cần đợi cooldown và retry.",
                ))
                log_lines.append(f"[RETRYABLE] {row_id} - {name}: {kf_msg or reason}")

            elif pi_stage.get("status") == "skipped" or "identity_unverified" in reason_code or "Operator review" in reason:
                classifications.append(IncidentClassification(
                    category="CRITICAL_NEED_HUMAN",
                    component="PI",
                    title=f"Kiểm duyệt chưa đạt: {name[:35]}...",
                    details=f"[{row_id}] {reason}",
                    suggested_action="Ảnh không khớp listing hoặc mập mờ phân loại giá. Cần người vận hành rà soát.",
                ))
                log_lines.append(f"[CRITICAL_OPERATOR] {row_id} - {name}: {reason}")

            elif p.get("status") in ("failed", "skipped") and reason:
                classifications.append(IncidentClassification(
                    category="CRITICAL_NEED_HUMAN",
                    component="VF",
                    title=f"Lỗi tại VF: {name[:35]}...",
                    details=f"[{row_id}] {reason}",
                    suggested_action="Kiểm tra chi tiết trong hermes.db hoặc file log đính kèm.",
                ))
                log_lines.append(f"[CRITICAL] {row_id} - {name}: {reason}")

        # Append recent runtime log if available
        vf_log = RUNTIME_DIR / "vf" / "logs" / "mcp-stderr.log"
        if vf_log.is_file():
            try:
                recent_vf_lines = vf_log.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
                log_lines.append("\n--- Recent MCP STDERR LOGS ---")
                log_lines.extend(recent_vf_lines)
            except Exception:
                pass

        if log_text:
            log_lines.append("\n--- Direct Log Output ---")
            log_lines.append(log_text)

        incident_log_file.write_text("\n".join(log_lines), encoding="utf-8")
        print(f"[Doctor] Đã ghi nhận log sự cố tại: {incident_log_file}")

        self._send_incident_telegram(
            run_id=run_id,
            status=status,
            mp4_count=mp4_count,
            primary_error=primary_error,
            classifications=classifications,
            log_file=incident_log_file,
            log_snippet="\n".join(log_lines[-20:]),
        )

        return incident_log_file

    def _send_incident_telegram(
        self,
        run_id: str,
        status: str,
        mp4_count: int,
        primary_error: str,
        classifications: list[IncidentClassification],
        log_file: Path,
        log_snippet: str,
    ) -> bool:
        token, chat_id = self.get_telegram_creds()
        if not (token and chat_id):
            print("[Doctor] Chưa cấu hình TELEGRAM_BOT_TOKEN hoặc TELEGRAM_CHAT_ID trong .env. Bỏ qua gửi tin.")
            return False

        now_str = now_bangkok().strftime("%H:%M:%S %d/%m/%Y")

        auto_fixed = [c for c in classifications if c.category == "AUTO_FIXED"]
        retryable = [c for c in classifications if c.category == "RETRYABLE"]
        critical = [c for c in classifications if c.category == "CRITICAL_NEED_HUMAN"]

        header_icon = "🚨" if critical else ("⚠️" if retryable else "ℹ️")

        lines = [
            f"{header_icon} <b>PI/VF DOCTOR: SỰ CỐ NGHẼN PIPELINE</b>",
            f"🕒 <i>Thời gian: {now_str} (Asia/Bangkok)</i>",
            f"🆔 <code>{run_id}</code> | Trạng thái: <b>{status.upper()}</b>",
            f"🎬 <b>Video MP4 hoàn tất: {mp4_count}</b>",
            f"❌ <b>Lỗi chính:</b> <code>{primary_error[:120]}</code>",
            "",
        ]

        if auto_fixed:
            lines.append(f"🛠 <b>ĐÃ TỰ ĐỘNG SỬA ({len(auto_fixed)}):</b>")
            for item in auto_fixed[:3]:
                lines.append(f"  ✓ {item.action_taken}")
            lines.append("")

        if retryable:
            lines.append(f"🔄 <b>CÓ THỂ RETRY / NGHẼN TẠM THỜI ({len(retryable)}):</b>")
            for item in retryable[:3]:
                lines.append(f"  • <b>{item.title}</b>")
                lines.append(f"    ↪ <i>{item.suggested_action}</i>")
            lines.append("")

        if critical:
            lines.append(f"⛔ <b>LỖI NGHIÊM TRỌNG CẦN CAN THIỆP ({len(critical)}):</b>")
            for item in critical[:4]:
                lines.append(f"  • <b>{item.title}</b>")
                lines.append(f"    Chi tiết: <i>{item.details[:100]}</i>")
                if item.suggested_action:
                    lines.append(f"    ↪ Hướng dẫn: <i>{item.suggested_action}</i>")
            lines.append("")

        lines.append("📋 <b>Trích đoạn Log gần nhất:</b>")
        clean_snippet = log_snippet[-400:].replace("<", "&lt;").replace(">", "&gt;")
        lines.append(f"<pre>{clean_snippet}</pre>")
        lines.append("")
        lines.append("📎 <i>File log chi tiết được gửi đính kèm bên dưới.</i>")

        full_message = "\n".join(lines)
        url = f"https://api.telegram.org/bot{token}/sendMessage"

        try:
            resp = httpx.post(url, json={
                "chat_id": chat_id,
                "text": full_message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=12)
            if resp.status_code != 200:
                print(f"[Doctor] Telegram sendMessage error: {resp.text}")

            if log_file.is_file():
                doc_url = f"https://api.telegram.org/bot{token}/sendDocument"
                with log_file.open("rb") as fstream:
                    httpx.post(
                        doc_url,
                        data={
                            "chat_id": chat_id,
                            "caption": f"Log sự cố: {log_file.name}",
                        },
                        files={"document": (log_file.name, fstream, "text/plain")},
                        timeout=20,
                    )
            print("[Doctor] Đã gửi cảnh báo và file log sự cố tới Telegram thành công!")
            return True
        except Exception as exc:
            print(f"[Doctor] Không thể gửi tới Telegram: {exc}")
            return False


def main() -> int:
    parser = argparse.ArgumentParser(description="PI/VF Doctor Agent")
    parser.add_argument("--test-incident", action="store_true", help="Simulate an incident and test doctor reporting")
    args = parser.parse_args()

    doctor = DoctorAgent()
    if args.test_incident:
        print("Testing doctor incident handler...")
        log_f = doctor.handle_pipeline_incident(
            error_msg="Test Simulation: Google Flow keyframe timeout and stale locks detected",
            log_text="Sample traceback and error diagnostics log for testing doctor Telegram alerts.",
        )
        print(f"Incident log generated: {log_f}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
