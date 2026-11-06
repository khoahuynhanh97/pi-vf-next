"""Product identity normalization and canonical key generation."""

from __future__ import annotations

import re
import unicodedata

_NOISE = re.compile(r"\b(?:sale|giam|chinh hang|freeship|combo|voucher)\b|\d+%", re.IGNORECASE)
_MARKETPLACE_PREFIX = re.compile(
    r"^(?:(?:https?://)?(?:www\.)?)?(?:shopee\.vn|lazada\.vn|tiktok\.com)/",
    re.IGNORECASE,
)
_PRICE_LIKE_MODEL = re.compile(r"^\d+(?:[.,]\d+)?\s*(?:k|tr|triệu|đ|vnd)$", re.IGNORECASE)

_PSEUDO_MODEL = re.compile(
    r"^(?:top|sale|giam|giảm|review|danh gia|đánh giá|thang|tháng|nam|năm|ngay|ngày|gio|giờ|lan|lần|thu|thứ|"
    r"so|số|muc|mức|cap|cấp|loai|loại|phan|phần|trong|tay|of|for|in|at|on|under|bank|power|"
    r"jan|feb|mar|march|apr|april|may|jun|june|jul|july|aug|sep|oct|nov|dec|"
    r"ces|ifa|mwc|wwdc|bh|ipx|ip|bluetooth|bt|wifi|usb|hdmi|led|rgb|pd|qc)\s*\d+[a-z]*$"
    r"|^\d+\s*(?:mah|wh|inch|hz|db|mp|gb|tb|mm|cm|kg|g|l|ml|t|tháng|năm|độ|mode)$"
    r"|^(?:2k|4k|8k|1080p|720p|360|2023|2024|2025|2026|2027|12t|24t|5g|4g|2\.4g|2\.0|3\.0|5\.0|5\.3|5\.4)$",
    re.IGNORECASE,
)


def normalize_identity_text(value: str) -> str:
    """Normalize text by folding unicode, stripping commerce noise and extra punctuation."""
    if not value:
        return ""
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    folded = _NOISE.sub(" ", folded.casefold())
    return " ".join(re.sub(r"[^a-z0-9]+", " ", folded).split())


def canonical_key(brand: str, model: str, name: str) -> str:
    """Generate a stable canonical key for cross-source and cross-day matching.

    Preference order:
    1. brand + model (if both present)
    2. brand + normalized name (if model is missing)
    3. normalized name (if brand is missing)
    """
    brand_key = normalize_identity_text(brand)
    model_key = normalize_identity_text(model)
    if brand_key and model_key:
        return f"{brand_key}|{model_key}"
    name_key = normalize_identity_text(name)
    if brand_key and name_key:
        return f"{brand_key}|{name_key}"
    return name_key


KNOWN_TECH_BRANDS = frozenset({
    "logitech", "baseus", "ugreen", "anker", "dareu", "ecovacs", "dreame", "roborock",
    "apple", "samsung", "xiaomi", "sony", "jbl", "marshall", "razer", "keychron",
    "oral-b", "oral b", "soundpeats", "rapoo", "edifier", "havit", "lenovo", "asus",
    "dell", "hp", "acer", "belkin", "choetech", "aukey", "tplink", "tp-link", "dlink",
    "imou", "ezviz", "yeelight", "aqara", "tineco", "deerma", "bear", "philips",
    "panasonic", "bose", "sennheiser", "garmin", "amazfit", "huawei", "oppo", "realme",
    "vivo", "dji", "insta360", "gopro", "fimi", "jisulife", "qcy", "kieslect", "moft",
    "akko", "hoco", "remax", "borofone", "romoss", "sandisk", "kingston", "corsair",
    "steelseries", "hyperx", "epomaker", "aula", "machenike", "fifine", "rode", "boya",
    "soundcore", "mijia", "enchen", "tribit", "velasboost", "cuktech", "zmi", "orico",
    "pisen", "vention", "mophie", "divoom", "lofree", "nuphy", "ajazz", "attack shark",
    "vxe", "edra", "e-dra", "tronsmart", "earfun", "moondrop", "fiio", "70mai", "ddpai",
    "tapo", "sonoff", "tuya", "simplus", "gaabor", "showsee", "usmile", "flyco", "lumias",
    "levoit", "redmi", "poco", "oneplus", "nothing", "cmf", "ulanzi", "maono", "xinmeng",
    "yoobao", "orashare", "locknlock", "halio", "lexar", "mchose", "monka", "haylou",
    "govee", "ravpower", "monster",
})

