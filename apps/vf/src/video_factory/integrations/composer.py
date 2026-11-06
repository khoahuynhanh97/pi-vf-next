"""PyAV-based video composition, normalization, and concatenation for Video Factory."""
from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

import av

logger = logging.getLogger(__name__)


class InvalidVideoArtifact(ValueError):
    """Raised when a media file exists but cannot be trusted as a video artifact."""


@dataclass(frozen=True)
class VideoProbe:
    path: str
    sha256: str
    duration_seconds: float
    width: int
    height: int
    fps: float
    codec: str
    frame_count: int
    audio_present: bool
    file_size: int = 0
    audio_codec: str | None = None


@dataclass(frozen=True)
class VideoExpectation:
    width: int = 720
    height: int = 1280
    duration_seconds: float = 16.0
    codec: str = "h264"
    fps: float = 24.0
    min_frames: int = 1
    max_duration_delta: float = 0.5
    check_black_bars: bool = False
    max_edge_black_bar_ratio: float = 0.02


def detect_black_bars(
    image_or_path_or_frame: Any,
    luminance_threshold: int = 15,
    max_edge_coverage_ratio: float = 0.02,
) -> dict[str, Any]:
    """Detect contiguous black/near-black borders along the 4 edges of an image or video frame.

    Returns coverage ratios (0.0 to 1.0) and whether any edge exceeds max_edge_coverage_ratio (default 2%).
    """
    from PIL import Image

    if isinstance(image_or_path_or_frame, (str, Path)):
        img = Image.open(str(image_or_path_or_frame)).convert("L")
    elif hasattr(image_or_path_or_frame, "to_image"):
        img = image_or_path_or_frame.to_image().convert("L")
    elif isinstance(image_or_path_or_frame, Image.Image):
        img = image_or_path_or_frame.convert("L")
    else:
        raise ValueError(f"Unsupported image type: {type(image_or_path_or_frame)}")

    width, height = img.size
    if width <= 0 or height <= 0:
        return {
            "has_black_bars": False,
            "top_coverage": 0.0,
            "bottom_coverage": 0.0,
            "left_coverage": 0.0,
            "right_coverage": 0.0,
            "active_width": width,
            "active_height": height,
        }

    pixels = list(img.tobytes())

    # Top border
    top_black_rows = 0
    for y in range(height):
        row_slice = pixels[y * width : (y + 1) * width]
        if sum(row_slice) / width < luminance_threshold:
            top_black_rows += 1
        else:
            break

    # Bottom border
    bottom_black_rows = 0
    for y in range(height - 1, -1, -1):
        row_slice = pixels[y * width : (y + 1) * width]
        if sum(row_slice) / width < luminance_threshold:
            bottom_black_rows += 1
        else:
            break

    # Left border
    left_black_cols = 0
    for x in range(width):
        col_sum = sum(pixels[y * width + x] for y in range(height))
        if col_sum / height < luminance_threshold:
            left_black_cols += 1
        else:
            break

    # Right border
    right_black_cols = 0
    for x in range(width - 1, -1, -1):
        col_sum = sum(pixels[y * width + x] for y in range(height))
        if col_sum / height < luminance_threshold:
            right_black_cols += 1
        else:
            break

    top_coverage = top_black_rows / height
    bottom_coverage = bottom_black_rows / height
    left_coverage = left_black_cols / width
    right_coverage = right_black_cols / width

    has_black_bars = any(
        cov > max_edge_coverage_ratio
        for cov in (top_coverage, bottom_coverage, left_coverage, right_coverage)
    )

    active_width = max(0, width - left_black_cols - right_black_cols)
    active_height = max(0, height - top_black_rows - bottom_black_rows)

    return {
        "has_black_bars": has_black_bars,
        "top_coverage": round(top_coverage, 4),
        "bottom_coverage": round(bottom_coverage, 4),
        "left_coverage": round(left_coverage, 4),
        "right_coverage": round(right_coverage, 4),
        "top_black_rows": top_black_rows,
        "bottom_black_rows": bottom_black_rows,
        "left_black_cols": left_black_cols,
        "right_black_cols": right_black_cols,
        "active_width": active_width,
        "active_height": active_height,
    }


