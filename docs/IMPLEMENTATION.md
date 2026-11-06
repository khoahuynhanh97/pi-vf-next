# Kế hoạch triển khai hợp nhất — 2026-11-03

> Nhật ký lịch sử của bản đầu bốn cảnh. [Kiến trúc chuẩn](DESIGN.md) và [lộ trình](BUILD-SEQUENCE.md) là tài liệu gốc cho thay đổi tiếp theo.

> Dùng executing-plans để triển khai trực tiếp trong phiên này. Không commit/push, không generation có phí hoặc đổi lịch chạy trong lượt xây dựng.

**Goal:** Một project độc lập Sheet → lọc → tư liệu → identity/brief → keyframe → clip → MP4, có resume và folder dễ hiểu.

**Architecture:** Một Python package, một runner, một SQLite. Dữ liệu chỉ trong `data/`: `state.sqlite`, `sheets/<date>/`, `products/<tên>-<mã>/`; mọi file từng sản phẩm nằm cùng folder, có prefix và revision. Không Exchange, ACK, outbox hoặc cây stage directories.

**Tech Stack:** Python 3.11+, sqlite3/tomllib/pathlib, httpx, openpyxl, Pillow, Playwright, FFmpeg/ffprobe; pytest cho kiểm chứng offline.

**Spec:** [DESIGN](DESIGN.md), điều chỉnh folder theo yêu cầu user mới nhất.

## Global constraints và rulings

- User đã yêu cầu tiếp tục xây project mới; triển khai trực tiếp, không chờ một vòng duyệt kế hoạch nữa.
- `D:\work\pi-vf-next` là project riêng, không cần thêm worktree/folder; không sửa source/DB của PI/VF cũ.
- Dùng `docs/IMPLEMENTATION.md` làm plan và ledger để tránh sinh thêm cây tài liệu `.superpowers`.
- Giảm 6 bảng dự kiến thành 3 bảng runs/items/operations: checkpoint và artifact receipts lưu trong JSON của item. Unique request và journal operation vẫn bền vững; không thêm hai nguồn state. Lock dùng OS file locking tự giải phóng khi process chết, không cần bảng meta/heartbeat.
- Folder dữ liệu mặc định đổi từ runtime sang `data`; file từng product ở cùng một folder, không thư mục riêng cho từng scene/stage.
- Không hứa live acquisition/Flow trước khi có smoke; offline E2E phải encode/decode MP4 thật.

## Review focus

1. Crash sau Send, trước receipt: reconcile operation cũ, không submit lại.
2. Hai process cùng runtime và fix khi operation đang chạy: không cùng dispatch hoặc đổi identity.
3. Listing ID dài, hyperlink/formula, variant conflict, rerun ngày sau: không mất identity/chọn trùng.
4. Source URL/ZIP/path không tin cậy và prompt injection: không thoát runtime/đổi policy/nâng claim.
5. Scene thành công và MP4 bị mất/hỏng: reuse/recover đúng phần; không lặp generation trả phí.

## Tasks

### 1. Store, config và contract

Files: `src/pi_vf/core.py`, `src/pi_vf/store.py`, `tests/test_store.py`, `config.example.toml`, `pyproject.toml`.
Interfaces: `Config.load(path)`, `Store(root)`, `Store.lease()`, `Store.create_run(day,candidates,config_digest)`, `Store.save(item)`, `Store.operation(item,kind,scope,payload,limit)`, `Store.update_operation(key,**fields)`.

- [x] RED: tests reservation qua ngày, lease hai connection, UNKNOWN giữ request/counter, path escape.
- [x] Implement minimal SQLite transactions, config absolute paths, item receipts; GREEN `python -m pytest tests/test_store.py -q`.

### 2. Sheet và acquisition

Files: `sheets.py`, `acquire.py`, `tests/test_inputs.py`.
Interfaces: `snapshot(config,day)`, `normalize(paths)`, `select(rows,complete,limit)`, `Acquirer.acquire(item,folder)`, `import_source(input,folder,url)`.

- [x] RED: XLSX hyperlink/dedup/long IDs/conflict; selection ID lạ; ZIP traversal; capture binding/decoded images.
- [x] Port logic hữu ích từ PI, browser acquisition thực; GREEN `python -m pytest tests/test_inputs.py -q`.

### 3. Creative và media

Files: `creative.py`, `media.py`, `tests/test_creative.py`.
Interfaces: `Router.complete(task,payload,images=())`, `identity(source,answer)`, `plan(lock,answer)`, `probe(path)`, `export(clips,target)`.

- [x] RED: generic identity, wrong/unresolved variant, claim refs, four START scene contract, actual MP4 verification.
- [x] Implement grounding validators + FFmpeg media; GREEN focused tests.

### 4. Flow và runner chuẩn

Files: `flow.py`, `pipeline.py`, `tests/test_pipeline.py`.
Interfaces: `Flow.submit(operation,folder)`, `Flow.poll(operation,folder)`, `Pipeline.run(run_id)`, `Pipeline.fix(alias,source_input)`.

