from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from openpyxl import Workbook

from tools.spaiauto.cli import main
from tools.spaiauto.intake import (finish_request, intake_summary, next_request,
                                  resolve_request, search_attempt, search_failed, start_intake)
from tools.spaiauto.intake_parser import parse_input
from tools.spaiauto.links import link_kind
from tools.spaiauto.storage import SPAIAUTO_URL, atomic_json, item_key, product_url, read_json
from tools.spaiauto.workflow import attach, begin_attempt, review_item

PDP = "https://shopee.vn/product/123/456"
SEARCH = "https://shopee.vn/search?keyword=quat"
FIXTURE = Path(__file__).parent / "fixtures" / "pi-seven-products.md"


def selection_file(tmp_path, price=50000):
    path = tmp_path / "selection.json"
    atomic_json(path, {
        "url": PDP, "observed_title": "Quạt M1", "selected_by": "test operator",
        "reason": "Matched requested features and selected variant in fixture.",
        "observed_price": price, "currency": "VND", "brand": "Example", "model": "M1",
        "criteria_checks": {"requested_features_match": True, "variant_checked": True},
        "candidates": [{"url": PDP, "title": "Quạt M1", "decision": "selected", "reason": "Matches fixture criteria."}],
    })
    return path


def input_file(tmp_path, text=f"Quạt mini | {SEARCH}\nKhay đá | {SEARCH}\n"):
    path = tmp_path / "requests.txt"
    path.write_text(text, encoding="utf-8")
    return path


def test_seven_real_example_rows_keep_first_and_all_constraints():
    rows = parse_input(FIXTURE)
    assert len(rows) == 7
    assert rows[0]["name"].startswith("Blindbox Móc khóa Capybara")
    assert rows[0]["price_min"] == 35000 and rows[0]["price_max"] == 65000
    assert rows[1]["name"].count("/") == 2
    assert rows[1]["alternatives"] == ["Skin1004", "Cocoon", "Sica White"]
    assert rows[1]["price_min"] == 180000
    assert rows[-1]["price_max"] == 110000
    assert all(row["links"][0]["kind"] == "search" for row in rows)
    assert all(len(row["links"]) == 1 for row in rows)
    assert all(len(row["raw_cells"]) == 11 for row in rows)
    assert rows[0]["metadata_verification"] == "user_supplied_unverified"


@pytest.mark.parametrize("url,kind", [
    (PDP, "direct"), ("https://shopee.vn/Quat-mini-i.123.456", "direct"),
    (SEARCH, "search"), ("https://shopee.vn/shop-example", "shop"),
    ("https://shopee.vn/home-cat.110", "category"),
    ("https://s.shopee.vn/abc123", "share"),
    ("https://shop-vn.tiktok.com/pdp/123456789", "direct"),
    ("https://vt.tiktok.com/abc/", "share"),
    ("https://www.tiktok.com/@user/video/123456789", "unsupported"),
    ("https://shopee.vn.evil.example/product/1/2", "unsupported"),
])
def test_link_classification(url, kind):
    assert link_kind(url) == kind


@pytest.mark.parametrize("url", [SEARCH, "https://shopee.vn/shop-example", "https://shopee.vn/home-cat.110"])
def test_search_shop_category_cannot_enter_direct_capture(url):
    with pytest.raises(ValueError, match="resolved"):
        product_url(url)


def test_csv_header_quoting_and_duplicate_rows_preserve_order(tmp_path):
    path = tmp_path / "products.csv"
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["Tên sản phẩm", "Danh mục", "Giá", "Link"])
    writer.writerow(["Quạt, gấp gọn", "Gia dụng", "35k - 65k", SEARCH])
    writer.writerow(["Quạt, gấp gọn", "Gia dụng", "35k - 65k", SEARCH])
    path.write_text(stream.getvalue(), encoding="utf-8")
    rows = parse_input(path)
    assert len(rows) == 2
    assert rows[0]["name"] == "Quạt, gấp gọn"
    assert rows[0]["price_max"] == 65000
    assert rows[1]["duplicate_of"] == "row_0001"


def test_file_headers_and_link_only_rows_preserve_source(tmp_path):
    source = tmp_path / "products.csv"
    source.write_text(f"product_name,Giá bán tham khảo,source_url\n,35000,{PDP}\nQuạt,50000,\n", encoding="utf-8")
    run = tmp_path / "run"
    state = start_intake(source, run)
    assert state["rows"][0]["price_min"] == 35000
    assert state["rows"][0]["name"] == PDP
    assert state["rows"][1]["reason"] == "missing_supported_source_link"
    assert next_request(run)["suggested_search_url"] == PDP
    intake_summary(run)
    payload = read_json(run / "resources.json")
    assert payload["input_sha256"] == state["input_sha256"]
    assert payload["total_products"] == 0