def check_black_bars_in_video(
    file_path: str | Path,
    num_samples: int = 5,
    luminance_threshold: int = 15,
    max_edge_coverage_ratio: float = 0.02,
) -> dict[str, Any]:
    """Sample decoded frames from a video and determine if black bars persist across the video."""
    p = _normalize_file_path(file_path)
    if not p.is_file() or p.stat().st_size == 0:
        return {"has_persistent_black_bars": False, "samples_checked": 0}

    try:
        container = av.open(str(p))
    except Exception:
        return {"has_persistent_black_bars": False, "samples_checked": 0}

    v_stream = next((s for s in container.streams if s.type == "video"), None)
    if not v_stream:
        container.close()
        return {"has_persistent_black_bars": False, "samples_checked": 0}

    if num_samples < 1:
        container.close()
        raise ValueError('num_samples must be positive')
    total_frames = int(v_stream.frames or 0)
    if total_frames <= 0:
        # Count once when container metadata omits frame count; never substitute early frames.
        total_frames = sum(1 for _ in container.decode(v_stream))
        container.close()
        container = av.open(str(p))
        v_stream = next(s for s in container.streams if s.type == 'video')
    sample_indices = set()
    if total_frames > 0:
        sample_indices = {round((total_frames-1)*i/max(1, num_samples-1)) for i in range(num_samples)}

    results = []
    frame_idx = 0
    timestamps = []
    try:
        for frame in container.decode(v_stream):
            if frame_idx in sample_indices:
                res = detect_black_bars(
                    frame,
                    luminance_threshold=luminance_threshold,
                    max_edge_coverage_ratio=max_edge_coverage_ratio,
                )
                results.append(res)
                timestamps.append(float(frame.time or 0))
                if len(results) >= num_samples:
                    break
            frame_idx += 1
    except Exception as exc:
        logger.warning("Error while decoding frames for black bar detection: %s", exc)
    finally:
        container.close()

    if not results:
        return {"has_persistent_black_bars": False, "samples_checked": 0}

    black_bar_counts = sum(1 for r in results if r["has_black_bars"])
    has_persistent = (black_bar_counts / len(results)) >= 0.6

    avg_top = sum(r["top_coverage"] for r in results) / len(results)
    avg_bottom = sum(r["bottom_coverage"] for r in results) / len(results)
    avg_left = sum(r["left_coverage"] for r in results) / len(results)
    avg_right = sum(r["right_coverage"] for r in results) / len(results)

    return {
        "has_persistent_black_bars": has_persistent,
        "samples_checked": len(results),
        'sample_timestamps': timestamps,
        'coverage_limitation': 'Sparse border samples; does not prove every frame or content continuity.',
        "black_bar_sample_count": black_bar_counts,
        "top_coverage": round(avg_top, 4),
        "bottom_coverage": round(avg_bottom, 4),
        "left_coverage": round(avg_left, 4),
        "right_coverage": round(avg_right, 4),
    }


def build_video_metadata(probe: VideoProbe) -> dict[str, Any]:
    """Build a deterministic dictionary of video facts directly derived from a VideoProbe."""
    return {
        "sha256": probe.sha256,
        "duration_seconds": probe.duration_seconds,
        "resolution": f"{probe.width}x{probe.height}",
        "fps": probe.fps,
        "width": probe.width,
        "height": probe.height,
        "frame_count": probe.frame_count,
        "codec": probe.codec,
        "audio_present": probe.audio_present,
    }


