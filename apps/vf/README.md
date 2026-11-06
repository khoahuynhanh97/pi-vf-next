# 🎬 AI Video Factory (Standalone Edition)

> **Scope release cá nhân hiện hành (PR-1, 2026-10-25):** đọc [master idea, spec, plan và agent handoff](docs/release-personal/README.md) trước khi triển khai. Ưu tiên batch thông suốt, QC thực dụng, keyframe tối đa 1+2 chọn ảnh hợp lệ tốt nhất, BLOCKED riêng item có cách sửa; không final LLM content QC bắt buộc. Các mô tả gate/HITL bên dưới là tổng quan có trước, không ghi đè PR-1.

Hệ thống sản xuất video ngắn tự động chất lượng cao (TikTok/Shorts/Reels) được phát triển độc lập từ pipeline **Product-Intelligence → Video Factory**.

## 🌟 Tính năng cốt lõi
1. **Grounded Creative Engine:** Không sinh prompt sáo rỗng; bám sát tính năng, USP, pain-point và review thật của sản phẩm.
2. **5 Cổng kiểm duyệt (HITL / Auto-Approve):**
   - **Gate 1:** Product Lock (Chốt sản phẩm và tư liệu hình ảnh).
   - **Gate 2:** Creative Brief (Duyệt góc tiếp cận, tone & mood, hook).
   - **Gate 3:** Script & Scene Plan (Duyệt kịch bản phân cảnh chi tiết).
   - **Gate 4:** Storyboard & Keyframes (Duyệt hình ảnh tạo ra trước khi render video).
   - **Gate 5:** Final Video Approval (Duyệt video hoàn chỉnh trước khi xuất file).
3. **Quality Control (QC) Fail-Closed:** Kiểm định độ nét, tỷ lệ khung hình, chống méo mó, chống text rác tự động.
4. **Đa dạng Render Provider:** Hỗ trợ Google Vertex AI (Veo 3.1) và Google Flow automation.

---

## 🚀 Cài đặt & Chạy

### 1. Cài đặt môi trường
Sử dụng `uv`:
```bash
cd D:\work\video-factory
uv venv
uv sync
```

### 2. Cấu hình
Copy và điền API keys / cấu hình đường dẫn:
- `config/config.yaml`
- `config/.env`

### 3. Lệnh vận hành chính (CLI)
```bash
# Kiểm tra trợ giúp
uv run video-factory --help

# Chạy pipeline cho 1 sản phẩm từ Product Intelligence
uv run video-factory run --product-id <PRODUCT_ID>

# Chạy server MCP (để cắm vào Claude, Cursor, Hermes khi cần)
uv run python -m video_factory.integrations.server
```

---

## 📂 Cấu trúc dự án
```text
video-factory/
├── config/             # Cấu hình runtime và secrets
├── docs/               # Đặc tả kỹ thuật (Master Spec 2.0)
├── src/
│   └── video_factory/  # Mã nguồn cốt lõi (Pipeline, Creative, QC, Storage, Integrations)
└── tests/              # Bộ kiểm thử chất lượng
```
