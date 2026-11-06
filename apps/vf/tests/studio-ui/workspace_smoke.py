"""Offline browser evidence. Never talks to the production application."""
import io
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from PIL import Image
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
REPORT = Path(os.environ.get("VF_STUDIO_REPORT_DIR", str(ROOT / "docs/reports/studio-redesign-20261026")))
BASE = "http://127.0.0.1:18743"


def main():
    REPORT.mkdir(parents=True, exist_ok=True)
    server = subprocess.Popen(["node", str(ROOT / "tests/studio-ui/fixture-server.mjs"), "--port=18743"], cwd=ROOT, stdout=subprocess.DEVNULL)
    trace, errors = [], []
    try:
        for _ in range(40):
            try:
                urllib.request.urlopen(BASE + "/operator/assets/studio.html", timeout=1).close()
                break
            except OSError:
                time.sleep(.2)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: trace.append({"method": request.method, "url": request.url, "body": request.post_data}))

            def scenario(name):
                context.request.post(BASE + "/__scenario", data={"scenario": name})

            def open_page(route="products", name="happy"):
                scenario(name)
                page.goto(BASE + f"/operator/assets/studio.html?fixture={time.time_ns()}#" + route)
                page.wait_for_timeout(400)

            def posts():
                return [r for r in trace if r["method"] == "POST" and "/api/" in r["url"]]

            def screenshot(name):
                page.screenshot(path=REPORT / name, full_page=True)

            def no_overflow():
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), "Horizontal page overflow"

            def contrast_check():
                result = page.evaluate("""() => {
                  const rgba = color => {
                    const values = color.match(/[\\d.]+/g)?.map(Number) || [0, 0, 0, 0];
                    return [...values.slice(0, 3), values[3] ?? 1];
                  };
                  const blend = (top, bottom) => top.slice(0, 3).map((v, i) => v * top[3] + bottom[i] * (1 - top[3])).concat(1);
                  const background = element => {
                    const ancestors = []; for (let n = element; n; n = n.parentElement) ancestors.unshift(n);
                    return ancestors.reduce((bg, n) => blend(rgba(getComputedStyle(n).backgroundColor), bg), [255, 255, 255, 1]);
                  };
                  const luminance = color => color.slice(0, 3).map(v => { v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }).reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
                  const failures = []; let checked = 0;
                  for (const el of document.body.querySelectorAll('*')) {
                    if (!el.getClientRects().length || el.closest('[hidden], button:disabled') || ![...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) continue;
                    const style = getComputedStyle(el); if (style.visibility === 'hidden') continue;
                    const bg = background(el), fg = blend(rgba(style.color), bg);
                    const a = luminance(bg), b = luminance(fg), ratio = (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
                    const large = parseFloat(style.fontSize) >= 24 || (parseFloat(style.fontSize) >= 18.66 && parseInt(style.fontWeight) >= 700);
                    checked++; if (ratio + .01 < (large ? 3 : 4.5)) failures.push({ text: el.textContent.trim().slice(0, 60), ratio: +ratio.toFixed(2), color: style.color, background: bg });
                  }
                  return { theme: document.body.dataset.theme || 'dark', checked, failures };
                }""")
                assert not result["failures"], result
                return result

            open_page()
            page.locator(".vf-product-row").first.wait_for()
            expect(page.locator('body')).to_have_attribute('data-theme', 'light')
            expect(page.locator('body')).to_have_css('background-color', 'rgb(248, 250, 252)')
            assert 'Segoe UI' in page.locator('body').evaluate('el => getComputedStyle(el).fontFamily')
            expect(page.locator('.vf-list-heading .vf-btn-primary')).to_have_css('background-color', 'rgb(109, 40, 217)')
            assert page.get_by_role("button", name="Cần xử lý").get_attribute("aria-pressed") == "true"
            assert not posts(), "Initial page must not submit actions"
            screenshot("01-product-list.png")
            contrast_results = [contrast_check()]
            page.get_by_label("Tìm sản phẩm").fill("khong-co-san-pham")
            assert page.locator(".vf-product-row").count() == 0
            page.get_by_label("Tìm sản phẩm").fill("")
            page.get_by_role("button", name="Đã tạo video").click()
            assert page.locator("a[href^='javascript:']").count() == 0
            assert not posts()

            open_page("products/workflow/wf1", "blocked")
            page.locator(".vf-workflow-workspace").wait_for()
            assert page.locator("section#detail-modal").is_visible()
            assert page.locator("dialog#detail-modal").count() == 0
            assert page.locator(".vf-step-button").count() == 10
            assert "variant" in page.locator(".vf-blocked-banner").inner_text()
            assert page.get_by_text("Chưa có ảnh nguồn.").is_visible()
            no_overflow()
            screenshot("02-workflow-blocked.png")
            contrast_results.append(contrast_check())
            page.locator("#tab-btn-overview").focus()
            page.keyboard.press("ArrowRight")
            assert page.locator("#tab-btn-scenes").get_attribute("aria-selected") == "true"
            page.keyboard.press("ArrowLeft")
            assert page.locator("#tab-btn-overview").get_attribute("aria-selected") == "true"
            page.get_by_role("button", name="Chạy lại từ bước…").click()
            page.get_by_label("Bước chạy lại").select_option("VIDEO_RENDER")
            expect(page.locator(".vf-rerun-dialog")).to_contain_text("Không có command")
            assert not posts()
            page.locator(".vf-rerun-dialog").get_by_role("button", name="Đóng", exact=True).click()

            image_bytes = io.BytesIO()
            Image.new("RGB", (100, 160), "#335577").save(image_bytes, format="PNG")
            upload = {"name": "anh-dung-sku.png", "mimeType": "image/png", "buffer": image_bytes.getvalue()}
            page.get_by_label("Chọn ảnh sản phẩm mẫu").set_input_files([upload])
            assert page.locator(".vf-upload-item").count() == 1
            screenshot("03-image-upload-preview.png")
            page.get_by_role("button", name="Lưu input & kiểm tra lại", exact=True).click()
            assert "Snapshot resource_pack cũ" in page.locator(".vf-rerun-dialog").inner_text()
            page.get_by_role("button", name="Xác nhận & chạy lại", exact=True).click()
            page.wait_for_timeout(400)
            upload_posts = [r for r in posts() if r["url"].endswith("/source-input")]
            assert len(upload_posts) == 1
            payload = json.loads(upload_posts[0]["body"])
            assert payload["expected_version"] == 2
            assert len(payload["uploaded_images"]) == 1
            assert "image_paths" not in payload

            # Unknown mutation stays blocked after reload; selecting files never replays it.
            open_page("products/workflow/wf1", "upload-unknown")
            page.get_by_label("Chọn ảnh sản phẩm mẫu").set_input_files([upload])
            page.get_by_role("button", name="Lưu input & kiểm tra lại", exact=True).click()
            page.get_by_role("button", name="Xác nhận & chạy lại", exact=True).click()
            page.wait_for_timeout(300)
            assert page.evaluate("sessionStorage.getItem('vf-studio.pending-intent.v1')")
            count = len(posts()); page.reload(); page.wait_for_timeout(300)
            assert len(posts()) == count
            page.evaluate("sessionStorage.clear()")

            # Edit the current image set; selecting files must not discard old refs.
            def source_media(route):
                pixels = io.BytesIO()
                Image.new("RGB", (80, 120), "blue" if "blue" in route.request.url else "red").save(pixels, format="PNG")
                route.fulfill(body=pixels.getvalue(), content_type="image/png")
            page.route("**/fixture-media/*.png", source_media)
            def editor_detail(route):
                route.fulfill(json={
                    "workflow_id": "wf-editor", "product_name": "Snack fixture", "state": "skipped", "version": 2,
                    "failure_code": "IMAGE_QC_FAILED", "failure_reason": "Ảnh nguồn sai variant",
                    "source_context": {"source": "hermes_product_video_exchange", "name": "Snack fixture",
                        "brand": "Brand", "model": "Snack", "variant": "Original", "images": [
                            {"file_name": "old-red.png", "sha256": "a" * 64, "asset_url": "/fixture-media/red.png"},
                            {"file_name": "old-blue.png", "sha256": "b" * 64, "asset_url": "/fixture-media/blue.png"}]},
                    "pipeline_steps": [{"step_name": "IMAGE_QC", "status": "failed"}]})
            page.route("**/api/operator/workflow/wf-editor", editor_detail)
            open_page("products/workflow/wf-editor", "blocked")
            editor = page.locator("#source-image-editor")
            expect(editor.locator(".vf-upload-item")).to_have_count(2)
            page.wait_for_function("[...document.querySelectorAll('#source-image-editor img')].every(img => img.naturalWidth > 0)")
            assert editor.get_by_role("button", name="Lưu input & kiểm tra lại", exact=True).is_disabled()
            count = len(posts())
            with page.expect_response("**/api/operator/workflow/wf-editor"):
                page.get_by_role("button", name="Làm mới trạng thái", exact=True).click()
            assert len(posts()) == count
            page.get_by_label("Chọn ảnh sản phẩm mẫu").set_input_files([upload])
            expect(editor.locator(".vf-upload-item")).to_have_count(3)
            editor.get_by_role("button", name="Bỏ ảnh old-red.png", exact=True).click()
            expect(editor.locator(".vf-upload-item")).to_have_count(2)
            editor.get_by_label("Tên sản phẩm", exact=True).fill("Snack đã xác minh")
            editor.get_by_label("Lý do sửa input", exact=True).fill("Lý do nháp chưa lưu")
            editor.get_by_role("button", name="Hủy thay đổi", exact=True).click()
            expect(editor.locator(".vf-upload-item")).to_have_count(2)
            expect(editor.get_by_label("Tên sản phẩm", exact=True)).to_have_value("Snack fixture")
            expect(editor.get_by_label("Lý do sửa input", exact=True)).not_to_have_value("Lý do nháp chưa lưu")
            assert len(posts()) == count
            # Removing all is only a draft and cannot submit an empty set.
            editor.get_by_role("button", name="Bỏ ảnh old-red.png", exact=True).click()
            editor.get_by_role("button", name="Bỏ ảnh old-blue.png", exact=True).click()
            assert editor.get_by_role("button", name="Lưu input & kiểm tra lại", exact=True).is_disabled()
            editor.get_by_role("button", name="Hủy thay đổi", exact=True).click()
            page.get_by_label("Chọn ảnh sản phẩm mẫu").set_input_files([upload])
            editor.get_by_role("button", name="Bỏ ảnh old-red.png", exact=True).click()
            editor.get_by_role("button", name="Chọn anh-dung-sku.png làm ảnh chính", exact=True).click()
            editor.get_by_label("Tên sản phẩm", exact=True).fill("Snack đã xác minh")
            editor.get_by_label("Variant / phiên bản", exact=True).fill("Blue")
            screenshot("12-image-editor-draft.png")
            editor.get_by_role("button", name="Lưu input & kiểm tra lại", exact=True).click()
            page.get_by_role("button", name="Xác nhận & chạy lại", exact=True).click()
            expect(page.locator(".vf-rerun-dialog")).to_have_count(0)
            page.wait_for_timeout(400)
            assert len(posts()) == count + 1
            saved = json.loads(posts()[-1]["body"])
            assert saved["image_selection"] == [{"upload_index": 0}, {"sha256": "b" * 64}]
            assert saved["product_info"] == {"name": "Snack đã xác minh", "variant": "Blue"}
            assert len(saved["uploaded_images"]) == 1 and saved["expected_version"] == 2
            page.unroute("**/api/operator/workflow/wf-editor", editor_detail)

            # A real command button now calls the fixed backend action contract.
            open_page("products/workflow/wf1", "command")
            def blocked_actions(route):
                route.fulfill(json={"version": 3, "options": [{
                    "id": "run-pending:1:0", "steps": ["KEYFRAME_GENERATION"],
                    "label": "Chạy job đang chờ", "command": "creative-batch",
                    "effect": "Tiếp tục job cũ", "enabled": False,
                    "reason": "QC_OUTCOME_UNCERTAIN: Chưa rõ kết quả QC; đối soát request hiện hữu trước khi tiếp tục."
                }], "unavailable_reasons": {"EXPORT": "Chưa có MP4 để xuất."}})
            page.route("**/api/operator/workflow/wf1/actions", blocked_actions)
            count = len(posts())
            page.get_by_role("button", name="Chạy lại từ bước…").click()
            alert = page.locator(".vf-rerun-dialog [role=alert]")
            alert.wait_for(timeout=3000)
            assert "QC_OUTCOME_UNCERTAIN" in alert.inner_text()
            assert "đối soát" in alert.inner_text()
            expect(alert).to_have_css("color", "rgb(190, 18, 60)")
            assert alert.bounding_box()["y"] < page.get_by_label("Bước chạy lại").bounding_box()["y"]
            assert page.get_by_role("button", name="Xác nhận & chạy", exact=True).is_disabled()
            page.get_by_label("Bước chạy lại").select_option("EXPORT")
            assert "Chưa có MP4" in alert.inner_text()
            assert len(posts()) == count
            page.locator(".vf-rerun-dialog").get_by_role("button", name="Đóng", exact=True).click()
            page.unroute("**/api/operator/workflow/wf1/actions", blocked_actions)

            open_page("products/workflow/wf1", "command")
            assert "Tạo keyframe" in page.locator(".vf-blocked-banner").inner_text()
            count = len(posts())
            page.get_by_role("button", name="Chạy tiếp từ bước này", exact=True).click()
            page.get_by_role("button", name="Xác nhận & chạy", exact=True).wait_for(state="visible")
            expect(page.get_by_label("Bước chạy lại")).to_have_value("KEYFRAME_GENERATION")
            page.wait_for_timeout(150)
            assert page.get_by_role("button", name="Xác nhận & chạy", exact=True).is_enabled()
            assert "recover-keyframe" in page.locator(".vf-rerun-dialog").inner_text()
            screenshot("09-command-confirmation.png")
            page.get_by_role("button", name="Xác nhận & chạy", exact=True).click()
            page.wait_for_timeout(300)
            assert len(posts()) == count + 1
            body = json.loads(posts()[-1]["body"])
            assert body["action_id"] == "recover-keyframe:operation-a" and body["expected_version"] == 3
            assert len(body["request_id"]) == 36 and body["note"]
            assert "Đã tiếp nhận command" in page.locator(".vf-rerun-dialog").inner_text()
            screenshot("10-command-accepted.png")
            page.locator(".vf-rerun-dialog").get_by_role("button", name="Đóng", exact=True).click()

            open_page("products/workflow/wf1", "command-stale")
            count = len(posts())
            page.get_by_role("button", name="Chạy lại từ bước…").click()
            page.wait_for_timeout(150)
            page.get_by_role("button", name="Xác nhận & chạy", exact=True).click()
            page.wait_for_timeout(250)
            assert len(posts()) == count
            assert "đã thay đổi" in page.locator(".vf-rerun-dialog").inner_text()
            page.locator(".vf-rerun-dialog").get_by_role("button", name="Đóng", exact=True).click()

            open_page("products/workflow/wf1", "command-unknown")
            page.get_by_role("button", name="Chạy lại từ bước…").click()
            page.wait_for_timeout(150)
            page.get_by_role("button", name="Xác nhận & chạy", exact=True).click()
            page.wait_for_timeout(250)
            assert page.evaluate("JSON.parse(sessionStorage.getItem('vf-studio.pending-intent.v1')).kind") == "command"
            count = len(posts()); page.reload(); page.wait_for_timeout(250)
            assert len(posts()) == count
            page.set_viewport_size({"width": 375, "height": 900})
            no_overflow()
            screenshot("11-command-receipt-mobile.png")
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.get_by_role("button", name="Kiểm tra receipt", exact=True).click()
            page.wait_for_timeout(200)
            assert not page.evaluate("sessionStorage.getItem('vf-studio.pending-intent.v1')")
            assert len(posts()) == count, "Receipt reconciliation is GET-only"

            open_page("products", "many")
            assert page.locator(".vf-product-row").count() == 30
            assert "350 sản phẩm" in page.locator(".vf-pagination").inner_text()
            page.get_by_role("button", name="Sau →", exact=True).click()
            assert "Trang 2/12" in page.locator(".vf-pagination").inner_text()
            screenshot("04-many-products.png")
            for width in [375, 768, 1440]:
                page.set_viewport_size({"width": width, "height": 900})
                open_page("products/workflow/wf1", "blocked")
                no_overflow()
                screenshot(f"05-detail-{width}.png")
                open_page("products", "many")
                no_overflow()
                screenshot(f"06-list-{width}.png")
            page.set_viewport_size({"width": 1440, "height": 1000})

            # Sorting, bounded selection, irreversible confirmation, partial result.
            deleted = set()
            table_rows = [
                {"workflow_id":"delete-a","product_name":"Bánh 10","state":"AwaitingProductLock","version":1,"created_at":"2026-10-24T00:00:00Z","updated_at":"2026-11-01T00:00:00Z"},
                {"workflow_id":"delete-b","product_name":"Áo","state":"skipped","version":1,"created_at":"2026-10-25T00:00:00Z","updated_at":"2026-10-26T00:00:00Z"}]
            def deletion_board(route):
                route.fulfill(json={"columns":{"error":[r for r in table_rows if r['workflow_id'] not in deleted]}})
            def deletion_post(route):
                sent = route.request.post_data_json
                assert sent['confirmation'] == 'XÓA VĨNH VIỄN'
                assert {r['workflow_id'] for r in sent['workflows']} == {'delete-a','delete-b'}
                assert all(r['expected_version'] == 1 for r in sent['workflows'])
                deleted.add('delete-b')
                route.fulfill(json={"ok":True,"results":[{"workflow_id":"delete-a","status":"blocked","reason":"Operation chưa rõ kết quả","deleted_files":[]},{"workflow_id":"delete-b","status":"deleted","deleted_files":["fixture.mp4"]}]})
            page.route('**/api/operator/kanban', deletion_board)
            page.route('**/api/operator/workflows/delete', deletion_post)
            open_page('production','happy')
            expect(page.locator('tbody tr')).to_have_count(2)
            expect(page.locator('tbody tr').first).to_contain_text('Bánh 10')
            page.get_by_role('columnheader').filter(has_text='Sản phẩm').get_by_role('button').click()
            expect(page.locator('tbody tr').first).to_contain_text('Áo')
            page.get_by_role('columnheader').filter(has_text='Sản phẩm').get_by_role('button').click()
            expect(page.locator('tbody tr').first).to_contain_text('Bánh 10')
            assert '07:00:00' in page.locator('tbody').inner_text()
            count=len(posts())
            page.get_by_label('Chọn tất cả hàng đang hiển thị',exact=True).check()
            page.get_by_placeholder('Tìm theo workflow ID, tên sản phẩm...').fill('Bánh')
            page.get_by_label('Chọn tất cả hàng đang hiển thị',exact=True).uncheck()
            expect(page.get_by_role('button',name='Xóa vĩnh viễn (1)',exact=True)).to_be_enabled()
            page.get_by_placeholder('Tìm theo workflow ID, tên sản phẩm...').fill('')
            assert page.get_by_label('Chọn tất cả hàng đang hiển thị',exact=True).evaluate('el=>el.indeterminate')
            page.get_by_label('Chọn tất cả hàng đang hiển thị',exact=True).check()
            page.get_by_role('button',name='Xóa vĩnh viễn (2)',exact=True).click()
            modal=page.get_by_role('dialog',name='Xác nhận xóa vĩnh viễn')
            assert modal.get_by_role('button',name='Xóa vĩnh viễn',exact=True).is_disabled()
            modal.get_by_role('button',name='Hủy',exact=True).click()
            assert len(posts())==count
            page.get_by_role('button',name='Xóa vĩnh viễn (2)',exact=True).click()
            modal.get_by_label('Nhập XÓA VĨNH VIỄN').fill('XÓA VĨNH VIỄN')
            screenshot('14-delete-confirmation.png')
            modal.get_by_role('button',name='Xóa vĩnh viễn',exact=True).click()
            expect(modal).to_contain_text('Đã xóa 1/2')
            expect(modal).to_contain_text('Operation chưa rõ kết quả')
            assert len(posts())==count+1
            modal.get_by_role('button',name='Đóng',exact=True).click()
            expect(page.locator('tbody tr')).to_have_count(1)
            page.unroute('**/api/operator/kanban',deletion_board)
            page.unroute('**/api/operator/workflows/delete',deletion_post)

            open_page("products", "empty")
            assert "Chưa có sản phẩm" in page.locator(".vf-empty-state").inner_text()
            screenshot("07-empty.png")

            # Auth denial stays stopped across multiple timer periods and visibility changes.
            open_page("products", "unauthorized")
            assert "phân quyền" in page.locator("#status-bar").inner_text()
            page.clock.install()
            prior = len([r for r in trace if r["method"] == "GET" and "/api/" in r["url"]])
            page.clock.fast_forward(45000)
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            page.wait_for_timeout(100)
            assert len([r for r in trace if r["method"] == "GET" and "/api/" in r["url"]]) == prior
            page.clock.resume()

            # Existing batch UI retains preflight and exact contract, including partial/unknown.
            for name in ["happy", "partial", "unknown", "network-drop"]:
                page.evaluate("sessionStorage.clear()")
                open_page("intake", name)
                checkbox = page.locator("input[type=checkbox]").nth(1)
                checkbox.check()
                previous = len(posts())
                page.locator("#btn-start-batch").click()
                page.wait_for_timeout(500)
                assert len(posts()) == previous + 1
                sent = json.loads(posts()[-1]["body"])
                assert sent["auto"] is False and sent["product_keys"] == ["p1"]
                if name in ["unknown", "network-drop"]:
                    assert page.evaluate("sessionStorage.getItem('vf-studio.pending-intent.v1')")
                    count = len(posts()); page.reload(); page.wait_for_timeout(200); assert len(posts()) == count
                if name == "partial": assert "MISSING_SOURCE" in page.locator("#status-bar").inner_text()

            # Changing the source date while preflight is pending must never submit date B.
            page.evaluate("sessionStorage.clear()")
            open_page("intake", "happy")
            held = []

            def hold_preflight(route):
                if not held:
                    held.append(route)
                else:
                    route.continue_()

            page.route("**/api/personal/product-video/candidates*", hold_preflight)
            page.locator("input[type=checkbox]").nth(1).check()
            count = len(posts())
            page.locator("#btn-start-batch").click()
            page.wait_for_timeout(100)
            assert held
            page.locator("input[type=date]").fill("2026-10-25")
            held[0].fulfill(json={"ok": True, "source_date": "2026-10-24", "candidates": [{"product_key": "p1", "name": "Sản phẩm mẫu", "brand": "Hãng mẫu", "model": "M1", "status": "unprocessed", "images": [{"asset_url": "/fixture-media/img1.jpg"}]}]})
            page.wait_for_timeout(300)
            assert len(posts()) == count, "Date changed during preflight must abort Start"
            page.unroute("**/api/personal/product-video/candidates*", hold_preflight)

            page.evaluate("sessionStorage.clear()")
            open_page("products/workflow/wf5", "changed-gate")
            count = len(posts()); page.locator("#btn-approve-gate").click(); page.wait_for_timeout(300)
            assert len(posts()) == count
            assert "thay đổi" in page.locator("#status-bar").inner_text()

            for route in ['production', 'issues', 'videos', 'intake']:
                open_page(route, 'happy')
                no_overflow()
                contrast_results.append(contrast_check())
                screenshot(f'13-{route}-light.png')

            open_page("products/workflow/wf1", "missing-data")
            assert page.locator(".vf-workflow-workspace").is_visible()
            page.get_by_role("button", name="Đổi giao diện sáng tối").click()
            expect(page.locator('body')).to_have_attribute('data-theme', 'dark')
            screenshot("08-dark-mode.png")
            contrast_results.append(contrast_check())
            open_page("products", "happy")
            expect(page.locator('body')).to_have_attribute('data-theme', 'dark')
            contrast_results.append(contrast_check())
            # A delayed detail GET shows a loading state before content arrives.
            pending_detail = []
            page.route("**/api/operator/workflow/wf-loading", lambda route: pending_detail.append(route))
            open_page("products/workflow/wf-loading", "happy")
            assert "Đang tải dữ liệu" in page.locator("#detail-body").inner_text()
            pending_detail[0].fulfill(json={"workflow_id": "wf-loading", "state": "researching", "version": 1})
            page.locator(".vf-workflow-workspace").wait_for()
            page.unroute("**/api/operator/workflow/wf-loading")
            assert not errors, errors
            (REPORT / "network-trace.json").write_text(json.dumps(trace, indent=2, ensure_ascii=False), encoding="utf-8")
            (REPORT / "contrast.json").write_text(json.dumps(contrast_results, indent=2, ensure_ascii=False), encoding="utf-8")
            browser.close()
            print("PASS: table sort/time, filtered bulk selection, typed permanent-delete confirmation, partial blocked/deleted outcome; image/input/step actions, unknown no replay, responsive/contrast, auth and Start/gate guards. Zero page errors.")
    finally:
        server.terminate()
        server.wait(timeout=5)


if __name__ == "__main__":
    main()