def validate_export(path: str | Path, expected: VideoExpectation | None = None) -> VideoProbe:
    """Probe and strictly validate a video export file against expected parameters."""
    if expected is None:
        expected = VideoExpectation()
    probe = probe_video(path, decode_all=True)
    if (probe.width, probe.height) != (expected.width, expected.height):
        raise InvalidVideoArtifact(
            f"resolution mismatch: got ({probe.width}, {probe.height}), expected ({expected.width}, {expected.height})"
        )
    if abs(probe.duration_seconds - expected.duration_seconds) > expected.max_duration_delta:
        raise InvalidVideoArtifact(
            f"duration mismatch: got {probe.duration_seconds}s, expected {expected.duration_seconds}s (±{expected.max_duration_delta}s)"
        )
    if probe.codec != expected.codec or probe.frame_count < expected.min_frames:
        raise InvalidVideoArtifact(
            f"codec/frame validation failed: codec={probe.codec} (expected {expected.codec}), frame_count={probe.frame_count} (expected >= {expected.min_frames})"
        )
    if expected.check_black_bars:
        bb = check_black_bars_in_video(
            path,
            max_edge_coverage_ratio=expected.max_edge_black_bar_ratio,
        )
        if int(bb.get("samples_checked", 0) or 0) < 1:
            detail = str(bb.get("error") or "no video frames were sampled")
            raise InvalidVideoArtifact(f"black bar validation failed: {detail}")
        if bb.get("has_persistent_black_bars"):
            raise InvalidVideoArtifact(
                f"persistent black bars detected exceeding {expected.max_edge_black_bar_ratio:.1%} limit: "
                f"top={bb['top_coverage']:.1%}, bottom={bb['bottom_coverage']:.1%}, "
                f"left={bb['left_coverage']:.1%}, right={bb['right_coverage']:.1%}"
            )
    return probe


def _normalize_file_path(path_or_url: str | Path) -> Path:
    from video_factory.storage.layout import get_workspace_layout
    try:
        ws = get_workspace_layout()
        return ws.resolve_path(path_or_url)
    except Exception:
        s = str(path_or_url).strip()
        if s.startswith("file://"):
            from urllib.parse import unquote, urlparse
            parsed = urlparse(s)
            raw = unquote(parsed.path)
            netloc = unquote(parsed.netloc)
            if netloc and not raw.startswith(("/", "\\")):
                raw = f"{netloc}/{raw}"
            elif netloc:
                raw = f"{netloc}{raw}"
            if len(raw) >= 3 and raw[0] in ("/", "\\") and raw[2] == ":" and raw[1].isalpha():
                raw = raw[1:]
            return Path(raw).resolve()
        return Path(s).resolve()


def compute_file_sha256(file_path: str | Path) -> str:
    p = _normalize_file_path(file_path)
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def probe_video(file_path: str | Path, *, decode_all: bool = True) -> VideoProbe:
    """Open and optionally fully decode a video, returning facts from its bytes."""

    p = _normalize_file_path(file_path)
    if not p.is_file() or p.stat().st_size == 0:
        raise FileNotFoundError(f"Video file not found or empty: {p}")

    try:
        with av.open(str(p)) as container:
            v_stream = next((stream for stream in container.streams if stream.type == "video"), None)
            a_stream = next((stream for stream in container.streams if stream.type == "audio"), None)
            if v_stream is None:
                raise InvalidVideoArtifact(f"Video decode failed: no video stream in {p}")

            frame_count = 0
            if decode_all:
                for _frame in container.decode(video=v_stream.index):
                    frame_count += 1
                if frame_count == 0:
                    raise InvalidVideoArtifact(f"Video decode failed: zero decoded frames in {p}")
            else:
                frame_count = int(v_stream.frames or 0)

            duration_sec = 0.0
            if container.duration is not None:
                duration_sec = float(container.duration / av.time_base)
            elif v_stream.duration is not None and v_stream.time_base is not None:
                duration_sec = float(v_stream.duration * v_stream.time_base)
            if duration_sec <= 0:
                raise InvalidVideoArtifact(f"Video decode failed: invalid duration in {p}")

            fps = float(v_stream.average_rate or v_stream.guessed_rate or 0.0)
            if fps <= 0 or int(v_stream.width) <= 0 or int(v_stream.height) <= 0:
                raise InvalidVideoArtifact(f"Video decode failed: invalid stream geometry in {p}")

            codec = v_stream.codec_context.name if v_stream.codec_context else "unknown"
            audio_codec = a_stream.codec_context.name if a_stream and a_stream.codec_context else None
    except (FileNotFoundError, InvalidVideoArtifact):
        raise
    except Exception as exc:
        raise InvalidVideoArtifact(f"Video decode failed for {p}: {exc}") from exc

    return VideoProbe(
        path=str(p),
        file_size=p.stat().st_size,
        sha256=compute_file_sha256(p),
        duration_seconds=round(duration_sec, 3),
        width=int(v_stream.width),
        height=int(v_stream.height),
        fps=round(fps, 3),
        codec=codec,
        frame_count=frame_count,
        audio_present=a_stream is not None,
        audio_codec=audio_codec,
    )


