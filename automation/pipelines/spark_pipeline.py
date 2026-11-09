"""End-to-end PI selection to Video Factory pipeline runner.

Executes Step 1 (PI Sheet or explicit CSV intake -> Exchange Inbox) followed by
Step 2 (VF Creative Batch -> MP4 Export) with verification.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from automation.config import (
    DAILY_VIDEO_LIMIT,
    DEFAULT_FOOD_PROFILE,
    DEFAULT_PROFILE,
    INBOX_ROOT,
    PI_DIR,
    PI_PYTHON,
    PI_RUNS_DIR,
    PI_SHEET_ROOT,
    ROOT_DIR,
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
from automation.storage import atomic_json, exclusive_file_lock


logger = logging.getLogger(__name__)


@dataclass
class VideoProbeResult:
    file_path: str
    sha256: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    frame_count: int | None = None
    decodable: bool = False
    error: str | None = None


@dataclass
class PipelineRunResult:
    run_id: str
    status: str  # "success", "partial", "waiting", "failed"
    production_date: str
    spark_csv: str
    spark_csv_sha256: str
    scenes: int
    limit: int
    provider_mode: str
    intake_result: dict[str, Any] = field(default_factory=dict)
    production_result: dict[str, Any] = field(default_factory=dict)
    exported_videos: list[dict[str, Any]] = field(default_factory=list)
    product_outcomes: list[dict[str, Any]] = field(default_factory=list)
    inspection_errors: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    error: str | None = None


def probe_mp4_file(file_path: Path | str) -> VideoProbeResult:
    """Probe an MP4 file with PyAV to verify decodability and metadata."""
    p = Path(file_path)
    if not p.is_file():
        return VideoProbeResult(
            file_path=str(p),
            sha256="",
            size_bytes=0,
            decodable=False,
            error="File does not exist",
        )

    file_bytes = p.read_bytes()
    file_sha = hashlib.sha256(file_bytes).hexdigest()
    size_bytes = len(file_bytes)

    try:
        import av

        container = av.open(str(p))
        if not container.streams.video:
            return VideoProbeResult(
                file_path=str(p),
                sha256=file_sha,
                size_bytes=size_bytes,
                decodable=False,
                error="No video streams found",
            )
        video_stream = container.streams.video[0]
        width = video_stream.width
        height = video_stream.height
        fps = float(video_stream.average_rate) if video_stream.average_rate else None
        
        # Decode all frames to guarantee full integrity
        frame_count = 0
        for frame in container.decode(video_stream):
            frame_count += 1

        return VideoProbeResult(
            file_path=str(p),
            sha256=file_sha,
            size_bytes=size_bytes,
            width=width,
            height=height,
            fps=fps,
            frame_count=frame_count,
            decodable=True,
        )
    except Exception as exc:
        return VideoProbeResult(
            file_path=str(p),
            sha256=file_sha,
            size_bytes=size_bytes,
            decodable=False,
            error=f"Decode error: {exc}",
        )


def _prepare_scene_profile(base_profile: Path, scenes: int, run_dir: Path) -> Path:
    """Generate a tuned run profile matching requested scene count (3, 4, 5)."""
    if scenes not in (3, 4, 5):
        raise ValueError(f"scenes must be 3, 4, or 5 (got {scenes})")

    cfg = json.loads(base_profile.read_text(encoding="utf-8"))
    cfg["max_scenes"] = scenes
    tuned_path = run_dir / f"profile_{scenes}scenes.json"
    tuned_path.parent.mkdir(parents=True, exist_ok=True)
    tuned_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return tuned_path


def is_verified_real_batch(batch_dir: Path | str) -> tuple[bool, str]:
    """Verify that an Exchange batch contains authentic operator media, not fixtures."""
    from PIL import Image
    import numpy as np

    b_path = Path(batch_dir).resolve()
    if not b_path.is_dir():
        return False, f"Thư mục batch không tồn tại: {b_path}"
    if not (b_path / "READY").is_file():
        return False, f"Cờ READY chưa có trong {b_path}"
    manifest_file = b_path / "manifest.json"
    if not manifest_file.is_file():
        return False, f"manifest.json không tồn tại trong {b_path}"

    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"Không đọc được manifest.json: {exc}"

    if manifest.get("fixture") is True or manifest.get("is_fixture") is True:
        return False, "Batch được đánh dấu rõ là fixture/dữ liệu giả lập."
    if b_path.name.startswith("run_fix_") or str(manifest.get("source_selection_batch_id", "")).startswith("fix_") or str(manifest.get("batch_id", "")).startswith("fix_"):
        return False, "Mã batch thuộc luồng fixture/offline test, không phải tư liệu thật."

    products = manifest.get("products", [])
    if not products:
        return False, "Batch manifest không chứa sản phẩm nào."

    for prod in products:
        images = prod.get("images", [])
        if not images:
            return False, f"Sản phẩm '{prod.get('name')}' không có ảnh tham chiếu."

        prov_rel = prod.get("provenance_path")
        if not prov_rel:
            return False, f"Sản phẩm '{prod.get('name')}' thiếu provenance_path (nguồn gốc chưa xác minh)."
        prov_file = (b_path / prov_rel).resolve()
        if not prov_file.is_relative_to(b_path) or not prov_file.is_file():
            return False, f"File provenance của sản phẩm '{prod.get('name')}' không tồn tại: {prov_file}"
        try:
            prov = json.loads(prov_file.read_text(encoding="utf-8"))
            rev = prov.get("review") or {}
            rev_by = str(rev.get("reviewed_by") or "").strip()
            if rev_by in ("fixture_operator", "synthetic_operator", ""):
                return False, f"Sản phẩm '{prod.get('name')}' chỉ được duyệt bởi fixture operator hoặc chưa có reviewer thật."
            if not rev.get("identity_verified"):
                return False, f"Sản phẩm '{prod.get('name')}' chưa qua xác minh identity."
        except Exception as exc:
            return False, f"Không đọc được provenance của sản phẩm '{prod.get('name')}': {exc}"

        local_import = (prov.get("acquisition_tool") == "spaiauto"
                        and isinstance(prov.get("archive_sha256"), str)
                        and len(prov["archive_sha256"]) == 64
                        and all(c in "0123456789abcdef" for c in prov["archive_sha256"]))
        reviewed_ids = rev.get("selected_asset_ids") or []
        for img_info in images:
            orig_url = str(img_info.get("original_url") or "")
            if not local_import and (not orig_url or "example.com" in orig_url
                                     or orig_url.startswith("file://") or "dummy" in orig_url.lower()):
                return False, f"Ảnh {img_info.get('asset_id')} có URL giả lập hoặc không rõ nguồn: {orig_url}"
            rel_path = img_info.get("relative_path")
            if not rel_path:
                return False, f"Ảnh {img_info.get('asset_id')} thiếu relative_path."
            img_file = (b_path / rel_path).resolve()
            if not img_file.is_relative_to(b_path) or not img_file.is_file():
                return False, f"File ảnh không tồn tại trong Exchange: {img_file}"
            if local_import:
                if not isinstance(reviewed_ids, list) or img_info.get("asset_id") not in reviewed_ids:
                    return False, f"Ảnh {img_info.get('asset_id')} chưa có trong biên nhận duyệt."
                try:
                    with img_file.open("rb") as stream:
                        actual_sha = hashlib.file_digest(stream, "sha256").hexdigest()
                except OSError as exc:
                    return False, f"Không đọc được ảnh local: {exc}"
                if img_info.get("sha256") != actual_sha:
                    return False, f"SHA của ảnh local không khớp: {img_file}"

            try:
                with Image.open(img_file) as im:
                    arr = np.array(im.convert("RGB"))
                    if float(arr.std()) < 1.0:
                        return False, f"Ảnh {img_file.name} là ảnh màu đơn sắc giả lập, không phải ảnh chụp sản phẩm thật."
            except Exception as exc:
                return False, f"Lỗi đọc ảnh {img_file}: {exc}"

    return True, "Batch hợp lệ: tư liệu thật đã được PI xác minh và duyệt."


def _inspect_run_workflows(
    db_path: Path,
    effective_run_id: str,
    exchange_batch_dir: Path | None = None,
    *,
    provider_mode: str = "fake",
    expected_product_key: str | None = None,
    inspection_errors: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Inspect SQLite DB to get exact workflows, outcomes, and export receipts for this run."""
    import sqlite3
    exported_probes = []
    product_outcomes = []
    if not db_path.is_file():
        return exported_probes, product_outcomes

    def report_error(location: str, exc: Exception) -> None:
        message = f"{location}: {type(exc).__name__}: {exc}"
        logger.error("VF inspection for %s: %s", effective_run_id, message, exc_info=True)
        if inspection_errors is None:
            raise exc
        inspection_errors.append(message)

    def verified_probe(path, receipt, project_id, exported):
        if receipt.get("project_id") != project_id:
            return None
        probe = probe_mp4_file(path)
        if not probe.decodable:
            return None
        if expected_product_key:
            recorded = (receipt.get("video") or {}).get("sha256") or (
                (receipt.get("files") or {}).get(path.name) or {}
            ).get("sha256")
            if recorded != probe.sha256 or exported.get("sha256") != probe.sha256:
                return None
        return probe

    manifest_products = {}
    if exchange_batch_dir and (exchange_batch_dir / "manifest.json").is_file():
        try:
            m_data = json.loads((exchange_batch_dir / "manifest.json").read_text(encoding="utf-8"))
            for p in m_data.get("products", []):
                manifest_products[p.get("product_key")] = p
        except Exception as exc:
            report_error("Exchange manifest", exc)

    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        
        selection_id = "pv-creative-batch-" + hashlib.sha256(effective_run_id.encode()).hexdigest()[:24]
        row = conn.execute("SELECT aggregate_json FROM workflow WHERE workflow_id = ?", (selection_id,)).fetchone()
        if not row:
            return exported_probes, product_outcomes

        selection_agg = json.loads(row["aggregate_json"])
        fake_workflows = set()
        if provider_mode == "configured" and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'product_video_qc_reports'"
        ).fetchone():
            fake_workflows = {
                row["workflow_id"] for row in conn.execute(
                    "SELECT DISTINCT workflow_id FROM product_video_qc_reports WHERE provider_mode = 'fake'"
                )
            }
        prod_refs = selection_agg.get("production_selected_refs") or selection_agg.get("prepared_refs") or []
        skipped_list = selection_agg.get("skipped", [])
        unresolved_list = selection_agg.get("unresolved", [])

        seen_refs = set()
        for ref in prod_refs:
            seen_refs.add(ref)
            wf_row = conn.execute("SELECT aggregate_json FROM workflow WHERE workflow_id = ?", (ref,)).fetchone()
            if not wf_row:
                continue
            wf_agg = json.loads(wf_row["aggregate_json"])
            state = wf_agg.get("state")
            source_ctx = wf_agg.get("source_context") or {}
            proj_id = wf_agg.get("project_id") or source_ctx.get("project_id") or ref
            p_key = source_ctx.get("product_key") or wf_agg.get("product_key")
            prod_info = manifest_products.get(p_key, {})
            name = (prod_info.get("name") if prod_info else None) or source_ctx.get("name") or (wf_agg.get("research_result") or {}).get("product_name") or ref
            if expected_product_key and (
                p_key != expected_product_key or source_ctx.get("source_batch_id") != selection_agg.get("batch_id")
            ):
                product_outcomes.append({
                    "product_name": name, "workflow_id": ref, "status": "failed",
                    "stage": "export_verification", "reason": "Workflow does not match the pinned PI batch and product",
                })
                continue

            if state == "completed":
                if ref in fake_workflows:
                    product_outcomes.append({
                        "product_name": name, "workflow_id": ref, "status": "failed",
                        "stage": "export_verification", "reason": "Existing export was produced in fake mode",
                        "next_step": "Run configured with a separate VF state and workspace",
                    })
                    continue
                exported = wf_agg.get("export_result") or {}
                raw_exp = json.loads(exported["payload"]) if isinstance(exported.get("payload"), str) else exported
                mp4_str = str(raw_exp.get("file_path") or raw_exp.get("video_path") or "")
                mp4_path = Path(mp4_str) if mp4_str else None
                receipt_file = mp4_path.parent / "metadata.json" if mp4_path else None
                receipt_valid = False

                if mp4_path and mp4_path.is_file() and receipt_file and receipt_file.is_file():
                    try:
                        receipt_data = json.loads(receipt_file.read_text(encoding="utf-8"))
                        probe = verified_probe(mp4_path, receipt_data, proj_id, raw_exp)
                        if probe:
                            receipt_valid = True
                            exported_probes.append(asdict(probe))
                            product_outcomes.append({
                                "product_name": name,
                                "workflow_id": ref,
                                "status": "success",
                                "stage": "completed",
                                "file_path": str(mp4_path),
                                "receipt_path": str(receipt_file),
                                "sha256": probe.sha256,
                                "decodable": True,
                                "reason": "Successfully exported and verified decodable MP4 from current run export receipt",
                                "next_step": "Delivery complete",
                            })
                    except Exception as exc:
                        report_error(f"Export receipt for {ref}", exc)

                if not receipt_valid:
                    product_outcomes.append({
                        "product_name": name,
                        "workflow_id": ref,
                        "status": "failed",
                        "stage": "export_verification",
                        "reason": "Export receipt missing, project_id mismatched, or video failed media check",
                        "next_step": "Inspect workspace exports and re-run export step",
                    })
            else:
                reason = (wf_agg.get("failure_reason") or wf_agg.get("skip_reason") or wf_agg.get("operator_reason")
                          or wf_agg.get("last_error") or f"Workflow state: {state}")
                product_outcomes.append({
                    "product_name": name,
                    "workflow_id": ref,
                    "status": "failed",
                    "stage": state or "creative_batch",
                    "reason": str(reason),
                    "next_step": "Inspect hermes.db or resume run",
                })

        for item in [*skipped_list, *unresolved_list]:
            w_id = item.get("workflow_id")
            if w_id and w_id not in seen_refs:
                seen_refs.add(w_id)
                # If an item was previously completed and is accepted as current final, retrieve its valid MP4 and receipt
                is_accepted = item.get("status") == "accepted" or item.get("reason") == "CURRENT_ACCEPTED_FINAL_EXISTS"
                if is_accepted and w_id in fake_workflows:
                    product_outcomes.append({
                        "product_name": item.get("product_key") or w_id, "workflow_id": w_id, "status": "failed",
                        "stage": "export_verification", "reason": "Existing export was produced in fake mode",
                        "next_step": "Run configured with a separate VF state and workspace",
                    })
                    continue
                accepted_probe = None
                accepted_outcome = None
                if is_accepted:
                    wf_row = conn.execute("SELECT aggregate_json FROM workflow WHERE workflow_id = ?", (w_id,)).fetchone()
                    if wf_row:
                        wf_agg = json.loads(wf_row["aggregate_json"])
                        source_ctx = wf_agg.get("source_context") or {}
                        proj_id = wf_agg.get("project_id") or source_ctx.get("project_id") or w_id
                        p_key = source_ctx.get("product_key") or wf_agg.get("product_key") or item.get("product_key")
                        prod_info = manifest_products.get(p_key, {})
                        name = (prod_info.get("name") if prod_info else None) or source_ctx.get("name") or (wf_agg.get("research_result") or {}).get("product_name") or w_id
                        exported = wf_agg.get("export_result") or {}
                        raw_exp = json.loads(exported["payload"]) if isinstance(exported.get("payload"), str) else exported
                        mp4_str = str(raw_exp.get("file_path") or raw_exp.get("video_path") or "")
                        mp4_path = Path(mp4_str) if mp4_str else None
                        receipt_file = mp4_path.parent / "metadata.json" if mp4_path else None
                        if mp4_path and mp4_path.is_file() and receipt_file and receipt_file.is_file():
                            try:
                                receipt_data = json.loads(receipt_file.read_text(encoding="utf-8"))
                                probe = verified_probe(mp4_path, receipt_data, proj_id, raw_exp)
                                if probe and (not expected_product_key or p_key == expected_product_key):
                                    reused_from_other_batch = bool(
                                        expected_product_key
                                        and source_ctx.get("source_batch_id") != selection_agg.get("batch_id")
                                    )
                                    accepted_probe = asdict(probe)
                                    accepted_outcome = {
                                        "product_name": name,
                                        "workflow_id": w_id,
                                        "status": "reused" if reused_from_other_batch else "success",
                                        "stage": "completed",
                                        "file_path": str(mp4_path),
                                        "receipt_path": str(receipt_file),
                                        "sha256": probe.sha256,
                                        "decodable": True,
                                        "reason": "Reused current accepted and verified decodable MP4 from export receipt",
                                        "next_step": "Delivery complete",
                                    }
                            except Exception as exc:
                                report_error(f"Reused export receipt for {w_id}", exc)

                if accepted_outcome and accepted_probe:
                    if accepted_outcome["status"] == "success":
                        exported_probes.append(accepted_probe)
                    product_outcomes.append(accepted_outcome)
                else:
                    product_outcomes.append({
                        "product_name": item.get("product_key") or w_id,
                        "product_key": item.get("product_key"),
                        "workflow_id": w_id,
                        "status": "failed",
                        "stage": "creative_batch",
                        "reason": str(item.get("reason") or "Item skipped or unresolved"),
                        "next_step": "Resume run or inspect hermes.db",
                    })

    except Exception as exc:
        report_error("VF workflow database", exc)
    finally:
        if conn is not None:
            conn.close()

    return exported_probes, product_outcomes


