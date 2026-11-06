"""Tests for VideoAI Hub - Human-friendly video deliverable storage."""
from __future__ import annotations

import json
from pathlib import Path
from fractions import Fraction

import av
import pytest

from video_factory.storage.video_hub import (
    sanitize_folder_name,
    mirror_export_to_hub,
    sync_all_exports_to_hub,
    find_latest_video,
)


def _write_video(path: Path, *, frames: int = 3, color: int = 32) -> Path:
    """Helper to create a small test video file."""
    container = av.open(str(path), mode="w", format="mp4")
    stream = container.add_stream("libx264", rate=24)
    stream.width = 64
    stream.height = 96
    stream.pix_fmt = "yuv420p"
    for index in range(frames):
        frame = av.VideoFrame(64, 96, "yuv420p")
        for plane in frame.planes:
            plane.update(bytes([(color + index) % 256]) * plane.buffer_size)
        frame.pts = index
        frame.time_base = Fraction(1, 24)
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    return path


def test_sanitize_folder_name_removes_illegal_characters() -> None:
    """Verify illegal filesystem characters are stripped."""
    assert sanitize_folder_name("Product<>:Name", "Brand") == "[Brand] ProductName"
    assert sanitize_folder_name('Test/Path\\Name', "Xiaomi") == "[Xiaomi] TestPathName"
    assert sanitize_folder_name("Model|With?Pipes*", "JBL") == "[JBL] ModelWithPipes"


def test_sanitize_folder_name_extracts_brand_from_identity() -> None:
    """Verify brand extraction from product_identity."""
    assert sanitize_folder_name("Xiaomi Robot Vacuum E10") == "[Xiaomi] Robot Vacuum E10"
    assert sanitize_folder_name("Baseus: Bowie WM02") == "[Baseus] Bowie WM02"
    assert sanitize_folder_name("JBL - PartyBox 310") == "[JBL] PartyBox 310"


def test_sanitize_folder_name_handles_no_brand() -> None:
    """Verify clean name when no brand is detectable."""
    # When product_identity matches pattern, first word becomes brand
    result = sanitize_folder_name("Generic Product Name")
    assert result == "[Generic] Product Name"
    
    # Single word should not extract brand
    result_single = sanitize_folder_name("Product")
    assert result_single == "Product"


def test_mirror_export_creates_clean_structure(tmp_path: Path) -> None:
    """Verify mirror_export_to_hub creates [Brand] Model folder with all files."""
    # Create mock export directory
    export_dir = tmp_path / "exports" / "export_proj-test_v1"
    export_dir.mkdir(parents=True)
    
    # Create export files
    video = _write_video(export_dir / "final.mp4", frames=24)
    caption = export_dir / "caption.txt"
    caption.write_text("Test caption", encoding="utf-8")
    
    metadata = export_dir / "metadata.json"
    metadata.write_text(
        json.dumps({"product_identity": "JBL PartyBox 310"}),
        encoding="utf-8",
    )
    
    scenes_dir = export_dir / "scenes"
    scenes_dir.mkdir()
    _write_video(scenes_dir / "scene_01.mp4", frames=12, color=50)
    
    keyframes_dir = export_dir / "keyframes"
    keyframes_dir.mkdir()
    (keyframes_dir / "scene_01.png").write_bytes(b"fake_png_data")
    
    # Mirror to hub
    hub_root = tmp_path / "VideoAI"
    target_folder = mirror_export_to_hub(
        export_dir=export_dir,
        product_identity="JBL PartyBox 310",
        hub_root=hub_root,
        brand="JBL",
    )
    
    # Verify structure
    assert target_folder == hub_root / "[JBL] PartyBox 310"
    assert target_folder.exists()
    assert (target_folder / "final.mp4").exists()
    assert (target_folder / "caption.txt").exists()
    assert (target_folder / "metadata.json").exists()
    assert (target_folder / "scenes").exists()
    assert (target_folder / "scenes" / "scene_01.mp4").exists()
    assert (target_folder / "keyframes").exists()
    assert (target_folder / "keyframes" / "scene_01.png").exists()