def normalize_video_duration(
    input_path: str | Path, output_path: str | Path, duration_seconds: float
) -> VideoProbe:
    """Trim or hold the final frame to match a scene's expected duration."""
    source = _normalize_file_path(input_path)
    output = _normalize_file_path(output_path)
    source_probe = probe_video(source, decode_all=False)
    fps = max(1, round(source_probe.fps))
    target_frames = max(1, round(duration_seconds * fps))
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{output.stem}.", suffix=".mp4", dir=output.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with av.open(str(source)) as input_container, av.open(str(temp_path), mode="w") as output_container:
            input_stream = next(stream for stream in input_container.streams if stream.type == "video")
            output_stream = output_container.add_stream("libx264", rate=fps)
            output_stream.width = input_stream.width
            output_stream.height = input_stream.height
            output_stream.pix_fmt = "yuv420p"
            last_frame = None
            frame_count = 0
            for frame in input_container.decode(video=input_stream.index):
                if frame_count >= target_frames:
                    break
                last_frame = frame
                converted = frame.reformat(format="yuv420p")
                converted.pts = frame_count
                converted.time_base = Fraction(1, fps)
                for packet in output_stream.encode(converted):
                    output_container.mux(packet)
                frame_count += 1
            if last_frame is None:
                raise InvalidVideoArtifact(f"Video decode failed: zero decoded frames in {source}")
            while frame_count < target_frames:
                converted = last_frame.reformat(format="yuv420p")
                converted.pts = frame_count
                converted.time_base = Fraction(1, fps)
                for packet in output_stream.encode(converted):
                    output_container.mux(packet)
                frame_count += 1
            for packet in output_stream.encode():
                output_container.mux(packet)
        os.replace(temp_path, output)
        return probe_video(output, decode_all=True)
    finally:
        temp_path.unlink(missing_ok=True)


def get_video_info(file_path: str | Path) -> dict[str, Any]:
    """Inspect and fully decode video bytes, preserving the legacy dict shape."""

    probe = probe_video(file_path, decode_all=True)
    data = asdict(probe)
    return {
        "file_path": data["path"],
        "file_size": data["file_size"],
        "duration_seconds": round(data["duration_seconds"], 2),
        "width": data["width"],
        "height": data["height"],
        "fps": round(data["fps"], 2),
        "video_codec": data["codec"],
        "audio_codec": data["audio_codec"],
        "has_audio": data["audio_present"],
        "sha256": data["sha256"],
        "frame_count": data["frame_count"],
    }


