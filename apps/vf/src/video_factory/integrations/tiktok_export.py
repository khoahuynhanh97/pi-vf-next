"""TikTok export integration: FFmpeg-based high-quality upscaler and transcoder.

Converts arbitrary aspect/resolution/framerate videos to TikTok/Reels standard
(1080x1920 @ 30fps) using high-quality Lanczos scaling and H.264/AAC encoding.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def _find_ffmpeg() -> str:
    """Find FFmpeg executable in PATH or standard system locations."""
    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin:
        return ffmpeg_bin

    # Fallback search for common Windows winget installation paths
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        winget_pattern = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
        if winget_pattern.is_dir():
            for match in winget_pattern.glob("**/ffmpeg.exe"):
                if match.is_file():
                    return str(match)

    raise FileNotFoundError(
        "FFmpeg executable not found on system PATH. Please ensure FFmpeg is installed."
    )


def transcode_to_tiktok_standard(
    input_path: Path,
    output_path: Path,
    target_width: int = 1080,
    target_height: int = 1920,
    target_fps: int = 30,
    crf: int = 18,
    preset: str = "slow",
) -> None:
    """Upscale and transcode video to TikTok/Reels standard using FFmpeg.

    Args:
        input_path: Source video (any resolution/fps).
        output_path: Output path for transcoded video.
        target_width: Target width in pixels (default 1080).
        target_height: Target height in pixels (default 1920).
        target_fps: Target frame rate (default 30).
        crf: Constant Rate Factor for quality (18=high, 23=medium, 28=low).
        preset: FFmpeg preset (slow=best quality, medium=balanced, fast=quick).

    Raises:
        FileNotFoundError: If input file doesn't exist or FFmpeg is not found.
        subprocess.CalledProcessError: If FFmpeg command execution fails.
        RuntimeError: If output file is missing or empty after transcode.
    """
    input_path = Path(input_path).resolve()
    output_path = Path(output_path).resolve()

    if not input_path.exists():
        raise FileNotFoundError(f"Input video file not found: {input_path}")

    ffmpeg_bin = _find_ffmpeg()

    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        ffmpeg_bin,
        "-y",
        "-i",
        str(input_path),
        "-vf",
        f"scale={target_width}:{target_height}:flags=lanczos",
        "-r",
        str(target_fps),
        "-c:v",
        "libx264",
        "-preset",
        str(preset),
        "-crf",
        str(crf),
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]

    logger.info("Executing FFmpeg command: %s", " ".join(cmd))

    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if proc.returncode != 0:
        error_msg = (
            f"FFmpeg transcode failed with return code {proc.returncode}.\n"
            f"Command: {' '.join(cmd)}\n"
            f"Stderr:\n{proc.stderr}"
        )
        logger.error(error_msg)
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr
        )

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError(
            f"FFmpeg completed but output file is missing or empty: {output_path}"
        )

    logger.info(
        "Successfully transcoded to TikTok standard: %s (%d bytes)",
        output_path,
        output_path.stat().st_size,
    )
