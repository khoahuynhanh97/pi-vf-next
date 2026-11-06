"""Test spatial anatomy lock and auditor gate 1 enforcement."""

from video_factory.creative.spatial_anatomy import ComponentPosition, SpatialAnatomy, extract_from_product_dossier
from video_factory.qc.auditor_agent import AuditorAgent, GateDecision


def test_spatial_anatomy_extraction():
    dossier = {
        "product_name": "Ugreen Uno Robot GaN 30W",
        "product_anatomy": {
            "top_side": "USB-C port at center top, cable plugs downward",
            "front_face": "LED screen with 2 pixel robot eyes, blinks when charging",
            "bottom_side": "2 flat US/EU plug prongs, magnetic shoe cover when unplugged",
            "critical_donts": [
                "cable plugged into side or front",
                "plug prongs turned into human legs",
                "distorted box shape",
            ],
        },
    }
    anatomy = extract_from_product_dossier(dossier)
    assert anatomy is not None
    assert anatomy.product_name == "Ugreen Uno Robot GaN 30W"
    assert len(anatomy.components) == 3
    
    prompt_text = anatomy.to_prompt_injection()
    assert "USB-C port" in prompt_text
    assert "LED screen" in prompt_text
    
    neg = anatomy.to_negative_prompt()
    assert "cable plugged into side" in neg
    assert "human legs" in neg


def test_auditor_gate_1_missing_anatomy():
    auditor = AuditorAgent(min_pass_score=80)
    candidate = {"product_name": "Test Product"}
    image_qc = [
        {"verdict": "pass", "score": 85, "hard_failure": False},
        {"verdict": "pass", "score": 90, "hard_failure": False},
    ]
    report = auditor.audit_gate_1_product_lock(candidate, image_qc)
    assert report.decision == GateDecision.RETRY_REQUIRED
    assert "spatial anatomy not defined" in report.reasons[0]
    assert report.metadata.get("requires_anatomy") is True


def test_auditor_gate_1_with_anatomy():
    auditor = AuditorAgent(min_pass_score=80)
    candidate = {
        "product_name": "Ugreen Uno Robot",
        "product_anatomy": {
            "top_side": "USB-C port center",
            "front_face": "LED screen",
        },
    }
    image_qc = [
        {"verdict": "pass", "score": 85, "hard_failure": False},
        {"verdict": "pass", "score": 90, "hard_failure": False},
    ]
    report = auditor.audit_gate_1_product_lock(candidate, image_qc)
    assert report.decision == GateDecision.APPROVED
    assert report.score == 90
    assert "spatial anatomy defined" in report.reasons[0]
    assert report.metadata.get("anatomy") is not None
