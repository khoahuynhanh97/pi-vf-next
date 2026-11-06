"""Unit tests for ProductImageCleaner watermark and store domain inpainting engine."""

from pathlib import Path
from PIL import Image, ImageDraw

from video_factory.preprocessing.image_cleaner import (
    BoundingBox,
    ProductImageCleaner,
    compute_sha256,
)


def _create_test_image(path: Path, width: int = 400, height: int = 400, with_text: str = "") -> None:
    img = Image.new("RGB", (width, height), color=(240, 240, 240))
    draw = ImageDraw.Draw(img)
    # Draw a simulated product in the center
    draw.rectangle([100, 100, 300, 300], fill=(50, 120, 200))
    if with_text:
        # Draw simulated watermark text at bottom right
        draw.rectangle([280, 340, 390, 380], fill=(255, 0, 0))
    img.save(path)


def test_bounding_box_to_pixel_box():
    box = BoundingBox(ymin=100, xmin=200, ymax=500, xmax=800)
    # On 1000x1000 image
    x1, y1, x2, y2 = box.to_pixel_box(1000, 1000, padding_px=0)
    assert (x1, y1, x2, y2) == (200, 100, 800, 500)

    # On 500x500 image with 5px padding
    x1, y1, x2, y2 = box.to_pixel_box(500, 500, padding_px=5)
    assert x1 == max(0, 100 - 5)
    assert y1 == max(0, 50 - 5)
    assert x2 == min(500, 400 + 5)
    assert y2 == min(500, 250 + 5)


def test_clean_image_clean_file(tmp_path):
    img_path = tmp_path / "clean_product.png"
    _create_test_image(img_path, with_text="")

    cleaner = ProductImageCleaner(fake_mode=True, output_dir=tmp_path / "out")
    res = cleaner.clean_image(img_path, asset_id="asset_clean")

    assert res.status == "clean"
    assert res.has_cleaned_text is False
    assert len(res.detected_watermarks) == 0
    assert Path(res.cleaned_path).is_file()
    assert res.clean_sha256 == compute_sha256(res.cleaned_path)


def test_clean_image_with_watermark_in_fake_mode(tmp_path):
    img_path = tmp_path / "mivietnam_watermark.png"
    _create_test_image(img_path, with_text="mivietnam.vn")

    cleaner = ProductImageCleaner(fake_mode=True, output_dir=tmp_path / "out")
    res = cleaner.clean_image(img_path, asset_id="asset_wm")

    assert res.status == "cleaned"
    assert res.has_cleaned_text is True
    assert "mivietnam.vn" in res.detected_watermarks
    assert Path(res.cleaned_path).is_file()
    assert res.clean_sha256 != ""
    assert res.clean_sha256 == compute_sha256(res.cleaned_path)


def test_clean_image_with_custom_evaluator(tmp_path):
    img_path = tmp_path / "custom_watermark.png"
    _create_test_image(img_path, with_text="bad_shop.com")

    def mock_evaluator(image_path: str, prompt: str, system_prompt: str):
        return {
            "has_unwanted_text": True,
            "detected_texts": ["bad_shop.com"],
            "bounding_boxes": [
                {"box_2d": [850, 700, 950, 980], "label": "bad_shop.com"}
            ],
            "reason": "Found store badge",
        }

    cleaner = ProductImageCleaner(evaluator_fn=mock_evaluator, output_dir=tmp_path / "out")
    res = cleaner.clean_image(img_path, asset_id="asset_custom", brand="Xiaomi", model="Desk Lamp")

    assert res.status == "cleaned"
    assert res.has_cleaned_text is True
    assert "bad_shop.com" in res.detected_watermarks
    assert Path(res.cleaned_path).is_file()


def test_clean_resource_pack(tmp_path):
    p1 = tmp_path / "clean_1.png"
    p2 = tmp_path / "watermark_2.png"
    _create_test_image(p1, with_text="")
    _create_test_image(p2, with_text="mivietnam.vn")

    resource_pack = {
        "resource_pack_id": "pack_123",
        "product_identity": {
            "brand": "Xiaomi",
            "model": "Mijia Lamp",
        },
        "assets": [
            {"asset_id": "a1", "local_path": str(p1)},
            {"asset_id": "a2", "local_path": str(p2)},
        ],
    }

    cleaner = ProductImageCleaner(fake_mode=True, output_dir=tmp_path / "cleaned_assets")
    updated_pack, results = cleaner.clean_resource_pack(resource_pack)

    assert updated_pack["has_preprocessed_images"] is True
    assert updated_pack["source_assets"] == resource_pack["assets"]
    assert len(results) == 2
    assert results[0].has_cleaned_text is False
    assert results[1].has_cleaned_text is True

    # Check updated paths
    assets = updated_pack["assets"]
    assert assets[0]["local_path"] == results[0].cleaned_path
    assert assets[1]["local_path"] == results[1].cleaned_path
    assert assets[1]["cleaned"] is True
    assert "mivietnam.vn" in assets[1]["detected_watermarks"]
    assert assets[1]["source_sha256"] == compute_sha256(p2)
    assert assets[1]["clean_sha256"] == compute_sha256(results[1].cleaned_path)
    assert assets[1]["preprocessing"]["requires_fresh_image_qc"] is True


