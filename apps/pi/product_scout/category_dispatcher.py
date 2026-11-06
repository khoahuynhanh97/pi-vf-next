"""Two-Category Dispatcher Agent.

Explicitly supports exactly two categories:
1. 'food_snack': Packaged food, snacks, candy, and packaged beverages with SKU identity.
2. 'tech': Technology, consumer electronics, smart gadgets, and accessories.

Provides robust, deterministic count/price extraction, heuristic classification with
optional LLM disambiguation, category task decomposition, and explicit error states
('unsupported_category', 'needs_review') for unsupported prompts.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from product_scout.daily_parser import ParsedCriteria, is_food_category

logger = logging.getLogger(__name__)

SUPPORTED_CATEGORIES = ("food_snack", "tech")

FOOD_KEYWORDS = frozenset({
    "snack", "an vat", "ăn vặt", "bánh tráng", "banh trang", "bắp rang", "bap rang",
    "bỏng ngô", "bong ngo", "khoai chiên", "khoai chien", "nui sấy", "nui say",
    "đồ ăn", "do an", "đồ uống", "do uong", "thực phẩm", "thuc pham",
    "bánh kẹo", "banh keo", "nước ngọt", "nuoc ngot", "trà sữa", "tra sua",
    "khoai tây", "khoai tay", "khô gà", "kho ga", "khô bò", "kho bo",
    "bánh quy", "banh quy", "kẹo dẻo", "keo deo", "xúc xích", "xuc xich",
    "rong biển", "rong bien", "đậu phộng", "dau phong", "hạt điều", "hat dieu",
    "bim bim", "bimbim", "bánh bông lan", "chocopie", "nước tăng lực",
    "nước ép", "nuoc ep", "đóng chai", "dong chai", "đóng gói", "dong goi",
    "bánh", "banh", "kẹo", "keo", "nước giải khát", "nuoc giai khat", "nước khoáng", "nuoc khoang", "nước suối", "nuoc suoi", "nước yến", "nuoc yen",
    "coca", "coca-cola", "pepsi", "trà", "tra", "cà phê", "ca phe",
})

TECH_KEYWORDS = frozenset({
    "công nghệ", "cong nghe", "tech", "tai nghe", "bàn phím", "ban phim",
    "chuột", "chuot", "củ sạc", "cu sac", "cáp sạc", "cap sac", "sạc", "sac",
    "pin dự phòng", "pin du phong", "smartwatch", "đồng hồ thông minh", "dong ho",
    "quạt mini", "quat mini", "camera", "loa", "đèn", "den", "hub", "ssd",
    "gadget", "bàn chải điện", "tam nuoc", "phụ kiện", "điện tử", "dien tu",
    "thiet bi thong minh", "thiết bị thông minh", "tai nghe bluetooth", "smart home",
})

UNSUPPORTED_KEYWORDS = frozenset({
    "thời trang", "thoi trang", "quần áo", "quan ao", "áo thun", "ao thun", "áo sơ mi",
    "giày dép", "giay dep", "túi xách", "tui xach", "váy", "vay",
    "mỹ phẩm", "my pham", "skincare", "làm đẹp", "lam dep", "son môi", "son moi",
    "nước hoa", "nuoc hoa", "nội thất", "noi that", "bàn ghế", "ban ghe",
    "sách", "sach", "truyện", "truyen",
    "xe máy", "xe may", "ô tô", "o to", "xe đạp", "xe dap",
    "bất động sản", "bat dong san", "nhà đất", "nha dat",
    "thuốc", "thuoc", "dược phẩm", "duoc pham",
})


def parse_price_token(token: str) -> int | None:
    """Parse a natural price token into integer VND."""
    t = token.lower().strip()

    # 1. Compound expression: e.g. "2 triệu 500k", "2 tr 500", "2tr500", "2tr5", "2 triệu 500"
    m_compound = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:triệu|trieu|tr)\s*(\d+)\s*(?:k|nghìn|ngàn)?", t)
    if m_compound:
        tr_val = int(float(m_compound.group(1).replace(",", ".")) * 1_000_000)
        rem_str = m_compound.group(2)
        rem_val = int(rem_str)
        if len(rem_str) == 1:
            rem_val *= 100_000  # e.g. 2tr5 -> 2.500.000
        elif len(rem_str) == 2:
            rem_val *= 10_000   # e.g. 2tr50 -> 2.500.000
        elif rem_val < 1000:
            rem_val *= 1_000    # e.g. 2tr 500 -> 2.500.000
        return tr_val + rem_val

    # 2. Decimal or integer "tr" / "triệu" / "million": e.g. "1.5tr", "1,5 triệu", "2tr", "2 triệu"
    m_tr = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:triệu|trieu|tr|million)\b", t)
    if m_tr:
        try:
            return int(float(m_tr.group(1).replace(",", ".")) * 1_000_000)
        except ValueError:
            pass

    # 3. Decimal or integer "k": e.g. "500k", "1.5k"
    m_k = re.search(r"(\d+(?:[.,]\d+)?)\s*k\b", t)
    if m_k:
        try:
            return int(float(m_k.group(1).replace(",", ".")) * 1_000)
        except ValueError:
            pass

    # 4. Standard thousands separated numbers with currency: e.g. "500.000đ", "1.000.000 vnd", "500,000"
    m_sep = re.search(r"(\d{1,3}(?:[.,]\d{3})+)\s*(?:vnd|đ|dong|đồng)?", t)
    if m_sep:
        clean_num = re.sub(r"[.,]", "", m_sep.group(1))
        try:
            v = int(clean_num)
            if 10_000 <= v <= 100_000_000:
                return v
        except ValueError:
            pass

    # 5. Number followed by currency symbol: e.g. "500000 vnd" or "500000đ"
    m_num = re.search(r"(\d+)\s*(?:vnd|đ|dong|đồng)", t)
    if m_num:
        try:
            v = int(m_num.group(1))
            if 10 <= v <= 999:
                v *= 1000
            return v
        except ValueError:
            pass

    # 6. Plain digits (strip thousands separators if followed by 3 digits)
    clean_t = re.sub(r"[.,](?=\d{3}(?:\D|$))", "", t)
    digits = re.sub(r"[^\d]", "", clean_t)
    if digits:
        try:
            v = int(digits)
            if 10 <= v <= 999:
                v *= 1000
            return v
        except ValueError:
            pass

    return None


def extract_price_and_spans(text: str) -> tuple[int, int, bool, list[tuple[int, int]]]:
    """Extract price range and character spans of price tokens to prevent count confusion."""
    price_spans: list[tuple[int, int]] = []
    price_min = 0
    price_max = 0
    has_custom_price = False

    # 1. Price range: "200k-500k", "từ 20k đến 50k", "1.5tr-2tr", "200.000 - 1.000.000đ"
    range_regex = re.compile(
        r"(?:giá\s*từ|từ\s*)?(\d+[\d.,\s]*(?:triệu|trieu|tr|k|vnd|đ|million)?)\s*(?:-|đến|to)\s*(\d+[\d.,\s]*(?:triệu|trieu|tr|k|vnd|đ|million)?)",
        re.IGNORECASE,
    )
    for m in range_regex.finditer(text):
        p1 = parse_price_token(m.group(1))
        p2 = parse_price_token(m.group(2))
        if p1 is not None or p2 is not None:
            has_custom_price = True
            price_min = p1 if p1 is not None else 0
            price_max = p2 if p2 is not None else 0
            price_spans.append((m.start(), m.end()))

    # 2. Threshold upper bound: "dưới 2 triệu 500k", "dưới 2 triệu", "dưới 50k", "< 1tr", "tối đa 500k", "under 2 million"
    under_regex = re.compile(
        r"(?:dưới|nhỏ hơn|tối đa|<|under|below)\s*(\d+[\d.,\s]*(?:triệu|trieu|tr|k|vnd|đ|million)?(?:\s*\d+\s*(?:k|nghìn|ngàn)?)?)",
        re.IGNORECASE,
    )
    for m in under_regex.finditer(text):
        p = parse_price_token(m.group(1))
        if p is not None:
            if not has_custom_price:
                has_custom_price = True
                price_min = 0
                price_max = p
            price_spans.append((m.start(), m.end()))

    # 3. Threshold lower bound: "từ 500k", "tối thiểu 500k", "trên 1tr", "> 500k" (when not already matched as range)
    min_only_regex = re.compile(
        r"(?:từ|lớn hơn|tối thiểu|trên|>|from|above)\s*(\d+[\d.,\s]*(?:triệu|trieu|tr|k|vnd|đ|million)?(?:\s*\d+\s*(?:k|nghìn|ngàn)?)?)",
        re.IGNORECASE,
    )
    for m in min_only_regex.finditer(text):
        if any(s <= m.start() and m.end() <= e for s, e in price_spans):
            continue
        p = parse_price_token(m.group(1))
        if p is not None:
            if not has_custom_price:
                has_custom_price = True
                price_min = p
                price_max = 0  # unconstrained maximum (do not set arbitrary ceiling)
            price_spans.append((m.start(), m.end()))

    # 4. Single target price: "giá 500k", "khoảng 500k", "tầm 500k", "ngân sách 500k"
    single_target_regex = re.compile(
        r"(?:giá|khoảng|tầm|tam|ngân sách|ngan sach)\s*(\d+[\d.,\s]*(?:triệu|trieu|tr|k|vnd|đ|million)?(?:\s*\d+\s*(?:k|nghìn|ngàn)?)?)",
        re.IGNORECASE,
    )
    for m in single_target_regex.finditer(text):
        if any(s <= m.start() and m.end() <= e for s, e in price_spans):
            continue
        p = parse_price_token(m.group(1))
        if p is not None:
            if not has_custom_price:
                has_custom_price = True
                price_min = 0
                price_max = p  # convention: budget ceiling
            price_spans.append((m.start(), m.end()))

    # 5. Currency-tagged tokens for masking: e.g. "50k", "2 triệu", "500.000đ", "200k"
    curr_regex = re.compile(
        r"\b\d+[\d.,]*(?:\s*(?:triệu|trieu|tr|k|vnd|đ|million))\b",
        re.IGNORECASE,
    )
    for m in curr_regex.finditer(text):
        span = (m.start(), m.end())
        if not any(s <= span[0] and span[1] <= e for s, e in price_spans):
            price_spans.append(span)

    return price_min, price_max, has_custom_price, price_spans


def extract_non_count_spans(text: str, price_spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Collect spans of all numeric tokens that represent prices, weights, years, etc."""
    spans = list(price_spans)

    # Unit tokens: weight/volume (100g, 350ml, 1.5L), tech units (65w, 10000mah)
    unit_regex = re.compile(
        r"\b\d+(?:[.,]\d+)?\s*(?:g|gr|gram|kg|ml|l|lít|lit|oz|w|watt|mah|wh|inch|hz|fps|gb|tb|mp)\b",
        re.IGNORECASE,
    )
    for m in unit_regex.finditer(text):
        spans.append((m.start(), m.end()))

    # Calendar year tokens: 2020-2029
    year_regex = re.compile(r"\b202[0-9]\b")
    for m in year_regex.finditer(text):
        spans.append((m.start(), m.end()))

    # Star rating tokens: 4 sao, 5 star
    rating_regex = re.compile(r"\b\d+\s*(?:sao|star)\b", re.IGNORECASE)
    for m in rating_regex.finditer(text):
        spans.append((m.start(), m.end()))

    return spans


