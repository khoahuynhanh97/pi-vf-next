# Lộ trình triển khai kiến trúc chuẩn

Ngày 2026-11-03, Asia/Bangkok. [DESIGN](DESIGN.md) là đặc tả đích. [IMPLEMENTATION](IMPLEMENTATION.md) là nhật ký bản đầu. Các mốc dưới đây là việc tiếp theo.

| Mốc | Phạm vi | Điều kiện đạt |
|---|---|---|
| 1. Nguồn thật | Dùng chung parser cho Excel local và snapshot Sheet. Kiểm một listing từ đầu vào đến ZIP và ảnh | Đúng listing/variant, thấy lý do lọc, ảnh hỏng riêng lẻ không chặn bộ ảnh hợp lệ, ZIP đã có phục hồi được |
| 2. Lock và biên kịch | Chọn ảnh theo góc, khóa chi tiết có dẫn chứng, biên kịch theo hợp đồng JSON | Đối chiếu đơn vị tương đương, từ chối hành động mâu thuẫn, gắn đúng ảnh từng cảnh. Thử Antigravity có giám sát trên cùng bộ dữ liệu |
| 3. Plan 3–5 cảnh | Sửa đồng bộ validator, hạn mức, checkpoint và FFmpeg export | Test 3/4/5 cảnh, thứ tự và thời lượng theo plan. Đọc lại batch 4 cảnh cũ, xử lý thiếu quota trước Send |
| 4. QC và phục hồi | Bỏ gate 9:16 ở QC ảnh, kiểm chi tiết theo scene, xuất storyboard, lấy mẫu clip | Keyframe khác tỷ lệ nhưng đúng sản phẩm qua QC. Nhãn sai hoặc drift bị phát hiện. Retry sửa đúng scene, recovery chỉ lấy lại operation cũ |
| 5. Pilot vận hành | Một sản phẩm thật qua hết luồng, sau đó batch nhỏ rồi tối đa 15 | MP4 decode đạt, kiểm trực quan đúng sản phẩm, ghi thời gian và số lượt theo bước. Restart giữa chừng và xác nhận số Send không tăng với operation chưa rõ |

Ưu tiên kiểm chứng một listing đến ZIP ngay khi môi trường sẵn sàng. Giữ một runner và một folder mỗi sản phẩm ở mọi mốc. Chi phí tối thiểu của batch là một keyframe và một clip mỗi scene, cộng model chọn, lock, kịch bản và QC. Đo số thực tế trước khi tăng batch hoặc mức song song.