- [x] RED: fake provider trả file thật; UNKNOWN không gửi lại, per-item isolation, resume/DONE không gọi model, budget giữ qua revision.
- [x] Port Flow UI mechanics có chọn lọc, journal trước Send, bounded runner; GREEN focused tests + fake E2E tới MP4 thật.

### 5. CLI, hướng dẫn và review

Files: `cli.py`, `__main__.py`, `README.md`, `tests/test_cli.py`.
Interfaces: `pi-vf run|resume|status|fix|doctor|login`; một config và đường dẫn cố định.

- [x] RED: CLI cwd khác, status không gọi provider, missing config báo rõ, folder budget.
- [x] Implement entrypoints, standalone environment, actual help/doctor/status and full tests.
- [x] Review độc lập; sửa lỗi quan trọng bằng regression test; cập nhật trạng thái live trung thực.

## Ledger

- 2026-11-03: đọc lại thiết kế và source boundaries; user đã chốt hợp nhất và giảm folder. Không sử dụng codebase cũ như runtime dependency.
- Task 1: đã quan sát RED → GREEN cho store/config/lock/operation reservation; môi trường riêng được tạo bằng uv, lockfile và package CLI hoạt động. Không tạo thêm worktree vì đây là folder project riêng được user yêu cầu.
- Task 2: RED → GREEN cho hyperlink, ID dài, variant conflict, selection validator, ZIP traversal, source binding. Browser acquisition đã implement nhưng chưa smoke; không coi live acquisition hoàn tất. Đọc thử hai XLSX cũ ngày 2026-11-02 bằng parser mới: **2 workbook, 7 tab, 69 listing OPEN**; không fetch live hoặc nhập state cũ.
- Task 3: RED → GREEN cho identity generic/variant mismatch, scene facts và START-only. Router đọc key từ env; không đọc SQLite credential hoặc import Hermes. FFmpeg compose/decode thật được chạy trong E2E.
- Task 4: RED → GREEN cho UNKNOWN/no resend, per-item isolation, fix guard, missing keyframe/clip recovery, file SHA mismatch và call budget qua revision. E2E đã mở rộng từ **hai snapshot XLSX → normalize → selection → import ảnh thật → fake model/Flow → encode/decode MP4 thật**. Missing clip/export được phục hồi mà không thêm Send.
- Task 5: CLI/Run.ps1/README đã tạo; status/doctor chạy thật, kể cả config từ cwd khác. `uv pip check` đạt và frozen/offline sync dùng lockfile đạt. Repo mới trên branch `codex/unified-pipeline`; không commit/push.
- Review độc lập đã đọc source và báo 4 Important: missing artifact recovery, existing reference chip, terminal failure, missing config dead-end. Đã thêm regression RED → GREEN và sửa trong một lượt. Lỗi variant mismatch được phát hiện/sửa trước khi report cuối. Không có minor bị hoãn trong review.
- Ruling: ngân sách/poll timeout không thuộc generation identity fingerprint; user có thể chỉnh trần có chủ đích rồi resume, toàn bộ counters cũ vẫn giữ. Model/profile/source binding vẫn pin. Nếu đổi model/provider giữa run, ứng dụng từ chối; không fallback ngầm.
- Ruling: chưa kết nối provider live hoặc đăng nhập thay user. Config thật đã được tạo với model để trống, ngân sách 0; preflight chặn **trước khi pin run**, tránh tạo một job không phục hồi được chỉ vì setup chưa đủ.
- Review set aside: selector/UI live, chất lượng identity của model và hình ảnh toàn clip chưa có smoke/evidence; ghi rõ trong README, không dùng offline PASS để nghiệm thu các phần này.

## Trạng thái nghiệm thu

Các task triển khai source/offline ở trên đã thực hiện. Những checkbox gốc mô tả bước công việc, không đại diện live acceptance; **AC14 và live acquisition/Flow vẫn mở**. Cần model text/vision, browser đăng nhập và hạn mức do user đặt trước một lượt live có phí. Chưa thêm UI/scheduler hoặc thay cấu hình của hệ cũ.

## Verification cuối — 2026-11-03

- `.venv/Scripts/python.exe -m pytest -q`: **30 passed in 13.20s**. Network bị chặn bởi autouse fixture; không gọi provider live.
- Compileall src/tests đạt; `uv pip check`: 20 packages compatible; `uv sync --frozen --offline --extra dev` đạt.
- `Run.ps1` PowerShell parse đạt; `status` chạy từ `D:/work` vẫn dùng đúng `D:/work/pi-vf-next/data`.
- `doctor`: FFmpeg/ffprobe có sẵn; text/vision models chưa đặt, credits 0, browser login chưa kiểm. Chưa tạo production run/generation.
- Review fixes và các kiểm tra trên không thay cho smoke live AC14.
