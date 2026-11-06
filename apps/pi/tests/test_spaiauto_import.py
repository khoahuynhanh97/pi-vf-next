from __future__ import annotations

import io
import json
import zipfile
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from tools.spaiauto.cli import main
from tools.spaiauto.importer import import_capture
from tools.spaiauto.storage import SPAIAUTO_URL, atomic_json, digest, item_key, read_json
from tools.spaiauto.workflow import (attach, begin_attempt, export_run, fail_attempt,
                                    initialize, publish, review_item, skip_item)

URL = "https://shopee.vn/product/123/456"
URL2 = "https://shop-vn.tiktok.com/pdp/123456789"


def test_resources_export_keeps_assets_without_inventing_identity(tmp_path):
    run, key, directory = imported_run(tmp_path)
    export_run(run)
    payload = read_json(run / "resources.json")
    product = payload["products"][0]
    assert payload["schema_version"] == "1.0.0"
    assert product["resource_status"] == "acquired"
    assert product["brand"] is None and product["model"] is None
    assert product["review_status"] == "needs_visual_review"
    assert len(product["images"]) == 1
    assert (run / product["images"][0]["local_path"]).is_file()
    assert product["videos"] == []


def test_actual_video_attachment_export_and_tampering(tmp_path):
    from tools.spaiauto.videos import attach_video

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe required for real video validation")
    run, key, directory = imported_run(tmp_path)
    video = tmp_path / "actual.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=32x32:r=5",
                    "-t", "0.4", "-c:v", "mpeg4", str(video)], check=True, timeout=30)
    asset = attach_video(run, key, video, "https://example.com/product-demo.mp4")
    assert attach_video(run, key, video, "https://example.com/product-demo.mp4") == asset
    export_run(run)
    videos = read_json(run / "resources.json")["products"][0]["videos"]
    assert len(videos) == 1 and videos[0]["width"] == 32
    assert videos[0]["status"] == "downloaded"
    Path(asset["absolute_path"]).write_bytes(b"tampered")
    payload = export_run(run)
    assert payload["total_products"] == 0
    assert payload["issues"][0]["status"] == "integrity_error"
    assert read_json(run / "resources.json")["products"] == []


def test_html_disguised_as_mp4_is_not_a_downloaded_video(tmp_path):
    from tools.spaiauto.videos import attach_video

    if not shutil.which("ffprobe"):
        pytest.skip("ffprobe required")
    run, key, directory = imported_run(tmp_path)
    video = tmp_path / "fake.mp4"
    video.write_text("<html>Login required</html>")
    with pytest.raises(ValueError, match="readable video"):
        attach_video(run, key, video, "https://example.com/demo.mp4")
    assert not read_json(run / "state.json")["items"][key].get("videos")


def image_bytes(color="red"):
    stream = io.BytesIO()
    Image.new("RGB", (80, 90), color).save(stream, format="PNG")
    return stream.getvalue()


def capture_files(tmp_path, *, description="Seller description", members=None, url=URL):
    tmp_path.mkdir(parents=True, exist_ok=True)
    capture = tmp_path / "capture.json"
    atomic_json(capture, {
        "schema_version": 1, "acquisition_tool": "spaiauto", "extractor_url": SPAIAUTO_URL,
        "source_url": url, "captured_at": "2026-10-23T10:00:00+07:00", "title": "Test Model A/B",
        "description": description, "price_display": "100.000₫ - 200.000₫", "currency": "VND",
        "variations_text": "A\nB", "asset_sources": [],
    })
    archive = tmp_path / "images.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for name, content in (members or {"album/one.png": image_bytes()}).items():
            output.writestr(name, content)
    screenshot = tmp_path / "page.png"
    screenshot.write_bytes(image_bytes("white"))
    return capture, archive, screenshot


def imported_run(tmp_path, *, urls=None, description="Seller description", target=5):
    run = tmp_path / "run"
    initialize(run, urls or [URL], target)
    key = item_key(URL)
    begin_attempt(run, key, "scan")
    begin_attempt(run, key, "download")
    directory = attach(run, key, *capture_files(tmp_path / "input", description=description))
    return run, key, directory


