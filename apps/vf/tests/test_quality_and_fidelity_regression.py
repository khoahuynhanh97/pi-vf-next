"""Comprehensive regression tests for Product Video fidelity and fail-closed quality upgrades."""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image

from video_factory.integrations.composer import (
    InvalidVideoArtifact,
    VideoExpectation,
    check_black_bars_in_video,
    detect_black_bars,
    validate_export,
)
from video_factory.integrations.scene_renderer import (
    MANDATORY_VIDEO_NEGATIVE_PROMPT,
    compile_grounded_scene_request,
)
from video_factory.creative.contracts import (
    AudienceSpec,
    CreativeBriefV2,
    EligibleAsset,
    EvidenceClaim,
    ProductIdentityV2,
    ReasonToBelieve,
    ResearchDossier,
    ScriptScenePlanV2,
    ScriptSceneV2,
)
from video_factory.creative.prompts import (
    SCRIPT_SCENE_PLAN_PROMPT_V2,
    SCRIPT_SCENE_PLAN_V2_SCHEMA,
)
from video_factory.creative.storyboard import (
    KEYFRAME_NEGATIVE_PROMPT,
    compile_keyframe_requests,
)
from video_factory.creative.validation import (
    CreativeValidationError,
    validate_script,
)
from video_factory.qc.models import NormalizedProduct, QCReport, QCStage, QCVerdict
from video_factory.qc.image_qc import run_image_qc
from video_factory.qc.repository import QCReportRepository
from video_factory.qc.rubric import VIDEO_QC_SYSTEM_PROMPT
from video_factory.qc.video_qc import run_video_qc
from test_video_qc import _create_synthetic_mp4


def _create_sample_dossier(
    asset_dir: Path,
    *,
    workflow_id: str = "wf-test-regression",
    brand: str = "Ugreen",
    model: str = "Uno Robot 65W",
    silhouette: str = "compact robotic cuboid body with boot-shaped feet",
    display_shape: str = "glossy front LED matrix screen showing robotic emoji expressions",
    control_placement: str = "top surface contains 2 USB-C ports and 1 USB-A port",
    port_placement: str = "top surface only, never on front screen",
) -> ResearchDossier:
    identity = ProductIdentityV2(
        brand=brand,
        model=model,
        display_name=f"{brand} {model}",
        variant="Black Robot",
        distinctive_features=("robotic expression screen", "top USB ports", "removable magnetic boots"),
        silhouette=silhouette,
        display_shape=display_shape,
        control_placement=control_placement,
        port_placement=port_placement,
    )
    claim = EvidenceClaim(claim_id="c1", text="65W Fast Charging", evidence_refs=("ref1",))
    
    p1 = asset_dir / "a1.png"
    p2 = asset_dir / "a2.png"
    Image.new("RGB", (100, 100), color="blue").save(p1)
    Image.new("RGB", (100, 100), color="green").save(p2)

    sha1 = hashlib.sha256(p1.read_bytes()).hexdigest()
    sha2 = hashlib.sha256(p2.read_bytes()).hexdigest()

    asset1 = EligibleAsset(
        asset_id="a1",
        path=str(p1),
        sha256=sha1,
        qc_report_id="qc1",
        qc_score=90,
        view_angle="front_three_quarter",
    )
    asset2 = EligibleAsset(
        asset_id="a2",
        path=str(p2),
        sha256=sha2,
        qc_report_id="qc2",
        qc_score=85,
        view_angle="top_down_ports",
    )
    return ResearchDossier.create(
        workflow_id=workflow_id,
        product_identity=identity,
        verified_claims=(claim,),
        eligible_assets=(asset1, asset2),
    )


def _create_sample_brief(dossier: ResearchDossier) -> CreativeBriefV2:
    return CreativeBriefV2.create(
        audience=AudienceSpec(segment="Tech Lovers", situation="Desk Setup", desired_outcome="Fast charge with style"),
        consumer_insight="Desk chargers are boring blocks",
        problem="Chargers are ugly",
        creative_angle="A cute robot desktop companion that delivers fast power",
        hook="Meet the charger with personality",
        promise="65W ultra-fast charging",
        reason_to_believe=(ReasonToBelieve("3 Ports on top", ("c1",)),),
        tone="Energetic, modern",
        story_archetype="product_hero",
        cta="Power up your desk today",
        restrictions=(),
        target_duration_seconds=16,
        dossier_digest=dossier.digest(),
    )