def prepare_sheet_intake(day: str, limit: int, *, acquire: bool = False) -> dict[str, Any]:
    """Run PI's existing two-Sheet selection and reviewed Exchange handoff."""
    proc = subprocess.run(
        [str(PI_PYTHON), "-m", "tools.sheet_daily", "--root", str(PI_SHEET_ROOT),
         "--day", day, "--db-path", str(PI_SHEET_ROOT / "pi_snapshots.db"),
         "--inbox-root", str(INBOX_ROOT), "--limit", str(limit), *(["--acquire"] if acquire else [])],
        cwd=PI_DIR, capture_output=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "NINEROUTER_BASE_URL": os.environ.get("NINEROUTER_BASE_URL", "http://127.0.0.1:20128/v1"), "PI_SELECTION_MODEL": os.environ.get("PI_SELECTION_MODEL", "ag/gemini-3.8-flash"), "PI_SPAIAUTO_REVIEW_MODEL": os.environ.get("PI_SPAIAUTO_REVIEW_MODEL", "ag/gemini-3.8-flash")},
    )
    if not proc.returncode and proc.stderr.strip():
        logger.warning("PI intake diagnostics for %s:\n%s", day, proc.stderr.strip())
    out_text = (proc.stderr if proc.returncode else proc.stdout).strip()
    json_candidate = None
    for line in reversed(out_text.splitlines()):
        line_s = line.strip()
        if line_s.startswith("{") and line_s.endswith("}"):
            json_candidate = line_s
            break
    try:
        data = json.loads(json_candidate or out_text)
    except (ValueError, TypeError):
        return {"status": "error", "error": (proc.stderr or proc.stdout).strip()}
    if not isinstance(data, dict):
        return {"status": "error", "error": "Unexpected response from PI Sheet intake"}
    return data


