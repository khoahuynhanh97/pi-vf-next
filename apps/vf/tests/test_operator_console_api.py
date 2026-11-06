from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from video_factory.application.operator_handler import _current_qc_digest
from video_factory.automation.contracts import compute_product_key
from video_factory.automation.importer import publish_daily_run_to_exchange
from video_factory.handlers import (
    ProductVideoWorkflowState,
    start_product_video_workflow,
)
from video_factory.qc.models import QCReport, QCStage, QCVerdict
from video_factory.qc.repository import QCReportRepository
from video_factory.application.app import create_application
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.approvals import ApprovalService, ApprovalStatus
from video_factory.workflows.envelope import parse_envelope
from video_factory.workflows.repository import WorkflowRepository


def create_gateway_application(database_path=None, api_key=None, provider_mode=None):
    conn = connect(database_path) if database_path else None
    return create_application(kanban_connection=conn)


def _write_image(path: Path, color: tuple[int, int, int] = (200, 200, 200)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 400), color=color).save(path, format="PNG")


def _seed_pass_image_qc(conn, workflow_id: str, artifact: dict) -> None:
    QCReportRepository(conn).save_report(
        QCReport(
            report_id=f"qc_image_{workflow_id}",
            workflow_id=workflow_id,
            stage=QCStage.IMAGE,
            artifact_id=str(artifact.get("artifact_id") or workflow_id),
            artifact_version=1,
            artifact_digest=_current_qc_digest(QCStage.IMAGE, artifact),
            reference_digests=[],
            evaluator_name="test_seeded_qc",
            evaluator_version="1",
            rubric_version="1",
            created_at="2026-09-15T00:00:00+00:00",
            score=90,
            verdict=QCVerdict.PASS,
            findings={"seeded_for": "operator console approval guard"},
        )
    )


def _setup_test_exchange_batch(tmp_path: Path) -> tuple[Path, Path]:
    src_run = tmp_path / "src_run"
    src_run.mkdir(parents=True, exist_ok=True)

    products = []
    # One usable image is sufficient; distinct pixels keep two-image test assets unique.
    for i, (brand, model) in enumerate([
        ("Jisulife", "Life 7"),
        ("AULA", "F75"),
        ("Baseus", "Bowie WM02"),
    ], start=1):
        img1 = src_run / f"img_{i}_1.png"
        img2 = src_run / f"img_{i}_2.png"
        _write_image(img1, (i * 30, 20, 20))
        _write_image(img2, (20, i * 30, 20))
        image_entries = [
            {"asset_id": f"a_{i}_1", "absolute_path": str(img1), "match_score": 1.0},
            {"asset_id": f"a_{i}_2", "absolute_path": str(img2), "match_score": 1.0},
        ]
        if brand == "Baseus":
            image_entries = image_entries[:1]
        products.append({
            "id": f"prod_{i:02d}",
            "name": f"{brand} {model}",
            "brand": brand,
            "model": model,
            "price_display": f"{i * 100}.000đ",
            "hot_reason": f"Trending product #{i}",
            "key_features": "Fast, durable, wireless",
            "keywords": [f"{brand} {model}"],
            "images": image_entries,
        })

    # Product 4: Invalid (no technically readable image)
    img_inv = src_run / "img_inv_1.png"
    products.append({
        "id": "prod_04_inv",
        "name": "Generic Invalid Earbuds",
        "brand": "Generic",
        "model": "BadPack",
        "price_display": "50.000đ",
        "hot_reason": "Cheap",
        "images": [{"asset_id": "a_inv_1", "absolute_path": str(img_inv), "match_score": 1.0}],
    })

    (src_run / "products.json").write_text(
        json.dumps({"version": "1.0", "date": "2026-09-12", "products": products}, ensure_ascii=False),
        encoding="utf-8",
    )

    inbox = tmp_path / "exchange_inbox"
    publish_daily_run_to_exchange(src_run, inbox, batch_id="run_console_test")
    return src_run, inbox


