"""Migration utility to backfill selection history from legacy products.json files."""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path

from .identity import canonical_key
from .models import AggregatedCandidate, BackfillSummary
from .repository import TrendHistoryRepository

logger = logging.getLogger(__name__)

_DATE_DIR_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def backfill_daily_runs(
    repository: TrendHistoryRepository,
    runs_dir: Path | str,
    migration_version: str = "daily-products-v1",
) -> BackfillSummary:
    """Read past daily_runs/*/products.json in ascending date order and backfill product history.

    This operation is strictly idempotent; running it multiple times will not duplicate selection counts.
    """
    runs_path = Path(runs_dir).resolve()
    summary = BackfillSummary()

    if not runs_path.exists():
        return summary

    # Collect date directories
    date_dirs: list[tuple[date, Path]] = []
    for entry in runs_path.iterdir():
        if entry.is_dir() and _DATE_DIR_PATTERN.match(entry.name):
            try:
                d = date.fromisoformat(entry.name)
                date_dirs.append((d, entry))
            except ValueError:
                continue

    # Sort ascending by date
    date_dirs.sort(key=lambda pair: pair[0])

    for run_date, day_dir in date_dirs:
        root_file = day_dir / "products.json"
        if not root_file.exists():
            # A run directory without a published root products.json is incomplete or failed; skip
            continue

        try:
            root_content = root_file.read_text(encoding="utf-8")
            root_data = json.loads(root_content)
        except Exception as exc:
            msg = f"Failed to parse root products file {root_file}: {exc}"
            logger.warning(msg)
            summary.files_skipped += 1
            summary.warnings.append(msg)
            continue

        # Strictly ensure backfill ignores sessions marked failed
        run_status = root_data.get("status")
        if run_status == "failed":
            continue

        # Identify published canonical category partitions via root category_summaries
        target_files: list[Path] = []
        cat_summaries = root_data.get("category_summaries")
        if isinstance(cat_summaries, dict) and cat_summaries:
            for cat_id, cat_info in sorted(cat_summaries.items()):
                if isinstance(cat_info, dict) and cat_info.get("status") == "success":
                    cat_file = day_dir / cat_id / "products.json"
                    if cat_file.exists():
                        target_files.append(cat_file)
        else:
            # If category_summaries not present, discover category subdirectories if any
            cat_files = [
                sub / "products.json"
                for sub in sorted(day_dir.iterdir(), key=lambda p: p.name)
                if sub.is_dir() and (sub / "products.json").exists()
            ]
            if cat_files:
                target_files = cat_files

        # Fallback to root products.json if no category partitions defined (legacy v1 daily runs)
        if not target_files:
            target_files = [root_file]

        seen_keys_for_day: set[str] = set()

        for prod_file in target_files:
            try:
                content = prod_file.read_text(encoding="utf-8")
                data = json.loads(content)
            except Exception as exc:
                msg = f"Failed to parse products file {prod_file}: {exc}"
                logger.warning(msg)
                summary.files_skipped += 1
                summary.warnings.append(msg)
                continue

            products = data.get("products")
            if not isinstance(products, list):
                msg = f"Missing products array in {prod_file}"
                logger.warning(msg)
                summary.files_skipped += 1
                summary.warnings.append(msg)
                continue

            # Extract file date if present, otherwise use folder date
            file_date_str = data.get("date")
            actual_date = run_date
            if file_date_str:
                try:
                    actual_date = date.fromisoformat(file_date_str)
                except ValueError:
                    actual_date = run_date

            summary.files_read += 1

            for item in products:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                brand = str(item.get("brand") or "").strip()
                model = str(item.get("model") or "").strip()
                ck = canonical_key(brand, model, name)
                if not ck or ck in seen_keys_for_day:
                    continue

                seen_keys_for_day.add(ck)
                candidate = AggregatedCandidate(
                    canonical_key=ck,
                    name=name or ck,
                    brand=brand,
                    model=model,
                    category=str(item.get("category") or ""),
                )
                imported = repository.apply_backfill_selection(
                    actual_date, candidate, migration_version=migration_version
                )
                if imported:
                    summary.selections_imported += 1

    return summary
