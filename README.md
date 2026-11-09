# PI / VF Next — Unified Product Automation Platform

Consolidated repository for **Product Intelligence (PI)** and **Video Factory (VF)** at `D:\work\pi-vf-next`.

Governed by [Master Prompt](docs/platform/MASTER-PROMPT.md), [Repository Layout](docs/platform/REPOSITORY-LAYOUT.md), [Migration Roadmap](docs/platform/08-migration-roadmap.md), and [AGENTS.md](AGENTS.md).

**Bắt đầu tại [Sổ tay PI/VF](docs/PI-VF-HANDBOOK.md)** để xem checkout vận hành, sơ đồ pipeline, lịch sử lỗi, các test đã chạy và điều kiện release.

**Hiện trạng đã đối chiếu 08/11/2026:** checkout scheduler ở `9863eeb` chưa nghiệm thu luồng Sheet → MP4 hằng ngày. PI trả `exchange_batches`, còn đường scheduler đọc `exchange_batch_dir`. Cần xác minh cả việc giữ slot Flow khi operation chưa rõ kết quả. Quyết định hiện tại là **chạy tuần tự với trạng thái riêng cho từng sản phẩm**. Các mốc offline bên dưới là bằng chứng lịch sử.

---

## 1. Cấu trúc thư mục thực tế (Phase 01)

```text
pi-vf-next/
├── apps/
│   ├── pi/                      # Product Intelligence application
│   │   ├── product_scout/       # Discovery & Legacy Discovery controls
│   │   ├── media/               # Research, assets, validation, persistence
│   │   ├── tools/               # File intake (Spaiauto, Sheet agents, handoff)
│   │   ├── scripts/             # Exchange publisher & processing scripts
│   │   ├── tests/               # PI test suite (453 passed baseline)
│   │   ├── pyproject.toml
│   │   └── uv.lock
│   └── vf/                      # Video Factory application
│       ├── src/video_factory/   # CLI, Studio, workflow engine, creative, QC, render
│       ├── config/              # Runtime configs, profiles, templates
│       ├── scripts/             # Daily operations & helper scripts
│       ├── tests/               # VF test suite & verification harnesses
│       ├── assets/              # Creative assets & templates
│       ├── pyproject.toml
│       └── uv.lock
├── data/                        # Preserved runtime data (e.g. pilot state.sqlite)
├── docs/                        # Platform architecture, layout, roadmaps
├── AGENTS.md                    # Working guidelines and principles
└── README.md
```

*Ghi chú bản sao Pilot:* Mã nguồn pilot trước đây (`src/pi_vf`, pilot `tests/`, `Run.ps1`, `config.example.toml`, `config.toml`, v.v.) đã được kiểm kê, sao lưu và đối chiếu toàn vẹn tại **`D:\work\pi-vf-pilot-backup`** trước khi dọn dẹp khỏi root.

---

## 2. Hướng dẫn cài đặt và kiểm chứng độc lập

Cả hai ứng dụng duy trì môi trường ảo và lockfile độc lập.

### Product Intelligence (`apps/pi`)

```powershell
# Di chuyển vào apps/pi
Set-Location D:\work\pi-vf-next\apps\pi

# Đồng bộ dependencies
uv sync --frozen --extra test

# Kiểm tra entrypoint & tools
uv run python -m tools.spaiauto --help

# Chạy test suite (453 passed; 3 failed do chính sách pause discovery có chủ đích)
uv run pytest -q
```

### Video Factory (`apps/vf`)

```powershell
# Di chuyển vào apps/vf
Set-Location D:\work\pi-vf-next\apps\vf

# Đồng bộ dependencies
uv sync --frozen --extra dev

# Kiểm tra CLI entrypoint
uv run video-factory --help

# Kiểm tra khởi tạo schema DB tạm
uv run video-factory --db-path temp.db daily-status --date 2026-11-03

# Chạy offline test suite
uv run pytest tests/test_export_integrity.py tests/test_composer.py tests/test_creative_contracts.py tests/test_qc_service.py -q
```

---