def test_clean_image_with_custom_evaluator_multi_category(tmp_path):
    img_path = tmp_path / "multi_artifacts.png"
    _create_test_image(img_path, with_text="bad_shop.com")

    def mock_evaluator(image_path: str, prompt: str, system_prompt: str):
        return {
            "has_unwanted_text": True,
            "detected_texts": ["bad_shop.com", "Flash Sale 50%", "Hotline: 0912345678"],
            "bounding_boxes": [
                {"box_2d": [850, 700, 950, 980], "label": "bad_shop.com", "category": "shop_domain"},
                {"box_2d": [20, 20, 100, 300], "label": "Flash Sale 50%", "category": "promotional_tag"},
                {"box_2d": [900, 20, 960, 350], "label": "Hotline: 0912345678", "category": "contact_and_social"},
            ],
            "reason": "Found multiple commercial overlays",
        }

    cleaner = ProductImageCleaner(evaluator_fn=mock_evaluator, output_dir=tmp_path / "out")
    res = cleaner.clean_image(img_path, asset_id="asset_multi", brand="Xiaomi", model="Desk Lamp")

    assert res.status == "cleaned"
    assert res.has_cleaned_text is True
    assert set(res.detected_categories) == {"contact_and_social", "promotional_tag", "shop_domain"}
    assert len(res.detected_watermarks) == 3
    assert Path(res.cleaned_path).is_file()


def test_clean_image_selective_category_filter(tmp_path):
    img_path = tmp_path / "promo_and_domain.png"
    _create_test_image(img_path, with_text="shop")

    def mock_evaluator(image_path: str, prompt: str, system_prompt: str):
        return {
            "has_unwanted_text": True,
            "detected_texts": ["bad_shop.com", "Flash Sale 50%"],
            "bounding_boxes": [
                {"box_2d": [850, 700, 950, 980], "label": "bad_shop.com", "category": "shop_domain"},
                {"box_2d": [20, 20, 100, 300], "label": "Flash Sale 50%", "category": "promotional_tag"},
            ],
            "reason": "Multiple artifacts",
        }

    # Only clean shop_domain, ignore promotional_tag
    cleaner = ProductImageCleaner(
        evaluator_fn=mock_evaluator,
        output_dir=tmp_path / "out",
        categories_to_clean=["shop_domain"],
    )
    res = cleaner.clean_image(img_path, asset_id="asset_selective")

    assert res.status == "cleaned"
    assert res.has_cleaned_text is True
    assert Path(res.cleaned_path).is_file()


def test_clean_image_missing_file():
    cleaner = ProductImageCleaner(fake_mode=True)
    res = cleaner.clean_image("non_existent_file_12345.png")
    assert res.status == "failed"
    assert "File not found" in res.error_message


def test_detector_failure_is_unknown_and_does_not_claim_clean(tmp_path):
    img_path = tmp_path / "product.png"
    _create_test_image(img_path)

    def failing_evaluator(_image_path: str, _prompt: str, _system_prompt: str):
        raise RuntimeError("vision unavailable")

    cleaner = ProductImageCleaner(evaluator_fn=failing_evaluator, output_dir=tmp_path / "out")
    res = cleaner.clean_image(img_path, asset_id="asset-1")

    assert res.status == "unknown"
    assert res.cleaned_path == str(img_path.resolve())
    assert res.has_cleaned_text is False
    assert "vision unavailable" in res.error_message


def test_detection_without_boxes_fails_closed(tmp_path):
    img_path = tmp_path / "product.png"
    _create_test_image(img_path)

    cleaner = ProductImageCleaner(
        evaluator_fn=lambda *_args: {
            "has_unwanted_text": True,
            "detected_texts": ["seller.example"],
            "bounding_boxes": [],
        },
        output_dir=tmp_path / "out",
    )
    res = cleaner.clean_image(img_path, asset_id="asset-1")

    assert res.status == "unknown"
    assert res.cleaned_path == str(img_path.resolve())
    assert not (tmp_path / "out" / "asset-1_clean.png").exists()


def test_suspicious_large_box_is_not_inpainted(tmp_path):
    img_path = tmp_path / "product.png"
    _create_test_image(img_path)

    cleaner = ProductImageCleaner(
        evaluator_fn=lambda *_args: {
            "has_unwanted_text": True,
            "detected_texts": ["large overlay"],
            "bounding_boxes": [
                {
                    "box_2d": [100, 100, 900, 900],
                    "label": "large overlay",
                    "category": "generic_watermark",
                    "confidence": 0.99,
                }
            ],
        },
        output_dir=tmp_path / "out",
    )
    res = cleaner.clean_image(img_path, asset_id="asset-1")

    assert res.status == "unknown"
    assert "unsafe" in res.error_message.lower()