def test_interaction_geometry_validation_fails_on_missing_landmarks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        dossier = _create_sample_dossier(Path(tmp))
        brief = _create_sample_brief(dossier)

        invalid_scene_1 = ScriptSceneV2(
            scene_id="s1",
            order=1,
            story_role="hook",
            duration_seconds=4,
            narration="Plug the cable in",
            overlay_text="Fast charge",
            visual_objective="Show cable insertion",
            environment="Modern desk setup A",
            subject_action="Hand inserts USB-C cable into top port",
            product_interaction="Cable clicks into top port C1",
            start_state="Unplugged",
            end_state="Plugged",
            visual_change="LED eyes change to charging expression",
            camera_framing="Close-up",
            camera_movement="Static",
            transition_out="Cut",
            interaction_actor="Human hand holding braided USB-C cable",
            interaction_target="Top surface USB-C port",
            contact_landmark="",  # EMPTY: triggers validation error
            approach_vector="Vertical downward from above",
            forbidden_contact_regions=("front glossy LED display screen",),
            interaction_risk="high",
            claim_refs=("c1",),
            reference_asset_ids=("a1",),
        )
        valid_scene_2 = ScriptSceneV2(
            scene_id="s2",
            order=2,
            story_role="demo",
            duration_seconds=4,
            narration="Look at the screen",
            overlay_text="Status",
            visual_objective="Show LED expression",
            environment="Living room table B",
            subject_action="Camera focuses on LED screen",
            product_interaction="Screen displays smiling face",
            start_state="Charging",
            end_state="Full power",
            visual_change="Screen blinks",
            camera_framing="Medium",
            camera_movement="Static",
            transition_out="Cut",
            claim_refs=("c1",),
            reference_asset_ids=("a2",),
        )
        valid_scene_3 = ScriptSceneV2(
            scene_id="s3",
            order=3,
            story_role="lifestyle",
            duration_seconds=4,
            narration="Compact footprint",
            overlay_text="Portable",
            visual_objective="Show small size",
            environment="Coffee shop C",
            subject_action="User places charger into backpack",
            product_interaction="Boots snap magnetically",
            start_state="On table",
            end_state="In bag",
            visual_change="Boots attached",
            camera_framing="Wide",
            camera_movement="Static",
            transition_out="Cut",
            claim_refs=("c1",),
            reference_asset_ids=("a1",),
        )
        valid_scene_4 = ScriptSceneV2(
            scene_id="s4",
            order=4,
            story_role="cta",
            duration_seconds=4,
            narration="Power up now",
            overlay_text="Ugreen Uno",
            visual_objective="Hero product shot",
            environment="Studio desk D",
            subject_action="Robot charger sits proudly",
            product_interaction="Stable power output",
            start_state="Standing",
            end_state="Glow",
            visual_change="Power lights steady",
            camera_framing="Hero shot",
            camera_movement="Static",
            transition_out="Cut",
            claim_refs=("c1",),
            reference_asset_ids=("a1",),
        )
        script_plan = ScriptScenePlanV2.create(
            target_duration_seconds=16,
            brief_digest=brief.digest(),
            scenes=(invalid_scene_1, valid_scene_2, valid_scene_3, valid_scene_4),
        )

        with pytest.raises(
            CreativeValidationError,
            match="requires contact_landmark, object_orientation",
        ):
            validate_script(dossier=dossier, brief=brief, script=script_plan)


def test_script_generation_contract_requires_interaction_geometry() -> None:
    scene_schema = SCRIPT_SCENE_PLAN_V2_SCHEMA["properties"]["scenes"]["items"]
    required = set(scene_schema["required"])
    expected_fields = {
        "interaction_actor",
        "interaction_target",
        "contact_landmark",
        "approach_vector",
        "object_orientation",
        "forbidden_contact_regions",
        "interaction_risk",
    }

    assert expected_fields <= set(scene_schema["properties"])
    assert expected_fields <= required
    for field_name in expected_fields:
        assert field_name in SCRIPT_SCENE_PLAN_PROMPT_V2


