"""Tests for Auto-Retry Image QC helper functions."""

from __future__ import annotations

import pytest

from video_factory.qc.models import QCReport, QCStage, QCVerdict
from video_factory.qc.auto_retry_helpers import (
    is_cleanable_failure,
    extract_artifacts_from_qc,
    replace_images_in_resource_pack,
    extract_product_from_resource_pack,
)


def test_is_cleanable_failure_with_watermark():
    """Test cleanable failure detection for watermark issues."""
    qc_report = QCReport(
        report_id="qc_test_001",
        workflow_id="wf_001",
        stage=QCStage.IMAGE,
        artifact_id="rp_001",
        artifact_version=1,
        artifact_digest="abc123",
        reference_digests=["img1", "img2"],
        evaluator_name="gemini",
        evaluator_version="2.5",
        rubric_version="v1",
        created_at="2026-09-29T10:00:00Z",
        score=45,
        verdict=QCVerdict.FAIL,
        findings={
            "items": [
                {
                    "asset_id": "img_001",
                    "eligible": False,
                    "score": 40,
                    "reason": "Watermark detected",
                    "findings": {"risk_flags": ["watermark", "text_overlay"]},
                },
                {
                    "asset_id": "img_002",
                    "eligible": True,
                    "score": 85,
                    "findings": {"risk_flags": []},
                },
            ],
            "recovery_actions": [],
        },
        fingerprint="fp_001",
        owner_user_id="user_001",
        project_id="proj_001",
    )

    assert is_cleanable_failure(qc_report) is True


def test_is_cleanable_failure_terminal_low_score():
    """Test terminal failure detection for very low scores (corrupted images)."""
    qc_report = QCReport(
        report_id="qc_test_002",
        workflow_id="wf_002",
        stage=QCStage.IMAGE,
        artifact_id="rp_002",
        artifact_version=1,
        artifact_digest="def456",
        reference_digests=["img1"],
        evaluator_name="gemini",
        evaluator_version="2.5",
        rubric_version="v1",
        created_at="2026-09-29T10:00:00Z",
        score=10,
        verdict=QCVerdict.FAIL,
        findings={
            "items": [
                {
                    "asset_id": "img_001",
                    "eligible": False,
                    "score": 0,
                    "hard_failure": True,
                    "reason": "Image file corrupted",
                    "findings": {"risk_flags": []},
                }
            ],
            "recovery_actions": ["reacquire_references"],
        },
        fingerprint="fp_002",
        owner_user_id="user_001",
        project_id="proj_001",
    )

    assert is_cleanable_failure(qc_report) is False


def test_is_cleanable_failure_reacquire_needed():
    """Test terminal failure when reacquire_references action is needed."""
    qc_report = QCReport(
        report_id="qc_test_003",
        workflow_id="wf_003",
        stage=QCStage.IMAGE,
        artifact_id="rp_003",
        artifact_version=1,
        artifact_digest="ghi789",
        reference_digests=[],
        evaluator_name="gemini",
        evaluator_version="2.5",
        rubric_version="v1",
        created_at="2026-09-29T10:00:00Z",
        score=30,
        verdict=QCVerdict.FAIL,
        findings={
            "items": [],
            "recovery_actions": ["reacquire_references"],
            "recovery_reason": "Insufficient eligible images",
        },
        fingerprint="fp_003",
        owner_user_id="user_001",
        project_id="proj_001",
    )

    assert is_cleanable_failure(qc_report) is False


def test_extract_artifacts_from_qc():
    """Test artifact extraction from QC report."""
    qc_report = QCReport(
        report_id="qc_test_004",
        workflow_id="wf_004",
        stage=QCStage.IMAGE,
        artifact_id="rp_004",
        artifact_version=1,
        artifact_digest="jkl012",
        reference_digests=["img1", "img2", "img3"],
        evaluator_name="gemini",
        evaluator_version="2.5",
        rubric_version="v1",
        created_at="2026-09-29T10:00:00Z",
        score=50,
        verdict=QCVerdict.FAIL,
        findings={
            "items": [
                {
                    "asset_id": "img_001",
                    "eligible": False,
                    "score": 35,
                    "reason": "HDRADIO watermark detected",
                    "findings": {"risk_flags": ["watermark", "logo"]},
                },
                {
                    "asset_id": "img_002",
                    "eligible": False,
                    "score": 45,
                    "reason": "Seller banner overlay",
                    "findings": {"risk_flags": ["banner", "text_overlay"]},
                },
                {
                    "asset_id": "img_003",
                    "eligible": True,
                    "score": 90,
                    "findings": {"risk_flags": []},
                },
            ],
        },
        fingerprint="fp_004",
        owner_user_id="user_001",
        project_id="proj_001",
    )

    artifacts = extract_artifacts_from_qc(qc_report)

    assert len(artifacts) == 2
    
    # Check first artifact (img_001)
    assert artifacts[0]["asset_id"] == "img_001"
    assert "GENERIC_WATERMARK" in artifacts[0]["categories"]
    assert "SELLER_BRANDING" in artifacts[0]["categories"]
    assert artifacts[0]["confidence"] == 0.35
    
    # Check second artifact (img_002)
    assert artifacts[1]["asset_id"] == "img_002"
    assert "PROMOTIONAL_TAG" in artifacts[1]["categories"]
    assert "SPEC_CALLOUT_OVERLAY" in artifacts[1]["categories"]


