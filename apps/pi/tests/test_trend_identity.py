from __future__ import annotations

from product_scout.trend_discovery.identity import (
    canonical_key,
    is_usable_product_identity,
    normalize_identity_text,
)


def test_canonical_key_prefers_brand_and_model():
    assert canonical_key("JISULIFE", "FA55", "Quạt Jisulife FA55 giảm 20%") == "jisulife|fa55"


def test_name_fallback_removes_commerce_noise():
    assert canonical_key("", "", "Tai nghe QCY T13 ANC - Sale 30% Chính hãng") == "tai nghe qcy t13 anc"


def test_nearby_models_remain_distinct():
    assert canonical_key("Jisulife", "FA55", "") != canonical_key("Jisulife", "FA55 Pro", "")


def test_unicode_normalization_is_stable():
    assert normalize_identity_text("Củ Sạc GaN") == normalize_identity_text("cu sac gan")


def test_canonical_key_brand_plus_name_when_model_missing():
    assert canonical_key("Baseus", "", "Cáp sạc nhanh 100W Freeship") == "baseus|cap sac nhanh 100w"


def test_canonical_key_removes_punctuation_and_excess_spaces():
    assert canonical_key("Anker", "A2663 / Nano II", "Anker Nano II 65W...") == "anker|a2663 nano ii"


def test_extract_product_identity_from_real_titles():
    from product_scout.trend_discovery.identity import extract_product_identity

    # Known brands and model extraction
    brand, model, name = extract_product_identity("Tai nghe Bluetooth Baseus Bowie M2+ chống ồn ANC")
    assert brand.lower() == "baseus"
    assert "bowie m2" in model.lower()
    assert "tai nghe bluetooth" in name.lower()

    brand, model, name = extract_product_identity("Chuột không dây Logitech MX Master 3S chính hãng")
    assert brand.lower() == "logitech"
    assert "mx master 3s" in model.lower()

    brand, model, name = extract_product_identity("Bàn phím cơ DareU EK87 Multi-Led")
    assert brand.lower() == "dareu"
    assert "ek87" in model.lower()

    brand, model, name = extract_product_identity("Củ sạc nhanh Ugreen Nexode 65W GaN")
    assert brand.lower() == "ugreen"
    assert "65w" in model.lower() or "nexode" in model.lower()

    brand, model, name = extract_product_identity("Robot hút bụi lau nhà Ecovacs Deebot T20 Omni")
    assert brand.lower() == "ecovacs"
    assert "t20" in model.lower() or "deebot" in model.lower()


def test_extract_product_identity_from_generic_articles():
    from product_scout.trend_discovery.identity import extract_product_identity

    # Generic articles should not produce false positive brand/model
    brand, model, name = extract_product_identity("12 video trends shaping 2026: social media video, AI editing")
    assert brand == ""
    assert model == ""


def test_marketplace_slug_is_cleaned_and_review_price_is_not_a_model():
    from product_scout.trend_discovery.identity import extract_product_identity

    brand, model, name = extract_product_identity(
        "shopee.vn/Đồng-hồ-thông-minh-HUAWEI-WATCH-GT-2-Pro---Pin-Liên...",
        "Cảm ơn bạn đã đánh giá 5 sao, giảm 150k",
    )
    assert brand.lower() == "huawei"
    assert normalize_identity_text(model) == "gt 2 pro"
    assert name.startswith("Đồng hồ thông minh HUAWEI")
    assert is_usable_product_identity(name, brand, model)

    brand, model, name = extract_product_identity(
        "shopee.vn/Đèn-cảm-ứng-chuyển-động-thông-minh-Baseus-Sunshine...",
        "Mua hàng được tặng sản phẩm trị giá 150k",
    )
    assert brand.lower() == "baseus"
    assert model == ""
    assert not is_usable_product_identity(name, brand, model)

    brand, model, name = extract_product_identity("8 cặp hot trends sẽ đến và đi trong năm 2026")
    assert brand == ""
    assert model == ""


def test_extract_prices_from_text():
    from product_scout.trend_discovery.identity import extract_prices_from_text

    assert extract_prices_from_text("Tai nghe giá chỉ 350k cực rẻ") == [350000]
    assert extract_prices_from_text("Giá từ 200.000đ đến 450.000đ") == [200000, 450000]
    assert extract_prices_from_text("Khuyến mãi 1.200.000 VND hôm nay") == [1200000]
    assert extract_prices_from_text("Combo 1tr5 tặng quà") == [1500000]
    assert extract_prices_from_text("Bài viết đánh giá không có giá") == []


def test_category_compatibility():
    from product_scout.trend_discovery.identity import check_category_compatibility

    match, conflict = check_category_compatibility("Chuột không dây Logitech bluetooth", "công nghệ")
    assert match is True
    assert conflict is False

    match, conflict = check_category_compatibility("6+ cách làm bánh thuẫn nở xốp thơm ngon đậm vị Tết", "công nghệ")
    assert match is False
    assert conflict is True


def test_extract_product_identity_rejects_listicle_and_event_pseudo_models():
    from product_scout.trend_discovery.identity import extract_product_identity

    for bad_title in (
        "Top 10 Thiết Bị Smart Home Philips Đáng Mua Nhất 2026",
        "Top 12 Loa Bluetooth JBL Công Suất Lớn Hay Nhất 2026",
        "Tổng hợp sản phẩm công nghệ Asus nổi bật tại CES 2026",
        "9 thiết bị công nghệ Asus đáng chú ý trong tháng 5-2026",
        "Robot hút bụi lau nhà Xiaomi | Sale 40% chính hãng",
    ):
        brand, model, name = extract_product_identity(bad_title)
        assert not is_usable_product_identity(name, brand, model), (
            f"Should reject pseudo-model in: {bad_title} (got {brand=} {model=})"
        )


def test_extract_product_identity_supports_diverse_emerging_brands_and_models():
    from product_scout.trend_discovery.identity import extract_product_identity

    cases = [
        ("Tai nghe Bluetooth SoundPEATS Air 6 HS Giá Tốt", "soundpeats", "air 6 hs"),
        ("Máy cạo râu mini du lịch Enchen Mini 6 chống nước IPX7", "enchen", "mini 6"),
        ("Pin sạc dự phòng Cuktech PB100P 10000mAh 120W", "cuktech", "pb100p"),
        ("Chuột gaming không dây Attack Shark X3 siêu nhẹ", "attack shark", "x3"),
        ("Camera Wifi trong nhà Tapo C210 2K xoay 360", "tapo", "c210"),
        ("Bàn phím cơ không dây Xinmeng M75 Gasket Mount", "xinmeng", "m75"),
        ("Loa Bluetooth Tronsmart T7 Mini chống nước IPX7", "tronsmart", "t7 mini"),
        ("Đồng hồ thông minh Haylou Solar Lite màn hình 1.38 inch", "haylou", "solar lite"),
    ]
    for title, exp_brand, exp_model in cases:
        brand, model, name = extract_product_identity(title)
        assert normalize_identity_text(brand) == exp_brand, f"Brand mismatch for {title}: {brand}"
        assert normalize_identity_text(model) == exp_model, f"Model mismatch for {title}: {model}"
        assert is_usable_product_identity(name, brand, model)