def is_inside_spans(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    """Return True if [start, end] overlaps with any recorded span."""
    return any(max(start, s) < min(end, e) for s, e in spans)


def extract_counts(text: str, non_count_spans: list[tuple[int, int]]) -> tuple[int | None, dict[str, int]]:
    """Extract product count and per-category counts without mistaking price/unit numbers."""
    per_cat_counts: dict[str, int] = {}
    total_count: int | None = None

    # 1. Per-category patterns
    # Food per-category: "20 snack", "10 đồ ăn vặt", "20 snacks"
    food_count_regex = re.compile(
        r"\b(\d+)\s*(?:loại\s+|món\s+)?(?:gói|hộp|túi|lon|chai\s+)?(?:snack|snacks|đồ ăn|do an|bánh|kẹo|thực phẩm|bánh tráng|bắp rang|khoai chiên|nui sấy)\b",
        re.IGNORECASE,
    )
    for m in food_count_regex.finditer(text):
        if not is_inside_spans(m.start(), m.end(), non_count_spans):
            cnt = int(m.group(1))
            if 1 <= cnt <= 100:
                per_cat_counts["food_snack"] = cnt

    # Tech per-category: "5 headphones", "10 tai nghe", "5 thiết bị công nghệ"
    tech_count_regex = re.compile(
        r"\b(\d+)\s*(?:loại\s+|cái\s+|chiếc\s+)?(?:tai nghe|headphones|earphones|chuột|bàn phím|củ sạc|sạc|chargers|smartwatch|loa|phụ kiện|công nghệ|tech|gadgets)\b",
        re.IGNORECASE,
    )
    for m in tech_count_regex.finditer(text):
        if not is_inside_spans(m.start(), m.end(), non_count_spans):
            cnt = int(m.group(1))
            if 1 <= cnt <= 100:
                per_cat_counts["tech"] = cnt

    # 2. General product count patterns: "10 sản phẩm", "10 products", "top 5"
    gen_count_regex = re.compile(
        r"\b(\d+)\s*(?:sản phẩm|san pham|products|product|items|item|món|mon|sp|cái|cai|loại|loai)\b",
        re.IGNORECASE,
    )
    for m in gen_count_regex.finditer(text):
        if not is_inside_spans(m.start(), m.end(), non_count_spans):
            cnt = int(m.group(1))
            if 1 <= cnt <= 100:
                total_count = cnt
                break

    if total_count is None:
        top_regex = re.compile(r"\btop\s*(\d+)\b", re.IGNORECASE)
        for m in top_regex.finditer(text):
            if not is_inside_spans(m.start(), m.end(), non_count_spans):
                cnt = int(m.group(1))
                if 1 <= cnt <= 100:
                    total_count = cnt
                    break

    # 3. Fallback: integer immediately following action verbs "tìm 10", "find 10"
    if total_count is None and not per_cat_counts:
        verb_regex = re.compile(r"\b(?:tìm|tim|find|get|scout)\s+(\d+)\b", re.IGNORECASE)
        for m in verb_regex.finditer(text):
            if not is_inside_spans(m.start(), m.end(), non_count_spans):
                cnt = int(m.group(1))
                if 1 <= cnt <= 100:
                    total_count = cnt
                    break

    # 4. If total_count wasn't explicitly given with 'sản phẩm' but per_cat_counts exist
    if total_count is None and per_cat_counts:
        total_count = sum(per_cat_counts.values())

    return total_count, per_cat_counts


@dataclass
class CategorySubTask:
    """A concrete category crawling task dispatched by the front-end agent."""
    category_id: str  # "food_snack" | "tech"
    category_name: str  # Display name e.g. "đồ ăn vặt"
    sub_prompt: str
    criteria: ParsedCriteria
    target_count: int
    focus_keywords: list[str] = field(default_factory=list)


@dataclass
class DispatchMetadata:
    """Metadata detailing how the prompt was classified and decomposed."""
    classification_method: str  # "heuristic" | "llm" | "fallback"
    confidence: float
    fallback_reason: str | None = None
    original_prompt: str = ""
    detected_categories: list[str] = field(default_factory=list)
    generated_sub_prompts: dict[str, str] = field(default_factory=dict)
    split_details: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification_method": self.classification_method,
            "confidence": self.confidence,
            "fallback_reason": self.fallback_reason,
            "original_prompt": self.original_prompt,
            "detected_categories": self.detected_categories,
            "generated_sub_prompts": self.generated_sub_prompts,
            "split_details": self.split_details,
        }


