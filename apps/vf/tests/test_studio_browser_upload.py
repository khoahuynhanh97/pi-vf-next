import base64
import hashlib
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from video_factory.application.app import create_application
from video_factory.application.studio_uploads import store_uploaded_images
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.repository import WorkflowRepository
from video_factory.workflows.steps import record_pipeline_step


def image_upload(color="red"):
    stream = io.BytesIO()
    Image.new("RGB", (80, 120), color).save(stream, format="PNG")
    return {"name": "../../not-a-server-path.svg", "content_base64": base64.b64encode(stream.getvalue()).decode()}


@pytest.fixture
def studio(tmp_path):
    conn = connect(tmp_path / "studio.db")
    aggregate = {
        "state": "skipped", "owner_user_id": "local-owner", "project_id": "p",
        "source_context": {"source": "hermes_product_video_exchange", "product_key": "p1", "images": []},
        "research_result": {"product_name": "Sản phẩm thử"},
        "failure_code": "IMAGE_QC_FAILED", "failure_reason": "Ảnh không rõ variant",
    }
    WorkflowRepository(conn).commit_workflow("wf-upload", json.dumps(aggregate), [], expected_version=0)
    with TestClient(create_application(kanban_connection=conn)) as client:
        yield client, conn, tmp_path
    conn.close()


URL = "/api/personal/product-video/workflows/wf-upload/source-input"


def existing_images(conn, root):
    images = []
    for color in ("red", "blue"):
        path = root / f"{color}.png"
        Image.new("RGB", (80, 120), color).save(path)
        images.append({"asset_id": color, "absolute_path": str(path),
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    aggregate, version = WorkflowRepository(conn).get_workflow("wf-upload")
    aggregate["source_context"].update(images=images, brand="Brand", model="Snack", variant="Original")
    WorkflowRepository(conn).commit_workflow("wf-upload", json.dumps(aggregate), [], expected_version=version)
    return images


def test_edit_image_selection_keeps_sha_order_and_replays_without_duplicate_job(studio):
    client, conn, root = studio
    old = existing_images(conn, root)
    new = image_upload("green")
    new_sha = hashlib.sha256(base64.b64decode(new["content_base64"])).hexdigest()
    body = {"uploaded_images": [new], "image_selection": [{"upload_index": 0}, {"sha256": old[1]["sha256"]}],
            "product_info": {"variant": "Blue"}, "reason": "Giữ ảnh đúng variant, thêm ảnh rõ hơn", "expected_version": 2}
    response = client.post(URL, json=body, headers={"origin": "http://testserver"})
    assert response.status_code == 200, response.text
    agg, version = WorkflowRepository(conn).get_workflow("wf-upload")
    assert [img["sha256"] for img in agg["source_context"]["images"]] == [new_sha, old[1]["sha256"]]
    assert agg["source_context"]["variant"] == "Blue"
    assert all(hashlib.sha256(Path(img["absolute_path"]).read_bytes()).hexdigest() == img["sha256"] for img in old)
    assert agg["source_input_recovery_history"][0]["previous_failure_reason"] == "Ảnh không rõ variant"
    replay = client.post(URL, json={**body, "expected_version": version}, headers={"origin": "http://testserver"})
    assert replay.status_code == 200, replay.text
    assert replay.json()["reused"] is True
    assert conn.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0] == 1
    assert len(list((root / "studio-inputs").iterdir())) == 1


def test_remove_existing_image_only_preserves_files_and_creates_qc_job(studio):
    client, conn, root = studio
    old = existing_images(conn, root)
    response = client.post(URL, json={"image_selection": [{"sha256": old[1]["sha256"]}], "reason": "Bỏ ảnh sai SKU", "expected_version": 2}, headers={"origin": "http://testserver"})
    assert response.status_code == 200, response.text
    agg, _ = WorkflowRepository(conn).get_workflow("wf-upload")
    assert [img["sha256"] for img in agg["source_context"]["images"]] == [old[1]["sha256"]]
    assert all(Path(img["absolute_path"]).is_file() for img in old)
    assert conn.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0] == 1
    assert not (root / "studio-inputs").exists()


