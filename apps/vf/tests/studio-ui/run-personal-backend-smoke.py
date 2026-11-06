"""Real FastAPI + temporary DB + historical MP4; batch dispatch is mocked."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"

import uvicorn
from fastapi.responses import FileResponse
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from test_studio_creative_batch import _ready_two_product_batch
from test_source_input_correction import _skipped_image_workflow
from video_factory.application import candidate_api
from video_factory.application.app import create_application
from video_factory.candidate_service import DailyCandidateService
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.repository import WorkflowRepository

REPORT = ROOT / "docs/reports/personal-cleanup-20261026/browser-backend"
REPORT.mkdir(parents=True, exist_ok=True)
MEDIA = Path(os.environ["VF_HISTORICAL_MP4"])
metadata = json.loads(MEDIA.with_name("metadata.json").read_text(encoding="utf8"))
assert metadata["product_identity"] == "Lay's Stax"
expected_sha = metadata["video"]["sha256"].removeprefix("sha256:")
assert hashlib.sha256(MEDIA.read_bytes()).hexdigest() == expected_sha

with tempfile.TemporaryDirectory(prefix="vf-studio-browser-") as tmp:
    base = Path(tmp)
    inbox, keys = _ready_two_product_batch(base)
    os.environ["HERMES_EXCHANGE_INBOX"] = str(inbox)
    conn = connect(base / "studio.db")
    repo = WorkflowRepository(conn)
    repo.commit_workflow("wf-lays-history", json.dumps({
        "state": "completed", "owner_user_id": "local-owner", "product_name": "Lay's Stax",
        "source_context": {"product_key": keys["Lay's"], "brand": "Lay's", "model": "Stax"},
        "export_result": {"file_path": str(MEDIA), "sha256": expected_sha},
    }), [], expected_version=0)
    _skipped_image_workflow(conn)
    aggregate, version = repo.get_workflow("wf-input-fix")
    aggregate["source_context"].update(product_key=keys["Bibigo"], brand="Bibigo", model="Snack Rong Bien An Lien 25g")
    repo.commit_workflow("wf-input-fix", json.dumps(aggregate), [], expected_version=version)
    image = next((inbox / "2026-10-25/run_studio_test").rglob("*.png"))
    calls = []
    def run_batch(**kwargs):
        calls.append({"keys": kwargs["product_keys"], "qc_level": kwargs["qc_level"]})
        time.sleep(0.2)
        return 0
    candidate_api.run_studio_selected_batch = run_batch
    def get_candidates(self, target_date=None, qc_level="middle"):
        state = repo.get_workflow("wf-input-fix")[0]["state"]
        candidates = []
        for brand, workflow, status in [("Lay's", "wf-lays-history", "completed"), ("Bibigo", "wf-input-fix", "blocked" if state == "skipped" else "in_production")]:
            row = {"name": "Lay's Stax" if brand == "Lay's" else "Bibigo Snack", "brand": brand,
                   "model": "Stax" if brand == "Lay's" else "Snack", "variant": "Original",
                   "product_key": keys[brand], "source_date": "2026-10-25", "workflow_id": workflow,
                   "status": status, "images": [{"asset_url": "/fixture-image.png"}],
                   "rejection_reason": "EXCHANGE_IMAGE_QC_FAILED" if status == "blocked" else None}
            candidates.append(SimpleNamespace(workflow_id=workflow, source_date="2026-10-25", to_dict=lambda row=row: dict(row)))
        return candidates
    DailyCandidateService.get_candidates = get_candidates
    app = create_application(kanban_connection=conn, rate_per_minute=10000, burst=1000)
    @app.get("/fixture-image.png")
    def fixture_image():
        return FileResponse(image)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started: break
            time.sleep(0.1)
        assert server.started
        url = f"http://127.0.0.1:{port}/studio"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: (errors.append(str(error)), print("PAGE ERROR", str(error), flush=True)))
            page.on("console", lambda msg: print("CONSOLE", msg.type, msg.text, flush=True) if msg.type == "error" else None)
            page.on("requestfailed", lambda req: print("REQUEST FAILED", req.url, req.failure, flush=True))
            page.goto(url)
            page.wait_for_function("document.querySelector('#page-title').innerText === 'Sản phẩm'")
            page.get_by_text("Original", exact=False).first.wait_for()
            assert "Batch đang chạy" not in page.locator("body").inner_text()
            assert page.locator("select[aria-label='Mức QC']").input_value() == "middle"
            assert page.locator("select[aria-label='Mức QC'] option[value='high']").count() == 0
            assert page.locator("img[alt='Ảnh sản phẩm']").first.evaluate("img => img.naturalWidth > 0")
            page.screenshot(path=str(REPORT / "products.png"))
            page.locator("#nav-results").click()
            page.get_by_role("link", name="Tải MP4").wait_for(timeout=30000)
            with page.expect_download() as pending:
                page.get_by_role("link", name="Tải MP4").click()
            download = pending.value
            target = base / "downloaded.mp4"
            download.save_as(target)
            assert hashlib.sha256(target.read_bytes()).hexdigest() == expected_sha
            with page.expect_popup() as pending:
                page.get_by_role("link", name="Xem MP4").click()
            popup = pending.value
            popup.wait_for_selector("video")
            popup.wait_for_function("document.querySelector('video').readyState >= 2", timeout=30000)
            assert popup.locator("video").evaluate("v => v.duration > 0 && v.videoWidth > 0")
            popup.close()
            page.get_by_text("Sửa ảnh / thông tin đầu vào", exact=True).click()
            page.get_by_label("Đường dẫn ảnh trên máy chạy Studio (mỗi dòng một ảnh)").fill(str(image))
            page.get_by_label("Lý do sửa (bắt buộc)").fill("Confirmed correct package variant")
            page.screenshot(path=str(REPORT / "source-correction.png"))
            page.get_by_role("button", name="Lưu input đã sửa").click()
            page.wait_for_function("document.querySelector('#page-title').innerText === 'Sản phẩm'")
            checkbox = page.locator(f"input[type='checkbox'][value='{keys['Bibigo']}']")
            checkbox.wait_for()
            assert checkbox.is_enabled()
            checkbox.check()
            page.locator("#btn-start-batch").click()
            page.wait_for_function("document.querySelector('#status-bar').innerText.includes('Đã nhận yêu cầu Chạy')")
            assert len(calls) == 1
            assert calls[0] == {"keys": [keys["Bibigo"]], "qc_level": "middle"}
            page.reload()
            page.wait_for_selector(".vf-table tr")
            assert len(calls) == 1
            assert conn.execute("SELECT count(*) FROM workflow_outbox WHERE workflow_id='wf-input-fix'").fetchone()[0] == 1
            page.screenshot(path=str(REPORT / "results.png"))
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(REPORT / "mobile.png"))
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors, errors
            browser.close()
        result = {"passed": True, "historical_media_identity": metadata["product_identity"], "historical_sha256": expected_sha,
                  "download_sha_matches": True, "browser_decode": True, "source_correction": True,
                  "resume_dispatch_count": len(calls), "outbox_count_after_reload": 1, "default_qc": "middle",
                  "default_view": "products", "provider_calls": 0, "production_db_mutations": 0,
                  "scope": "Real FastAPI endpoints and browser; temporary DB, fixture candidates, mocked batch dispatch; historical media only."}
        (REPORT / "result.json").write_text(json.dumps(result, indent=2), encoding="utf8")
        print(json.dumps(result, indent=2))
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        conn.close()
