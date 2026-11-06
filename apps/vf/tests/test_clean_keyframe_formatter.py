"""Unit tests for pristine 9:16 vertical keyframe formatting and border stripping."""

from __future__ import annotations

from pathlib import Path
from PIL import Image

import pytest

from video_factory.preprocessing.image_cleaner import format_clean_916_keyframe


def test_format_clean_916_keyframe_geometry(tmp_path: Path):
    # 1. Create a synthetic test square image with colored bands
    raw_img_path = tmp_path / "raw_square_shopee.png"
    im = Image.new("RGB", (1000, 1000), (255, 0, 0)) # Red
    # Draw central product area
    for x in range(300, 700):
        for y in range(300, 700):
            im.putpixel((x, y), (255, 255, 255))
    im.save(raw_img_path)

    # 2. Format to clean 9:16 keyframe
    out_img_path = tmp_path / "clean_916_keyframe.png"
    res_path = format_clean_916_keyframe(
        input_image_path=raw_img_path,
        output_image_path=out_img_path,
        target_width=720,
        target_height=1280,
    )

    # 3. Assertions
    assert res_path == out_img_path
    assert out_img_path.is_file()

    with Image.open(out_img_path) as out_im:
        assert out_im.size == (720, 1280)
        # Background pixel at top corner should match background_color (8, 6, 8)
        assert out_im.getpixel((10, 10)) == (8, 6, 8)
        # Center should contain the preserved product content
        center_color = out_im.getpixel((360, 640))
        assert center_color == (255, 255, 255)


def test_format_clean_916_keyframe_custom_crops(tmp_path: Path):
    raw_img_path = tmp_path / "banner_product.jpg"
    im = Image.new("RGB", (800, 800), (50, 50, 50))
    im.save(raw_img_path)

    out_img_path = tmp_path / "out_custom.png"
    format_clean_916_keyframe(
        input_image_path=raw_img_path,
        output_image_path=out_img_path,
        target_width=1080,
        target_height=1920,
        crop_header_ratio=0.1,
        crop_footer_ratio=0.1,
        crop_margin_ratio=0.05,
        background_color=(0, 0, 0),
    )

    with Image.open(out_img_path) as out_im:
        assert out_im.size == (1080, 1920)
        assert out_im.getpixel((0, 0)) == (0, 0, 0)