## 3. Quy ước runtime cho các phase tiếp theo

Đường dẫn runtime đích được định nghĩa theo `docs/platform/REPOSITORY-LAYOUT.md`:

```text
runtime/
├── pi/
│   └── runs/<run_id>/                      # ZIP, capture, ảnh, resources.json
├── exchange/inbox/<date>/run_<batch>/      # manifest.json, ảnh đã duyệt, cờ READY
└── vf/workspace/
    ├── projects/<project_id>/              # Storyboard, keyframe, clip
    └── exports/export_<project_id>_<ver>/  # final.mp4, metadata.json
```

---

## 4. Mốc triển khai và kiểm chứng trước đây

- **Phase 01 — Source Consolidation, 02/11:** Gom source gốc vào `apps/pi` và `apps/vf`, giữ môi trường và cấu hình riêng.
- **Phase 02 — Unified Pipeline, 03/11:** Kiểm chứng offline Spark CSV → PI → Exchange → VF → MP4 giải mã được bằng provider fake.
- **Phase 03 — Automation Orchestration, 03/11:** Có CLI lịch ngày theo Bangkok, run ID và ledger dedupe. Khả năng tiếp tục từng sản phẩm qua scheduler cần nghiệm thu lại trên luồng Sheet hiện tại.
- **Phase 04 — Agent & Operational Optimization, 03/11:** Baseline fake kiểm tra cấu hình 3–5 cảnh và limit 1–2. Xem [bằng chứng offline](docs/PHASE-02-04-COMPLETION.md) và [điều kiện cutover](docs/platform/08-migration-roadmap.md).

---

## 5. Hướng dẫn vận hành Platform Entrypoint (`Run.ps1`)

Repo cung cấp lệnh điều phối thống nhất duy nhất qua PowerShell script `Run.ps1`:

### 5.1. Kiểm tra môi trường hệ thống (Doctor)
```powershell
powershell -ExecutionPolicy Bypass -File .\Run.ps1 doctor
```
Kiểm tra cả 2 virtualenv (`apps/pi/.venv` và `apps/vf/.venv`), các thư viện trọng yếu (`PIL`, `pydantic`, `av` PyAV, `numpy`), cấu hình nguồn Google Sheets, dung lượng ổ đĩa và timezone `Asia/Bangkok`.

### 5.2. Chạy Pipeline từ hai Google Sheets

Ví dụ cấu hình có thể gọi provider thật. Xem các lỗi đang mở trong sổ tay trước khi dùng để chạy kiểm chứng.


```powershell
.\Run.ps1 run --limit 1 --scenes 4 --qc-level low
```

PI tải hai Google Sheets đã cấu hình thành snapshot XLSX theo ngày tại `runtime/pi/sheet_daily`.
9Router chọn sản phẩm từ hai snapshot. PI giữ biên nhận nguồn, thu ảnh qua Spaiauto và bàn giao gói `READY`. Trên checkout đã đối chiếu, gói có thể đã xuất nhưng scheduler vẫn báo `waiting` do lệch giao kèo PI/VF. Kiểm tra output và manifest trước khi quyết định chạy lại.

Chỉ dùng `--csv <đường-dẫn>` khi muốn chỉ định file đầu vào để kiểm thử.

### 5.3. Xem trạng thái hệ thống (Status)
```powershell
powershell -ExecutionPolicy Bypass -File .\Run.ps1 status
```
Hiển thị giờ Bangkok hiện tại, đường dẫn Inbox, đường dẫn Hermes SQLite DB, lịch sử chạy gần nhất trong scheduler ledger, các batch trong Exchange Inbox, và danh sách workflow trong database.

### 5.4. Tìm và kiểm tra tính toàn vẹn MP4 (Find MP4)
```powershell
powershell -ExecutionPolicy Bypass -File .\Run.ps1 find-mp4
```
Tự động quét thư mục `runtime/vf/workspace/exports/`, giải mã video stream bằng PyAV, kiểm tra resolution, frame count, FPS và tính mã băm SHA-256. Mỗi bundle bao gồm: `final.mp4`, `caption.txt`, `metadata.json`.