def approve(run, key, directory):
    review = read_json(directory / "review-template.json")
    record = read_json(directory / "import.json")
    review.update(reviewed_by="test operator", note="Fixture image reviewed for test identity.",
                  identity_verified=True, identity={"brand": "Example", "model": "M1", "category": "tech"},
                  selected_asset_ids=[record["assets"][0]["asset_id"]])
    path = directory / "filled-review.json"
    atomic_json(path, review)
    review_item(run, key, path)


@pytest.mark.parametrize("name", ["../escape.png", "/absolute.png", "C:/escape.png",
                                  "a/../../escape.png", "a\\..\\escape.png", "a/b:stream.png"])
def test_unsafe_zip_never_extracts_outside_import(tmp_path, name):
    files = capture_files(tmp_path / "input", members={name: image_bytes()})
    with pytest.raises(ValueError, match="Unsafe ZIP"):
        import_capture(*files, tmp_path / "import", URL)
    assert not (tmp_path / "escape.png").exists()


def test_corrupt_duplicate_and_unsupported_files_are_recorded(tmp_path):
    files = capture_files(tmp_path / "input", members={
        "one.png": image_bytes(), "two.png": image_bytes(),
        "bad.jpg": b"bad image", "payload.exe": b"not executed",
    })
    original = [digest(path) for path in files]
    record = import_capture(*files, tmp_path / "import", URL)
    assert len(record["assets"]) == 1
    assert {r["reason"] for r in record["rejected"]} == {"duplicate_sha256", "invalid_image", "unsupported_file"}
    assert [digest(path) for path in files] == original
    assert record["assets"][0]["original_url"] is None


def test_capture_cannot_be_attached_to_other_product(tmp_path):
    files = capture_files(tmp_path / "input")
    with pytest.raises(ValueError, match="does not match"):
        import_capture(*files, tmp_path / "import", URL2)


def test_imports_start_as_draft_without_guessing_model_price_or_selected_images(tmp_path):
    run, key, directory = imported_run(tmp_path)
    product = export_run(run)["products"][0]
    assert product["model"] is None
    assert product["price_min"] is product["price_max"] is None
    assert product["status"] == "needs_review"
    assert product["images"] == []
    assert product["quota_met"] is False
    assert product["claims_verified"] is False
    assert (directory / "machine" / "spaiauto-product.json").exists()
    assert not (directory / "machine" / "research.json").exists()
    assert (directory / "contact-sheet.jpg").exists()


def test_missing_description_blocks_review_and_publish(tmp_path):
    run, key, directory = imported_run(tmp_path, description="")
    with pytest.raises(ValueError, match="Description is missing"):
        approve(run, key, directory)
    with pytest.raises(ValueError, match="pending/failed"):
        publish(run, tmp_path / "inbox")
    assert not (tmp_path / "inbox").exists()


def test_resume_deduplicates_input_and_does_not_reimport_completed_items(tmp_path):
    run, key, directory = imported_run(tmp_path)
    initialize(run, [URL, URL])
    state = read_json(run / "state.json")
    assert len(state["items"]) == 1
    assert state["items"][key]["import_dir"] == str(directory)
    with pytest.raises(ValueError, match="finished"):
        attach(run, key, *capture_files(tmp_path / "again"))


def test_three_stage_failures_skip_item_and_leave_batch_running(tmp_path):
    run = tmp_path / "run"
    initialize(run, [URL, URL2])
    key = item_key(URL)
    for index in range(3):
        begin_attempt(run, key, "scan")
        item = fail_attempt(run, key, "upstream timeout")
        assert item["status"] == ("skipped" if index == 2 else "retry_pending")
    with pytest.raises(ValueError):
        begin_attempt(run, key, "scan")
    assert read_json(run / "state.json")["items"][item_key(URL2)]["status"] == "waiting_capture"


@pytest.mark.parametrize("target", ["raw", "candidate", "manifest", "review", "selected"])
def test_changed_inputs_or_review_cannot_publish(tmp_path, target):
    run, key, directory = imported_run(tmp_path)
    approve(run, key, directory)
    payload = export_run(run)
    paths = {
        "raw": directory / "raw" / "capture.json",
        "candidate": Path(read_json(directory / "import.json")["assets"][0]["absolute_path"]),
        "manifest": directory / "import.json", "review": directory / "review.json",
        "selected": Path(payload["products"][0]["images"][0]["absolute_path"]),
    }
    with paths[target].open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="pending/failed"):
        publish(run, tmp_path / "inbox")
    assert export_run(run)["issues"][0]["status"] == "integrity_error"


