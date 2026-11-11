# Audit vận hành PI/VF và nhiệm vụ khắc phục cho Claude

**Ảnh chụp 10/11/2026, 16:51 Bangkok.** PI/VF chuyển sản phẩm từ hai Google Sheets thành video dọc để người vận hành duyệt. Phạm vi audit là luồng hằng ngày, từ intake đến export và resume. Tải giả định là một người vận hành, xử lý từng sản phẩm tuần tự. Cấu hình hiện giới hạn 20, mục tiêu handbook là 2–5 video dùng được mỗi ngày.

## Nhiệm vụ cho Claude

**Sửa các lỗi làm luồng Sheet → MP4 nghẽn, báo sai kết quả hoặc phải can thiệp tay khi hệ thống có thể tự tiếp tục.** Bắt đầu bằng diff đang làm dở và các phát hiện dưới đây. Với từng mục, xác định `đã sửa trên diff / còn lỗi / cần thêm bằng chứng`. Sửa đến hết các lỗi vận hành đã xác nhận trong phạm vi này. Ghi riêng vấn đề phụ thuộc Google Flow, tài khoản hoặc dữ liệu sản phẩm, cùng cách hệ thống tiếp tục và việc người vận hành cần làm nếu có.

Thứ tự xử lý là **capacity và resume → scheduler và khóa → số liệu export → chất lượng đầu vào**. Sửa tại seam đang gây lỗi và giữ workflow tuần tự, SQLite, Exchange, provider, operation ID, chính sách QC nhẹ và quyền duyệt MP4 của người dùng. Ưu tiên đường chạy 06:30, lệnh `resume` và các bước compose/export từ clip có sẵn. Dùng bảng và Graphify để mở đúng đoạn code, tránh audit lại toàn repo ở mỗi vòng.

### Khi nào coi là vận hành mượt

| Tình huống | Kết quả cần quan sát |
| --- | --- |
| Hai Sheet có sản phẩm đủ ảnh | Snapshot đúng ngày, PI phát gói `READY`, VF xử lý đúng sản phẩm. Mỗi dòng có trạng thái và bước tiếp theo rõ ràng. |
| A timeout sau Send, B cần slot, C đã có đủ clip | A đối soát bằng operation ID cũ. B ghi lý do chờ và có đường tiếp tục khi capacity trở lại. C vẫn ghép và xuất được. Khôi phục A/B giữ lịch sử, tránh gửi lặp. |
| Dừng giữa chừng rồi `resume` | Dùng lại binding đã lưu của ngày chạy, gói Exchange, profile/QC và operation còn dở. Phần đã xong được tái sử dụng. |
| Sheet hoặc đường dẫn input lỗi, một dòng thiếu ảnh, hoặc quota hết | Phân biệt lỗi nguồn, chờ duyệt, chờ provider, bỏ qua và thành công. Tiếp tục phần khả thi. Lịch vẫn có đường chạy tiếp sau khi điều kiện được khôi phục. |
| Lượt 06:30 kết thúc | Ledger, report và receipt khớp danh sách MP4 theo run/workflow. Có MP4 giải mã được của chính lượt đó nếu báo đã xuất. Đo số lần phải can thiệp và thời gian chờ từ artifact/log sẵn có. |

Bàn giao theo từng mục với **bằng chứng trước, file sửa, kết quả sau và phần còn phụ thuộc bên ngoài**. Chỉ thêm hoặc chạy test theo yêu cầu rõ ràng của người dùng và `AGENTS.md`. Tận dụng log, receipt và artifact hiện có trước. Khi cần xác minh bằng provider thật hoặc lượt 06:30, ghi đúng kết quả quan sát được và giữ mục ở trạng thái chưa nghiệm thu cho tới khi có bằng chứng.

## Claude đọc theo thứ tự này