KNOWN_FOOD_BRANDS = frozenset({
    "lay's", "lays", "orion", "oishi", "pringles", "swing", "karamucho", "koikeya",
    "slide", "lorenz", "tao kae noi", "taokaenoi", "bibigo", "one one", "oneone",
    "ichi", "richy", "gumiho", "koreno", "chupa chups", "alpenliebe", "haribo",
    "play more", "playmore", "sugus", "hải hà", "hai ha", "mentos", "oreo", "gery",
    "ritz", "nabati", "cosy", "danisa", "lotte", "meiji", "pocky", "kinh đô", "kinh do",
    "custas", "phạm nguyên", "pham nguyen", "solite", "hura", "bảo ngọc", "bao ngoc",
    "c2", "không độ", "khong do", "tea+ plus", "tea+", "tea plus", "kirin", "fuzetea",
    "lipton", "yeo's", "pokka", "sting", "red bull", "redbull", "monster", "warrior",
    "wake-up 247", "wake up 247", "number 1", "coca-cola", "cocacola", "coca cola",
    "pepsi", "7up", "mirinda", "fanta", "aquafina", "lavie", "dasani", "twister",
    "th true milk", "vinamilk", "milo", "vissan", "cp", "ba huân", "ba huan",
    "heo cao bồi", "heo cao boi", "g kitchen", "xuxifarm", "ponnie", "cầu tre",
    "cau tre", "hảo hảo", "hao hao", "acecook", "omachi", "samyang", "indomie",
    "cung đình", "cung dinh", "militket", "vifon", "vinamit", "tân tân", "tan tan",
    "l'angfarm", "langfarm", "dan d pak", "bà tư bình phước", "ba tu binh phuoc",
    "chú tễu", "chu teu", "mixifood", "bà tư", "ba tu", "gia vị cố đô",
})

KNOWN_BRANDS = KNOWN_TECH_BRANDS | KNOWN_FOOD_BRANDS

TECH_KEYWORDS = frozenset({
    "tai nghe", "chuot", "chuột", "ban phim", "bàn phím", "cu sac", "củ sạc",
    "day sac", "dây sạc", "cap sac", "cáp sạc", "pin du phong", "pin dự phòng",
    "sac du phong", "sạc dự phòng", "loa", "smartwatch", "dong ho thong minh",
    "đồng hồ thông minh", "vong deo tay", "vòng đeo tay", "robot", "hut bui", "hút bụi",
    "camera", "wifi", "man hinh", "màn hình", "laptop", "dien thoai", "điện thoại",
    "phu kien", "phụ kiện", "bluetooth", "khong day", "không dây", "thong minh",
    "thông minh", "gadget", "cong nghe", "công nghệ", "smart", "sound", "audio",
    "led", "hub", "gia do", "giá đỡ", "ban chai dien", "bàn chải điện", "tam nuoc",
    "tăm nước", "may cao rau", "máy cạo râu", "quat mini", "quạt mini", "quat", "quạt",
    "sac nhanh", "sạc nhanh", "gan", "anc", "tws", "ssd", "usb", "o cam", "ổ cắm",
})

TECH_CONFLICT_KEYWORDS = frozenset({
    "banh", "bánh", "keo", "kẹo", "thuc pham", "thực phẩm", "nau an", "nấu ăn",
    "mon an", "món ăn", "quan ao", "quần áo", "vay", "váy", "dam", "đầm",
    "son moi", "son môi", "my pham", "mỹ phẩm", "tui xach", "túi xách",
    "giay dep", "giày dép", "trang suc", "trang sức", "noi com", "nồi cơm",
    "snack", "an vat", "ăn vặt", "do an", "đồ ăn", "do uong", "đồ uống",
    "bim bim", "bimbim", "khoai tay", "khoai tây", "nuoc ngot", "nước ngọt",
})

