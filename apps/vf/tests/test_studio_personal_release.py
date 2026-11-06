from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from video_factory.application.app import create_application
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.repository import WorkflowRepository
from test_export_integrity import _write_valid_video
from test_source_input_correction import _skipped_image_workflow


def test_mp4_playback_download_range_and_changed_bytes(tmp_path: Path):
    conn = connect(tmp_path / "media.db")
    path = _write_valid_video(tmp_path / "final.mp4")
    expected = path.read_bytes()
    aggregate = {"state": "completed", "export_result": {"payload": json.dumps({
        "file_path": str(path), "sha256": hashlib.sha256(expected).hexdigest(),
    })}}
    WorkflowRepository(conn).commit_workflow("wf-media", json.dumps(aggregate), [], expected_version=0)
    with TestClient(create_application(kanban_connection=conn)) as client:
        listing = client.get("/api/personal/product-video/videos").json()["videos"][0]
        assert listing["result_status"] == "completed"
        url = listing["video_url"]
        assert url.startswith("/api/")
        response = client.get(url)
        assert response.status_code == 200 and response.content == expected
        assert response.headers["content-type"] == "video/mp4"
        download = client.get(url + "?download=true")
        assert "attachment" in download.headers["content-disposition"]
        partial = client.get(url, headers={"Range": "bytes=0-31"})
        assert partial.status_code == 206 and partial.content == expected[:32]
        path.write_bytes(b"broken export")
        assert client.get(url).status_code == 409
        listing = client.get("/api/personal/product-video/videos").json()["videos"][0]
        assert listing["result_status"] == "technical_error" and listing["video_url"] is None
        assert client.get("/api/personal/product-video/videos/missing/media").status_code == 404
    conn.close()


def test_source_correction_api_replays_and_preserves_history(tmp_path: Path):
    conn = connect(tmp_path / "correction.db")
    _skipped_image_workflow(conn)
    image = tmp_path / "replacement.png"
    Image.new("RGB", (100, 160), "red").save(image)
    payload = {"image_paths": [str(image)], "reason": "Verified correct variant", "product_info": {"brand": "Tao Kae Noi", "model": "Seaweed Snack", "variant": "classic"}}
    with TestClient(create_application(kanban_connection=conn)) as client:
        url = "/api/personal/product-video/workflows/wf-input-fix/source-input"
        assert client.post(url, json=payload, headers={"Origin": "https://other.example"}).status_code == 403
        response = client.post(url, json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "building_resource_pack"
        assert client.post(url, json=payload).json()["reused"] is True
        assert conn.execute("SELECT count(*) FROM workflow_outbox").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM workflow_event").fetchone()[0] == 1
        aggregate = WorkflowRepository(conn).get_workflow("wf-input-fix")[0]
        assert aggregate["failure_report"] == {"old_report": "preserve"}
        assert client.post(url, json={"image_paths": "bad"}).status_code == 400
    conn.close()


def test_source_correction_api_refuses_active_work(tmp_path: Path):
    conn = connect(tmp_path / "active.db")
    _skipped_image_workflow(conn)
    image = tmp_path / "replacement.png"
    Image.new("RGB", (100, 160), "red").save(image)
    conn.execute("INSERT INTO workflow_outbox(workflow_id,message,state,created_at) VALUES(?,?,'pending',datetime('now'))", ("wf-input-fix", "{}"))
    conn.commit()
    with TestClient(create_application(kanban_connection=conn)) as client:
        response = client.post("/api/personal/product-video/workflows/wf-input-fix/source-input", json={"image_paths": [str(image)], "reason": "Changed image"})
        assert response.status_code == 400 and "active outbox" in response.text
        assert WorkflowRepository(conn).get_workflow("wf-input-fix")[0]["state"] == "skipped"
    conn.close()


def test_start_rejects_malformed_inputs_as_client_errors(tmp_path: Path):
    conn = connect(tmp_path / "bad-input.db")
    with TestClient(create_application(kanban_connection=conn)) as client:
        for payload in ({"minimum_daily": "bad"}, {"target_date": "bad"}):
            assert client.post("/api/personal/product-video/start-batch", json=payload).status_code == 400
    conn.close()


def test_studio_modules_have_javascript_mime_even_with_windows_registry_override(tmp_path: Path):
    import mimetypes
    mimetypes.add_type("text/plain", ".mjs")
    conn = connect(tmp_path / "mime.db")
    with TestClient(create_application(kanban_connection=conn)) as client:
        for module in ["studio.mjs", "studio-model.mjs", "studio-api.mjs"]:
            response = client.get("/operator/assets/" + module)
            assert response.status_code == 200
            assert response.headers["content-type"].split(";")[0] == "application/javascript"
    conn.close()


def test_skipped_workflow_is_never_in_production_column(tmp_path: Path):
    from video_factory.application.kanban_api import _get_kanban_cards
    conn = connect(tmp_path / "skipped-board.db")
    _skipped_image_workflow(conn)
    cards = _get_kanban_cards(conn)
    assert not cards["in_production"]
    assert cards["error"][0]["workflow_id"] == "wf-input-fix"
    conn.close()


def test_invalid_profile_and_concurrent_start_do_not_dispatch(tmp_path: Path, monkeypatch):
    from test_studio_creative_batch import _ready_two_product_batch
    from video_factory.application import candidate_api
    inbox, keys = _ready_two_product_batch(tmp_path)
    monkeypatch.setenv("HERMES_EXCHANGE_INBOX", str(inbox))
    profile = tmp_path / "bad-profile.json"
    profile.write_text("{}")
    monkeypatch.setenv("VF_CREATIVE_RUN_CONFIG", str(profile))
    monkeypatch.setattr(candidate_api, "run_studio_selected_batch", lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not dispatch")))
    conn = connect(tmp_path / "preflight.db")
    with TestClient(create_application(kanban_connection=conn)) as client:
        payload = {"product_keys": [keys["Bibigo"]], "target_date": "2026-10-25"}
        response = client.post("/api/personal/product-video/start-batch", json=payload)
        assert response.status_code == 400 and "profile" in response.json()["error"]
        profile.write_text((Path(__file__).parents[1] / "config/creative_run_profile.json").read_text())
        assert candidate_api._studio_batch_lock.acquire(blocking=False)
        try:
            assert client.post("/api/personal/product-video/start-batch", json=payload).status_code == 409
        finally:
            candidate_api._studio_batch_lock.release()
    conn.close()
