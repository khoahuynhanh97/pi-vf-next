import csv
import io
import json

import pytest
from openpyxl import Workbook

from tools.selection_handoff import SHEETS
from tools.sheet_daily import run_daily


def _workbook(shop, item):
    book = Workbook()
    page = book.active
    page.append(["product_name", "source_url"])
    page.append([f"Product {item}", f"https://shopee.vn/product/{shop}/{item}"])
    output = io.BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


def test_daily_replay_reuses_selection_batch_intake_and_ack(tmp_path):
    workbooks = {SHEETS[0]: _workbook(11, 22), SHEETS[1]: _workbook(33, 44)}
    selection_calls = []

    def complete(messages):
        assert len(json.loads(messages[1]["content"])) == 1
        selection_calls.append(messages)
        return {"selected": [{"candidate_id": "shopee:11:22", "selection_reason": "Clear product listing",
                              "video_concept": "Show the item", "script_draft": "Show package, then product",
                              "claims_to_verify": "Check material"}]}

    kwargs = {"root": tmp_path / "daily", "day": "2026-11-02", "db_path": tmp_path / "test.db",
              "fetch": lambda sheet_id: workbooks[sheet_id], "complete": complete, "candidate_limit": 1}
    first = run_daily(**kwargs)
    second = run_daily(**kwargs)

    assert first == second
    assert first["status"] == "awaiting_acquisition"
    assert first["selected_count"] == 1
    assert len(selection_calls) == 1
    batch = tmp_path / "daily" / "selection_batches" / first["batch_id"]
    assert (batch / "READY").is_file()
    assert (batch / "import_ack.json").is_file()
    assert json.loads((batch / "import_ack.json").read_text(encoding="utf-8"))["rows"][0]["candidate_id"] == "shopee:11:22"
    with (tmp_path / "daily" / "candidate_pool.csv").open(encoding="utf-8-sig", newline="") as stream:
        states = {row["candidate_id"]: row["state"] for row in csv.DictReader(stream)}
    assert states == {"shopee:11:22": "HANDED_OFF", "shopee:33:44": "OPEN"}
    intake = json.loads((tmp_path / "daily" / "pi_runs" / first["batch_id"] / "intake.json").read_text(encoding="utf-8"))
    assert len(intake["rows"]) == 1
    assert intake["rows"][0]["creative_input"]["script_draft"] == "Show package, then product"
    refs = json.loads(intake["rows"][0]["creative_input"]["source_refs_json"])
    assert refs[0]["observed_at_basis"] == "snapshot_file_mtime"



def test_scheduled_browser_failure_records_skip_without_retry(tmp_path, monkeypatch):
    from playwright.sync_api import Error
    from tools.spaiauto import scheduled
    from pathlib import Path

    result = run_daily(root=tmp_path / "daily", day="2026-11-02", db_path=tmp_path / "pi.db",
                       fetch=lambda _: _workbook(11, 22),
                       complete=lambda _: {"selected": [{"candidate_id": "shopee:11:22",
                                                         "selection_reason": "Test candidate"}]})
    run = Path(result["run_dir"])
    calls = []

    def browser_error(url, incoming):
        calls.append(url)
        raise Error("browser unavailable")

    monkeypatch.setattr(scheduled, "_capture", browser_error)
    scheduled.acquire_batch(run, hist_roots=[])
    scheduled.acquire_batch(run, hist_roots=[])
    row = json.loads((run / "intake.json").read_text(encoding="utf8"))["rows"][0]
    assert row["status"] == "skipped"
    assert row["reason_code"] == "browser_error"
    assert row["retryable"] is False
    assert len(calls) == 1



@pytest.mark.parametrize("restart_before_review", [False, True])
def test_scheduled_reviews_extracted_zip_without_source_urls_and_releases_zip(tmp_path, monkeypatch,
                                                                             restart_before_review):
    from pathlib import Path
    from shutil import move
    from io import BytesIO
    from PIL import Image
    from test_spaiauto_import import capture_files
    from tools.spaiauto import scheduled

    intake = run_daily(root=tmp_path / "daily", day="2026-11-02", db_path=tmp_path / "pi.db",
                       fetch=lambda _: _workbook(11, 22),
                       complete=lambda _: {"selected": [{"candidate_id": "shopee:11:22",
                                                         "selection_reason": "Test candidate"}]})
    run = Path(intake["run_dir"])

    captures = []

    def capture(url, incoming):
        captures.append(url)
        image = Image.new("RGB", (80, 90), "red")
        image.paste((0, 100, 255), (0, 0, 40, 90))
        data = BytesIO()
        image.save(data, format="PNG")
        for path, name in zip(capture_files(tmp_path / "browser", url=url, members={"local.png": data.getvalue()}),
                              ("capture.json", "download.zip", "page.png")):
            move(path, incoming / name)

    def complete(messages, *, model):
        context = json.loads(messages[-1]["content"][0]["text"])
        assert context["allowed_asset_ids"]
        assert not list(run.rglob("*.zip"))
        return {"identity_verified": True, "note": "Matches selected product.",
                "identity": {"brand": "Brand", "model": "Model", "variant": "Single", "category": "tech"},
                "selected_asset_ids": context["allowed_asset_ids"]}

    monkeypatch.setattr(scheduled, "_capture", capture)
    if restart_before_review:
        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt()
        monkeypatch.setattr(scheduled, "_complete", interrupted)
        with pytest.raises(KeyboardInterrupt):
            scheduled.acquire_batch(run, hist_roots=[])
        assert not list(run.rglob("*.zip"))
    monkeypatch.setattr(scheduled, "_complete", complete)
    scheduled.acquire_batch(run, hist_roots=[])
    assert len(captures) == 1
    row = json.loads((run / "intake.json").read_text(encoding="utf8"))["rows"][0]
    assert row["status"] == "completed"
    assert not list(run.rglob("*.zip"))
    assert len(list(run.rglob("media/candidates/*.png"))) == 1
    from tools.spaiauto.workflow import publish
    batch = publish(run, tmp_path / "inbox", eligible_only=True)
    manifest = json.loads((batch / "manifest.json").read_text(encoding="utf8"))
    image = manifest["products"][0]["images"][0]
    assert not image.get("original_url")
    assert (batch / image["relative_path"]).is_file()
    from tools.spark_intake import is_verified_real_batch
    assert is_verified_real_batch(batch)[0]
    with (batch / image["relative_path"]).open("ab") as stream:
        stream.write(b"tampered")
    assert not is_verified_real_batch(batch)[0]