def test_xlsx_real_hyperlink_and_formula_link_are_read_without_evaluation(tmp_path):
    path = tmp_path / "products.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Tên sản phẩm", "Danh mục", "Giá", "Link"])
    sheet.append(["Quạt", "Gia dụng", "35.000 - 65.000 VNĐ", "Mở sản phẩm"])
    sheet["D2"].hyperlink = SEARCH
    sheet.append(["Khay đá", "Gia dụng", "59.000 - 115.000 VNĐ", f'=HYPERLINK("{PDP}","Link")'])
    workbook.save(path)
    workbook.close()
    rows = parse_input(path)
    assert len(rows) == 2
    assert rows[0]["links"] == [{"url": SEARCH, "kind": "search"}]
    assert rows[1]["links"] == [{"url": PDP, "kind": "direct"}]


def test_docx_table_hyperlink_label_and_numbered_fields(tmp_path):
    path = tmp_path / "products.docx"
    xml = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>
    <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Quạt mini</w:t></w:r></w:p></w:tc>
    <w:tc><w:p><w:r><w:t>Gia dụng</w:t></w:r></w:p></w:tc>
    <w:tc><w:p><w:r><w:t>35.000 - 65.000 VNĐ</w:t></w:r></w:p></w:tc>
    <w:tc><w:p><w:hyperlink r:id="r1"><w:r><w:t>Mở link</w:t></w:r></w:hyperlink></w:p></w:tc></w:tr></w:tbl>
    <w:p><w:r><w:t>2. Khay đá</w:t></w:r></w:p>
    <w:p><w:r><w:t>Giá: 59.000 - 115.000 VNĐ</w:t></w:r></w:p>
    </w:body></w:document>'''
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)
        archive.writestr("word/_rels/document.xml.rels", f'''<Relationships>
        <Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
        Target="{PDP}" TargetMode="External"/></Relationships>''')
    rows = parse_input(path)
    assert len(rows) == 2
    assert rows[0]["links"][0]["url"] == PDP
    assert rows[1]["name"] == "Khay đá"
    assert rows[1]["price_min"] == 59000


def test_name_only_requires_link_and_resume_rejects_changed_input(tmp_path):
    source, run = input_file(tmp_path, "Quạt mini\n"), tmp_path / "run"
    state = start_intake(source, run)
    assert next_request(run) is None
    assert "missing_supported_source_link" in state["rows"][0]["parse_warnings"]
    start_intake(source, run)
    source.write_text("Different product", encoding="utf-8")
    with pytest.raises(ValueError, match="Input changed"):
        start_intake(source, run)


def test_second_row_cannot_resolve_until_first_is_closed(tmp_path):
    run = tmp_path / "run"
    start_intake(input_file(tmp_path), run)
    with pytest.raises(ValueError, match="first unfinished"):
        search_attempt(run, "row_0002")
    search_attempt(run, "row_0001")
    resolve_request(run, "row_0001", selection_file(tmp_path))
    with pytest.raises(ValueError, match="first unfinished"):
        search_attempt(run, "row_0002")
    finish_request(run, "row_0001", "needs_review", "Identity not confirmed.")
    assert next_request(run)["row_id"] == "row_0002"


def test_shared_pdp_urls_do_not_collapse_rows_or_child_runs(tmp_path):
    run = tmp_path / "run"
    start_intake(input_file(tmp_path), run)
    children = []
    for row_id in ("row_0001", "row_0002"):
        search_attempt(run, row_id)
        row = resolve_request(run, row_id, selection_file(tmp_path))
        children.append(row["acquisition_run"])
        finish_request(run, row_id, "needs_review", "Test separate request provenance.")
    assert len(set(children)) == 2
    assert next_request(run) is None
    assert intake_summary(run)["total_rows"] == 2


def test_three_failed_search_attempts_advance_without_crawler_fallback(tmp_path):
    run = tmp_path / "run"
    start_intake(input_file(tmp_path), run)
    for _ in range(3):
        search_attempt(run, "row_0001")
        search_failed(run, "row_0001", "login required")
    assert next_request(run)["row_id"] == "row_0002"
    assert not list(run.glob("rows/*/acquisition"))


def test_candidate_url_and_budget_cannot_be_silently_changed(tmp_path):
    run = tmp_path / "run"
    start_intake(input_file(tmp_path, f"| Quạt | Gia dụng | 35.000 - 65.000 VNĐ | {SEARCH} |\n"), run)
    search_attempt(run, "row_0001")
    with pytest.raises(ValueError, match="outside"):
        resolve_request(run, "row_0001", selection_file(tmp_path, price=7900))
    path = selection_file(tmp_path)
    value = read_json(path)
    value["candidates"][0]["url"] = "https://shopee.vn/product/3/4"
    atomic_json(path, value)
    with pytest.raises(ValueError, match="Exactly one"):
        resolve_request(run, "row_0001", path)


def test_changed_selection_blocks_finish_and_summary(tmp_path):
    run = tmp_path / "run"
    start_intake(input_file(tmp_path), run)
    search_attempt(run, "row_0001")
    row = resolve_request(run, "row_0001", selection_file(tmp_path))
    evidence = run / row["selection_file"]
    evidence.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="evidence changed"):
        finish_request(run, "row_0001", "needs_review", "Test tampering")
    summary = intake_summary(run)
    assert summary["rows"][0]["status"] == "integrity_error"
    assert summary["rows"][1]["status"] == "pending_resolution"


def test_completed_requires_actual_capture_and_visual_review(tmp_path):
    run = tmp_path / "run"
    start_intake(input_file(tmp_path, f"Quạt mini | {SEARCH}"), run)
    search_attempt(run, "row_0001")
    row = resolve_request(run, "row_0001", selection_file(tmp_path))
    with pytest.raises(ValueError, match="visually reviewed"):
        finish_request(run, "row_0001", "completed", "Cannot claim acquisition yet.")
    child = Path(row["acquisition_run"])
    key = row["acquisition_item_id"]
    for stage in ("scan", "download"):
        begin_attempt(child, key, stage)
    capture = tmp_path / "capture.json"
    atomic_json(capture, {"schema_version": 1, "acquisition_tool": "spaiauto", "extractor_url": SPAIAUTO_URL,
                "source_url": PDP, "captured_at": "2026-10-23T12:00:00+07:00",
                "title": "Quạt mini M1", "description": "Fixture seller description."})
    stream = io.BytesIO()
    Image.new("RGB", (80, 90), "red").save(stream, format="PNG")
    screenshot = tmp_path / "page.png"
    screenshot.write_bytes(stream.getvalue())
    archive = tmp_path / "images.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("one.png", stream.getvalue())
    imported = attach(child, key, capture, archive, screenshot)
    finish_request(run, "row_0001", "needs_review", "Awaiting identity review.")
    with pytest.raises(ValueError, match="visually reviewed"):
        finish_request(run, "row_0001", "completed", "Not approved yet.", after_review=True)
    review = read_json(imported / "review-template.json")
    review.update(reviewed_by="fixture reviewer", note="Reviewed actual fixture image.", identity_verified=True,
                  identity={"brand": "Example", "model": "M1", "category": "tech"},
                  selected_asset_ids=[read_json(imported / "import.json")["assets"][0]["asset_id"]])
    review_path = tmp_path / "review.json"
    atomic_json(review_path, review)
    review_item(child, key, review_path)
    with pytest.raises(ValueError, match="first unfinished"):
        finish_request(run, "row_0001", "completed", "Requires explicit after-review transition.")
    finish_request(run, "row_0001", "completed", "Actual local fixture capture reviewed.", after_review=True)
    summary = intake_summary(run)
    assert summary["counts"] == {"completed": 1}
    assert read_json(run / "products.json")["products"][0]["id"] == "row_0001"
    resource = read_json(run / "resources.json")["products"][0]
    assert resource["source_row"] == 1
    assert resource["input_sha256"] == read_json(run / "intake.json")["input_sha256"]
    assert (run / resource["images"][0]["local_path"]).is_file()


def test_start_cli_accepts_file_and_reports_first_row(tmp_path, capsys):
    run = tmp_path / "run"
    assert main(["start", "--file", str(FIXTURE), "--run-dir", str(run)]) == 0
    assert '"total_rows": 7' in capsys.readouterr().out
    assert main(["next", "--run-dir", str(run)]) == 0


def test_unsupported_and_empty_inputs_are_actionable_errors(tmp_path):
    with pytest.raises(ValueError, match="No product rows"):
        parse_input(input_file(tmp_path, ""))
    unsupported = tmp_path / "products.pdf"
    unsupported.write_bytes(b"unsupported")
    with pytest.raises(ValueError, match="Supported input"):
        parse_input(unsupported)


def test_brand_alternatives_choose_one_without_expanding_rows(tmp_path):
    run = tmp_path / "run"
    source = input_file(tmp_path, f"Kem chống nắng (Skin1004 / Cocoon / Sica White) | {SEARCH}")
    assert len(start_intake(source, run)["rows"]) == 1
    search_attempt(run, "row_0001")
    path = selection_file(tmp_path)
    with pytest.raises(ValueError, match="chosen_alternative"):
        resolve_request(run, "row_0001", path)
    value = read_json(path)
    value.update(brand="Cocoon", model=None, chosen_alternative="Cocoon")
    atomic_json(path, value)
    assert resolve_request(run, "row_0001", path)["status"] == "resolved"


def test_malformed_docx_and_csv_are_errors_instead_of_partial_success(tmp_path):
    path = tmp_path / "bad.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", "<broken")
    with pytest.raises(ValueError, match="Cannot read"):
        parse_input(path)
    path = tmp_path / "bad.csv"
    path.write_text('Name,Price\n"Unclosed,35.000', encoding="utf-8")
    with pytest.raises(ValueError, match="Cannot read"):
        parse_input(path)


def test_failed_search_keeps_rejected_candidate_evidence(tmp_path):
    run = tmp_path / "run"
    start_intake(input_file(tmp_path), run)
    search_attempt(run, "row_0001")
    evidence = tmp_path / "evidence.json"
    atomic_json(evidence, {"candidates": [{"url": PDP, "title": "Quạt", "decision": "rejected",
                                          "reason": "Actual PDP showed sold out."}]})
    row = search_failed(run, "row_0001", "No suitable listing.", evidence)
    saved = read_json(run / row["search_evidence"][0]["path"])
    assert saved["candidates"][0]["decision"] == "rejected"
    assert row["status"] == "pending_resolution"


def test_resolved_waiting_capture_is_not_reported_as_integrity_failure(tmp_path):
    run = tmp_path / "run"
    start_intake(input_file(tmp_path), run)
    search_attempt(run, "row_0001")
    resolve_request(run, "row_0001", selection_file(tmp_path))
    summary = intake_summary(run)
    assert summary["rows"][0]["status"] == "resolved"
    assert summary["rows"][0]["acquisition_issues"][0]["status"] == "waiting_capture"
    assert read_json(run / "products.json")["total_products"] == 0


def test_excel_preserves_original_sheet_and_row_numbers(tmp_path):
    path = tmp_path / "sheets.xlsx"
    workbook = Workbook()
    workbook.active.title = "First"
    workbook.active.append(["Tên sản phẩm", "Link"])
    workbook.active.append(["Quạt", PDP])
    sheet = workbook.create_sheet("Second")
    sheet.append(["Khay đá", SEARCH])
    workbook.save(path)
    workbook.close()
    rows = parse_input(path)
    assert [(r["source_sheet"], r["source_row"], r["name"]) for r in rows] == [
        ("First", 2, "Quạt"), ("Second", 1, "Khay đá")]


def test_changed_saved_original_prevents_resume(tmp_path):
    run = tmp_path / "run"
    source = input_file(tmp_path)
    state = start_intake(source, run)
    (run / state["input_file"]).write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="Saved original input changed"):
        next_request(run)


def test_original_changing_during_parse_does_not_bind_stale_rows(tmp_path, monkeypatch):
    source, run = input_file(tmp_path), tmp_path / "run"
    original_parser = parse_input
    def mutating_parser(path):
        rows = original_parser(path)
        path.write_text("changed after reading", encoding="utf-8")
        return rows
    monkeypatch.setattr("tools.spaiauto.intake.parse_input", mutating_parser)
    with pytest.raises(ValueError, match="while parsing"):
        start_intake(source, run)
    assert not (run / "intake.json").exists()


def test_missing_name_row_does_not_abort_valid_rows(tmp_path):
    source = tmp_path / "products.csv"
    source.write_text("Tên sản phẩm,Giá,Link\n,35000," + SEARCH + "\nQuạt,50000," + PDP, encoding="utf-8")
    run = tmp_path / "run"
    state = start_intake(source, run)
    assert len(state["rows"]) == 2
    assert state["rows"][0]["status"] == "pending_resolution"
    assert state["rows"][0]["name"] == SEARCH
    assert next_request(run)["row_id"] == "row_0001"


def test_invalid_price_range_is_not_silently_dropped_from_constraints(tmp_path):
    source = input_file(tmp_path, f"| Quạt | Gia dụng | 65.000 - 35.000 VNĐ | {SEARCH} |\n| Khay đá | Gia dụng | 59.000 - 115.000 VNĐ | {SEARCH} |")
    run = tmp_path / "run"
    state = start_intake(source, run)
    assert state["rows"][0]["status"] == "needs_review"
    assert "ambiguous_price_constraint" in state["rows"][0]["parse_warnings"]
    assert next_request(run)["row_id"] == "row_0002"
