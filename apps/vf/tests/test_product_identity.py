"""Tests for truthful product identity normalization and grounded claims."""

import pytest
from pathlib import Path
from PIL import Image

from video_factory.qc.normalization import (
    normalize_product_identity,
)


def test_normalize_product_identity_clean():
    rp = {
        "resource_pack": {
            "resource_pack_id": "pack_snap_123456",
            "brand": "Jisulife",
            "model": "Life 7",
            "product_name": "Quạt cầm tay Jisulife Life 7",
            "category": "Quạt mini",
            "verified_selling_points": ["Pin 5000mAh", "5 cấp độ gió"],
            "assets": [
                {"asset_id": "a1", "local_path": "/tmp/a1.jpg"},
                {"asset_id": "a2", "local_path": "/tmp/a2.jpg"},
            ],
        }
    }
    src_ctx = {
        "brand": "Jisulife",
        "model": "Life 7",
    }

    norm = normalize_product_identity(rp, source_context=src_ctx)
    assert norm.brand == "Jisulife"
    assert norm.model == "Life 7"
    assert norm.display_name == "Quạt cầm tay Jisulife Life 7"
    assert norm.category == "Quạt mini"
    assert "Pin 5000mAh" in norm.verified_claims
    assert len(norm.approved_media) == 2


def test_normalize_product_identity_rejects_pack_id_as_name():
    rp = {
        "resource_pack": {
            "resource_pack_id": "pack_snap_abcdef1234567890",
            "brand": "Jisulife",
            "model": "Life 7",
            "product_name": "pack_snap_abcdef1234567890",  # Raw ID passed as name
        }
    }
    norm = normalize_product_identity(rp)
    # Should replace pack_snap_ ID with brand + model
    assert norm.display_name == "Jisulife Life 7"
    assert not norm.display_name.startswith("pack_snap_")


def test_normalize_product_identity_rejects_generic_fallback():
    rp = {
        "resource_pack": {
            "resource_pack_id": "pack_snap_123",
            "brand": "",
            "model": "",
            "product_name": "Sản phẩm công nghệ tiện ích",
        }
    }
    with pytest.raises(ValueError, match="truthful product identity"):
        normalize_product_identity(rp)


def test_normalize_product_identity_blocks_mismatch():
    rp = {
        "resource_pack": {
            "brand": "Baseus",
            "model": "WM02",
        }
    }
    src_ctx = {
        "brand": "Jisulife",
        "model": "Life 7",
    }
    with pytest.raises(ValueError, match="brand mismatch"):
        normalize_product_identity(rp, source_context=src_ctx)


def test_model_variant_is_not_substring_match():
    with pytest.raises(ValueError, match="model mismatch"):
        normalize_product_identity(
            {"brand": "QCY", "model": "T13 ANC2"},
            source_context={"brand": "QCY", "model": "T13 ANC"},
        )


def test_normalize_product_identity_unwraps_pi_envelope_and_builds_media_manifest(tmp_path: Path):
    image_one = tmp_path / "one.png"
    image_two = tmp_path / "two.png"
    Image.new("RGB", (8, 8), color="red").save(image_one)
    Image.new("RGB", (8, 8), color="blue").save(image_two)

    normalized = normalize_product_identity(
        {
            "raw_result": {
                "resource_pack": {
                    "resolved_values": {"brand": "AULA", "model": "F75", "variant": "Black"},
                    "assets_mapping": {"asset_1": str(image_one), "asset_2": str(image_two)},
                    "verified_selling_points": ["Hot-swap sockets"],
                }
            }
        }
    )

    assert normalized.display_name == "AULA F75"
    assert normalized.verified_claims == ["Hot-swap sockets"]
    assert [asset["asset_id"] for asset in normalized.approved_media] == ["asset_1", "asset_2"]
    assert all(asset["sha256"] for asset in normalized.approved_media)


def test_normalize_product_identity_drops_seller_name_from_display_name():
    normalized = normalize_product_identity(
        {
            "brand": "AULA",
            "model": "F75",
            "product_name": "Minh Gear - AULA F75",
        }
    )

    assert normalized.display_name == "AULA F75"
    assert "Minh Gear" not in normalized.display_name


def test_normalize_product_identity_filters_hallucinated_claims():
    rp = {
        "resource_pack": {
            "brand": "Jisulife",
            "model": "Life 7",
            "verified_selling_points": [
                "Động cơ không chổi than",
                "Review chân thực nhất hệ mặt trời",  # Hallucinated marketing phrase
                "Bấm vào giỏ hàng ngay",             # Call-to-action hallucination
                "Giảm giá 50% hôm nay",             # Fake discount
            ],
        }
    }
    norm = normalize_product_identity(rp)
    assert norm.verified_claims == ["Động cơ không chổi than"]