CONFLICT_KEYWORDS = TECH_CONFLICT_KEYWORDS

FOOD_KEYWORDS = frozenset({
    "banh", "bánh", "keo", "kẹo", "thuc pham", "thực phẩm", "snack", "an vat", "ăn vặt",
    "do an", "đồ ăn", "do uong", "đồ uống", "nuoc giai khat", "nước giải khát",
    "nuoc ngot", "nước ngọt", "tra", "trà", "ca phe", "cà phê", "rong bien", "rong biển",
    "khoai tay", "khoai tây", "kho bo", "khô bò", "kho ga", "khô gà", "xuc xich", "xúc xích",
    "mi goi", "mì gói", "hat dieu", "hạt điều", "dau phong", "đậu phộng", "keo deo", "kẹo dẻo",
    "dong chai", "đóng chai", "dong lon", "đóng lon", "dong goi", "đóng gói",
    "bim bim", "bimbim", "banh quy", "bánh quy", "ngu coc", "ngũ cốc", "sua chua", "sữa chua",
    "thach", "thạch", "nuoc ep", "nước ép", "khoai tay say", "khoai tây sấy",
})

FOOD_CONFLICT_KEYWORDS = frozenset({
    "tai nghe", "chuot", "chuột", "ban phim", "bàn phím", "cu sac", "củ sạc",
    "cap sac", "cáp sạc", "pin du phong", "pin dự phòng", "sac du phong", "sạc dự phòng",
    "loa", "smartwatch", "dong ho thong minh", "đồng hồ thông minh", "camera", "dien thoai",
    "điện thoại", "laptop", "may tinh", "máy tính", "quan ao", "quần áo", "son moi", "son môi",
    "my pham", "mỹ phẩm", "cách làm", "cach lam", "công thức", "cong thuc",
    "hướng dẫn nấu", "huong dan nau", "quán ăn", "quan an", "vỉa hè", "via he",
    "tự làm", "tu lam", "nấu tại nhà",
})

NON_PACKAGED_FOOD_PATTERNS = re.compile(
    r"\b(?:cach lam|cách làm|cong thuc|công thức|huong dan nau|hướng dẫn nấu|"
    r"quan an|quán ăn|via he|vỉa hè|tu lam|tự làm|nau tai nha|nấu tại nhà|"
    r"tai quan|tại quán|quan cafe|quán cafe|hamburger|gà rán|ga ran|mực vòng|muc vong|"
    r"bánh cá rán|banh ca ran|lẩu|lau|buffet|cơm tấm|com tam|phở|pho|bún bò|bun bo|"
    r"lotte mart|siêu thị|sieu thi|bách hóa xanh|bach hoa xanh|winmart|coopmart|menu|thực đơn)\b",
    re.IGNORECASE,
)

_GENERIC_ARTICLE_PATTERNS = re.compile(
    r"\b(?:video trends|megatrends|social media|huong dan|hướng dẫn|clip on gian|"
    r"la trend gi|là trend gì|cap hot trends|cặp hot trends|se en va i|sẽ đến và đi|"
    r"cach lam|cách làm|bi quyet|bí quyết|tong hop|tổng hợp|xep hang|xếp hạng|"
    r"cach tim san pham|cách tìm sản phẩm|ban gi tren|bán gì trên|"
    r"co tot khong|có tốt không|cua nuoc nao|của nước nào|co may vi|có mấy vị|"
    r"nao ngon nhat|nào ngon nhất|mua o dau|mua ở đâu|gia bao nhieu|giá bao nhiêu|"
    r"danh gia|đánh giá|review|try and review|dia chi|địa chỉ|thuc don|thực đơn|menu)\b"
    r"|\?"
    r"|^\s*(?:top\s+\d+|\d+\s+(?:thiết bị|thiet bi|món|mon|sản phẩm|san pham|phụ kiện|phu kien|xu hướng|xu huong))\b",
    re.IGNORECASE,
)


