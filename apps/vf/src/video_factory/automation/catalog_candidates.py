"""Read-only contract for Catalog V2 product eligibility.

Catalog rows select product identities only. Exchange remains the sole source
for product metadata and image bytes used by the workflow.
"""

from __future__ import annotations

import csv
from pathlib import Path


DEFAULT_CATALOG_V2_CANDIDATES = (
    Path(__file__).resolve().parents[3]
    / "reports"
    / "catalog-normalization"
    / "2026-10-17-v2"
    / "PRODUCT_CATALOG_V2.csv"
)

_REQUIRED_COLUMNS = {"canonical_product_key", "identity_status", "pipeline_status"}

DISALLOWED_CANDIDATE_KEYS = {
    "sandisk:ssd-1tb", "ugreen:100w", "baseus:magnetic-bracket-10000mah",
    "xiaomi:pocket-10000mah", "baseus:heyo", "orico:pw11",
    "xiaomi:redmi-watch-5", "xiaomi:smart-band-9", "xiaomi:mijia-2-bluetooth",
    "ugreen:uno-robot-gan", "soundcore:r50i", "jisulife:pro1-mini",
    "tribit:stormbox-mini", "ugreen:uno-robot-gan:30w",
    "kim:01mm", "ly:1g", "ly:cao", "nam:cao", "nam:xoay",
    "nghe:a026", "nghe:open-ear", "phong:retro", "vi:50x-1000x", "xoay:unko"
}



def load_image_qc_candidate_keys(path: Path) -> tuple[str, ...]:
    """Return Catalog keys eligible for Image QC; reject malformed contracts."""
    statuses = load_candidate_statuses(path)
    return tuple(
        key for key, (identity, pipeline) in statuses.items()
        if identity == "VALID" and pipeline == "READY_FOR_IMAGE_QC"
    )


def load_candidate_statuses(path: Path) -> dict[str, tuple[str, str]]:
    """Return normalized statuses for every row in a Catalog V2 candidate list."""
    candidate_path = Path(path)
    try:
        with candidate_path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            columns = set(reader.fieldnames or ())
            missing = sorted(_REQUIRED_COLUMNS - columns)
            if missing:
                raise ValueError(f"Catalog V2 candidate contract is missing columns: {', '.join(missing)}")
            rows = list(reader)
    except OSError as exc:
        raise ValueError(f"Cannot read Catalog V2 candidate list '{candidate_path}': {exc}") from exc

    statuses: dict[str, tuple[str, str]] = {}
    for row_number, row in enumerate(rows, start=2):
        key = str(row.get("canonical_product_key") or "").strip().casefold()
        if not key:
            raise ValueError(f"Catalog V2 candidate row {row_number} has no canonical_product_key")
        if key in statuses:
            raise ValueError(f"Catalog V2 candidate list has duplicate canonical_product_key '{key}'")
        statuses[key] = (
            str(row.get("identity_status") or "").strip().upper(),
            str(row.get("pipeline_status") or "").strip().upper(),
        )
    return statuses
