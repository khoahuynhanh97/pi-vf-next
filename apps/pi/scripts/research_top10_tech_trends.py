"""Research and download at least 2 images for top 10 trending tech products under 1M VND."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Disable imagehash if too slow or leave default
os.environ["PRODUCT_RESEARCH_DISABLE_IMAGEHASH"] = "false"

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media import MediaIntelligenceService, ProductIdentityHint  # noqa: E402
from media.config import MediaConfig  # noqa: E402

PRODUCTS = [
    {
        "name": "Quạt cầm tay mini Jisulife Life 7",
        "brand": "Jisulife",
        "model": "Life 7",
        "category": "Gia dụng & Tiện ích thông minh",
        "price_range": "380.000đ - 520.000đ",
        "keywords": ["Jisulife Life 7 handheld fan", "quat cam tay jisulife life 7 5000mah"],
        "hot_reasons": "Pin siêu khủng 5000mAh dùng 19h, màn hình LED hiển thị % pin và 5 cấp độ gió, thiết kế gập 180 độ bỏ túi cực hot trên TikTok mùa nóng.",
        "key_features": "Pin 5000mAh, sạc Type-C, màn hình kỹ thuật số LED, 5 tốc độ gió mạnh mẽ, gập 180 độ."
    },
    {
        "name": "Tai nghe Bluetooth Baseus Bowie WM02",
        "brand": "Baseus",
        "model": "Bowie WM02",
        "category": "Âm thanh di động",
        "price_range": "260.000đ - 350.000đ",
        "keywords": ["Baseus Bowie WM02 TWS", "tai nghe bluetooth baseus wm02"],
        "hot_reasons": "Thiết kế nắp trong suốt như viên nang không gian, độ trễ siêu thấp 0.06s cho game thủ, pin 25h, kích thước siêu nhỏ nhẹ dạng hạt đậu.",
        "key_features": "Bluetooth 5.3, độ trễ 60ms, pin hộp sạc 25h, app định vị tai nghe Baseus, đèn báo led dạng vòng."
    },
    {
        "name": "Tai nghe chống ồn chủ động QCY T13 ANC",
        "brand": "QCY",
        "model": "T13 ANC",
        "category": "Âm thanh di động",
        "price_range": "390.000đ - 490.000đ",
        "keywords": ["QCY T13 ANC true wireless", "tai nghe qcy t13 anc 28db"],
        "hot_reasons": "Vua tai nghe chống ồn ANC phân khúc dưới 500k, triệt tiêu tiếng ồn 28dB, 4 mic chống ồn gió đàm thoại rõ nét, pin 30h.",
        "key_features": "Chống ồn ANC 28dB, xuyên âm Transparency mode, màng loa 10mm Bio-diaphragm, pin 30h, app tinh chỉnh EQ."
    },
    {
        "name": "Củ sạc nhanh GaN Ugreen Uno Robot 30W / 65W",
        "brand": "Ugreen",
        "model": "Uno Robot GaN",
        "category": "Phụ kiện sạc thông minh",
        "price_range": "320.000đ - 580.000đ",
        "keywords": ["Ugreen Uno robot GaN charger", "cu sac ugreen uno robot 30w 65w"],
        "hot_reasons": "Thiết kế biểu cảm mặt cười robot ngộ nghĩnh thay đổi theo trạng thái sạc, công nghệ GaN nhỏ gọn không nóng máy, cực viral trên TikTok.",
        "key_features": "Công nghệ GaN Fast, sạc nhanh PD 30W/65W cho iPhone/MacBook, màn hình LED mini hiển thị cảm xúc robot, bảo vệ quá nhiệt Thermal Guard."
    },
    {
        "name": "Loa Bluetooth di động Tribit XSound Go",
        "brand": "Tribit",
        "model": "XSound Go",
        "category": "Âm thanh di động",
        "price_range": "590.000đ - 750.000đ",
        "keywords": ["Tribit XSound Go bluetooth speaker", "loa bluetooth tribit xsound go 16w"],
        "hot_reasons": "Chất âm vượt tầm giá với công suất 16W, kháng nước IPX7 ngâm nước thoải mái, pin nghe liên tục 24 tiếng được đánh giá top 1 budget speaker.",
        "key_features": "Công suất 16W Stereo, chống nước IPX7, pin 24 giờ liên tục, Bluetooth 5.0, hỗ trợ cổng AUX 3.5mm."
    },
    {
        "name": "Camera an ninh gia đình Imou Ranger 2 (A22EP / A42P)",
        "brand": "Imou",
        "model": "Ranger 2",
        "category": "Nhà thông minh (Smart Home)",
        "price_range": "420.000đ - 650.000đ",
        "keywords": ["Imou Ranger 2 A22EP camera", "camera imou ranger 2 4mp xoay 360"],
        "hot_reasons": "Camera an ninh gia đình bán chạy số 1, xoay 360 độ, AI phát hiện chuyển động con người và tiếng khóc trẻ em, đàm thoại 2 chiều sắc nét.",
        "key_features": "Độ phân giải 2K/Full HD, xoay 355 độ ngang & 80 độ dọc, AI Human Detection, phát hiện âm thanh bất thường, còi báo động tích hợp."
    },
    {
        "name": "Bàn phím cơ không dây AULA F75",
        "brand": "AULA",
        "model": "F75",
        "category": "Gaming & Văn phòng",
        "price_range": "650.000đ - 890.000đ",
        "keywords": ["AULA F75 mechanical keyboard", "ban phim co aula f75 gasket mount"],
        "hot_reasons": "Bàn phím cơ 'quốc dân' gây bão cộng đồng công nghệ nhờ cấu trúc Gasket Mount êm ái, âm gõ 'thock' đã tai, 3 chế độ kết nối, núm xoay kim loại.",
        "key_features": "Layout 75%, Gasket mount 5 lớp tiêu âm, 3 mode (Type-C / 2.4G / BT5.0), Hotswap 5 pin, LED RGB, pin 4000mAh."
    },
    {
        "name": "Đèn bàn thông minh Xiaomi Mijia Smart Desk Lamp Lite",
        "brand": "Xiaomi",
        "model": "Mijia Desk Lamp Lite",
        "category": "Nhà thông minh (Smart Home)",
        "price_range": "280.000đ - 380.000đ",
        "keywords": ["Xiaomi Mijia Smart Desk Lamp Lite", "den ban xiaomi mijia lite"],
        "hot_reasons": "Đèn học/làm việc chuẩn bảo vệ mắt không ánh sáng xanh gây hại, độ sáng chuẩn A quốc gia, thiết kế tối giản thanh lịch phong cách Bắc Âu.",
        "key_features": "Độ sáng 1200 lux chuẩn A, không nhấp nháy (RG0 không ánh sáng xanh), 3 mức sáng cảm ứng chạm, khớp xoay 218 độ linh hoạt."
    },
    {
        "name": "Sạc dự phòng không dây từ tính Baseus Magnetic Bracket 10000mAh 20W",
        "brand": "Baseus",
        "model": "Magnetic Bracket 10000mAh",
        "category": "Phụ kiện sạc thông minh",
        "price_range": "450.000đ - 590.000đ",
        "keywords": ["Baseus Magnetic Bracket Power Bank 10000mAh", "pin du phong baseus magnetic 20w co gia do"],
        "hot_reasons": "Hít từ tính MagSafe siêu chắc cho iPhone 12-16, tích hợp chân đế gập xem phim rảnh tay, sạc nhanh dây 20W và không dây 15W.",
        "key_features": "Dung lượng 10000mAh, sạc không dây 15W MagSafe, sạc có dây Type-C PD 20W, chân đế kickstand gập gọn, màn hình LED báo pin."
    },
    {
        "name": "Chuột không dây Silent DareU LM115B Dual Mode",
        "brand": "DareU",
        "model": "LM115B",
        "category": "Gaming & Văn phòng",
        "price_range": "190.000đ - 260.000đ",
        "keywords": ["DareU LM115B silent mouse", "chuot khong day dareu lm115b bluetooth"],
        "hot_reasons": "Chuột văn phòng không tiếng ồn (Silent Click) cực rẻ, hỗ trợ kết nối kép Bluetooth + USB 2.4Ghz mượt mà cho cả laptop và iPad.",
        "key_features": "Dual mode (Bluetooth 5.0 + Wireless 2.4GHz), nút bấm Silent click êm tuyệt đối, DPI tùy chỉnh 800-1600, thiết kế công thái học nhẹ 70g."
    }
]


def run():
    config = MediaConfig(max_search_results=8, target_selected_assets=3, minimum_short_edge=400)
    service = MediaIntelligenceService(config)
    
    results = []
    print("=== BẮT ĐẦU NGHIÊN CỨU & TẢI ẢNH CHO 10 SẢN PHẨM HOT TREND CÔNG NGHỆ < 1 TRIỆU VNĐ ===")
    
    for idx, p in enumerate(PRODUCTS, start=1):
        print(f"\n[{idx}/10] Đang xử lý: {p['name']} ({p['brand']} {p['model']})...")
        hint = ProductIdentityHint(
            name=p["name"],
            brand=p["brand"],
            model=p["model"],
            keywords=p["keywords"]
        )
        try:
            res = service.research(hint, target_assets=3, minimum_short_edge=400)
            assets = [
                {
                    "asset_id": a.asset_id,
                    "local_path": str(Path(a.local_path).resolve()),
                    "url": a.source.get("image_url"),
                    "host": a.source.get("host"),
                    "view": a.assessment.view.value if hasattr(a.assessment.view, "value") else str(a.assessment.view),
                    "match_status": a.assessment.match_status.value if hasattr(a.assessment.match_status, "value") else str(a.assessment.match_status),
                    "score": a.assessment.product_match_score,
                    "width": a.width,
                    "height": a.height,
                    "format": a.format,
                }
                for a in res.selected_assets
            ]
            
            status_str = res.status.value
            downloaded_count = len(assets)
            print(f"  -> Trạng thái: {status_str} | Số ảnh đạt chuẩn: {downloaded_count}")
            for a_idx, a in enumerate(assets, 1):
                print(f"     Ảnh {a_idx}: {a['local_path']} (View: {a['view']}, Score: {a['score']})")
                
            results.append({
                "info": p,
                "status": status_str,
                "asset_count": downloaded_count,
                "assets": assets,
                "is_success": downloaded_count >= 2
            })
        except Exception as e:
            print(f"  -> Lỗi: {e}")
            results.append({
                "info": p,
                "status": "error",
                "asset_count": 0,
                "assets": [],
                "error": str(e),
                "is_success": False
            })
            
    output_path = Path("data/hot_tech_10_products.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n=== HOÀN TẤT! Đã lưu kết quả chi tiết vào {output_path} ===")
    
    success_count = sum(1 for r in results if r["is_success"])
    print(f"Tổng số sản phẩm đạt yêu cầu (>= 2 ảnh): {success_count}/10")


if __name__ == "__main__":
    from product_scout.workflow_policy import require_product_discovery

    require_product_discovery()
    run()