def test_operator_console_full_lifecycle(tmp_path: Path, monkeypatch) -> None:
    _, inbox = _setup_test_exchange_batch(tmp_path)
    monkeypatch.setenv("HERMES_EXCHANGE_INBOX", str(inbox))

    db_path = tmp_path / "runtime.sqlite3"
    app = create_gateway_application(
        database_path=db_path,
        api_key="test-api-key",
        provider_mode="fake",
    )

    with TestClient(app) as client:
        # 1. Console HTML route serves valid operator page
        console_res = client.get("/personal/product-video")
        assert console_res.status_code == 200
        assert "Product Video Operator Console" in console_res.text
        assert "Auto Start" in console_res.text

        # 2. Candidate list rendering / API mapping
        res = client.get("/api/personal/product-video/candidates")
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        candidates = data["candidates"]
        assert len(candidates) == 4

        # Check statuses: 3 unprocessed, 1 invalid
        unprocessed = [c for c in candidates if c["status"] == "unprocessed"]
        invalid = [c for c in candidates if c["status"] == "invalid"]
        assert len(unprocessed) == 3
        assert len(invalid) == 1
        single_image_candidate = next(c for c in unprocessed if c["model"] == "Bowie WM02")
        assert len(single_image_candidate["images"]) == 1
        assert "no technically readable" in invalid[0]["rejection_reason"]

        # Legacy manual selection remains a service contract. Studio now uses
        # async PR-1 dispatch, verified separately in test_studio_creative_batch.
        from video_factory.candidate_service import DailyCandidateService
        selection_conn = connect(db_path)
        service = DailyCandidateService(selection_conn)
        inv_key = invalid[0]["product_key"]
        inv_data = service.start_selection([inv_key]).to_dict()
        assert len(inv_data["started"]) == 0
        assert len(inv_data["skipped"]) == 1
        assert "INVALID_ADMISSION" in inv_data["skipped"][0]["reason"]

        p1_key = unprocessed[0]["product_key"]
        batch_data = service.start_selection([p1_key, "non_existent_key_12345"]).to_dict()
        assert len(batch_data["started"]) == 1
        assert batch_data["started"][0]["product_key"] == p1_key
        assert batch_data["skipped"][0]["reason"] == "NOT_FOUND_IN_MANIFEST"
        started_wf_id = batch_data["started"][0]["workflow_id"]
        duplicate = service.start_selection([p1_key]).to_dict()
        assert not duplicate["started"]
        assert "ALREADY" in duplicate["skipped"][0]["reason"]
        auto_data = service.start_auto(limit=2).to_dict()
        assert len(auto_data["started"]) == 1
        assert auto_data["started"][0]["canonical_product_key"] == "aula:f75"
        selection_conn.close()

        # 7. Workflow detail uses the current operator API contract.
        wf_res = client.get(f"/api/operator/workflow/{started_wf_id}")
        assert wf_res.status_code == 200, wf_res.text
        wf = wf_res.json()
        assert wf["workflow_id"] == started_wf_id
        assert wf["state"] == "building_resource_pack"
        assert wf["source_context"] is not None
        assert wf["source_context"]["brand"] == "Jisulife"


        # 8. Setup workflow into AWAITING_PRODUCT_LOCK and verify review details
        conn = connect(db_path)
        wf_repo = WorkflowRepository(conn)
        app_svc = ApprovalService(conn)

        # Mock research completion into AwaitingProductLock
        research_dossier = {
            "summary": "Top selling handheld fan with LED screen",
            "snapshot_id": "snap_test_123",
            "status": "researched",
            "selling_points": ["5000mAh battery", "19h runtime", "Pocket foldable"],
            "competitors": ["Baseus Mini", "Xiaomi Fan"],
        }
        req_id = app_svc.submit_request({
            "workflow_id": started_wf_id,
            "project_id": f"proj-{started_wf_id}",
            "gate": ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value,
            "workflow_version": 2,
            "artifact_id": "snap_test_123",
            "artifact_version": 1,
            "artifact_ref": {"report": json.dumps(research_dossier), "summary": "Top selling"},
            "owner_user_id": "local-owner",
        })
        agg_lock = {
            "state": ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value,
            "project_id": f"proj-{started_wf_id}",
            "owner_user_id": "local-owner",
            "product_key": p1_key,
            "pending_approval_id": req_id,
            "pending_approval_gate": ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value,
            "latest_artifact": {"report": json.dumps(research_dossier), "summary": "Top selling"},
            "source_context": wf["source_context"],
        }
        wf_repo.commit_workflow(started_wf_id, json.dumps(agg_lock), [], expected_version=1)
        _seed_pass_image_qc(conn, started_wf_id, agg_lock["latest_artifact"])
        conn.close()

        # Check candidate list now displays AWAITING_PRODUCT_LOCK
        c_refresh = client.get("/api/personal/product-video/candidates").json()
        jisulife_cand = next(c for c in c_refresh["candidates"] if c["product_key"] == p1_key)
        assert jisulife_cand["status"] == "awaiting_product_lock"
        assert jisulife_cand["approval_id"] == req_id

        # 9. GET workflow detail exposes research snapshot evidence and approval
        wf_lock_res = client.get(f"/api/operator/workflow/{started_wf_id}")
        assert wf_lock_res.status_code == 200
        wf_lock = wf_lock_res.json()
        assert wf_lock["state"] == ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value
        assert wf_lock["pending_approval_id"] == req_id

        # 10. Approve Product Lock through the current operator endpoint.
        approve_res = client.post(
            "/api/operator/approve",
            json={"workflow_id": started_wf_id, "gate": "AwaitingProductLock", "action": "approve"},
        )
        assert approve_res.status_code == 200
        approve_data = approve_res.json()
        assert approve_data

        # Verify state in DB
        wf_after_res = client.get(f"/api/operator/workflow/{started_wf_id}")
        wf_after = wf_after_res.json()
        assert wf_after["state"] == "locking_resource_pack"
        assert wf_after["pending_approval_id"] is None

        # Verify Outbox task
        conn2 = connect(db_path)
        outbox_row = conn2.execute("SELECT message FROM workflow_outbox WHERE workflow_id = ? ORDER BY rowid DESC LIMIT 1", (started_wf_id,)).fetchone()
        assert outbox_row is not None
        env = parse_envelope(outbox_row[0])
        assert env.topic == "job.dispatch"
        assert env.payload["approval_request_id"] == req_id
        conn2.close()


