"""Bounded browser-image transport for the existing source-correction command.

No workflow transitions live here. The existing handler owns admission and replay.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import shutil
import tempfile
from pathlib import Path

from PIL import Image

MAX_UPLOAD_BODY = 9 * 1024 * 1024


def resolve_image_selection(selection: object, source_images: list[dict], uploads: list[str]) -> list[str]:
    """Bind the ordered draft to this workflow's recorded bytes, never client paths."""
    if not isinstance(selection, list) or not 1 <= len(selection) <= 10:
        raise ValueError("Giữ ít nhất 1 và tối đa 10 ảnh trong bộ chọn")
    known = {str(img.get("sha256") or img.get("digest") or "").removeprefix("sha256:").lower(): img
             for img in source_images if isinstance(img, dict)}
    paths = []
    used_uploads = set()
    for item in selection:
        if isinstance(item, dict) and set(item) == {"sha256"} and isinstance(item["sha256"], str):
            digest = item["sha256"].removeprefix("sha256:").lower()
            original = known.get(digest) if len(digest) == 64 else None
            if not original:
                raise ValueError("Ảnh giữ lại không thuộc bộ ảnh hiện hành; hãy làm mới sản phẩm")
            path = Path(original.get("absolute_path") or original.get("local_path") or original.get("path") or "").resolve()
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("Ảnh nguồn đã mất hoặc thay đổi SHA; hãy chọn lại ảnh từ máy")
            paths.append(str(path))
        elif isinstance(item, dict) and set(item) == {"upload_index"} and type(item["upload_index"]) is int:
            index = item["upload_index"]
            if not 0 <= index < len(uploads) or index in used_uploads:
                raise ValueError("Thứ tự ảnh tải lên không hợp lệ")
            used_uploads.add(index)
            paths.append(uploads[index])
        else:
            raise ValueError("Bộ chọn ảnh không hợp lệ")
    if used_uploads != set(range(len(uploads))):
        raise ValueError("Có ảnh tải lên không nằm trong bộ chọn")
    return paths


def store_uploaded_images(uploads: object, root: Path) -> tuple[Path, list[str]]:
    if not isinstance(uploads, list) or not 1 <= len(uploads) <= 10:
        raise ValueError("Cần từ 1 đến 10 ảnh")
    decoded: list[tuple[bytes, str]] = []
    total = 0
    for item in uploads:
        if not isinstance(item, dict) or not isinstance(item.get("content_base64"), str):
            raise TypeError("Dữ liệu ảnh không hợp lệ")
        try:
            data = base64.b64decode(item["content_base64"], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Nội dung ảnh không phải base64 hợp lệ") from exc
        total += len(data)
        if not data or len(data) > 4 * 1024 * 1024 or total > 6 * 1024 * 1024:
            raise ValueError("Tối đa 4 MB mỗi ảnh và 6 MB tổng")
        try:
            with Image.open(io.BytesIO(data)) as image:
                extension = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(image.format)
                if not extension or image.width * image.height > 20_000_000:
                    raise ValueError("Chỉ nhận JPEG/PNG/WebP, tối đa 20 megapixel")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
        except Exception as exc:
            raise ValueError("Ảnh không đọc được hoặc vượt giới hạn kích thước") from exc
        decoded.append((data, extension))
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="input-", dir=root)).resolve()
    try:
        paths = []
        for index, (data, extension) in enumerate(decoded, 1):
            # Client names never enter server filesystem paths.
            path = directory / f"image-{index}{extension}"
            path.write_bytes(data)
            paths.append(str(path))
        return directory, paths
    except Exception:
        discard_upload(directory, root)
        raise


def discard_upload(directory: Path, root: Path) -> None:
    target = directory.resolve()
    if target.parent != root.resolve() or not target.name.startswith("input-"):
        raise ValueError("Upload cleanup escaped its dedicated directory")
    shutil.rmtree(target)