@dataclass
class DispatchResult:
    """Outcome of prompt category classification and task decomposition."""
    original_prompt: str
    status: str  # "dispatched" | "unsupported_category" | "needs_review"
    is_multi_category: bool
    tasks: list[CategorySubTask]
    metadata: DispatchMetadata
    message: str = ""


class CategoryDispatcherAgent:
    """Front-end AI Dispatcher Agent explicitly supporting 'food_snack' and 'tech'."""

    def __init__(
        self,
        enable_llm: bool | None = None,
        llm_caller: Callable[[str, str], dict[str, Any] | None] | None = None,
    ):
        if enable_llm is None:
            enable_llm = os.environ.get("PRODUCT_SCOUT_ENABLE_LLM_DISPATCHER") == "true"
        self.enable_llm = enable_llm
        self._llm_caller = llm_caller

    def analyze_and_dispatch(self, prompt: str) -> DispatchResult:
        """Analyze user prompt and partition into isolated category tasks."""
        text = (prompt or "").strip()
        lower_text = text.lower()
        folded_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").casefold()

        # Step 1: Extract price constraints and mask non-count spans
        price_min, price_max, has_custom_price, price_spans = extract_price_and_spans(text)
        non_count_spans = extract_non_count_spans(text, price_spans)

        # Validate price range: reject invalid ranges where min > max
        if price_min > 0 and price_max > 0 and price_min > price_max:
            logger.info("Invalid price range detected (min > max): %d > %d in prompt '%s'", price_min, price_max, text)
            meta = DispatchMetadata(
                classification_method="heuristic",
                confidence=0.95,
                original_prompt=text,
                fallback_reason=None,
                detected_categories=[],
            )
            return DispatchResult(
                original_prompt=text,
                status="needs_review",
                is_multi_category=False,
                tasks=[],
                metadata=meta,
                message=f"Khoảng giá không hợp lệ: giá tối thiểu ({price_min:,}đ) lớn hơn giá tối đa ({price_max:,}đ).",
            )

        # Step 2: Extract counts (total and per-category)
        parsed_total_count, per_cat_counts = extract_counts(text, non_count_spans)

        # Validate explicit count <= 0
        if parsed_total_count is not None and parsed_total_count <= 0:
            meta = DispatchMetadata(
                classification_method="heuristic",
                confidence=0.95,
                original_prompt=text,
                detected_categories=[],
            )
            return DispatchResult(
                original_prompt=text,
                status="needs_review",
                is_multi_category=False,
                tasks=[],
                metadata=meta,
                message="Số lượng sản phẩm yêu cầu phải lớn hơn 0.",
            )

        # Step 3: Heuristic keyword matching
        def match_vocab(vocab: frozenset[str]) -> int:
            score = 0
            for kw in vocab:
                pat = rf"\b{re.escape(kw)}\b"
                if re.search(pat, text, re.IGNORECASE) or re.search(pat, folded_text, re.IGNORECASE):
                    score += 2 if len(kw.split()) > 1 else 1
            return score

        food_score = match_vocab(FOOD_KEYWORDS)
        tech_score = match_vocab(TECH_KEYWORDS)
        unsupported_score = match_vocab(UNSUPPORTED_KEYWORDS)

        # Check explicit colloquial food signals
        if any(term in lower_text for term in ("ăn vặt", "an vat", "bán chay", "ban chay", "đồ ăn", "bánh kẹo", "snack")):
            food_score = max(food_score, 3)

        method = "heuristic"
        confidence = 0.95
        fallback_reason: str | None = None
        split_details: str | None = None

        # Case 1: Unsupported Category requested
        if unsupported_score > 0:
            logger.info("Unsupported category detected in prompt: %s", text)
            meta = DispatchMetadata(
                classification_method="heuristic",
                confidence=0.95,
                original_prompt=text,
                fallback_reason=None,
                detected_categories=[],
            )
            return DispatchResult(
                original_prompt=text,
                status="unsupported_category",
                is_multi_category=False,
                tasks=[],
                metadata=meta,
                message="Chỉ hỗ trợ 2 ngành hàng: 'food_snack' (đồ ăn vặt) và 'tech' (sản phẩm công nghệ). Yêu cầu chứa ngành hàng không được hỗ trợ.",
            )

        # Case 2: Ambiguous prompt (no explicit food, no explicit tech, no unsupported keywords)
        if food_score == 0 and tech_score == 0:
            has_attribute_only = any(term in lower_text or term in folded_text for term in ("chống nước", "chong nuoc", "kháng nước", "khang nuoc"))
            if has_attribute_only:
                meta = DispatchMetadata(
                    classification_method="heuristic",
                    confidence=0.85,
                    fallback_reason="Attribute 'chống nước / kháng nước' is not standalone proof of tech",
                    original_prompt=text,
                    detected_categories=[],
                )
                return DispatchResult(
                    original_prompt=text,
                    status="needs_review",
                    is_multi_category=False,
                    tasks=[],
                    metadata=meta,
                    message="Thuộc tính 'chống nước / kháng nước' không tự xác định ngành hàng. Vui lòng nêu rõ loại sản phẩm (ví dụ: 'tai nghe chống nước' hoặc 'đồng hồ chống nước').",
                )

            product_signal = any(
                term in lower_text or term in folded_text
                for term in ("san pham", "sản phẩm", "mon", "món", "do", "đồ", "hang", "hàng", "hot trend", "trend", "ban chay", "bán chạy", "tiktok", "shopee")
            )
            if not product_signal:
                meta = DispatchMetadata(
                    classification_method="heuristic",
                    confidence=0.3,
                    fallback_reason="No product or category signals found in prompt",
                    original_prompt=text,
                    detected_categories=[],
                )
                return DispatchResult(
                    original_prompt=text,
                    status="needs_review",
                    is_multi_category=False,
                    tasks=[],
                    metadata=meta,
                    message="Không thể xác định sản phẩm hoặc ngành hàng 'food_snack' hoặc 'tech' từ yêu cầu.",
                )

            # We have a generic product request without category keywords (e.g. "tìm 10 sản phẩm")
            if self.enable_llm:
                llm_cats, llm_reason = self._call_llm_analyzer(text)
                if llm_cats:
                    method = "llm"
                    confidence = 0.85
                    detected_cats = llm_cats
                else:
                    method = "fallback"
                    confidence = 0.80
                    fallback_reason = f"No explicit category keywords found; LLM fallback: {llm_reason}. Applied deterministic fallback category 'tech'."
                    limit = parsed_total_count or 10
                    detected_cats = [("tech", "sản phẩm công nghệ", limit)]
            else:
                method = "fallback"
                confidence = 0.80
                fallback_reason = "No explicit category keywords found and LLM is disabled; applied deterministic fallback category 'tech'."
                limit = parsed_total_count or 10
                detected_cats = [("tech", "sản phẩm công nghệ", limit)]
        else:
            # Case 3: We have category signals
            is_multi = (food_score > 0 and tech_score > 0)
            if is_multi:
                meta = DispatchMetadata(
                    classification_method="heuristic",
                    confidence=0.95,
                    original_prompt=text,
                    detected_categories=["food_snack", "tech"],
                )
                return DispatchResult(
                    original_prompt=text,
                    status="needs_review",
                    is_multi_category=True,
                    tasks=[],
                    metadata=meta,
                    message="Mỗi workflow chỉ hỗ trợ một ngành hàng. Hãy chọn một trong hai: food_snack hoặc tech trước khi chạy discovery.",
                )

                # Multi-category prompt
                k_food = per_cat_counts.get("food_snack")
                k_tech = per_cat_counts.get("tech")

                if k_food is not None and k_tech is not None:
                    # Both explicit category counts given
                    if parsed_total_count is not None and (k_food + k_tech != parsed_total_count):
                        meta = DispatchMetadata(
                            classification_method="heuristic",
                            confidence=0.95,
                            original_prompt=text,
                            detected_categories=[],
                        )
                        return DispatchResult(
                            original_prompt=text,
                            status="needs_review",
                            is_multi_category=True,
                            tasks=[],
                            metadata=meta,
                            message=f"Mâu thuẫn số lượng: tổng yêu cầu là {parsed_total_count} sản phẩm nhưng tổng từng ngành ({k_food} đồ ăn + {k_tech} công nghệ = {k_food + k_tech}) không khớp.",
                        )
                    food_limit = k_food
                    tech_limit = k_tech
                    split_details = f"explicit_per_category: food_snack={food_limit}, tech={tech_limit}"
                elif k_food is not None:
                    # Only food count explicit
                    if parsed_total_count is not None:
                        if k_food >= parsed_total_count:
                            meta = DispatchMetadata(
                                classification_method="heuristic",
                                confidence=0.95,
                                original_prompt=text,
                                detected_categories=[],
                            )
                            return DispatchResult(
                                original_prompt=text,
                                status="needs_review",
                                is_multi_category=True,
                                tasks=[],
                                metadata=meta,
                                message=f"Mâu thuẫn số lượng: số lượng đồ ăn ({k_food}) lớn hơn hoặc bằng tổng số sản phẩm yêu cầu ({parsed_total_count}), không thể chia cho ngành công nghệ.",
                            )
                        food_limit = k_food
                        tech_limit = parsed_total_count - k_food
                        split_details = f"deduced_remainder: total={parsed_total_count}, food_snack={food_limit}, tech={tech_limit}"
                    else:
                        food_limit = k_food
                        tech_limit = 5
                        split_details = f"single_explicit: food_snack={food_limit}, tech=5"
                elif k_tech is not None:
                    # Only tech count explicit
                    if parsed_total_count is not None:
                        if k_tech >= parsed_total_count:
                            meta = DispatchMetadata(
                                classification_method="heuristic",
                                confidence=0.95,
                                original_prompt=text,
                                detected_categories=[],
                            )
                            return DispatchResult(
                                original_prompt=text,
                                status="needs_review",
                                is_multi_category=True,
                                tasks=[],
                                metadata=meta,
                                message=f"Mâu thuẫn số lượng: số lượng công nghệ ({k_tech}) lớn hơn hoặc bằng tổng số sản phẩm yêu cầu ({parsed_total_count}), không thể chia cho ngành đồ ăn.",
                            )
                        food_limit = parsed_total_count - k_tech
                        tech_limit = k_tech
                        split_details = f"deduced_remainder: total={parsed_total_count}, food_snack={food_limit}, tech={tech_limit}"
                    else:
                        food_limit = 5
                        tech_limit = k_tech
                        split_details = f"single_explicit: food_snack=5, tech={tech_limit}"
                elif parsed_total_count is not None:
                    # Neither category count explicit, but total count given
                    if parsed_total_count < 2:
                        meta = DispatchMetadata(
                            classification_method="heuristic",
                            confidence=0.95,
                            original_prompt=text,
                            detected_categories=[],
                        )
                        return DispatchResult(
                            original_prompt=text,
                            status="needs_review",
                            is_multi_category=True,
                            tasks=[],
                            metadata=meta,
                            message=f"Không thể phân bổ {parsed_total_count} sản phẩm cho 2 ngành hàng khác nhau (food_snack và tech). Cần tối thiểu 2 sản phẩm.",
                        )
                    if self.enable_llm:
                        llm_cats, llm_reason = self._call_llm_analyzer(text)
                        if llm_cats and len(llm_cats) == 2:
                            method = "llm"
                            confidence = 0.85
                            llm_cat_dict = {c[0]: c[2] for c in llm_cats}
                            food_limit = llm_cat_dict.get("food_snack", parsed_total_count // 2)
                            tech_limit = llm_cat_dict.get("tech", parsed_total_count - food_limit)
                            split_details = f"llm_split: food_snack={food_limit}, tech={tech_limit}"
                        else:
                            method = "fallback"
                            confidence = 0.75
                            fallback_reason = f"Multi-category split unclear from prompt; LLM fallback: {llm_reason}. Used deterministic equal split."
                            half = max(1, parsed_total_count // 2)
                            food_limit = half
                            tech_limit = max(1, parsed_total_count - half)
                            split_details = f"deterministic_equal_split: total={parsed_total_count} -> food_snack={food_limit}, tech={tech_limit}"
                    else:
                        method = "heuristic"
                        confidence = 0.85
                        half = max(1, parsed_total_count // 2)
                        food_limit = half
                        tech_limit = max(1, parsed_total_count - half)
                        split_details = f"deterministic_equal_split: total={parsed_total_count} -> food_snack={food_limit}, tech={tech_limit}"
                else:
                    # Default split
                    method = "heuristic"
                    food_limit = 5
                    tech_limit = 5
                    split_details = "default_equal_split: food_snack=5, tech=5"

                detected_cats = [
                    ("food_snack", "đồ ăn vặt", food_limit),
                    ("tech", "sản phẩm công nghệ", tech_limit),
                ]
            elif food_score > 0:
                limit = per_cat_counts.get("food_snack") or parsed_total_count or 10
                detected_cats = [("food_snack", "đồ ăn vặt", limit)]
            else:
                limit = per_cat_counts.get("tech") or parsed_total_count or 10
                detected_cats = [("tech", "sản phẩm công nghệ", limit)]

        # Step 4: Build category sub-tasks
        is_multi_cat = len(detected_cats) > 1
        tasks: list[CategorySubTask] = []
        generated_sub_prompts: dict[str, str] = {}

        # Platforms
        platforms: list[str] = []
        if "tiktok" in lower_text:
            platforms.append("tiktok")
        if "shopee" in lower_text:
            platforms.append("shopee")
        if "lazada" in lower_text:
            platforms.append("lazada")
        if not platforms:
            platforms = ["tiktok", "shopee"]

        for cat_id, cat_name, cat_limit in detected_cats:
            focus_kw = self._extract_focus_keywords(text, cat_id)
            sub_prompt = self._build_sub_prompt(text, cat_id, cat_name, cat_limit, focus_kw, has_custom_price, price_min, price_max)
            generated_sub_prompts[cat_id] = sub_prompt

            # Build category-isolated criteria
            if cat_id == "food_snack":
                c_price_min = price_min if has_custom_price else 0
                c_price_max = price_max if has_custom_price else 0
            else:
                c_price_min = price_min if has_custom_price else 200_000
                c_price_max = price_max if has_custom_price else 1_000_000

            criteria = ParsedCriteria(
                raw_prompt=sub_prompt,
                limit=cat_limit,
                category=cat_name,
                price_min=c_price_min,
                price_max=c_price_max,
                platforms=platforms,
                extra_requirements=text,
            )

            tasks.append(
                CategorySubTask(
                    category_id=cat_id,
                    category_name=cat_name,
                    sub_prompt=sub_prompt,
                    criteria=criteria,
                    target_count=cat_limit,
                    focus_keywords=focus_kw,
                )
            )

        metadata = DispatchMetadata(
            classification_method=method,
            confidence=confidence,
            fallback_reason=fallback_reason,
            original_prompt=text,
            generated_sub_prompts=generated_sub_prompts,
            split_details=split_details,
        )

        logger.info(
            "Dispatched prompt into %d task(s) via %s (confidence=%.2f): %s",
            len(tasks),
            method,
            confidence,
            [t.category_id for t in tasks],
        )

        return DispatchResult(
            original_prompt=text,
            status="dispatched",
            is_multi_category=is_multi_cat,
            tasks=tasks,
            metadata=metadata,
            message="Successfully dispatched",
        )

    @staticmethod
    def _extract_focus_keywords(text: str, category_id: str) -> list[str]:
        """Extract explicit product keywords mentioned in user prompt."""
        keywords: list[str] = []
        low = text.lower()
        if category_id == "food_snack":
            candidates = [
                "bánh tráng trộn", "bánh tráng", "bắp rang bơ", "bắp rang",
                "bỏng ngô", "khoai tây chiên", "khoai chiên", "nui sấy giòn", "nui sấy",
                "khô gà lá chanh", "khô bò", "rong biển", "bánh quy", "kẹo dẻo",
                "trà sữa đóng chai", "trà xanh", "nước ngọt",
            ]
            for kw in candidates:
                if kw in low:
                    keywords.append(kw)
        elif category_id == "tech":
            candidates = [
                "tai nghe bluetooth", "tai nghe chống ồn", "tai nghe",
                "chuột không dây", "chuột gaming", "bàn phím cơ",
                "củ sạc nhanh", "sạc dự phòng", "quạt mini", "smartwatch",
            ]
            for kw in candidates:
                if kw in low:
                    keywords.append(kw)
        return keywords

    @staticmethod
    def _build_sub_prompt(
        original: str,
        cat_id: str,
        cat_name: str,
        limit: int,
        focus_kw: list[str],
        has_custom_price: bool,
        price_min: int,
        price_max: int,
    ) -> str:
        """Construct an optimized sub-prompt for the category-specific run."""
        kw_phrase = f", ưu tiên {', '.join(focus_kw)}" if focus_kw else ""
        platform_phrase = "hot trend tiktok/shopee" if "tiktok" in original.lower() else "bán chạy hot trend"

        if has_custom_price:
            if price_min > 0 and price_max > 0:
                price_phrase = f" trong phạm vi giá từ {price_min:,}đ đến {price_max:,}đ"
            elif price_max > 0:
                price_phrase = f" trong phạm vi giá dưới {price_max:,}đ"
            elif price_min > 0:
                price_phrase = f" trong phạm vi giá từ {price_min:,}đ"
            else:
                price_phrase = ""
        elif cat_id == "tech":
            price_phrase = " trong phạm vi giá từ 200k-1tr vnd"
        else:
            price_phrase = ""

        return f"tìm {limit} sản phẩm {cat_name.lower()} {platform_phrase}{price_phrase}{kw_phrase}"

    def _call_llm_analyzer(self, text: str) -> tuple[list[tuple[str, str, int]] | None, str | None]:
        """Optional LLM decomposition for ambiguous or unclear multi-category inputs."""
        caller = self._llm_caller
        if not caller:
            try:
                from product_scout.trend_discovery.query_planner import QueryPlanner
                caller = QueryPlanner._call_default_llm
            except Exception as exc:
                return None, f"llm_caller_unavailable: {exc}"

        try:
            system_prompt = (
                "You are an e-commerce category classification agent. "
                "Supported categories are ONLY: 'food_snack' and 'tech'. "
                "If the user asks for anything else (e.g. fashion, books, cosmetics), return {\"status\": \"unsupported\"}. "
                "Otherwise return JSON: {\"status\": \"supported\", \"categories\": [{\"id\": \"food_snack\"|\"tech\", \"limit\": int}]}"
            )
            res = caller(system_prompt, text)
            if not res or not isinstance(res, dict):
                return None, "llm_empty_response"

            if res.get("status") == "unsupported":
                return None, "unsupported"

            raw_cats = res.get("categories")
            if isinstance(raw_cats, list):
                valid_cats: list[tuple[str, str, int]] = []
                for item in raw_cats:
                    cid = item.get("id")
                    clim = int(item.get("limit", 5))
                    if cid == "food_snack":
                        valid_cats.append(("food_snack", "đồ ăn vặt", clim))
                    elif cid == "tech":
                        valid_cats.append(("tech", "sản phẩm công nghệ", clim))
                if valid_cats:
                    return valid_cats, None
                return None, "invalid_category_returned"

            return None, "missing_categories_field"
        except Exception as exc:
            return None, f"exception: {exc}"