### 5.5. Kích hoạt Scheduler hàng ngày (Trigger)

Các lệnh dưới đây minh họa cú pháp. Cấu hình task đã đối chiếu và trạng thái release nằm trong sổ tay.


```powershell
.\Run.ps1 trigger --limit 1 --qc-level low --acquire
.\Run.ps1 trigger --daemon --limit 1 --qc-level low
```

Windows Task Scheduler chạy lúc 06:30 giờ Bangkok. `--acquire` lấy ZIP và capture
qua Spaiauto, giải nén một lần và chạy duyệt ảnh local trước khi giao cho VF.
PI xóa ZIP mới sau khi đã lưu ảnh và biên nhận import. VF đọc ảnh theo
`relative_path` và kiểm SHA-256 của file. URL ảnh riêng lẻ là dữ liệu tùy chọn từ lượt cũ.

### 5.6. Phục hồi workflow dở dang (Resume)

```powershell
.\Run.ps1 resume --run-id <run_id>
```

Đối chiếu workflow, receipt và operation ID gốc trước khi resume. Với timeout sau khi gửi, kiểm tra yêu cầu cũ trong Flow trước khi cho phép tạo lại.

### 5.7. Theo dõi chất lượng và duyệt export

Run VF mới chấm tiền kỳ ở chế độ `observe`. Có thể đặt `preproduction_threshold`
trong JSON profile (mặc định 80/100). Verdict tiền kỳ được lưu để đối chiếu.
Chế độ `enforce` chưa bật.

Các lệnh `run`, `resume` và `trigger` ở mode `configured` cập nhật
`runtime/reports/YYYY-MM-DD.json`. Tổng USD gồm các receipt đã ghi nhận.
Chi phí chưa có số liệu được đánh dấu bằng `cost.complete: false`.
Telegram cần cặp `TELEGRAM_BOT_TOKEN` và `TELEGRAM_CHAT_ID`. Nếu thiếu cặp này,
hệ thống đọc cấu hình PI cũ `PRODUCT_SCOUT_TELEGRAM_BOT_TOKEN` và
`PRODUCT_SCOUT_TELEGRAM_OWNER_ID` từ môi trường hoặc `apps/pi/.env`.

```powershell
.\Run.ps1 human-verdict "D:\work\pi-vf-next\runtime\vf\workspace\exports\export_example" --verdict reject --cause render --reason "Sản phẩm đổi màu ở cảnh 2"
.\Run.ps1 compare-qc
```

Verdict nằm trong `human_verdict.json` cạnh export, có SHA của MP4 đã duyệt.
Bảng đối chiếu để trống điểm khi export thiếu receipt tương ứng. `compare-qc --threshold 80`
dùng điểm preproduction để tính phân bố và đối chiếu với verdict của người.
Nhóm `script+product` dùng để hiệu chỉnh ngưỡng, còn `render/other` được đếm riêng.
Verdict cũ thiếu `cause` hiển thị `unknown` và nằm ngoài hai nhóm.

## 6. Kết quả kiểm chứng Phase 02–04 (Offline, Fake Provider Mode)

Các kết quả dưới đây thuộc lượt kiểm chứng lịch sử. Gán `$offlineCsv` tới fixture đã chọn và chạy trên checkout với runtime thử nghiệm. `Run.ps1` mặc định dùng thư mục runtime của checkout chứa script.

### Phase 02 — Unified Pipeline
```powershell
# Chạy thành công: Spark CSV → PI Intake → Exchange READY → VF Creative → MP4
.\Run.ps1 run --limit 1 --scenes 4 --csv $offlineCsv --provider-mode fake
# Kết quả: 2 MP4 decodable (720x1280@30fps, 60 frames), SHA256 verified
# Duration: ~2–3s
```

**Đường dẫn MP4:**
- `runtime\vf\workspace\exports\export_proj-pv-daily-7fc8a79f1410f331_v1\final.mp4`
- `runtime\vf\workspace\exports\export_proj-pv-daily-67cf239d3eaf44d9_v1\final.mp4`