def test_edit_information_only_keeps_current_images_without_upload(studio):
    client, conn, root = studio
    old = existing_images(conn, root)
    body = {"image_selection": [{"sha256": img["sha256"]} for img in old], "uploaded_images": [],
            "product_info": {"name": "Tên sản phẩm đã xác minh"}, "reason": "Sửa tên", "expected_version": 2}
    response = client.post(URL, json=body, headers={"origin": "http://testserver"})
    assert response.status_code == 200, response.text
    agg, _ = WorkflowRepository(conn).get_workflow("wf-upload")
    assert agg["source_context"]["name"] == "Tên sản phẩm đã xác minh"
    assert [img["sha256"] for img in agg["source_context"]["images"]] == [img["sha256"] for img in old]
    assert not (root / "studio-inputs").exists()


@pytest.mark.parametrize("case", ["foreign", "changed", "duplicate", "empty", "bad_index", "unselected_upload"])
def test_image_selection_rejects_unbound_stale_or_invalid_selection(studio, case):
    client, conn, root = studio
    old = existing_images(conn, root)
    selection = [{"sha256": old[0]["sha256"]}]
    body = {"image_selection": selection, "reason": "Ảnh đúng SKU", "expected_version": 2}
    if case == "foreign":
        selection[0]["sha256"] = "f" * 64
    elif case == "changed":
        Image.new("RGB", (80, 120), "yellow").save(old[0]["absolute_path"])
    elif case == "duplicate":
        selection.append(selection[0].copy())
    elif case == "empty":
        selection.clear()
    elif case == "bad_index":
        selection[:] = [{"upload_index": 5}]
    elif case == "unselected_upload":
        body["uploaded_images"] = [image_upload("green")]
    before = WorkflowRepository(conn).get_workflow("wf-upload")
    result = client.post(URL, json=body, headers={"origin": "http://testserver"})
    assert result.status_code == 400, result.text
    assert WorkflowRepository(conn).get_workflow("wf-upload") == before
    assert conn.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0] == 0
    assert not (root / "studio-inputs").exists() or not list((root / "studio-inputs").iterdir())


@pytest.mark.parametrize("blocker", ["locked", "pending", "unknown"])
def test_image_editor_does_not_bypass_existing_workflow_guards(studio, blocker):
    client, conn, root = studio
    old = existing_images(conn, root)
    if blocker == "locked":
        agg, version = WorkflowRepository(conn).get_workflow("wf-upload")
        agg["locked_pack"] = {"resource_pack_id": "locked"}
        WorkflowRepository(conn).commit_workflow("wf-upload", json.dumps(agg), [], expected_version=version)
    elif blocker == "pending":
        conn.execute("INSERT INTO workflow_outbox(workflow_id,message,state,created_at) VALUES('wf-upload','{}','pending',CURRENT_TIMESTAMP)")
        conn.commit()
    else:
        from video_factory.qc.retry_policy import ensure_qc_retry_schema
        ensure_qc_retry_schema(conn)
        conn.execute("INSERT INTO workflow_qc_retries(workflow_id,retry_type,item_id,attempt_number,verdict,status,created_at) VALUES('wf-upload','image','input',1,'UNKNOWN','technical_unknown',CURRENT_TIMESTAMP)")
        conn.commit()
    before = WorkflowRepository(conn).get_workflow("wf-upload")
    result = client.post(URL, json={"image_selection": [{"sha256": old[0]["sha256"]}], "reason": "Bỏ ảnh sai SKU"}, headers={"origin": "http://testserver"})
    assert result.status_code == 400, result.text
    assert WorkflowRepository(conn).get_workflow("wf-upload") == before