def _saved_run_qc_level(db_path: Path, run_id: str) -> str:
    """Read the policy pinned to this run; absent legacy levels mean middle."""
    import sqlite3
    if not db_path.is_file():
        raise ValueError('Cannot resume QC policy: saved workflow database is missing')
    selection_id = 'pv-creative-batch-' + hashlib.sha256(run_id.encode()).hexdigest()[:24]
    with sqlite3.connect(f'file:{db_path.as_posix()}?mode=ro', uri=True) as conn:
        row = conn.execute('SELECT aggregate_json FROM workflow WHERE workflow_id=?', (selection_id,)).fetchone()
        if row is None:
            raise ValueError('Cannot resume QC policy: saved creative batch is missing')
        selection = json.loads(row[0])
        if selection.get('qc_level'):
            return selection['qc_level']
        levels = set()
        for ref in selection.get('prepared_refs') or []:
            child = conn.execute('SELECT aggregate_json FROM workflow WHERE workflow_id=?', (ref,)).fetchone()
            if child:
                levels.add((json.loads(child[0]).get('creative_policy') or {}).get('qc_level') or 'middle')
        if len(levels) > 1:
            raise ValueError('Cannot resume QC policy: saved creative batch policies disagree')
        return next(iter(levels), 'middle')


def run_pipeline(
    spark_csv: Path | str | None = None,
    limit: int = 1,
    scenes: int = 4,
    date: str | None = None,
    provider_mode: str = "fake",
    run_id: str | None = None,
    profile_path: Path | str | None = None,
    db_path: Path | str | None = None,
    offline_intake: bool | None = None,
    qc_level: str | None = "high",
    sheet_intake: dict[str, Any] | None = None,
) -> PipelineRunResult:
    """Run the pipeline from two Google Sheets or an explicitly supplied CSV."""
    ensure_runtime_dirs()
    start_time = time.time()
    started_at = now_bangkok().isoformat()
    prod_date = date or today_bangkok_str()

    # In configured mode, intake must be live and batches must be authentic
    if provider_mode == "configured":
        effective_offline_intake = False
    elif offline_intake is None:
        effective_offline_intake = True
    else:
        effective_offline_intake = bool(offline_intake)

    from_sheets = spark_csv is None
    if from_sheets and not 1 <= limit <= DAILY_VIDEO_LIMIT:
        raise ValueError(f"Sheet limit must be between 1 and {DAILY_VIDEO_LIMIT}")
    if from_sheets and sheet_intake is None:
        sheet_intake = prepare_sheet_intake(prod_date, limit, acquire=not effective_offline_intake)
    selection_csv = (sheet_intake or {}).get("selection_csv") if from_sheets else spark_csv
    csv_path = Path(selection_csv).resolve() if selection_csv else None
    if csv_path is not None and not csv_path.is_file():
        raise FileNotFoundError(f"Spark sheet CSV not found: {csv_path}")

    csv_sha = hashlib.sha256(csv_path.read_bytes()).hexdigest() if csv_path is not None else ""
    source_key = csv_sha[:8] or "sheets"
    effective_run_id = run_id or f"run_{prod_date.replace('-', '')}_{source_key}_s{scenes}_n{limit}"

    run_dir = PI_RUNS_DIR / effective_run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    profile_file = Path(profile_path or DEFAULT_PROFILE).resolve()
    if not profile_file.is_file():
        raise FileNotFoundError(f"Creative run profile not found: {profile_file}")

    tuned_profile = _prepare_scene_profile(profile_file, scenes, run_dir)
    effective_db = Path(db_path or VF_DB_PATH).resolve()
    sheet_batches = from_sheets and (
        "exchange_batches" in (sheet_intake or {}) or (run_dir / "vf-sheet-policy.json").is_file()
    )
    if qc_level is None and not sheet_batches:
        qc_level = _saved_run_qc_level(effective_db, effective_run_id)

    result = PipelineRunResult(
        run_id=effective_run_id,
        status="running",
        production_date=prod_date,
        spark_csv=str(csv_path) if csv_path is not None else "",
        spark_csv_sha256=csv_sha,
        scenes=scenes,
        limit=limit,
        provider_mode=provider_mode,
        started_at=started_at,
    )

    # -------------------------------------------------------------
    # Stage 1: PI Intake -> Exchange Inbox
    # -------------------------------------------------------------
    if from_sheets:
        intake_data = sheet_intake
        intake_stderr = ""
        intake_failed = intake_data.get("status") == "error"
    else:
        intake_cmd = [
            str(PI_PYTHON),
            "-m", "tools.spark_intake",
            "--spark-file", str(csv_path),
            "--run-dir", str(run_dir),
            "--inbox-root", str(INBOX_ROOT),
            "--limit", str(limit),
            "--date", prod_date,
        ]
        if effective_offline_intake:
            intake_cmd.append("--offline")
        else:
            intake_cmd.append("--live-intake")

        intake_env = os.environ.copy()
        intake_env["PYTHONIOENCODING"] = "utf-8"

        intake_proc = subprocess.run(
            intake_cmd,
            cwd=str(PI_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=intake_env,
        )

        intake_stdout = intake_proc.stdout or ""
        intake_stderr = intake_proc.stderr or ""

        try:
            intake_data = json.loads(intake_stdout)
        except Exception:
            intake_data = {
                "stdout": intake_stdout,
                "stderr": intake_stderr,
                "returncode": intake_proc.returncode,
            }

        intake_failed = intake_proc.returncode != 0 or intake_data.get("status") == "error"

    result.intake_result = intake_data

    if intake_failed:
        result.status = "failed"
        result.error = f"PI Intake failed: {intake_data.get('error') or intake_stderr}"
        result.finished_at = now_bangkok().isoformat()
        result.duration_seconds = round(time.time() - start_time, 2)
        return result

    if intake_data.get("status") in ("waiting", "no_eligible_products") and not sheet_batches:
        result.status = "waiting"
        result.error = intake_data.get("reason") or "Tư liệu thật chưa sẵn sàng."
        pending = intake_data.get("pending_items") or []
        result.product_outcomes = [
            {
                "product_name": name,
                "status": "waiting",
                "stage": "pi_intake",
                "reason": intake_data.get("reason"),
                "next_step": intake_data.get("next_step"),
            }
            for name in (pending or ["All selected products"])
        ]
        result.finished_at = now_bangkok().isoformat()
        result.duration_seconds = round(time.time() - start_time, 2)
        return result

    if sheet_batches:
        qc_level, batches = _pin_sheet_batches(result, run_dir, effective_db, qc_level)
        day_key = hashlib.sha256(f"{effective_db}:{prod_date}".encode()).hexdigest()[:24]
        with exclusive_file_lock(PI_RUNS_DIR / f"sheet-{day_key}", label="daily video"):
            _run_sheet_batches(
                result, batches, effective_db, tuned_profile, run_dir, profile_path, qc_level, start_time,
            )
        result.finished_at = now_bangkok().isoformat()
        result.duration_seconds = round(time.time() - start_time, 2)
        return result

    return _run_exchange_batch(
        result, Path(intake_data.get("exchange_batch_dir") or ""), effective_db, tuned_profile,
        run_dir, profile_path, qc_level, start_time, from_sheets=from_sheets,
    )


def _sheet_child_run_id(parent_id: str, batch_name: str) -> str:
    digest = hashlib.sha256(json.dumps([parent_id, batch_name]).encode("utf-8")).hexdigest()
    return f"sheet_{digest[:32]}"


def _pin_sheet_batches(result, run_dir, db_path, qc_level):
    from contextlib import closing
    import sqlite3

    batches = result.intake_result.get("exchange_batches", [])
    if not isinstance(batches, list):
        raise ValueError("PI exchange_batches must be a list")
    if result.limit < 1:
        raise ValueError("Sheet limit must be positive")
    binding_path = run_dir / "vf-sheet-policy.json"
    with exclusive_file_lock(binding_path):
        saved = json.loads(binding_path.read_text(encoding="utf-8")) if binding_path.is_file() else {}
        if saved:
            if qc_level is not None and qc_level != saved["qc_level"]:
                raise ValueError("QC level differs from the pinned Sheet run")
            if result.provider_mode != saved["provider_mode"]:
                raise ValueError("Provider mode differs from the pinned Sheet run")
            if result.limit != saved.get("limit", result.limit):
                raise ValueError("Limit differs from the pinned Sheet run")
            qc_level = saved["qc_level"]
        elif qc_level is None:
            raise ValueError("Cannot resume QC policy: saved Sheet run policy is missing")

        def row_key(entry):
            return (entry.get("candidate_id") or entry.get("row_id") or entry.get("batch_path")
                    if isinstance(entry, dict) else None)

        pinned = saved.get("admitted_batches")
        if pinned is None:
            pinned = []
            # Older runs pinned child selections in VF but had no parent roster.
            if db_path.is_file():
                current_paths = {str(Path(entry["batch_path"]).resolve()): entry for entry in batches
                                 if isinstance(entry, dict) and entry.get("batch_path")}
                with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as conn:
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow'").fetchone():
                        for workflow_id, raw in conn.execute(
                            "SELECT workflow_id, aggregate_json FROM workflow "
                            "WHERE workflow_id LIKE 'pv-creative-batch-%' ORDER BY rowid"
                        ):
                            batch_path = json.loads(raw).get("batch_dir")
                            if not batch_path:
                                continue
                            path = Path(batch_path).resolve()
                            child = _sheet_child_run_id(result.run_id, path.name)
                            if workflow_id == "pv-creative-batch-" + hashlib.sha256(child.encode()).hexdigest()[:24]:
                                pinned.append(current_paths.get(str(path), {"batch_path": str(path)}))
        known = {row_key(entry): index for index, entry in enumerate(pinned) if row_key(entry)}
        known_paths = {entry["batch_path"] for entry in pinned if isinstance(entry, dict) and entry.get("batch_path")}
        for entry in batches:
            key = row_key(entry)
            if key in known:
                previous = pinned[known[key]]
                if not previous.get("batch_path") and entry.get("batch_path") and not entry.get("error"):
                    pinned[known[key]] = entry
            elif (len(pinned) < result.limit or result.limit == DAILY_VIDEO_LIMIT) and (
                not isinstance(entry, dict) or entry.get("batch_path") not in known_paths
            ):
                if key:
                    known[key] = len(pinned)
                pinned.append(entry)
                if isinstance(entry, dict) and entry.get("batch_path"):
                    known_paths.add(entry["batch_path"])
        if len(pinned) > result.limit and result.limit != DAILY_VIDEO_LIMIT:
            raise ValueError("Existing Sheet selections exceed this run's pinned limit")
        atomic_json(binding_path, {"qc_level": qc_level, "provider_mode": result.provider_mode,
                                  "limit": result.limit, "admitted_batches": pinned})
    return qc_level, pinned


def _sheet_video_slots(db_path: Path, day: str) -> set[str]:
    import sqlite3
    from contextlib import closing

    if not db_path.is_file():
        return set()
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow'").fetchone():
            return set()
        rows = conn.execute(
            """SELECT workflow_id, aggregate_json FROM workflow
               WHERE json_extract(aggregate_json, '$.source_context.source_date')=?""", (day,),
        )
        slots = set()
        for workflow_id, raw in rows:
            aggregate = json.loads(raw)
            state = aggregate.get("state")
            if aggregate.get("export_result") or (
                aggregate.get("production_run_id") and state not in {"skipped", "contract_broken"}
            ):
                slots.add(aggregate["source_context"].get("product_key") or workflow_id)
        return slots


def _run_sheet_batches(result, batches, db_path, tuned_profile, run_dir, profile_path, qc_level, start_time):
    result.production_result = {"batches": []}
    statuses = []
    seen = set()
    for entry in batches:
        child_dir = None
        row_id = entry.get("row_id") if isinstance(entry, dict) else None
        candidate_id = entry.get("candidate_id") if isinstance(entry, dict) else None
        name = (entry.get("name") if isinstance(entry, dict) else None) or row_id or "PI product"
        product_key = None
        try:
            if not isinstance(entry, dict):
                raise ValueError("Invalid PI batch entry")
            if entry.get("error") or not entry.get("batch_path"):
                raise ValueError(entry.get("error") or "Missing PI batch path")
            child_dir = Path(entry["batch_path"]).resolve()
            if child_dir in seen:
                continue
            seen.add(child_dir)
            if child_dir.parent != (INBOX_ROOT / result.production_date).resolve():
                raise ValueError("PI batch must be inside the inbox for this production date")
            if not (child_dir / "READY").is_file():
                raise ValueError("PI batch is missing READY")
            manifest = json.loads((child_dir / "manifest.json").read_text(encoding="utf-8"))
            products = manifest["products"]
            if len(products) != 1 or not products[0].get("product_key"):
                raise ValueError("Per-row PI batch must contain exactly one product key")
            product_key = products[0]["product_key"]
            slots = _sheet_video_slots(db_path, result.production_date)
            if len(slots) >= DAILY_VIDEO_LIMIT and product_key not in slots:
                statuses.append("success")
                result.product_outcomes.append({
                    "row_id": row_id, "candidate_id": candidate_id, "product_name": name, "product_key": product_key,
                    "status": "skipped", "stage": "exchange_admission", "reason": "DAILY_VIDEO_CAP_REACHED",
                })
                continue
            child = PipelineRunResult(
                _sheet_child_run_id(result.run_id, child_dir.name), "running", result.production_date,
                result.spark_csv, result.spark_csv_sha256, result.scenes, 1, result.provider_mode,
            )
            _run_exchange_batch(
                child, child_dir, db_path, tuned_profile, run_dir, profile_path, qc_level, start_time,
                from_sheets=True, product_key=product_key,
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            statuses.append("failed")
            result.product_outcomes.append({
                "row_id": row_id, "candidate_id": candidate_id, "product_name": name, "product_key": product_key,
                "status": "failed", "stage": "exchange_admission", "reason": str(exc),
            })
            continue
        statuses.append(child.status)
        result.production_result["batches"].append({
            "batch_path": str(child_dir), "run_id": child.run_id, "status": child.status,
            **child.production_result,
        })
        result.product_outcomes.extend(
            {**item, "row_id": row_id, "candidate_id": candidate_id, "product_key": product_key}
            for item in child.product_outcomes
        )
        if not child.product_outcomes:
            result.product_outcomes.append({
                "row_id": row_id, "candidate_id": candidate_id, "product_key": product_key, "product_name": name,
                "status": child.status, "stage": "creative_batch", "reason": child.error,
            })
        result.inspection_errors.extend(child.inspection_errors)
        result.exported_videos.extend(child.exported_videos)
    pending = (result.intake_result.get("pending_items") or []) if len(batches) < result.limit else []
    for name in pending:
        statuses.append("waiting")
        result.product_outcomes.append({
            "product_name": name, "status": "waiting", "stage": "pi_intake",
            "reason": result.intake_result.get("reason"),
        })
    if statuses and all(status == "success" for status in statuses):
        result.status = "success"
    elif result.exported_videos:
        result.status = "partial"
        result.error = "Some products still need attention"
    elif not statuses or all(status == "waiting" for status in statuses):
        result.status = "waiting"
        result.error = "No completed PI/VF product yet"
    else:
        result.status = "failed"
        result.error = "No product has a verified MP4"


def _run_exchange_batch(result, exchange_batch_dir, effective_db, tuned_profile, run_dir,
                        profile_path, qc_level, start_time, *, from_sheets, product_key=None):
    effective_run_id = result.run_id
    prod_date = result.production_date
    provider_mode = result.provider_mode
    scenes = result.scenes
    if not exchange_batch_dir.is_dir() or not (exchange_batch_dir / "READY").is_file():
        result.status = "waiting"
        result.error = "Exchange READY artifact is missing or pending operator intake completion."
        result.finished_at = now_bangkok().isoformat()
        result.duration_seconds = round(time.time() - start_time, 2)
        return result

    # Check configured mode batch authenticity
    if provider_mode == "configured":
        is_real, reason = is_verified_real_batch(exchange_batch_dir)
        if not is_real:
            result.status = "waiting"
            result.error = f"Chế độ configured chỉ nhận batch chứa tư liệu thật đã được PI xác minh và duyệt. {reason}"
            result.product_outcomes = [
                {
                    "product_name": "Selected products",
                    "status": "waiting",
                    "stage": "exchange_admission",
                    "reason": reason,
                    "next_step": "Thực hiện Spaiauto browser acquisition để thu thập tư liệu thật và duyệt ảnh trước khi chạy configured mode.",
                }
            ]
            result.finished_at = now_bangkok().isoformat()
            result.duration_seconds = round(time.time() - start_time, 2)
            return result

    # -------------------------------------------------------------
    # Stage 2: VF Creative Batch -> MP4 Export
    # -------------------------------------------------------------
    # Dynamically select appropriate profile if default profile was used
    effective_profile_to_use = tuned_profile
    if profile_path is None and (exchange_batch_dir / "manifest.json").is_file():
        try:
            m_data = json.loads((exchange_batch_dir / "manifest.json").read_text(encoding="utf-8"))
            has_food = False
            food_markers = ("food", "snack", "do an", "thuc pham", "do uong", "banh", "keo", "mi an lien", "dac san")
            import unicodedata
            for p in m_data.get("products", []):
                cat = unicodedata.normalize("NFKD", str(p.get("category") or "").casefold().replace("đ", "d"))
                cat = cat.encode("ascii", "ignore").decode("ascii")
                if any(m in cat for m in food_markers):
                    has_food = True
                    break
            if has_food and DEFAULT_FOOD_PROFILE.is_file():
                effective_profile_to_use = _prepare_scene_profile(DEFAULT_FOOD_PROFILE, scenes, run_dir)
        except Exception:
            pass

    vf_cmd = [
        str(VF_PYTHON),
        "-m", "video_factory.cli", "creative-batch",
        "--run-id", effective_run_id,
        "--date", prod_date,
        "--profile", str(effective_profile_to_use),
        "--provider-mode", provider_mode,
        "--qc-level", qc_level,
        "--inbox-root", str(INBOX_ROOT),
        "--db-path", str(effective_db),
    ]
    if product_key is None:
        vf_cmd.extend(("--limit", str(result.limit)))
    else:
        vf_cmd.extend(("--product-key", product_key))
    if from_sheets:
        vf_cmd.extend(("--exchange-batch", str(exchange_batch_dir)))

    vf_env = os.environ.copy()
    vf_env["PYTHONIOENCODING"] = "utf-8"
    vf_env["HERMES_VIDEO_FACTORY_WORKSPACE"] = str(VF_WORKSPACE_DIR)
    vf_env["HERMES_EXCHANGE_INBOX"] = str(INBOX_ROOT)
    vf_env["VIDEO_FACTORY_HOME"] = str(VF_RUNTIME_DIR)
    vf_env["HERMES_HOME"] = str(VF_RUNTIME_DIR)
    vf_env["HERMES_APPROVAL_DB_PATH"] = str(effective_db)
    vf_env["HERMES_VIDEO_FACTORY_DB_PATH"] = str(effective_db)

    vf_proc = subprocess.run(
        vf_cmd,
        cwd=str(VF_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=vf_env,
    )

    vf_stdout = vf_proc.stdout or ""
    vf_stderr = vf_proc.stderr or ""
    vf_output = {
        "stdout": vf_stdout,
        "stderr": vf_stderr,
        "returncode": vf_proc.returncode,
    }
    result.production_result = vf_output

    # -------------------------------------------------------------
    # Stage 3: Verification & MP4 Discovery & Structured Outcomes
    # -------------------------------------------------------------
    # Strictly query export receipts and workflows belonging to THIS run
    db_probes, db_outcomes = _inspect_run_workflows(
        effective_db, effective_run_id, exchange_batch_dir, provider_mode=provider_mode,
        expected_product_key=product_key,
        inspection_errors=result.inspection_errors,
    )

    exported_probes: list[dict[str, Any]] = list(db_probes)
    product_outcomes: list[dict[str, Any]] = list(db_outcomes)

    # If DB inspection didn't find items (e.g. mock test environment), check stdout receipts
    if product_key is None and not exported_probes and not product_outcomes and not result.inspection_errors:
        for line in vf_stdout.splitlines():
            line_clean = line.strip()
            if "Thành công — " in line_clean:
                parts = line_clean.split("Thành công — ")
                prod_name = parts[0].strip().rstrip(":")
                reported_path = parts[1].strip()
                # Verify export receipt metadata.json exists
                receipt_p = Path(reported_path).parent / "metadata.json"
                if Path(reported_path).is_file() and receipt_p.is_file():
                    probe = probe_mp4_file(reported_path)
                    if probe.decodable:
                        exported_probes.append(asdict(probe))
                        product_outcomes.append({
                            "product_name": prod_name,
                            "status": "success",
                            "stage": "completed",
                            "file_path": reported_path,
                            "receipt_path": str(receipt_p),
                            "decodable": probe.decodable,
                            "reason": "Successfully exported and verified decodable MP4 from export receipt",
                            "next_step": "Delivery complete",
                        })
            elif "Lỗi kỹ thuật:" in line_clean:
                parts = line_clean.split("Lỗi kỹ thuật:")
                sub_parts = parts[1].strip().split(":", 1)
                prod_name = sub_parts[0].strip()
                reason = sub_parts[1].strip() if len(sub_parts) > 1 else "Technical error encountered during creative batch"
                product_outcomes.append({
                    "product_name": prod_name,
                    "status": "failed",
                    "stage": "creative_batch",
                    "reason": reason,
                    "next_step": "Retry or inspect workflow state in hermes.db",
                })

    result.exported_videos = exported_probes
    result.product_outcomes = product_outcomes

    total_prods = len(product_outcomes)
    succ_count = sum(1 for p in product_outcomes if p.get("status") in {"success", "reused"})

    if result.inspection_errors:
        result.status = "partial" if succ_count else "failed"
        result.error = "VF inspection failed: " + "; ".join(result.inspection_errors)
    elif total_prods > 0 and succ_count == total_prods and vf_proc.returncode == 0:
        result.status = "success"
    elif succ_count > 0:
        result.status = "partial"
        result.error = f"Partial success: {succ_count}/{total_prods} product(s) completed successfully; resume required for remaining items."
    elif total_prods > 0 and succ_count == 0:
        result.status = "failed"
        result.error = f"VF Creative Batch failed to produce valid videos (code {vf_proc.returncode}): {vf_proc.stderr or vf_proc.stdout}"
    elif vf_proc.returncode == 0 and len(exported_probes) > 0:
        result.status = "success"
    elif len(exported_probes) > 0:
        result.status = "partial"
    else:
        result.status = "failed"
        result.error = f"VF Creative Batch failed with code {vf_proc.returncode}: {vf_proc.stderr or vf_proc.stdout}"

    result.finished_at = now_bangkok().isoformat()
    result.duration_seconds = round(time.time() - start_time, 2)
    return result
