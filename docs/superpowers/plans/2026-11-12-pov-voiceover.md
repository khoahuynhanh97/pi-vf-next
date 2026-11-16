# Kế hoạch triển khai lời thoại cho video POV

**Mục tiêu.** Tạo một MP4 có voice tiếng Việt từ một export đã xác minh và giữ bản gốc sẵn sàng sử dụng.

**Thiết kế.** `Run.ps1` gọi CLI hiện có. VF chịu trách nhiệm kiểm nguồn, soạn lời, tạo audio và xuất bản riêng. Thử thủ công trên ba MP4 thật trước khi thêm voice vào Daily Trigger.

**Công cụ.** Python, `httpx` có sẵn, mô hình JSON cấu hình trong VF, ElevenLabs, FFmpeg/ffprobe và pytest.

**Spec.** [Spec thêm lời thoại cho video POV](../specs/2026-11-12-pov-voiceover-design.md)

**Cách thực hiện.** Áp dụng ECC TDD Workflow. Mỗi lượt viết một ca tại giao diện công khai, quan sát RED do thiếu đúng hành vi rồi sửa nhỏ nhất để đạt GREEN. Kiểm thử tự động trong plan dành cho lần triển khai được giao. Phiên lập plan không tạo test, chạy suite, gọi TTS hoặc đổi scheduler. Nghiệm thu chất lượng giọng dùng video thật.

## Tệp và ranh giới

| Tệp | Thay đổi |
|---|---|
| `automation/cli.py` | Thêm `voiceover` với `--draft` hoặc `--script`, gọi VF và in đường dẫn hoặc lý do. `--scene-notes` chỉ dùng với `--draft`. Dùng `Run.ps1` hiện có. |
| `apps/vf/src/video_factory/application/voiceover.py` | Thêm `draft_voiceover(export_dir, voice_root, *, notes_path=None) -> Path` và `create_voiceover(export_dir, voice_root, *, script_path) -> ExportPackage`. Dùng `configured_creative_model()` và `StructuredCreativeModel.generate_json()` cho bản nháp. Mọi nguồn đọc được phải gắn với chính export đang xử lý. |
| `apps/vf/src/video_factory/integrations/elevenlabs_tts.py` | Thêm adapter hẹp cho một endpoint. `get_or_create_voice_audio(*, voice_root: Path, source_sha256: str, tts_text: str) -> tuple[Path, str | None]` trả đường dẫn audio đã kiểm và request ID nếu có. Đọc `ELEVENLABS_API_KEY` và `ELEVENLABS_VOICE_ID`, dùng `httpx`. |
| `apps/vf/src/video_factory/integrations/export_package.py` | Thêm `mirror_to_hub: bool = True` vào `build_export_package()`. Call site voice dùng `False`, bảo toàn mặc định của export thường. Dùng lại `probe_video()` và `validate_export()` trong `integrations/composer.py`. |
| `apps/vf/tests/test_voiceover.py` | Một tệp cho các ca trọng điểm bên dưới. Dùng thư mục tạm, MP4 cục bộ và HTTP giả ở ranh giới provider. Kiểm hành vi qua CLI và file nhận được. |

## 1. Kiểm nguồn và soạn lời

- [x] **RED.** Thêm `test_voiceover_draft_binds_source_and_rejects_tampered_input`. `--draft` với receipt đúng tạo bản nháp khóa SHA và danh tính. Bản nháp dùng dữ kiện do chủ công cụ cung cấp. Khi chỉ có receipt, đầu vào mô hình chỉ chứa danh tính và chỉ dẫn viết lời chung. SHA sai hoặc nguồn đã có audio thì dừng trước khi gọi mô hình hay TTS. Xác minh lệnh trả lý do cụ thể. Chạy ca với `-k voiceover_draft` và ghi lỗi thiếu hành vi.
- [x] **GREEN.** Thêm hai chế độ CLI và hoàn thiện `draft_voiceover()`. Thử JSON có đủ `source_video_sha256`, `product_identity`, `display_text`, `tts_text`. Nếu chưa có mô hình, ghi mẫu để điền tay. Không đưa caption hoặc dữ liệu seller chưa xác minh vào prompt. Cùng nguồn thì giữ bản nháp người dùng đã sửa, tránh ghi đè âm thầm. Chạy lại đúng ca.
- [x] **Bàn giao.** CLI in đường dẫn JSON. `--draft` chỉ ghi bản nháp, chưa phát sinh TTS. Có thể quay lui bằng cách bỏ command thử nghiệm, bản gốc không cần phục hồi.

## 2. Tạo audio và bảo vệ credit

- [x] **RED.** Qua `get_or_create_voice_audio()`, thêm `test_voiceover_reuses_audio_and_blocks_duplicate_after_uncertain_post`. Quan sát tại HTTP giả. Cùng đầu vào chỉ có một POST khi chạy lại hoặc khi hai lệnh chạy trùng. Timeout sau Send vẫn giữ kết quả chưa rõ khi mở lại trạng thái từ đĩa. Thêm `test_voiceover_handles_confirmed_rejections` với `402`, hai lần `429` và `Retry-After` dài hơn 5 giây. Xác nhận có thể chạy thủ công lại cùng lời sau khi sửa lỗi từ chối chắc chắn. Chạy `-k 'voiceover_reuses_audio or voiceover_handles_confirmed_rejections'` và ghi RED.
- [x] **GREEN.** Dùng digest ổn định và dấu bắt đầu được tạo độc quyền trên đĩa trước POST. Mất tiến trình khi đã có dấu thì xử lý như `voice_unknown` nếu thiếu audio hoàn chỉnh. Ghi audio qua file tạm, xác minh trước khi dùng lại. Nếu lỗi chắc chắn, cập nhật dấu để lần gọi thủ công sau có thể tiếp tục. Áp dụng timeout và giới hạn retry trong spec, tắt mọi retry tự động của HTTP client. Kiểm văn bản và binding trước khi đặt dấu. Log mã lỗi/request ID đã lọc, không ghi key. Chạy lại đúng hai ca.
- [x] **Bàn giao.** In trạng thái `voice_unknown` cùng hướng xử lý khi kết quả chưa rõ. Với lỗi xác nhận, in lý do cụ thể như `voice_no_credit` hoặc `voice_rate_limited`. Khi cần quay lui, dừng dùng command hoặc thu hồi key TTS. Audio và trạng thái cũ vẫn đủ để đối chiếu.

