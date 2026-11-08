# Test: Independent Product Flow

## Mục tiêu

Kiểm chứng từng sản phẩm đi độc lập, một sản phẩm lỗi không chặn sản phẩm khác.

## Ca kiểm chứng

### Setup

1. PI xuất 3 gói Exchange:
   - Product A: ảnh bị lỗi keyframe (sai sản phẩm hoặc hỏng)
   - Product B: keyframe OK nhưng render hết quota
   - Product C: tất cả clip OK

2. VF nhận 3 gói → xử lý song song

### Expected

- **Product A**: keyframe lỗi → tạo lại keyframe (max 2 retry) → render → MP4 output
- **Product B**: keyframe OK → render gặp `PROVIDER_CAPACITY_WAIT` → skip, log "quota exhausted", không có MP4
- **Product C**: keyframe OK → render OK → MP4 output

### Assert

1. A và C có MP4 file trong output
2. B có log chứa "PROVIDER_CAPACITY_WAIT" hoặc "quota", không có MP4
3. Báo cáo ngày ghi cả 3 sản phẩm:
   - A: link MP4 + ghi "keyframe_retried"
   - B: reason "quota_exhausted"
   - C: link MP4
4. Worker không exit sau B lỗi quota, vẫn xử lý C

## Thực hiện

### PI side

```bash
cd D:/work/pi-vf-next/apps/pi
python -m tools.sheet_daily \
  --root data/test_independent \
  --day 2026-11-08 \
  --db-path runtime/test.db \
  --inbox-root ../vf/runtime/exchange \
  --acquire \
  --limit 3
```

Kiểm tra output:
- `status="success"`
- `exchange_batches` có 3 items với `row_id` khác nhau

### VF side

```bash
cd D:/work/pi-vf-next/apps/vf
python -m video_factory.workflows.worker \
  --db-path runtime/test.db \
  --once \
  --provider-mode=configured
```

Giả lập lỗi:
- Mock keyframe provider trả lỗi cho product A lần đầu
- Mock render provider trả `PROVIDER_CAPACITY_WAIT` cho product B
- Product C không mock

### Verify

```python
# Check MP4 outputs
import json
from pathlib import Path

output_dir = Path("runtime/outputs/2026-11-08")
products = list(output_dir.glob("*/final.mp4"))
assert len(products) == 2  # A và C

# Check logs
log = Path("runtime/daily_report_2026-11-08.json").read_text()
report = json.loads(log)
assert report["products"][0]["status"] == "completed"  # A
assert "keyframe_retried" in report["products"][0].get("notes", "")
assert report["products"][1]["status"] == "skipped"  # B
assert "quota" in report["products"][1]["reason"].lower()
assert report["products"][2]["status"] == "completed"  # C
```

## Kết quả mong đợi

Pipeline xử lý 3 sản phẩm độc lập. B lỗi quota không ngăn A và C xuất MP4.

## Lưu ý

- Retry keyframe tối đa 2 lần: cấu hình trong `operator_handler.py:311`
- Quota pause không dừng worker: thay đổi trong `worker.py:225-228`
- Per-product publish: thay đổi trong `sheet_daily.py:73-100`
