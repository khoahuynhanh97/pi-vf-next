"""Computer Vision-based automated video clip integrity and quality checker.

Evaluates technical constraints (resolution, frame rate) and visual sharpness
(Laplacian variance) across sampled keyframes to replace hardcoded QC scores.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class SafeZoneCheckResult:
    is_safe: bool
    safe_zone_score: float  # 0-100
    collision_zones: list[str]
    action_centroid: tuple[float, float]  # Normalized (x, y)


@dataclass
class IntegrityCheckResult:
    scene_id: str
    video_path: str
    resolution_valid: bool        # Width/height match target (±33% tolerance)
    fps_valid: bool                # FPS >= min_fps
    mean_laplacian: float          # Average sharpness across 3 sample frames
    blur_detected: bool            # mean_laplacian < threshold
    frame_count: int
    actual_width: int
    actual_height: int
    actual_fps: float
    total_score: int               # 0-100
    verdict: str                   # "pass" or "fail"
    issues: list[str]              # Human-readable issue descriptions
    safe_zone_result: SafeZoneCheckResult | None = None


class VideoFrameIntegrityChecker:
    """Automated CV-based video frame integrity and sharpness checker."""

    def __init__(
        self,
        target_width: int = 1080,
        target_height: int = 1920,
        min_fps: float = 24.0,
        min_laplacian: float = 70.0,
        resolution_tolerance: float = 0.33,  # Allow 67%-133% of target
        check_safe_zone: bool = True,
        config_path: Path | str | None = None,
    ) -> None:
        self.target_width = target_width
        self.target_height = target_height
        self.min_fps = min_fps
        self.min_laplacian = min_laplacian
        self.resolution_tolerance = resolution_tolerance
        self.check_safe_zone = check_safe_zone
        self.config_path = Path(config_path) if config_path else Path("config/tiktok_safe_zone.yaml")
        self._safe_zone_config = self._load_safe_zone_config()

    def _load_safe_zone_config(self) -> dict[str, Any]:
        """Load safe zone config from YAML file with resilient fallback."""
        default_cfg = {
            "safe_zone": {"x_min": 0.08, "x_max": 0.82, "y_min": 0.12, "y_max": 0.76},
            "dead_zones": {
                "right_action_bar": {"x_min": 0.82, "x_max": 1.0, "y_min": 0.35, "y_max": 0.82},
                "bottom_overlay": {"x_min": 0.0, "x_max": 1.0, "y_min": 0.78, "y_max": 1.0},
                "top_header": {"x_min": 0.0, "x_max": 1.0, "y_min": 0.0, "y_max": 0.10},
            },
        }
        if self.config_path.exists():
            try:
                import yaml
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                platform_cfg = data.get("platforms", {}).get("tiktok", {})
                if "safe_zone" in platform_cfg and "dead_zones" in platform_cfg:
                    return platform_cfg
            except Exception as e:
                logger.warning("Failed to load %s: %s. Using default config.", self.config_path, e)
        return default_cfg

    def check_tiktok_safe_zone(self, video_path: Path) -> SafeZoneCheckResult:
        """Detect if scene action/motion collides with TikTok 9:16 UI dead zones.

        Configurable via config/tiktok_safe_zone.yaml.
        """
        video_path = Path(video_path)
        if not video_path.exists():
            return SafeZoneCheckResult(is_safe=False, safe_zone_score=0.0, collision_zones=["file_not_found"], action_centroid=(0.5, 0.5))

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return SafeZoneCheckResult(is_safe=False, safe_zone_score=0.0, collision_zones=["cannot_open_file"], action_centroid=(0.5, 0.5))

        try:
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if total_frames < 2:
                return SafeZoneCheckResult(is_safe=True, safe_zone_score=100.0, collision_zones=[], action_centroid=(0.5, 0.5))

            # Sample 2 frames across the clip
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ret1, frame1 = cap.read()
            cap.set(cv2.CAP_PROP_POS_FRAMES, total_frames // 2)
            ret2, frame2 = cap.read()

            if not (ret1 and ret2 and frame1 is not None and frame2 is not None):
                return SafeZoneCheckResult(is_safe=True, safe_zone_score=100.0, collision_zones=[], action_centroid=(0.5, 0.5))

            gray1 = cv2.cvtColor(frame1, cv2.COLOR_BGR2GRAY)
            gray2 = cv2.cvtColor(frame2, cv2.COLOR_BGR2GRAY)
            diff = cv2.absdiff(gray1, gray2)
            _, thresh = cv2.threshold(diff, 30, 255, cv2.THRESH_BINARY)

            h, w = gray1.shape
            M = cv2.moments(thresh)
            if M["m00"] > 0:
                cx = (M["m10"] / M["m00"]) / w
                cy = (M["m01"] / M["m00"]) / h
            else:
                cx, cy = 0.5, 0.5

            # Dynamic check against configured dead zones
            dead_zones = self._safe_zone_config.get("dead_zones", {})
            collision_zones = []
            for zone_name, bounds in dead_zones.items():
                x_min = bounds.get("x_min", 0.0)
                x_max = bounds.get("x_max", 1.0)
                y_min = bounds.get("y_min", 0.0)
                y_max = bounds.get("y_max", 1.0)
                if x_min <= cx <= x_max and y_min <= cy <= y_max:
                    collision_zones.append(zone_name)

            # Check safe zone boundary
            sz = self._safe_zone_config.get("safe_zone", {})
            in_safe_zone = (sz.get("x_min", 0.08) <= cx <= sz.get("x_max", 0.82)) and (
                sz.get("y_min", 0.12) <= cy <= sz.get("y_max", 0.76)
            )
            is_safe = (len(collision_zones) == 0) and in_safe_zone
            safe_zone_score = 100.0 - (len(collision_zones) * 35.0) if not is_safe else 100.0
            safe_zone_score = max(0.0, min(100.0, safe_zone_score))

            return SafeZoneCheckResult(
                is_safe=is_safe,
                safe_zone_score=round(safe_zone_score, 1),
                collision_zones=collision_zones,
                action_centroid=(round(float(cx), 3), round(float(cy), 3)),
            )
        finally:
            cap.release()

    def check_scene(self, video_path: Path, scene_id: str) -> IntegrityCheckResult:
        """Perform technical QC on a single video clip.

        Checks:
        1. Resolution within tolerance (e.g., 720-1440 width for 1080 target)
        2. FPS >= min_fps
        3. Sharpness (Laplacian variance) on 3 sample frames (start, mid, end)
        4. Overall scoring: 100 base, -30 resolution, -10 fps, -40 blur

        Args:
            video_path: Path to the video file to inspect.
            scene_id: Identifier for the scene (e.g. 'scene_01').

        Returns:
            IntegrityCheckResult with verdict 'pass' (score >= 80) or 'fail'.
        """
        video_path = Path(video_path)

        if not video_path.exists():
            logger.warning("Video file does not exist: %s", video_path)
            return IntegrityCheckResult(
                scene_id=scene_id,
                video_path=str(video_path),
                resolution_valid=False,
                fps_valid=False,
                mean_laplacian=0.0,
                blur_detected=True,
                frame_count=0,
                actual_width=0,
                actual_height=0,
                actual_fps=0.0,
                total_score=0,
                verdict="fail",
                issues=[f"Video file not found: {video_path}"],
            )

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            logger.warning("Failed to open video file: %s", video_path)
            return IntegrityCheckResult(
                scene_id=scene_id,
                video_path=str(video_path),
                resolution_valid=False,
                fps_valid=False,
                mean_laplacian=0.0,
                blur_detected=True,
                frame_count=0,
                actual_width=0,
                actual_height=0,
                actual_fps=0.0,
                total_score=0,
                verdict="fail",
                issues=[f"Failed to open video file with OpenCV: {video_path}"],
            )

        try:
            # Read technical metadata
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = float(cap.get(cv2.CAP_PROP_FPS))
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

            # Check resolution tolerance
            width_min = self.target_width * (1 - self.resolution_tolerance)
            width_max = self.target_width * (1 + self.resolution_tolerance)
            height_min = self.target_height * (1 - self.resolution_tolerance)
            height_max = self.target_height * (1 + self.resolution_tolerance)

            resolution_valid = (
                width_min <= width <= width_max and
                height_min <= height <= height_max
            )

            fps_valid = fps >= self.min_fps

            # Sample 3 frames for sharpness check (start, mid, end)
            sample_indices = [0, frame_count // 2, max(0, frame_count - 1)]
            laplacian_vars = []

            for idx in sample_indices:
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ret, frame = cap.read()
                if ret and frame is not None:
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    laplacian_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
                    laplacian_vars.append(laplacian_var)

            mean_laplacian = float(np.mean(laplacian_vars)) if laplacian_vars else 0.0
            blur_detected = mean_laplacian < self.min_laplacian

            # Scoring
            score = 100
            issues: list[str] = []

            if not resolution_valid:
                score -= 30
                issues.append(
                    f"Resolution {width}×{height} outside tolerance "
                    f"(target {self.target_width}×{self.target_height} ±{int(self.resolution_tolerance*100)}%)"
                )

            if not fps_valid:
                score -= 10
                issues.append(f"FPS {fps:.1f} below minimum {self.min_fps}")

            if blur_detected:
                score -= 40
                issues.append(
                    f"Blur detected: Laplacian variance {mean_laplacian:.1f} < threshold {self.min_laplacian}"
                )

            safe_zone_res = None
            if self.check_safe_zone:
                # We can call check_tiktok_safe_zone directly on video_path
                # Note: cap will be released in finally block
                safe_zone_res = self.check_tiktok_safe_zone(video_path)
                if not safe_zone_res.is_safe:
                    score -= 20
                    issues.append(
                        f"TikTok UI collision detected in zones: {', '.join(safe_zone_res.collision_zones)}"
                    )

            verdict = "pass" if score >= 80 else "fail"

            return IntegrityCheckResult(
                scene_id=scene_id,
                video_path=str(video_path),
                resolution_valid=resolution_valid,
                fps_valid=fps_valid,
                mean_laplacian=round(mean_laplacian, 1),
                blur_detected=blur_detected,
                frame_count=frame_count,
                actual_width=width,
                actual_height=height,
                actual_fps=round(fps, 1),
                total_score=max(0, score),
                verdict=verdict,
                issues=issues,
                safe_zone_result=safe_zone_res,
            )
        finally:
            cap.release()

    def check_all_scenes(
        self,
        video_paths: list[Path],
        scene_id_prefix: str = "scene_",
    ) -> list[IntegrityCheckResult]:
        """Check multiple scene clips and return list of results.

        Args:
            video_paths: List of file paths for each scene video clip.
            scene_id_prefix: Prefix to prepend to 1-based scene numbers.

        Returns:
            List of IntegrityCheckResult instances.
        """
        results: list[IntegrityCheckResult] = []
        for idx, path in enumerate(video_paths, start=1):
            scene_id = f"{scene_id_prefix}{idx:02d}"
            result = self.check_scene(Path(path), scene_id)
            results.append(result)
        return results