def concatenate_videos(
    input_paths: list[str | Path],
    output_path: str | Path,
    target_width: int = 720,
    target_height: int = 1280,
    target_fps: int = 24,
    silent: bool = False,
) -> dict[str, Any]:
    """Concatenate and normalize multiple video clips into a single H.264/AAC MP4.

    Ensures 9:16 vertical resolution (default 720x1280), target fps (24), yuv420p pix_fmt.
    """
    if not input_paths:
        raise ValueError("input_paths list cannot be empty")

    resolved_inputs = [_normalize_file_path(p) for p in input_paths]
    for p in resolved_inputs:
        if not p.is_file() or p.stat().st_size == 0:
            raise FileNotFoundError(f"Input clip not found or empty: {p}")
        probe = probe_video(p, decode_all=True)
        clip_ratio = probe.width / probe.height
        if abs(clip_ratio - (9.0 / 16.0)) > 0.08:
            raise InvalidVideoArtifact(
                f"Square/landscape input rejected: clip {p.name} has geometry {probe.width}x{probe.height} "
                f"(aspect ratio {clip_ratio:.3f}), which violates 9:16 portrait requirement and would distort product proportions"
            )
        if probe.frame_count < 1:
            raise InvalidVideoArtifact(f"Input clip {p.name} contains zero decoded frames")

    out_p = _normalize_file_path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)

    out_container = av.open(str(out_p), mode="w", format="mp4")
    out_v_stream = out_container.add_stream("libx264", rate=target_fps)
    out_v_stream.width = target_width
    out_v_stream.height = target_height
    out_v_stream.pix_fmt = "yuv420p"
    out_v_stream.options = {"crf": "23", "preset": "medium"}

    out_a_stream = None
    audio_by_clip: list[bool] = []
    for p in resolved_inputs:
        with av.open(str(p)) as in_c:
            audio_by_clip.append(any(s.type == "audio" for s in in_c.streams))

    # Mixing voiced and silent clips without silence insertion would shift
    # later narration onto the wrong scene. A silent export is preferable.
    if all(audio_by_clip) and not silent:
        try:
            out_a_stream = out_container.add_stream("aac", rate=44100)
        except Exception:
            out_a_stream = None
    elif any(audio_by_clip):
        logger.warning("Some scene clips have no audio; composing a silent video to avoid voiceover drift")

    current_video_pts = 0
    current_audio_pts = 0
    total_frames_written = 0

    for in_idx, in_file in enumerate(resolved_inputs):
        with av.open(str(in_file)) as in_container:
            in_v_stream = next((s for s in in_container.streams if s.type == "video"), None)
            in_a_stream = next((s for s in in_container.streams if s.type == "audio"), None)

            if in_v_stream is None:
                continue
            source_fps = float(in_v_stream.average_rate or target_fps)
            source_frame_index = 0
            clip_start_pts = current_video_pts

            for packet in in_container.demux(in_v_stream):
                for frame in packet.decode():
                    # Reformat/resize if dimensions don't match
                    if frame.width != target_width or frame.height != target_height or frame.format.name != "yuv420p":
                        reformatted = frame.reformat(
                            width=target_width,
                            height=target_height,
                            format="yuv420p",
                            interpolation="BILINEAR",
                        )
                    else:
                        reformatted = frame

                    # Preserve clip duration when normalizing 24/25/30fps sources.
                    source_frame_index += 1
                    target_end = clip_start_pts + round(source_frame_index * target_fps / source_fps)
                    while current_video_pts < target_end:
                        reformatted.pts = current_video_pts
                        reformatted.time_base = Fraction(1, target_fps)
                        current_video_pts += 1
                        total_frames_written += 1
                        for enc_packet in out_v_stream.encode(reformatted):
                            out_container.mux(enc_packet)

            if in_a_stream is not None and out_a_stream is not None:
                for packet in in_container.demux(in_a_stream):
                    for a_frame in packet.decode():
                        a_frame.pts = current_audio_pts
                        a_frame.time_base = Fraction(1, 44100)
                        current_audio_pts += a_frame.samples
                        for enc_packet in out_a_stream.encode(a_frame):
                            out_container.mux(enc_packet)

    # Flush video encoder
    for enc_packet in out_v_stream.encode():
        out_container.mux(enc_packet)

    # Flush audio encoder if active
    if out_a_stream is not None:
        for enc_packet in out_a_stream.encode():
            out_container.mux(enc_packet)

    out_container.close()

    return get_video_info(out_p)
