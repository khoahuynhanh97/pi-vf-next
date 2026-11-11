# CLAUDE PM SIGN-OFF — PI/VF Next
**Ngày ký:** 11/11/2026, 22:30 (Bangkok / Hanoi Time)
**Branch:** `codex/unified-pipeline`
**HEAD tại thời điểm ký:** `1e07e80`
**Reviewer:** Claude PM (Antigravity)
**Bản review gốc:** [`docs/reports/CLAUDE-PM-REVIEW-REPORT-2026-11-11.md`](./CLAUDE-PM-REVIEW-REPORT-2026-11-11.md)

---

## Tóm tắt kết quả kiểm tra

### Trạng thái mã nguồn đã kiểm tra
| Mục | Trạng thái |
|-----|-----------|
| Working tree | ✅ Clean (confirmed `git status`) |
| Commit HEAD | `1e07e80` → `54f6095` → `5e26b34` → `d392fff` |
| A/B offline test (`task-320`) | ✅ Passed: Product A `failed` với reason, Product B xuất `final.mp4` decodable, SHA256 khớp |
| Real Google Flow MP4 đã xác minh | ✅ `export_proj-pv-daily-f1bb41ca374ce799_1/final.mp4` — 1080×1920, 30fps, 32s, 12.4 MB, SHA256 `b17f94f7...` khớp `metadata.json` |
| Số video thực tế hôm nay | 5 Google Flow MP4 hợp lệ (loại trừ mock `24fac71e`) |

### Các blocker đã xác nhận đã fix (`d392fff`)
| Blocker | Fix | Bằng chứng |
|---------|-----|-----------|
| `KEYFRAME_TIMEOUT_EXPIRED` → wedge vĩnh viễn | Thêm vào retryable set, claim reconciliation chấp nhận `status='failed'` với timeout codes | `keyframe_provider.py`, `coordinator.py`, `capacity.py` |
| `_wait_for_flow_keyframe_slot` gọi `recover-keyframe --advance` → mở lại Product A khi Product B đang chạy → stall 1200s | Thay bằng direct `release_exhausted_reconciliation_slot` | `spark_pipeline.py` L1065–1135 |
| `DIRECTOR_REQUIRED_FIELD_MISSING` khi LLM bỏ qua `environment`/`lighting` | Auto-backfill từ `scene_bible.*` | `director.py` |
| `prepare_sheet_intake` không persist `selection_csv` | Persist vào `vf-sheet-policy.json` trước khi return | `spark_pipeline.py` L804, `daily_trigger.py` L146–176 |

---

## Quyết định 1 — Tiêu chí đánh giá Affiliate (QC = low)

**✅ APPROVED**

**Căn cứ:**
- AGENTS.md đã quy định rõ: `Giữ QC low, xác minh đúng sản phẩm, MP4 phát được và receipt khớp SHA256`.
- 5 MP4 thực xuất hôm nay qua Google Flow đều decodable, đúng sản phẩm, không watermark rác — đáp ứng đủ tiêu chí affiliate thực chiến.
- `qc_level='low'` đã được hardcode đúng trong invocation (`--qc-level low --provider-mode configured`). Logic bypass đã kiểm tra:
  - Keyframe candidate integrity check: bỏ qua khi `low`
  - Preproduction gate: bỏ qua khi `low`
  - Strict clip content review: `strict_content = qc_level == 'high'` → `False` khi `low`

**Điều kiện bất biến (giữ nguyên, không thay đổi):**
- Chặn cứng khi có bằng chứng **nhầm sản phẩm** (sai tên, sai bao bì gây hiểu lầm người xem). Đây không phải "cầu toàn" mà là điều kiện pháp lý affiliate.
- Ánh sáng, góc độ, phông nền khác biệt nhỏ → gắn cờ `ready_with_flags`, không block.

---

## Quyết định 2 — Mô hình vận hành Semi-Automated qua Dashboard

**✅ APPROVED**

**Căn cứ:**
- `automation/dashboard.py` đã xác minh: 717 dòng, FastAPI, HTTP 206 partial content streaming, file upload, CLI command copy-paste. Đủ để vận hành solo.
- Mô hình: Daily Trigger chạy → operator xem Dashboard → approve/reject từng video → pipeline ghi nhận kết quả.
- Đây là bước Integrate đúng trong Move → Verify → **Integrate** → Automate → Optimize. Chưa đủ bằng chứng vận hành ổn định để nhảy thẳng sang Automate (Windows Scheduler).

**Lưu ý vận hành:**
```bash
# Khởi động Dashboard
.\apps\vf\.venv\Scripts\python.exe -m automation.cli dashboard --port 8080
# Truy cập: http://127.0.0.1:8080
```

---

## Quyết định 3 — Phê duyệt Single Rescue cho Sản phẩm 6 (SEAVY Mực Rim Me Nha Trang)

**✅ APPROVED — với điều kiện rescue đúng phạm vi**

**Phạm vi được phép:**
- Giữ nguyên clip Cảnh 1 và Cảnh 4 (đã đạt).
- Tạo lại **chỉ keyframe** cho Cảnh 2 và Cảnh 3 bằng prompt anti-watermark đã nâng cấp.
- Render lại 2 clip giữa, ghép lại `final.mp4` với 2 clip cũ đã đạt.

