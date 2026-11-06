"""Daily request prompt parser and trend candidate discovery engine.

Parses natural language requests (e.g. from request.txt) and synthesizes
product candidates using LLM (OpenAI / 9Router) or resilient Heuristic & Web Search fallback.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


@dataclass
class ParsedCriteria:
    raw_prompt: str
    limit: int = 10
    category: str = "công nghệ"
    price_min: int = 200_000
    price_max: int = 1_000_000
    currency: str = "VND"
    platforms: list[str] = field(default_factory=lambda: ["tiktok", "shopee"])
    extra_requirements: str = ""


@dataclass
class DiscoveredProduct:
    name: str
    brand: str
    model: str
    category: str
    price_display: str
    price_min: int
    price_max: int
    currency: str = "VND"
    keywords: list[str] = field(default_factory=list)
    hot_reason: str = ""
    key_features: str = ""


# High-confidence catalog of verified trending products in Vietnam under 1M VND
CURATED_TREND_CATALOG: list[dict[str, Any]] = [
    {
        "name": "Quạt cầm tay mini Jisulife Life 7",
        "brand": "Jisulife",
        "model": "Life 7",
        "category": "Gia dụng & Tiện ích thông minh",
        "price_display": "380.000đ - 520.000đ",
        "price_min": 380000,
        "price_max": 520000,
        "keywords": ["Jisulife Life 7 handheld fan", "quat cam tay jisulife life 7 5000mah"],
        "hot_reasons": "Pin 5000mAh dùng 19h, màn hình LED hiển thị % pin và 5 cấp độ gió, thiết kế gập 180 độ bỏ túi cực hot trên TikTok mùa nóng.",
        "key_features": "Pin 5000mAh, sạc Type-C, màn hình kỹ thuật số LED, 5 tốc độ gió, gập 180 độ."
    },
    {
        "name": "Tai nghe Bluetooth Baseus Bowie WM02",
        "brand": "Baseus",
        "model": "Bowie WM02",
        "category": "Âm thanh di động",
        "price_display": "260.000đ - 350.000đ",
        "price_min": 260000,
        "price_max": 350000,
        "keywords": ["Baseus Bowie WM02 TWS", "tai nghe bluetooth baseus wm02"],
        "hot_reasons": "Thiết kế nắp trong suốt như viên nang không gian, độ trễ siêu thấp 0.06s cho game thủ, pin 25h, kích thước siêu nhỏ nhẹ.",
        "key_features": "Bluetooth 5.3, độ trễ 60ms, pin hộp sạc 25h, app định vị tai nghe Baseus, đèn báo led dạng vòng."
    },
    {
        "name": "Tai nghe chống ồn chủ động QCY T13 ANC",
        "brand": "QCY",
        "model": "T13 ANC",
        "category": "Âm thanh di động",
        "price_display": "390.000đ - 490.000đ",
        "price_min": 390000,
        "price_max": 490000,
        "keywords": ["QCY T13 ANC true wireless", "tai nghe qcy t13 anc 28db"],
        "hot_reasons": "Vua tai nghe chống ồn ANC phân khúc dưới 500k, triệt tiêu tiếng ồn 28dB, 4 mic chống ồn gió đàm thoại rõ nét, pin 30h.",
        "key_features": "Chống ồn ANC 28dB, xuyên âm Transparency mode, màng loa 10mm Bio-diaphragm, pin 30h, app tinh chỉnh EQ."
    },
    {
        "name": "Củ sạc nhanh GaN Ugreen Uno Robot 30W / 65W",
        "brand": "Ugreen",
        "model": "Uno Robot GaN",
        "category": "Phụ kiện sạc thông minh",
        "price_display": "320.000đ - 580.000đ",
        "price_min": 320000,
        "price_max": 580000,
        "keywords": ["Ugreen Uno robot GaN charger", "cu sac ugreen uno robot 30w 65w"],
        "hot_reasons": "Thiết kế biểu cảm mặt cười robot ngộ nghĩnh thay đổi theo trạng thái sạc, công nghệ GaN nhỏ gọn không nóng máy, cực viral trên TikTok.",
        "key_features": "Công nghệ GaN Fast, sạc nhanh PD 30W/65W cho iPhone/MacBook, màn hình LED mini hiển thị cảm xúc robot."
    },
    {
        "name": "Loa Bluetooth di động Tribit XSound Go",
        "brand": "Tribit",
        "model": "XSound Go",
        "category": "Âm thanh di động",
        "price_display": "590.000đ - 750.000đ",
        "price_min": 590000,
        "price_max": 750000,
        "keywords": ["Tribit XSound Go bluetooth speaker", "loa bluetooth tribit xsound go 16w"],
        "hot_reasons": "Chất âm vượt tầm giá với công suất 16W, kháng nước IPX7 ngâm nước thoải mái, pin nghe liên tục 24 tiếng.",
        "key_features": "Công suất 16W Stereo, chống nước IPX7, pin 24 giờ liên tục, Bluetooth 5.0, hỗ trợ cổng AUX 3.5mm."
    },
    {
        "name": "Camera an ninh gia đình Imou Ranger 2",
        "brand": "Imou",
        "model": "Ranger 2",
        "category": "Nhà thông minh (Smart Home)",
        "price_display": "420.000đ - 650.000đ",
        "price_min": 420000,
        "price_max": 650000,
        "keywords": ["Imou Ranger 2 A22EP camera", "camera imou ranger 2 4mp xoay 360"],
        "hot_reasons": "Camera an ninh gia đình bán chạy số 1, xoay 360 độ, AI phát hiện chuyển động con người và tiếng khóc trẻ em.",
        "key_features": "Độ phân giải 2K/Full HD, xoay 355 độ ngang & 80 độ dọc, AI Human Detection, phát hiện âm thanh bất thường."
    },
    {
        "name": "Bàn phím cơ không dây AULA F75",
        "brand": "AULA",
        "model": "F75",
        "category": "Gaming & Văn phòng",
        "price_display": "650.000đ - 890.000đ",
        "price_min": 650000,
        "price_max": 890000,
        "keywords": ["AULA F75 mechanical keyboard", "ban phim co aula f75 gasket mount"],
        "hot_reasons": "Bàn phím cơ 'quốc dân' gây bão cộng đồng công nghệ nhờ cấu trúc Gasket Mount êm ái, âm gõ 'thock' đã tai, 3 chế độ kết nối.",
        "key_features": "Layout 75%, Gasket mount 5 lớp tiêu âm, 3 mode (Type-C / 2.4G / BT5.0), Hotswap 5 pin, LED RGB, pin 4000mAh."
    },
    {
        "name": "Đèn bàn thông minh Xiaomi Mijia Desk Lamp Lite",
        "brand": "Xiaomi",
        "model": "Mijia Desk Lamp Lite",
        "category": "Nhà thông minh (Smart Home)",
        "price_display": "280.000đ - 380.000đ",
        "price_min": 280000,
        "price_max": 380000,
        "keywords": ["Xiaomi Mijia Smart Desk Lamp Lite", "den ban xiaomi mijia lite"],
        "hot_reasons": "Đèn học/làm việc chuẩn bảo vệ mắt không ánh sáng xanh gây hại, độ sáng chuẩn A quốc gia, thiết kế tối giản thanh lịch.",
        "key_features": "Độ sáng 1200 lux chuẩn A, không nhấp nháy (RG0 không ánh sáng xanh), 3 mức sáng cảm ứng chạm, khớp xoay 218 độ."
    },
    {
        "name": "Sạc dự phòng từ tính Baseus Magnetic Bracket 10000mAh 20W",
        "brand": "Baseus",
        "model": "Magnetic Bracket 10000mAh",
        "category": "Phụ kiện sạc thông minh",
        "price_display": "450.000đ - 590.000đ",
        "price_min": 450000,
        "price_max": 590000,
        "keywords": ["Baseus Magnetic Bracket Power Bank 10000mAh", "pin du phong baseus magnetic 20w"],
        "hot_reasons": "Hít từ tính MagSafe siêu chắc cho iPhone, tích hợp chân đế gập xem phim rảnh tay, sạc nhanh dây 20W và không dây 15W.",
        "key_features": "Dung lượng 10000mAh, sạc không dây 15W MagSafe, sạc có dây Type-C PD 20W, chân đế kickstand gập gọn."
    },
    {
        "name": "Chuột không dây Silent DareU LM115B Dual Mode",
        "brand": "DareU",
        "model": "LM115B",
        "category": "Gaming & Văn phòng",
        "price_display": "190.000đ - 260.000đ",
        "price_min": 190000,
        "price_max": 260000,
        "keywords": ["DareU LM115B silent mouse", "chuot khong day dareu lm115b bluetooth"],
        "hot_reasons": "Chuột văn phòng không tiếng ồn (Silent Click) cực rẻ, hỗ trợ kết nối kép Bluetooth + USB 2.4Ghz mượt mà cho laptop và iPad.",
        "key_features": "Dual mode (Bluetooth 5.0 + Wireless 2.4GHz), nút bấm Silent click êm tuyệt đối, DPI tùy chỉnh 800-1600."
    },
    {
        "name": "Giá đỡ điện thoại kiêm sạc không dây Baseus City Lights 15W",
        "brand": "Baseus",
        "model": "City Lights",
        "category": "Phụ kiện xe & Bàn làm việc",
        "price_display": "350.000đ - 480.000đ",
        "price_min": 350000,
        "price_max": 480000,
        "keywords": ["Baseus City Lights wireless car charger", "gia do dien thoai baseus city lights"],
        "hot_reasons": "Cảm ứng tự động kẹp nhả điện thoại cực ngầu, đèn LED đổi màu viền, sạc nhanh không dây 15W.",
        "key_features": "Cảm ứng tự kẹp cơ học, sạc nhanh 15W, đèn LED viền phong cách công nghệ, chống rung lắc."
    },
    {
        "name": "Máy cạo râu mini du lịch Enchen Mini 6",
        "brand": "Enchen",
        "model": "Mini 6",
        "category": "Gia dụng & Tiện ích cá nhân",
        "price_display": "180.000đ - 250.000đ",
        "price_min": 180000,
        "price_max": 250000,
        "keywords": ["Enchen Mini 6 electric shaver", "may cao rau mini enchen mini 6"],
        "hot_reasons": "Kích thước siêu nhỏ chỉ bằng củ sạc, chống nước IPX7 cạo ướt cạo khô, lưỡi dao tự mài bén ngọt, viral triệu view TikTok.",
        "key_features": "Kháng nước IPX7, cổng sạc Type-C, pin 60 ngày, lưỡi dao hợp kim tự mài, kích thước quả trứng."
    },
    {
        "name": "Đồng hồ thông minh theo dõi sức khỏe Haylou Solar Lite",
        "brand": "Haylou",
        "model": "Solar Lite",
        "category": "Thiết bị đeo thông minh",
        "price_display": "420.000đ - 550.000đ",
        "price_min": 420000,
        "price_max": 550000,
        "keywords": ["Haylou Solar Lite smartwatch", "dong ho thong minh haylou solar lite"],
        "hot_reasons": "Smartwatch màn hình lớn 1.38 inch giá siêu hời, đo SpO2, nhịp tim 24/7, pin 20 ngày, kháng nước IP68.",
        "key_features": "Màn hình 1.38 inch HD, đo nhịp tim & oxy máu SpO2, 100 chế độ thể thao, pin trâu 20 ngày, chống nước IP68."
    }
]


def is_food_category(category: str) -> bool:
    """Check if category string relates to packaged food, snacks, or beverages."""
    cat = (category or "").lower()
    return any(
        term in cat
        for term in (
            "thực phẩm", "thuc pham", "đồ ăn", "do an", "đồ uống", "do uong",
            "ăn vặt", "an vat", "snack", "bánh kẹo", "banh keo", "nước giải khát",
            "nuoc giai khat", "nước ngọt", "nuoc ngot", "trà", "tra", "cà phê",
            "ca phe", "đóng chai", "dong chai", "đóng gói", "dong goi", "bánh", "kẹo",
        )
    )


class DailyPromptParser:
    """Parses prompt from text file and resolves product list."""

    @staticmethod
    def parse_criteria(prompt_text: str) -> ParsedCriteria:
        """Extract structured criteria from natural language prompt."""
        text = prompt_text.strip()
        lower_text = text.lower()
        import unicodedata
        folded_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").casefold()

        from product_scout.category_dispatcher import (
            extract_price_and_spans,
            extract_non_count_spans,
            extract_counts,
            FOOD_KEYWORDS,
            TECH_KEYWORDS,
        )

        price_min, price_max, has_custom_price, price_spans = extract_price_and_spans(text)
        non_count_spans = extract_non_count_spans(text, price_spans)
        total_count, per_cat = extract_counts(text, non_count_spans)

        limit = total_count or (list(per_cat.values())[0] if per_cat else 10)
        limit = max(1, min(limit, 50))

        # Check category
        def match_vocab(vocab: frozenset[str]) -> int:
            score = 0
            for kw in vocab:
                pat = rf"\b{re.escape(kw)}\b"
                if re.search(pat, text, re.IGNORECASE) or re.search(pat, folded_text, re.IGNORECASE):
                    score += 2 if len(kw.split()) > 1 else 1
            return score

        food_score = match_vocab(FOOD_KEYWORDS)
        tech_score = match_vocab(TECH_KEYWORDS)
        if any(term in lower_text for term in ("ăn vặt", "an vat", "bán chay", "ban chay", "đồ ăn", "bánh kẹo", "snack")):
            food_score = max(food_score, 3)

        if "đồ uống" in lower_text or "đóng chai" in lower_text:
            category = "đồ uống đóng chai"
        elif "gia dụng" in lower_text:
            category = "gia dụng thông minh"
        elif food_score >= tech_score and food_score > 0:
            category = "đồ ăn vặt"
        elif tech_score > 0:
            category = "sản phẩm công nghệ"
        elif is_food_category(lower_text):
            category = "đồ ăn vặt"
        else:
            category = "sản phẩm công nghệ"

        # Apply price defaults if user didn't specify
        if not has_custom_price:
            if is_food_category(category):
                price_min = 0
                price_max = 0
            else:
                price_min = 200_000
                price_max = 1_000_000

        platforms: list[str] = []
        if "tiktok" in lower_text:
            platforms.append("tiktok")
        if "shopee" in lower_text:
            platforms.append("shopee")
        if "lazada" in lower_text:
            platforms.append("lazada")
        if not platforms:
            platforms = ["tiktok", "shopee"]

        return ParsedCriteria(
            raw_prompt=text,
            limit=limit,
            category=category,
            price_min=price_min,
            price_max=price_max,
            platforms=platforms,
            extra_requirements=text,
        )

    @classmethod
    def discover_candidates(cls, criteria: ParsedCriteria) -> list[DiscoveredProduct]:
        """Legacy emergency discovery from static catalog. Disabled by default in production."""
        from product_scout.workflow_policy import require_product_discovery

        require_product_discovery()
        if os.environ.get("PRODUCT_SCOUT_ENABLE_CURATED_EMERGENCY") != "true":
            raise RuntimeError("CURATED_DISCOVERY_DISABLED")

        # 1. Try LLM extraction
        llm_products = cls._try_llm_discovery(criteria)
        if llm_products:
            logger.info("Successfully discovered %d products via LLM", len(llm_products))
            return llm_products[:criteria.limit]

        # 2. Fallback to Heuristic & Curated + Search engine
        logger.info("Using Heuristic & Web Search fallback for discovery")
        return cls._heuristic_discovery(criteria)

    @classmethod
    def _try_llm_discovery(cls, criteria: ParsedCriteria) -> list[DiscoveredProduct] | None:
        """Call LLM (OpenAI or 9Router) to get structured product list."""
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("NINEROUTER_API_KEY")
        base_url = os.environ.get("NINEROUTER_BASE_URL")
        model = os.environ.get("NINEROUTER_MODEL") or "gpt-4o-mini"

        if not api_key:
            return None

        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=base_url if base_url else None)

            system_prompt = (
                "Bạn là một chuyên gia nghiên cứu thị trường E-Commerce (TikTok Shop & Shopee Việt Nam). "
                "Nhiệm vụ của bạn là dựa theo yêu cầu của người dùng, đề xuất chính xác các sản phẩm cụ thể "
                "(tên thương hiệu, model rõ ràng, đang bán chạy/hot trend thực tế tại Việt Nam). "
                "Trả về định dạng JSON thuần túy theo schema yêu cầu."
            )

            user_prompt = (
                f"Yêu cầu: {criteria.raw_prompt}\n"
                f"- Số lượng: đúng {criteria.limit} sản phẩm.\n"
                f"- Khoảng giá: từ {criteria.price_min:,} VND đến {criteria.price_max:,} VND.\n"
                f"- Ngành hàng / Chủ đề: {criteria.category}\n\n"
                "Schema JSON yêu cầu:\n"
                "{\n"
                '  "products": [\n'
                "    {\n"
                '      "name": "Tên sản phẩm đầy đủ kèm model",\n'
                '      "brand": "Thương hiệu",\n'
                '      "model": "Tên model",\n'
                '      "category": "Phân loại cụ thể",\n'
                '      "price_display": "ví dụ: 350.000đ - 450.000đ",\n'
                '      "price_min": 350000,\n'
                '      "price_max": 450000,\n'
                '      "keywords": ["từ khóa tìm ảnh 1", "từ khóa tìm ảnh 2"],\n'
                '      "hot_reason": "Lý do vì sao sản phẩm này đang viral / hot trend trên TikTok hoặc Shopee",\n'
                '      "key_features": "3-5 tính năng/thông số kỹ thuật nổi bật nhất"\n'
                "    }\n"
                "  ]\n"
                "}"
            )

            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                response_format={"type": "json_object"}
            )
            content = response.choices[0].message.content or "{}"
            data = json.loads(content)
            raw_list = data.get("products", [])
            results = []
            for item in raw_list:
                results.append(DiscoveredProduct(
                    name=item["name"],
                    brand=item.get("brand", ""),
                    model=item.get("model", ""),
                    category=item.get("category", criteria.category),
                    price_display=item.get("price_display", f"{criteria.price_min:,}đ - {criteria.price_max:,}đ"),
                    price_min=int(item.get("price_min", criteria.price_min)),
                    price_max=int(item.get("price_max", criteria.price_max)),
                    keywords=item.get("keywords", [item["name"]]),
                    hot_reason=item.get("hot_reason", ""),
                    key_features=item.get("key_features", "")
                ))
            if results:
                return results
        except Exception as exc:
            logger.warning("LLM discovery failed: %s", exc)
            return None
        return None

    @classmethod
    def _heuristic_discovery(cls, criteria: ParsedCriteria) -> list[DiscoveredProduct]:
        """Heuristic selection matching price boundaries and category from curated pool."""
        matched: list[DiscoveredProduct] = []

        for item in CURATED_TREND_CATALOG:
            # Check price intersection
            item_min = item.get("price_min", 0)
            item_max = item.get("price_max", 10_000_000)
            # Allow items overlapping with requested range
            if item_min <= criteria.price_max and item_max >= criteria.price_min:
                matched.append(DiscoveredProduct(
                    name=item["name"],
                    brand=item["brand"],
                    model=item["model"],
                    category=item["category"],
                    price_display=item["price_display"],
                    price_min=item["price_min"],
                    price_max=item["price_max"],
                    keywords=item["keywords"],
                    hot_reason=item["hot_reasons"],
                    key_features=item["key_features"]
                ))

        # If not enough items due to strict range, backfill with closest candidates
        if len(matched) < criteria.limit:
            for item in CURATED_TREND_CATALOG:
                if any(m.name == item["name"] for m in matched):
                    continue
                matched.append(DiscoveredProduct(
                    name=item["name"],
                    brand=item["brand"],
                    model=item["model"],
                    category=item["category"],
                    price_display=item["price_display"],
                    price_min=item["price_min"],
                    price_max=item["price_max"],
                    keywords=item["keywords"],
                    hot_reason=item["hot_reasons"],
                    key_features=item["key_features"]
                ))
                if len(matched) >= criteria.limit:
                    break

        return matched[:criteria.limit]