1. Đọc audit này và `AGENTS.md`. Từ `D:/work/pi-vf-next`, chạy `git rev-parse --short HEAD`, `git status --short` và `git diff --stat` để xác định checkout và phần thay đổi đang dở. Bản audit quan sát nhánh `codex/unified-pipeline`, HEAD `d392fff`, với sửa đổi cục bộ ở PI, VF và automation. Worktree `daily-product-flow` tại `8e84878` là checkout khác.
2. Dùng Graphify để định vị hàm và caller. Mỗi lượt chỉ tra một câu hỏi, giới hạn khoảng 600–800 token. Sau đó dùng `rg -n` và đọc đoạn code liên quan trên checkout hiện tại. Nếu Graphify vắng hoặc trả node mơ hồ, đi thẳng theo bảng bên dưới.
3. Với lỗi vận hành, bắt đầu từ `run_id` trong `runtime/scheduler_ledger.json`, đối chiếu `runtime/reports/<ngày>.json`, rồi lấy `workflow_id` và `operation_id` của đúng sản phẩm. Mở artifact hoặc đoạn log của ID đó. Dùng MP4 và receipt cùng lượt để đánh giá xuất video.

```powershell
Set-Location D:/work/pi-vf-next
graphify query 'trigger_daily_run()' --dfs --budget 650
graphify explain 'ExchangeImporter'
graphify query 'SceneRenderOperationStore' --dfs --budget 600
graphify query 'VisualDirector' --dfs --budget 650
```

Graph hiện có tại `graphify-out/graph.json`, được tạo ngày 05/11, trước HEAD đang chạy. Nó có 7.282 node và 22.194 cạnh. `GRAPH_REPORT.md` khoảng 86 KB và `graph.json` khoảng 13 MB. Tra theo tên hàm qua CLI để tránh đưa toàn bộ hai file vào context. Xác nhận cạnh `INFERRED` và số dòng bằng source hiện tại. Nếu `graphify-out/` vắng ở máy Claude, dùng bảng bên dưới. Thư mục graph hiện là dữ liệu local chưa được Git theo dõi.

## Bản đồ luồng chính

| Bước | Điểm mở đầu | Giao kèo cần giữ |
| --- | --- | --- |
| Lịch và điều phối | `Run.ps1` → `automation/cli.py:cmd_trigger` → `automation/scheduler/daily_trigger.py:trigger_daily_run` → `automation/pipelines/spark_pipeline.py:run_pipeline` | Ngày Bangkok, dedupe theo input, một run cha với trạng thái riêng cho từng sản phẩm. Windows task `PI-VF Two Sheets 0630` dùng `--acquire --scenes 4 --qc-level low --provider-mode configured` và `IgnoreNew`. |
| Chọn sản phẩm và thu ảnh | `apps/pi/tools/sheet_snapshot.py:download_snapshots` → `sheet_daily.py:run_daily` → `spaiauto/scheduled.py:acquire_batch` | Hai Sheet là nguồn chính. PI giữ snapshot, chọn dòng, lấy ZIP và review ảnh local theo listing. |
| Bàn giao | `apps/pi/tools/spaiauto/workflow.py:publish` → `spark_pipeline.py:_run_sheet_batches` → `apps/vf/src/video_factory/automation/importer.py:ExchangeImporter` | Mỗi gói có manifest, ảnh và `READY`. Consumer hiện đọc `exchange_batches`; kiểm tra đúng ngày, đúng gói và đúng một `product_key`. `exchange_batch_dir` còn trên đường intake cũ. |
| Sáng tạo và render | Trong `apps/vf/src/video_factory/`, mở `automation/coordinator.py`, `creative/agents/director.py`, `creative/playbooks/food_pov.yaml`, `integrations/scene_renderer.py` | Director chọn ảnh theo hành động. Luồng snack có `source_product` để giữ nhận diện theo nguồn. VF ghim scene, ảnh và prompt trước khi gửi provider. |
| Tiếp tục và xuất | Trong VF, mở `workflows/runtime_control.py`, `workflows/outbox_repository.py`, `integrations/capacity.py` và `integrations/composer.py`. Báo cáo ở `automation/daily_report.py` | `unknown` sau Send cần đối soát operation cũ. Khi đủ clip, tiếp tục ghép và xuất từ clip đã có. Dùng receipt gắn workflow và MP4 để xác nhận kết quả. |

## Phát hiện theo mức ưu tiên

