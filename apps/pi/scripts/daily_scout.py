"""Daily Product Scout CLI.

Subcommands:
  run          Execute one daily run with catch-up logic (ideal for Task Scheduler/Cron).
  daemon       Run persistent background process with catch-up on startup.
  test-prompt  Dry-run prompt parsing and candidate discovery without downloading images.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

# Fix Windows console encoding for Vietnamese characters
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Safe default for imagehash in stdio/background tasks
os.environ.setdefault("PRODUCT_RESEARCH_DISABLE_IMAGEHASH", "false")

from product_scout.daily_parser import DailyPromptParser  # noqa: E402
from product_scout.daily_runner import DailyScoutRunner  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("daily_scout")


def cmd_run(args: argparse.Namespace) -> int:
    runner = DailyScoutRunner(base_dir=PROJECT_ROOT)
    target_date = None
    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d").date()

    print(f"=== KHỞI ĐỘNG DAILY PRODUCT SCOUT ({target_date or date.today()}) ===")
    result = runner.run_daily(
        force=args.force,
        target_date=target_date,
        max_images_per_product=args.max_images,
        prompt=getattr(args, "prompt", None),
    )

    if result["status"] == "already_completed":
        print(f"\n[THÔNG BÁO] {result['message']}")
        print(f"Thư mục kết quả hiện tại: {result['output_dir']}")
        print("Mẹo: Dùng cờ '--force' nếu bạn muốn quét lại ngày hôm nay.")
        return 0

    if result["status"] in ("unsupported_category", "needs_review"):
        print(f"\n[TỪ CHỐI THỰC THI] {result.get('message')}")
        print(f"Trạng thái: {result['status']}")
        if result.get("prompt"):
            print(f"Câu lệnh input: \"{result['prompt']}\"")
        return 1

    if result["status"] == "failed":
        print(f"\n[LỖI THỰC THI] {result.get('message')} (Mã lỗi: {result.get('failure_code')})")
        return 1

    if result.get("warnings"):
        print(f"\n[CẢNH BÁO VẬN HÀNH] {', '.join(result['warnings'])}")

    print("\n=== HOÀN TẤT PHIÊN QUÉT NGÀY! ===")
    print(f"Trạng thái: {result['status']}")
    print(f"Tổng số sản phẩm chọn: {result['total_products']}")
    print(f"Số sản phẩm tải thành công ảnh: {result['products_with_images']}")
    print(f"File JSON cho Agent: {result['output_json']}")
    print(f"File Báo cáo trực quan: {result['output_report']}")
    return 0


def cmd_classify(args: argparse.Namespace) -> int:
    """Classify input prompt using the Category Dispatcher."""
    from product_scout.category_dispatcher import CategoryDispatcherAgent
    dispatcher = CategoryDispatcherAgent()
    text = args.prompt.strip()
    res = dispatcher.analyze_and_dispatch(text)

    print(f"=== PHÂN LOẠI NGÀNH HÀNG (CATEGORY DISPATCHER) ===")
    print(f"Câu lệnh input:      \"{text}\"")
    print(f"Trạng thái:          {res.status}")
    if res.status in ("unsupported_category", "needs_review"):
        print(f"Thông báo:           {res.message}")
        print(f"Phương pháp:         {res.metadata.classification_method}")
        if res.metadata.fallback_reason:
            print(f"Lý do fallback:      {res.metadata.fallback_reason}")
        return 0

    print(f"Số lượng task chia:  {len(res.tasks)}")
    for t in res.tasks:
        print(f"  - [{t.category_id}] {t.category_name}: {t.target_count} sản phẩm | Khung giá: {t.criteria.price_min:,}đ - {t.criteria.price_max:,}đ")
        print(f"    Sub-prompt: \"{t.sub_prompt}\"")
    print(f"Phương pháp:         {res.metadata.classification_method} (Độ tin cậy: {res.metadata.confidence * 100:.0f}%)")
    if res.metadata.fallback_reason:
        print(f"Lý do fallback:      {res.metadata.fallback_reason}")
    if res.metadata.split_details:
        print(f"Chi tiết split:      {res.metadata.split_details}")
    return 0


def cmd_test_prompt(args: argparse.Namespace) -> int:
    runner = DailyScoutRunner(base_dir=PROJECT_ROOT)
    if getattr(args, "prompt", None):
        text = args.prompt.strip()
        source_desc = f"CLI argument: \"{text}\""
    else:
        req_file = runner.request_file
        if not req_file.exists():
            print(f"File {req_file} không tồn tại. Tạo file mẫu...")
            runner.run_daily(force=True)
        text = req_file.read_text(encoding="utf-8").strip()
        source_desc = req_file.name

    print(f"=== KIỂM THỬ NỘI DUNG PROMPT ({source_desc}) ===")
    print(f"Prompt gốc:\n{text}\n")

    criteria = DailyPromptParser.parse_criteria(text)
    print("--- Tiêu chí bóc tách ECC ---")
    print(f"Số lượng mục tiêu: {criteria.limit}")
    print(f"Ngành hàng:        {criteria.category}")
    print(f"Mức giá:           {criteria.price_min:,} VND -> {criteria.price_max:,} VND")
    print(f"Sàn thương mại:    {', '.join(criteria.platforms)}")

    print("\n--- Đang khám phá sản phẩm phù hợp qua TrendDiscoveryService... ---")
    discovery_res = runner.discovery_service.discover(criteria, run_date=date.today())
    if discovery_res.status == "failed":
        print(f"[LỖI] Khám phá thất bại: {discovery_res.failure_message} ({discovery_res.failure_code})")
        return 1

    candidates = discovery_res.selection.selected
    print(f"Tìm thấy {len(candidates)} sản phẩm được chọn:")
    for idx, sc in enumerate(candidates, start=1):
        c = sc.candidate
        status = discovery_res.selection.statuses.get(c.canonical_key, "new")
        print(f"[{idx:02d}] {c.name} ({c.brand} {c.model}) - Trạng thái: {status}")
        print(f"     Điểm: Final={sc.final_score} (Trend={sc.trend_score:.1f}, Fresh={sc.freshness_score:.1f}, Conf={sc.confidence_score:.1f})")
        print(f"     Lý do: {c.observations[0].snippet[:80] if c.observations else 'N/A'}...")

    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    runner = DailyScoutRunner(base_dir=PROJECT_ROOT)
    target_hour, target_min = map(int, args.time.split(":"))
    print(f"=== BẮT ĐẦU CHẾ ĐỘ DAEMON (Hẹn giờ: {args.time} mỗi ngày) ===")
    print(f"Thư mục làm việc: {PROJECT_ROOT}")
    print("Nhấn Ctrl+C để dừng.")

    # 1. Startup Catch-up Check
    today = date.today()
    now = datetime.now()
    scheduled_today = now.replace(hour=target_hour, minute=target_min, second=0, microsecond=0)

    if now >= scheduled_today:
        if not runner.is_already_completed(today):
            print(f"\n[CATCH-UP] Phát hiện hôm nay ({today}) đã qua giờ hẹn ({args.time}) nhưng chưa chạy. Bắt đầu chạy bù ngay...")
            runner.run_daily(force=False, max_images_per_product=args.max_images)
        else:
            print(f"[STATUS] Hôm nay ({today}) đã có kết quả hoàn tất.")
    else:
        print(f"[STATUS] Sẽ kích hoạt phiên quét vào lúc {args.time} hôm nay.")

    # 2. Main Loop
    last_run_day: date | None = today if runner.is_already_completed(today) else None

    while True:
        try:
            current_now = datetime.now()
            current_day = current_now.date()
            current_target = current_now.replace(hour=target_hour, minute=target_min, second=0, microsecond=0)

            if current_now >= current_target and last_run_day != current_day:
                print(f"\n[TRIGGER] Đạt mốc hẹn giờ {args.time} ngày {current_day}. Bắt đầu quét...")
                runner.run_daily(force=False, max_images_per_product=args.max_images)
                last_run_day = current_day

            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nĐã dừng Daemon.")
            break
        except Exception as exc:
            logger.error("Lỗi trong vòng lặp daemon: %s", exc)
            time.sleep(10)

    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Daily Product Intelligence Scout")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # run command
    p_run = subparsers.add_parser("run", help="Chạy 1 lần phiên quét (phù hợp Task Scheduler hoặc Crontab)")
    p_run.add_argument("--force", action="store_true", help="Bỏ qua kiểm tra đã chạy hôm nay và chạy lại")
    p_run.add_argument("--date", type=str, default=None, help="Chỉ định ngày theo định dạng YYYY-MM-DD")
    p_run.add_argument("--max-images", type=int, choices=range(3, 11), default=5, help="Số ảnh mục tiêu cho mỗi sản phẩm (3-10)")
    p_run.add_argument("--prompt", type=str, default=None, help="Câu lệnh prompt tùy biến ghi đè request.txt")

    # daemon command
    p_daemon = subparsers.add_parser("daemon", help="Chạy tiến trình nền liên tục có tính năng tự động chạy bù")
    p_daemon.add_argument("--time", type=str, default="07:00", help="Giờ hẹn chạy mỗi ngày (mặc định 07:00)")
    p_daemon.add_argument("--interval", type=int, default=30, help="Chu kỳ kiểm tra thời gian (giây)")
    p_daemon.add_argument("--max-images", type=int, choices=range(3, 11), default=5, help="Số ảnh mục tiêu cho mỗi sản phẩm (3-10)")

    # test-prompt command
    p_test_prompt = subparsers.add_parser("test-prompt", help="Thử nghiệm bóc tách request.txt mà không tải ảnh")
    p_test_prompt.add_argument("--prompt", type=str, default=None, help="Câu lệnh prompt tùy biến để thử nghiệm")

    # classify command (ECC)
    p_classify = subparsers.add_parser("classify", help="Phân loại ECC trực tiếp cho một câu lệnh input")
    p_classify.add_argument("--prompt", type=str, required=True, help="Câu lệnh prompt cần phân loại")

    # export-excel command
    p_excel = subparsers.add_parser("export-excel", help="Tổng hợp và cập nhật file Master Excel và CSV từ các ngày crawl")
    p_excel.add_argument("--out-excel", type=str, default=None, help="Đường dẫn file Excel đích")
    p_excel.add_argument("--out-csv", type=str, default=None, help="Đường dẫn file CSV đích")

    args = parser.parse_args(argv)

    from product_scout.workflow_policy import PRODUCT_DISCOVERY_PAUSED, PAUSED_MESSAGE

    if PRODUCT_DISCOVERY_PAUSED and args.command in ("run", "daemon", "test-prompt"):
        print(PAUSED_MESSAGE, file=sys.stderr)
        return 2

    if args.command == "run":
        return cmd_run(args)
    elif args.command == "daemon":
        return cmd_daemon(args)
    elif args.command == "test-prompt":
        return cmd_test_prompt(args)
    elif args.command == "classify":
        return cmd_classify(args)
    elif args.command == "export-excel":
        return cmd_export_excel(args)

    return 0


def cmd_export_excel(args: argparse.Namespace) -> int:
    from product_scout.master_excel import MasterExcelExporter
    excel_path = Path(args.out_excel) if args.out_excel else None
    csv_path = Path(args.out_csv) if args.out_csv else None
    exporter = MasterExcelExporter(runs_dir=PROJECT_ROOT / "data" / "daily_runs", output_dir=PROJECT_ROOT / "data")
    res = exporter.export(excel_path=excel_path, csv_path=csv_path)
    print("✅ Đã xuất Master Excel & CSV thành công!")
    print(f"- File Excel: {res['excel_path']}")
    print(f"- File CSV:   {res['csv_path']}")
    print(f"- Tổng số ngày: {res['total_days']}")
    print(f"- Tổng số lượt ghi nhận sản phẩm: {res['total_products']}")
    print(f"- Sản phẩm độc nhất: {res['unique_products']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
