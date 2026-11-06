"""Download the two public Google Sheets as an immutable daily XLSX pair."""

from __future__ import annotations

import argparse
import json
import tempfile
import urllib.request
from pathlib import Path
from zipfile import BadZipFile

from openpyxl import load_workbook

from tools.selection_handoff import SHEETS
from tools.spaiauto.storage import bangkok_date, digest

MAX_BYTES = 20 * 1024 * 1024


def fetch_public_xlsx(sheet_id: str) -> bytes:
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"
    request = urllib.request.Request(url, headers={"User-Agent": "PI-Sheet-Snapshot/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise ValueError(f"Sheet {sheet_id} returned HTTP {response.status}")
        data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError(f"Sheet {sheet_id} exceeds {MAX_BYTES} bytes")
        return data


def _validate(path: Path) -> None:
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_BYTES:
        raise ValueError(f"Missing or oversized XLSX snapshot: {path}")
    try:
        workbook = load_workbook(path, read_only=False, data_only=False, keep_links=False)
        try:
            if not workbook.worksheets:
                raise ValueError(f"XLSX has no sheets: {path}")
        finally:
            workbook.close()
    except (BadZipFile, KeyError, OSError) as exc:
        raise ValueError(f"Invalid XLSX snapshot: {path}") from exc


def download_snapshots(target_dir: Path, *, fetch=fetch_public_xlsx) -> dict[str, Path]:
    """Publish both snapshots together; a rerun reuses the immutable pair."""
    paths = {sheet_id: target_dir / f"{sheet_id}.xlsx" for sheet_id in SHEETS}
    if target_dir.exists():
        for path in paths.values():
            _validate(path)
        return paths
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".pi-sheets-", dir=target_dir.parent) as temp:
        staging = Path(temp)
        for sheet_id in SHEETS:
            path = staging / f"{sheet_id}.xlsx"
            path.write_bytes(fetch(sheet_id))
            _validate(path)
        staging.rename(target_dir)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Snapshot both configured public Sheets as XLSX")
    parser.add_argument("--output-dir", type=Path, default=Path("data/pi_sheet_daily") / bangkok_date())
    args = parser.parse_args(argv)
    paths = download_snapshots(args.output_dir)
    print(json.dumps({"date": args.output_dir.name, "sources": [
        {"sheet_id": sheet_id, "path": str(path.resolve()), "sha256": digest(path)}
        for sheet_id, path in paths.items()]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
