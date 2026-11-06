from video_factory.qc.evaluator import validate_qc_response
from video_factory.qc.models import QCStage
from pathlib import Path
from PIL import Image


def test_low_admission_allows_identity_metadata_mismatch_but_middle_blocks(tmp_path):
    from video_factory.automation.contracts import ExchangeImage, ExchangeProduct
    from video_factory.automation.importer import ExchangeImporter

    image = tmp_path / "product.png"
    Image.new("RGB", (32, 48), "red").save(image)
    batch = tmp_path / "batch"
    batch.mkdir()
    (batch / "product.png").write_bytes(image.read_bytes())
    product = ExchangeProduct(
        priority=1, canonical_product_key="wrong:variant", product_key="wrong-key",
        name="Wrong variant", brand="Wrong", model="Variant", images=[
            ExchangeImage(asset_id="image-1", relative_path="product.png")
        ], identity_mode="source_product", source_snapshot={}, source_product_id="source-1",
    )
    importer = ExchangeImporter(tmp_path)

    low_ok, low_reason, low_images = importer.validate_admission(product, batch, qc_level="low")
    middle_ok, _, _ = importer.validate_admission(product, batch, qc_level="middle")

    assert low_ok and low_reason is None
    assert low_images[0]["identity_review_reason"] == "LOW_QC_ADMITTED_FOR_PRODUCT_LOCK"
    assert not middle_ok


def test_current_failed_keyframe_qc_is_warning_only_in_low_and_blocking_in_middle():
    from video_factory.creative.agents.gates import require_storyboard_qc_pass
    from video_factory.creative.storyboard import storyboard_package_digest

    image = {"frame_id": "scene_1-start", "scene_id": "scene_1", "image_path": "unused.png", "sha256": "bad"}
    package = {
        "creative_mode": "four_agent", "workflow_id": "wf", "project_id": "project",
        "schema_version": "storyboard-package-v2", "scenes": [{"scene_id": "scene_1"}],
        "storyboard_frames": [{"scene_id": "scene_1", "keyframe_image_path": "unused.png"}],
        "generated_keyframes": [image], "recovery_attempt": 0,
    }
    package["package_digest"] = storyboard_package_digest(package)
    package["keyframe_qc_receipt"] = {
        "stage": "keyframe", "verdict": "fail", "artifact_digest": package["package_digest"],
        "findings": {"items": [{"frame_id": image["frame_id"], "verdict": "fail",
            "hard_failure": True, "reason": "wrong product identity"}]},
    }

    warning = require_storyboard_qc_pass(package, qc_level="low")

    assert warning["verdict"] == "fail"
    assert "wrong product identity" in warning["reason"]
    from video_factory.automation.creative_policy import evaluate_auto_gate
    low_decision = evaluate_auto_gate(state="AwaitingStoryboardApproval", artifact=package,
        reviews=(), policy={"creative_mode": "four_agent", "auto_export": True, "qc_level": "low"})
    assert low_decision["action"] == "advance"
    import pytest
    with pytest.raises(ValueError, match="current keyframe QC PASS"):
        require_storyboard_qc_pass(package, qc_level="middle")


def test_keyframe_score_is_informational_when_specific_checks_pass():
    result = validate_qc_response({
        'identity_consistency': 8, 'scene_adherence': 3, 'visual_cleanliness': 1,
        'total_score': 12, 'verdict': 'pass', 'hard_failure': False, 'reason': 'Correct product.',
        'black_bars_detected': False, 'prompt_reference_consistent': True,
        'script_adherence': True, 'important_details_preserved': True,
        'composition_usable': True, 'animation_suitable': False, 'recommended_action': 'minor motion polish',
    }, QCStage.KEYFRAME)

    assert result['verdict'] == 'pass'
    assert result['total_score'] == 12


def test_corrupt_final_mp4_is_rejected_regardless_of_qc_level(tmp_path):
    import pytest
    from video_factory.creative.agents.video_review import review_video

    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not an mp4")
    for level in ("low", "middle", "high"):
        with pytest.raises((OSError, ValueError)):
            review_video(path=broken, package={"qc_level": level, "scenes": [], "package_digest": "x"},
                         critic=None, stage="final")


def test_high_clip_gate_blocks_on_bad_content_even_when_technical_qc_passes(monkeypatch):
    from video_factory.creative.agents import video_review

    assessment = {"technical_qc": {"verdict": "PASS"}, "clip_review": {"verdict": "FAIL"}}
    seen = []

    def review(**kwargs):
        seen.append(kwargs["scene_id"])
        return assessment

    monkeypatch.setattr(video_review, "review_video", review)
    clips = [("s1", Path("s1.mp4")), ("s2", Path("s2.mp4"))]
    critic = object()
    assert video_review.check_completed_clips({"qc_level": "middle"}, clips, critic) is None
    assert seen == []
    assert video_review.check_completed_clips({"qc_level": "high"}, clips, critic) == (
        "s1", "CLIP_CONTENT_FAIL", assessment)
    assert seen == ["s1"]