def test_operator_console_asset_serving_and_path_traversal_protection(tmp_path: Path, monkeypatch) -> None:
    src_run, inbox_root = _setup_test_exchange_batch(tmp_path)
    monkeypatch.setenv("HERMES_EXCHANGE_INBOX", str(inbox_root))

    db_path = tmp_path / "hermes_assets.db"
    app = create_gateway_application(
        database_path=db_path,
        api_key="test-api-key",
        provider_mode="fake",
    )
    with TestClient(app) as client:
        # Discover candidate to get real asset_url
        cands_res = client.get("/api/personal/product-video/candidates")
        assert cands_res.status_code == 200
        candidates = cands_res.json()["candidates"]
        jisulife = next(c for c in candidates if c["model"] == "Life 7")
        assert len(jisulife["images"]) >= 2

        asset_url = jisulife["images"][0]["asset_url"]
        assert asset_url.startswith("/api/personal/product-video/assets/")

        # 1. Valid asset fetch -> HTTP 200 with image MIME type
        asset_res = client.get(asset_url)
        assert asset_res.status_code == 200
        assert asset_res.headers["content-type"].startswith("image/")
        assert len(asset_res.content) > 0

        # 2. Path traversal attempt using .. -> HTTP 403 Forbidden
        date_part = jisulife["source_date"]
        batch_part = jisulife["source_batch_id"]
        traversal_url = f"/api/personal/product-video/assets/{date_part}/{batch_part}/../../secret.txt"
        bad_res = client.get(traversal_url)
        assert bad_res.status_code in (403, 404)

        # 3. Path traversal outside batch directory -> rejected (400 Bad Request or 403 Forbidden)
        outside_res = client.get(f"/api/personal/product-video/assets/{date_part}/{batch_part}/../manifest.json")
        assert outside_res.status_code in (400, 403)

        # 4. Invalid date / batch regex format -> HTTP 400 Bad Request
        invalid_format_res = client.get(f"/api/personal/product-video/assets/invalid-date/{batch_part}/img.png")
        assert invalid_format_res.status_code == 400

        # 5. Unsupported file extension (non-image) -> HTTP 400 Bad Request
        text_file = inbox_root / date_part / batch_part / "notes.txt"
        text_file.write_text("unsupported file", encoding="utf-8")
        non_img_res = client.get(f"/api/personal/product-video/assets/{date_part}/{batch_part}/notes.txt")
        assert non_img_res.status_code == 400


def test_operator_console_anti_xss_and_rendering_safety() -> None:
    from video_factory.operator_console import render_operator_console_html

    hostile_candidates = [
        {
            "priority": 1,
            "source_date": "2026-09-12",
            "source_batch_id": "run_test",
            "source_product_id": "p_xss",
            "canonical_product_key": "xss:test",
            "product_key": "key_xss_123",
            "name": '<script>alert("pwned_name")</script>',
            "brand": '<b>EvilBrand</b>',
            "model": '<img src=x onerror=alert(1)>',
            "variant": '"><script>alert(2)</script>',
            "price_display": '<script>alert(3)</script>',
            "hot_reason": '"><svg onload=alert(4)>',
            "key_features": "test features",
            "keywords": ["test"],
            "images": [
                {
                    "asset_id": "img1",
                    "asset_url": "javascript:alert(5)",
                    "original_url": "javascript:alert(6)",
                }
            ],
            "status": "unprocessed",
        }
    ]

    html = render_operator_console_html(hostile_candidates)

    # 1. Script tag breakout is strictly neutralized in JSON injection (< -> \u003c)
    assert '<script>alert' not in html
    assert r'\u003cscript>alert' in html

    # 2. escapeHtml and sanitizeUrl helpers are embedded in the client script
    assert "function escapeHtml" in html
    assert "function sanitizeUrl" in html
    assert "safePkey" in html
    assert "safeName" in html
    assert "safeBrand" in html


