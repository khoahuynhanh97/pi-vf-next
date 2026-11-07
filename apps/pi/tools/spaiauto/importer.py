"""Import a real browser capture without inventing MCP research results."""

from __future__ import annotations

import math
import re
import shutil
import stat
import warnings
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from PIL import Image, ImageDraw, UnidentifiedImageError

from .storage import SPAIAUTO_URL, atomic_json, digest, now, product_url, read_json

MAX_FILES = 300
MAX_MEMBER = 40 * 1024 * 1024
MAX_TOTAL = 300 * 1024 * 1024
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
LEGACY_SPAIAUTO_URL = "https://tools.spaiauto.com/lay-thong-tin-san-pham"


def validate_capture(data: dict, source_url: str) -> None:
    if data.get("schema_version") != 1 or data.get("acquisition_tool") != "spaiauto":
        raise ValueError("Expected Spaiauto capture schema_version=1.")
    if product_url(data.get("source_url", "")) != product_url(source_url):
        raise ValueError("Capture source URL does not match the queued product.")
    if data.get("extractor_url") not in (SPAIAUTO_URL, LEGACY_SPAIAUTO_URL):
        raise ValueError("Unexpected extractor URL.")
    if not isinstance(data.get("title"), str) or not data["title"].strip():
        raise ValueError("A visible product title is required.")
    captured = datetime.fromisoformat(data.get("captured_at", ""))
    if captured.tzinfo is None:
        raise ValueError("captured_at must include a timezone.")
    for field in ("description", "price_display", "variations_text"):
        if not isinstance(data.get(field, ""), str):
            raise ValueError(f"{field} must be text.")
    assets = data.get("asset_sources", [])
    videos = data.get("video_urls", [])
    if not isinstance(videos, list) or any(not isinstance(url, str) or not url.startswith("https://") for url in videos):
        raise ValueError("video_urls must be a list of observed HTTPS URLs.")
    if not isinstance(assets, list) or any(not isinstance(a, dict) for a in assets):
        raise ValueError("asset_sources must be a list of objects.")
    names = [a.get("archive_member") for a in assets]
    if any(not isinstance(n, str) for n in names) or len(names) != len(set(names)):
        raise ValueError("Each asset source needs a unique explicit archive_member.")
    for asset in assets:
        url = asset.get("original_url")
        if url is not None and (not isinstance(url, str) or not url.startswith("https://")):
            raise ValueError("Asset URLs must be HTTPS or null.")


