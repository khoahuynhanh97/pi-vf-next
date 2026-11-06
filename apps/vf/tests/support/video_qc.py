"""Test support helpers for Video QC and media generation."""
from __future__ import annotations

import sqlite3
import tempfile
from fractions import Fraction
from pathlib import Path

import av
from PIL import Image


def _create_synthetic_mp4(path: Path, width: int = 720, height: int = 1280, duration_sec: float = 1.0, fps: int = 24) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    container = av.open(str(path), mode="w")
    stream = container.add_stream("libx264", rate=fps)
    stream.width = width
    stream.height = height
    stream.pix_fmt = "yuv420p"
    stream.time_base = Fraction(1, fps)

    num_frames = int(duration_sec * fps)
    for i in range(num_frames):
        color = ((i * 10) % 255, 128, 200)
        img = Image.new("RGB", (width, height), color=color)
        frame = av.VideoFrame.from_image(img)
        frame.time_base = Fraction(1, fps)
        frame.pts = i
        for packet in stream.encode(frame):
            container.mux(packet)

    for packet in stream.encode():
        container.mux(packet)
    container.close()