### Phase 03 — Automation & Scheduler
```powershell
# Trigger scheduler (deduplication by date + CSV SHA256)
.\Run.ps1 trigger --limit 1 --scenes 4 --csv $offlineCsv --provider-mode fake
# Output: [SKIPPED] Already completed for 2026-11-03 with CSV digest 3f7719fc

# Force re-run
.\Run.ps1 trigger --limit 1 --scenes 4 --csv $offlineCsv --provider-mode fake --force
# Output: Daily Run Result: success
```

**Scheduler Ledger:**
- Date: `2026-11-03`, Run: `run_20261103_3f7719fc_s4`, Status: `success`, MP4s: `2`, Duration: `2.39s`

### Phase 04 — Scene Flexibility (3, 4, 5 scenes)
```powershell
# 3 cảnh
.\Run.ps1 run --limit 1 --scenes 3 --csv $offlineCsv --provider-mode fake
# Status: SUCCESS, 2 MP4 decodable

# 5 cảnh
.\Run.ps1 run --limit 1 --scenes 5 --csv $offlineCsv --provider-mode fake
# Status: SUCCESS, 2 MP4 decodable

# 2 sản phẩm
.\Run.ps1 run --limit 2 --scenes 4 --csv $offlineCsv --provider-mode fake
# Status: SUCCESS, 2 MP4 decodable
```

**Run ID Format:** `run_<YYYYMMDD>_<csv_sha[:8]>_s<scenes>_n<limit>` để tránh conflict khi thay đổi tham số.

---

## 7. Cấu trúc Runtime cô lập (`runtime/`)

Các đường dẫn ghi mặc định của checkout vận hành nằm trong `D:\work\pi-vf-next\runtime`:

```text
runtime/
├── pi/runs/<run_id>/                      # PI intake, fixtures, handoff, resources.json, unresolved_links.json
├── exchange/inbox/<date>/run_<batch>/      # manifest.json, ảnh sản phẩm, READY
├── vf/
│   ├── hermes.db                           # SQLite workflow state và outbox events
│   └── workspace/
│       ├── projects/<project_id>/          # Storyboards, keyframes, rendered clips
│       └── exports/export_<id>_<ver>/      # final.mp4, caption.txt, metadata.json
└── scheduler_ledger.json                   # Lịch sử daily scheduler (date, sha, status, mp4_count, duration)
```
Một số đường đọc ảnh lịch sử hoặc tìm MCP còn tham chiếu vị trí khác. Kiểm tra riêng các phụ thuộc này trước cutover.

---

## 8. Tiêu chuẩn Sẵn sàng Vận hành Sản xuất (Production Readiness)

### 8.1. Tách bạch Tư liệu Thật và Fixture (Real vs Fixture Isolation)
- **Chế độ `configured`:** Chỉ chấp nhận các batch chứa tư liệu thật được thu thập qua browser operator và xác minh danh tính (`identity_verified: true`). Ngăn chặn hoàn toàn việc nhận batch fixture (`run_fix_...`), ảnh đơn sắc tổng hợp (`arr.std() < 1.0`), hoặc operator giả lập.
- **Không tự ý giả mạo tư liệu:** Khi chạy `--live-intake` hoặc `provider_mode=configured` mà tư liệu thật chưa sẵn sàng, pipeline dừng an toàn ở trạng thái `WAITING` kèm hướng dẫn thao tác (Next Step), tuyệt đối không tự sinh file ZIP/capture giả.
- **Phân tách định danh batch:** Batch fixture sử dụng tiền tố `fix_`, batch tư liệu thật sử dụng tiền tố `sel_`.