def _check_archive(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    entries = archive.infolist()
    if len(entries) > MAX_FILES:
        raise ValueError("ZIP contains too many entries.")
    total = 0
    names: set[str] = set()
    for entry in entries:
        name = entry.filename.replace("\\", "/")
        path = PurePosixPath(name)
        parts = name.rstrip("/").split("/")
        if (path.is_absolute() or not parts or any(p in ("", ".", "..") for p in parts)
                or any(re.search(r'[<>:"|?*\x00-\x1f]', p) for p in parts)
                or any(p.endswith((".", " ")) for p in parts)):
            raise ValueError(f"Unsafe ZIP member: {entry.filename!r}")
        if stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
            raise ValueError("Symlink/encrypted ZIP members are unsupported.")
        key = name.casefold().rstrip("/")
        if key in names:
            raise ValueError("Duplicate ZIP member name.")
        names.add(key)
        total += entry.file_size
        if entry.file_size > MAX_MEMBER or total > MAX_TOTAL:
            raise ValueError("ZIP exceeds extraction limits.")
        if entry.file_size > 1024 * 1024 and entry.file_size / max(1, entry.compress_size) > 200:
            raise ValueError("Suspicious ZIP compression ratio.")
    return entries


def contact_sheet(assets: list[dict], output: Path) -> None:
    if not assets:
        return
    width, height, columns = 220, 190, 4
    sheet = Image.new("RGB", (columns * width, math.ceil(len(assets) / columns) * height), "white")
    draw = ImageDraw.Draw(sheet)
    for index, asset in enumerate(assets):
        x, y = (index % columns) * width, (index // columns) * height
        with Image.open(asset["absolute_path"]) as source:
            thumb = source.convert("RGB")
            thumb.thumbnail((width - 12, height - 35))
            sheet.paste(thumb, (x + 6, y + 6))
        draw.text((x + 6, y + height - 27), asset["asset_id"], fill="black")
    sheet.save(output, quality=88)


def import_capture(capture_file: Path, archive_file: Path, screenshot: Path,
                   destination: Path, source_url: str) -> dict:
    """Create one immutable import; destination must not already exist."""
    data = read_json(capture_file)
    validate_capture(data, source_url)
    if archive_file.stat().st_size > MAX_TOTAL:
        raise ValueError("ZIP exceeds download size limit.")
    with Image.open(screenshot) as proof:
        proof.verify()
    destination.mkdir(parents=True, exist_ok=False)
    raw = destination / "raw"
    media = destination / "media" / "candidates"
    raw.mkdir()
    media.mkdir(parents=True)
    shutil.copy2(capture_file, raw / "capture.json")
    shutil.copy2(screenshot, raw / "page.png")
    hashes = {name: digest(raw / name) for name in ("capture.json", "page.png")}
    archive_sha256 = digest(archive_file)
    atomic_json(raw / "checksums.json", hashes)
    assets, rejected = [], []
    sources = {a["archive_member"]: a.get("original_url") for a in data.get("asset_sources", [])}
    seen: dict[str, str] = {}
    with zipfile.ZipFile(archive_file) as archive:
        entries = _check_archive(archive)
        for index, entry in enumerate(entries, start=1):
            if entry.is_dir():
                continue
            suffix = PurePosixPath(entry.filename).suffix.lower()
            if suffix not in IMAGE_EXTENSIONS:
                rejected.append({"archive_member": entry.filename, "reason": "unsupported_file"})
                continue
            target = media / f"candidate_{index:03d}{suffix}"
            with archive.open(entry) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(target) as image:
                        image.verify()
                    with Image.open(target) as image:
                        image.load()
                        width, height = image.size
                        image_format = image.format
            except (OSError, UnidentifiedImageError, Image.DecompressionBombError,
                    Image.DecompressionBombWarning, ValueError) as error:
                rejected.append({"archive_member": entry.filename, "reason": "invalid_image",
                                 "detail": type(error).__name__})
                target.unlink()
                continue
            sha = digest(target)
            if sha in seen:
                rejected.append({"archive_member": entry.filename, "reason": "duplicate_sha256",
                                 "duplicate_of": seen[sha]})
                target.unlink()
                continue
            asset_id = f"img_{sha[:16]}"
            seen[sha] = asset_id
            assets.append({"asset_id": asset_id, "archive_member": entry.filename,
                           "sha256": sha, "width": width, "height": height,
                           "format": image_format.lower(), "absolute_path": str(target.resolve()),
                           "relative_path": target.relative_to(destination).as_posix(),
                           "original_url": sources.get(entry.filename),
                           "match_status": "needs_semantic_review"})
    record = {"schema_version": 1, "imported_at": now(), "capture": data,
              "raw_checksums": hashes, "archive_sha256": archive_sha256,
              "assets": assets, "rejected": rejected}
    atomic_json(destination / "import.json", record)
    atomic_json(destination / "review-template.json", {
        "capture_digest": digest(raw / "capture.json"), "reviewed_by": "", "note": "",
        "identity": {"brand": "", "model": "", "variant": None, "category": ""},
        "identity_verified": False, "selected_asset_ids": [],
    })
    contact_sheet(assets, destination / "contact-sheet.jpg")
    return record


def verify_import(destination: Path) -> dict:
    record = read_json(destination / "import.json")
    for name, expected in record["raw_checksums"].items():
        if name not in ("capture.json", "download.zip", "page.png"):
            raise ValueError("Unexpected raw checksum entry.")
        if digest(destination / "raw" / name) != expected:
            raise ValueError(f"Raw input changed: {name}")
    if read_json(destination / "raw" / "capture.json") != record["capture"]:
        raise ValueError("Imported metadata differs from raw capture.")
    for asset in record["assets"]:
        relative = asset.get("relative_path")
        path = (destination / relative).resolve() if relative else Path(asset["absolute_path"]).resolve()
        if not path.is_relative_to((destination / "media" / "candidates").resolve()):
            raise ValueError("Asset path outside import directory.")
        if digest(path) != asset["sha256"]:
            raise ValueError(f"Image changed: {asset['asset_id']}")
        asset["absolute_path"] = str(path)
    return record