def test_prompt_compilers_enforce_identity_and_forbidden_regions() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        dossier = _create_sample_dossier(Path(tmp))
        brief = _create_sample_brief(dossier)

        scene = ScriptSceneV2(
            scene_id="s1",
            order=1,
            story_role="demo",
            duration_seconds=4,
            narration="Plug the cable in",
            overlay_text="Fast charge",
            visual_objective="Show cable insertion",
            environment="Modern desk",
            subject_action="Hand inserts USB-C cable into top port",
            product_interaction="Cable clicks into top port C1",
            start_state="Unplugged",
            end_state="Plugged",
            visual_change="LED eyes change to charging expression",
            camera_framing="Close-up",
            camera_movement="Static",
            transition_out="Cut",
            interaction_actor="Human hand holding braided USB-C cable",
            interaction_target="Top surface USB-C port",
            contact_landmark="Top USB-C1 port connector",
            approach_vector="Vertical downward from top",
            forbidden_contact_regions=("front glossy LED display screen", "side body"),
            interaction_risk="high",
            claim_refs=("c1",),
            reference_asset_ids=("a1",),
        )
        script_plan = ScriptScenePlanV2.create(
            target_duration_seconds=4,
            brief_digest=brief.digest(),
            scenes=(scene,),
        )

        keyframe_requests = compile_keyframe_requests(
            workflow_id="wf-1",
            project_id="proj-1",
            script=script_plan,
            dossier=dossier,
        )
        assert len(keyframe_requests) == 1
        kf_prompt = keyframe_requests[0].prompt
        assert "Forbidden contact regions" in kf_prompt
        assert "front glossy LED display screen" in kf_prompt
        assert "Immutable identity cues:" in kf_prompt
        assert "Interaction geometry:" in kf_prompt
        assert "cable entering forbidden region" in KEYFRAME_NEGATIVE_PROMPT

        kf_path = Path(tmp) / "kf1.png"
        Image.new("RGB", (720, 1280), color="purple").save(kf_path)
        kf_sha = hashlib.sha256(kf_path.read_bytes()).hexdigest()
        package_digest = "sha256:" + "c" * 64
        sha1 = dossier.eligible_assets[0].sha256

        keyframe_dict = {
            "scene_id": "s1",
            "frame_id": "s1-start",
            "image_path": str(kf_path),
            "sha256": kf_sha,
            "reference_sha256s": [sha1],
        }
        keyframe_qc_receipt = {
            "stage": "keyframe",
            "verdict": "pass",
            "score": 90,
            "hard_failure": False,
            "artifact_digest": package_digest,
            "findings": {
                "items": [
                    {
                        "scene_id": "s1",
                        "frame_id": "s1-start",
                        "verdict": "pass",
                        "score": 90,
                        "sha256": kf_sha,
                        "reference_digests": [sha1],
                    }
                ],
            },
        }

        video_req = compile_grounded_scene_request(
            owner_user_id="user-1",
            workflow_id="wf-1",
            project_id="proj-1",
            scene=asdict(scene),
            keyframe=keyframe_dict,
            keyframe_qc_receipt=keyframe_qc_receipt,
            package_digest=package_digest,
            product_identity="Ugreen Uno Robot 65W",
            provider_model="veo-2.0-generate-001",
        )
        vid_prompt = video_req.prompt
        assert "Forbidden contact regions:" in vid_prompt or "front glossy LED display screen" in vid_prompt
        assert "front glossy LED display screen" in vid_prompt
        assert "cable entering forbidden region" in MANDATORY_VIDEO_NEGATIVE_PROMPT
        assert "cable entering screen" in MANDATORY_VIDEO_NEGATIVE_PROMPT


def test_black_bar_detector_identifies_letterboxing_and_pillarboxing() -> None:
    clean_img = Image.new("RGB", (720, 1280), color=(128, 128, 128))
    res = detect_black_bars(clean_img)
    assert res["has_black_bars"] is False
    assert res["top_coverage"] == 0.0

    letterbox_img = Image.new("RGB", (720, 1280), color=(128, 128, 128))
    for y in range(40):
        for x in range(720):
            letterbox_img.putpixel((x, y), (0, 0, 0))
    res_lb = detect_black_bars(letterbox_img)
    assert res_lb["has_black_bars"] is True
    assert res_lb["top_coverage"] > 0.03


