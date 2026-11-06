"""Read-only media binding: workflow identity and recorded SHA before playback."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path


def bound_media_path(connection: sqlite3.Connection, workflow_id: str, kind: str, asset_id: str) -> Path:
    row = connection.execute("SELECT aggregate_json FROM workflow WHERE workflow_id=?", (workflow_id,)).fetchone()
    if row is None:
        raise KeyError("Workflow không tồn tại")
    aggregate = json.loads(row["aggregate_json"])
    if aggregate.get("owner_user_id") != "local-owner":
        raise PermissionError("Workflow thuộc người dùng khác")
    path_text = digest = ""
    if kind == "source":
        image = next((image for image in (aggregate.get("source_context") or {}).get("images", [])
                      if isinstance(image, dict) and image.get("asset_id") == asset_id), {})
        path_text = image.get("absolute_path") or image.get("local_path") or image.get("path") or ""
        digest = image.get("sha256") or image.get("digest") or ""
    elif kind == "keyframe":
        storyboard = aggregate.get("storyboard") or {}
        if isinstance(storyboard.get("payload"), str):
            storyboard = json.loads(storyboard["payload"])
        frame = next((f for f in storyboard.get("generated_keyframes", []) if isinstance(f, dict) and f.get("frame_id") == asset_id), {})
        path_text, digest = frame.get("image_path") or "", frame.get("sha256") or ""
    elif kind == "scene":
        batch = (aggregate.get("render_result") or {}).get("render_id")
        try:
            operation = connection.execute(
                "SELECT output_path,output_sha256 FROM video_factory_scene_operation "
                "WHERE operation_id=? AND render_batch_id=? AND owner_user_id=?",
                (asset_id, batch, "local-owner"),
            ).fetchone()
        except sqlite3.OperationalError:
            operation = None
        if operation:
            path_text, digest = operation["output_path"] or "", operation["output_sha256"] or ""
    else:
        raise KeyError("Loại media không được hỗ trợ")
    digest = str(digest).removeprefix("sha256:").lower()
    path = Path(path_text)
    extensions = {".png", ".jpg", ".jpeg", ".webp", ".gif"} if kind in {"keyframe", "source"} else {".mp4", ".webm", ".mov"}
    if not path.is_absolute() or path.suffix.lower() not in extensions or not path.is_file():
        raise KeyError("Media chưa có trên máy phục vụ Studio")
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Chưa có SHA để đối chiếu media")
    with path.open("rb") as handle:
        actual = hashlib.file_digest(handle, "sha256").hexdigest()
    if actual != digest:
        raise ValueError("Media đã thay đổi so với SHA ghi nhận; không phát file này")
    return path
