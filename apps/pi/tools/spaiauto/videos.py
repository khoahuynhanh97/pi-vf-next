"""Attach actual browser-downloaded videos; never fetch media URLs implicitly."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from .storage import atomic_json, digest, now, read_json

MAX_VIDEO_BYTES = 300 * 1024 * 1024


def attach_video(run_dir: Path, key: str, source: Path, source_url: str) -> dict:
    parts = urlsplit(source_url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("Record the actual HTTPS video source URL observed in the browser.")
    state = read_json(run_dir / "state.json")
    item = state["items"][key]
    if item["status"] not in ("imported", "reviewed"):
        raise ValueError("Import the product capture before attaching a video.")
    if source.suffix.lower() not in (".mp4", ".webm", ".mov"):
        raise ValueError("Supported video files: MP4, WebM, MOV.")
    if not source.is_file() or not 0 < source.stat().st_size <= MAX_VIDEO_BYTES:
        raise ValueError("Video must be a non-empty local file up to 300 MiB.")
    probe = shutil.which("ffprobe")
    if not probe:
        raise ValueError("ffprobe is required to validate downloaded video files.")
    before = digest(source)
    try:
        result = subprocess.run(
            [probe, "-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries",
             "stream=codec_type,width,height:format=duration", "-of", "json", str(source.resolve())],
            capture_output=True, text=True, timeout=30, check=True,
        )
        metadata = json.loads(result.stdout)
        stream = next(s for s in metadata.get("streams", []) if s.get("codec_type") == "video")
        if not stream.get("width") or not stream.get("height"):
            raise ValueError("No video dimensions found.")
    except (subprocess.SubprocessError, ValueError, StopIteration) as error:
        raise ValueError("File does not contain a readable video stream.") from error
    assets = item.setdefault("videos", [])
    existing = next((asset for asset in assets if asset["sha256"] == before), None)
    if existing:
        verified_videos(run_dir, key, item)
        return existing
    target = run_dir / "items" / key / "videos" / (before + source.suffix.lower())
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    if digest(target) != before or digest(source) != before:
        raise ValueError("Video changed during validation/copy; retry with a stable download.")
    asset = {"asset_id": "vid_" + before[:16], "sha256": before,
             "absolute_path": str(target.resolve()), "local_path": target.relative_to(run_dir).as_posix(),
             "original_url": source_url, "width": stream["width"], "height": stream["height"],
             "duration_seconds": metadata.get("format", {}).get("duration"),
             "status": "downloaded", "review_status": "needs_visual_review", "attached_at": now()}
    assets.append(asset)
    state["events"].append({"at": now(), "action": "attach_video", "item_id": key, "asset_id": asset["asset_id"]})
    atomic_json(run_dir / "state.json", state)
    return asset


def verified_videos(run_dir: Path, key: str, item: dict) -> list[dict]:
    assets = item.get("videos", [])
    root = (run_dir / "items" / key / "videos").resolve()
    for asset in assets:
        path = Path(asset["absolute_path"]).resolve()
        if not path.is_relative_to(root) or digest(path) != asset["sha256"]:
            raise ValueError("Downloaded video changed or is outside the item directory.")
    return assets
