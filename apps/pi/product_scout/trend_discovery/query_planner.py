"""AI & Combinatorial Search Query Planner for Fresh Trend Discovery.

Decomposes a natural-language user prompt (ParsedCriteria) into diverse, rotating,
history-aware search query variations so daily runs discover fresh products across
different sub-niches and brands instead of repeating stale SERPs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from product_scout.daily_parser import ParsedCriteria, is_food_category

from .identity import normalize_identity_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NicheBlueprint:
    niche_id: str
    product_terms: tuple[str, ...]
    brand_groups: tuple[tuple[str, ...], ...]
    model_seeds: tuple[str, ...]


# 12 diverse tech & smart-living sub-verticals in Vietnam (200k - 1M VND sweet spot)
NICHE_BLUEPRINTS: tuple[NicheBlueprint, ...] = (
    NicheBlueprint(
        niche_id="audio_tws",
        product_terms=(
            "tai nghe bluetooth",
            "tai nghe không dây ANC",
            "tai nghe bluetooth gaming",
        ),
        brand_groups=(
            ("SoundPEATS", "Edifier", "QCY"),
            ("Soundcore", "Havit", "EarFun"),
            ("Moondrop", "Baseus", "Lenovo"),
        ),
        model_seeds=(
            "SoundPEATS Air4 Lite",
            "SoundPEATS T3 Pro",
            "Edifier X5 Pro",
            "Soundcore R50i",
            "Havit TW969",
            "Baseus MA10",
        ),
    ),
    NicheBlueprint(
        niche_id="fast_charging",
        product_terms=(
            "củ sạc nhanh GaN",
            "bộ sạc nhanh Type-C",
            "trạm sạc nhanh GaN",
        ),
        brand_groups=(
            ("Anker", "Cuktech", "Ugreen"),
            ("Velasboost", "Pisen", "Vention"),
            ("Baseus", "ZMI", "Choetech"),
        ),
        model_seeds=(
            "Anker Zolo 30W",
            "Cuktech 65W",
            "Ugreen Nexode 30W",
            "Velasboost M1",
            "Pisen Quick 30W",
            "Vention GaN 65W",
        ),
    ),
    NicheBlueprint(
        niche_id="power_bank",
        product_terms=(
            "pin sạc dự phòng",
            "sạc dự phòng MagSafe",
            "pin dự phòng sạc nhanh",
        ),
        brand_groups=(
            ("Cuktech", "Anker", "Ugreen"),
            ("Xiaomi", "Remax", "Hoco"),
            ("Romoss", "Velasboost", "Pisen"),
        ),
        model_seeds=(
            "Cuktech PB100P",
            "Anker Zolo 10000mAh",
            "Ugreen PB561",
            "Xiaomi Pocket 10000mAh",
            "Remax RPP-625",
            "Hoco J101A",
        ),
    ),
    NicheBlueprint(
        niche_id="desk_keyboard",
        product_terms=(
            "bàn phím cơ không dây",
            "bàn phím cơ gasket mount",
            "bàn phím bluetooth",
        ),
        brand_groups=(
            ("Xinmeng", "Ajazz", "Machenike"),
            ("Akko", "E-Dra", "Rapoo"),
            ("AULA", "DareU", "Keychron"),
        ),
        model_seeds=(
            "Xinmeng M75",
            "Ajazz AK820",
            "Machenike K500",
            "Akko 3087 RF",
            "E-Dra EK375",
            "Rapoo V500Pro",
        ),
    ),
    NicheBlueprint(
        niche_id="desk_mouse",
        product_terms=(
            "chuột không dây gaming",
            "chuột bluetooth silent",
            "chuột không dây công thái học",
        ),
        brand_groups=(
            ("VXE", "Attack Shark", "Rapoo"),
            ("Logitech", "Ugreen", "Havit"),
            ("DareU", "Razer", "E-Dra"),
        ),
        model_seeds=(
            "VXE R1 SE",
            "Attack Shark X3",
            "Logitech M350s",
            "Rapoo MT760",
            "Ugreen MU006",
            "Havit MS955",
        ),
    ),
    NicheBlueprint(
        niche_id="smart_cooling",
        product_terms=(
            "quạt cầm tay mini",
            "quạt tích điện để bàn",
            "quạt mini phản lực",
        ),
        brand_groups=(
            ("Jisulife", "Lumias", "Yoobao"),
            ("Orashare", "Deerma", "Simplus"),
            ("Baseus", "Remax", "Xiaomi"),
        ),
        model_seeds=(
            "Jisulife FA53",
            "Jisulife FA42",
            "Lumias F08",
            "Yoobao F3",
            "Deerma FD200",
            "Simplus QFJH003",
        ),
    ),
    NicheBlueprint(
        niche_id="smart_home_cam",
        product_terms=(
            "camera wifi trong nhà",
            "camera an ninh 360",
            "ổ cắm thông minh wifi",
        ),
        brand_groups=(
            ("Tapo", "Ezviz", "Imou"),
            ("Xiaomi", "Aqara", "Sonoff"),
            ("TP-Link", "Tuya", "D-Link"),
        ),
        model_seeds=(
            "Tapo C210",
            "Tapo C200",
            "Ezviz C6N",
            "Xiaomi C301",
            "Aqara E1",
            "Sonoff Mini R4",
        ),
    ),
    NicheBlueprint(
        niche_id="smart_lighting",
        product_terms=(
            "đèn treo màn hình",
            "đèn bàn thông minh",
            "đèn cảm ứng thông minh",
        ),
        brand_groups=(
            ("Yeelight", "Mijia", "Baseus"),
            ("Philips", "Xiaomi", "Ugreen"),
            ("Remax", "Tapo", "Deerma"),
        ),
        model_seeds=(
            "Baseus i-wok 2",
            "Yeelight Screen Bar",
            "Mijia MJTD01SYL",
            "Philips WiZ",
            "Tapo L530E",
            "Xiaomi MJD02YL",
        ),
    ),
    NicheBlueprint(
        niche_id="personal_care",
        product_terms=(
            "máy cạo râu mini",
            "bàn chải điện thông minh",
            "máy tăm nước cầm tay",
        ),
        brand_groups=(
            ("Enchen", "ShowSee", "Flyco"),
            ("Usmile", "Oral-B", "LocknLock"),
            ("Philips", "Xiaomi", "Lumias"),
        ),
        model_seeds=(
            "Enchen Mini 6",
            "ShowSee F305",
            "Flyco FS889",
            "Usmile Y1S",
            "Oral-B Pro 500",
            "LocknLock ENR156",
        ),
    ),
    NicheBlueprint(
        niche_id="bluetooth_speaker",
        product_terms=(
            "loa bluetooth mini",
            "loa bluetooth chống nước",
            "loa không dây để bàn",
        ),
        brand_groups=(
            ("Tronsmart", "Edifier", "Tribit"),
            ("JBL", "Divoom", "Gravastar"),
            ("Havit", "Soundcore", "Sony"),
        ),
        model_seeds=(
            "Tronsmart T7 Mini",
            "Edifier MP85",
            "JBL Go 3",
            "JBL Clip 4",
            "Soundcore Select 4 Go",
            "Havit SK802",
        ),
    ),
    NicheBlueprint(
        niche_id="smart_wearables",
        product_terms=(
            "vòng đeo tay thông minh",
            "đồng hồ thông minh",
            "smartwatch thể thao",
        ),
        brand_groups=(
            ("Haylou", "Kieslect", "Amazfit"),
            ("Xiaomi", "Redmi", "SoundPEATS"),
            ("Huawei", "Realme", "Oppo"),
        ),
        model_seeds=(
            "Haylou Solar Lite",
            "Kieslect Kr Pro",
            "Amazfit Bip 5",
            "Xiaomi Smart Band 8",
            "Redmi Watch 4",
            "SoundPEATS Watch 4",
        ),
    ),
    NicheBlueprint(
        niche_id="hub_storage_mounts",
        product_terms=(
            "hub chuyển đổi Type-C",
            "ổ cứng SSD di động",
            "giá đỡ điện thoại MagSafe",
        ),
        brand_groups=(
            ("Orico", "Vention", "Ugreen"),
            ("SanDisk", "Kingston", "Lexar"),
            ("Ulanzi", "Moft", "Baseus"),
        ),
        model_seeds=(
            "Orico PW11",
            "Vention Type-C Hub",
            "Ugreen CM512",
            "Kingston Exodia M",
            "SanDisk CZ73",
            "Ulanzi MT-44",
        ),
    ),
)


# 11 diverse packaged food, snack & beverage sub-verticals in Vietnam
FOOD_NICHE_BLUEPRINTS: tuple[NicheBlueprint, ...] = (
    NicheBlueprint(
        niche_id="street_packaged_snacks",
        product_terms=(
            "bánh tráng phơi sương đóng gói",
            "bắp rang bơ đóng hộp",
            "bỏng ngô phô mai",
            "khoai tây chiên giòn đóng gói",
            "nui sấy bò lá chanh giòn",
        ),
        brand_groups=(
            ("Oishi", "Lay's", "Orion"),
            ("Tân Tân", "Vinamit", "Bà Tư"),
            ("MixiFood", "Chú Tễu", "Gia Vị Cố Đô"),
        ),
        model_seeds=(
            "Oishi bắp rang bơ caramel 120g",
            "Lay's Stax khoai tây chiên 100g",
            "Tân Tân nui sấy giòn cay 150g",
            "MixiFood khô gà lá chanh hũ 250g",
            "Chú Tễu bánh tráng phơi sương sốt bơ",
        ),
    ),
    NicheBlueprint(
        niche_id="snack_potato_chips",
        product_terms=(
            "snack khoai tây",
            "khoai tây sấy giòn",
            "snack khoai tây lon",
        ),
        brand_groups=(
            ("Lay's", "Oishi", "Orion"),
            ("Pringles", "Swing", "Karamucho"),
            ("Koikeya", "Slide", "Lorenz"),
        ),
        model_seeds=(
            "Lay's Stax vị tự nhiên 100g",
            "Lay's Wavy sườn nướng BBQ 95g",
            "Swing bít tết tiêu đen 90g",
            "Pringles vị truyền thống 107g",
            "Oishi Pinattsu vị mực cay 85g",
            "Karamucho vị cay đặc biệt 80g",
        ),
    ),
    NicheBlueprint(
        niche_id="snack_seaweed_rice_crackers",
        product_terms=(
            "snack rong biển giòn",
            "bánh gạo nướng",
            "snack rong biển cuộn",
        ),
        brand_groups=(
            ("Tao Kae Noi", "Bibigo", "Orion"),
            ("One One", "Ichi", "Richy"),
            ("Gumiho", "Kobuk", "Koreno"),
        ),
        model_seeds=(
            "Tao Kae Noi Big Roll vị mực cay 32.4g",
            "Bibigo rong biển sấy truyền thống 5g",
            "Orion bánh gạo An vị tảo biển 111.3g",
            "One One vị phô mai ngô 150g",
            "Ichi vị mật ong shoyu 180g",
            "Richy bánh gạo Jinju bò nướng 135g",
        ),
    ),
    NicheBlueprint(
        niche_id="snack_confectionery_biscuits",
        product_terms=(
            "bánh quy kẹp kem",
            "bánh xốp phô mai",
            "bánh que giòn",
        ),
        brand_groups=(
            ("Oreo", "Gery", "Ritz"),
            ("Nabati", "Cosy", "Danisa"),
            ("Lotte", "Meiji", "Pocky"),
        ),
        model_seeds=(
            "Oreo kẹp kem vani 248.4g",
            "Gery Cheese Crackers phô mai 200g",
            "Nabati Richeese xốp phô mai 145g",
            "Pocky que vị dâu 38g",
            "Cosy Marie bánh quy mè 288g",
            "Danisa bánh quy bơ truyền thống 200g",
        ),
    ),
    NicheBlueprint(
        niche_id="soft_cakes_pies",
        product_terms=(
            "bánh chocopie",
            "bánh bông lan mềm",
            "bánh trứng kem bơ",
        ),
        brand_groups=(
            ("Orion", "Lotte", "Phạm Nguyên"),
            ("Kinh Đô", "Custas", "Bảo Ngọc"),
            ("Solite", "Hura", "Maruto"),
        ),
        model_seeds=(
            "Orion Chocopie vị truyền thống 360g",
            "Custas bánh trứng kem bơ 141g",
            "Lotte Chocopie cacao phô mai 336g",
            "Solite bánh bông lan cuộn kem dâu 360g",
            "Phạm Nguyên Choco PN dâu 264g",
            "Kinh Đô bánh tươi hoa cúc 150g",
        ),
    ),
    NicheBlueprint(
        niche_id="gummies_candy",
        product_terms=(
            "kẹo dẻo trái cây",
            "kẹo ngậm không đường",
            "kẹo mềm dâu sữa",
        ),
        brand_groups=(
            ("Chupa Chups", "Alpenliebe", "Haribo"),
            ("Play More", "Sugus", "Hải Hà"),
            ("Mentos", "Oishi", "UHA"),
        ),
        model_seeds=(
            "Haribo Goldbears kẹo dẻo gấu 80g",
            "Play More kẹo ngậm dưa hấu 22g",
            "Alpenliebe kẹo mềm vị dâu sữa 100g",
            "Chupa Chups kẹo dẻo Sour Bites 56g",
            "Mentos kẹo ngậm Clean Breath 35g",
            "Sugus kẹo mềm nho cam dâu 100g",
        ),
    ),
    NicheBlueprint(
        niche_id="bottled_teas_infusions",
        product_terms=(
            "trà xanh đóng chai",
            "trà ô long đóng chai",
            "trà sữa đóng lon",
        ),
        brand_groups=(
            ("C2", "Không Độ", "TEA+ Plus"),
            ("Kirin", "Fuzetea", "Lipton"),
            ("Yeo's", "Pokka", "Mori"),
        ),
        model_seeds=(
            "TEA+ Plus trà ô long không đường 455ml",
            "C2 trà xanh hương chanh 455ml",
            "Không Độ trà xanh vị chanh 455ml",
            "Kirin Latte trà sữa đóng chai 345ml",
            "Fuzetea trà đào hạt chia 450ml",
            "Lipton trà đen vị chanh 455ml",
        ),
    ),
    NicheBlueprint(
        niche_id="sparkling_energy_drinks",
        product_terms=(
            "nước tăng lực đóng lon",
            "nước có ga đóng lon",
            "nước giải khát đóng chai",
        ),
        brand_groups=(
            ("Red Bull", "Monster", "Sting"),
            ("Warrior", "Wake-up 247", "Number 1"),
            ("Coca-Cola", "Pepsi", "7Up"),
        ),
        model_seeds=(
            "Sting dâu tây đỏ lon 320ml",
            "Red Bull lon vàng truyền thống 250ml",
            "Monster Energy lon xanh Ultra 355ml",
            "Warrior vị dâu lon 325ml",
            "Pepsi không calo lon 320ml",
            "Coca-Cola nguyên bản lon 320ml",
        ),
    ),
    NicheBlueprint(
        niche_id="ready_meats_sausages",
        product_terms=(
            "xúc xích tiệt trùng ăn liền",
            "bò khô miếng đóng gói",
            "khô gà lá chanh hũ",
        ),
        brand_groups=(
            ("CP", "Vissan", "Ba Huân"),
            ("Heo Cao Bồi", "G Kitchen", "Xuxifarm"),
            ("Ponnie", "Cầu Tre", "CJ Cầu Tre"),
        ),
        model_seeds=(
            "Vissan xúc xích bò tiệt trùng gói 175g",
            "Heo Cao Bồi xúc xích lắc phô mai bắp 60g",
            "CP xúc xích Vealz phô mai 200g",
            "G Kitchen khô heo cháy tỏi 100g",
            "Ponnie xúc xích thịt heo thanh 175g",
            "Vissan khô bò vị cay truyền thống 100g",
        ),
    ),
    NicheBlueprint(
        niche_id="nuts_dried_fruits",
        product_terms=(
            "hạt điều rang muối",
            "đậu phộng nước cốt dừa",
            "trái cây sấy dẻo",
        ),
        brand_groups=(
            ("Vinamit", "Bà Tư Bình Phước", "Tân Tân"),
            ("L'angfarm", "Dan D Pak", "Việt San"),
            ("Đồng Xanh", "Nutty", "Savoury"),
        ),
        model_seeds=(
            "Tân Tân đậu phộng nước cốt dừa 150g",
            "Vinamit mít sấy giòn gói 100g",
            "Bà Tư hạt điều rang muối 500g",
            "L'angfarm xoài sấy dẻo 150g",
            "Dan D Pak hạt hạnh nhân bơ 200g",
            "Nutty hạt óc chó tách vỏ 200g",
        ),
    ),
    NicheBlueprint(
        niche_id="instant_noodles_meals",
        product_terms=(
            "mì gói ăn liền",
            "mì xào khô đóng gói",
            "mì cay đóng gói",
        ),
        brand_groups=(
            ("Hảo Hảo", "Acecook", "Omachi"),
            ("Koreno", "Samyang", "Indomie"),
            ("Cung Đình", "Miliket", "Vifon"),
        ),
        model_seeds=(
            "Hảo Hảo tôm chua cay gói 75g",
            "Omachi xốt bò hầm gói 80g",
            "Indomie Mi Goreng sườn nướng 85g",
            "Samyang mì cay gà Buldak 140g",
            "Koreno mì cay Jumbo bò 100g",
            "Cung Đình sườn hầm ngũ vị 80g",
        ),
    ),
)


@dataclass
class QueryPlan:
    web_queries: list[str] = field(default_factory=list)
    commerce_queries: list[str] = field(default_factory=list)
    source: str = "combinatorial"


class QueryPlanner:
    """Decomposes user criteria into diverse, history-aware search query variations."""

    def __init__(
        self,
        *,
        enable_llm: bool | None = None,
        llm_caller: Callable[[str, str], dict[str, Any] | None] | None = None,
    ):
        if enable_llm is None:
            enable_llm = os.environ.get("PRODUCT_SCOUT_DISABLE_LLM_QUERY_PLANNER") != "true"
        self.enable_llm = enable_llm
        self._llm_caller = llm_caller
        self._lock = threading.Lock()
        self._cache: dict[tuple[Any, ...], QueryPlan] = {}

    def plan_queries(
        self,
        criteria: ParsedCriteria,
        *,
        run_at: datetime,
        recent_identities: list[dict[str, str]] | None = None,
        web_count: int = 6,
        commerce_count: int = 6,
    ) -> QueryPlan:
        recent_identities = recent_identities or []
        cache_key = (
            criteria.raw_prompt,
            run_at.date().isoformat(),
            web_count,
            commerce_count,
            tuple(sorted(item.get("canonical_key", "") for item in recent_identities)),
        )
        with self._lock:
            if cache_key in self._cache:
                return self._cache[cache_key]
            plan = self._compute_plan(
                criteria,
                run_at=run_at,
                recent_identities=recent_identities,
                web_count=web_count,
                commerce_count=commerce_count,
            )
            self._cache[cache_key] = plan
            return plan

    def _compute_plan(
        self,
        criteria: ParsedCriteria,
        *,
        run_at: datetime,
        recent_identities: list[dict[str, str]],
        web_count: int,
        commerce_count: int,
    ) -> QueryPlan:
        excluded_models = self._extract_excluded_models(recent_identities)
        excluded_brands = self._extract_overused_brands(recent_identities)

        combinatorial = self._build_combinatorial_queries(
            criteria,
            run_at=run_at,
            excluded_models=excluded_models,
            excluded_brands=excluded_brands,
            web_count=max(web_count, 8),
            commerce_count=max(commerce_count, 8),
        )

        if not self.enable_llm:
            return QueryPlan(
                web_queries=combinatorial.web_queries[:web_count],
                commerce_queries=combinatorial.commerce_queries[:commerce_count],
                source="combinatorial",
            )

        ai_data = self._try_llm_variations(
            criteria,
            run_at=run_at,
            recent_identities=recent_identities,
            web_count=web_count,
            commerce_count=commerce_count,
        )
        if not ai_data:
            return QueryPlan(
                web_queries=combinatorial.web_queries[:web_count],
                commerce_queries=combinatorial.commerce_queries[:commerce_count],
                source="combinatorial",
            )

        raw_web = [str(q).strip() for q in ai_data.get("web_queries", []) if str(q).strip()]
        raw_comm = [str(q).strip() for q in ai_data.get("commerce_queries", []) if str(q).strip()]

        # When using default live LLM, blend half AI queries + half high-yield combinatorial queries
        # so search engines always return fast, reliable hits alongside AI exploration.
        if self._llm_caller is None:
            ai_web_cap = max(1, web_count // 2)
            ai_comm_cap = max(1, commerce_count // 2)
            raw_web = raw_web[:ai_web_cap]
            raw_comm = raw_comm[:ai_comm_cap]

        web_final = self._merge_and_filter_queries(
            raw_web,
            combinatorial.web_queries,
            excluded_models=excluded_models,
            target_count=web_count,
        )
        comm_final = self._merge_and_filter_queries(
            raw_comm,
            combinatorial.commerce_queries,
            excluded_models=excluded_models,
            target_count=commerce_count,
            require_site_prefix=True,
        )

        return QueryPlan(
            web_queries=web_final,
            commerce_queries=comm_final,
            source="ai_hybrid" if (raw_web or raw_comm) else "combinatorial",
        )

    @staticmethod
    def _extract_excluded_models(recent_identities: list[dict[str, str]]) -> set[str]:
        excluded: set[str] = set()
        for item in recent_identities:
            model = (item.get("model") or "").strip()
            brand = (item.get("brand") or "").strip()
            ck = (item.get("canonical_key") or "").strip()
            if model and len(model) >= 2:
                excluded.add(model.casefold())
                excluded.add(normalize_identity_text(model))
            if brand and model:
                excluded.add(f"{brand} {model}".casefold())
            if "|" in ck:
                _, m_part = ck.split("|", 1)
                if m_part and len(m_part) >= 2:
                    excluded.add(m_part.casefold())
        return {m for m in excluded if m}

    @staticmethod
    def _extract_overused_brands(recent_identities: list[dict[str, str]]) -> set[str]:
        counts: dict[str, int] = {}
        for item in recent_identities:
            brand = normalize_identity_text(item.get("brand") or "")
            if brand:
                counts[brand] = counts.get(brand, 0) + 1
        return {b for b, cnt in counts.items() if cnt >= 2}

    @classmethod
    def _query_contains_excluded(cls, query: str, excluded_models: set[str]) -> bool:
        if not excluded_models:
            return False
        q_low = query.casefold()
        q_norm = normalize_identity_text(query)
        for mod in excluded_models:
            if not mod:
                continue
            if mod in q_low or (len(mod) >= 3 and mod in q_norm):
                return True
        return False

    @classmethod
    def _merge_and_filter_queries(
        cls,
        primary: list[str],
        fallback: list[str],
        *,
        excluded_models: set[str],
        target_count: int,
        require_site_prefix: bool = False,
    ) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()

        for candidate in primary + fallback:
            q = candidate.strip()
            if not q:
                continue
            if require_site_prefix and not q.lower().startswith("site:"):
                q = f"site:shopee.vn {q}"
            if cls._query_contains_excluded(q, excluded_models):
                continue
            norm = normalize_identity_text(q)
            if norm in seen:
                continue
            seen.add(norm)
            merged.append(q)
            if len(merged) >= target_count:
                break

        return merged[:target_count]

    def _build_combinatorial_queries(
        self,
        criteria: ParsedCriteria,
        *,
        run_at: datetime,
        excluded_models: set[str],
        excluded_brands: set[str],
        web_count: int,
        commerce_count: int,
    ) -> QueryPlan:
        day_ordinal = run_at.date().toordinal()
        prompt_seed = int(hashlib.md5(criteria.raw_prompt.encode("utf-8")).hexdigest()[:8], 16)
        is_food = is_food_category(criteria.category)
        blueprints = FOOD_NICHE_BLUEPRINTS if is_food else NICHE_BLUEPRINTS
        total_niches = len(blueprints)

        stride = 3 if is_food else 5
        start_idx = (day_ordinal * 3 + prompt_seed) % total_niches

        prompt_low = criteria.raw_prompt.casefold()

        def _blueprint_match_score(bp: NicheBlueprint) -> int:
            score = 0
            for term in bp.product_terms:
                t_low = term.casefold()
                if t_low in prompt_low:
                    score += 5
                else:
                    for word in t_low.split():
                        if len(word) >= 4 and word in prompt_low:
                            score += 2
            for m in bp.model_seeds:
                m_low = m.casefold()
                for word in m_low.split():
                    if len(word) >= 4 and word in prompt_low:
                        score += 1
            return score

        scored_niches = [(_blueprint_match_score(bp), i, bp) for i, bp in enumerate(blueprints)]
        scored_niches.sort(key=lambda item: (-item[0], (item[1] - start_idx) % total_niches))
        ordered_niches = [item[2] for item in scored_niches]

        if is_food:
            price_hooks = (
                "bán chạy",
                "chính hãng",
                "ăn vặt ngon",
                "giá rẻ",
                "đóng gói",
            )
        elif criteria.price_max <= 1_000_000:
            price_hooks = (
                "dưới 1 triệu",
                "dưới 500k",
                "giá rẻ",
                "chính hãng",
            )
        else:
            price_hooks = (
                "chính hãng",
                "đáng mua",
                "bán chạy",
                "review",
            )

        web_queries: list[str] = []
        commerce_queries: list[str] = []

        for idx, blueprint in enumerate(ordered_niches):
            term_idx = (day_ordinal + idx) % len(blueprint.product_terms)
            product_term = blueprint.product_terms[term_idx]

            bg_idx = (day_ordinal + idx * 2) % len(blueprint.brand_groups)
            raw_brands = list(blueprint.brand_groups[bg_idx])
            fresh_brands = [
                b for b in raw_brands
                if normalize_identity_text(b) not in excluded_brands
            ] or raw_brands
            brand_phrase = " ".join(fresh_brands[:3])

            available_models = [
                m for m in blueprint.model_seeds
                if not self._query_contains_excluded(m, excluded_models)
            ]
            if len(available_models) >= 2:
                m1 = available_models[(day_ordinal + idx) % len(available_models)]
                m2 = available_models[(day_ordinal + idx + 2) % len(available_models)]
                model_pair = f"{m1} {m2}" if m1 != m2 else m1
            elif available_models:
                model_pair = available_models[0]
            else:
                model_pair = brand_phrase

            price_hook = price_hooks[(day_ordinal + idx) % len(price_hooks)]

            # Keep queries concise (6-8 words) combining multiple brands/models for maximum diversity
            if idx % 2 == 0:
                wq = f"review {product_term} {brand_phrase} {price_hook}"
            else:
                wq = f"đánh giá {product_term} {model_pair}"

            if not self._query_contains_excluded(wq, excluded_models):
                web_queries.append(wq)

            cq = f"site:shopee.vn {product_term} {brand_phrase}"
            if not self._query_contains_excluded(cq, excluded_models):
                commerce_queries.append(cq)

        return QueryPlan(
            web_queries=web_queries[:web_count],
            commerce_queries=commerce_queries[:commerce_count],
            source="combinatorial",
        )

    def _try_llm_variations(
        self,
        criteria: ParsedCriteria,
        *,
        run_at: datetime,
        recent_identities: list[dict[str, str]],
        web_count: int,
        commerce_count: int,
    ) -> dict[str, Any] | None:
        excluded_summary = ", ".join(
            f"{item.get('brand', '')} {item.get('model', '')}".strip() or item.get("name", "")
            for item in recent_identities[:20]
            if item.get("brand") or item.get("model") or item.get("name")
        )

        day_str = run_at.date().isoformat()
        day_ordinal = run_at.date().toordinal()
        is_food = is_food_category(criteria.category)
        blueprints = FOOD_NICHE_BLUEPRINTS if is_food else NICHE_BLUEPRINTS
        focus_niches = [
            blueprints[(day_ordinal + i * 5) % len(blueprints)].product_terms[0]
            for i in range(min(6, len(blueprints)))
        ]

        if is_food:
            system_prompt = (
                "Bạn là chuyên gia tìm kiếm sản phẩm đồ ăn, đồ uống, snack ăn vặt đóng gói trên E-Commerce Việt Nam (Shopee, TikTok Shop). "
                "Nhiệm vụ: Cắt ghép yêu cầu của người dùng thành các câu truy vấn tìm kiếm NGẮN GỌN (4-7 từ), "
                "100% TIẾNG VIỆT, kết hợp loại đồ ăn/uống + 2 thương hiệu/vị cụ thể. "
                "TUYỆT ĐỐI KHÔNG tìm sản phẩm công nghệ/điện tử hay món ăn không có bao bì. "
                "Chỉ trả về duy nhất 1 JSON object."
            )
            price_phrase = (
                f"Khoảng giá: {criteria.price_min:,} - {criteria.price_max:,} VND"
                if (criteria.price_min or criteria.price_max)
                else "Khoảng giá: theo giá thị trường"
            )
            user_prompt = (
                f"Ngày chạy: {day_str}\n"
                f"Yêu cầu gốc: {criteria.raw_prompt}\n"
                f"Ngành hàng: {criteria.category} | {price_phrase}\n"
                f"Các ngách ưu tiên hôm nay: {'; '.join(focus_niches)}\n"
                f"CÁC SẢN PHẨM CŨ CẦN TRÁNH LẶP LẠI: {excluded_summary or 'Không có'}\n\n"
                f"Hãy tạo (BẮT BUỘC NGẮN GỌN 4-7 TỪ BẰNG TIẾNG VIỆT, ví dụ: 'review snack khoai tây Lay\'s Oishi' "
                f"hoặc 'site:shopee.vn trà ô long TEA+ C2'):\n"
                f"- `web_queries`: {web_count} câu truy vấn tiếng Việt ngắn gọn.\n"
                f"- `commerce_queries`: {commerce_count} câu truy vấn ngắn gọn bắt đầu bằng `site:shopee.vn `.\n"
                f"TUYỆT ĐỐI KHÔNG nhắc lại các sản phẩm/thương hiệu trong danh sách cần tránh.\n"
                'Định dạng JSON: {"web_queries": ["..."], "commerce_queries": ["..."]}'
            )
        else:
            system_prompt = (
                "Bạn là chuyên gia tìm kiếm sản phẩm E-Commerce Việt Nam (Shopee, TikTok Shop). "
                "Nhiệm vụ: Cắt ghép yêu cầu của người dùng thành các câu truy vấn tìm kiếm NGẮN GỌN (4-7 từ), "
                "100% TIẾNG VIỆT, kết hợp loại sản phẩm + 2 thương hiệu/model cụ thể. "
                "Chỉ trả về duy nhất 1 JSON object."
            )
            user_prompt = (
                f"Ngày chạy: {day_str}\n"
                f"Yêu cầu gốc: {criteria.raw_prompt}\n"
                f"Ngành hàng: {criteria.category} | Khoảng giá: {criteria.price_min:,} - {criteria.price_max:,} VND\n"
                f"Các ngách ưu tiên hôm nay: {'; '.join(focus_niches)}\n"
                f"CÁC SẢN PHẨM CŨ CẦN TRÁNH LẶP LẠI: {excluded_summary or 'Không có'}\n\n"
                f"Hãy tạo (BẮT BUỘC NGẮN GỌN 4-7 TỪ BẰNG TIẾNG VIỆT, ví dụ: 'review tai nghe bluetooth SoundPEATS Edifier' "
                f"hoặc 'site:shopee.vn củ sạc nhanh Anker Cuktech'):\n"
                f"- `web_queries`: {web_count} câu truy vấn tiếng Việt ngắn gọn.\n"
                f"- `commerce_queries`: {commerce_count} câu truy vấn ngắn gọn bắt đầu bằng `site:shopee.vn `.\n"
                f"TUYỆT ĐỐI KHÔNG nhắc lại các sản phẩm/model trong danh sách cần tránh.\n"
                'Định dạng JSON: {"web_queries": ["..."], "commerce_queries": ["..."]}'
            )

        caller = self._llm_caller or self._call_default_llm
        try:
            return caller(system_prompt, user_prompt)
        except Exception as exc:
            logger.debug("LLM query variation fallback to combinatorial: %s", exc)
            return None

    @staticmethod
    def _call_default_llm(system_prompt: str, user_prompt: str) -> dict[str, Any] | None:
        base_url = (os.environ.get("NINEROUTER_BASE_URL") or "").strip().rstrip("/")
        api_key = (
            os.environ.get("NINEROUTER_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ("sk-9router" if base_url else "")
        ).strip()
        if not api_key and not base_url:
            return None

        if base_url:
            endpoint = f"{base_url}/chat/completions"
            model = (os.environ.get("NINEROUTER_MODEL") or "").strip() or "fast_combo"
        else:
            endpoint = "https://api.openai.com/v1/chat/completions"
            model = "gpt-4o-mini"

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": model,
            "stream": False,
            "temperature": 0.7,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        resp = httpx.post(endpoint, headers=headers, json=payload, timeout=5.0)
        if resp.status_code != 200:
            return None

        raw_text = resp.text.strip()
        content_text = ""

        lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
        for ln in reversed(lines):
            candidate_line = ln.removeprefix("data:").strip()
            if not candidate_line.startswith("{"):
                continue
            try:
                obj = json.loads(candidate_line)
                choices = obj.get("choices") or []
                if choices:
                    msg = choices[0].get("message") or {}
                    if msg.get("content"):
                        content_text = str(msg["content"])
                        break
            except ValueError:
                continue

        if not content_text:
            return None

        match = re.search(r"\{.*\}", content_text, re.DOTALL)
        if not match:
            return None
        parsed = json.loads(match.group(0))
        if isinstance(parsed, dict):
            return parsed
        return None