**Lệnh rescue:**
```bash
.\apps\vf\.venv\Scripts\python.exe -m video_factory.cli recover-keyframe \
  --workflow-id pv-daily-ef695010df5b0aa2 \
  --scenes 2,3 \
  --qc-level low \
  --provider-mode configured
```

**Điều kiện kết thúc rescue:**
- MP4 mới decodable (PyAV), 1080×1920, 30fps.
- SHA256 `final.mp4` khớp `metadata.json` mới.
- Cảnh 2–3 hiển thị miếng mực ngào rim me (không phải mực khô nguyên con).
- Nếu rescue thất bại → giữ `failed`, ghi lý do, **không tự mở lại** trong cùng lượt.

> [!CAUTION]
> Đây là **lần cứu duy nhất** theo policy (max 1 rescue/product/run). Nếu lần này vẫn sai cảnh 2–3, chuyển sản phẩm và upload ảnh sạch qua Dashboard cho lần chạy kế tiếp.

---

## Quyết định 4 — Tiêu chí Full Cutover (Bật Windows Task Scheduler)

**✅ APPROVED — chỉ kích hoạt khi ĐỦ CẢ 3 điều kiện sau**

| # | Điều kiện | Trạng thái hiện tại |
|---|-----------|-------------------|
| 4a | ≥ 3 video POV được operator approve trong **1 ngày thực tế** (không phải offline test) | ⏳ Pending — hôm nay có 5 MP4 real nhưng chưa có approve action từ operator qua Dashboard |
| 4b | Ca error-transfer pass: Product A `failed` với reason ghi DB, Product B xuất MP4 hợp lệ trong cùng lượt | ✅ **PASSED** — A/B offline test `task-320` confirmed (23.11s, `row_0001` failed `creating_storyboard`/`OUTPUT_RECONCILIATION_EXHAUSTED`, `row_0002` exported `final.mp4` SHA256 `de3e2249...` decodable) |
| 4c | Doctor-fix test: `doctor --fix` reset `KEYFRAME_TIMEOUT_EXPIRED` slots, pipeline tiếp tục không bị wedge | ⏳ Pending — logic đã code trong `doctor_agent.py` nhưng chưa chạy thực tế với DB production |

**Lệnh kích hoạt sau khi đủ điều kiện:**
```bash
# 1. Kiểm tra trước
.\apps\vf\.venv\Scripts\python.exe -m automation.cli doctor --fix --dry-run

# 2. Kích hoạt scheduler (chỉ khi 4a + 4b + 4c đều ✅)
# Vào Windows Task Scheduler → Enable task "PI-VF Two Sheets 0630"
```

> [!IMPORTANT]
> Blocker 4a là điều kiện **vận hành**, không phải điều kiện kỹ thuật. Operator cần chạy Dashboard ít nhất 1 ngày thực, approve ≥ 3 video, rồi mới bật scheduler ngầm.

---

## Backlog được ghi nhận (không làm trong đợt này)

| Hạng mục | Lý do defer | Bằng chứng cần để mở |
|----------|------------|---------------------|
| Kẹo dừa Bến Tre (`row_0009`) & Mứt gừng (`row_0010`) | Ảnh sàn nhiều biến thể, khung sale che khuất | Upload ảnh sạch qua Dashboard → requeue |
| `pv-daily-817683b38fe02c63` & `pv-daily-f8d16bb5de28c828` | `creating_storyboard` outbox quarantined `GOOGLE_FLOW_KEYFRAME_TIMEOUT` | Chạy `doctor --fix` → verify 1 lần trước khi re-trigger |
| `pv-daily-3bb80e8fdd3ff548` | `PROVIDER_CAPACITY_WAIT`, chưa có keyframe ops | Doctor fix → next trigger |
| TTS / nhạc nền tự động | Ngoài phạm vi Phase này | Chờ sản lượng ổn định ≥ 7 ngày |
| Auto-upload TikTok/Facebook | Ngoài phạm vi | Chờ cutover hoàn tất và dữ liệu vận hành thực |

---

## Kết luận

**Toàn bộ 4 quyết định: APPROVED.**

Mã nguồn tại `1e07e80` trên `codex/unified-pipeline` đã:
1. Fix đúng các blocker tái hiện được (timeout wedge, slot contention, director field missing).
2. Giữ nguyên PI/VF boundary, schema, migrations — không rewrite.
3. Có bằng chứng thực nghiệm: 5 MP4 Google Flow thật, A/B test pass offline.
4. Có lộ trình rõ từ Semi-Auto (Dashboard) → Full Cutover với điều kiện đo được.

**Bước tiếp theo ngay sau sign-off:**
1. Chạy rescue Mực Rim Me (Quyết định 3).
2. Operator xem Dashboard, approve ≥ 3 video thực để ghi nhận 4a.
3. Chạy `doctor --fix` trên DB production để verify 4c.
4. Sau khi 4a + 4c pass → bật Windows Scheduler.

---
*Sign-off bởi: Claude PM / Antigravity*
*Dựa trên: git log, file verification, A/B offline test log, production MP4 SHA256 check*
*Không có quyết định nào dựa trên output chưa xác minh của subagent.*