### 8.2. Báo cáo Kết quả Run hiện hành & Chống mượn file cũ (Run Result Integrity)
- **Pinned Lookups & Export Receipts:** Trạng thái `SUCCESS` hoặc `PARTIAL` chỉ được xác nhận khi tồn tại biên nhận xuất bản `metadata.json` hợp lệ, khớp chính xác `project_id` của lượt chạy hiện hành và file video giải mã được toàn bộ luồng khung hình qua PyAV.
- **Không mượn file cũ:** Khi VF gặp lỗi hoặc sản phẩm bị bỏ qua, hệ thống không bao giờ quét bừa các file MP4 cũ còn lưu trong thư mục `exports/` để báo cáo thành công giả mạo.
- **Scheduler Deduplication Ledger:** Ledger lưu `SUCCESS`, `PARTIAL`, `WAITING` hoặc `FAILED` theo ngày và digest. Quyết định chạy lại dựa vào ledger; khả năng resume từng mục phụ thuộc trạng thái workflow và provider receipt.

### 8.3. Độ linh hoạt phân cảnh & Giải mã đa phương tiện (3, 4, 5 Scenes Flex)
- **Tạo clip độc lập:** Mỗi cảnh trong kế hoạch (scene plan) được dựng thành một clip MP4 độc lập với bảng màu, visual banner và mã băm SHA-256 riêng biệt.
- **Ghép nối chính xác:** Sử dụng PyAV composer ghép nối tuần tự theo đúng thứ tự kịch bản và xuất ra package tiêu chuẩn.
  - 3 scenes: 24.0s @ 30fps (720 frames)
  - 4 scenes: 32.0s @ 30fps (960 frames)
  - 5 scenes: 40.0s @ 30fps (1200 frames)
- **Xác thực toàn vẹn bằng PyAV:** `find-mp4` và kiểm tra tự động giải mã toàn bộ khung hình, từ chối file hỏng hoặc lệch spec.

### 8.4. Kiểm chứng bộ kiểm thử tự động (Automated Test Suites)
Các nhóm dưới đây từng được dùng để kiểm chứng hợp đồng offline. Kết quả mới nhất đã đối chiếu và hồi quy capacity nằm trong sổ tay.
```powershell
# 1. Bộ test đảm bảo tính toàn vẹn export & PyAV decoder (7 passed)
uv run pytest tests/test_export_integrity.py -v

# 2. Bộ test hợp đồng kỹ thuật final video & profile 720x1280 / 1080x1920 (6 passed)
uv run pytest tests/test_silent_final_contract.py -v

# 3. Bộ test sẵn sàng vận hành & chống hồi quy 5 tiêu chí cốt lõi (7 passed)
uv run pytest tests/test_automation_readiness.py -v
```

### 8.5. Preflight Kiểm tra Sản phẩm Thật Spark (Pilot Preflight)
Kết quả dưới đây là hồ sơ pilot lịch sử. Các ghi chú profile và cấu hình của lần chạy đó cần kiểm tra lại trước khi áp dụng cho máy hiện tại.
Đã thực hiện preflight đối soát trên sản phẩm có PDP trực tiếp từ `gemini_spark_sheet.csv`:
- **Sản phẩm:** `Bình giữ nhiệt TYESO Inox 304 nắp thể thao có vòi hút (750ml - 1000ml)` (Row 53).
- **PDP URL:** `https://shopee.vn/B%C3%ACnh-gi%E1%BB%AF-nhi%E1%BB%87t-TYESO-INOX-304-c%C3%A1ch-nhi%E1%BB%87t-ch%C3%A2n-kh%C3%B4ng-gi%E1%BB%AF-l%E1%BA%A1nh-v%C3%A0-n%C3%B3ng-01-01008-01009-01010-i.230537546.55253485624`
- **Hạ tầng Browser:** Chrome đã cài đặt tại `C:\Program Files\Google\Chrome\Application\chrome.exe`. Chưa gắn profile tự động hóa đăng nhập active cho `tools.spaiauto.com`.
- **Model API Keys:** Pinned profile `google_flow` cần `GEMINI_API_KEY` hoặc `GOOGLE_API_KEY` (hiện chưa set trong biến môi trường).
- **Kết quả Preflight:** Lượt kiểm chứng CSV trước đây phản hồi `WAITING` tại `pi_intake`, kèm bước tiếp theo. Kết quả này chỉ xác nhận hành vi preflight của lượt đó.