def test_studio_route_serves_the_single_page_application(tmp_path: Path) -> None:
    app = create_gateway_application(database_path=tmp_path / "studio.sqlite3")

    with TestClient(app) as client:
        response = client.get("/studio")
        script = client.get("/operator/assets/studio.mjs")
        styles = client.get("/operator/assets/studio.css")

    assert response.status_code == 200
    assert 'src="./studio.mjs"' in response.text
    assert 'href="./studio.css"' in response.text
    assert script.status_code == 200
    assert styles.status_code == 200


def test_candidate_api_exposes_skipped_workflow_as_blocked(tmp_path: Path, monkeypatch) -> None:
    _, inbox = _setup_test_exchange_batch(tmp_path)
    monkeypatch.setenv("HERMES_EXCHANGE_INBOX", str(inbox))
    db_path = tmp_path / "skipped.sqlite3"
    app = create_gateway_application(database_path=db_path)

    with TestClient(app) as client:
        candidates = client.get("/api/personal/product-video/candidates").json()["candidates"]
        product = next(item for item in candidates if item["model"] == "Life 7")
        from video_factory.candidate_service import DailyCandidateService
        setup_connection = connect(db_path)
        started = DailyCandidateService(setup_connection).start_selection([product["product_key"]]).to_dict()["started"][0]
        setup_connection.close()

        conn = connect(db_path)
        repository = WorkflowRepository(conn)
        aggregate, version = repository.get_workflow(started["workflow_id"])
        aggregate.update(state="skipped", skip_reason="KEYFRAME_CONTENT_FAIL")
        repository.commit_workflow(started["workflow_id"], json.dumps(aggregate), [], expected_version=version)
        conn.close()

        refreshed = client.get("/api/personal/product-video/candidates").json()["candidates"]

    result = next(item for item in refreshed if item["product_key"] == product["product_key"])
    assert result["status"] == "blocked"
    assert result["rejection_reason"] == "KEYFRAME_CONTENT_FAIL"


def test_videos_api_does_not_report_missing_export_as_completed(tmp_path: Path) -> None:
    conn = connect(tmp_path / "studio-videos.sqlite3")
    conn.execute(
        "INSERT INTO workflow (workflow_id, aggregate_json, version) VALUES (?, ?, ?)",
        (
            "wf-no-final-critic",
            json.dumps({
                "state": "completed",
                "product_name": "Snack",
                "source_context": {"product_key": "snack-v1"},
                "latest_artifact": {"video_url": "C:/output/snack.mp4"},
                "export_result": {"video_path": "C:/output/snack.mp4"},
                "qc_reports": [],
            }),
            1,
        ),
    )
    conn.commit()
    app = create_application(kanban_connection=conn)

    with TestClient(app) as client:
        response = client.get("/api/personal/product-video/videos")

    assert response.status_code == 200
    video = response.json()["videos"][0]
    assert video["result_status"] == "technical_error"
    assert video["video_url"] is None
    assert "not found" in video["technical_error"].lower()


