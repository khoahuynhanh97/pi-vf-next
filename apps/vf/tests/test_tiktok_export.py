"""Unit tests for Phase 1B TikTok export module."""

from pathlib import Path
import pytest

from video_factory.integrations.tiktok_export import transcode_to_tiktok_standard, _find_ffmpeg


def test_find_ffmpeg():
    """Verify FFmpeg binary can be located."""
    ffmpeg_path = _find_ffmpeg()
    assert Path(ffmpeg_path).is_file() or Path(ffmpeg_path).name.startswith("ffmpeg")


def test_transcode_nonexistent_file(tmp_path: Path):
    """Verify FileNotFoundError is raised when input file does not exist."""
    fake_input = tmp_path / "nonexistent.mp4"
    fake_output = tmp_path / "output.mp4"

    with pytest.raises(FileNotFoundError, match="Input video file not found"):
        transcode_to_tiktok_standard(fake_input, fake_output)
