from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from video_factory.application import candidate_api
from video_factory.application.app import create_application
from video_factory.storage_infra.sqlite import connect


def _ready_two_product_batch(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    from video_factory.automation.importer import publish_daily_run_to_exchange

    source = tmp_path / "source"
    source.mkdir()
    products = []
    keys = {}
    for slug, brand, model in (("lays", "Lay's", "Stax"), ("bibigo", "Bibigo", "Snack Rong Bien An Lien 25g")):
        images = []
        for index, color in enumerate(((180, 40, 20), (20, 50, 170)), start=1):
            image = source / f"{slug}-{index}.png"
            Image.new("RGB", (120, 160), color).save(image)
            images.append({"asset_id": f"{slug}-{index}", "absolute_path": str(image)})
        products.append({
            "id": slug,
            "name": f"{brand} {model}",
            "brand": brand,
            "model": model,
            "images": images,
        })
    (source / "products.json").write_text(
        json.dumps({"date": "2026-10-25", "products": products}), encoding="utf-8"
    )
    inbox = tmp_path / "inbox"
    batch = publish_daily_run_to_exchange(source, inbox, batch_id="run_studio_test")
    from video_factory.automation.importer import ExchangeImporter

    for product in ExchangeImporter(inbox).load_manifest(batch).products:
        keys[product.brand] = product.product_key
    return inbox, keys


def test_studio_start_dispatches_selected_products_to_pr1_batch_not_manual_selection(
    tmp_path: Path, monkeypatch
) -> None:
    inbox, keys = _ready_two_product_batch(tmp_path)
    from video_factory.automation.importer import ExchangeImporter

    manifest = ExchangeImporter(inbox).load_manifest(ExchangeImporter(inbox).find_latest_ready_batch())
    bibigo_canonical_key = next(
        item.canonical_product_key for item in manifest.products if item.product_key == keys["Bibigo"]
    )
    monkeypatch.setenv("HERMES_EXCHANGE_INBOX", str(inbox))
    calls = []

    def run_batch(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(candidate_api, "run_studio_selected_batch", run_batch)
    monkeypatch.setattr(
        "video_factory.candidate_service.DailyCandidateService.start_selection",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("manual start_selection called")),
    )
    db_path = tmp_path / "studio.db"
    with TestClient(create_application(kanban_connection=connect(db_path))) as client:
        candidates_response = client.get(
            "/api/personal/product-video/candidates?target_date=2026-10-25&qc_level=low"
        )
        assert candidates_response.status_code == 200
        response = client.post(
            "/api/personal/product-video/start-batch",
            json={"product_keys": [keys["Lay's"], bibigo_canonical_key], "target_date": "2026-10-25",
                  "qc_level": "low"},
        )

    assert response.status_code == 202
    assert [item["product_key"] for item in response.json()["started"]] == [
        keys["Lay's"], keys["Bibigo"]
    ]
    assert len(calls) == 1
    assert calls[0]["product_keys"] == [keys["Lay's"], keys["Bibigo"]]
    assert calls[0]["db_path"] == db_path
    assert calls[0]["inbox_root"] == inbox
    assert calls[0]["qc_level"] == "low"
    assert calls[0]["disable_keyframe_qc"] is False


def test_advance_creative_resumes_verified_legacy_storyboard_quarantine(tmp_path: Path, monkeypatch) -> None:
    from video_factory.automation.coordinator import ProductVideoCoordinator
    from video_factory.creative.storyboard import storyboard_package_digest

    workflow_id = "pv-daily-verified-render"
    connection = connect(tmp_path / "resume.db")
    coordinator = ProductVideoCoordinator(connection, db_path=tmp_path / "resume.db")
    generated = []
    items = []
    for index in range(1, 5):
        frame_id = f"scene_{index}-start"
        image_path = tmp_path / f"{frame_id}.png"
        Image.new("RGB", (32, 48), (index * 30, 20, 40)).save(image_path)
        sha = hashlib.sha256(image_path.read_bytes()).hexdigest()
        generated.append({
            "frame_id": frame_id,
            "scene_id": f"scene_{index}",
            "image_path": str(image_path),
            "sha256": sha,
            "status": "completed",
        })
        items.append({
            "frame_id": frame_id,
            "image_path": str(image_path),
            "sha256": sha,
            "verdict": "pass",
            "hard_failure": False,
        })

    artifact = {
        "schema_version": "storyboard-package-v2",
        "workflow_id": workflow_id,
        "project_id": "proj-verified-render",
        "creative_mode": "four_agent",
        "scenes": [{"scene_id": f"scene_{index}"} for index in range(1, 5)],
        "backend_profile": {"provider_kind": "google_flow"},
        "storyboard_frames": [
            {"scene_id": f"scene_{index}", "keyframe_image_path": generated[index - 1]["image_path"]}
            for index in range(1, 5)
        ],
        "generated_keyframes": generated,
        "recovery_attempt": 0,
    }
    artifact["package_digest"] = storyboard_package_digest(artifact)
    report_id = "qc-verified-keyframes"
    artifact["keyframe_qc_receipt"] = {
        "report_id": report_id,
        "workflow_id": workflow_id,
        "stage": "keyframe",
        "verdict": "pass",
        "artifact_digest": artifact["package_digest"],
        "findings": {"items": items, "hard_failure": False},
    }
    aggregate = {
        "state": "rendering",
        "project_id": artifact["project_id"],
        "owner_user_id": "local-owner",
        "source_context": {"canonical_product_key": "example:product"},
        "creative_policy": {"creative_mode": "four_agent", "category": "product_demo"},
        "latest_artifact": artifact,
    }
    message = json.dumps({
        "topic": "job.dispatch",
        "idempotency_key": "idemp_product_video.render_pv-daily-ec9f666e0f2753f1_v15",
        "payload": {"workflow_id": workflow_id, "job_name": "product_video.render"},
    })
    coordinator.workflow_repo.commit_workflow(
        workflow_id, json.dumps(aggregate), [], outbox_message=message, expected_version=0
    )
    connection.execute(
        "UPDATE workflow_outbox SET state='quarantined', attempt_count=1, last_error=? WHERE workflow_id=?",
        ("Job execution failed [TOOL_OPERATION_TERMINAL_FAILED]: Current full storyboard content PASS with unchanged pixels is required", workflow_id),
    )
    connection.execute(
        """CREATE TABLE video_factory_keyframe_operation (
            operation_id TEXT, workflow_id TEXT, project_id TEXT, frame_id TEXT, status TEXT,
            provider_operation_id TEXT, output_path TEXT, output_sha256 TEXT
        )"""
    )
    for index, frame in enumerate(generated, start=1):
        connection.execute(
            "INSERT INTO video_factory_keyframe_operation VALUES (?,?,?,?,?,?,?,?)",
            (f"keyframe-{index}", workflow_id, artifact["project_id"], frame["frame_id"],
             "completed", f"https://flow.google.com/project/{index}", frame["image_path"], frame["sha256"]),
        )
    from video_factory.qc.repository import QCReportRepository

    QCReportRepository(connection).ensure_schema()
    connection.execute(
        """INSERT INTO product_video_qc_reports (
            report_id, workflow_id, stage, artifact_id, artifact_version, artifact_digest,
            reference_digests, evaluator_name, evaluator_version, rubric_version, created_at,
            score, verdict, findings_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (report_id, workflow_id, "keyframe", workflow_id, 1, artifact["package_digest"],
         "[]", "test", "1", "test", "2026-10-25T00:00:00Z", 100, "pass", json.dumps({"items": items})),
    )
    connection.commit()

    monkeypatch.setattr(
        "video_factory.automation.coordinator.run_worker",
        lambda **kwargs: type("WorkerReport", (), {"failed": 0})(),
    )
    result = coordinator.advance_creative(workflow_id)
    again = coordinator.advance_creative(workflow_id)

    assert result["action"] == "pending", result
    assert again["action"] == "pending"
    assert connection.execute("SELECT state FROM workflow_outbox WHERE workflow_id=?", (workflow_id,)).fetchone()[0] == "pending"
    assert connection.execute("SELECT attempt_count FROM workflow_outbox WHERE workflow_id=?", (workflow_id,)).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM video_factory_keyframe_operation WHERE workflow_id=? AND status='completed'",
        (workflow_id,),
    ).fetchone()[0] == 4
    assert connection.execute(
        "SELECT COUNT(*) FROM workflow_event WHERE workflow_id=? AND event_type='VerifiedStoryboardQuarantineRecovered'",
        (workflow_id,),
    ).fetchone()[0] == 1
    audit = coordinator.workflow_repo.get_workflow(workflow_id)[0]["render_recovery_history"]
    assert audit[0]["prior_error"].endswith("Current full storyboard content PASS with unchanged pixels is required")
    connection.close()


def test_advance_creative_does_not_recover_unrelated_quarantine(tmp_path: Path, monkeypatch) -> None:
    from video_factory.automation.coordinator import ProductVideoCoordinator

    workflow_id = "pv-unrelated-quarantine"
    connection = connect(tmp_path / "unrelated.db")
    coordinator = ProductVideoCoordinator(connection, db_path=tmp_path / "unrelated.db")
    coordinator.workflow_repo.commit_workflow(
        workflow_id,
        json.dumps({
            "state": "rendering",
            "project_id": "proj-unrelated",
            "owner_user_id": "local-owner",
            "creative_policy": {"creative_mode": "four_agent"},
        }),
        [],
        outbox_message=json.dumps({
            "topic": "job.dispatch",
            "payload": {"workflow_id": workflow_id, "job_name": "product_video.render"},
        }),
        expected_version=0,
    )
    connection.execute(
        "UPDATE workflow_outbox SET state='quarantined', last_error='provider authentication failed' WHERE workflow_id=?",
        (workflow_id,),
    )
    connection.commit()
    monkeypatch.setattr(
        "video_factory.automation.coordinator.run_worker",
        lambda **kwargs: type("WorkerReport", (), {"failed": 0})(),
    )

    result = coordinator.advance_creative(workflow_id)

    assert result["action"] == "quarantined"
    assert connection.execute(
        "SELECT state FROM workflow_outbox WHERE workflow_id=?", (workflow_id,)
    ).fetchone()[0] == "quarantined"
    assert connection.execute(
        "SELECT COUNT(*) FROM workflow_event WHERE workflow_id=? AND event_type='VerifiedStoryboardQuarantineRecovered'",
        (workflow_id,),
    ).fetchone()[0] == 0
    connection.close()


def test_studio_dispatch_uses_only_the_pr1_batch_path(
    tmp_path: Path, monkeypatch
) -> None:
    from video_factory.automation import creative_cli

    inbox, keys = _ready_two_product_batch(tmp_path)
    db_path = tmp_path / "studio-run.db"
    invoked = []
    def creative_batch(args):
        invoked.append(args)
        return 0

    monkeypatch.setattr(creative_cli, "cmd_creative_batch", creative_batch)
    result = creative_cli.run_studio_selected_batch(
        db_path=db_path,
        inbox_root=inbox,
        profile_path=tmp_path / "profile.json",
        product_keys=[keys["Lay's"], keys["Bibigo"]],
        target_date="2026-10-25",
    )

    assert result == 0
    assert len(invoked) == 1
    assert invoked[0].product_key == [keys["Lay's"], keys["Bibigo"]]
    assert invoked[0].qc_level == 'high'
    assert len(invoked) == 1


def test_studio_new_batch_defaults_to_high_but_preserves_explicit_levels(tmp_path, monkeypatch):
    inbox, keys = _ready_two_product_batch(tmp_path)
    monkeypatch.setenv('HERMES_EXCHANGE_INBOX', str(inbox))
    dispatched = []
    monkeypatch.setattr(candidate_api, 'run_studio_selected_batch', lambda **kwargs: dispatched.append(kwargs))
    with TestClient(create_application(kanban_connection=connect(tmp_path / 'defaults.db'))) as client:
        for level in (None, 'low', 'middle'):
            payload = {'product_keys': [keys["Lay's"]], 'target_date': '2026-10-25'}
            if level:
                payload['qc_level'] = level
            response = client.post('/api/personal/product-video/start-batch', json=payload)
            assert response.status_code == 202, response.json()
            assert response.json()['qc_level'] == (level or 'high')
            assert dispatched[-1]['qc_level'] == (level or 'high')
