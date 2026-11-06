"""Unit tests for Phase 2 CV integrity checker module."""

from pathlib import Path
import cv2
import numpy as np
import pytest

from video_factory.qc.cv_integrity_checker import (
    IntegrityCheckResult,
    VideoFrameIntegrityChecker,
)


def test_checker_missing_file(tmp_path: Path):
    """Verify missing file handling returns fail with descriptive issue."""
    checker = VideoFrameIntegrityChecker()
    missing_path = tmp_path / "missing.mp4"

    result = checker.check_scene(missing_path, "scene_01")
    assert isinstance(result, IntegrityCheckResult)
    assert result.verdict == "fail"
    assert result.total_score == 0
    assert result.blur_detected is True
    assert any("not found" in issue.lower() for issue in result.issues)


def test_checker_synthetic_video(tmp_path: Path):
    """Verify technical checks on a synthetic test video."""
    video_path = tmp_path / "sharp_test.mp4"
    width, height, fps, num_frames = 720, 1280, 30.0, 30

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(str(video_path), fourcc, fps, (width, height))

    # Generate high-contrast pattern (sharp edges -> high Laplacian variance)
    for _ in range(num_frames):
        frame = np.zeros((height, width, 3), dtype=np.uint8)
        # Add alternating black and white stripes
        frame[::20, :, :] = 255
        frame[:, ::20, :] = 255
        out.write(frame)
    out.release()

    checker = VideoFrameIntegrityChecker(
        target_width=720,
        target_height=1280,
        min_fps=24.0,
        min_laplacian=70.0,
        resolution_tolerance=0.33,
    )
    result = checker.check_scene(video_path, "scene_01")
    assert result.verdict == "pass"
    assert result.resolution_valid is True
    assert result.fps_valid is True
    assert result.blur_detected is False
    assert result.mean_laplacian > 100.0
    assert result.total_score == 100


def test_checker_blurry_synthetic_video(tmp_path: Path):
    """Verify blur detection on a blurred synthetic test video."""
    video_path = tmp_path / "blurry_test.mp4"
    width, height, fps, num_frames = 720, 1280, 30.0, 30

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(str(video_path), fourcc, fps, (width, height))

    # Generate uniform/smooth grey frames (no high-frequency content -> near zero Laplacian)
    for _ in range(num_frames):
        frame = np.full((height, width, 3), 128, dtype=np.uint8)
        out.write(frame)
    out.release()

    checker = VideoFrameIntegrityChecker(
        target_width=720,
        target_height=1280,
        min_fps=24.0,
        min_laplacian=70.0,
        resolution_tolerance=0.33,
    )
    result = checker.check_scene(video_path, "scene_01")
    assert result.blur_detected is True
    assert result.mean_laplacian < 1.0
    assert result.verdict == "fail"
    assert result.total_score == 60  # 100 - 40 blur