def _seed_quarantined_creative_brief(conn, tmp_path: Path, workflow_id: str = "wf-brief-quarantine") -> tuple[str, Path]:
    import hashlib

    from video_factory.workflows.envelope import build_envelope, parse_envelope

    inbox = tmp_path / "configured-inbox"
    batch_dir = inbox / "2026-10-25" / "run_test"
    source = batch_dir / "products" / "snack" / "images" / "image.png"
    _write_image(source, (10, 20, 30))
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "manifest.json").write_text(json.dumps({
        "batch_id": "run_test", "production_date": "2026-10-25", "products": [{
            "id": "product-test", "product_key": "product-key-test",
            "canonical_product_key": "test:snack", "name": "Test Snack",
            "brand": "Test", "model": "Snack", "images": [{
                "asset_id": "source-1", "relative_path": "products/snack/images/image.png",
            }],
        }],
    }), encoding="utf-8")
    (batch_dir / "READY").write_text("ready\n", encoding="utf-8")
    project_id = f"proj-{workflow_id}"
    owner_id = "local-owner"
    pack = {
        "resource_pack_id": f"rp-{workflow_id}", "version": 1,
        "brand": "Test", "model": "Snack", "variant": None,
        "assets": [{"asset_id": "source-1", "local_path": str(source),
                    "absolute_path": str(source), "sha256": sha}],
    }
    approval_service = ApprovalService(conn)
    request_id = approval_service.submit_request({
        "workflow_id": workflow_id, "project_id": project_id, "owner_user_id": owner_id,
        "gate": "AwaitingProductLock", "artifact_ref": pack,
    })
    approval_service._update_status(request_id, ApprovalStatus.APPROVED)
    aggregate = {
        "state": "creating_brief", "project_id": project_id,
        "owner_user_id": owner_id, "product_name": "Test Snack",
        "locked_pack": pack, "resource_pack": pack, "latest_artifact": pack,
        "latest_approval_receipt": {
            "approval_request_id": request_id, "project_id": project_id,
            "gate": "AwaitingProductLock", "artifact_id": pack["resource_pack_id"],
            "artifact_version": 1, "owner_user_id": owner_id,
        },
        "creative_policy": {"category": "food_pov", "preparation_id": "prep-test", "qc_level": "middle"},
        "research_result": {"source_context": {
            "source": "hermes_product_video_exchange", "source_date": "2026-10-25",
            "source_batch_id": "run_test", "source_product_id": "product-test",
            "source_file": str(batch_dir / "manifest.json"),
            "canonical_product_key": "test:snack", "product_key": "product-key-test",
            "brand": "Test", "model": "Snack", "variant": None,
            "images": [{"asset_id": "source-1", "absolute_path": str(source),
                        "relative_path": "products/snack/images/image.png", "sha256": sha}],
        }},
    }
    envelope = build_envelope(
        workflow_id=workflow_id, topic="job.dispatch",
        idempotency_key=f"idemp_brief_{workflow_id}_v1",
        payload={"job_name": "product_video.creative_brief", "workflow_id": workflow_id,
                 "args": [pack["resource_pack_id"], {"resource_pack": pack, "project_id": project_id}],
                 "kwargs": {"project_id": project_id}, "owner_user_id": owner_id, "project_id": project_id},
    )
    parsed = parse_envelope(envelope)
    WorkflowRepository(conn).commit_workflow(workflow_id, json.dumps(aggregate), [], envelope, expected_version=0)
    conn.execute(
        "UPDATE workflow_outbox SET state='quarantined',attempt_count=1,max_attempts=5,last_error=? WHERE workflow_id=?",
        ("Job execution failed [TOOL_OPERATION_TERMINAL_FAILED]: UNTRUSTED_REFERENCE_SOURCE: source outside inbox", workflow_id),
    )
    WorkflowRepository(conn).commit_workflow(
        workflow_id, json.dumps(aggregate), [{
            "event_id": f"failed-{workflow_id}", "command_id": f"failed-{workflow_id}",
            "event_type": "PipelineStep", "timestamp": "2026-10-25T12:00:00+00:00",
            "payload": {"step_name": "CREATIVE_BRIEF", "status": "pending_operator",
                        "operation_id": parsed.message_id, "error_code": "TOOL_OPERATION_TERMINAL_FAILED",
                        "message": "UNTRUSTED_REFERENCE_SOURCE: source outside inbox",
                        "cost_status": "not_applicable"},
        }], expected_version=1,
    )
    return parsed.message_id, inbox