def test_replace_images_in_resource_pack():
    """Test replacing original images with cleaned versions."""
    resource_pack = {
        "resource_pack_id": "rp_001",
        "version": 1,
        "media_assets": [
            {
                "asset_id": "img_001",
                "local_path": "/original/img_001.jpg",
                "sha256": "original_hash_1",
            },
            {
                "asset_id": "img_002",
                "local_path": "/original/img_002.jpg",
                "sha256": "original_hash_2",
            },
            {
                "asset_id": "img_003",
                "local_path": "/original/img_003.jpg",
                "sha256": "original_hash_3",
            },
        ],
    }

    cleaned_images = [
        {
            "asset_id": "img_001",
            "original_path": "/original/img_001.jpg",
            "cleaned_path": "/cleaned/img_001_clean.png",
            "clean_sha256": "cleaned_hash_1",
            "detected_watermarks": ["HDRADIO"],
            "detected_categories": ["GENERIC_WATERMARK"],
            "status": "cleaned",
        },
        {
            "asset_id": "img_002",
            "original_path": "/original/img_002.jpg",
            "cleaned_path": "/cleaned/img_002_clean.png",
            "clean_sha256": "cleaned_hash_2",
            "detected_watermarks": ["SELLER_BANNER"],
            "detected_categories": ["PROMOTIONAL_TAG"],
            "status": "cleaned",
        },
    ]

    cleaned_pack = replace_images_in_resource_pack(resource_pack, cleaned_images)

    # Check img_001 was replaced
    assert cleaned_pack["media_assets"][0]["local_path"] == "/cleaned/img_001_clean.png"
    assert cleaned_pack["media_assets"][0]["sha256"] == "cleaned_hash_1"
    assert cleaned_pack["media_assets"][0]["original_path"] == "/original/img_001.jpg"
    assert cleaned_pack["media_assets"][0]["cleaning_applied"] is True
    assert "HDRADIO" in cleaned_pack["media_assets"][0]["detected_artifacts"]

    # Check img_002 was replaced
    assert cleaned_pack["media_assets"][1]["local_path"] == "/cleaned/img_002_clean.png"
    assert cleaned_pack["media_assets"][1]["cleaning_applied"] is True

    # Check img_003 was NOT replaced (not in cleaned_images list)
    assert cleaned_pack["media_assets"][2]["local_path"] == "/original/img_003.jpg"
    assert cleaned_pack["media_assets"][2]["sha256"] == "original_hash_3"
    assert "cleaning_applied" not in cleaned_pack["media_assets"][2]


def test_extract_product_from_resource_pack():
    """Test product metadata extraction from various resource pack structures."""
    # Test with nested product dict
    rp_with_product = {
        "resource_pack_id": "rp_001",
        "product": {
            "brand": "JBL",
            "model": "PartyBox 310",
            "variant": None,
            "display_name": "JBL PartyBox 310 Portable Speaker",
            "verified_claims": ["240W output", "Waterproof IPX4"],
        },
    }

    product = extract_product_from_resource_pack(rp_with_product)
    assert product["brand"] == "JBL"
    assert product["model"] == "PartyBox 310"
    assert "240W output" in product["verified_claims"]

    # Test with top-level fields
    rp_flat = {
        "resource_pack_id": "rp_002",
        "brand": "Xiaomi",
        "model": "Robot Vacuum E10",
        "product_name": "Xiaomi Robot Vacuum E10",
        "key_features": ["4000Pa suction", "Auto-empty station"],
    }

    product = extract_product_from_resource_pack(rp_flat)
    assert product["brand"] == "Xiaomi"
    assert product["model"] == "Robot Vacuum E10"
    assert product["display_name"] == "Xiaomi Robot Vacuum E10"
