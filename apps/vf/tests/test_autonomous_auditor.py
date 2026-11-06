import pytest
from pathlib import Path

from video_factory.qc.auditor_agent import AuditorAgent, GateDecision
from video_factory.automation.autonomous_pipeline import AutonomousVideoPipeline


def test_auditor_agent_gate_2_duration_boundary():
    auditor = AuditorAgent(min_pass_score=80)
    
    # 20s should fail when bound is 30-50s
    short_brief = {
        "title": "Tai nghe Baseus",
        "target_duration_seconds": 20,
        "hook": "Tai nghe cực kỳ nhỏ gọn cho bạn",
        "reason_to_believe": [{"text": "Pin trâu 30h"}],
    }
    report = auditor.audit_gate_2_creative_brief(short_brief, target_min_seconds=30, target_max_seconds=50)
    assert report.decision == GateDecision.RETRY_REQUIRED
    assert any("out of required bounds" in r for r in report.reasons)

    # 35s should pass
    valid_brief = {
        "title": "Tai nghe Baseus",
        "target_duration_seconds": 35,
        "hook": "Tai nghe cực kỳ nhỏ gọn cho bạn",
        "reason_to_believe": [{"text": "Pin trâu 30h"}],
    }
    report_valid = auditor.audit_gate_2_creative_brief(valid_brief, target_min_seconds=30, target_max_seconds=50)
    assert report_valid.decision == GateDecision.APPROVED


def test_autonomous_pipeline_end_to_end(tmp_path):
    pipeline = AutonomousVideoPipeline(
        workspace_root=tmp_path,
        target_min_seconds=30,
        target_max_seconds=50,
        provider_mode="fake",
    )
    
    product_data = {
        "title": "Loa Bluetooth JBL",
        "features": ["Âm bass mạnh mẽ", "Kháng nước IPX7", "Pin 12 tiếng"],
        "product_anatomy": {
            "top_side": {"name": "Control buttons", "description": "Play, pause, volume on top"},
            "front_face": {"name": "Speaker grille", "description": "Front mesh with JBL logo"},
        },
    }
    
    dummy_img = tmp_path / "img1.jpg"
    dummy_img.touch()
    dummy_img2 = tmp_path / "img2.jpg"
    dummy_img2.touch()

    result = pipeline.run_pipeline(product_data, [dummy_img, dummy_img2])
    
    assert result.success is True
    assert 30.0 <= result.duration_seconds <= 50.0
    assert result.final_video_path is not None
    assert result.final_video_path.exists()


def test_autonomous_pipeline_configured_mode_fails_closed(tmp_path):
    result = AutonomousVideoPipeline(workspace_root=tmp_path).run_pipeline({}, [])

    assert result.success is False
    assert result.final_video_path is None
    assert "CONFIGURED_PIPELINE_UNAVAILABLE" in result.errors[0]



def test_autonomous_pipeline_is_blocked_outside_test(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    with pytest.raises(RuntimeError, match="DEPRECATED_AUTONOMOUS_PIPELINE"):
        AutonomousVideoPipeline(workspace_root=tmp_path, provider_mode="fake").run_pipeline({}, [])
    assert not list(tmp_path.iterdir())
