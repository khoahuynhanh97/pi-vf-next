from pathlib import Path
import cv2
import numpy as np
import pytest

from video_factory.qc.auto_retry_helpers import (
    SAFE_ZONE_POLICY,
    inject_safe_zone_constraint,
)
from video_factory.qc.cv_integrity_checker import VideoFrameIntegrityChecker


def create_synthetic_test_video(path: Path, width: int = 720, height: int = 1280, motion_pos: str = "center") -> None:
    """Create a 10-frame synthetic mp4 video with motion at specific normalized position."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(str(path), fourcc, 24.0, (width, height))

    # Determine motion bounding box
    if motion_pos == "center":
        # Safe center area: (0.45, 0.45)
        box_x = int(0.45 * width)
        box_y = int(0.45 * height)
    elif motion_pos == "right_action_bar":
        # X ~ 0.90, Y ~ 0.50
        box_x = int(0.90 * width)
        box_y = int(0.50 * height)
    elif motion_pos == "bottom_overlay":
        # X ~ 0.50, Y ~ 0.90
        box_x = int(0.50 * width)
        box_y = int(0.90 * height)
    elif motion_pos == "top_header":
        # X ~ 0.50, Y ~ 0.05
        box_x = int(0.50 * width)
        box_y = int(0.05 * height)
    else:
        box_x, box_y = int(0.5 * width), int(0.5 * height)

    for frame_idx in range(10):
        frame = np.ones((height, width, 3), dtype=np.uint8) * 120
        # In second half of frames, draw a high-contrast moving patch
        if frame_idx >= 5:
            cv2.rectangle(
                frame,
                (box_x, box_y),
                (box_x + 60, box_y + 60),
                (255, 255, 255),
                -1,
            )
        out.write(frame)
    out.release()


def test_safe_zone_detection_center(tmp_path: Path):
    """Motion in center should pass safe zone check."""
    video_path = tmp_path / "safe_center.mp4"
    create_synthetic_test_video(video_path, motion_pos="center")

    checker = VideoFrameIntegrityChecker()
    res = checker.check_tiktok_safe_zone(video_path)

    assert res.is_safe is True
    assert res.safe_zone_score == 100.0
    assert len(res.collision_zones) == 0


def test_safe_zone_detection_right_collision(tmp_path: Path):
    """Motion on right edge should collide with right_action_bar."""
    video_path = tmp_path / "collide_right.mp4"
    create_synthetic_test_video(video_path, motion_pos="right_action_bar")

    checker = VideoFrameIntegrityChecker()
    res = checker.check_tiktok_safe_zone(video_path)

    assert res.is_safe is False
    assert "right_action_bar" in res.collision_zones
    assert res.safe_zone_score < 100.0


def test_safe_zone_detection_bottom_collision(tmp_path: Path):
    """Motion at bottom edge should collide with bottom_overlay."""
    video_path = tmp_path / "collide_bottom.mp4"
    create_synthetic_test_video(video_path, motion_pos="bottom_overlay")

    checker = VideoFrameIntegrityChecker()
    res = checker.check_tiktok_safe_zone(video_path)

    assert res.is_safe is False
    assert "bottom_overlay" in res.collision_zones


def test_inject_safe_zone_constraint():
    """Verify constraint injection prepends guidance for collision zones."""
    prompt = "A close-up shot of the woman smiling."
    injected = inject_safe_zone_constraint(prompt, ["right_action_bar", "bottom_overlay"])
    assert "[SPATIAL CONSTRAINT:" in injected
    assert "Shift action leftward" in injected
    assert "Raise subject framing upward" in injected
    assert injected.endswith(prompt)

    # Empty zones should return unchanged prompt
    assert inject_safe_zone_constraint(prompt, []) == prompt