def test_studio_quarantined_brief_resume_requeues_same_job_once(tmp_path: Path, monkeypatch) -> None:
    import video_factory.application.candidate_api as candidate_api

    db_path = tmp_path / "resume-brief.sqlite3"
    conn = connect(db_path)
    workflow_id = "wf-studio-brief-resume"
    message_id, inbox = _seed_quarantined_creative_brief(conn, tmp_path, workflow_id)
    profile = tmp_path / "profile.json"
    profile.write_text((Path(__file__).parents[1] / "config/creative_run_profile.json").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("HERMES_EXCHANGE_INBOX", str(inbox))
    monkeypatch.setenv("VF_CREATIVE_RUN_CONFIG", str(profile))
    launches = []

    def capture_worker(**kwargs):
        launches.append(kwargs)

    monkeypatch.setattr(candidate_api, "run_studio_selected_batch", capture_worker)
    app = create_application(kanban_connection=conn)

    with TestClient(app) as client:
        detail = client.get(f"/api/operator/workflow/{workflow_id}").json()
        assert detail["quarantined_job"]["job_name"] == "product_video.creative_brief"
        assert detail["quarantined_job"]["recovery_available"] is True
        assert detail["quarantined_job"]["step"] == "CREATIVE_BRIEF"
        board = client.get("/api/operator/kanban").json()["columns"]
        assert any(card["workflow_id"] == workflow_id for card in board["error"])
        assert not any(card["workflow_id"] == workflow_id for card in board["in_production"])

        first = client.post(f"/api/operator/workflow/{workflow_id}/retry-quarantined")
        assert first.status_code == 200
        second = client.post(f"/api/operator/workflow/{workflow_id}/retry-quarantined")
        assert second.status_code == 200
        assert second.json()["status"] == "already_queued"

    rows = conn.execute("SELECT outbox_id,message,state,attempt_count,last_error FROM workflow_outbox WHERE workflow_id=?", (workflow_id,)).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["message"])["message_id"] == message_id
    assert rows[0]["state"] == "pending"
    assert rows[0]["attempt_count"] == 1
    assert "UNTRUSTED_REFERENCE_SOURCE" in rows[0]["last_error"]
    assert len(launches) == 1
    assert launches[0]["product_keys"] == ["product-key-test"]
    recovery = conn.execute(
        "SELECT payload_json FROM workflow_event WHERE workflow_id=? AND event_type='CreativeStageRecovered'",
        (workflow_id,),
    ).fetchone()
    assert recovery is not None
    assert json.loads(recovery[0])["outbox_id"] == rows[0]["outbox_id"]
    conn.close()


