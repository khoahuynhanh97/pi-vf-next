"""Provider output validation before terminal completion."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import av


@dataclass(frozen=True)
class SceneValidationResult:
    """Validation outcome for a scene render output."""
    valid: bool
    sha256: str
    duration_seconds: float
    width: int
    height: int
    fps: float
    frame_count: int
    codec: str
    aspect_ratio: str
    error: str | None = None


def validate_scene_output(
    *,
    output_path: str | Path,
    expected_duration: float,
    expected_aspect_ratio: str = "9:16",
    duration_tolerance: float = 0.5,
) -> SceneValidationResult:
    """Fully decode and validate a scene render output before marking it completed.
    
    Validates:
    - File exists and is non-empty
    - Fully decodable with at least one frame
    - Duration within tolerance of expected
    - Aspect ratio matches expected
    - Returns SHA-256 digest computed from bytes
    
    Raises ValueError with specific error code on validation failure.
    """
    path = Path(output_path).resolve()
    
    if not path.is_file():
        raise ValueError(f"SCENE_OUTPUT_MISSING: Output file does not exist: {path}")
    
    if path.stat().st_size == 0:
        raise ValueError(f"SCENE_OUTPUT_EMPTY: Output file is empty: {path}")
    
    # Compute SHA-256 before decode validation
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    sha256 = h.hexdigest()
    
    # Full decode validation
    try:
        with av.open(str(path)) as container:
            v_stream = next((s for s in container.streams if s.type == "video"), None)
            if v_stream is None:
                raise ValueError(f"SCENE_OUTPUT_NO_VIDEO_STREAM: No video stream in {path}")
            
            # Decode all frames
            frame_count = 0
            for _ in container.decode(video=v_stream.index):
                frame_count += 1
            
            if frame_count == 0:
                raise ValueError(f"SCENE_OUTPUT_ZERO_FRAMES: No decodable frames in {path}")
            
            # Extract metadata
            duration_sec = 0.0
            if container.duration is not None:
                duration_sec = float(container.duration / av.time_base)
            elif v_stream.duration is not None and v_stream.time_base is not None:
                duration_sec = float(v_stream.duration * v_stream.time_base)
            
            if duration_sec <= 0:
                raise ValueError(f"SCENE_OUTPUT_INVALID_DURATION: Invalid duration in {path}")
            
            fps = float(v_stream.average_rate or v_stream.guessed_rate or 0.0)
            if fps <= 0:
                raise ValueError(f"SCENE_OUTPUT_INVALID_FPS: Invalid FPS in {path}")
            
            width = int(v_stream.width)
            height = int(v_stream.height)
            if width <= 0 or height <= 0:
                raise ValueError(f"SCENE_OUTPUT_INVALID_DIMENSIONS: Invalid dimensions {width}x{height}")
            
            codec = v_stream.codec_context.name if v_stream.codec_context else "unknown"
            
            # Validate duration tolerance
            delta = abs(duration_sec - expected_duration)
            if delta > duration_tolerance:
                raise ValueError(
                    f"SCENE_OUTPUT_DURATION_MISMATCH: Got {duration_sec:.2f}s, "
                    f"expected {expected_duration:.2f}s (tolerance ±{duration_tolerance}s)"
                )
            
            # Validate aspect ratio
            actual_aspect = f"{width}:{height}"
            if expected_aspect_ratio == "9:16":
                expected_w, expected_h = 9, 16
            elif expected_aspect_ratio == "16:9":
                expected_w, expected_h = 16, 9
            else:
                parts = expected_aspect_ratio.split(":")
                if len(parts) != 2:
                    raise ValueError(f"Invalid expected_aspect_ratio format: {expected_aspect_ratio}")
                expected_w, expected_h = int(parts[0]), int(parts[1])
            
            # Allow tolerance for aspect ratio (within 2% of expected ratio)
            expected_ratio = expected_w / expected_h
            actual_ratio = width / height
            ratio_diff = abs(actual_ratio - expected_ratio) / expected_ratio
            
            if ratio_diff > 0.02:
                raise ValueError(
                    f"SCENE_OUTPUT_ASPECT_MISMATCH: Got {actual_aspect} (ratio {actual_ratio:.3f}), "
                    f"expected {expected_aspect_ratio} (ratio {expected_ratio:.3f})"
                )
            
            return SceneValidationResult(
                valid=True,
                sha256=sha256,
                duration_seconds=round(duration_sec, 3),
                width=width,
                height=height,
                fps=round(fps, 3),
                frame_count=frame_count,
                codec=codec,
                aspect_ratio=actual_aspect,
                error=None,
            )
            
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"SCENE_OUTPUT_DECODE_FAILED: Failed to decode {path}: {exc}") from exc