@pytest.mark.parametrize("case", ["no_origin", "foreign_origin", "other_owner", "stale_version"])
def test_image_selection_requires_owner_origin_and_current_version(studio, case):
    client, conn, root = studio
    old = existing_images(conn, root)
    if case == "other_owner":
        agg, version = WorkflowRepository(conn).get_workflow("wf-upload")
        agg["owner_user_id"] = "other-owner"
        WorkflowRepository(conn).commit_workflow("wf-upload", json.dumps(agg), [], expected_version=version)
    before = WorkflowRepository(conn).get_workflow("wf-upload")
    headers = {} if case == "no_origin" else {"origin": "http://evil.example" if case == "foreign_origin" else "http://testserver"}
    body = {"image_selection": [{"sha256": old[0]["sha256"]}], "uploaded_images": [image_upload()],
            "reason": "Sửa ảnh", "expected_version": 1 if case == "stale_version" else before[1]}
    result = client.post(URL, json=body, headers=headers)
    assert result.status_code == (409 if case == "stale_version" else 403), result.text
    assert WorkflowRepository(conn).get_workflow("wf-upload") == before
    assert not (root / "studio-inputs").exists()


def test_upload_reuses_existing_handler_and_binds_preview_without_worker(studio):
    client, conn, root = studio
    response = client.post(URL, json={"uploaded_images": [image_upload(), image_upload("blue")], "reason": "Ảnh đúng SKU", "expected_version": 1}, headers={"origin": "http://testserver"})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "building_resource_pack"
    aggregate, _ = WorkflowRepository(conn).get_workflow("wf-upload")
    assert aggregate["source_input_recovery_history"][0]["previous_failure_reason"] == "Ảnh không rõ variant"
    assert conn.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0] == 1
    detail = client.get("/api/operator/workflow/wf-upload").json()
    images = detail["source_context"]["images"]
    assert len(images) == 2
    assert (root / "studio-inputs").is_dir()
    assert client.get(images[0]["asset_url"]).content.startswith(b"\x89PNG")
    assert "not-a-server-path" not in images[0]["absolute_path"]
    assert client.get("/api/personal/product-video/workflows/wf-upload/source-images/unbound").status_code == 404
    # Exact replay keeps the one original outbox entry and discards unused upload bytes.
    replay = client.post(URL, json={"uploaded_images": [image_upload(), image_upload("blue")], "reason": "Ảnh đúng SKU"}, headers={"origin": "http://testserver"})
    assert replay.json()["reused"] is True
    assert len(list((root / "studio-inputs").iterdir())) == 1


@pytest.mark.parametrize("origin", ["http://evil.example", None])
def test_browser_upload_requires_same_origin(studio, origin):
    client, conn, root = studio
    response = client.post(URL, json={"uploaded_images": [image_upload()], "reason": "Đúng SKU"}, headers={"origin": origin} if origin else {})
    assert response.status_code == 403
    assert WorkflowRepository(conn).get_workflow("wf-upload")[0]["state"] == "skipped"
    assert not (root / "studio-inputs").exists()


def test_stale_version_and_running_state_fail_without_changing_workflow(studio):
    client, conn, root = studio
    response = client.post(URL, json={"uploaded_images": [image_upload()], "reason": "Đúng SKU", "expected_version": 0}, headers={"origin": "http://testserver"})
    assert response.status_code == 409
    aggregate, version = WorkflowRepository(conn).get_workflow("wf-upload")
    aggregate["state"] = "rendering"
    WorkflowRepository(conn).commit_workflow("wf-upload", json.dumps(aggregate), [], expected_version=version)
    response = client.post(URL, json={"uploaded_images": [image_upload()], "reason": "Đúng SKU"}, headers={"origin": "http://testserver"})
    assert response.status_code == 400
    assert not list((root / "studio-inputs").iterdir())
    assert conn.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0] == 0


@pytest.mark.parametrize("uploads", [[], [{"content_base64": "!invalid!"}], [{"content_base64": base64.b64encode(b"<svg>bad</svg>").decode()}]])
def test_upload_rejects_empty_invalid_or_non_raster_bytes(tmp_path, uploads):
    with pytest.raises(ValueError):
        store_uploaded_images(uploads, tmp_path / "uploads")
    assert not (tmp_path / "uploads").exists()


