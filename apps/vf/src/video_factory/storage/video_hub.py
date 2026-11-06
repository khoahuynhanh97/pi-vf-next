"""Human-First VideoAI Delivery Hub - Clean, browsable video deliverables."""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_VIDEO_AI_HUB = Path(r"D:\work\hermes-data\video-ai-data")
FALLBACK_VIDEO_AI_HUB = Path(r"D:\work\hermes-agent-data\VideoAI")
LEGACY_VIDEO_AI_HUB = Path(r"D:\work\VideoAI")


def get_video_ai_hub_root() -> Path:
    """Resolve VideoAI hub root from environment or config."""
    from video_factory.constants import get_hermes_home
    
    # Check environment variable first
    env_hub = os.getenv("HERMES_VIDEO_AI_HUB")
    if env_hub:
        return Path(env_hub)
    
    # Check config.yaml
    try:
        config_path = get_hermes_home() / "config.yaml"
        if config_path.exists():
            import yaml
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            hub_path = config.get("product_video", {}).get("video_ai_hub")
            if hub_path:
                return Path(hub_path)
    except Exception:
        pass
    
    if DEFAULT_VIDEO_AI_HUB.exists():
        return DEFAULT_VIDEO_AI_HUB
    if FALLBACK_VIDEO_AI_HUB.exists():
        return FALLBACK_VIDEO_AI_HUB
    if LEGACY_VIDEO_AI_HUB.exists():
        return LEGACY_VIDEO_AI_HUB
    return DEFAULT_VIDEO_AI_HUB


def sanitize_folder_name(product_identity: str, brand: str | None = None) -> str:
    """
    Create clean folder name: [Brand] Model or Clean Name.
    
    Removes illegal filesystem characters: < > : " / \\ | ? *
    """
    # Extract brand from product_identity if not provided
    if not brand:
        # Common patterns: "Brand Model", "Brand: Model", "Brand - Model"
        match = re.match(r'^([A-Za-z0-9]+)[\s\-:]+(.+)$', product_identity)
        if match:
            brand = match.group(1)
            model = match.group(2)
        else:
            # No clear brand, use full identity
            model = product_identity
    else:
        # Remove brand prefix if it exists in product_identity
        model = re.sub(f'^{re.escape(brand)}[\\s\\-:]+', '', product_identity, flags=re.IGNORECASE)
    
    # Remove illegal characters
    illegal_chars = r'[<>:"/\\|?*]'
    clean_brand = re.sub(illegal_chars, '', brand) if brand else None
    clean_model = re.sub(illegal_chars, '', model).strip()
    
    # Collapse multiple spaces
    clean_model = re.sub(r'\s+', ' ', clean_model)
    
    if clean_brand:
        return f"[{clean_brand}] {clean_model}"
    return clean_model


def mirror_export_to_hub(
    export_dir: Path,
    product_identity: str,
    hub_root: Path | None = None,
    brand: str | None = None,
) -> Path:
    """
    Mirror export package to human-friendly VideoAI hub.
    
    Creates directory junction (symlink on non-Windows) or copies files
    to hub_root/[Brand] Model/ for easy browsing.
    
    Returns the target hub folder path.
    """
    if hub_root is None:
        hub_root = get_video_ai_hub_root()
    
    folder_name = sanitize_folder_name(product_identity, brand)
    target_folder = hub_root / folder_name
    
    # Create hub root if needed
    hub_root.mkdir(parents=True, exist_ok=True)
    
    # Create target folder
    target_folder.mkdir(parents=True, exist_ok=True)
    
    # Mirror files and directories
    items_to_mirror = [
        ("final.mp4", False),
        ("caption.txt", False),
        ("metadata.json", False),
        ("scenes", True),
        ("keyframes", True),
        ("qc", True),
    ]
    
    for item_name, is_dir in items_to_mirror:
        source = export_dir / item_name
        target = target_folder / item_name
        
        if not source.exists():
            continue
        
        # Remove existing target if it exists
        if target.exists():
            if target.is_dir():
                target.rmdir() if not any(target.iterdir()) else None
            else:
                target.unlink()
        
        try:
            if os.name == 'nt':
                # Windows: Use directory junction for directories, hardlink for files
                if is_dir and source.is_dir():
                    os.system(f'mklink /J "{target}" "{source.resolve()}"')
                elif source.is_file():
                    # Try hardlink first, fall back to copy
                    try:
                        os.link(source, target)
                    except OSError:
                        import shutil
                        shutil.copy2(source, target)
            else:
                # Unix: Use symlinks
                os.symlink(source.resolve(), target)
        except Exception as e:
            logger.warning(f"Failed to link {item_name}, falling back to copy: {e}")
            import shutil
            if is_dir and source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            elif source.is_file():
                shutil.copy2(source, target)
    
    return target_folder


def sync_all_exports_to_hub(
    exports_root: Path,
    hub_root: Path | None = None,
) -> dict[str, Any]:
    """
    Scan all historical exports and mirror them to VideoAI hub.
    
    Returns summary: {total_synced, skipped, errors, synced_folders: [...]}
    """
    if hub_root is None:
        hub_root = get_video_ai_hub_root()
    
    total_synced = 0
    skipped = 0
    errors = 0
    synced_folders = []
    
    if not exports_root.exists():
        return {
            "total_synced": 0,
            "skipped": 0,
            "errors": 1,
            "error_message": f"Exports root does not exist: {exports_root}",
            "synced_folders": [],
        }
    
    # Find all export_* directories
    export_dirs = sorted(exports_root.glob("export_*"))
    
    for export_dir in export_dirs:
        if not export_dir.is_dir():
            continue
        
        metadata_path = export_dir / "metadata.json"
        if not metadata_path.exists():
            skipped += 1
            continue
        
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            product_identity = metadata.get("product_identity")
            brand = metadata.get("provenance", {}).get("brand")
            
            if not product_identity:
                skipped += 1
                continue
            
            target_folder = mirror_export_to_hub(
                export_dir,
                product_identity,
                hub_root,
                brand,
            )
            
            total_synced += 1
            synced_folders.append({
                "product": product_identity,
                "hub_path": str(target_folder),
                "export_dir": str(export_dir.name),
            })
        except Exception as e:
            errors += 1
            logger.error(f"Failed to sync {export_dir.name}: {e}")
    
    return {
        "total_synced": total_synced,
        "skipped": skipped,
        "errors": errors,
        "synced_folders": synced_folders,
    }


def find_latest_video(hub_root: Path | None = None, brand: str | None = None) -> Path | None:
    """Find the most recently modified video folder in the hub."""
    if hub_root is None:
        hub_root = get_video_ai_hub_root()
    
    if not hub_root.exists():
        return None
    
    # Find all product folders
    folders = []
    for folder in hub_root.iterdir():
        if not folder.is_dir():
            continue
        if brand and not folder.name.lower().startswith(f"[{brand.lower()}]"):
            continue
        
        # Check if it has a final.mp4
        if (folder / "final.mp4").exists():
            folders.append(folder)
    
    if not folders:
        return None
    
    # Sort by modification time (most recent first)
    folders.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return folders[0]
