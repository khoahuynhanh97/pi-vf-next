"""Typed configuration for Media Intelligence."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel
from dotenv import load_dotenv


load_dotenv()


def _default_data_dir() -> Path:
    import os

    return Path(os.environ.get("PRODUCT_SCOUT_DATA_DIR", _runtime_data_root() / "product_scout"))


def _runtime_data_root() -> Path:
    import os

    configured_root = os.environ.get("PRODUCT_INTELLIGENCE_DATA_DIR")
    if configured_root:
        return Path(configured_root)

    configured_product_scout = os.environ.get("PRODUCT_SCOUT_DATA_DIR")
    if configured_product_scout:
        return Path(configured_product_scout).parent

    return Path(__file__).resolve().parent.parent / "data"


def research_snapshots_db_path() -> Path:
    return _runtime_data_root() / "research" / "snapshots.db"


def resource_pack_locks_db_path() -> Path:
    return _runtime_data_root() / "research" / "resource_pack_locks.db"


def trend_history_db_path() -> Path:
    return _runtime_data_root() / "daily_scout" / "trend_history.db"


class MediaConfig(BaseModel):
    data_dir: Path = _default_data_dir()
    images_dir: Path | None = None
    official_domains_path: Path | None = None

    max_image_bytes: int = 15 * 1024 * 1024
    minimum_short_edge: int = 700
    maximum_aspect_ratio: float = 4.0
    phash_duplicate_distance: int = 8

    initial_search_results: int = 5
    max_search_results: int = 15
    target_selected_assets: int = 4

    max_queries_per_identity: int = 3
    max_candidates_to_download: int = 25
    max_download_bytes_per_product: int = 40 * 1024 * 1024

    download_timeout_seconds: float = 20.0
    product_match_review_threshold: float = 0.5

    @property
    def storage_dir(self) -> Path:
        return self.images_dir or (self.data_dir / "images")

    def image_candidate_provider_name(self) -> str:
        return "duckduckgo_images"
