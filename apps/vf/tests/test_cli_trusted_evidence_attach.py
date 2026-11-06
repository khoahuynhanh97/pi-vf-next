"""The supported CLI must attach only validator-backed claims to a locked workflow."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from video_factory.creative.trusted_evidence import publish_validated_record
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.repository import WorkflowRepository


def test_cli_attach_source_evidence_is_durable_and_rejects_tampered_archive(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "official.html"
    archive.write_text("<h1>Mijia Desk Lamp Lite</h1>\n<p>Three brightness levels</p>\n", encoding="utf-8")
    source_url = "https://www.mi.com/global/product/mijia-desk-lamp-lite/"
    archive.with_name(archive.name + ".meta.json").write_text(json.dumps({
        "source_url": source_url,
        "retrieved_at": "2026-10-20T00:00:00+00:00",
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }), encoding="utf-8")
    record_dir = tmp_path / "records"
    record_id = publish_validated_record({
        "canonical_product_key": "xiaomi:mijia-desk-lamp-lite",
        "canonical_claim_id": "brightness",
        "claim_text": "Three brightness levels",
        "evidence_source": source_url,
        "evidence_locator": "line 2",
        "evidence_snippet": "Three brightness levels",
        "evidence_archive": str(archive),
    }, source_root=tmp_path, record_dir=record_dir)

    db_path = tmp_path / "workflow.db"
    connection = connect(db_path)
    original_pack = {
        "resource_pack_id": "pack-1",
        "product_identity": {"brand": "Xiaomi", "model": "Mijia Desk Lamp Lite"},
    }
    WorkflowRepository(connection).commit_workflow("wf-1", json.dumps({
        "state": "WaitingForOperator",
        "operator_action": "attach_source_evidence",
        "operator_reason": "SOURCE_BACKED_REQUIRED",
        "project_id": "project-1",
        "owner_user_id": "owner",
        "resource_pack": original_pack,
        "locked_pack": {"resource_pack_id": "pack-1"},
        "latest_approval_receipt": {"approval_request_id": "approval-1"},
        "source_context": {"canonical_product_key": "xiaomi:mijia-desk-lamp-lite"},
    }), [], expected_version=None)
    connection.close()

    command = [
        shutil.which("uv") or "uv", "run", "video-factory", "--db-path", str(db_path),
        "attach-source-evidence", "wf-1", "--record-id", record_id,
        "--record-dir", str(record_dir), "--operator-id", "owner",
        "--review-note", "Reviewed exact model and official source",
    ]
    repo_root = Path(__file__).resolve().parents[1]
    first = subprocess.run(command, cwd=repo_root, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["status"] == "attached"
    second = subprocess.run(command, cwd=repo_root, capture_output=True, text=True, check=False)
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["status"] == "already_attached"

    connection = connect(db_path)
    aggregate, version = WorkflowRepository(connection).get_workflow("wf-1")
    assert aggregate["resource_pack"] == original_pack
    assert [row["record_id"] for row in aggregate["trusted_source_claims"]] == [record_id]
    assert aggregate["state"] == "creating_brief"
    assert aggregate.get("operator_action") is None
    assert connection.execute("SELECT COUNT(*) FROM workflow_outbox WHERE workflow_id=? AND state='pending'", ("wf-1",)).fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM workflow_event WHERE event_type='TrustedSourceClaimAttached'").fetchone()[0] == 1
    connection.close()

    # A new worker process has no environment variable from the CLI process.
    # Its verifier must resolve the durable attachment from this workflow DB.
    monkeypatch.delenv("VF_TRUSTED_EVIDENCE_DIR", raising=False)
    monkeypatch.setattr("video_factory.integrations.nine_router.configured_creative_model", lambda: object())
    monkeypatch.setattr(
        "video_factory.creative.source_backed.SourceEvidenceVerifier._default_resolve_host",
        staticmethod(lambda _host: ["8.8.8.8"]),
    )
    from video_factory.creative.trusted_evidence import validated_workflow_record_lookup
    from video_factory.handlers.common import create_configured_creative_generator

    restarted = connect(db_path)
    lookup = validated_workflow_record_lookup(restarted)
    generator = create_configured_creative_generator(record_lookup=lookup)
    assert generator.source_verifier.verify(
        source_url, "line 2", "Three brightness levels", record_id,
        claim_text="Three brightness levels", product_key="xiaomi:mijia-desk-lamp-lite",
    )
    restarted.close()

    archive.write_text("changed bytes", encoding="utf-8")
    restarted = connect(db_path)
    assert validated_workflow_record_lookup(restarted)(record_id) is None
    assert not create_configured_creative_generator(
        record_lookup=validated_workflow_record_lookup(restarted),
    ).source_verifier.verify(
        source_url, "line 2", "Three brightness levels", record_id,
        claim_text="Three brightness levels", product_key="xiaomi:mijia-desk-lamp-lite",
    )
    restarted.close()
    rejected = subprocess.run(command, cwd=repo_root, capture_output=True, text=True, check=False)
    assert rejected.returncode == 1
    assert "failed Catalog V2 validation" in rejected.stderr
    connection = connect(db_path)
    assert WorkflowRepository(connection).get_workflow("wf-1")[1] == version
    connection.close()
