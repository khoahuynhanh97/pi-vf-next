"""Validator-backed evidence publication and the configured creative gate."""

import hashlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from video_factory.creative.contracts import EligibleAsset, EvidenceClaim, ProductIdentityV2, ResearchDossier
from video_factory.creative.generator import SingleModelCreativeGenerator, StructuredGenerationError
from video_factory.creative.source_backed import SourceEvidenceVerifier
from video_factory.creative.trusted_evidence import (
    attach_validated_source_claim, publish_validated_record, validated_record_lookup,
)
from video_factory.handlers.common import create_configured_creative_generator
from video_factory.handlers.common import _locked_research_dossier
from video_factory.qc.models import QCReport, QCStage, QCVerdict
from video_factory.qc.repository import QCReportRepository
from video_factory.workflows.repository import WorkflowRepository
from video_factory.storage_infra.sqlite import connect


SOURCE_URL = "https://www.mi.com/global/product/mijia-desk-lamp-lite/"
CLAIM_TEXT = "Three brightness levels"
PRODUCT_KEY = "xiaomi:mijia-desk-lamp-lite"


def _archive_claim(tmp_path: Path):
    archive = tmp_path / "brand-product.html"
    archive.write_text("<h1>Mijia Desk Lamp Lite</h1>\n<p>Three brightness levels</p>\n", encoding="utf-8")
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_name(archive.name + ".meta.json").write_text(json.dumps({
        "source_url": SOURCE_URL, "retrieved_at": "2026-10-19T00:00:00+00:00", "sha256": sha,
    }), encoding="utf-8")
    return {
        "canonical_product_key": PRODUCT_KEY,
        "canonical_claim_id": "claim-brightness",
        "claim_text": CLAIM_TEXT,
        "evidence_source": SOURCE_URL,
        "evidence_locator": "line 2",
        "evidence_snippet": "Three brightness levels",
        "evidence_archive": str(archive),
    }, archive


def _dossier(record_id: str, tmp_path: Path) -> ResearchDossier:
    assets = []
    for number in (1, 2):
        path = tmp_path / f"reference-{number}.png"
        path.write_bytes(f"offline-reference-{number}".encode("ascii"))
        assets.append(EligibleAsset(
            f"asset-{number}", str(path), hashlib.sha256(path.read_bytes()).hexdigest(), "qc-offline",
        ))
    return ResearchDossier.create(
        workflow_id="wf-evidence-test",
        product_identity=ProductIdentityV2(
            brand="Xiaomi", model="Mijia Desk Lamp Lite", display_name="Mijia Desk Lamp Lite"
        ),
        verified_claims=(EvidenceClaim(
            claim_id="claim-brightness", text=CLAIM_TEXT, evidence_refs=(SOURCE_URL,),
            source_locators=(SOURCE_URL,), evidence_locator="line 2",
            evidence_snippets=("Three brightness levels",), evidence_record_id=record_id,
        ),),
        eligible_assets=tuple(assets),
    )


class ModelNeverCalled:
    calls = 0

    def generate(self, **_kwargs):
        self.calls += 1
        raise AssertionError("provider must not be called while source gate is closed")


def test_published_record_is_revalidated_and_bound_to_claim_and_product(tmp_path):
    claim, archive = _archive_claim(tmp_path)
    records = tmp_path / "validated-records"
    record_id = publish_validated_record(claim, source_root=tmp_path, record_dir=records)
    lookup = validated_record_lookup(records, source_root=tmp_path)
    verifier = SourceEvidenceVerifier(record_lookup=lookup, resolve_host=lambda _host: ["8.8.8.8"])

    assert verifier.verify(SOURCE_URL, "line 2", "Three brightness levels", record_id,
                           claim_text=CLAIM_TEXT, product_key=PRODUCT_KEY)
    assert not verifier.verify(SOURCE_URL, "line 2", "Three brightness levels", record_id,
                               claim_text="Four brightness levels", product_key=PRODUCT_KEY)
    assert not verifier.verify(SOURCE_URL, "line 2", "Three brightness levels", record_id,
                               claim_text=CLAIM_TEXT, product_key="xiaomi:another-model")

    archive.write_text("<h1>Mijia Desk Lamp Lite</h1>\n<p>Four brightness levels</p>\n", encoding="utf-8")
    assert lookup(record_id) is None


