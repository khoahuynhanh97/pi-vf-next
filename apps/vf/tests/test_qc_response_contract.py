import pytest

from video_factory.qc.evaluator import validate_qc_response
from video_factory.qc.models import QCStage


@pytest.mark.parametrize("bad", [
    {"total_score": 95, "verdict": "pass"},
    {"total_score": 95, "verdict": "pass", "hard_failure": "false"},
])
def test_incomplete_response_never_becomes_pass(bad):
    with pytest.raises(ValueError):
        validate_qc_response(bad, QCStage.IMAGE)


def test_non_obscuring_source_branding_does_not_disqualify_a_matching_reference():
    result = validate_qc_response(
        {
            "identity_score": 60,
            "clarity_score": 25,
            "usability_score": 15,
            "total_score": 100,
            "verdict": "pass",
            "eligible": True,
            "hard_failure": False,
            "source_branding_detected": True,
            "identity_mismatch": False,
            "view_angle": "front",
            "shot_type": "clean product",
            "visible_details": ["product body"],
            "scene_suitability": ["hero"],
            "risk_flags": ["watermark"],
            "text_coverage_percent": 5,
            "reason": "Seller watermark visible",
        },
        QCStage.IMAGE,
    )

    assert result["verdict"] == "pass"
    assert result["eligible"] is True
    assert result["source_branding_detected"] is False
    assert result["hard_failure"] is False


def test_image_response_missing_reference_profile_fails_closed():
    with pytest.raises(ValueError, match="view_angle"):
        validate_qc_response(
            {
                "identity_score": 55,
                "clarity_score": 23,
                "usability_score": 12,
                "total_score": 90,
                "verdict": "pass",
                "eligible": True,
                "hard_failure": False,
                "source_branding_detected": False,
                "identity_mismatch": False,
                "reason": "Looks clean but profile is incomplete",
            },
            QCStage.IMAGE,
        )


def test_keyframe_response_missing_fidelity_gates_fails_closed():
    with pytest.raises(ValueError, match="prompt_reference_consistent"):
        validate_qc_response(
            {
                "identity_consistency": 45,
                "scene_adherence": 27,
                "visual_cleanliness": 18,
                "total_score": 90,
                "verdict": "pass",
                "hard_failure": False,
                "source_branding_detected": False,
                "identity_mismatch": False,
                "black_bars_detected": False,
                "reason": "Incomplete keyframe assessment",
            },
            QCStage.KEYFRAME,
        )


def test_video_response_rejects_component_sum_mismatch():
    with pytest.raises(ValueError):
        validate_qc_response(
            {
                "identity_consistency": 45,
                "scene_adherence": 25,
                "visual_cleanliness": 20,
                "total_score": 95,
                "verdict": "pass",
                "hard_failure": False,
                "source_branding_detected": False,
                "identity_mismatch": False,
                "reason": "ok",
            },
            QCStage.VIDEO,
        )


def test_keyframe_aesthetic_qc_strictly_validates_boolean_and_missing_records_warning():
    base_payload = {
        "identity_consistency": 45,
        "scene_adherence": 27,
        "visual_cleanliness": 18,
        "total_score": 90,
        "verdict": "pass",
        "hard_failure": False,
        "prompt_reference_consistent": True,
        "script_adherence": True,
        "important_details_preserved": True,
        "composition_usable": True,
        "animation_suitable": True,
        "recommended_action": "none",
        "reason": "Valid keyframe",
    }

    # 1. Non-boolean black_bars_detected must fail validation
    with pytest.raises(ValueError, match="must be a boolean"):
        validate_qc_response({**base_payload, "black_bars_detected": "false"}, QCStage.KEYFRAME)
    with pytest.raises(ValueError, match="must be a boolean"):
        validate_qc_response({**base_payload, "black_bars_detected": 1}, QCStage.KEYFRAME)

    # 2. Missing black_bars_detected records unknown/warning and proceeds without failing
    res_missing = validate_qc_response(base_payload, QCStage.KEYFRAME)
    assert res_missing["black_bars_detected"] == "unknown"
    assert "black_bars_detected:unknown" in res_missing.get("warnings", [])
    assert res_missing["verdict"] == "pass"
    assert res_missing["hard_failure"] is False

    # 3. Explicit False passes cleanly
    res_false = validate_qc_response({**base_payload, "black_bars_detected": False}, QCStage.KEYFRAME)
    assert res_false["black_bars_detected"] is False
    assert res_false["verdict"] == "pass"
    assert res_false["hard_failure"] is False

    # 4. Explicit True records aesthetic warning without hard failure
    res_true = validate_qc_response({**base_payload, "black_bars_detected": True}, QCStage.KEYFRAME)
    assert res_true["black_bars_detected"] is True
    assert "black_bars_detected:aesthetic_warning" in res_true.get("warnings", [])
    assert res_true["verdict"] == "pass"
    assert res_true["hard_failure"] is False
