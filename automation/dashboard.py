"""PI/VF Lightweight Operator Dashboard.

FastAPI-powered single-file local web interface for monitoring daily runs,
previewing generated POV videos, uploading user-fixed images for skipped/failed
products, and orchestrating Antigravity/Codex agent runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, HTMLResponse, JSONResponse

from automation.config import (
    BANGKOK_TZ,
    DAILY_VIDEO_LIMIT,
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

logger = logging.getLogger("dashboard")

app = FastAPI(title="PI/VF Video Automation Studio", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Background task runner tracking
ACTIVE_PROCESS: dict[str, Any] = {"running": False, "pid": None, "log_file": None, "command": None}


def _get_today_report(date_str: str | None = None) -> dict[str, Any]:
    target_date = date_str or today_bangkok_str()
    report_file = RUNTIME_DIR / "reports" / f"{target_date}.json"
    if report_file.is_file():
        try:
            return json.loads(report_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"date": target_date, "runs": {}, "error": "Chưa có báo cáo cho ngày này."}


def _get_exports_summary() -> list[dict[str, Any]]:
    results = []
    if not VF_EXPORTS_DIR.is_dir():
        return results
    for export_dir in sorted(VF_EXPORTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not export_dir.is_dir():
            continue
        final_mp4 = export_dir / "final.mp4"
        meta_file = export_dir / "metadata.json"
        if not final_mp4.is_file():
            continue
        meta = {}
        if meta_file.is_file():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        stat = final_mp4.stat()
        mtime_str = datetime.fromtimestamp(stat.st_mtime, tz=BANGKOK_TZ).strftime("%Y-%m-%d %H:%M:%S")
        v_info = meta.get("video", {})
        results.append({
            "export_id": export_dir.name,
            "product_name": meta.get("product_identity") or meta.get("project_id") or export_dir.name,
            "project_id": meta.get("project_id"),
            "file_size_mb": round(stat.st_size / (1024 * 1024), 2),
            "created_at": mtime_str,
            "duration_seconds": v_info.get("duration_seconds", 32.0),
            "resolution": f"{v_info.get('width', 1080)}x{v_info.get('height', 1920)}",
            "fps": v_info.get("fps", 30.0),
            "sha256": v_info.get("sha256", ""),
            "has_caption": (export_dir / "caption.txt").is_file(),
            "video_hub_path": meta.get("video_ai_hub_path"),
        })
    return results


def _get_intake_items(date_str: str | None = None) -> list[dict[str, Any]]:
    target_date = date_str or today_bangkok_str()
    runs_dir = PI_SHEET_ROOT / "pi_runs"
    items = []
    if not runs_dir.is_dir():
        return items
    # Find matching pi_runs for today
    for run_dir in sorted(runs_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not run_dir.is_dir():
            continue
        intake_file = run_dir / "intake.json"
        if not intake_file.is_file():
            continue
        try:
            data = json.loads(intake_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        run_date = data.get("date") or ""
        if run_date and run_date != target_date:
            continue
        batch_id = run_dir.name
        for row in data.get("rows", []):
            items.append({
                "batch_id": batch_id,
                "row_id": row.get("row_id"),
                "name": row.get("name"),
                "status": row.get("status"),
                "reason": row.get("reason"),
                "reason_code": row.get("reason_code"),
                "source_url": row.get("source_url"),
                "alternatives": row.get("alternatives", []),
                "requested_variant": (row.get("input_metadata") or {}).get("requested_variant"),
            })
    return items


# =====================================================================
# API Endpoints
# =====================================================================

@app.get("/api/status")
def get_status():
    report = _get_today_report()
    exports = _get_exports_summary()
    today_exports = [e for e in exports if e["created_at"].startswith(today_bangkok_str())]
    return {
        "bangkok_time": now_bangkok().strftime("%Y-%m-%d %H:%M:%S"),
        "today": today_bangkok_str(),
        "today_completed_count": len(today_exports),
        "daily_limit": DAILY_VIDEO_LIMIT,
        "total_exports_count": len(exports),
        "worker_running": ACTIVE_PROCESS["running"],
        "worker_command": ACTIVE_PROCESS["command"],
    }


@app.get("/api/products")
def get_products(date: str | None = None):
    target_date = date or today_bangkok_str()
    report = _get_today_report(target_date)
    intake_items = _get_intake_items(target_date)
    exports = _get_exports_summary()
    return {
        "date": target_date,
        "intake_products": intake_items,
        "report": report,
        "exports": exports,
    }


@app.get("/api/video-stream/{export_id}")
def stream_video(export_id: str):
    video_path = VF_EXPORTS_DIR / export_id / "final.mp4"
    if not video_path.is_file():
        raise HTTPException(status_code=404, detail="Video file not found")
    return FileResponse(path=str(video_path), media_type="video/mp4", filename=f"{export_id}.mp4")


@app.get("/api/caption/{export_id}")
def get_caption(export_id: str):
    cap_path = VF_EXPORTS_DIR / export_id / "caption.txt"
    if not cap_path.is_file():
        return {"caption": ""}
    return {"caption": cap_path.read_text(encoding="utf-8", errors="replace")}


@app.post("/api/action/reopen-product")
def reopen_product(batch_id: str = Form(...), row_id: str = Form(...)):
    """Reset a skipped or failed product back to pending for re-acquisition."""
    intake_file = PI_SHEET_ROOT / "pi_runs" / batch_id / "intake.json"
    if not intake_file.is_file():
        raise HTTPException(status_code=404, detail="Batch intake.json not found")
    try:
        data = json.loads(intake_file.read_text(encoding="utf-8"))
        updated = False
        for row in data.get("rows", []):
            if row.get("row_id") == row_id:
                row["status"] = "pending"
                row["reason"] = None
                row["reason_code"] = None
                updated = True
                break
        if not updated:
            raise HTTPException(status_code=404, detail=f"Row {row_id} not found in batch")
        intake_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"success": True, "message": f"Sản phẩm {row_id} đã được mở lại (status: pending)."}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/action/upload-fixed-images")
async def upload_fixed_images(
    batch_id: str = Form(...),
    row_id: str = Form(...),
    files: list[UploadFile] = File(...),
):
    """Save user-cleaned/fixed images directly into product candidates directory."""
    if not files:
        raise HTTPException(status_code=400, detail="Vui lòng chọn ít nhất 1 ảnh sạch.")
    target_dir = PI_SHEET_ROOT / "pi_runs" / batch_id / "rows" / row_id / "scheduled_capture" / "extracted" / "media" / "candidates"
    target_dir.mkdir(parents=True, exist_ok=True)
    saved_paths = []
    for idx, f in enumerate(files, 1):
        ext = Path(f.filename or "img.jpg").suffix or ".jpg"
        save_file = target_dir / f"user_fixed_{idx:03d}{ext}"
        content = await f.read()
        save_file.write_bytes(content)
        saved_paths.append(str(save_file))

    # Mark row as resolved / pending
    intake_file = PI_SHEET_ROOT / "pi_runs" / batch_id / "intake.json"
    if intake_file.is_file():
        try:
            data = json.loads(intake_file.read_text(encoding="utf-8"))
            for r in data.get("rows", []):
                if r.get("row_id") == row_id:
                    r["status"] = "pending"
                    r["reason"] = f"Operator uploaded {len(saved_paths)} cleaned images."
                    break
            intake_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    return {
        "success": True,
        "message": f"Đã lưu {len(saved_paths)} ảnh sạch vào tư liệu sản phẩm {row_id}!",
        "saved_files": saved_paths,
    }


@app.post("/api/action/trigger-pipeline")
def trigger_pipeline(
    limit: int = Form(1),
    provider_mode: str = Form("configured"),
    qc_level: str = Form("low"),
):
    """Launch Daily Trigger in background."""
    if ACTIVE_PROCESS["running"]:
        raise HTTPException(status_code=409, detail="Đang có một tiến trình pipeline khác đang chạy!")

    log_file = RUNTIME_DIR / "reports" / f"dashboard_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    cmd = [
        str(VF_PYTHON),
        "-m", "automation.cli", "trigger",
        "--limit", str(limit),
        "--provider-mode", provider_mode,
        "--qc-level", qc_level,
        "--force",
    ]

    def _run():
        ACTIVE_PROCESS["running"] = True
        ACTIVE_PROCESS["command"] = " ".join(cmd)
        ACTIVE_PROCESS["log_file"] = str(log_file)
        with open(log_file, "w", encoding="utf-8") as out:
            proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT_DIR),
                stdout=out,
                stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            ACTIVE_PROCESS["pid"] = proc.pid
            proc.wait()
        ACTIVE_PROCESS["running"] = False

    asyncio.get_event_loop().run_in_executor(None, _run)

    return {
        "success": True,
        "message": f"Đã khởi chạy tiến trình điều phối (Limit: {limit}, Provider: {provider_mode})!",
        "log_file": str(log_file),
    }


# =====================================================================
# HTML Dashboard Interface
# =====================================================================

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>PI/VF Studio Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #0f172a;
      --card-bg: #1e293b;
      --card-border: #334155;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --primary: #3b82f6;
      --primary-hover: #2563eb;
      --success: #10b981;
      --warning: #f59e0b;
      --danger: #ef4444;
      --accent: #8b5cf6;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); padding: 24px; min-height: 100vh; }
    .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--card-border); }
    .title { font-size: 24px; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 10px; }
    .badge { padding: 4px 10px; border-radius: 9999px; font-size: 12px; font-weight: 600; text-transform: uppercase; }
    .badge-success { background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid #10b981; }
    .badge-warning { background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid #f59e0b; }
    .badge-danger { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid #ef4444; }
    .badge-blue { background: rgba(59, 130, 246, 0.2); color: #60a5fa; border: 1px solid #3b82f6; }
    
    .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }
    .stat-card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 12px; padding: 16px; }
    .stat-val { font-size: 28px; font-weight: 700; margin-top: 8px; color: #fff; }
    .stat-label { font-size: 13px; color: var(--text-muted); }

    .tabs { display: flex; gap: 8px; margin-bottom: 20px; border-bottom: 1px solid var(--card-border); padding-bottom: 8px; }
    .tab-btn { background: transparent; border: none; color: var(--text-muted); padding: 8px 16px; font-size: 14px; font-weight: 600; cursor: pointer; border-radius: 6px; }
    .tab-btn.active { background: var(--primary); color: #fff; }

    .card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 12px; padding: 20px; margin-bottom: 20px; }
    .product-list { display: flex; flex-direction: column; gap: 12px; }
    .product-item { background: #0f172a; border: 1px solid var(--card-border); border-radius: 8px; padding: 16px; display: flex; justify-content: space-between; align-items: center; }
    .product-info { flex: 1; margin-right: 16px; }
    .product-name { font-weight: 600; font-size: 15px; margin-bottom: 4px; color: #fff; }
    .product-desc { font-size: 13px; color: var(--text-muted); line-height: 1.4; }
    .product-actions { display: flex; gap: 8px; align-items: center; }
    
    .btn { padding: 8px 14px; border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer; border: none; transition: 0.2s; display: inline-flex; align-items: center; gap: 6px; text-decoration: none; }
    .btn-primary { background: var(--primary); color: #fff; }
    .btn-primary:hover { background: var(--primary-hover); }
    .btn-success { background: var(--success); color: #fff; }
    .btn-warning { background: var(--warning); color: #000; }
    .btn-secondary { background: #334155; color: #fff; }
    .btn-secondary:hover { background: #475569; }

    .video-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 20px; }
    .video-card { background: #0f172a; border: 1px solid var(--card-border); border-radius: 10px; overflow: hidden; display: flex; flex-direction: column; }
    .video-player { width: 100%; aspect-ratio: 9/16; max-height: 480px; background: #000; object-fit: contain; }
    .video-card-body { padding: 14px; flex: 1; display: flex; flex-direction: column; justify-content: space-between; }

    .modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.7); z-index: 100; align-items: center; justify-content: center; padding: 20px; }
    .modal-content { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 12px; width: 100%; max-width: 500px; padding: 24px; position: relative; }
    .close-modal { position: absolute; top: 16px; right: 16px; cursor: pointer; font-size: 20px; color: var(--text-muted); }
    
    .code-box { background: #090d16; border: 1px solid #1e293b; border-radius: 6px; padding: 12px; font-family: monospace; font-size: 12px; color: #38bdf8; overflow-x: auto; margin-top: 8px; word-break: break-all; }
  </style>
</head>
<body>

  <div class="header">
    <div>
      <div class="title">
        🎬 PI/VF Studio Dashboard
        <span class="badge badge-success" id="status-badge">ONLINE</span>
      </div>
      <div style="font-size: 13px; color: var(--text-muted); margin-top: 4px;" id="bangkok-clock">
        Giờ Bangkok: Đang tải...
      </div>
    </div>
    <div style="display: flex; gap: 10px;">
      <button class="btn btn-secondary" onclick="loadData()">🔄 Làm mới</button>
      <button class="btn btn-primary" onclick="openTriggerModal()">🚀 Chạy Tiếp Pipeline</button>
    </div>
  </div>

  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-label">Video Hoàn Tất Hôm Nay</div>
      <div class="stat-val" id="stat-today">0 / 20</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Tổng Video Đã Xuất</div>
      <div class="stat-val" id="stat-total">0</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Tiến Trình Ngầm</div>
      <div class="stat-val" style="font-size: 18px; margin-top: 14px;" id="stat-worker">SẴN SÀNG</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Chế Độ Provider</div>
      <div class="stat-val" style="font-size: 18px; margin-top: 14px; color: #60a5fa;">Google Flow (Configured)</div>
    </div>
  </div>

  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('tab-products')">📋 Sản Phẩm & Điều Phối</button>
    <button class="tab-btn" onclick="switchTab('tab-videos')">🎥 Thư Viện Video POV</button>
    <button class="tab-btn" onclick="switchTab('tab-commands')">🤖 Lệnh Điều Phối Agent</button>
  </div>

  <!-- TAB 1: SẢN PHẨM -->
  <div id="tab-products">
    <div class="card">
      <h3 style="margin-bottom: 16px;">Danh Sách Sản Phẩm Hôm Nay (Google Sheet Intake)</h3>
      <div class="product-list" id="product-container">
        <div style="color: var(--text-muted); padding: 20px; text-align: center;">Đang tải danh sách sản phẩm...</div>
      </div>
    </div>
  </div>

  <!-- TAB 2: VIDEO -->
  <div id="tab-videos" style="display: none;">
    <div class="video-grid" id="video-container">
      <div style="color: var(--text-muted); padding: 20px; text-align: center;">Đang tải video...</div>
    </div>
  </div>

  <!-- TAB 3: COMMANDS -->
  <div id="tab-commands" style="display: none;">
    <div class="card">
      <h3 style="margin-bottom: 12px;">Lệnh Điều Phối Cho Agent (Antigravity / Codex)</h3>
      <p style="font-size: 13px; color: var(--text-muted); margin-bottom: 16px;">
        Click vào nút Copy để gửi thẳng lệnh cho Antigravity hoặc dán vào Terminal để chạy nhanh:
      </p>

      <div style="margin-bottom: 16px;">
        <strong>1. Cứu Cảnh & Ghép Lại SEAVY Mực Rim Me (Scene 2 & 3):</strong>
        <div class="code-box" id="cmd-rescue">.\\apps\\vf\\.venv\\Scripts\\python.exe -m video_factory.cli recover-keyframe --workflow-id pv-daily-ef695010df5b0aa2</div>
        <button class="btn btn-secondary" style="margin-top: 6px;" onclick="copyText('cmd-rescue')">📋 Copy Lệnh</button>
      </div>

      <div style="margin-bottom: 16px;">
        <strong>2. Thay Thế Ảnh Sạch Cho Sản Phẩm Bị Lỗi (replace-source-images):</strong>
        <div class="code-box" id="cmd-replace">.\\apps\\vf\\.venv\\Scripts\\python.exe -m video_factory.cli replace-source-images &lt;product_key&gt; --image &lt;path_to_clean_image.jpg&gt; --reason "Operator uploaded clean packaging image"</div>
        <button class="btn btn-secondary" style="margin-top: 6px;" onclick="copyText('cmd-replace')">📋 Copy Mẫu Lệnh</button>
      </div>

      <div style="margin-bottom: 16px;">
        <strong>3. Chạy Tiếp Daily Trigger (Google Flow Thật, QC Low, Bù sản lượng):</strong>
        <div class="code-box" id="cmd-trigger-cli">.\\apps\\vf\\.venv\\Scripts\\python.exe -m automation.cli trigger --limit 8 --provider-mode configured --qc-level low --force</div>
        <button class="btn btn-secondary" style="margin-top: 6px;" onclick="copyText('cmd-trigger-cli')">📋 Copy Lệnh</button>
      </div>
    </div>
  </div>

  <!-- MODAL: UPLOAD ẢNH ĐÃ SỬA -->
  <div class="modal" id="upload-modal">
    <div class="modal-content">
      <span class="close-modal" onclick="closeModal('upload-modal')">&times;</span>
      <h3 style="margin-bottom: 12px;">Upload Ảnh Sạch / Đã Chỉnh Sửa</h3>
      <p style="font-size: 13px; color: var(--text-muted); margin-bottom: 16px;">
        Tải lên 1-5 ảnh thực tế của bao bì (đã xóa watermark/banner). Hệ thống sẽ nạp vào hồ sơ sản phẩm để Video Factory chạy tiếp:
      </p>
      <form id="upload-form" onsubmit="submitFixedImages(event)">
        <input type="hidden" id="upload-batch-id" name="batch_id">
        <input type="hidden" id="upload-row-id" name="row_id">
        <div style="margin-bottom: 14px;">
          <label style="font-size: 12px; font-weight: 600; color: var(--text-muted); display: block; margin-bottom: 6px;">Chọn file ảnh (JPG, PNG):</label>
          <input type="file" id="upload-files" name="files" multiple accept="image/*" style="width: 100%; color: #fff;" required>
        </div>
        <div style="display: flex; justify-content: flex-end; gap: 8px;">
          <button type="button" class="btn btn-secondary" onclick="closeModal('upload-modal')">Hủy</button>
          <button type="submit" class="btn btn-primary">Lưu & Mở Lại Sản Phẩm</button>
        </div>
      </form>
    </div>
  </div>

  <!-- MODAL: TRIGGER PIPELINE -->
  <div class="modal" id="trigger-modal">
    <div class="modal-content">
      <span class="close-modal" onclick="closeModal('trigger-modal')">&times;</span>
      <h3 style="margin-bottom: 12px;">Khởi Chạy Pipeline Điều Phối</h3>
      <form id="trigger-form" onsubmit="submitTriggerPipeline(event)">
        <div style="margin-bottom: 14px;">
          <label style="font-size: 12px; font-weight: 600; color: var(--text-muted); display: block; margin-bottom: 6px;">Số lượng video muốn xuất (Limit):</label>
          <input type="number" id="trigger-limit" value="1" min="1" max="15" style="width: 100%; padding: 8px; border-radius: 6px; background: #0f172a; border: 1px solid var(--card-border); color: #fff;">
        </div>
        <div style="margin-bottom: 14px;">
          <label style="font-size: 12px; font-weight: 600; color: var(--text-muted); display: block; margin-bottom: 6px;">Provider Mode:</label>
          <select id="trigger-mode" style="width: 100%; padding: 8px; border-radius: 6px; background: #0f172a; border: 1px solid var(--card-border); color: #fff;">
            <option value="configured">Google Flow (Thật)</option>
            <option value="fake">Fake (Mock kiểm thử offline)</option>
          </select>
        </div>
        <div style="display: flex; justify-content: flex-end; gap: 8px;">
          <button type="button" class="btn btn-secondary" onclick="closeModal('trigger-modal')">Hủy</button>
          <button type="submit" class="btn btn-primary">Bắt Đầu Chạy</button>
        </div>
      </form>
    </div>
  </div>

  <script>
    let currentData = null;

    async function loadData() {
      try {
        const [statusRes, prodRes] = await Promise.all([
          fetch('/api/status').then(r => r.json()),
          fetch('/api/products').then(r => r.json())
        ]);
        currentData = { status: statusRes, products: prodRes };
        renderStatus(statusRes);
        renderProducts(prodRes);
        renderVideos(prodRes.exports || []);
      } catch (err) {
        console.error("Error loading data:", err);
      }
    }

    function renderStatus(s) {
      document.getElementById('bangkok-clock').innerText = `Giờ Bangkok: ${s.bangkok_time}`;
      document.getElementById('stat-today').innerText = `${s.today_completed_count} / ${s.daily_limit}`;
      document.getElementById('stat-total').innerText = s.total_exports_count;
      const workerEl = document.getElementById('stat-worker');
      if (s.worker_running) {
        workerEl.innerText = "ĐANG CHẠY...";
        workerEl.style.color = "#f59e0b";
      } else {
        workerEl.innerText = "SẴN SÀNG";
        workerEl.style.color = "#10b981";
      }
    }

    function renderProducts(p) {
      const container = document.getElementById('product-container');
      const items = p.intake_products || [];
      if (!items.length) {
        container.innerHTML = '<div style="color: var(--text-muted); padding: 20px; text-align: center;">Chưa có sản phẩm nào cho ngày hôm nay.</div>';
        return;
      }
      container.innerHTML = items.map(item => {
        let badgeClass = 'badge-warning';
        let statusText = item.status || 'Chờ';
        if (item.status === 'completed') {
          badgeClass = 'badge-success';
          statusText = 'HOÀN TẤT';
        } else if (item.status === 'skipped') {
          badgeClass = 'badge-danger';
          statusText = 'BỎ QUA / SKIP';
        }

        let actions = '';
        if (item.status === 'skipped' || item.status === 'failed') {
          actions = `
            <button class="btn btn-secondary" onclick="openUploadModal('${item.batch_id}', '${item.row_id}')">📤 Upload Ảnh Sửa</button>
            <button class="btn btn-primary" onclick="reopenProduct('${item.batch_id}', '${item.row_id}')">🔄 Mở Lại</button>
          `;
        } else if (item.name && item.name.includes('Mực')) {
          actions = `
            <span class="badge badge-warning" style="margin-right: 8px;">CẦN CỨU CẢNH</span>
            <button class="btn btn-warning" onclick="copyText('cmd-rescue')">⚡ Cứu Cảnh Bị Lỗi</button>
          `;
        }

        return `
          <div class="product-item">
            <div class="product-info">
              <div class="product-name">
                <span class="badge ${badgeClass}" style="margin-right: 8px;">${statusText}</span>
                ${item.row_id}: ${item.name}
              </div>
              <div class="product-desc">
                ${item.reason ? `<strong>Lý do:</strong> ${item.reason}<br>` : ''}
                ${item.requested_variant ? `<strong>Biến thể yêu cầu:</strong> ${item.requested_variant} | ` : ''}
                ${item.source_url ? `<a href="${item.source_url}" target="_blank" style="color: #60a5fa; text-decoration: none;">🔗 Xem Link Sản Phẩm</a>` : ''}
              </div>
            </div>
            <div class="product-actions">
              ${actions}
            </div>
          </div>
        `;
      }).join('');
    }

    function renderVideos(exports) {
      const container = document.getElementById('video-container');
      if (!exports.length) {
        container.innerHTML = '<div style="color: var(--text-muted); padding: 20px; text-align: center;">Chưa có video nào.</div>';
        return;
      }
      container.innerHTML = exports.map(v => `
        <div class="video-card">
          <video class="video-player" controls preload="metadata">
            <source src="/api/video-stream/${v.export_id}" type="video/mp4">
            Trình duyệt không hỗ trợ thẻ video.
          </video>
          <div class="video-card-body">
            <div>
              <div style="font-weight: 700; font-size: 15px; margin-bottom: 6px; color: #fff;">${v.product_name}</div>
              <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 8px;">
                ⏱️ ${v.duration_seconds}s | 📐 ${v.resolution} | 💾 ${v.file_size_mb} MB | 📅 ${v.created_at}
              </div>
            </div>
            <div style="display: flex; gap: 6px; margin-top: 10px;">
              <a href="/api/video-stream/${v.export_id}" download="${v.export_id}.mp4" class="btn btn-secondary" style="flex: 1; text-align: center; justify-content: center;">⬇️ Tải Video</a>
              ${v.video_hub_path ? `<button class="btn btn-secondary" onclick="alert('Thư mục Video AI Hub:\\n${v.video_hub_path.replace(/\\\\/g, '\\\\\\\\')}')">📁 Hub</button>` : ''}
            </div>
          </div>
        </div>
      `).join('');
    }

    function switchTab(tabId) {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      event.target.classList.add('active');
      ['tab-products', 'tab-videos', 'tab-commands'].forEach(id => {
        document.getElementById(id).style.display = (id === tabId) ? 'block' : 'none';
      });
    }

    function openUploadModal(batchId, rowId) {
      document.getElementById('upload-batch-id').value = batchId;
      document.getElementById('upload-row-id').value = rowId;
      document.getElementById('upload-modal').style.display = 'flex';
    }

    function openTriggerModal() {
      document.getElementById('trigger-modal').style.display = 'flex';
    }

    function closeModal(modalId) {
      document.getElementById(modalId).style.display = 'none';
    }

    async function reopenProduct(batchId, rowId) {
      if (!confirm(`Bạn có chắc muốn mở lại sản phẩm ${rowId} để hệ thống kiểm tra và chạy tiếp?`)) return;
      const form = new FormData();
      form.append('batch_id', batchId);
      form.append('row_id', rowId);
      const res = await fetch('/api/action/reopen-product', { method: 'POST', body: form });
      const data = await res.json();
      alert(data.message || 'Thao tác thành công');
      loadData();
    }

    async function submitFixedImages(e) {
      e.preventDefault();
      const form = new FormData(document.getElementById('upload-form'));
      try {
        const res = await fetch('/api/action/upload-fixed-images', { method: 'POST', body: form });
        const data = await res.json();
        alert(data.message || 'Đã lưu ảnh sạch thành công!');
        closeModal('upload-modal');
        loadData();
      } catch (err) {
        alert('Lỗi upload: ' + err);
      }
    }

    async function submitTriggerPipeline(e) {
      e.preventDefault();
      const form = new FormData();
      form.append('limit', document.getElementById('trigger-limit').value);
      form.append('provider_mode', document.getElementById('trigger-mode').value);
      form.append('qc_level', 'low');
      try {
        const res = await fetch('/api/action/trigger-pipeline', { method: 'POST', body: form });
        const data = await res.json();
        alert(data.message || 'Pipeline đã khởi chạy ngầm!');
        closeModal('trigger-modal');
        loadData();
      } catch (err) {
        alert('Lỗi khởi chạy: ' + err);
      }
    }

    function copyText(elemId) {
      const text = document.getElementById(elemId).innerText;
      navigator.clipboard.writeText(text);
      alert('Đã copy lệnh vào Clipboard! Bạn có thể dán vào Antigravity hoặc Terminal.');
    }

    // Auto-refresh every 15s
    loadData();
    setInterval(loadData, 15000);
  </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(content=DASHBOARD_HTML)


def run_dashboard(host: str = "127.0.0.1", port: int = 8080):
    """Start uvicorn server for the dashboard."""
    import uvicorn
    print(f"=== PI/VF Operator Studio Dashboard ===")
    print(f"Truy cap tai: http://{host}:{port}")
    print(f"Nhan Ctrl+C de dung server.\n")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_dashboard()
