# Nguyên tắc làm việc

Nói thẳng, trung thực, ngắn gọn. Nêu rõ điều đã kiểm chứng và điều chưa chắc.
Giữ nguyên thay đổi không liên quan; không tự commit hoặc push.

## Luồng lấy sản phẩm ưu tiên

**Cập nhật 2026-10-30:** tạm dừng PI tự discovery/chọn sản phẩm từ trend, prompt hoặc catalog. Luồng chính nhận Excel/CSV và chỉ lấy sản phẩm nằm trong file. Dòng thiếu link đánh dấu needs_review, không tự tạo truy vấn sản phẩm mới. Link search/shop/category có sẵn được mở để resolve sản phẩm tương ứng. Dòng chỉ có link vẫn xử lý được.

Đầu ra nguyên liệu chính là `resources.json` (schema 1.0.0), gồm thông tin, ảnh, file video đã tải và nguồn file/dòng. Kiểm tra hình ảnh đúng loại sản phẩm ở mức cơ bản; brand/model chưa biết để null, không phải chặn xuất nguyên liệu. Review/publish exchange cũ vẫn giữ gate riêng.

Video có thể được tải qua browser rồi gắn bằng `attach-video --file <local> --source-url <url>`; cần ffprobe. URL chưa tải không tính là file video. Thiếu video không chặn ảnh/thông tin. Không tự đăng ký lịch mới hoặc bật lại discovery. Các hướng dẫn cũ bên dưới áp dụng trong phạm vi không mâu thuẫn cập nhật này.

Khi nhận danh sách/bảng sản phẩm, file hoặc lệnh **“start PI”**, đọc toàn bộ
đầu vào rồi xử lý **tuần tự từng dòng**: tìm/chọn một PDP phù hợp → Spaiauto
qua browser → tải ZIP + capture thông tin → import → duyệt ảnh → export.
Đầu vào đã có link trực tiếp cũng dùng flow này hoặc subflow init.
Đọc và thực hiện [hướng dẫn Spaiauto](docs/spaiauto-flow.md).

- Entry point: `python -m tools.spaiauto` từ root project.
- Với danh sách/file: `python -m tools.spaiauto start --file <path>`; hỗ trợ
  TXT/Markdown, CSV, XLSX, DOCX. Danh sách dán trong chat được agent lưu thành
  file nguồn nguyên văn. Người dùng không cần tự lấy PDP, viết JSON hay chạy CLI.
- Dùng `next` để lấy dòng hiện tại. Một dòng chọn tối đa một sản phẩm; không
  tách “Skin1004 / Cocoon / Sica White” thành ba dòng. Ghi chosen_alternative,
  thương hiệu/model/biến thể thật nếu xác định được, lý do chọn và ứng viên đã xem.
- Link /search, shop, danh mục chưa phải PDP. Agent tìm PDP qua browser, ghi
  evidence bằng `resolve`, rồi xử lý child acquisition run. Không xử lý trước
  dòng kế tiếp; phải `finish` completed/needs_review/skipped cho dòng hiện tại.
- Hết batch chạy `summary`. Giữ tiêu chí giá gốc, raw metadata và liên kết mỗi
  kết quả về đúng dòng đầu vào; số liệu trend/lượt bán trong file chưa được xác minh.
- Thực hiện browser capture bằng browser tools được cấp cho agent. CLI local
  không tự điều khiển browser và không chứng minh có tự động hóa chạy lịch.
- Với batch, tiếp tục item còn lại sau lỗi/skip; tối đa 1 lần đầu + 2 retry
  cho mỗi bước search/scan/download, lưu lý do. Không tự bỏ qua captcha hoặc login.
- Giữ ZIP và capture gốc; kiểm tra ảnh thật trước khi chọn, không đoán model,
  biến thể, giá SKU hay khẳng định đủ ảnh chỉ dựa vào số file.
- Flow code cũ vẫn giữ nguyên và dùng được khi người dùng yêu cầu. Không tự gọi
  crawler cũ để thay Spaiauto khi lỗi; không đổi schedule hoặc MCP mặc định.
- Yêu cầu chỉ có tên sản phẩm hoặc link tìm kiếm được PI resolve trước khi gọi
  Spaiauto; không nói chính Spaiauto có khả năng discovery.
