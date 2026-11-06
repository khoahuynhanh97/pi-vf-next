"""Tests for unified QC PASS validation at Product Lock, Render, and Export gates (T02, T03)."""
import pytest
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from video_factory.qc.models import QCReport, QCStage, QCVerdict
from video_factory.qc.repository import QCReportRepository
from video_factory.qc.validation import require_current_pass_report, QCValidationError
from video_factory.qc.normalization import resource_pack_fingerprint
from video_factory.qc.fingerprint import IMAGE_POLICY_VERSION, IMAGE_RUBRIC_VERSION
from video_factory.storage_infra.sqlite import connect
from video_factory.handlers._base import JobContext, ProductVideoJobHandlers
from video_factory.job_contracts import JobFailed


@pytest.fixture
def memory_db():
    conn = connect(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def sample_pack(tmp_path):
    import hashlib
    from PIL import Image

    paths = [tmp_path / "img1.png", tmp_path / "img2.png"]
    for idx, path in enumerate(paths):
        Image.new("RGB", (32, 32), (idx * 100, 80, 160)).save(path)
    return {
        "resource_pack_id": "pack_wf_test_1",
        "version": 1,
        "product_id": "prod_1",
        "pack_status": "ready_to_lock",
        "assets": [
            {
                "asset_id": "img_1",
                "sha256": hashlib.sha256(paths[0].read_bytes()).hexdigest(),
                "local_path": str(paths[0]),
                "clean_916_formatted": True,
            },
            {
                "asset_id": "img_2",
                "sha256": hashlib.sha256(paths[1].read_bytes()).hexdigest(),
                "local_path": str(paths[1]),
                "clean_916_formatted": True,
            },
        ],
    }


def _create_report(
    workflow_id="wf_test_1",
    stage=QCStage.IMAGE,
    verdict=QCVerdict.PASS,
    score=85,
    artifact_digest="sha256:dummy_digest",
    findings=None,
    owner_user_id="test_owner",
    project_id="test_proj",
    evaluator_name="eval_test",
    evaluator_version="1.0",
    rubric_version=IMAGE_RUBRIC_VERSION,
    policy_version=IMAGE_POLICY_VERSION,
    artifact_id="pack_wf_test_1",
    artifact_version=1,
    provider_mode="configured",
):
    if findings is None:
        findings = {
            "eligible_count": 2,
            "hard_failure": False,
            "items": [
                {
                    "asset_id": "img_1",
                    "sha256": "aaaa" * 16,
                    "score": score,
                    "verdict": verdict.value if hasattr(verdict, "value") else str(verdict),
                    "eligible": verdict == QCVerdict.PASS,
                    "hard_failure": False,
                    "findings": {
                        "eligible": verdict == QCVerdict.PASS,
                        "verdict": verdict.value if hasattr(verdict, "value") else str(verdict),
                        "hard_failure": False,
                        "source_branding_detected": False,
                        "identity_mismatch": False,
                        "risk_flags": [],
                    },
                },
                {
                    "asset_id": "img_2",
                    "sha256": "bbbb" * 16,
                    "score": score,
                    "verdict": verdict.value if hasattr(verdict, "value") else str(verdict),
                    "eligible": verdict == QCVerdict.PASS,
                    "hard_failure": False,
                    "findings": {
                        "eligible": verdict == QCVerdict.PASS,
                        "verdict": verdict.value if hasattr(verdict, "value") else str(verdict),
                        "hard_failure": False,
                        "source_branding_detected": False,
                        "identity_mismatch": False,
                        "risk_flags": [],
                    },
                },
            ],
        }
    return QCReport(
        report_id=f"rep_{datetime.now(timezone.utc).timestamp()}",
        workflow_id=workflow_id,
        stage=stage,
        artifact_id=artifact_id,
        artifact_version=artifact_version,
        artifact_digest=artifact_digest,
        reference_digests=["ref1", "ref2"],
        evaluator_name=evaluator_name,
        evaluator_version=evaluator_version,
        model_identifier="test-image-model",
        rubric_version=rubric_version,
        policy_version=policy_version,
        created_at=datetime.now(timezone.utc).isoformat(),
        score=score,
        verdict=verdict,
        findings=findings,
        provider_mode=provider_mode,
        owner_user_id=owner_user_id,
        project_id=project_id,
    )


def _bind_image_report_to_pack(report, pack):
    report.reference_digests = [ref["sha256"] for ref in pack["assets"]]
    for item, ref in zip(report.findings.get("items", []), pack["assets"]):
        item["sha256"] = ref["sha256"]
    return report


class TestRequireCurrentPassReport:
    """T02 Unit Tests: require_current_pass_report enforces strict criteria."""

    @pytest.mark.parametrize(
        ("field", "code"),
        [("rubric_version", "QC_RUBRIC_VERSION_MISMATCH"),
         ("policy_version", "QC_POLICY_VERSION_MISMATCH")],
    )
    def test_stale_image_policy_or_rubric_fails(self, memory_db, sample_pack, field, code):
        repo = QCReportRepository(memory_db)
        report = _bind_image_report_to_pack(
            _create_report(artifact_digest=resource_pack_fingerprint(sample_pack)), sample_pack,
        )
        setattr(report, field, "obsolete-version")
        repo.save_report(report)
        with pytest.raises(QCValidationError) as exc:
            require_current_pass_report(
                repo, workflow_id="wf_test_1", stage=QCStage.IMAGE,
                artifact=sample_pack, provider_mode="configured",
            )
        assert exc.value.code == code

    def test_missing_report_raises_error(self, memory_db, sample_pack):
        repo = QCReportRepository(memory_db)
        with pytest.raises(QCValidationError) as exc:
            require_current_pass_report(
                repo,
                workflow_id="wf_test_1",
                stage=QCStage.IMAGE,
                artifact=sample_pack,
                provider_mode="configured",
            )
        assert exc.value.code in ("QC_REPORT_MISSING", "QC_STALE_REPORT")

    def test_image_report_score_is_not_a_hard_gate(self, memory_db, sample_pack):
        repo = QCReportRepository(memory_db)
        digest = resource_pack_fingerprint(sample_pack)
        rep = _create_report(
            score=79,
            verdict=QCVerdict.PASS,
            artifact_digest=digest,
            findings={
                "eligible_count": 2,
                "hard_failure": False,
                "items": [
                    {
                        "asset_id": "img_1", "score": 85, "verdict": "pass", "eligible": True,
                        "hard_failure": False,
                        "findings": {"eligible": True, "verdict": "pass", "hard_failure": False,
                                     "source_branding_detected": False, "identity_mismatch": False, "risk_flags": []},
                    },
                    {
                        "asset_id": "img_2", "score": 85, "verdict": "pass", "eligible": True,
                        "hard_failure": False,
                        "findings": {"eligible": True, "verdict": "pass", "hard_failure": False,
                                     "source_branding_detected": False, "identity_mismatch": False, "risk_flags": []},
                    },
                ],
            },
        )
        _bind_image_report_to_pack(rep, sample_pack)
        repo.save_report(rep)

        result = require_current_pass_report(
            repo,
            workflow_id="wf_test_1",
            stage=QCStage.IMAGE,
            artifact=sample_pack,
            provider_mode="configured",
        )
        assert result.score == 79

    def test_current_image_report_accepts_one_selected_usable_reference(self, memory_db, sample_pack):
        sample_pack['assets'] = sample_pack['assets'][:1]
        repo = QCReportRepository(memory_db)
        rep = _create_report(
            score=66,
            verdict=QCVerdict.PASS,
            artifact_digest=resource_pack_fingerprint(sample_pack),
            findings={
                'eligible_count': 1,
                'hard_failure': False,
                'eligible_reference_manifest': [{
                    **sample_pack['assets'][0],
                    'eligible': True,
                    'score': 66,
                    'verdict': 'pass',
                }],
                'items': [{
                    'asset_id': sample_pack['assets'][0]['asset_id'],
                    'score': 66,
                    'verdict': 'pass',
                    'eligible': True,
                    'hard_failure': False,
                    'findings': {
                        'eligible': True,
                        'verdict': 'pass',
                        'hard_failure': False,
                        'source_branding_detected': False,
                        'identity_mismatch': False,
                        'risk_flags': [],
                    },
                }],
            },
        )
        _bind_image_report_to_pack(rep, sample_pack)
        repo.save_report(rep)

        result = require_current_pass_report(
            repo,
            workflow_id='wf_test_1',
            stage=QCStage.IMAGE,
            artifact=sample_pack,
            provider_mode='configured',
        )

        assert result.report_id == rep.report_id

    def test_verdict_fail_with_score_100_fails(self, memory_db, sample_pack):
        repo = QCReportRepository(memory_db)
        digest = resource_pack_fingerprint(sample_pack)
        rep = _create_report(score=100, verdict=QCVerdict.FAIL, artifact_digest=digest)
        repo.save_report(rep)

        with pytest.raises(QCValidationError) as exc:
            require_current_pass_report(
                repo,
                workflow_id="wf_test_1",
                stage=QCStage.IMAGE,
                artifact=sample_pack,
                provider_mode="configured",
            )
        assert exc.value.code == "QC_VERDICT_NOT_PASS"

    def test_verdict_unknown_with_score_100_fails(self, memory_db, sample_pack):
        repo = QCReportRepository(memory_db)
        digest = resource_pack_fingerprint(sample_pack)
        rep = _create_report(score=100, verdict=QCVerdict.UNKNOWN, artifact_digest=digest)
        repo.save_report(rep)

        with pytest.raises(QCValidationError) as exc:
            require_current_pass_report(
                repo,
                workflow_id="wf_test_1",
                stage=QCStage.IMAGE,
                artifact=sample_pack,
                provider_mode="configured",
            )
        assert exc.value.code == "QC_VERDICT_NOT_PASS"

    def test_stale_report_digest_mismatch_fails(self, memory_db, sample_pack):
        repo = QCReportRepository(memory_db)
        rep = _create_report(score=90, verdict=QCVerdict.PASS, artifact_digest="sha256:outdated_digest")
        repo.save_report(rep)

        with pytest.raises(QCValidationError) as exc:
            require_current_pass_report(
                repo,
                workflow_id="wf_test_1",
                stage=QCStage.IMAGE,
                artifact=sample_pack,
                provider_mode="configured",
            )
        assert exc.value.code == "QC_STALE_REPORT"

    def test_hard_failure_fails_even_with_pass_and_high_score(self, memory_db, sample_pack):
        repo = QCReportRepository(memory_db)
        digest = resource_pack_fingerprint(sample_pack)
        rep = _create_report(
            score=95,
            verdict=QCVerdict.PASS,
            artifact_digest=digest,
            findings={"hard_failure": True, "reason": "Severe artifact defect"},
        )
        repo.save_report(rep)

        with pytest.raises(QCValidationError) as exc:
            require_current_pass_report(
                repo,
                workflow_id="wf_test_1",
                stage=QCStage.IMAGE,
                artifact=sample_pack,
                provider_mode="configured",
            )
        assert exc.value.code == "QC_HARD_FAILURE"

    def test_insufficient_eligible_images_fails(self, memory_db, sample_pack):
        repo = QCReportRepository(memory_db)
        digest = resource_pack_fingerprint(sample_pack)
        rep = _create_report(
            score=90,
            verdict=QCVerdict.PASS,
            artifact_digest=digest,
            findings={"eligible_count": 1, "hard_failure": False},
        )
        _bind_image_report_to_pack(rep, sample_pack)
        repo.save_report(rep)

        with pytest.raises(QCValidationError) as exc:
            require_current_pass_report(
                repo,
                workflow_id="wf_test_1",
                stage=QCStage.IMAGE,
                artifact=sample_pack,
                provider_mode="configured",
            )
        assert exc.value.code == "QC_INSUFFICIENT_COVERAGE"

    def test_valid_pass_report_succeeds(self, memory_db, sample_pack, tmp_path):
        import hashlib

        for idx, ref in enumerate(sample_pack["assets"]):
            image_path = tmp_path / f"img_{idx}.png"
            from PIL import Image
            Image.new("RGB", (32, 32), (idx * 100, 40, 180)).save(image_path)
            ref["local_path"] = str(image_path)
            ref["sha256"] = hashlib.sha256(image_path.read_bytes()).hexdigest()
        repo = QCReportRepository(memory_db)
        digest = resource_pack_fingerprint(sample_pack)
        rep = _create_report(
            score=85,
            verdict=QCVerdict.PASS,
            artifact_digest=digest,
        )
        rep.reference_digests = [ref["sha256"] for ref in sample_pack["assets"]]
        for item, ref in zip(rep.findings["items"], sample_pack["assets"]):
            item["sha256"] = ref["sha256"]
        repo.save_report(rep)

        result = require_current_pass_report(
            repo,
            workflow_id="wf_test_1",
            stage=QCStage.IMAGE,
            artifact=sample_pack,
            provider_mode="configured",
        )
        assert result.report_id == rep.report_id
        assert result.score == 85

    def test_image_report_reuses_selected_pass_manifest_from_larger_pack(self, memory_db, sample_pack, tmp_path):
        """A saved PASS can select two images from a pack that also contains rejected images."""
        import hashlib

        rejected_path = tmp_path / "rejected.png"
        rejected_path.write_bytes(b"seller overlay asset")
        rejected = {
            "asset_id": "rejected",
            "sha256": hashlib.sha256(rejected_path.read_bytes()).hexdigest(),
            "local_path": str(rejected_path),
        }
        sample_pack["assets"].append(rejected)
        sample_pack["product_references"] = list(sample_pack["assets"])
        selected = [dict(
            ref, eligible=True, score=90, verdict="pass", hard_failure=False,
            findings={
                "eligible": True, "verdict": "pass", "hard_failure": False,
                "source_branding_detected": False, "identity_mismatch": False, "risk_flags": [],
            },
        ) for ref in sample_pack["assets"][:2]]
        rejected_item = dict(rejected, eligible=False, score=0, verdict="fail", hard_failure=True)
        digest = resource_pack_fingerprint(sample_pack)
        report = _create_report(
            score=92,
            verdict=QCVerdict.PASS,
            artifact_digest=digest,
            findings={
                "eligible_count": 2,
                "hard_failure": False,
                "eligible_reference_manifest": selected,
                "items": [
                    *[
                        {
                            "asset_id": ref["asset_id"],
                            "sha256": ref["sha256"],
                            "score": 90,
                            "verdict": "pass",
                            "eligible": True,
                            "hard_failure": False,
                            "findings": {
                                "eligible": True, "verdict": "pass", "hard_failure": False,
                                "source_branding_detected": False, "identity_mismatch": False,
                                "risk_flags": [],
                            },
                        }
                        for ref in selected
                    ],
                    rejected_item,
                ],
            },
        )
        report.reference_digests = [ref["sha256"] for ref in sample_pack["assets"]]
        repo = QCReportRepository(memory_db)
        repo.save_report(report)

        result = require_current_pass_report(
            repo,
            workflow_id="wf_test_1",
            stage=QCStage.IMAGE,
            artifact=sample_pack,
            provider_mode="configured",
        )

        assert result.report_id == report.report_id
        assert [item["asset_id"] for item in result.findings["eligible_reference_manifest"]] == [
            "img_1", "img_2"
        ]

    def test_image_pass_manifest_cannot_select_images_outside_current_pack(self, memory_db, sample_pack, tmp_path):
        import hashlib

        foreign_refs = []
        foreign_items = []
        for index in range(2):
            path = tmp_path / f"foreign-{index}.png"
            path.write_bytes(f"foreign image {index}".encode())
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            foreign_refs.append({
                "asset_id": f"foreign-{index}",
                "local_path": str(path),
                "sha256": sha,
                "eligible": True,
                "score": 90,
                "verdict": "pass",
            })
            foreign_items.append({
                "asset_id": f"foreign-{index}",
                "sha256": sha,
                "score": 90,
                "verdict": "pass",
                "eligible": True,
            })
        report = _create_report(
            artifact_digest=resource_pack_fingerprint(sample_pack),
            findings={
                "hard_failure": False,
                "eligible_reference_manifest": foreign_refs,
                "items": foreign_items,
            },
        )
        report.reference_digests = [item["sha256"] for item in foreign_refs]
        repo = QCReportRepository(memory_db)
        repo.save_report(report)

        with pytest.raises(QCValidationError) as exc:
            require_current_pass_report(
                repo,
                workflow_id="wf_test_1",
                stage=QCStage.IMAGE,
                artifact=sample_pack,
                provider_mode="configured",
            )

        assert exc.value.code == "QC_REFERENCE_DIGEST_MISMATCH"

    def test_configured_image_report_requires_readable_current_reference_bytes(self, memory_db, sample_pack):
        repo = QCReportRepository(memory_db)
        report = _create_report(artifact_digest=resource_pack_fingerprint(sample_pack))
        _bind_image_report_to_pack(report, sample_pack)
        repo.save_report(report)
        Path(sample_pack["assets"][0]["local_path"]).unlink()
        with pytest.raises(QCValidationError) as exc:
            require_current_pass_report(
                repo,
                workflow_id="wf_test_1",
                stage=QCStage.IMAGE,
                artifact=sample_pack,
                provider_mode="configured",
            )
        assert exc.value.code in ("QC_STALE_REPORT", "QC_REFERENCE_DIGEST_MISMATCH")

    def test_configured_report_missing_mandatory_owner_fails_closed(self, memory_db, sample_pack):
        repo = QCReportRepository(memory_db)
        digest = resource_pack_fingerprint(sample_pack)
        rep = _create_report(score=85, verdict=QCVerdict.PASS, artifact_digest=digest, owner_user_id="")
        repo.save_report(rep)

        with pytest.raises(QCValidationError) as exc:
            require_current_pass_report(
                repo,
                workflow_id="wf_test_1",
                stage=QCStage.IMAGE,
                artifact=sample_pack,
                provider_mode="configured",
            )
        assert exc.value.code == "QC_OWNER_MISMATCH"

    def test_configured_report_missing_mandatory_project_fails_closed(self, memory_db, sample_pack):
        repo = QCReportRepository(memory_db)
        digest = resource_pack_fingerprint(sample_pack)
        rep = _create_report(score=85, verdict=QCVerdict.PASS, artifact_digest=digest, project_id="")
        repo.save_report(rep)

        with pytest.raises(QCValidationError) as exc:
            require_current_pass_report(
                repo,
                workflow_id="wf_test_1",
                stage=QCStage.IMAGE,
                artifact=sample_pack,
                provider_mode="configured",
            )
        assert exc.value.code == "QC_PROJECT_MISMATCH"

    def test_configured_report_missing_evaluator_or_versions_fails_closed(self, memory_db, sample_pack):
        repo = QCReportRepository(memory_db)
        digest = resource_pack_fingerprint(sample_pack)
        rep = _create_report(score=85, verdict=QCVerdict.PASS, artifact_digest=digest, evaluator_name="")
        repo.save_report(rep)

        with pytest.raises(QCValidationError) as exc:
            require_current_pass_report(
                repo,
                workflow_id="wf_test_1",
                stage=QCStage.IMAGE,
                artifact=sample_pack,
                provider_mode="configured",
            )
        assert exc.value.code == "QC_EVALUATOR_MISSING"

    def test_report_with_two_digests_but_missing_item_pass_evidence_fails_closed(self, memory_db, sample_pack):
        """SPEC S2-S3: Report cannot infer coverage from reference_digests or eligible_count without item PASS."""
        repo = QCReportRepository(memory_db)
        digest = resource_pack_fingerprint(sample_pack)
        # reference_digests has 2, eligible_count=2, but NO items in findings
        rep = _create_report(
            score=90,
            verdict=QCVerdict.PASS,
            artifact_digest=digest,
            findings={"eligible_count": 2, "hard_failure": False, "reference_digests": ["aaaa"*16, "bbbb"*16]},
        )
        _bind_image_report_to_pack(rep, sample_pack)
        repo.save_report(rep)

        with pytest.raises(QCValidationError) as exc:
            require_current_pass_report(
                repo,
                workflow_id="wf_test_1",
                stage=QCStage.IMAGE,
                artifact=sample_pack,
                provider_mode="configured",
            )
        assert exc.value.code == "QC_INSUFFICIENT_COVERAGE"

    def test_report_with_two_digests_but_item_verdict_fail_fails_closed(self, memory_db, sample_pack):
        repo = QCReportRepository(memory_db)
        digest = resource_pack_fingerprint(sample_pack)
        rep = _create_report(
            score=85,
            verdict=QCVerdict.PASS,
            artifact_digest=digest,
            findings={
                "eligible_count": 2,
                "hard_failure": False,
                "items": [
                    {"asset_id": "img_1", "sha256": "aaaa" * 16, "score": 90, "verdict": "pass", "eligible": True},
                    {"asset_id": "img_2", "sha256": "bbbb" * 16, "score": 40, "verdict": "fail", "eligible": False},
                ],
            },
        )
        _bind_image_report_to_pack(rep, sample_pack)
        repo.save_report(rep)

        with pytest.raises(QCValidationError) as exc:
            require_current_pass_report(
                repo,
                workflow_id="wf_test_1",
                stage=QCStage.IMAGE,
                artifact=sample_pack,
                provider_mode="configured",
            )
        assert exc.value.code == "QC_INSUFFICIENT_COVERAGE"


class TestGateRejections:
    """T03 Tests: Endpoints and handlers strictly reject non-PASS or score < 80."""

    def test_lock_job_rejects_without_valid_qc_pass(self, memory_db, sample_pack, monkeypatch):
        wf_repo = MagicMock()
        wf_repo.conn = memory_db
        wf_repo.get_workflow.return_value = ({"workflow_id": "wf_test_1", "state": "building_resource_pack", "version": 1}, [])

        service = ProductVideoJobHandlers(workflow_repository=wf_repo)
        service.provider_mode = "configured"

        # Mock image byte hash resolution to succeed
        monkeypatch.setattr(
            "video_factory.handlers._base._resolve_image_bytes_hash",
            lambda a: a.get("sha256"),
        )

        ctx = JobContext(workflow_id="wf_test_1", job_id="job_lock_1", idempotency_key="idemp_1", owner_user_id="test_owner")

        # Case 1: No report at all -> lock_job must return JobFailed
        res = service.lock_job(ctx, sample_pack)
        assert isinstance(res, JobFailed)
        assert res.code == "APPROVED_IMAGE_MISMATCH"

        # Case 2: Report with FAIL score 100 -> lock_job must return JobFailed
        repo = QCReportRepository(memory_db)
        digest = resource_pack_fingerprint(sample_pack)
        rep = _create_report(score=100, verdict=QCVerdict.FAIL, artifact_digest=digest)
        repo.save_report(rep)

        res2 = service.lock_job(ctx, sample_pack)
        assert isinstance(res2, JobFailed)
        assert res2.code == "APPROVED_IMAGE_MISMATCH"

    def test_exchange_lock_accepts_missing_pack_status_only_with_current_pass_report(
        self, memory_db, sample_pack, monkeypatch,
    ):
        wf_repo = MagicMock()
        wf_repo.conn = memory_db
        wf_repo.get_workflow.return_value = (
            {
                "workflow_id": "wf_test_1",
                "project_id": "test_proj",
                "state": "building_resource_pack",
                "version": 1,
            },
            [],
        )
        service = ProductVideoJobHandlers(workflow_repository=wf_repo)
        service.provider_mode = "configured"
        monkeypatch.setattr(
            "video_factory.handlers._base._resolve_image_bytes_hash",
            lambda asset: asset.get("sha256"),
        )
        pack = dict(sample_pack)
        pack.pop("pack_status")
        pack["source_context"] = {"source": "hermes_product_video_exchange"}
        report = _bind_image_report_to_pack(
            _create_report(
                artifact_digest=resource_pack_fingerprint(pack),
                project_id="test_proj",
            ),
            pack,
        )
        QCReportRepository(memory_db).save_report(report)

        result = service.lock_job(
            JobContext(
                workflow_id="wf_test_1",
                job_id="job_lock_exchange",
                idempotency_key="lock-exchange-1",
                owner_user_id="test_owner",
            ),
            pack,
        )

        assert not isinstance(result, JobFailed)

    def test_repository_get_verified_pass_report_filters_high_score_fail(self, memory_db):
        repo = QCReportRepository(memory_db)
        rep = _create_report(
            stage=QCStage.VIDEO,
            verdict=QCVerdict.FAIL,
            score=95,
            artifact_digest="sha256:vid_digest",
        )
        repo.save_report(rep)

        retrieved = repo.get_verified_pass_report(
            owner_user_id="test_owner",
            project_id="test_proj",
            workflow_id="wf_test_1",
            artifact_id="pack_wf_test_1",
            artifact_version=1,
            stage=QCStage.VIDEO,
            expected_sha256="sha256:vid_digest",
        )
        assert retrieved is None

    def test_repository_get_verified_pass_report_filters_score_79_pass(self, memory_db):
        repo = QCReportRepository(memory_db)
        rep = _create_report(
            stage=QCStage.VIDEO,
            verdict=QCVerdict.PASS,
            score=79,
            artifact_digest="sha256:vid_digest",
        )
        repo.save_report(rep)

        retrieved = repo.get_verified_pass_report(
            owner_user_id="test_owner",
            project_id="test_proj",
            workflow_id="wf_test_1",
            artifact_id="pack_wf_test_1",
            artifact_version=1,
            stage=QCStage.VIDEO,
            expected_sha256="sha256:vid_digest",
        )
        assert retrieved is None

    def test_newer_fail_for_same_artifact_supersedes_older_pass(self, memory_db):
        repo = QCReportRepository(memory_db)
        old_pass = _create_report(
            stage=QCStage.VIDEO, verdict=QCVerdict.PASS, score=90,
            artifact_digest="sha256:vid_digest",
        )
        old_pass.report_id = "old-pass"
        old_pass.created_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        repo.save_report(old_pass)
        newer_fail = _create_report(
            stage=QCStage.VIDEO, verdict=QCVerdict.FAIL, score=95,
            artifact_digest="sha256:vid_digest",
        )
        newer_fail.report_id = "newer-fail"
        repo.save_report(newer_fail)

        assert repo.get_verified_pass_report(
            owner_user_id="test_owner", project_id="test_proj",
            workflow_id="wf_test_1", artifact_id="pack_wf_test_1",
            artifact_version=1, stage=QCStage.VIDEO,
            expected_sha256="sha256:vid_digest",
        ) is None

    def test_scene_renderer_receipt_uses_per_frame_verdict_not_aggregate_score(self):
        from video_factory.integrations.scene_renderer import _validate_keyframe_receipt

        ref_sha = "cccc" * 16
        keyframe = {
            "frame_id": "frame_1",
            "sha256": "aaaa" * 16,
            "reference_sha256s": [ref_sha],
        }

        receipt_sub_80 = {
            "stage": "keyframe",
            "verdict": "pass",
            "score": 79,
            "artifact_digest": "pkg_1",
            "findings": {
                "items": [
                    {
                        "frame_id": "frame_1",
                        "verdict": "pass",
                        "score": 79,
                        "sha256": "aaaa" * 16,
                        "reference_digests": [ref_sha],
                    }
                ]
            },
        }
        _validate_keyframe_receipt(keyframe=keyframe, receipt=receipt_sub_80, package_digest="pkg_1")

        receipt_fail_100 = {
            "stage": "keyframe",
            "verdict": "fail",
            "score": 100,
            "artifact_digest": "pkg_1",
            "findings": {
                "items": [
                    {
                        "frame_id": "frame_1",
                        "verdict": "fail",
                        "score": 100,
                        "sha256": "aaaa" * 16,
                        "reference_digests": [ref_sha],
                    }
                ]
            },
        }
        with pytest.raises(ValueError, match="Keyframe QC PASS receipt"):
            _validate_keyframe_receipt(keyframe=keyframe, receipt=receipt_fail_100, package_digest="pkg_1")

        receipt_valid_80 = {
            "stage": "keyframe",
            "verdict": "pass",
            "score": 80,
            "artifact_digest": "pkg_1",
            "findings": {
                "items": [
                    {
                        "frame_id": "frame_1",
                        "verdict": "pass",
                        "score": 80,
                        "sha256": "aaaa" * 16,
                        "reference_digests": [ref_sha],
                    }
                ]
            },
        }
        # Valid receipt should not raise
        _validate_keyframe_receipt(keyframe=keyframe, receipt=receipt_valid_80, package_digest="pkg_1")