def _is_valid_model_candidate(val: str, brand_clean: str) -> bool:
    cleaned = val.strip(" -_.,|/()")
    if not cleaned or len(cleaned) < 2:
        return False
    low = cleaned.casefold()
    if low in brand_clean or brand_clean in low:
        return False
    # Reject if model candidate contains another known brand name (e.g., "Ugreen 2" when brand is Baseus)
    for other_brand in KNOWN_BRANDS:
        if len(other_brand) >= 3 and re.search(rf"\b{re.escape(other_brand)}\b", low):
            return False
    if _PRICE_LIKE_MODEL.fullmatch(cleaned) or _PSEUDO_MODEL.fullmatch(cleaned):
        return False
    norm = normalize_identity_text(cleaned)
    if not norm or _PSEUDO_MODEL.fullmatch(norm):
        return False
    return True


def extract_product_identity(title: str, snippet: str = "", category: str = "") -> tuple[str, str, str]:
    """Extract brand, model, and cleaned product name from title and snippet.

    Returns (brand, model, cleaned_name).
    If title is a generic article or cannot be resolved, brand and model will be empty strings.
    """
    text = title.strip()
    marketplace_prefix = _MARKETPLACE_PREFIX.match(text)
    if marketplace_prefix:
        text = text[marketplace_prefix.end():].split("?", 1)[0].replace("-", " ").replace("/", " ")
        text = text.replace("...", " ").strip()

    # Check for generic non-product article indicators
    if _GENERIC_ARTICLE_PATTERNS.search(text):
        return "", "", text

    # Strip promotional clauses like "| Sale 40%" before model extraction
    text_for_model = re.sub(
        r"\b(?:sale|giảm|giam)\s+\d+%?\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )

    # 1. Detect Brand
    detected_brand = ""
    for b in sorted(KNOWN_BRANDS, key=len, reverse=True):
        pattern = rf"\b{re.escape(b)}\b"
        if re.search(pattern, text, re.IGNORECASE):
            detected_brand = b
            break

    if not detected_brand and snippet:
        for b in sorted(KNOWN_BRANDS, key=len, reverse=True):
            pattern = rf"\b{re.escape(b)}\b"
            if re.search(pattern, snippet, re.IGNORECASE):
                detected_brand = b
                break

    # 2. Detect Model
    detected_model = ""
    is_food = (detected_brand.casefold() in KNOWN_FOOD_BRANDS) or (
        category and any(
            t in category.casefold()
            for t in ("thực phẩm", "đồ ăn", "đồ uống", "snack", "bánh kẹo", "nước giải khát", "nước ngọt")
        )
    )

    if detected_brand:
        brand_clean = detected_brand.replace("-", " ").casefold()
        if is_food:
            if NON_PACKAGED_FOOD_PATTERNS.search(text):
                return "", "", text
            detected_model = _extract_food_sku(text_for_model, detected_brand)
        else:
            model_patterns = [
                # Named series and multi-word product families
                r"\b("
                r"MX Master \w+|MX Keys \w+|Deebot \w+(?: \w+)?|Bowie \w+\+?|"
                r"Nexode(?:\s+RG)?(?:\s+\d+W)?|PowerCore \w+|AirPods (?:Pro )?\w+|"
                r"Galaxy (?:Watch|Buds) \w+|(?:Smart )?Band \d+(?: Pro)?|"
                r"Flip \d+|Emberton \w+|DeathAdder \w+|"
                r"StormBox(?:\s+(?:Mini|Micro|Flow|Blast))?|MaxSound(?:\s+Plus)?|"
                r"Air\s*\d+(?:\s+(?:Pro|Lite|HS|Plus|Max|ANC))?|"
                r"Mini\s+\d+(?:\s+Pro)?|Life\s+\d+|Ranger\s+\d+|"
                r"Solar\s+(?:Lite|Pro|Plus)|Kr\s+Pro|Bip\s+\d+|"
                r"Space\s+(?:One|Travel)|XSound\s+Go|Uno\s+Robot(?:\s+GaN)?|"
                r"Desk\s+Lamp\s+Lite|Screen\s+(?:Light\s+)?Bar|i-wok\s*\d+|"
                r"Select\s+\d+\s+Go|Go\s+\d+|Clip\s+\d+|"
                r"T\d+\s+Mini|R\d+\s+SE|3087\s+RF|Pro\s+\d+|"
                r"Zolo(?:\s+A\d{4})?|Exodia(?:\s+\w+)?"
                r")\b",
                # Alphanumeric model codes (e.g., WM02, T13 ANC, PB100P, C210, M75, X3, BP1 Ultra, W820NB Plus)
                r"\b([A-Za-z]{1,6}[- ]?\d{1,4}[A-Za-z0-9+]*(?:[- ](?:Pro|Plus|Ultra|Max|Mini|Lite|SE|ANC|HS|RF|Omni))?)\b",
                # 5-digit SKU/model codes (e.g. Ugreen 15495, 35998), N-in-1 hubs, or wattage
                r"\b(\d{5}|\d+\s*in\s*1|\d{2,4}[A-Za-z]{1,4}|\d{2,3}W)\b",
            ]

            # Prefer searching right after the brand occurrence in the title first
            brand_match = re.search(rf"\b{re.escape(detected_brand)}\b", text_for_model, re.IGNORECASE)
            search_scopes = []
            if brand_match:
                after_brand = text_for_model[brand_match.end():]
                if after_brand.strip():
                    search_scopes.append(after_brand)
            search_scopes.append(text_for_model)

            for scope in search_scopes:
                for pat in model_patterns:
                    for m in re.finditer(pat, scope, re.IGNORECASE):
                        val = m.group(1).strip()
                        if _is_valid_model_candidate(val, brand_clean):
                            detected_model = val
                            break
                    if detected_model:
                        break
                if detected_model:
                    break

    # 3. Clean Name
    clean_name = text
    noise_patterns = [
        r"[-|/]\s*(?:Chính hãng|Giá tốt|Mua ngay|Freeship|Shopee|Lazada|TikTok|ZNEWS|Gadget|Bảo hành).*$",
        r"\b(?:chính hãng|giá tốt|mua ngay|freeship|sale \d+%?|giảm \d+%?)\b",
        r"\bTháng \d+,\s*\d{4}\b",
    ]
    for np in noise_patterns:
        clean_name = re.sub(np, "", clean_name, flags=re.IGNORECASE)
    clean_name = " ".join(clean_name.split()).strip("-:,| ")

    special_casing = {
        "ugreen": "Ugreen",
        "dareu": "DareU",
        "jbl": "JBL",
        "qcy": "QCY",
        "soundpeats": "SoundPEATS",
        "aula": "AULA",
        "vxe": "VXE",
        "e-dra": "E-Dra",
        "edra": "E-Dra",
        "oral-b": "Oral-B",
        "oral b": "Oral-B",
        "tp-link": "TP-Link",
        "tplink": "TP-Link",
        "zmi": "ZMI",
        "dji": "DJI",
        "hp": "HP",
        "cmf": "CMF",
        "showsee": "ShowSee",
        "locknlock": "LocknLock",
        "lays": "Lay's",
        "lay's": "Lay's",
        "orion": "Orion",
        "oishi": "Oishi",
        "pringles": "Pringles",
        "swing": "Swing",
        "karamucho": "Karamucho",
        "koikeya": "Koikeya",
        "tao kae noi": "Tao Kae Noi",
        "taokaenoi": "Tao Kae Noi",
        "bibigo": "Bibigo",
        "one one": "One One",
        "oneone": "One One",
        "ichi": "Ichi",
        "richy": "Richy",
        "chupa chups": "Chupa Chups",
        "alpenliebe": "Alpenliebe",
        "haribo": "Haribo",
        "play more": "Play More",
        "playmore": "Play More",
        "sugus": "Sugus",
        "hảo hảo": "Hảo Hảo",
        "hao hao": "Hảo Hảo",
        "acecook": "Acecook",
        "omachi": "Omachi",
        "samyang": "Samyang",
        "indomie": "Indomie",
        "cung đình": "Cung Đình",
        "cung dinh": "Cung Đình",
        "kinh đô": "Kinh Đô",
        "kinh do": "Kinh Đô",
        "danisa": "Danisa",
        "oreo": "Oreo",
        "gery": "Gery",
        "nabati": "Nabati",
        "custas": "Custas",
        "c2": "C2",
        "không độ": "Không Độ",
        "khong do": "Không Độ",
        "tea+ plus": "TEA+ Plus",
        "tea+": "TEA+ Plus",
        "tea plus": "TEA+ Plus",
        "kirin": "Kirin",
        "fuzetea": "Fuzetea",
        "sting": "Sting",
        "red bull": "Red Bull",
        "redbull": "Red Bull",
        "monster": "Monster",
        "warrior": "Warrior",
        "vissan": "Vissan",
        "cp": "CP",
        "heo cao bồi": "Heo Cao Bồi",
        "heo cao boi": "Heo Cao Bồi",
        "g kitchen": "G Kitchen",
        "ponnie": "Ponnie",
        "vinamit": "Vinamit",
        "tân tân": "Tân Tân",
        "tan tan": "Tân Tân",
    }
    brand_disp = special_casing.get(detected_brand.casefold(), detected_brand.title()) if detected_brand else ""

    return brand_disp, detected_model, clean_name or text


def _extract_food_sku(text: str, brand: str) -> str:
    """Extract product line, flavor, weight/volume/pack count distinguishing the food SKU."""
    brand_match = re.search(rf"\b{re.escape(brand)}\b", text, re.IGNORECASE)
    if not brand_match:
        return ""
    before = text[:brand_match.start()].strip(" -:,|/()–—")
    after = text[brand_match.end():].strip(" -:,|/()–—")

    noise_re = re.compile(
        r"[-|/–—]\s*(?:Chính hãng|Giá tốt|Shopee.*|Lazada.*|TikTok.*|Freeship.*|Sale.*|Giảm.*|Combo.*|Voucher.*|"
        r"Shop\s+.*|Chuỗi siêu thị.*|Nhà sách.*|Try and Review.*|Websosanh.*|Bách hóa.*|Foodland.*|"
        r"Quality.*|US MART.*)$",
        re.IGNORECASE,
    )
    after = noise_re.sub("", after).strip(" -:,|/()–—")
    before = re.sub(r"\[.*?\]", "", before)
    before = re.sub(
        r"^(?:sản phẩm|món|hàng|combo|chính hãng)\s*",
        "",
        before,
        flags=re.IGNORECASE,
    ).strip(" -:,|/()–—")

    if before and after:
        if any(w in before.lower() for w in ("vị", "snack", "bánh", "kẹo", "bắp rang", "khoai", "nui", "rong biển", "khô")):
            cleaned = f"{before} {after}".strip()
        else:
            cleaned = after
    elif after:
        cleaned = after
    else:
        cleaned = before

    if not cleaned or len(cleaned) < 3:
        return ""
    if _PRICE_LIKE_MODEL.fullmatch(cleaned) or _PSEUDO_MODEL.fullmatch(cleaned):
        return ""
    if any(w in cleaned.lower() for w in ("nhiều loại", "nhiều vị", "đủ vị", "mix vị", "tùy chọn")):
        return ""
    if "?" in cleaned or any(w in cleaned.lower() for w in ("là một loại", "la mot loai", "được làm từ", "duoc lam tu", "có tốt không", "nào ngon nhất", "mua ở đâu", "của nước nào", "mấy vị")):
        return ""
    return cleaned


def is_usable_product_identity(name: str, brand: str, model: str, category: str = "") -> bool:
    """Require a concrete title identity before admitting a trend to video intake."""
    name_text = name.strip()
    brand_text = normalize_identity_text(brand)
    model_text = model.strip()
    if not name_text or _MARKETPLACE_PREFIX.match(name_text):
        return False
    if _GENERIC_ARTICLE_PATTERNS.search(name_text) or _GENERIC_ARTICLE_PATTERNS.search(model_text):
        return False
    if not brand_text or not model_text:
        return False

    is_food = (brand_text in KNOWN_FOOD_BRANDS) or (
        category and any(
            t in category.casefold()
            for t in ("thực phẩm", "đồ ăn", "đồ uống", "snack", "bánh kẹo", "nước giải khát", "nước ngọt")
        )
    )

    if is_food:
        if f" {brand_text} " not in f" {normalize_identity_text(name_text)} ":
            return False
        if NON_PACKAGED_FOOD_PATTERNS.search(name_text) or NON_PACKAGED_FOOD_PATTERNS.search(model_text):
            return False
        if "?" in name_text or "?" in model_text:
            return False
        folded_name = unicodedata.normalize("NFKD", name_text).encode("ascii", "ignore").decode("ascii").casefold()
        if any(pat in folded_name for pat in ("cach lam", "huong dan nau", "cong thuc", "quan an", "via he", "tu lam", "nau tai nha", "co tot khong", "nao ngon nhat")):
            return False
        return len(model_text) >= 3
    else:
        if not _is_valid_model_candidate(model_text, brand_text):
            return False
        return f" {brand_text} " in f" {normalize_identity_text(name_text)} "


def extract_prices_from_text(text: str) -> list[int]:
    """Extract price values in VND from natural text/snippets."""
    prices: list[int] = []
    tr_matches = re.finditer(
        r"(\d+)\s*(?:tr|trieu|triệu)\s*(\d+)?\b|(\d+[.,]\d+)\s*(?:tr|trieu|triệu)\b",
        text,
        re.IGNORECASE,
    )
    for m in tr_matches:
        if m.group(1):
            main_part = m.group(1)
            sub_part = m.group(2)
            val_str = f"{main_part}.{sub_part}" if sub_part else main_part
        else:
            val_str = m.group(3).replace(",", ".")
        try:
            val = float(val_str)
            prices.append(int(val * 1_000_000))
        except ValueError:
            pass

    k_matches = re.finditer(r"(\d+)\s*k\b", text, re.IGNORECASE)
    for m in k_matches:
        try:
            prices.append(int(m.group(1)) * 1_000)
        except ValueError:
            pass

    dot_matches = re.finditer(r"(\d{1,3}(?:\.\d{3})+)\s*(?:đ|vnd|dong|đồng)?", text, re.IGNORECASE)
    for m in dot_matches:
        num_str = m.group(1).replace(".", "")
        try:
            val = int(num_str)
            if 10_000 <= val <= 100_000_000:
                prices.append(val)
        except ValueError:
            pass

    return sorted(list(set(prices)))


def check_category_compatibility(text: str, target_category: str) -> tuple[bool, bool]:
    """Check whether text matches the target category and if it has conflicting keywords.

    Returns (is_match, is_conflict).
    When target_category is food, "bánh", "kẹo", "thực phẩm" are matching signals,
    not conflict signals. When target_category is tech, they remain conflict signals.
    """
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").casefold()
    cat_folded = unicodedata.normalize("NFKD", target_category).encode("ascii", "ignore").decode("ascii").casefold()

    is_food = any(
        t in cat_folded
        for t in (
            "thuc pham", "do an", "do uong", "an vat", "snack",
            "banh keo", "nuoc giai khat", "nuoc ngot", "tra", "dong chai", "dong goi"
        )
    )

    is_match = False
    is_conflict = False

    if is_food:
        if any(kw in folded for kw in FOOD_KEYWORDS) or cat_folded in folded:
            is_match = True
        if any(ck in folded for ck in FOOD_CONFLICT_KEYWORDS):
            is_conflict = True
    else:
        if any(kw in folded for kw in TECH_KEYWORDS) or cat_folded in folded:
            is_match = True
        if any(ck in folded for ck in TECH_CONFLICT_KEYWORDS):
            is_conflict = True

    return is_match, is_conflict