## 3. Ghép và đóng gói phiên bản có voice

- [x] **RED.** Thêm `test_voiceover_exports_new_receipt_without_touching_source_or_hub`. Từ MP4 và audio thật cục bộ, xác nhận bản mới có AAC giải mã được, giữ số khung hình, receipt khớp SHA và CLI duyệt đúng bản mới. So SHA file gốc, verdict cũ và bản Hub trước/sau. Chạy lại khi chỉ lỗi ghép và kiểm số POST vẫn là một. Audio dài hơn video phải trả `voice_too_long` và không phát hành MP4 cắt lời. Chạy `-k voiceover_exports_new_receipt` và ghi RED.
- [x] **GREEN.** Nối `create_voiceover()` vào `--script`. Kiểm binding nguồn và script trước khi gọi `get_or_create_voice_audio()`. Ghép vào file tạm bằng FFmpeg, sao chép track hình, thêm AAC, lấp khoảng trống cuối theo thời lượng hình. Giải mã kiểm cả audio và video trước khi đóng gói. Dùng `build_export_package(mirror_to_hub=False)` dưới `voiceovers/exports/`. Khóa phiên bản theo nguồn, script và cấu hình audio. Lưu provenance theo spec và dùng lại gói khi chạy đúng cùng đầu vào. Nếu chỉ sửa lời hiển thị, dùng lại audio. Chạy lại đúng ca.
- [x] **Bàn giao.** Chạy `Run.ps1 voiceover <export_dir> --draft`, duyệt bản lời, sau đó chạy `Run.ps1 voiceover <export_dir> --script <voice_script.json>`. Mở file mà CLI in ra. Ghi verdict bằng `Run.ps1 human-verdict <voice_export_dir> --verdict approve --cause other --reason "Giọng rõ, lời khớp hình"`. Để quay lui, chọn MP4 gốc để đăng.

## Bằng chứng triển khai

- Checkout `3839948`. Quan sát RED khi chưa có lệnh draft, chưa có adapter TTS và chưa có bước ghép. Ca track AAC bị cắt cũng từng RED dù thời lượng track hình giữ nguyên.
- GREEN gồm 5 ca trong `apps/vf/tests/test_voiceover.py`. Kiểm lệnh CLI, trạng thái sau timeout, không gửi trùng, bản nháp không đè lời đã sửa, từ chối audio bị cắt, đóng gói và duyệt theo SHA. Dùng audio cục bộ và HTTP giả.
- Đã xác minh receipt và giải mã ba MP4 nguồn thật. Mỗi file 32 giây, 960 khung và chưa có audio. Phiên hiện tại thiếu `ELEVENLABS_API_KEY` và `ELEVENLABS_VOICE_ID`, nên mục 4 còn chờ nghe và duyệt bản voice thật.

## 4. Nghiệm thu thật và điều kiện nối Daily Trigger

- [ ] Dùng ba MP4 thật đã nêu trong spec. Nghe và chốt một `voice_id`, sau đó tạo bản voice cho từng sản phẩm. Ghi đường dẫn, SHA, verdict, thời gian chạy/duyệt và số credit quan sát được. Dùng kết quả này để chốt giới hạn thời gian và credit cho lần tích hợp.
- [ ] Khi cả ba bản đạt, dùng điểm đã xác minh export trong `automation/pipelines/spark_pipeline.py`, hàm `verify_and_record_export()`, để lấy danh sách MP4 nguồn của lượt hiện tại. Sau khi pipeline đã ghi kết quả sản xuất, mới chạy voice theo tùy chọn mặc định tắt. Ghi `voice_status`, `voice_reason`, đường dẫn nếu thành công và số có voice trong `automation/daily_report.py`. Dùng một phiên bản để duyệt, đếm mỗi nguồn một lần và giữ bản gốc khi voice lỗi. Chốt nguồn dữ liệu viết lời tự động từ dossier có binding trước khi bật. Scheduler đang chạy là điểm vận hành hiện hữu.
- [ ] **Ca TDD cho bước tích hợp.** Sau khi có lệnh triển khai bước này, tái hiện một lượt A lỗi voice, B có voice và cả hai MP4 gốc vẫn được ghi nhận. Kiểm ca hết giờ, hết credit và rerun không gửi trùng, không tăng bộ đếm MP4. Bằng chứng cuối lấy từ một lượt thực tế. Tắt tùy chọn voice để quay lui.

## Lệnh TDD khi triển khai

Chạy từ root `D:\work\pi-vf-next`. Thay biểu thức `-k` bằng đúng ca của từng bước.

```powershell
.\apps\vf\.venv\Scripts\python.exe -m pytest apps/vf/tests/test_voiceover.py -q -k voiceover_draft
```

Ghi tên ca và lý do RED, kết quả GREEN của cùng lệnh, HEAD kiểm chứng và chỗ còn cần nghe thử. TDD tập trung vào giao diện CLI, số POST và file đầu ra. Giữ kiểm thử trong scope voice.