def test_publication_cli_writes_validated_record_offline(tmp_path):
    claim, _archive = _archive_claim(tmp_path)
    claim_path = tmp_path / "reviewed-claim.json"
    claim_path.write_text(json.dumps(claim), encoding="utf-8")
    record_dir = tmp_path / "validated-records"
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [shutil.which("uv") or "uv", "run", "python", str(repo / "tools" / "publish_source_evidence.py"),
         "--claim-json", str(claim_path), "--record-dir", str(record_dir),
         "--source-root", str(tmp_path)],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert validated_record_lookup(record_dir, source_root=tmp_path)(result.stdout.strip()) is not None


@pytest.mark.parametrize("mutation", ["locator", "snippet", "hash", "url", "claim_text"])
def test_publisher_rejects_unverified_source_coordinates(tmp_path, mutation):
    claim, archive = _archive_claim(tmp_path)
    if mutation == "locator":
        claim["evidence_locator"] = "line 1"
    elif mutation == "snippet":
        claim["evidence_snippet"] = "Five brightness levels"
    elif mutation == "hash":
        archive.with_name(archive.name + ".meta.json").write_text(json.dumps({
            "source_url": SOURCE_URL, "retrieved_at": "2026-10-19T00:00:00+00:00", "sha256": "0" * 64,
        }), encoding="utf-8")
    else:
        if mutation == "url":
            claim["evidence_source"] = "https://www.mi.com/global/product/another-model/"
        else:
            claim["claim_text"] = "Supports 100W power output"
    with pytest.raises((AssertionError, ValueError)):
        publish_validated_record(claim, source_root=tmp_path, record_dir=tmp_path / "records")


def test_configured_creative_stays_closed_without_trusted_record(tmp_path, monkeypatch):
    monkeypatch.setenv("VF_TRUSTED_EVIDENCE_DIR", str(tmp_path / "missing-records"))
    monkeypatch.setattr("video_factory.integrations.nine_router.configured_creative_model", lambda: ModelNeverCalled())
    generator = create_configured_creative_generator()
    assert isinstance(generator, SingleModelCreativeGenerator)
    assert generator._has_source_backed_claims(_dossier("missing-record", tmp_path)) is False
    with pytest.raises(StructuredGenerationError, match="verified source evidence"):
        generator.create_direction(_dossier("missing-record", tmp_path))
    assert generator.model.calls == 0


def test_configured_creative_accepts_only_validator_published_record(tmp_path, monkeypatch):
    claim, _archive = _archive_claim(tmp_path)
    records = tmp_path / "validated-records"
    record_id = publish_validated_record(claim, source_root=tmp_path, record_dir=records)
    monkeypatch.setenv("VF_TRUSTED_EVIDENCE_DIR", str(records))
    monkeypatch.setattr("video_factory.integrations.nine_router.configured_creative_model", lambda: ModelNeverCalled())
    monkeypatch.setattr(SourceEvidenceVerifier, "_default_resolve_host", staticmethod(lambda _host: ["8.8.8.8"]))
    generator = create_configured_creative_generator()
    assert generator._has_source_backed_claims(_dossier(record_id, tmp_path)) is True
    wrong_product = _dossier(record_id, tmp_path).to_dict()
    wrong_product["product_identity"]["model"] = "Another Model"
    wrong_product.pop("dossier_digest", None)
    assert generator._has_source_backed_claims(ResearchDossier.from_dict(wrong_product)) is False