def test_validate_export_rejects_video_with_black_bars() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        vid_path = Path(tmp) / "black_bars.mp4"
        import av
        container = av.open(str(vid_path), mode="w")
        stream = container.add_stream("h264", rate=24)
        stream.width = 720
        stream.height = 1280
        stream.pix_fmt = "yuv420p"

        frame_img = Image.new("RGB", (720, 1280), color=(120, 120, 120))
        for y in range(50):
            for x in range(720):
                frame_img.putpixel((x, y), (0, 0, 0))

        for _ in range(24):
            frame = av.VideoFrame.from_image(frame_img)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
        container.close()

        bb_res = check_black_bars_in_video(vid_path)
        assert bb_res["has_persistent_black_bars"] is True
        assert bb_res["top_coverage"] > 0.03

        expectation = VideoExpectation(
            width=720,
            height=1280,
            duration_seconds=1.0,
            check_black_bars=True,
        )
        with pytest.raises(InvalidVideoArtifact, match="persistent black bars detected"):
            validate_export(vid_path, expectation)


def test_validate_export_fails_closed_when_black_bar_sampling_returns_no_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        vid_path = Path(tmp) / "valid.mp4"
        _create_synthetic_mp4(
            vid_path,
            width=720,
            height=1280,
            duration_sec=1.0,
            fps=24,
        )
        monkeypatch.setattr(
            "video_factory.integrations.composer.check_black_bars_in_video",
            lambda *_args, **_kwargs: {
                "has_persistent_black_bars": False,
                "samples_checked": 0,
                "error": "decoder unavailable",
            },
        )

        with pytest.raises(InvalidVideoArtifact, match="black bar validation failed"):
            validate_export(
                vid_path,
                VideoExpectation(
                    width=720,
                    height=1280,
                    duration_seconds=1.0,
                    check_black_bars=True,
                ),
            )


def test_qc_fail_closed_on_evaluator_error_or_malformed_json() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        video_path = Path(tmp) / "test.mp4"
        ref_path = Path(tmp) / "ref.png"
        Image.new("RGB", (100, 100), color="blue").save(ref_path)
        _create_synthetic_mp4(video_path, width=720, height=1280, duration_sec=1.0, fps=24)

        evaluator = MagicMock()
        evaluator.evaluate_multiple_images.side_effect = RuntimeError("Vision API network timeout")

        report = run_video_qc(
            workflow_id="wf-fail-closed",
            video_path=video_path,
            storyboard={"frames": [{"description": "Scene 1", "duration_seconds": 1.0}]},
            product=NormalizedProduct(brand="Baseus", model="WM02", display_name="Baseus Bowie WM02"),
            approved_reference_paths=[ref_path],
            expected_duration=1.0,
            evaluator=evaluator,
            owner_user_id="owner-fail-closed",
            project_id="proj-fail-closed",
            artifact_id="art-fail-closed",
            artifact_version=1,
        )

        assert report.verdict == QCVerdict.UNKNOWN
        assert report.score == 0
        assert report.findings["visual_scenes"][0]["hard_failure"] is False
        assert report.findings["visual_scenes"][0]["diagnostics"]["reconcile_required"] is True
        assert "RuntimeError" in report.findings["visual_scenes"][0].get("error", "") or "Vision API network timeout" in report.findings["visual_scenes"][0].get("error", "")


def test_image_qc_triggers_reacquire_when_insufficient_clean_references() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        img1 = Path(tmp) / "img1.png"
        Image.new("RGB", (100, 100), color="red").save(img1)

        evaluator = MagicMock()
        evaluator.evaluate_image.return_value = {
            "total_score": 30,
            "hard_failure": True,
            "source_branding_detected": True,
            "identity_mismatch": False,
            "verdict": "fail",
            "reason": "Shop watermark present",
            "eligible": False,
        }

        conn = sqlite3.connect(":memory:")
        repo = QCReportRepository(conn)

        report, eligible_items = run_image_qc(
            workflow_id="wf-reacquire",
            resource_pack={
                "resource_pack_id": "pack-reacquire",
                "version": 1,
                "owner_user_id": "owner-reacquire",
                "project_id": "proj-reacquire",
                "media_manifest": [{"asset_id": "img1", "path": str(img1)}],
            },
            product=NormalizedProduct(brand="Tribit", model="XSound Go", display_name="Tribit XSound Go"),
            evaluator=evaluator,
            repo=repo,
        )

        assert report.verdict == QCVerdict.FAIL
        assert "reacquire_references" in report.findings.get("recovery_actions", [])
