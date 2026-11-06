from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from video_factory.creative.dossier import (
    DossierBuildError,
    build_research_dossier,
)
from video_factory.qc.models import QCReport, QCStage, QCVerdict


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _report(manifest: list[dict[str, object]]) -> QCReport:
    return QCReport(
        report_id="iqc-report-1",
        workflow_id="pv-dossier-test",
        stage=QCStage.IMAGE,
        artifact_id="pack-1",
        artifact_version=1,
        artifact_digest="pack-digest",
        reference_digests=[str(item["sha256"]) for item in manifest],
        evaluator_name="configured-evaluator",
        evaluator_version="1",
        rubric_version="2.0",
        created_at=datetime.now(timezone.utc).isoformat(),
        score=91,
        verdict=QCVerdict.PASS,
        findings={"eligible_reference_manifest": manifest},
    )


def test_build_research_dossier_uses_only_qc_eligible_media(tmp_path: Path) -> None:
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"
    rejected = tmp_path / "rejected.jpg"
    first.write_bytes(b"first-product-image")
    second.write_bytes(b"second-product-image")
    rejected.write_bytes(b"seller-listing-image")
    manifest = [
        {"asset_id": "asset-1", "uri": str(first), "sha256": _sha(first), "eligible": True},
        {"asset_id": "asset-2", "uri": str(second), "sha256": _sha(second), "eligible": True},
        {"asset_id": "asset-3", "uri": str(rejected), "sha256": _sha(rejected), "eligible": False},
    ]
    dossier = build_research_dossier(
        workflow_id="pv-dossier-test",
        resource_pack={
            "resource_pack_id": "pack-1",
            "product_identity": {
                "brand": "Baseus",
                "model": "Bowie WM02",
                "display_name": "Baseus Bowie WM02",
            },
        },
        qc_report=_report(manifest),
        research_snapshot={
            "claims": [
                {
                    "claim_id": "claim-01",
                    "text": "Tai nghe đi kèm hộp sạc",
                    "evidence_refs": ["evidence-01"],
                    "status": "verified",
                }
            ],
            "pain_points": ["Khó cất tai nghe nhanh khi di chuyển"],
            "use_cases": ["Rời bàn làm việc"],
            "source_refs": ["evidence-01"],
        },
    )

    assert [asset.asset_id for asset in dossier.eligible_assets] == ["asset-1", "asset-2"]
    assert dossier.product_identity.display_name == "Baseus Bowie WM02"
    assert dossier.verified_claims[0].evidence_refs == ("evidence-01",)
    assert "seller" not in dossier.to_json().lower()


def test_build_research_dossier_rejects_stale_media_digest(tmp_path: Path) -> None:
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"
    first.write_bytes(b"changed")
    second.write_bytes(b"second")
    manifest = [
        {"asset_id": "asset-1", "uri": str(first), "sha256": "0" * 64, "eligible": True},
        {"asset_id": "asset-2", "uri": str(second), "sha256": _sha(second), "eligible": True},
    ]

    with pytest.raises(DossierBuildError, match="digest"):
        build_research_dossier(
            workflow_id="pv-dossier-test",
            resource_pack={
                "resource_pack_id": "pack-1",
                "product_identity": {
                    "brand": "Baseus",
                    "model": "Bowie WM02",
                    "display_name": "Baseus Bowie WM02",
                },
            },
            qc_report=_report(manifest),
            research_snapshot={"claims": []},
        )
