# PI/VF Consolidation & Automation Integration

Follow **Move → Verify → Integrate → Automate → Optimize**. The original PI and VF applications are the source of business behavior. The pilot in this repo is reference material for orchestration.

- Preserve existing features, Legacy Discovery controls, providers, entrypoints, models, migrations, job state and recovery semantics.
- Before proposing changes, identify source evidence for what to reuse unchanged, move or reconfigure, adapt minimally, and implement because it is missing.
- Keep source consolidation separate from runtime consolidation. Preserve existing database schemas and application boundaries during the move. Prefer configuration or a small integration adapter when a seam is missing.
- Avoid rewrites, unrelated refactors, duplicated business logic and new frameworks or abstractions without demonstrated need.
- **Tập trung code thực chiến:** KHÔNG tự ý viết unit test, sinh file test hoặc tự động chạy test suite sau khi code/sửa code trừ khi người dùng yêu cầu rõ ràng.
- Phase 01 acceptance requires independent installation and offline verification of both transferred apps with temporary data. Final cutover requires the unified pipeline and scheduler to run without either original checkout path.

## Ưu tiên xuất video POV trong Daily Trigger

- Sản phẩm có trạng thái `failed` thì giữ trạng thái, ghi lý do và chuyển sang sản phẩm đủ điều kiện tiếp theo trong cùng lượt. Khi cần bù sản lượng, lấy sản phẩm khác theo luồng intake hiện có và trong giới hạn của lượt chạy. Không tự mở lại sản phẩm đã `failed` trong lượt đó.
- Trước khi chốt `failed`, cho tối đa một lần cứu tự động nếu xác định được lỗi cục bộ và có cách sửa trực tiếp. Ví dụ, tạo lại đúng keyframe sai hoặc ghép lại từ clip đã xác minh. Giữ phần đã đạt. Hết lượt cứu hoặc thiếu bằng chứng để sửa thì chuyển sản phẩm.
- Với yêu cầu provider chưa rõ kết quả, giữ operation ID và trạng thái đối soát. Tiếp tục ghép hoặc xuất các video đã đủ clip, hoặc xử lý sản phẩm khác khi có capacity phù hợp. Nếu hết việc khả thi, báo lý do và kết thúc lượt. Giữ QC `low`, xác minh đúng sản phẩm, MP4 phát được và receipt khớp file. Giữ trần 20 video hoàn tất mỗi ngày.
- Khi sửa Daily Trigger, chốt một ca nghiệm thu cụ thể. Sản phẩm A lỗi, sản phẩm B vẫn xuất được MP4 hợp lệ. Ưu tiên lỗi tái hiện được đang chặn sản phẩm khác hoặc làm sai đầu ra. Dừng vòng sửa khi ca đã đạt. Nếu còn vướng sau bản sửa tối thiểu, bàn giao bằng chứng và điểm chặn cụ thể trước khi đề xuất mở rộng phạm vi. Ghi lỗi riêng từng sản phẩm để xử lý sau.

The [four-phase roadmap](docs/platform/08-migration-roadmap.md) and [Master Prompt](docs/platform/MASTER-PROMPT.md) govern delivery. Older architecture and pilot documents provide context.

Start with the [PI/VF handbook](docs/PI-VF-HANDBOOK.md) for the verified runtime snapshot, current scope, and release blockers. Check the working directory, HEAD, and local diff before editing. The scheduled runtime checkout and development worktree may differ. Report the checkout, exact verification, and remaining gaps when handing off work.
