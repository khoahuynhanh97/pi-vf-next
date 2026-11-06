import io
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from tools.selection_handoff import SHEETS
from tools.sheet_snapshot import download_snapshots


def _xlsx() -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.append(["product_name", "source_url"])
    sheet.append(["Item", "Open product"])
    sheet["B2"].hyperlink = "https://shopee.vn/product/123/456"
    buf = io.BytesIO()
    book.save(buf)
    book.close()
    return buf.getvalue()


def test_downloads_both_public_workbooks_and_keeps_hyperlinks(tmp_path):
    calls = []
    def fetch(sheet_id):
        calls.append(sheet_id)
        return _xlsx()
    paths = download_snapshots(tmp_path / "2026-11-02", fetch=fetch)
    assert calls == list(SHEETS)
    assert set(paths) == set(SHEETS)
    assert all(path.is_file() for path in paths.values())
    book = load_workbook(paths[SHEETS[0]])
    assert book.active["B2"].hyperlink.target == "https://shopee.vn/product/123/456"
    book.close()


def test_second_source_failure_does_not_publish_one_sided_snapshot(tmp_path):
    def fetch(sheet_id):
        if sheet_id == SHEETS[1]:
            raise OSError("source unavailable")
        return _xlsx()
    target = tmp_path / "2026-11-02"
    with pytest.raises(OSError, match="source unavailable"):
        download_snapshots(target, fetch=fetch)
    assert not target.exists()