def test_detail_and_board_expose_real_journal_without_state_mutation(studio):
    client, conn, _ = studio
    record_pipeline_step(conn, workflow_id="wf-upload", product_key="p1", step_name="IMAGE_QC", status="failed", message="Ảnh mờ", attempt_number=2)
    before = WorkflowRepository(conn).get_workflow("wf-upload")
    detail = client.get("/api/operator/workflow/wf-upload").json()
    assert detail["pipeline_steps"][0]["attempt_number"] == 2
    assert detail["failure_reason"] == "Ảnh không rõ variant"
    columns = client.get("/api/operator/kanban").json()["columns"]
    card = next(card for cards in columns.values() for card in cards)
    assert card["pipeline_steps"][0]["step_name"] == "IMAGE_QC"
    assert card["product_key"] == "p1"
    assert WorkflowRepository(conn).get_workflow("wf-upload") == before


def test_media_preview_binds_exact_frame_owner_and_recorded_sha(studio):
    client, conn, root = studio
    path = root / "keyframe.png"
    Image.new("RGB", (80, 120), "green").save(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    aggregate, version = WorkflowRepository(conn).get_workflow("wf-upload")
    aggregate["storyboard"] = {"generated_keyframes": [{"frame_id": "scene-1-start", "image_path": str(path), "sha256": digest}]}
    WorkflowRepository(conn).commit_workflow("wf-upload", json.dumps(aggregate), [], expected_version=version)
    detail = client.get("/api/operator/workflow/wf-upload").json()
    url = detail["storyboard"]["generated_keyframes"][0]["preview_url"]
    assert client.get(url).content == path.read_bytes()
    assert client.get(url.replace("scene-1-start", "other-frame")).status_code == 404
    Image.new("RGB", (80, 120), "blue").save(path)
    assert client.get(url).status_code == 409
    aggregate, version = WorkflowRepository(conn).get_workflow("wf-upload")
    aggregate["owner_user_id"] = "other-owner"
    WorkflowRepository(conn).commit_workflow("wf-upload", json.dumps(aggregate), [], expected_version=version)
    assert client.get(url).status_code == 403


def test_scene_prompt_projection_and_clip_binding_use_current_render_batch(studio):
    from video_factory.integrations.scene_renderer import SceneRenderOperationStore

    client, conn, root = studio
    SceneRenderOperationStore(conn)  # Existing schema, isolated temporary DB.
    clip = root / "clip.mp4"
    clip.write_bytes(b"fixture bytes; playback not asserted")
    digest = hashlib.sha256(clip.read_bytes()).hexdigest()
    aggregate, version = WorkflowRepository(conn).get_workflow("wf-upload")
    aggregate["render_result"] = {"render_id": "render-current"}
    WorkflowRepository(conn).commit_workflow("wf-upload", json.dumps(aggregate), [], expected_version=version)
    conn.execute(
        "INSERT INTO video_factory_scene_operation(operation_id,owner_user_id,project_id,scene_id,request_digest,status,output_path,output_sha256,render_batch_id,scene_order,request_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("op-current", "local-owner", "p", "scene1", "request", "completed", str(clip), digest, "render-current", 1, json.dumps({"prompt": "Actual stored render prompt", "reference_images": [{"path": "source.png", "sha256": "source-sha"}]})),
    )
    conn.commit()
    detail = client.get("/api/operator/workflow/wf-upload").json()
    operation = detail["scene_operations"][0]
    assert operation["render_prompt"] == "Actual stored render prompt"
    assert client.get(operation["output_url"]).status_code == 200
    aggregate, version = WorkflowRepository(conn).get_workflow("wf-upload")
    aggregate["render_result"] = {"render_id": "another-batch"}
    WorkflowRepository(conn).commit_workflow("wf-upload", json.dumps(aggregate), [], expected_version=version)
    assert client.get(operation["output_url"]).status_code == 404
