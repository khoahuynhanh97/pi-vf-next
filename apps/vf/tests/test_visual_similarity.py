"""Unit tests for Phase 3 visual similarity module."""

from pathlib import Path
import cv2
import numpy as np
import pytest

from video_factory.qc.visual_similarity import (
    SimilarityResult,
    VisualSimilarityChecker,
)


def test_visual_similarity_missing_reference(tmp_path: Path):
    """Verify missing reference image produces a fail result with notes."""
    checker = VisualSimilarityChecker(ssim_threshold=0.65)
    missing_ref = tmp_path / "missing_ref.png"
    fake_ren = tmp_path / "fake_ren.png"
    fake_ren.write_bytes(b"data")

    res = checker.compare(missing_ref, fake_ren, "scene_01")
    assert isinstance(res, SimilarityResult)
    assert res.passes_threshold is False
    assert res.ssim_score == 0.0
    assert any("not found" in n.lower() for n in res.notes)


def test_visual_similarity_identical_images(tmp_path: Path):
    """Verify identical images yield SSIM = 1.0."""
    checker = VisualSimilarityChecker(ssim_threshold=0.65)
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    img[20:80, 20:80] = [255, 200, 100]

    ref_path = tmp_path / "ref.png"
    ren_path = tmp_path / "ren.png"
    cv2.imwrite(str(ref_path), img)
    cv2.imwrite(str(ren_path), img)

    res = checker.compare(ref_path, ren_path, "scene_01")
    assert res.passes_threshold is True
    assert res.ssim_score >= 0.99
    assert res.mse == 0.0


def test_visual_similarity_different_dimensions(tmp_path: Path):
    """Verify checker auto-resizes rendered image to match reference dimensions."""
    checker = VisualSimilarityChecker(ssim_threshold=0.65)
    ref_img = np.full((1280, 720, 3), 150, dtype=np.uint8)
    ren_img = np.full((1920, 1080, 3), 150, dtype=np.uint8)

    ref_path = tmp_path / "ref.png"
    ren_path = tmp_path / "ren.png"
    cv2.imwrite(str(ref_path), ref_img)
    cv2.imwrite(str(ren_path), ren_img)

    res = checker.compare(ref_path, ren_path, "scene_02")
    assert res.passes_threshold is True
    assert res.ssim_score >= 0.99
    assert any("resized" in n.lower() for n in res.notes)