**1. P1 — Flow capacity vẫn làm sản phẩm sau phải chờ.** Incident lúc 15:27 của `run_20261110_59d97e41_s4_n20` ghi `PROVIDER_CAPACITY_WAIT` tại `row_0018` và `row_0020`. Báo cáo cuối lại có MP4 cho hai dòng này sau các lần tiếp tục, nên bằng chứng cho thấy gián đoạn và khả năng hồi phục, chưa xác định lỗi còn tái hiện trên diff hiện tại. Trước khi chỉnh, đọc `runtime_control.py:workflow_can_advance_during_capacity_wait`, `capacity.py:release_exhausted_reconciliation_slot`, và `spark_pipeline.py:_wait_for_flow_keyframe_slot`. Chấp nhận khi sản phẩm có clip sẵn ghép được trong lúc sản phẩm khác chờ, và một lần Send chưa rõ kết quả giữ nguyên operation ID khi đối soát. [Incident](D:/work/pi-vf-next/runtime/reports/incident_run_20261110_59d97e41_s4_n20_20261110_152717.log)

**2. P1 — Doctor đánh giá lock theo tuổi file.** Incident ghi Doctor đã xóa `sheet-*.lock` và `vf-sheet-policy.json.lock`. `clean_stale_locks` dùng `mtime > 300s`, trong khi `exclusive_file_lock` dùng đường dẫn ổn định và để OS nhả lock khi đóng handle. Một file lock cũ có thể hoàn toàn bình thường. Tách hai loại lock trước khi dọn. Tiêu chí là Doctor giữ file dùng cho khóa OS và xác định lease hoặc chủ tiến trình ở các đường cần hồi phục. Chưa quan sát thấy lần ghi trùng nào từ sự cố này. [Doctor](D:/work/pi-vf-next/automation/doctor_agent.py:131) · [Khóa OS](D:/work/pi-vf-next/automation/storage.py:32)

**3. P1 — Số MP4 chưa đối soát được theo cùng run.** Ledger lúc 15:27 ghi `partial`, `mp4_count=3`. Daily report có 20 dòng sản phẩm và hai outcome `success` kèm path. Cả hai MP4 của `row_0018` và `row_0020` mở được bằng `ffprobe` và giải mã hết video bằng FFmpeg, 1080×1920, 32 giây mỗi file. MP4 thứ ba chưa xác định. Ledger đếm phần tử `exported_videos`, report ghép outcome theo `workflow_id`. Đối soát danh sách export bằng run ID, workflow ID và receipt trước khi sửa cách đếm. Mã `0` của task lúc 06:30 cũng chưa nói có MP4 vì CLI trả `0` cho `skipped`. [Ledger](D:/work/pi-vf-next/runtime/scheduler_ledger.json) · [Daily report](D:/work/pi-vf-next/runtime/reports/2026-11-10.json) · [Mã thoát](D:/work/pi-vf-next/automation/cli.py:377)

**4. P2 — Intake và creative cần kiểm riêng.** Daily report ghi 5 dòng `identity_unverified`. Phân biệt listing thật sự mơ hồ với ảnh đã đủ nhận diện theo nguồn. Với video quà bánh, kiểm tra ảnh detail có xuất hiện ở cảnh giữa, thao tác có bằng chứng và bao bì có dễ nhận ra ở cảnh cuối. Rule MuseAI đã review ở mức đề xuất. Điểm sửa nhỏ nhất là `food_pov.yaml` và prompt Director. Giữ schema, critic và QC sẵn có. Chấp nhận bằng đối chiếu ảnh, prompt thực gửi và cảnh liên quan.

**5. P1 — Đầu vào mất có thể được báo `skipped` với mã `0`.** `should_run_today` trả `False` cho cả CSV không tồn tại và lượt đã hoàn thành. Caller biến cả hai thành `skipped`, rồi CLI trả `0`. Trường hợp đường dẫn snapshot/selection mất khiến lịch có thể trông như hoạt động bình thường. Tách lỗi đầu vào khỏi skip do trùng. Kiểm thêm nhánh Sheet có status lỗi nhưng chưa có CSV, để thông báo chỉ rõ nguồn hoặc bước thất bại. [Quyết định lịch](D:/work/pi-vf-next/automation/scheduler/daily_trigger.py:95) · [Nhánh trả kết quả](D:/work/pi-vf-next/automation/scheduler/daily_trigger.py:149)

