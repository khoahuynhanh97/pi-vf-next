import hashlib
from pathlib import Path

from video_factory.creative.generator import SingleModelCreativeGenerator
from video_factory.handlers import common
from video_factory.qc.models import QCReport, QCStage, QCVerdict


class _ReportRepository:
    def __init__(self, report: QCReport) -> None:
        self.report = report

    def get_latest_report(self, _workflow_id: str, _stage: QCStage) -> QCReport:
        return self.report


class _UnusedModel:
    pass


def test_no_source_backed_claims_use_only_product_name_and_qc_visible_details(
    tmp_path: Path, monkeypatch,
) -> None:
    assets = []
    for index, payload in enumerate((b"approved image one", b"approved image two"), start=1):
        path = tmp_path / f"image-{index}.jpg"
        path.write_bytes(payload)
        assets.append({
            "asset_id": f"asset-{index}",
            "uri": str(path),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "eligible": True,
            "visual_observations": [f"visible detail {index}"],
        })

    report = QCReport(
        report_id="qc-image-pass",
        workflow_id="wf-neutral",
        stage=QCStage.IMAGE,
        artifact_id="resource-pack",
        artifact_version=1,
        artifact_digest="sha256:pack",
        reference_digests=[],
        evaluator_name="test",
        evaluator_version="1",
        rubric_version="1",
        created_at="2026-10-21T00:00:00+00:00",
        score=90,
        verdict=QCVerdict.PASS,
        findings={"eligible_reference_manifest": assets},
    )
    monkeypatch.setattr(common, "QCReportRepository", lambda _connection: _ReportRepository(report))

    dossier = common._locked_research_dossier(
        workflow_id="wf-neutral",
        aggregate={
            "source_context": {
                "brand": "Brand",
                "model": "Model 1",
                "variant": "Blue",
            },
            "resource_pack": {
                "product_name": "Brand Model 1 Blue",
                "product_identity": {"brand": "Brand", "model": "Model 1", "variant": "Blue"},
                "key_features": ["100W fast charging"],
                "claims": [{"claim_id": "pi-power", "text": "100W fast charging", "status": "verified", "evidence_refs": ["fake"]}],
            },
        },
        locked_pack={},
        connection=object(),
    )

    claims = dossier["verified_claims"]
    assert claims
    assert any("Brand Model 1 Blue" in claim["text"] for claim in claims)
    assert any("visible detail 1" in claim["text"] for claim in claims)
    assert all("100W" not in claim["text"] for claim in claims)
    assert all(claim.get("source_url") != "exchange_manifest" for claim in claims)

    generator = SingleModelCreativeGenerator(_UnusedModel())
    from video_factory.creative.contracts import ResearchDossier

    assert generator._has_admissible_claims(ResearchDossier.from_dict(dossier)) is True