def test_studio_quarantined_brief_resume_refuses_running_or_unfixed_source(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "resume-brief-guards.sqlite3"
    conn = connect(db_path)
    running_id = "wf-brief-running"
    _, inbox = _seed_quarantined_creative_brief(conn, tmp_path, running_id)
    conn.execute(
        "UPDATE workflow_outbox SET state='leased',lease_token='lease',lease_owner='worker',"
        "lease_expires_at='2999-01-01T00:00:00+00:00' WHERE workflow_id=?", (running_id,),
    )
    unfixed_id = "wf-brief-unfixed"
    _seed_quarantined_creative_brief(conn, tmp_path / "unfixed", unfixed_id)
    invalid_receipt_id = "wf-brief-invalid-receipt"
    _seed_quarantined_creative_brief(conn, tmp_path / "invalid-receipt", invalid_receipt_id)
    invalid_aggregate = json.loads(conn.execute(
        "SELECT aggregate_json FROM workflow WHERE workflow_id=?", (invalid_receipt_id,),
    ).fetchone()[0])
    invalid_aggregate["latest_approval_receipt"]["artifact_id"] = "other-artifact"
    conn.execute("UPDATE workflow SET aggregate_json=? WHERE workflow_id=?",
                 (json.dumps(invalid_aggregate), invalid_receipt_id))
    wrong_inbox = tmp_path / "wrong-inbox"
    wrong_inbox.mkdir()
    monkeypatch.setenv("HERMES_EXCHANGE_INBOX", str(wrong_inbox))
    app = create_application(kanban_connection=conn)

    with TestClient(app) as client:
        running_detail = client.get(f"/api/operator/workflow/{running_id}").json()
        assert running_detail["quarantined_job"]["recovery_available"] is False
        running = client.post(f"/api/operator/workflow/{running_id}/retry-quarantined")
        assert running.status_code == 409
        unfixed = client.post(f"/api/operator/workflow/{unfixed_id}/retry-quarantined")
        assert unfixed.status_code == 409
        invalid_receipt = client.post(f"/api/operator/workflow/{invalid_receipt_id}/retry-quarantined")
        assert invalid_receipt.status_code == 409

    assert conn.execute("SELECT state,attempt_count FROM workflow_outbox WHERE workflow_id=?", (running_id,)).fetchone()[0] == "leased"
    assert conn.execute("SELECT state,attempt_count FROM workflow_outbox WHERE workflow_id=?", (unfixed_id,)).fetchone()[0] == "quarantined"
    assert conn.execute("SELECT state FROM workflow_outbox WHERE workflow_id=?", (invalid_receipt_id,)).fetchone()[0] == "quarantined"
    assert conn.execute("SELECT COUNT(*) FROM workflow_event WHERE event_type='CreativeStageRecovered'").fetchone()[0] == 0
    conn.close()


def test_studio_can_select_idle_pr1_checkpoint_and_dispatch_existing_product(tmp_path: Path, monkeypatch) -> None:
    import video_factory.application.candidate_api as candidate_api

    db_path = tmp_path / "idle-pr1-resume.sqlite3"
    conn = connect(db_path)
    workflow_id = "wf-idle-pr1-resume"
    _, inbox = _seed_quarantined_creative_brief(conn, tmp_path, workflow_id)
    profile = tmp_path / "profile.json"
    profile.write_text((Path(__file__).parents[1] / "config/creative_run_profile.json").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("HERMES_EXCHANGE_INBOX", str(inbox))
    monkeypatch.setenv("VF_CREATIVE_RUN_CONFIG", str(profile))

    brief = {"brief_id": "brief-resume", "version": 1}
    approval_id = ApprovalService(conn).submit_request({
        "workflow_id": workflow_id, "project_id": f"proj-{workflow_id}",
        "owner_user_id": "local-owner", "gate": "AwaitingBriefApproval", "artifact_ref": brief,
    })
    aggregate = json.loads(conn.execute(
        "SELECT aggregate_json FROM workflow WHERE workflow_id=?", (workflow_id,),
    ).fetchone()[0])
    aggregate.update({
        "state": "AwaitingBriefApproval", "pending_approval_id": approval_id,
        "pending_approval_gate": "AwaitingBriefApproval", "latest_artifact": brief,
        "creative_policy": {"creative_mode": "four_agent", "auto_export": True,
                            "preparation_only": True, "preparation_id": "prep-test"},
    })
    conn.execute("UPDATE workflow SET aggregate_json=? WHERE workflow_id=?",
                 (json.dumps(aggregate), workflow_id))
    conn.execute("DELETE FROM workflow_outbox WHERE workflow_id=?", (workflow_id,))
    conn.execute(
        "INSERT INTO daily_product_video_import (product_key,canonical_product_key,source_date,source_batch_id,"
        "source_product_id,source_path,workflow_id,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
        ("product-key-test", "test:snack", "2026-10-25", "run_test", "product-test",
         str(inbox / "2026-10-25" / "run_test" / "manifest.json"), workflow_id, "started"),
    )
    launches = []
    monkeypatch.setattr(candidate_api, "run_studio_selected_batch", lambda **kw: launches.append(kw))
    app = create_application(kanban_connection=conn)

    with TestClient(app) as client:
        candidates = client.get("/api/personal/product-video/candidates?target_date=2026-10-25").json()["candidates"]
        item = next(row for row in candidates if row["product_key"] == "product-key-test")
        assert item["status"] == "ready_to_resume"
        assert item["workflow_id"] == workflow_id
        response = client.post("/api/personal/product-video/start-batch", json={
            "product_keys": [item["product_key"]], "target_date": "2026-10-25", "qc_level": "middle",
        })
        assert response.status_code == 202

    assert len(launches) == 1
    assert launches[0]["product_keys"] == ["product-key-test"]
    assert conn.execute("SELECT COUNT(*) FROM workflow WHERE workflow_id=?", (workflow_id,)).fetchone()[0] == 1
    conn.close()


def test_pr1_preparation_auto_gate_accepts_current_pending_artifact_receipt_after_version_advance(tmp_path: Path) -> None:
    from video_factory.application.operator_handler import approve_and_advance_workflow

    conn = connect(tmp_path / "auto-preparation-gate.sqlite3")
    workflow_id = "wf-pr1-preparation-auto-gate"
    project_id = f"proj-{workflow_id}"
    owner_id = "local-owner"
    artifact = {"brief_id": "brief-1", "version": 1, "concept": "Grounded snack brief"}
    aggregate = {
        "state": "AwaitingBriefApproval", "project_id": project_id,
        "owner_user_id": owner_id, "product_name": "Test Snack",
        "latest_artifact": artifact,
        "creative_policy": {"creative_mode": "four_agent", "preparation_only": True},
    }
    repo = WorkflowRepository(conn)
    repo.commit_workflow(workflow_id, json.dumps(aggregate), [], expected_version=0)
    service = ApprovalService(conn)
    request_id = service.submit_request({
        "workflow_id": workflow_id, "project_id": project_id, "owner_user_id": owner_id,
        "gate": "AwaitingBriefApproval", "workflow_version": 1,
        "artifact_ref": artifact, "artifact_id": "brief-1", "artifact_version": 1,
    })
    aggregate.update(pending_approval_id=request_id, pending_approval_gate="AwaitingBriefApproval")
    repo.commit_workflow(workflow_id, json.dumps(aggregate), [], expected_version=1)

    result = approve_and_advance_workflow(
        conn, target_id=workflow_id, owner_user_id=owner_id,
        payload={"automation_actor": "creative_auto_policy"},
    )

    current, version = repo.get_workflow(workflow_id)
    assert result["status"] == "approved"
    assert current["state"] == "creating_scene_plan"
    assert "pending_approval_id" not in current
    assert version == 3
    assert conn.execute(
        "SELECT status FROM approval_request WHERE request_id=?", (request_id,),
    ).fetchone()[0] == "approved"
    conn.close()


def test_studio_detail_keeps_v2_creative_brief_visible(tmp_path: Path) -> None:
    conn = connect(tmp_path / "studio-v2-brief-detail.sqlite3")
    workflow_id = "wf-v2-brief-detail"
    brief = {
        "brief_id": "brief-v2", "hook": "Open with the package",
        "audience": {"segment": "snack buyers"}, "target_duration_seconds": 24,
        "script": [],
    }
    conn.execute(
        "INSERT INTO workflow (workflow_id, aggregate_json, version) VALUES (?, ?, ?)",
        (workflow_id, json.dumps({"state": "AwaitingBriefApproval", "creative_brief": brief}), 3),
    )

    with TestClient(create_application(kanban_connection=conn)) as client:
        response = client.get(f"/api/operator/workflow/{workflow_id}")

    assert response.status_code == 200
    assert response.json()["creative_brief"] == brief
    conn.close()


@pytest.mark.parametrize("health", ["healthy", "unavailable"])
def test_brief_binding_recovery_requires_repaired_snapshot(tmp_path, monkeypatch, health):
    from video_factory.application.operator_handler import inspect_quarantined_creative_brief, resume_quarantined_creative_brief
    from video_factory.capabilities.bindings import ImplementationBinding
    from video_factory.capabilities.manifest import INITIAL_CAPABILITIES
    from video_factory.capabilities.discovery import DEFAULT_WIRE_BINDING_RULES
    from video_factory.capabilities.snapshots import CapabilitySnapshotRepository

    conn = connect(tmp_path / "binding.db")
    wid = "wf-binding"
    _, inbox = _seed_quarantined_creative_brief(conn, tmp_path, wid)
    monkeypatch.setenv("HERMES_EXCHANGE_INBOX", str(inbox))
    error = "MISSING_PROVIDER_CONFIG: active capability binding unavailable for video.brief.create (unhealthy)"
    conn.execute("UPDATE workflow_outbox SET last_error=? WHERE workflow_id=?", (error, wid))
    row = conn.execute("SELECT event_id,payload_json FROM workflow_event WHERE workflow_id=? AND event_type='PipelineStep'", (wid,)).fetchone()
    payload = json.loads(row["payload_json"])
    payload.update(error_code="BINDING_UNAVAILABLE", message=error)
    conn.execute("UPDATE workflow_event SET payload_json=? WHERE event_id=?", (json.dumps(payload), row["event_id"]))
    conn.commit()
    bindings = []
    for rule in DEFAULT_WIRE_BINDING_RULES:
        spec = INITIAL_CAPABILITIES[rule.capability_id]
        bindings.append(ImplementationBinding(rule.capability_id, rule.accepted_wire_names[0], rule.source, rule.provider,
            "1", spec.input_schema_version, spec.output_schema_version, "sha256:" + "a" * 64, health))
    CapabilitySnapshotRepository(conn).save_and_activate(bindings)
    assert inspect_quarantined_creative_brief(conn, workflow_id=wid)["recovery_available"] == (health == "healthy")
    if health == "healthy":
        result = resume_quarantined_creative_brief(conn, workflow_id=wid)
        assert result["status"] == "recovered"
        assert resume_quarantined_creative_brief(conn, workflow_id=wid)["status"] == "already_queued"
        assert conn.execute("SELECT count(*) FROM workflow_outbox").fetchone()[0] == 1
        assert conn.execute("SELECT attempt_count FROM workflow_outbox").fetchone()[0] == 1
    conn.close()


def test_missing_brief_checkpoint_is_not_a_transport_error(tmp_path, monkeypatch):
    from video_factory.integrations import server
    from video_factory.integrations.repository import SQLiteVideoFactoryRepository
    from video_factory.integrations.service import VideoFactoryService
    from video_factory.mcp_adapters import MCPVideoFactoryAdapter

    service = VideoFactoryService(SQLiteVideoFactoryRepository(tmp_path / "video.sqlite"))
    service.create_project("owner", "project")
    monkeypatch.setattr(server, "_custom_service", service)
    result = server.creative_brief_get("owner", "project")
    assert result["ok"] is True
    assert result["project"]["creative_brief"] is None
    adapter = MCPVideoFactoryAdapter({}, {"creative_brief_get": "mcp__video_factory__creative_brief_get"})
    assert adapter.get_existing_creative_brief(owner_user_id="owner", project_id="project") is None
