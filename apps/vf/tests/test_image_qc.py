"""Tests for Image QC logic, rubric scoring, persistence, and invalidation."""

import sqlite3
import tempfile
import json
from pathlib import Path
from unittest.mock import MagicMock

from video_factory.qc.image_qc import (
    compute_artifact_digest,
    run_image_qc,
)
from video_factory.qc.models import (
    NormalizedProduct,
    QCStage,
    QCVerdict,
)
from video_factory.qc.repository import QCReportRepository
from video_factory.qc.rubric import build_image_qc_prompt
from video_factory.qc.evaluator import QCResponseParseError, MultimodalEvaluator
from video_factory.qc import evaluator as evaluator_module


def _make_dummy_image(path: Path, color: str = "blue") -> None:
    from PIL import Image
    img = Image.new("RGB", (100, 100), color=color)
    img.save(path)


def _reference_profile() -> dict[str, object]:
    return {
        "view_angle": "front three-quarter",
        "shot_type": "clean product",
        "visible_details": ["product body", "controls"],
        "scene_suitability": ["hero", "detail"],
        "risk_flags": [],
        "text_coverage_percent": 0,
    }


def test_qc_repository_migrates_legacy_table_before_creating_fingerprint_index():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE product_video_qc_reports (
            report_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, stage TEXT NOT NULL,
            artifact_id TEXT NOT NULL, artifact_version INTEGER NOT NULL,
            artifact_digest TEXT NOT NULL, reference_digests TEXT NOT NULL,
            evaluator_name TEXT NOT NULL, evaluator_version TEXT NOT NULL,
            rubric_version TEXT NOT NULL, created_at TEXT NOT NULL, score INTEGER NOT NULL,
            verdict TEXT NOT NULL, findings_json TEXT NOT NULL, sample_timestamps_json TEXT
        )
        """
    )

    QCReportRepository(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(product_video_qc_reports)")}
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(product_video_qc_reports)")}
    assert {"fingerprint", "provider_mode", "model_identifier", "policy_version"} <= columns
    assert "idx_qc_fingerprint" in indexes


def test_configured_evaluator_requests_structured_json(monkeypatch, tmp_path):
    image_path = tmp_path / "reference.png"
    _make_dummy_image(image_path)
    captured: dict[str, object] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": json.dumps({
                    "identity_score": 55,
                    "clarity_score": 25,
                    "usability_score": 15,
                    "total_score": 95,
                    "verdict": "pass",
                    "eligible": True,
                    "hard_failure": False,
                    "source_branding_detected": False,
                    "identity_mismatch": False,
                    "reason": "Clean target product image",
                })}}]}
            ).encode("utf-8")

    def _urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(evaluator_module, "get_vertex_config", lambda: ("token", "https://vertex.example"))
    monkeypatch.setattr(evaluator_module.urllib.request, "urlopen", _urlopen)

    result = MultimodalEvaluator().evaluate_image(image_path, "Return the required object")

    assert result["verdict"] == "pass"
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert captured["body"]["max_tokens"] >= 2048


def test_image_qc_persists_truncated_evaluator_response_diagnostics(tmp_path):
    image_path = tmp_path / "reference.png"
    _make_dummy_image(image_path)
    evaluator = MagicMock()
    evaluator.evaluator_name = "configured_vision"
    evaluator.evaluator_version = "1.0"
    evaluator.model = "test/model"
    evaluator.evaluate_image.side_effect = QCResponseParseError(
        '{"identity_score": 55, "reason": "',
        "max_tokens",
        "Unterminated string starting at line 15 column 5 (char 336)",
    )

    report, _eligible = run_image_qc(
        "wf-truncated-response",
        {"resource_pack": {"resource_pack_id": "pack", "version": 1, "owner_user_id": "owner-truncated", "project_id": "proj-truncated", "assets": [{"asset_id": "a1", "local_path": str(image_path)}]}},
        NormalizedProduct(brand="Baseus", model="Bowie WM02"),
        evaluator=evaluator,
        attempt_id="qc-retry-2",
    )

    item = report.findings["items"][0]
    assert report.verdict == QCVerdict.UNKNOWN
    assert report.findings["attempt_id"] == "qc-retry-2"
    assert {key: item["findings"][key] for key in (
        "raw_response", "finish_reason", "parse_error",
    )} == {
        "raw_response": '{"identity_score": 55, "reason": "',
        "finish_reason": "max_tokens",
        "parse_error": "Unterminated string starting at line 15 column 5 (char 336)",
    }
    evaluator.evaluate_image.assert_called_once()


def test_image_qc_pass_when_two_eligible():
    with tempfile.TemporaryDirectory() as tmp:
        p1 = Path(tmp) / "img1.png"
        p2 = Path(tmp) / "img2.png"
        _make_dummy_image(p1)
        _make_dummy_image(p2, "gray")

        rp = {
            "resource_pack": {
                "resource_pack_id": "pack_1",
                "version": 1,
                "owner_user_id": "owner-1",
                "project_id": "proj-1",
                "assets": [
                    {"asset_id": "a1", "local_path": str(p1)},
                    {"asset_id": "a2", "local_path": str(p2)},
                ],
            }
        }
        product = NormalizedProduct(brand="Jisulife", model="Life 7", display_name="Jisulife Life 7")

        mock_evaluator = MagicMock()
        mock_evaluator.evaluator_name = "mock_vision"
        mock_evaluator.evaluator_version = "1.0"
        mock_evaluator.evaluate_image.return_value = {
            **_reference_profile(),
            "total_score": 90,
            "identity_score": 55,
            "clarity_score": 23,
            "usability_score": 12,
            "hard_failure": False,
            "eligible": True,
            "source_branding_detected": False,
            "identity_mismatch": False,
            "verdict": "pass",
            "reason": "Clear and matches product",
        }

        conn = sqlite3.connect(":memory:")
        repo = QCReportRepository(conn)

        report, eligible = run_image_qc("wf-1", rp, product, evaluator=mock_evaluator, repo=repo)

        assert report.verdict == QCVerdict.PASS
        assert report.score == 90
        assert len(eligible) == 2
        assert mock_evaluator.evaluate_image.call_count == 2

        # Verify saved in SQLite
        saved = repo.get_report(report.report_id)
        assert saved is not None
        assert saved.score == 90
        assert saved.stage == QCStage.IMAGE


def test_image_qc_score_alone_does_not_block_a_usable_reference():
    with tempfile.TemporaryDirectory() as tmp:
        p1 = Path(tmp) / "img1.png"
        p2 = Path(tmp) / "img2.png"
        _make_dummy_image(p1)
        _make_dummy_image(p2, "gray")

        rp = {
            "resource_pack": {
                "resource_pack_id": "pack_1",
                "version": 1,
                "owner_user_id": "owner-2",
                "project_id": "proj-2",
                "assets": [
                    {"asset_id": "a1", "local_path": str(p1)},
                    {"asset_id": "a2", "local_path": str(p2)},
                ],
            }
        }
        product = NormalizedProduct(brand="Jisulife", model="Life 7", display_name="Jisulife Life 7")

        mock_evaluator = MagicMock()
        mock_evaluator.evaluator_name = "mock_vision"
        mock_evaluator.evaluator_version = "1.0"
        mock_evaluator.evaluate_image.return_value = {
            **_reference_profile(),
            "total_score": 75,  # ranking score only
            "identity_score": 45,
            "clarity_score": 20,
            "usability_score": 10,
            "hard_failure": False,
            "eligible": True,
            "source_branding_detected": False,
            "identity_mismatch": False,
            "verdict": "pass",
            "reason": "Clear matching product despite lower ranking score",
        }

        report, eligible = run_image_qc("wf-2", rp, product, evaluator=mock_evaluator)
        assert report.verdict == QCVerdict.PASS
        assert len(eligible) == 2


def test_image_qc_hard_failure_blocks_eligibility():
    with tempfile.TemporaryDirectory() as tmp:
        p1 = Path(tmp) / "img1.png"
        _make_dummy_image(p1)

        rp = {
            "resource_pack": {
                "resource_pack_id": "pack_1",
                "version": 1,
                "owner_user_id": "owner-3",
                "project_id": "proj-3",
                "assets": [{"asset_id": "a1", "local_path": str(p1)}],
            }
        }
        product = NormalizedProduct(brand="Jisulife", model="Life 7", display_name="Jisulife Life 7")

        mock_evaluator = MagicMock()
        mock_evaluator.evaluator_name = "mock_vision"
        mock_evaluator.evaluator_version = "1.0"
        mock_evaluator.evaluate_image.return_value = {
            **_reference_profile(),
            "total_score": 40,
            "identity_score": 0,
            "clarity_score": 25,
            "usability_score": 15,
            "hard_failure": True,  # wrong model
            "eligible": False,
            "source_branding_detected": False,
            "identity_mismatch": True,
            "verdict": "fail",
            "reason": "Depicts Baseus earphone, not Jisulife fan",
        }

        report, eligible = run_image_qc("wf-3", rp, product, evaluator=mock_evaluator)
        assert report.verdict == QCVerdict.FAIL
        assert len(eligible) == 0


def test_image_qc_stale_report_invalidation():
    with tempfile.TemporaryDirectory() as tmp:
        p1 = Path(tmp) / "img1.png"
        p2 = Path(tmp) / "img2.png"
        _make_dummy_image(p1)
        _make_dummy_image(p2, "gray")

        rp1 = {
            "resource_pack": {
                "resource_pack_id": "pack_1",
                "version": 1,
                "owner_user_id": "owner-4",
                "project_id": "proj-4",
                "assets": [{"asset_id": "a1", "local_path": str(p1)}],
            }
        }
        product = NormalizedProduct(brand="Jisulife", model="Life 7", display_name="Jisulife Life 7")

        mock_evaluator = MagicMock()
        mock_evaluator.evaluator_name = "mock_vision"
        mock_evaluator.evaluator_version = "1.0"
        mock_evaluator.evaluate_image.return_value = {
            **_reference_profile(),
            "total_score": 95,
            "identity_score": 60,
            "clarity_score": 23,
            "usability_score": 12,
            "hard_failure": False,
            "eligible": True,
            "source_branding_detected": False,
            "identity_mismatch": False,
            "verdict": "pass",
            "reason": "OK",
        }

        conn = sqlite3.connect(":memory:")
        repo = QCReportRepository(conn)

        report1, _ = run_image_qc("wf-4", rp1, product, evaluator=mock_evaluator, repo=repo)
        assert report1.verdict == QCVerdict.PASS  # one usable reference is sufficient

        # Now simulate modifying resource pack by adding second asset (digest changes!)
        rp2 = {
            "resource_pack": {
                "resource_pack_id": "pack_1",
                "version": 1,
                "owner_user_id": "owner-4",
                "project_id": "proj-4",
                "assets": [
                    {"asset_id": "a1", "local_path": str(p1)},
                    {"asset_id": "a2", "local_path": str(p2)},
                ],
            }
        }
        new_digest = compute_artifact_digest(rp2["resource_pack"])

        # get_latest_report with new digest MUST return None because cached report is stale
        stale = repo.get_latest_report("wf-4", QCStage.IMAGE, expected_artifact_digest=new_digest)
        assert stale is None

        # Re-running with rp2 runs evaluator again and passes
        mock_evaluator.evaluate_image.reset_mock()
        report2, eligible2 = run_image_qc("wf-4", rp2, product, evaluator=mock_evaluator, repo=repo)
        assert report2.verdict == QCVerdict.PASS
        assert len(eligible2) == 2
        assert mock_evaluator.evaluate_image.call_count == 2

        # Calling run_image_qc again with identical rp2 reuses cached report (zero extra evaluator calls)
        mock_evaluator.evaluate_image.reset_mock()
        report_cached, _ = run_image_qc("wf-4", rp2, product, evaluator=mock_evaluator, repo=repo)
        assert report_cached.report_id == report2.report_id
        assert mock_evaluator.evaluate_image.call_count == 0


def test_image_qc_prompt_does_not_reject_cosmetic_branding():
    prompt = build_image_qc_prompt("AULA", "F75", display_name="AULA F75")
    lower_prompt = prompt.lower()
    assert "small text/watermark does not itself justify rejection" in lower_prompt
    assert "covers the product silhouette" in lower_prompt


def test_image_qc_prompt_treats_original_package_print_as_product_content():
    prompt = build_image_qc_prompt("Play More", "Gummy 48g", "Lemon Lime")
    lower_prompt = prompt.lower()
    assert "original printing on the product/package is allowed" in lower_prompt


def test_image_qc_excessive_text_warning_does_not_override_visual_pass():
    response = {
        "identity_score": 55, "clarity_score": 23, "usability_score": 12,
        "total_score": 90, "verdict": "pass", "eligible": True,
        "hard_failure": False, "source_branding_detected": False,
        "identity_mismatch": False, "view_angle": "rear",
        "shot_type": "clean package", "visible_details": ["ingredients"],
        "scene_suitability": ["rear"], "risk_flags": ["excessive_text"],
        "text_coverage_percent": 60, "reason": "Original package print is visible",
    }

    normalized = evaluator_module.validate_qc_response(response, QCStage.IMAGE)

    assert normalized["eligible"] is True
    assert normalized["verdict"] == "pass"


def test_image_qc_non_obscuring_watermark_does_not_block_eligibility():
    response = {
        "identity_score": 55, "clarity_score": 23, "usability_score": 12,
        "total_score": 90, "verdict": "pass", "eligible": True,
        "hard_failure": False, "source_branding_detected": False,
        "identity_mismatch": False, "view_angle": "front",
        "shot_type": "clean product", "visible_details": [],
        "scene_suitability": ["hero"], "risk_flags": ["watermark"],
        "text_coverage_percent": 0, "reason": "A source watermark is visible",
    }

    normalized = evaluator_module.validate_qc_response(response, QCStage.IMAGE)

    assert normalized["eligible"] is True
    assert normalized["verdict"] == "pass"


def test_image_qc_reads_assets_mapping_from_pi_raw_result(tmp_path):
    p1 = tmp_path / "img1.png"
    p2 = tmp_path / "img2.png"
    _make_dummy_image(p1)
    _make_dummy_image(p2, "gray")
    product = NormalizedProduct(brand="AULA", model="F75", display_name="AULA F75")

    evaluator = MagicMock()
    evaluator.evaluator_name = "mock_vision"
    evaluator.evaluator_version = "1.0"
    evaluator.evaluate_image.return_value = {
        **_reference_profile(),
        "total_score": 90,
        "identity_score": 55,
        "clarity_score": 23,
        "usability_score": 12,
        "hard_failure": False,
        "eligible": True,
        "source_branding_detected": False,
        "identity_mismatch": False,
        "verdict": "pass",
        "reason": "ok",
    }

    report, eligible = run_image_qc(
        "wf-raw-result",
        {"raw_result": {"resource_pack": {"resource_pack_id": "pack-raw-result", "version": 1, "owner_user_id": "owner-raw-result", "project_id": "proj-raw-result", "assets_mapping": {"a1": str(p1), "a2": str(p2)}}}},
        product,
        evaluator=evaluator,
    )

    assert report.verdict == QCVerdict.PASS
    assert [item.asset_id for item in eligible] == ["a1", "a2"]