def test_reviewed_exchange_is_immutable_complete_and_keeps_provenance(tmp_path):
    run, key, directory = imported_run(tmp_path)
    approve(run, key, directory)
    batch = publish(run, tmp_path / "inbox", "test_batch")
    assert (batch / "READY").exists()
    manifest = read_json(batch / "manifest.json")
    product = manifest["products"][0]
    assert manifest["schema_version"] == "1.0.0"
    assert product["canonical_product_key"] == "example:m1"
    assert product["description"] == "Seller description"
    assert product["quota_met"] is False
    assert product["trend_evidence"] == []
    assert product["claims_verified"] is False
    assert (batch / product["provenance_path"]).is_file()
    for image in product["images"]:
        assert digest(batch / image["relative_path"]) == image["sha256"]
        assert image["match_method"] == "operator_visual_review"
    original = digest(batch / "manifest.json")
    with pytest.raises(ValueError, match="already exists"):
        publish(run, tmp_path / "inbox", "test_batch")
    assert digest(batch / "manifest.json") == original


def test_partial_batch_requires_explicit_eligible_only(tmp_path):
    run, key, directory = imported_run(tmp_path, urls=[URL, URL2])
    approve(run, key, directory)
    skip_item(run, item_key(URL2), "product unavailable")
    with pytest.raises(ValueError, match="pending/failed"):
        publish(run, tmp_path / "inbox")
    batch = publish(run, tmp_path / "inbox", eligible_only=True)
    assert read_json(batch / "manifest.json")["excluded_item_ids"] == [item_key(URL2)]


def test_cli_reports_actionable_errors_without_traceback(tmp_path, capsys):
    assert main(["init", "--url", "https://example.com/product", "--run-dir", str(tmp_path / "run")]) == 2
    assert json.loads(capsys.readouterr().err)["status"] == "error"


def test_corrupt_zip_records_error_without_ending_other_items(tmp_path):
    run = tmp_path / "run"
    initialize(run, [URL, URL2])
    key = item_key(URL)
    for stage in ("scan", "download"):
        begin_attempt(run, key, stage)
    files = capture_files(tmp_path / "input")
    files[1].write_bytes(b"corrupt zip")
    with pytest.raises(zipfile.BadZipFile):
        attach(run, key, *files)
    state = read_json(run / "state.json")
    assert state["items"][key]["status"] == "import_error"
    assert state["items"][item_key(URL2)]["status"] == "waiting_capture"


def test_archive_entry_limit_is_enforced_before_extraction(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.spaiauto.importer.MAX_FILES", 1)
    files = capture_files(tmp_path / "input", members={"one.png": image_bytes(), "two.png": image_bytes("blue")})
    with pytest.raises(ValueError, match="too many"):
        import_capture(*files, tmp_path / "import", URL)
    assert not list((tmp_path / "import" / "media" / "candidates").iterdir())


def test_review_cannot_select_nonexistent_asset(tmp_path):
    run, key, directory = imported_run(tmp_path)
    review = read_json(directory / "review-template.json")
    review.update(reviewed_by="operator", note="Checked", identity_verified=True,
                  identity={"brand": "Example", "model": "M1", "category": "tech"},
                  selected_asset_ids=["invented"])
    atomic_json(directory / "invalid-review.json", review)
    with pytest.raises(ValueError, match="actual imported"):
        review_item(run, key, directory / "invalid-review.json")


def test_missing_local_image_does_not_hide_successful_other_product(tmp_path):
    run, key, directory = imported_run(tmp_path)
    Path(read_json(directory / "import.json")["assets"][0]["absolute_path"]).unlink()
    payload = export_run(run)
    assert payload["products"] == []
    assert payload["issues"][0]["status"] == "integrity_error"


def test_status_distinguishes_capture_work_from_review(tmp_path, capsys):
    run, key, directory = imported_run(tmp_path)
    assert main(["status", "--run-dir", str(run)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["next_items"] == []
    assert result["review_items"] == [key]