**6. P1 — Rà ranh giới chờ tiến trình con và `resume`.** Hai lời gọi đồng bộ PI/VF trong `spark_pipeline.py` không có deadline ở tiến trình cha. Browser Spaiauto có timeout ở từng thao tác, nhưng nếu tiến trình con hoặc MCP mắc kẹt ngoài các timeout đó, luồng tuần tự sẽ chờ và `IgnoreNew` có thể khiến lịch kế tiếp không khởi động. `resume` cũng có thể gọi PI intake trước khi đọc policy run đã lưu. Đây là hai đường cần xác nhận bằng log/timing và binding hiện có. Nếu sửa, dùng cơ chế timeout/lease đang có, lưu checkpoint rồi giữ operation cũ nếu timeout xảy ra sau Send. Với resume, tải binding đã lưu trước việc thu ảnh lại khi binding đủ dữ liệu. [PI subprocess](D:/work/pi-vf-next/automation/pipelines/spark_pipeline.py:482) · [VF subprocess](D:/work/pi-vf-next/automation/pipelines/spark_pipeline.py:991) · [Thứ tự resume](D:/work/pi-vf-next/automation/pipelines/spark_pipeline.py:548)

**7. P2 — Kiểm chứng độc lập đường dẫn khi cutover.** Spaiauto còn dò thư mục dữ liệu lịch sử của checkout PI gốc, và incident cho thấy export dùng thư mục tài nguyên ngoài repo qua junction. Chưa xác định đường nào chỉ là fallback, đường nào cần cho lần chạy tiếp theo. Kiểm kê binding đang dùng và phân biệt kho dữ liệu được giữ có chủ đích với phụ thuộc source checkout cũ. Chỉ đổi binding sau khi xác định dữ liệu nào cần chuyển. [Spaiauto](D:/work/pi-vf-next/apps/pi/tools/spaiauto/scheduled.py:101) · [Roadmap cutover](D:/work/pi-vf-next/docs/platform/08-migration-roadmap.md:14)

## Ranh giới bàn giao

- **Tái sử dụng** PI intake, Exchange importer, VF job/outbox, creative và composer. **Cấu hình** theo task và provider thực tế của checkout chạy. **Sửa tối thiểu** tại seam recovery/reporting sau khi có ID và bằng chứng. **Phần thiếu cần xác nhận** là đối soát MP4 thứ ba và lần chạy 06:30 có receipt gắn đúng lượt.
- `AGENTS.md` trên working tree hiện yêu cầu tránh tự viết unit test hoặc chạy test suite khi người dùng chưa yêu cầu. Audit này dùng đọc source, Graphify, Windows Task Scheduler, ledger, báo cáo và giải mã hai file hiện có. Không khởi chạy batch, resume, provider hay suite. Chỉ thêm tài liệu này.
- Handbook §2 và §6 lưu các mốc cũ như `9863eeb` và `--qc-level high`. Đối chiếu source, task và artifact mới trước khi dùng các mốc đó làm hiện trạng. Chưa kiểm toàn bộ Legacy Discovery, Studio UI, bảo mật hay cutover khi checkout gốc không còn truy cập được.
- Với Legacy Discovery, Studio UI, auth/secrets và migration, Claude rà các điểm chạm vào luồng chính hoặc thay đổi trong diff. Mở rộng sang phần riêng của các hệ này khi có lỗi cụ thể ảnh hưởng vận hành. Phạm vi bàn giao này ưu tiên chạy bền cho một người vận hành.

**Kết luận tại thời điểm audit** — Luồng chính đã có MP4 thật. Bằng chứng còn thiếu để xác nhận lịch hằng ngày vận hành ổn định là đối soát export theo đúng run và kiểm chứng cô lập lỗi capacity giữa các sản phẩm.
