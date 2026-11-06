# Audit README và kiến trúc PI/VF

Kiểm tra: **2026-11-03**, Asia/Bangkok.

Baseline PI: `5b9c642be74ec248bd9085c62a79bde96c89307e` **+ working tree có thay đổi**.
Baseline VF: `1416e15ba76003320e6d1247ccbfd01494bcbfbb` **+ working tree có thay đổi**.
Commit không đại diện toàn bộ code đang đọc; xem [SHA nguồn](source-evidence.json).

## 1. Xác định đúng hai source folder

| Vị trí | Vai trò | Bằng chứng |
|---|---|---|
| `D:\work\Personal\Product-Intelligence` | PI đang được hướng dẫn dùng với VF; có luồng Sheet/file-first và Spaiauto | `D:\work\video-factory\docs\release-personal\README.md:40`; PI `README.md:3`, `AGENTS.md:8` |
| `D:\work\video-factory` | Source creative, workflow, QC, provider, compose/export, CLI/Studio | `pyproject.toml:32`; `src/video_factory/cli.py:2768` |
| `D:\work\pi-vf-runtime-20261102` | Folder dữ liệu/config/script chạy, không phải project mới độc lập | `Run-VF.ps1:2`, `Start-Studio.ps1:4`, `Activate.ps1:2` |
| `D:\work\code\TIKTOK\Product-Intelligence` | Checkout PI khác; không dùng làm baseline thiết kế này | Khác HEAD `cb58f19d413b224c5b3791e2d388eb85100c5ea8`; runtime/runbook phía trên chỉ tới PI trong Personal |

Evidence PI/VF bên dưới dùng đường dẫn tương đối với hai source root vừa nêu. Không audit toàn bộ checkout PI khác.

## 2. Những gì code thực sự làm

**PI:** `sheet_snapshot` tải hai workbook; `selection_handoff` đọc tab/hyperlink, chuẩn hóa listing và giữ provenance; `sheet_selection_agent` gọi 9Router để đề xuất tối đa 15; `sheet_daily` commit selection và intake vào PI. Sau đó agent/browser lấy PDP/capture/ZIP; `spaiauto` import, review, xuất resources và publish Exchange.

Điểm rất quan trọng: `sheet_daily.run_daily()` trả **`awaiting_acquisition`**, không phải hoàn thành acquisition hoặc MP4 (`tools/sheet_daily.py:19`, `:52`, `:55`). CLI không tự thực hiện browser acquisition theo hướng dẫn PI `AGENTS.md`.

**VF:** `creative-batch` tìm READY, pin selection/config, chuẩn bị resource/dossier, đi qua worker/handlers, brief/scene/storyboard, keyframe/QC, scene render và compose/export. Auto policy tiếp tục chuyển qua các state approval cũ; chưa phải một pipeline tuyến tính mới (`automation/creative_cli.py:349`, `:383`, `:405`; `handlers/registry.py:1119`; `automation/creative_policy.py:20`).

## 3. Đối chiếu tài liệu với code

| Mục | Loại / trạng thái | Mức | Evidence và hệ quả |
|---|---|---|---|
| README VF đưa ví dụ `video-factory run --product-id` | Sai với parser hiện tại | P1 | `README.md` mục lệnh chính; `src/video_factory/cli.py:2732`, `:2768`, `:2802` có creative-run/creative-batch/start; không có subparser `run`. Người mới có thể bắt đầu bằng lệnh không tồn tại |
| README VF hướng dẫn `config/config.yaml`, loader đọc cwd/config.yaml | Thiếu/mơ hồ | P1 | `src/video_factory/config.py:14` tìm cwd/config.yaml rồi ~/.hermes/config.yaml; các caller còn dùng loader khác. Không thể suy ra một nguồn config chỉ từ README |
| Nhãn Standalone | Đúng một phần, còn coupling | P1 | `workflows/worker.py:142` thêm path checkout Hermes; `integrations/keyframe_provider_factory.py:29`, `handlers/common.py:278` còn import hermes_cli; `application/app.py:23` có import Hermes trong nhánh tương thích. Không khẳng định mọi nhánh đều bắt buộc Hermes, nhưng không thể bê nguyên repo rồi coi độc lập |
| README mô tả 5 HITL gates nhưng release cá nhân ưu tiên tự chạy | Tài liệu nhiều thời kỳ, khó xác định nguồn chuẩn | P1 | Banner đầu `README.md`; `docs/release-personal/README.md`; `automation/creative_policy.py:24` vẫn xử lý AwaitingBrief/Scene/Storyboard/FinalApproval |
| Architecture AS-BUILT ngày 2026-10-19 | Có ích nhưng cũ | P2 | `docs/developer/architecture.md:3`; entrypoint Studio, catalog preparation và thay đổi Sheet mới hơn nằm ngoài snapshot. Không lấy sơ đồ cũ làm toàn bộ as-built ngày 2026-11-03 |
| PI file-first và discovery paused | Có code/policy/hướng dẫn rõ | Giữ | PI `AGENTS.md:8`, `README.md:7`; không cần đưa trend discovery/Telegram vào project mới |
| Sheet → intake | Đã có implementation và test offline | Giữ | PI `tools/sheet_daily.py:19`; `tests/test_sheet_daily.py:22`; lưu và replay cùng selection/ACK |
| Acquisition tự động toàn phần | Chưa chứng minh; dependency ngoài từng lỗi | P0 cho full-flow | PI `docs/sheet-selection-agent.md` ghi scan Spaiauto lỗi ngày 2026-11-02; `AGENTS.md` nói CLI không tự lái browser. Đây là reported incident, không phải kiểm tra live mới |
| Resources xuất được khi brand/model null, Exchange review lại yêu cầu đủ | Hai contract khác nhau, dễ nhầm | P1 | PI `README.md`; `tools/spaiauto/workflow.py:131` bắt brand/model/category, identity_verified và selected assets. Có file không đồng nghĩa đủ điều kiện vào VF |
| Nhiều checkpoint và lớp retry | Rủi ro nghẽn có cơ sở; không kết luận tất cả đang hỏng | P1 | PI pool/JSON/SQLite/ACK/READY; VF selection/workflow/outbox/QC/operations. `workflows/outbox_repository.py:95` đếm delivery khi claim, khác paid submit; `automation/creative_cli.py:129` còn vòng advance/recovery |
| Keyframe reconcile và video no-resend | Logic quan trọng phải giữ | Giữ | VF `integrations/google_flow_keyframe_provider.py:671`, `:793`; `integrations/google_flow_provider.py:308`, `:477`, `:488`; timeout sau Send không cho phép gửi lại mù |
| Keyframe 1+2, chọn candidate hợp lệ tốt nhất | Requirement cá nhân có regression | Giữ | `docs/release-personal/README.md`; `tests/test_keyframe_best_candidate_selection.py`; không chuyển thành sinh đủ 3 ảnh bắt buộc |
| QC low/middle/high | Nhánh chưa có ý nghĩa đồng đều | P2 | `automation/creative_policy.py:5` và TODO high; project mới chỉ cần một policy thiết yếu |
| MP4 cuối không cần final content critic | Đã có đường technical-only, nên giữ | Giữ | `tests/test_silent_final_contract.py:164`, `:191`; `integrations/composer.py:243` kiểm decode/profile |
| Runtime mới vẫn pin date và product keys cố định | Có ích cho lượt live, chưa phải daily runner tổng quát | P1 | `D:\work\pi-vf-runtime-20261102\Run-VF.ps1:4`; date 2026-11-02, hai product keys cố định |