def test_sync_all_exports_scans_and_mirrors_multiple(tmp_path: Path) -> None:
    """Verify sync_all_exports processes all export directories."""
    exports_root = tmp_path / "exports"
    exports_root.mkdir()
    
    # Create two mock exports
    for i, product in enumerate(["Xiaomi Robot E10", "Baseus Bowie WM02"]):
        export_dir = exports_root / f"export_proj-{i}_v1"
        export_dir.mkdir()
        _write_video(export_dir / "final.mp4", frames=12 + i * 12)
        (export_dir / "caption.txt").write_text(f"Caption {i}", encoding="utf-8")
        (export_dir / "metadata.json").write_text(
            json.dumps({"product_identity": product}),
            encoding="utf-8",
        )
    
    # Sync all
    hub_root = tmp_path / "VideoAI"
    result = sync_all_exports_to_hub(exports_root, hub_root)
    
    # Verify results
    assert result["total_synced"] == 2
    assert result["skipped"] == 0
    assert result["errors"] == 0
    assert len(result["synced_folders"]) == 2
    
    # Verify hub structure
    assert (hub_root / "[Xiaomi] Robot E10" / "final.mp4").exists()
    assert (hub_root / "[Baseus] Bowie WM02" / "final.mp4").exists()


def test_sync_all_exports_skips_invalid_directories(tmp_path: Path) -> None:
    """Verify sync skips exports without metadata.json."""
    exports_root = tmp_path / "exports"
    exports_root.mkdir()
    
    # Create valid export
    valid = exports_root / "export_proj-valid_v1"
    valid.mkdir()
    _write_video(valid / "final.mp4")
    (valid / "metadata.json").write_text(
        json.dumps({"product_identity": "Valid Product"}),
        encoding="utf-8",
    )
    
    # Create invalid export (no metadata)
    invalid = exports_root / "export_proj-invalid_v1"
    invalid.mkdir()
    _write_video(invalid / "final.mp4")
    
    # Sync all
    hub_root = tmp_path / "VideoAI"
    result = sync_all_exports_to_hub(exports_root, hub_root)
    
    assert result["total_synced"] == 1
    assert result["skipped"] == 1
    assert result["errors"] == 0


def test_find_latest_video_returns_most_recent(tmp_path: Path) -> None:
    """Verify find_latest_video returns the most recently modified folder."""
    hub_root = tmp_path / "VideoAI"
    hub_root.mkdir()
    
    # Create two product folders
    older = hub_root / "[Brand] Older Product"
    older.mkdir()
    _write_video(older / "final.mp4")
    
    import time
    time.sleep(0.01)  # Ensure different mtime
    
    newer = hub_root / "[Brand] Newer Product"
    newer.mkdir()
    _write_video(newer / "final.mp4")
    
    # Find latest
    latest = find_latest_video(hub_root)
    assert latest == newer


def test_find_latest_video_filters_by_brand(tmp_path: Path) -> None:
    """Verify find_latest_video can filter by brand."""
    hub_root = tmp_path / "VideoAI"
    hub_root.mkdir()
    
    xiaomi = hub_root / "[Xiaomi] Product"
    xiaomi.mkdir()
    _write_video(xiaomi / "final.mp4")
    
    import time
    time.sleep(0.01)
    
    jbl = hub_root / "[JBL] Product"
    jbl.mkdir()
    _write_video(jbl / "final.mp4")
    
    # Find latest Xiaomi
    latest = find_latest_video(hub_root, brand="Xiaomi")
    assert latest == xiaomi


def test_export_package_integration_mirrors_to_hub(tmp_path: Path) -> None:
    """Verify build_export_package automatically calls mirror_export_to_hub."""
    from video_factory.integrations.export_package import build_export_package
    
    source = _write_video(tmp_path / "source.mp4", frames=72)
    exports_root = tmp_path / "exports"
    hub_root = tmp_path / "VideoAI"
    
    # Patch hub root for this test
    import os
    original_env = os.getenv("HERMES_VIDEO_AI_HUB")
    os.environ["HERMES_VIDEO_AI_HUB"] = str(hub_root)
    
    try:
        package = build_export_package(
            source_video=source,
            exports_root=exports_root,
            project_id="proj-integration",
            version="v1",
            product_identity="Baseus Bowie WM02",
            provenance={"brand": "Baseus"},
        )
        
        # Verify hub folder exists
        hub_folder = hub_root / "[Baseus] Bowie WM02"
        assert hub_folder.exists()
        assert (hub_folder / "final.mp4").exists()
        
        # Verify metadata includes hub path
        metadata = json.loads(package.metadata_path.read_text(encoding="utf-8"))
        assert "video_ai_hub_path" in metadata
        assert metadata["video_ai_hub_path"] == str(hub_folder)
    finally:
        if original_env:
            os.environ["HERMES_VIDEO_AI_HUB"] = original_env
        else:
            os.environ.pop("HERMES_VIDEO_AI_HUB", None)
