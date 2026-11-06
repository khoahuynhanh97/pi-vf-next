# Product Intelligence — File-first

Luồng chính: **Excel/CSV → danh sách sản phẩm và link nguồn → Spaiauto qua browser → ảnh/video + thông tin → `resources.json`**.

Đối với hai Google Sheets đã cấu hình, dùng [Sheets snapshot → agent selection → PI](docs/sheet-selection-agent.md). `tools.sheet_daily` tải snapshot XLSX, chuẩn hóa pool, gọi agent 9Router chọn tối đa 15, tạo READY handoff, tiếp nhận vào PI và đối soát ACK. Lệnh này có thể chạy lại cùng ngày mà không chọn hoặc tạo batch mới. Acquisition Spaiauto và review/publish vẫn là bước tiếp theo; hiện chưa có lịch Windows hoạt động cho luồng này. Handoff mở rộng `spaiauto start --file`, lưu receipt `pi_csv_intake_v1` vào SQLite nghiên cứu hiện có, và root `publish` tạo một Exchange `run_*` batch.

Discovery tự chọn sản phẩm/trend đang tạm dừng. `daily_scout.py run`, `daemon`, `test-prompt`, daily runner và trend discovery service bị chặn bởi `product_scout/workflow_policy.py`. Không dùng request.txt hoặc catalog để bổ sung sản phẩm ngoài file.

```powershell
python -m tools.spaiauto start --file 'G:\My Drive\PI-input\products.xlsx'
```

Cột khuyến nghị: `Tên sản phẩm`, `Danh mục`, `Giá bán tham khảo`, `Link sản phẩm`. CSV hỗ trợ cả `product_name`, `source_url`. Dòng chỉ có link vẫn được giữ; dòng thiếu link được đánh dấu cần bổ sung. Mở link search/shop/category để chọn đúng sản phẩm của dòng, không tìm sản phẩm thay thế.

Agent thực hiện browser scan/download theo [hướng dẫn](docs/spaiauto-flow.md). CLI không tự điều khiển browser. Mỗi dòng xử lý tuần tự, giữ nguồn và trạng thái lỗi; không dừng cả batch vì một sản phẩm không tải được.

Đầu ra chính `resources.json` schema `1.0.0`: `products[]` có tên/mô tả/link, `images[]`, `videos[]`, `video_urls[]`, đường dẫn local, trạng thái tài nguyên và nguồn file/dòng. Brand/model có thể null. `review_status=needs_visual_review` không cản xuất tài nguyên nhưng không có nghĩa ảnh/video đã được duyệt. Không cần xác minh sâu SKU để xuất gói nguyên liệu.

Video chỉ được tính là đã tải sau khi agent lưu file thật, chạy `attach-video --file <local_video> --source-url <observed_https_url>` và ffprobe đọc được video stream. Không có video thì xuất `videos: []`; không chặn ảnh/thông tin. URL video chưa tải chỉ nằm ở `video_urls`.

Sau mỗi dòng và cuối batch chạy `summary --run-dir <run>` để tổng hợp `resources.json`, `summary.json` và báo cáo. Publish root từ selection handoff là luồng riêng có gate review/identity và hỗ trợ `--eligible-only`; publish child tương thích cũ vẫn giữ gate nghiêm ngặt.

Việc tạm dừng nằm trong mã nguồn. Chưa chứng minh OS scheduler/process trên máy khác đã dừng hoặc tự reload mã mới. Không có lịch nhập ổ G mới được cài.

## Legacy discovery (paused)

Research 10 smart tech products priced 200k-500k VND.

## TikTok Shop Snacks discovery

For a food or snack request that includes TikTok, the daily discovery flow also opens the [TikTok Shop Snacks category](https://shop.tiktok.com/vn/c/snacks/915336). Product cards become PDP candidates, with duplicate links removed and displayed prices checked against the requested range. The existing scoring and portfolio selection then choose products. A selected category product uses images from its own TikTok PDP gallery for media research.

The former `daily_scout.py test-prompt` flow is paused. The implementation remains for a future explicit reactivation; it is not a fallback for file acquisition.