## 4. Kiểm tra thực hiện trong lượt này

| Phạm vi | Kết quả mới | Giới hạn |
|---|---|---|
| PI: test_sheet_daily, test_selection_handoff, test_selection_intake, test_selection_publish | **13 passed / 5.37s** | Network bị chặn trước pytest, DB/ảnh fixture; chưa kiểm browser live |
| VF: test_creative_batch_entrypoint, test_silent_final_contract, test_keyframe_best_candidate_selection | **23 passed / 18.56s** | Network bị chặn trước pytest; không phải full suite/provider smoke |
| MP4 Nhà Mứt trong runtime 20261102 | ffprobe: H.264, 1080×1920, 30/1 fps, 960 frames, 32s, chỉ video stream | Probe metadata mới; không decode toàn file trong lượt này, không đánh giá chất lượng nội dung |
| SHA-256 MP4 | `f329dc08f06a37c2f8d133c9c4b573d8cfc5ebf26f6ea23beb72c3ed3489ad1c` khớp runtime README | Output cũ; không chứng minh batch nhiều sản phẩm hoặc project mới |

File MP4: `D:\work\pi-vf-runtime-20261102\workspace\exports\export_proj-pv-daily-534f6e82cab9e9c0_1\final.mp4`.
Runtime README báo Cocofarm kẹt brief sau 5 delivery attempts và Nhà Mứt từng phát sinh END image ngoài policy. Đây là evidence lịch sử của lượt cũ; chưa tái hiện để kết luận bug còn tồn tại ở toàn bộ code hiện tại.

## 5. Kết luận kiến trúc

Không cần thêm coordinator bọc PI và VF cũ. Làm vậy vẫn giữ hai hệ state, hai lần admission và nhiều entrypoint/config. Lựa chọn đề xuất: **gộp capability cần dùng thành một project**, tái sử dụng parser, provider mechanics và media validation có chọn lọc; thay phần orchestration bằng một runner có checkpoint rõ.

Không bỏ identity, hash, giới hạn generation hay UNKNOWN reconciliation để làm luồng có vẻ thông. Bỏ những state approval tự tạo rồi tự duyệt, các mode/provider chưa dùng, và contract nội bộ chỉ cần vì hai repo tách rời.

## 6. Tài liệu cần có cho project mới

| Tài liệu | Mục đích | Có tại folder này |
|---|---|---|
| README duy nhất dẫn vào đúng luồng | Chạy gì, kết quả gì, giới hạn gì | [README](../README.md) |
| Spec kiến trúc + contract + recovery | Builder không cần ghép nhiều master spec cũ | [DESIGN](DESIGN.md) |
| Trình tự xây theo vertical slice | Chứng minh acquisition và một MP4 trước UI/schedule | [BUILD-SEQUENCE](BUILD-SEQUENCE.md) |
| Source/test evidence | Tách observed/reported/proposed | [source-evidence.json](source-evidence.json) |

Phạm vi audit và bản thiết kế mới đã được user yêu cầu trực tiếp. Không sửa README/source hiện tại trong hai repo. Các hướng dẫn cũ được giữ làm bằng chứng, không tự chuyển thành backlog.