def test_locked_dossier_does_not_trust_pi_source_backed_label(monkeypatch):
    captured = {}

    def build_dossier(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(to_dict=lambda: {"ok": True})

    monkeypatch.setattr("video_factory.creative.dossier.build_research_dossier", build_dossier)
    monkeypatch.setattr(
        "video_factory.qc.repository.QCReportRepository.get_latest_report",
        lambda *_args, **_kwargs: SimpleNamespace(verdict=QCVerdict.PASS),
    )
    pack = {
        "product_identity": {"brand": "Xiaomi", "model": "Mijia Desk Lamp Lite"},
        "snapshot_id": "pi-snapshot-1", "canonical_product_id": PRODUCT_KEY,
        "source_results": [{"url": SOURCE_URL, "usable": True, "source_class": "official"}],
        "evidence": [{
            "evidence_id": "pi-evidence-1", "snapshot_id": "pi-snapshot-1",
            "canonical_product_id": PRODUCT_KEY,
            "source": {"canonical_url": SOURCE_URL},
        }],
        "claims": [{
            "claim_id": "claim-brightness", "text": CLAIM_TEXT,
            "status": "SOURCE_BACKED", "evidence_refs": ["pi-evidence-1"],
            "evidence_source": SOURCE_URL, "evidence_locator": "line 2",
            "evidence_snippet": "Three brightness levels", "evidence_record_id": "src_record",
        }, {
            "claim_id": "pi-claim", "text": "Claim from PI only",
            "status": "supported", "evidence_refs": ["pi-evidence-1"],
        }],
    }
    assert _locked_research_dossier(
        workflow_id="wf-evidence-test", aggregate={"resource_pack": pack},
        locked_pack={}, connection=sqlite3.connect(":memory:"),
    ) == {"ok": True}
    trusted, ordinary = captured["research_snapshot"]["claims"]
    assert trusted["status"] == "supported"
    assert "evidence_record_id" not in trusted
    assert ordinary["status"] == "supported"
    assert "evidence_record_id" not in ordinary


def test_operator_attachment_survives_restart_without_changing_locked_pack(tmp_path, monkeypatch):
    claim, archive = _archive_claim(tmp_path)
    records = tmp_path / "validated-records"
    record_id = publish_validated_record(claim, source_root=tmp_path, record_dir=records)
    database = tmp_path / "workflow.db"
    conn = connect(database)
    manifest = []
    for number, color in ((1, "red"), (2, "blue")):
        path = tmp_path / f"approved-{number}.png"
        Image.new("RGB", (12, 12), color=color).save(path)
        manifest.append({
            "asset_id": f"asset-{number}", "uri": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "eligible": True,
        })
    pack = {
        "resource_pack_id": "pack-1", "snapshot_id": "pi-snapshot-1",
        "product_identity": {"brand": "Xiaomi", "model": "Mijia Desk Lamp Lite"},
        "claims": [{"claim_id": "claim-brightness", "text": CLAIM_TEXT,
                    "status": "supported", "evidence_refs": ["pi-evidence-1"]}],
    }
    lock = {"resource_pack_id": "pack-1", "snapshot_id": "pi-snapshot-1"}
    aggregate = {
        "workflow_id": "wf-evidence-attach", "resource_pack": pack,
        "locked_pack": lock,
        "latest_approval_receipt": {"approval_request_id": "approval-1"},
        "state": "WaitingForOperator", "operator_action": "attach_source_evidence",
        "project_id": "project-1", "owner_user_id": "operator-local",
    }
    WorkflowRepository(conn).commit_workflow(
        "wf-evidence-attach", json.dumps(aggregate), [], expected_version=None,
    )
    from datetime import datetime, timezone

    QCReportRepository(conn).save_report(QCReport(
        report_id="qc-images", workflow_id="wf-evidence-attach", stage=QCStage.IMAGE,
        artifact_id="pack-1", artifact_version=1, artifact_digest="pack-digest",
        reference_digests=[item["sha256"] for item in manifest],
        evaluator_name="offline", evaluator_version="1", rubric_version="1",
        created_at=datetime.now(timezone.utc).isoformat(), score=90,
        verdict=QCVerdict.PASS, findings={"eligible_reference_manifest": manifest},
    ))
    attached = attach_validated_source_claim(
        conn, "wf-evidence-attach", record_id, record_dir=records,
        operator_id="operator-local", review_note="Checked official source and exact model",
    )
    assert attached["status"] == "attached"
    persisted, version = WorkflowRepository(conn).get_workflow("wf-evidence-attach")
    assert persisted["locked_pack"] == lock
    assert persisted["resource_pack"] == pack
    assert persisted["trusted_source_claims"][0]["record_dir"] == str(records.resolve())
    assert attach_validated_source_claim(
        conn, "wf-evidence-attach", record_id, record_dir=records,
        operator_id="operator-local", review_note="Checked official source and exact model",
    )["status"] == "already_attached"
    assert WorkflowRepository(conn).get_workflow("wf-evidence-attach")[1] == version
    conn.close()

    reopened = connect(database)
    persisted, _ = WorkflowRepository(reopened).get_workflow("wf-evidence-attach")
    dossier = _locked_research_dossier(
        workflow_id="wf-evidence-attach", aggregate=persisted,
        locked_pack=lock, connection=reopened,
    )
    claims = dossier["verified_claims"]
    assert len(claims) == 1
    assert claims[0]["evidence_record_id"] == record_id
    assert claims[0]["text"] == CLAIM_TEXT
    assert claims[0]["source_locators"] == (SOURCE_URL,)
    assert len(dossier["eligible_assets"]) == 2

    archive.write_text("changed bytes", encoding="utf-8")
    stale = _locked_research_dossier(
        workflow_id="wf-evidence-attach", aggregate=persisted,
        locked_pack=lock, connection=reopened,
    )
    assert stale["verified_claims"] == ()
    reopened.close()


def test_operator_attachment_rejects_wrong_identity_and_unlocked_workflow(tmp_path):
    claim, _archive = _archive_claim(tmp_path)
    records = tmp_path / "validated-records"
    record_id = publish_validated_record(claim, source_root=tmp_path, record_dir=records)
    conn = connect(tmp_path / "workflow.db")
    repo = WorkflowRepository(conn)
    base = {
        "resource_pack": {
            "resource_pack_id": "pack-1",
            "product_identity": {"brand": "Xiaomi", "model": "Other Model"},
        },
        "locked_pack": {"resource_pack_id": "pack-1"},
        "latest_approval_receipt": {"approval_request_id": "approval-1"},
    }
    repo.commit_workflow("wf-wrong-model", json.dumps(base), [], expected_version=None)
    with pytest.raises(ValueError, match="product key"):
        attach_validated_source_claim(
            conn, "wf-wrong-model", record_id, record_dir=records,
            operator_id="operator-local", review_note="Reviewed source",
        )
    unlocked = {**base, "resource_pack": {**base["resource_pack"],
                "product_identity": {"brand": "Xiaomi", "model": "Mijia Desk Lamp Lite"}}}
    unlocked.pop("locked_pack")
    repo.commit_workflow("wf-unlocked", json.dumps(unlocked), [], expected_version=None)
    with pytest.raises(ValueError, match="Product Lock"):
        attach_validated_source_claim(
            conn, "wf-unlocked", record_id, record_dir=records,
            operator_id="operator-local", review_note="Reviewed source",
        )
    conflicting = {**unlocked,
                   "locked_pack": {"resource_pack_id": "pack-1"},
                   "source_context": {"canonical_product_key": "xiaomi:another-model"}}
    repo.commit_workflow("wf-conflicting-keys", json.dumps(conflicting), [], expected_version=None)
    with pytest.raises(ValueError, match="identity conflict"):
        attach_validated_source_claim(
            conn, "wf-conflicting-keys", record_id, record_dir=records,
            operator_id="operator-local", review_note="Reviewed source",
        )
    xiaomi_shape = {
        "resource_pack": {
            "resource_pack_id": "pack-xiaomi",
            "product_identity": {"brand": "Xiaomi", "name": "Xiaomi Desk Lamp Lite", "model": None},
        },
        "locked_pack": {"resource_pack_id": "pack-xiaomi"},
        "latest_approval_receipt": {"approval_request_id": "approval-xiaomi"},
        "source_context": {
            "brand": "Xiaomi", "model": "Mijia Desk Lamp Lite",
            "canonical_product_key": PRODUCT_KEY,
        },
    }
    repo.commit_workflow("wf-xiaomi-shape", json.dumps(xiaomi_shape), [], expected_version=None)
    with pytest.raises(ValueError, match="identity conflict"):
        attach_validated_source_claim(
            conn, "wf-xiaomi-shape", record_id, record_dir=records,
            operator_id="operator-local", review_note="Reviewed source",
        )
    conn.close()


def test_operator_attachment_rejects_second_record_for_same_claim_id(tmp_path):
    claim, archive = _archive_claim(tmp_path)
    records = tmp_path / "validated-records"
    first = publish_validated_record(claim, source_root=tmp_path, record_dir=records)
    second_archive = tmp_path / "other-official-page.html"
    second_archive.write_bytes(archive.read_bytes())
    second_archive.with_name(second_archive.name + ".meta.json").write_text(
        archive.with_name(archive.name + ".meta.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    second_claim = {**claim, "evidence_archive": str(second_archive)}
    second = publish_validated_record(second_claim, source_root=tmp_path, record_dir=records)
    assert second != first
    conn = connect(tmp_path / "workflow.db")
    aggregate = {
        "resource_pack": {
            "resource_pack_id": "pack-1",
            "product_identity": {"brand": "Xiaomi", "model": "Mijia Desk Lamp Lite"},
        },
        "locked_pack": {"resource_pack_id": "pack-1"},
        "latest_approval_receipt": {"approval_request_id": "approval-1"},
        "state": "WaitingForOperator", "operator_action": "attach_source_evidence",
        "project_id": "project-1", "owner_user_id": "operator-local",
    }
    WorkflowRepository(conn).commit_workflow("wf-duplicate-claim", json.dumps(aggregate), [], expected_version=None)
    attach_validated_source_claim(
        conn, "wf-duplicate-claim", first, record_dir=records,
        operator_id="operator-local", review_note="Reviewed first source",
    )
    with pytest.raises(ValueError, match="claim ID"):
        attach_validated_source_claim(
            conn, "wf-duplicate-claim", second, record_dir=records,
            operator_id="operator-local", review_note="Reviewed second source",
        )
    conn.close()


def test_new_evidence_cannot_change_claims_after_creative_started(tmp_path):
    claim, _archive = _archive_claim(tmp_path)
    records = tmp_path / "validated-records"
    record_id = publish_validated_record(claim, source_root=tmp_path, record_dir=records)
    conn = connect(tmp_path / "workflow.db")
    aggregate = {
        "resource_pack": {
            "resource_pack_id": "pack-1",
            "product_identity": {"brand": "Xiaomi", "model": "Mijia Desk Lamp Lite"},
        },
        "locked_pack": {"resource_pack_id": "pack-1"},
        "latest_approval_receipt": {"approval_request_id": "approval-1"},
        "state": "AwaitingBriefApproval",
    }
    repo = WorkflowRepository(conn)
    repo.commit_workflow("wf-creative-started", json.dumps(aggregate), [], expected_version=None)
    with pytest.raises(ValueError, match="pre-creative"):
        attach_validated_source_claim(
            conn, "wf-creative-started", record_id, record_dir=records,
            operator_id="operator-local", review_note="Reviewed later source",
        )
    saved, version = repo.get_workflow("wf-creative-started")
    assert version == 1
    assert "trusted_source_claims" not in saved


def test_operator_attachment_accepts_pack_name_confirming_source_model(tmp_path):
    claim, _archive = _archive_claim(tmp_path)
    records = tmp_path / "validated-records"
    record_id = publish_validated_record(claim, source_root=tmp_path, record_dir=records)
    conn = connect(tmp_path / "workflow.db")
    aggregate = {
        "resource_pack": {
            "resource_pack_id": "pack-tribit-like",
            "raw_result": {
                "resolved_values": {
                    "brand": "Xiaomi",
                    "model": None,
                    "name": "Xiaomi Mijia Desk Lamp Lite White",
                },
            },
        },
        "locked_pack": {"resource_pack_id": "pack-tribit-like"},
        "latest_approval_receipt": {"approval_request_id": "approval-tribit-like"},
        "source_context": {
            "brand": "Xiaomi",
            "model": "Mijia Desk Lamp Lite",
            "canonical_product_key": PRODUCT_KEY,
        },
        "state": "WaitingForOperator",
        "operator_action": "attach_source_evidence",
        "project_id": "project-1",
        "owner_user_id": "operator-local",
    }
    repo = WorkflowRepository(conn)
    repo.commit_workflow("wf-confirmed-name", json.dumps(aggregate), [], expected_version=None)
    attached = attach_validated_source_claim(
        conn, "wf-confirmed-name", record_id, record_dir=records,
        operator_id="operator-local", review_note="Confirmed model in display name",
    )
    assert attached["status"] == "attached"
    conn.close()

