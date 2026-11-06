"""Content-addressed atomic image storage."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from ..config import MediaConfig

_EXT_BY_FORMAT = {
    "JPEG": "jpg",
    "JPG": "jpg",
    "PNG": "png",
    "WEBP": "webp",
    "GIF": "gif",
    "BMP": "bmp",
    "TIFF": "tif",
}


def image_extension(image) -> str:
    return _EXT_BY_FORMAT.get((image.format or "").upper(), "img")


def extension_for_format(image_format: str | None) -> str:
    """Map a decoded Pillow format string to a conventional file extension.

    The extension always derives from the decoded format, never the URL.
    """
    return _EXT_BY_FORMAT.get((image_format or "").upper(), "img")


class ContentAddressedImageStore:
    """Stores payloads under ``<sha256>.<ext>`` atomically.

    The extension always comes from the decoded image format, never the URL
    suffix. Two identical payloads share one physical file.
    """

    def __init__(self, config: MediaConfig):
        self.directory = Path(config.storage_dir)

    def ensure_directory(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)

    def store(self, payload: bytes, ext: str) -> tuple[str, str]:
        self.ensure_directory()
        sha256 = hashlib.sha256(payload).hexdigest()
        filename = f"{sha256}.{ext}"
        destination = self.directory / filename
        if not destination.exists():
            fd, temporary = tempfile.mkstemp(prefix=f".{sha256}.", dir=self.directory)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return sha256, str(destination)
