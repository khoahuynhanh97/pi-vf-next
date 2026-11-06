from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from product_scout.daily_parser import DailyPromptParser, ParsedCriteria
from product_scout.trend_discovery.query_planner import QueryPlanner


PROMPT = (
    "tìm 10 sản phẩm ngành công nghệ hot trend tiktok/shopee trong phạm vi giá "
    "từ 200k-1tr vnd. Ưu tiên các sản phẩm tiện ích, độc lạ hoặc gia dụng thông minh bán chạy."
)


def test_combinatorial_planner_decomposes_prompt_into_diverse_queries():
    criteria = DailyPromptParser.parse_criteria(PROMPT)
    planner = QueryPlanner(enable_llm=False)

    run_day_1 = datetime(2026, 10, 13, 7, 0, tzinfo=timezone.utc)
    plan = planner.plan_queries(criteria, run_at=run_day_1, web_count=6, commerce_count=6)

    assert len(plan.web_queries) == 6
    assert len(plan.commerce_queries) == 6
    assert len(set(plan.web_queries)) == 6
    assert len(set(plan.commerce_queries)) == 6

    # Queries should target concrete product categories/brands rather than only generic "công nghệ"
    combined_text = " | ".join(plan.web_queries + plan.commerce_queries).lower()
    assert any(
        niche in combined_text
        for niche in (
            "tai nghe",
            "củ sạc",
            "bàn phím",
            "chuột",
            "quạt",
            "camera",
            "đèn",
            "loa",
            "máy cạo râu",
            "bàn chải điện",
            "sạc dự phòng",
            "ổ cắm",
        )
    )


def test_combinatorial_planner_rotates_queries_across_days():
    criteria = DailyPromptParser.parse_criteria(PROMPT)
    planner = QueryPlanner(enable_llm=False)

    day_1 = datetime(2026, 10, 13, 7, 0, tzinfo=timezone.utc)
    day_2 = datetime(2026, 10, 14, 7, 0, tzinfo=timezone.utc)

    plan_1 = planner.plan_queries(criteria, run_at=day_1, web_count=6, commerce_count=6)
    plan_2 = planner.plan_queries(criteria, run_at=day_2, web_count=6, commerce_count=6)

    # Different days must yield substantially different query sets so we don't hit stale SERPs
    assert plan_1.web_queries != plan_2.web_queries
    assert plan_1.commerce_queries != plan_2.commerce_queries


def test_planner_avoids_recently_selected_brands_and_models():
    criteria = DailyPromptParser.parse_criteria(PROMPT)
    recent_identities = [
        {"canonical_key": "huawei|gt 2 pro", "brand": "Huawei", "model": "GT 2 Pro", "name": "Đồng hồ Huawei GT 2 Pro"},
        {"canonical_key": "jbl|partybox 310", "brand": "JBL", "model": "PartyBox 310", "name": "Loa JBL PartyBox 310"},
        {"canonical_key": "xiaomi|robot vacuum e10", "brand": "Xiaomi", "model": "Robot Vacuum E10", "name": "Robot Xiaomi E10"},
    ]
    planner = QueryPlanner(enable_llm=False)
    run_day = datetime(2026, 10, 13, 7, 0, tzinfo=timezone.utc)

    plan = planner.plan_queries(
        criteria,
        run_at=run_day,
        recent_identities=recent_identities,
        web_count=6,
        commerce_count=6,
    )

    for q in plan.web_queries + plan.commerce_queries:
        q_lower = q.lower()
        assert "partybox 310" not in q_lower
        assert "gt 2 pro" not in q_lower
        assert "vacuum e10" not in q_lower


def test_llm_query_planner_uses_ai_variations_when_available():
    criteria = DailyPromptParser.parse_criteria(PROMPT)
    captured_prompts: list[str] = []

    def fake_llm_caller(system_prompt: str, user_prompt: str) -> dict:
        captured_prompts.append(user_prompt)
        return {
            "web_queries": [
                "review tai nghe bluetooth SoundPEATS Air4 Pro chính hãng giá dưới 1 triệu",
                "đánh giá bàn phím cơ không dây AULA F75 Mchose G75 hot trend",
                "máy cạo râu mini Enchen Mini 6 Flyco FS371 bán chạy",
                "camera wifi trong nhà Tapo C200 Ezviz C6N 360 độ",
            ],
            "commerce_queries": [
                "site:shopee.vn tai nghe bluetooth SoundPEATS QCY Edifier giá 300k 800k",
                "site:shopee.vn củ sạc nhanh GaN Cuktech Anker Ugreen 30W 65W",
                "site:tiktok.com quạt cầm tay mini Jisulife Lumias pin trâu",
                "site:shopee.vn đèn bàn thông minh học tập Mijia Baseus",
            ],
        }

    planner = QueryPlanner(enable_llm=True, llm_caller=fake_llm_caller)
    run_day = datetime(2026, 10, 13, 7, 0, tzinfo=timezone.utc)
    recent_identities = [
        {"canonical_key": "huawei|gt 2 pro", "brand": "Huawei", "model": "GT 2 Pro", "name": "Huawei GT 2 Pro"},
    ]

    plan = planner.plan_queries(
        criteria,
        run_at=run_day,
        recent_identities=recent_identities,
        web_count=6,
        commerce_count=6,
    )

    assert len(captured_prompts) == 1
    assert "Huawei GT 2 Pro" in captured_prompts[0] or "gt 2 pro" in captured_prompts[0].lower()
    # AI queries should be prioritized and topped up to requested count with combinatorial queries if needed
    assert len(plan.web_queries) == 6
    assert len(plan.commerce_queries) == 6
    assert "SoundPEATS Air4 Pro" in plan.web_queries[0]
    assert "site:shopee.vn" in plan.commerce_queries[0]
