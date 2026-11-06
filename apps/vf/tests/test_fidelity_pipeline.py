from __future__ import annotations

import hashlib
from dataclasses import asdict
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image

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
from video_factory.creative.dossier import build_research_dossier
from video_factory.creative.storyboard import (
    compile_keyframe_requests,
    storyboard_package_digest,
)
from video_factory.creative.prompts import SCRIPT_SCENE_PLAN_PROMPT_V2
from video_factory.creative.validation import (
    CreativeValidationError,
    validate_script,
)
from video_factory.qc.image_qc import run_image_qc
from video_factory.qc.keyframe_qc import run_keyframe_qc
from video_factory.qc.models import NormalizedProduct, QCReport, QCStage, QCVerdict
from video_factory.integrations.scene_renderer import compile_grounded_scene_request


def _image(path: Path, color: str) -> None:
    Image.new("RGB", (120, 120), color=color).save(path)


def test_script_prompt_duration_example_uses_five_to_eight_scenes() -> None:
    """Verify script prompt examples reflect 30-50s duration with 5-8 scenes."""
    # Should NOT contain old short-duration examples
    assert "6 + 4 + 4 + 6" not in SCRIPT_SCENE_PLAN_PROMPT_V2
    assert "5x4s" not in SCRIPT_SCENE_PLAN_PROMPT_V2
    
    # Should contain new 30-50s examples
    assert "30s = 5x6s" in SCRIPT_SCENE_PLAN_PROMPT_V2 or "30s = 6+8+6+6+4" in SCRIPT_SCENE_PLAN_PROMPT_V2
    assert "40s = 5x8s" in SCRIPT_SCENE_PLAN_PROMPT_V2 or "40s = 6+6+8+8+6+6" in SCRIPT_SCENE_PLAN_PROMPT_V2
    assert "48s = 6x8s" in SCRIPT_SCENE_PLAN_PROMPT_V2 or "48s = 8x6s" in SCRIPT_SCENE_PLAN_PROMPT_V2
    
    # Should specify 5-8 scenes and 30-50s range
    assert "5 and 8 scenes" in SCRIPT_SCENE_PLAN_PROMPT_V2 or "5 8 scenes" in SCRIPT_SCENE_PLAN_PROMPT_V2
    assert "30 and 50 seconds" in SCRIPT_SCENE_PLAN_PROMPT_V2 or "30 50 seconds" in SCRIPT_SCENE_PLAN_PROMPT_V2


def _qc_response(score: int, *, view_angle: str, suitability: list[str]) -> dict[str, Any]:
    return {
        "identity_score": min(60, score - 35),
        "clarity_score": 22,
        "usability_score": 13,
        "total_score": score,
        "verdict": "pass",
        "eligible": True,
        "hard_failure": False,
        "source_branding_detected": False,
        "identity_mismatch": False,
        "view_angle": view_angle,
        "shot_type": "clean product close-up",
        "visible_details": ["white circular base", "single slim lamp stem"],
        "scene_suitability": suitability,
        "risk_flags": [],
        "text_coverage_percent": 0,
        "reason": "clean target product reference",
    }


def test_image_qc_rejects_duplicate_bytes_and_ranks_reference_manifest(tmp_path: Path) -> None:
    front = tmp_path / "front.png"
    duplicate = tmp_path / "front-copy.png"
    detail = tmp_path / "detail.png"
    _image(front, "white")
    duplicate.write_bytes(front.read_bytes())
    _image(detail, "gray")
    evaluator = MagicMock()
    evaluator.evaluator_name = "ranked-vision"
    evaluator.evaluator_version = "2"
    evaluator.model = "vision-model"
    evaluator.evaluate_image.side_effect = [
        _qc_response(88, view_angle="front three-quarter", suitability=["hero", "context"]),
        _qc_response(95, view_angle="close-up controls", suitability=["detail", "interaction"]),
    ]

    report, eligible = run_image_qc(
        "wf-ranked",
        {
            "resource_pack": {
                "resource_pack_id": "pack-ranked",
                "version": 1,
                "owner_user_id": "owner-ranked",
                "project_id": "proj-ranked",
                "assets": [
                    {"asset_id": "front", "local_path": str(front)},
                    {"asset_id": "duplicate", "local_path": str(duplicate)},
                    {"asset_id": "detail", "local_path": str(detail)},
                ],
            }
        },
        NormalizedProduct(brand="Xiaomi", model="Mijia Desk Lamp Lite"),
        evaluator=evaluator,
    )

    assert evaluator.evaluate_image.call_count == 2
    assert report.verdict is QCVerdict.PASS
    assert [item.asset_id for item in eligible] == ["detail", "front"]
    manifest = report.findings["eligible_reference_manifest"]
    assert [item["asset_id"] for item in manifest] == ["detail", "front"]
    assert manifest[0]["reference_rank"] == 1
    assert manifest[0]["view_angle"] == "close-up controls"
    assert manifest[0]["scene_suitability"] == ["detail", "interaction"]
    duplicate_item = next(item for item in report.findings["items"] if item["asset_id"] == "duplicate")
    assert duplicate_item["eligible"] is False
    assert "duplicate" in duplicate_item["reason"].lower()


def test_non_exclusionary_risk_flags_are_warnings_when_identity_and_threshold_pass(tmp_path: Path) -> None:
    collage = tmp_path / "collage.png"
    clean = tmp_path / "clean.png"
    _image(collage, "red")
    _image(clean, "white")
    risky = _qc_response(95, view_angle="front", suitability=["hero"])
    risky["risk_flags"] = ["collage", "excessive_text"]
    evaluator = MagicMock()
    evaluator.evaluator_name = "risk-vision"
    evaluator.evaluator_version = "2"
    evaluator.model = "vision-model"
    evaluator.evaluate_image.side_effect = [
        risky,
        _qc_response(90, view_angle="front three-quarter", suitability=["hero"]),
    ]

    report, eligible = run_image_qc(
        "wf-risk",
        {
            "resource_pack": {
                "resource_pack_id": "pack-risk",
                "version": 1,
                "owner_user_id": "owner-risk",
                "project_id": "proj-risk",
                "assets": [
                    {"asset_id": "collage", "local_path": str(collage)},
                    {"asset_id": "clean", "local_path": str(clean)},
                ],
            }
        },
        NormalizedProduct(brand="Xiaomi", model="Mijia Desk Lamp Lite"),
        evaluator=evaluator,
    )

    assert report.verdict is QCVerdict.PASS
    assert [item.asset_id for item in eligible] == ["collage", "clean"]
    assert report.findings["eligible_reference_manifest"][0]["risk_flags"] == ["collage", "excessive_text"]


def test_dossier_preserves_ranked_reference_profile(tmp_path: Path) -> None:
    first = tmp_path / "front.png"
    second = tmp_path / "detail.png"
    _image(first, "white")
    _image(second, "gray")
    manifest = [
        {
            "asset_id": "detail",
            "uri": str(second),
            "sha256": hashlib.sha256(second.read_bytes()).hexdigest(),
            "eligible": True,
            "reference_rank": 1,
            "qc_score": 95,
            "view_angle": "close-up controls",
            "shot_type": "detail",
            "visible_details": ["touch control"],
            "scene_suitability": ["interaction", "detail"],
            "risk_flags": [],
        },
        {
            "asset_id": "front",
            "uri": str(first),
            "sha256": hashlib.sha256(first.read_bytes()).hexdigest(),
            "eligible": True,
            "reference_rank": 2,
            "qc_score": 88,
            "view_angle": "front three-quarter",
            "shot_type": "hero",
            "visible_details": ["circular base"],
            "scene_suitability": ["hero", "context"],
            "risk_flags": [],
        },
    ]
    report = QCReport(
        report_id="qc-ranked",
        workflow_id="wf-ranked",
        stage=QCStage.IMAGE,
        artifact_id="pack-ranked",
        artifact_version=1,
        artifact_digest="sha256:pack",
        reference_digests=[str(item["sha256"]) for item in manifest],
        evaluator_name="vision",
        evaluator_version="2",
        rubric_version="3",
        created_at="2026-09-17T00:00:00+00:00",
        score=92,
        verdict=QCVerdict.PASS,
        findings={"eligible_reference_manifest": manifest},
    )

    dossier = build_research_dossier(
        workflow_id="wf-ranked",
        resource_pack={
            "product_identity": {
                "brand": "Xiaomi",
                "model": "Mijia Desk Lamp Lite",
                "display_name": "Xiaomi Mijia Desk Lamp Lite",
                "shape": "round base with a single slim stem",
                "color": "white",
                "materials": "matte plastic and metal",
                "logo_placement": "small Mijia mark on the base",
            }
        },
        qc_report=report,
        research_snapshot={"claims": []},
    )

    assert [asset.asset_id for asset in dossier.eligible_assets] == ["detail", "front"]
    assert dossier.eligible_assets[0].reference_rank == 1
    assert dossier.eligible_assets[0].view_angle == "close-up controls"
    assert dossier.eligible_assets[0].scene_suitability == ("interaction", "detail")
    assert dossier.product_identity.shape == "round base with a single slim stem"
    assert dossier.product_identity.color == "white"
    assert dossier.product_identity.materials == "matte plastic and metal"
    assert dossier.product_identity.logo_placement == "small Mijia mark on the base"


def _dossier(tmp_path: Path) -> ResearchDossier:
    hero = tmp_path / "hero.png"
    detail = tmp_path / "detail.png"
    _image(hero, "white")
    _image(detail, "gray")
    return ResearchDossier.create(
        workflow_id="wf-fidelity",
        product_identity=ProductIdentityV2(
            "Xiaomi",
            "Mijia Desk Lamp Lite",
            "Xiaomi Mijia Desk Lamp Lite",
            "white",
            ("circular white base", "single slim stem", "wide horizontal light bar"),
        ),
        verified_claims=(EvidenceClaim("claim-light", "Provides desk lighting", ("source-1",)),),
        eligible_assets=(
            EligibleAsset(
                "hero",
                str(hero),
                hashlib.sha256(hero.read_bytes()).hexdigest(),
                "qc-ranked",
                reference_rank=2,
                qc_score=90,
                view_angle="front three-quarter",
                shot_type="hero",
                visible_details=("circular white base", "single slim stem"),
                scene_suitability=("hero", "context"),
            ),
            EligibleAsset(
                "detail",
                str(detail),
                hashlib.sha256(detail.read_bytes()).hexdigest(),
                "qc-ranked",
                reference_rank=1,
                qc_score=95,
                view_angle="close-up controls",
                shot_type="detail",
                visible_details=("touch control", "light bar"),
                scene_suitability=("interaction", "detail"),
            ),
        ),
    )


def _brief(dossier: ResearchDossier) -> CreativeBriefV2:
    return CreativeBriefV2.create(
        audience=AudienceSpec("desk workers", "evening work", "clear desk lighting"),
        consumer_insight="Lighting changes how the desk feels",
        problem="The desk is dim",
        creative_angle="One deliberate light-on interaction",
        hook="A dim desk changes in one touch",
        promise="Show the real lamp in use",
        reason_to_believe=(ReasonToBelieve("Desk lighting", ("claim-light",)),),
        tone="clean and practical",
        story_archetype="problem_solution_demo",
        cta="See the product details.",
        restrictions=("No seller text",),
        target_duration_seconds=16,
        dossier_digest=dossier.digest(),
    )


def _scene(**overrides: Any) -> ScriptSceneV2:
    values: dict[str, Any] = {
        "scene_id": "scene-01",
        "order": 1,
        "story_role": "interaction",
        "duration_seconds": 4,
        "narration": "Bật đèn cho góc làm việc.",
        "overlay_text": "Bật sáng góc bàn",
        "visual_objective": "Show one real touch interaction",
        "environment": "Minimal home-office desk at night",
        "subject_action": "A hand presses the lamp control once",
        "product_interaction": "The real control is pressed and the light turns on",
        "start_state": "The lamp is off and the hand is beside the base",
        "end_state": "The same lamp is on and lighting the desk",
        "visual_change": "The light bar changes from off to visibly illuminated",
        "camera_framing": "Vertical close-up showing base, stem, and light bar",
        "camera_movement": "Locked camera with a subtle action-following adjustment",
        "transition_out": "Cut on the light turning on",
        "claim_refs": ("claim-light",),
        "reference_asset_ids": ("detail", "hero"),
        "product_identity_cues": ("circular white base", "single slim stem", "wide horizontal light bar"),
        "product_placement": "Lamp fully visible on the right third; control unobstructed",
        "lighting": "Cool room ambience; lamp itself starts off",
        "continuity_from_previous": "First scene; establish the unchanged white lamp",
        "continuity_to_next": "End with the lamp on in the same position and orientation",
        "required_visible_details": ("touch control", "circular base", "light bar"),
        "reference_requirements": ("interaction", "close-up controls", "front three-quarter"),
    }
    values.update(overrides)
    return ScriptSceneV2(**values)


def test_v3_script_rejects_missing_identity_placement_and_continuity(tmp_path: Path) -> None:
    dossier = _dossier(tmp_path)
    brief = _brief(dossier)
    scenes = tuple(
        replace(
            _scene(
                scene_id=f"scene-{index:02d}",
                order=index,
                environment=f"desk setup {index}",
                subject_action=f"hand performs product action {index}",
                visual_change=f"visible product state changes {index}",
            ),
            product_placement="" if index == 1 else "lamp stays on the right third",
        )
        for index in range(1, 5)
    )
    script = ScriptScenePlanV2.create(
        target_duration_seconds=16,
        brief_digest=brief.digest(),
        scenes=scenes,
        prompt_version="script-scene-plan-v3",
    )

    with pytest.raises(CreativeValidationError, match="product placement"):
        validate_script(dossier, brief, script)


def test_keyframe_prompt_uses_ranked_reference_profile_and_full_scene_lock(tmp_path: Path) -> None:
    dossier = _dossier(tmp_path)
    script = ScriptScenePlanV2.create(
        target_duration_seconds=4,
        brief_digest="sha256:" + "a" * 64,
        scenes=(_scene(),),
        prompt_version="script-scene-plan-v3",
    )

    request = compile_keyframe_requests(
        workflow_id="wf-fidelity",
        project_id="project-fidelity",
        dossier=dossier,
        script=script,
    )[0]

    assert request.reference_image_paths[0].endswith("detail.png")
    assert "circular white base" in request.prompt
    assert "right third" in request.prompt
    assert "Cool room ambience" in request.prompt
    assert "same position and orientation" in request.prompt
    assert "another lamp" not in request.prompt
    assert "wrong variant" in request.negative_prompt
    assert "duplicated product" in request.negative_prompt
    assert "do not invent, approximate, or try to reconstruct small unreadable package text" in request.prompt.lower()


def test_explicit_reference_order_keeps_primary_before_secondary(tmp_path: Path) -> None:
    dossier = _dossier(tmp_path)
    script = ScriptScenePlanV2.create(
        target_duration_seconds=4,
        brief_digest="sha256:" + "b" * 64,
        scenes=(_scene(reference_asset_ids=("hero", "detail")),),
        prompt_version="script-scene-plan-v3",
    )

    request = compile_keyframe_requests(
        workflow_id="wf-fidelity",
        project_id="project-fidelity",
        dossier=dossier,
        script=script,
    )[0]

    assert request.reference_image_paths[0].endswith("hero.png")
    assert request.reference_image_paths[1].endswith("detail.png")


def test_storyboard_package_digest_binds_script_prompt_and_negative_prompt() -> None:
    package = {
        "schema_version": "storyboard-package-v2",
        "workflow_id": "wf-bound",
        "project_id": "project-bound",
        "script": {"script_digest": "sha256:script", "scenes": [{"scene_id": "scene-01"}]},
        "scenes": [{"scene_id": "scene-01", "subject_action": "press control"}],
        "storyboard_frames": [{"scene_id": "scene-01", "visual_objective": "show control"}],
        "generated_keyframes": [
            {
                "frame_id": "scene-01-start",
                "scene_id": "scene-01",
                "sha256": "a" * 64,
                "prompt": "exact white lamp with control visible",
                "negative_prompt": "wrong model, duplicate product",
                "reference_sha256s": ["b" * 64],
            }
        ],
    }

    original = storyboard_package_digest(package)
    changed_prompt = {**package, "generated_keyframes": [dict(package["generated_keyframes"][0])]}
    changed_prompt["generated_keyframes"][0]["prompt"] = "generic lamp"
    changed_scene = {**package, "scenes": [{"scene_id": "scene-01", "subject_action": "do nothing"}]}

    assert storyboard_package_digest(changed_prompt) != original
    assert storyboard_package_digest(changed_scene) != original


def test_keyframe_recovery_rewrites_failed_scene_and_changes_operation_id(tmp_path: Path) -> None:
    dossier = _dossier(tmp_path)
    script = ScriptScenePlanV2.create(
        target_duration_seconds=4,
        brief_digest="sha256:" + "a" * 64,
        scenes=(_scene(),),
        prompt_version="script-scene-plan-v3",
    )
    original = compile_keyframe_requests(
        workflow_id="wf-fidelity",
        project_id="project-fidelity",
        dossier=dossier,
        script=script,
    )[0]

    recovered = compile_keyframe_requests(
        workflow_id="wf-fidelity",
        project_id="project-fidelity",
        dossier=dossier,
        script=script,
        recovery_plan={
            "frames": [
                {
                    "frame_id": "scene-01-start",
                    "actions": ["reselect_reference", "rewrite_prompt", "regenerate_keyframe"],
                    "feedback": "Keep the touch control and full circular base visible",
                }
            ]
        },
        recovery_attempt=2,
        previous_package={
            "generated_keyframes": [
                {
                    "frame_id": "scene-01-start",
                    "reference_image_paths": list(original.reference_image_paths),
                }
            ]
        },
    )[0]

    assert recovered.operation_id != original.operation_id
    assert "Keep the touch control and full circular base visible" in recovered.prompt
    assert "Recovery attempt 2" in recovered.prompt
    assert "create a new image" in recovered.prompt
    assert "attached approved product photo(s) as the visual identity reference" in recovered.prompt


def test_keyframe_retry_turns_square_black_bar_qc_feedback_into_targeted_end_prompt(
    tmp_path: Path,
) -> None:
    dossier = _dossier(tmp_path)
    script = ScriptScenePlanV2.create(
        target_duration_seconds=4,
        brief_digest="sha256:" + "d" * 64,
        scenes=(_scene(end_keyframe_policy="generated"),),
        prompt_version="script-scene-plan-v3",
    )

    retry = next(
        request
        for request in compile_keyframe_requests(
            workflow_id="wf-fidelity-retry",
            project_id="project-fidelity-retry",
            dossier=dossier,
            script=script,
            recovery_plan={"frames": [{
                "frame_id": "scene-01-end",
                "actions": ["rewrite_prompt", "regenerate_keyframe", "rerun_keyframe_qc"],
                "feedback": "Black bars from a square image; END background and table do not match START.",
            }]},
            recovery_attempt=1,
        )
        if request.frame_role == "end"
    )

    assert "create a new image" in retry.prompt
    assert "native full-frame 9:16 vertical portrait image" in retry.prompt
    assert "no black bars, borders, padding, frame, inset square image" in retry.prompt
    assert "Do not place a square image on a vertical canvas" in retry.prompt
    assert "attached START frame for continuity" in retry.prompt
    assert "Keep the START frame's setting, background, lighting, camera direction, and product placement" in retry.prompt
    assert "change only the visual state expressly required by the approved scene plan" in retry.prompt
    assert "QC finding is diagnostic feedback only" in retry.prompt


def test_keyframe_retry_keeps_repair_guidance_scene_bound_and_does_not_add_sample_product(
    tmp_path: Path,
) -> None:
    dossier = _dossier(tmp_path)
    script = ScriptScenePlanV2.create(
        target_duration_seconds=4,
        brief_digest="sha256:" + "e" * 64,
        scenes=(_scene(),),
        prompt_version="script-scene-plan-v3",
    )

    retry = compile_keyframe_requests(
        workflow_id="wf-fidelity-retry-bound",
        project_id="project-fidelity-retry-bound",
        dossier=dossier,
        script=script,
        recovery_plan={"frames": [{
            "frame_id": "scene-01-start",
            "actions": ["rewrite_prompt", "regenerate_keyframe", "rerun_keyframe_qc"],
            "feedback": "Product is too small in frame.",
        }]},
        recovery_attempt=2,
    )[0]

    assert "create a new image for this exact START frame" in retry.prompt
    assert "Use the attached approved product photo(s)" in retry.prompt
    assert "Play More Lemon Lime" not in retry.prompt
    assert "FORMAT CORRECTION: create a native full-frame 9:16 vertical" not in retry.prompt
    assert "Do not add product claims, physical details" in retry.prompt


def test_recovery_of_start_assigns_new_operation_to_dependent_end(tmp_path: Path) -> None:
    dossier = _dossier(tmp_path)
    script = ScriptScenePlanV2.create(
        target_duration_seconds=4,
        brief_digest="sha256:" + "c" * 64,
        scenes=(_scene(end_keyframe_policy="generated"),),
        prompt_version="script-scene-plan-v3",
    )
    original_end = next(
        request for request in compile_keyframe_requests(
            workflow_id="wf-fidelity",
            project_id="project-fidelity",
            dossier=dossier,
            script=script,
        )
        if request.frame_role == "end"
    )

    recovered_end = next(
        request for request in compile_keyframe_requests(
            workflow_id="wf-fidelity",
            project_id="project-fidelity",
            dossier=dossier,
            script=script,
            recovery_plan={"frames": [{
                "frame_id": "scene-01-start",
                "actions": ["regenerate_keyframe", "rerun_keyframe_qc"],
                "feedback": "Place the lamp on the desk.",
            }]},
            recovery_attempt=5,
        )
        if request.frame_role == "end"
    )

    assert recovered_end.operation_id != original_end.operation_id
    assert "START frame was regenerated" in recovered_end.prompt
    assert "Recovery attempt 5" in recovered_end.prompt
    assert "native full-bleed 9:16 portrait keyframe" in recovered_end.prompt
    assert "attached START frame for scene continuity" in recovered_end.prompt
    assert "same setting, background, lighting, and camera direction as START" in recovered_end.prompt
    assert "Do not invent a variant" in recovered_end.prompt


def test_keyframe_qc_failure_emits_ordered_recovery_plan(tmp_path: Path) -> None:
    reference = tmp_path / "reference.png"
    keyframe = tmp_path / "keyframe.png"
    _image(reference, "white")
    Image.new("RGB", (720, 1280), color="black").save(keyframe)

    class Evaluator:
        fake_mode = False
        evaluator_name = "vision"
        evaluator_version = "2"
        model = "vision-model"

        def evaluate_multiple_images(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "identity_consistency": 28,
                "scene_adherence": 20,
                "visual_cleanliness": 16,
                "total_score": 64,
                "verdict": "fail",
                "hard_failure": False,
                "source_branding_detected": False,
                "identity_mismatch": False,
                "black_bars_detected": False,
                "prompt_reference_consistent": False,
                "script_adherence": True,
                "important_details_preserved": False,
                "composition_usable": False,
                "animation_suitable": False,
                "recommended_action": "Use the close-up control reference and keep the full lamp visible",
                "reason": "Control and light bar drifted; composition crops the base",
            }

    report = run_keyframe_qc(
        workflow_id="wf-recovery",
        package={
            "storyboard_id": "story-recovery",
            "version": 1,
            "owner_user_id": "owner-recovery",
            "project_id": "proj-recovery",
            "package_digest": "sha256:" + "b" * 64,
            "script": {"scenes": [{"scene_id": "scene-01", "subject_action": "Show full Xiaomi lamp with control and base visible"}]},
            "generated_keyframes": [
                {
                    "frame_id": "scene-01-start",
                    "scene_id": "scene-01",
                    "image_path": str(keyframe),
                    "sha256": hashlib.sha256(keyframe.read_bytes()).hexdigest(),
                    "prompt": "Full Xiaomi lamp, control visible, base uncropped",
                    "reference_image_paths": [str(reference)],
                        "reference_sha256s": [hashlib.sha256(reference.read_bytes()).hexdigest()],
                }
            ],
        },
        product=NormalizedProduct(brand="Xiaomi", model="Mijia Desk Lamp Lite"),
        evaluator=Evaluator(),
    )

    assert report.verdict is QCVerdict.FAIL
    plan = report.findings["recovery_plan"]
    assert plan["blocked_from_video"] is True
    assert plan["frames"][0]["actions"] == [
        "reselect_reference",
        "rewrite_prompt",
        "adjust_composition",
        "regenerate_keyframe",
        "rerun_keyframe_qc",
    ]
    assert "close-up control reference" in plan["frames"][0]["feedback"]


@pytest.mark.parametrize(
    ("black_bars_detected", "expected_verdict"),
    [(False, QCVerdict.PASS), (True, QCVerdict.FAIL)],
)
def test_keyframe_policy_allows_score_60_but_black_bars_remain_hard_failure(
    tmp_path: Path, black_bars_detected: bool, expected_verdict: QCVerdict
) -> None:
    reference = tmp_path / "reference-916.png"
    keyframe = tmp_path / "keyframe-916.png"
    Image.new("RGB", (90, 160), "white").save(reference)
    Image.new("RGB", (90, 160), "gray").save(keyframe)

    class Evaluator:
        fake_mode = False
        evaluator_name = "vision"
        evaluator_version = "3"
        model = "vision-model"

        def evaluate_multiple_images(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "identity_consistency": 35,
                "scene_adherence": 20,
                "visual_cleanliness": 10,
                "total_score": 65,
                "verdict": "pass",
                "hard_failure": False,
                "black_bars_detected": black_bars_detected,
                "source_branding_detected": False,
                "identity_mismatch": False,
                "prompt_reference_consistent": True,
                "script_adherence": True,
                "important_details_preserved": True,
                "composition_usable": True,
                "animation_suitable": True,
                "recommended_action": "none",
                "reason": "Product and approved scene match; only minor aesthetic variance.",
            }

    report = run_keyframe_qc(
        workflow_id="wf-keyframe-threshold",
        package={
            "storyboard_id": "story-keyframe-threshold",
            "version": 1,
            "owner_user_id": "owner",
            "project_id": "project",
            "package_digest": "sha256:" + "c" * 64,
            "script": {"scenes": [{"scene_id": "scene-01", "subject_action": "Show the product"}]},
            "generated_keyframes": [{
                "frame_id": "scene-01-start",
                "scene_id": "scene-01",
                "image_path": str(keyframe),
                "sha256": hashlib.sha256(keyframe.read_bytes()).hexdigest(),
                "prompt": "Native full-bleed portrait product keyframe",
                "reference_image_paths": [str(reference)],
                "reference_sha256s": [hashlib.sha256(reference.read_bytes()).hexdigest()],
            }],
        },
        product=NormalizedProduct(brand="Play More", model="48g citrus candy pouch"),
        evaluator=Evaluator(),
    )

    assert report.score == 65
    assert report.verdict is expected_verdict
    assert report.findings["items"][0]["hard_failure"] is black_bars_detected


def test_keyframe_qc_evaluates_square_image_against_product_and_scene_context(tmp_path: Path) -> None:
    reference = tmp_path / "reference-916.png"
    keyframe = tmp_path / "square-frame.png"
    Image.new("RGB", (90, 160), "white").save(reference)
    Image.new("RGB", (1024, 1024), "gray").save(keyframe)
    evaluator = MagicMock()
    evaluator.fake_mode = False
    evaluator.evaluator_name = "vision"
    evaluator.evaluator_version = "3"
    evaluator.model = "vision-model"
    evaluator.evaluate_multiple_images.return_value = {
        "identity_consistency": 38,
        "scene_adherence": 20,
        "visual_cleanliness": 10,
        "total_score": 68,
        "verdict": "pass",
        "hard_failure": False,
        "black_bars_detected": False,
        "source_branding_detected": False,
        "identity_mismatch": False,
        "prompt_reference_consistent": True,
        "script_adherence": True,
        "important_details_preserved": True,
        "composition_usable": True,
        "animation_suitable": True,
        "recommended_action": "none",
        "reason": "The square image shows the correct product in the approved scene context.",
    }

    report = run_keyframe_qc(
        workflow_id="wf-square-rejected",
        package={
            "storyboard_id": "story-square-contextual",
            "version": 1,
            "owner_user_id": "owner",
            "project_id": "project",
            "package_digest": "sha256:" + "d" * 64,
            "script": {"scenes": [{"scene_id": "scene-01"}]},
            "generated_keyframes": [{
                "frame_id": "scene-01-start",
                "scene_id": "scene-01",
                "image_path": str(keyframe),
                "sha256": hashlib.sha256(keyframe.read_bytes()).hexdigest(),
                "reference_image_paths": [str(reference)],
                "reference_sha256s": [hashlib.sha256(reference.read_bytes()).hexdigest()],
            }],
        },
        product=NormalizedProduct(brand="Play More", model="48g citrus candy pouch"),
        evaluator=evaluator,
    )

    assert report.verdict is QCVerdict.PASS
    assert report.findings["items"][0]["verdict"] == "pass"
    assert report.findings["items"][0]["aspect_class"] == "square"
    assert report.findings["items"][0]["aspect_ratio_warning"] is True
    evaluator.evaluate_multiple_images.assert_called_once()
    assert "candidate is square" in evaluator.evaluate_multiple_images.call_args.kwargs["prompt"]


def test_keyframe_qc_still_rejects_landscape_output(tmp_path: Path) -> None:
    reference = tmp_path / "reference.png"
    keyframe = tmp_path / "landscape-frame.png"
    Image.new("RGB", (90, 160), "white").save(reference)
    Image.new("RGB", (160, 90), "gray").save(keyframe)
    evaluator = MagicMock()
    evaluator.fake_mode = False
    evaluator.evaluator_name = "vision"
    evaluator.evaluator_version = "3"
    evaluator.model = "vision-model"

    report = run_keyframe_qc(
        workflow_id="wf-landscape-rejected",
        package={
            "storyboard_id": "story-landscape-rejected",
            "version": 1,
            "owner_user_id": "owner",
            "project_id": "project",
            "package_digest": "sha256:" + "e" * 64,
            "script": {"scenes": [{"scene_id": "scene-01"}]},
            "generated_keyframes": [{
                "frame_id": "scene-01-start",
                "scene_id": "scene-01",
                "image_path": str(keyframe),
                "sha256": hashlib.sha256(keyframe.read_bytes()).hexdigest(),
                "reference_image_paths": [str(reference)],
                "reference_sha256s": [hashlib.sha256(reference.read_bytes()).hexdigest()],
            }],
        },
        product=NormalizedProduct(brand="Play More", model="48g citrus candy pouch"),
        evaluator=evaluator,
    )

    assert report.verdict is QCVerdict.FAIL
    assert report.findings["items"][0]["findings"]["contract_error"] == "KEYFRAME_ASPECT_RATIO_INVALID"
    evaluator.evaluate_multiple_images.assert_not_called()


def test_video_prompt_repeats_approved_scene_fidelity_lock(tmp_path: Path) -> None:
    keyframe = tmp_path / "keyframe.png"
    reference = tmp_path / "reference.png"
    _image(keyframe, "white")
    _image(reference, "gray")
    keyframe_sha = hashlib.sha256(keyframe.read_bytes()).hexdigest()
    reference_sha = hashlib.sha256(reference.read_bytes()).hexdigest()
    scene = asdict(_scene())
    receipt = {
        "report_id": "qc-keyframe-pass",
        "stage": "keyframe",
        "verdict": "pass",
        "score": 92,
        "hard_failure": False,
        "artifact_digest": "sha256:approved-package",
        "findings": {
            "items": [
                {
                    "frame_id": "scene-01-start",
                    "sha256": keyframe_sha,
                    "reference_digests": [reference_sha],
                    "score": 92,
                    "verdict": "pass",
                    "hard_failure": False,
                }
            ]
        },
    }

    request = compile_grounded_scene_request(
        owner_user_id="owner",
        workflow_id="wf-fidelity",
        project_id="project-fidelity",
        scene=scene,
        keyframe={
            "frame_id": "scene-01-start",
            "scene_id": "scene-01",
            "image_path": str(keyframe),
            "sha256": keyframe_sha,
            "reference_sha256s": [reference_sha],
        },
        keyframe_qc_receipt=receipt,
        package_digest="sha256:approved-package",
        product_identity="Xiaomi Mijia Desk Lamp Lite, white",
        provider_model="veo-test",
    )

    assert "right third" in request.prompt
    assert "Cool room ambience" in request.prompt
    assert "circular white base" in request.prompt
    assert "same position and orientation" in request.prompt
    assert "exact visual start state" in request.prompt


def test_script_validation_accepts_rich_30s_and_48s_plans(tmp_path: Path) -> None:
    dossier = _dossier(tmp_path)
    brief_30s = replace(_brief(dossier), target_duration_seconds=30)
    scenes_30s = tuple(
        replace(
            _scene(
                scene_id=f"scene-{i:02d}",
                order=i,
                duration_seconds=6,
                environment=f"creative setup {i % 2}",
                subject_action=f"interaction step {i}",
                visual_change=f"state change {i}",
            ),
            product_placement="lamp on right third",
        )
        for i in range(1, 6)
    )
    script_30s = ScriptScenePlanV2.create(
        target_duration_seconds=30,
        brief_digest=brief_30s.digest(),
        scenes=scenes_30s,
        prompt_version="script-scene-plan-v3",
    )
    validate_script(dossier, brief_30s, script_30s)

    brief_48s = replace(_brief(dossier), target_duration_seconds=48)
    scenes_48s = tuple(
        replace(
            _scene(
                scene_id=f"scene-{i:02d}",
                order=i,
                duration_seconds=6,
                environment=f"creative setup {i % 2}",
                subject_action=f"interaction step {i}",
                visual_change=f"state change {i}",
            ),
            product_placement="lamp on right third",
        )
        for i in range(1, 9)
    )
    script_48s = ScriptScenePlanV2.create(
        target_duration_seconds=48,
        brief_digest=brief_48s.digest(),
        scenes=scenes_48s,
        prompt_version="script-scene-plan-v3",
    )
    validate_script(dossier, brief_48s, script_48s)


def test_script_validation_rejects_exceeding_max_duration(tmp_path: Path) -> None:
    dossier = _dossier(tmp_path)
    with pytest.raises(ValueError, match="between 16 and 50"):
        replace(_brief(dossier), target_duration_seconds=56)

    brief_50s = replace(_brief(dossier), target_duration_seconds=50)
    scenes_56s = tuple(
        replace(
            _scene(
                scene_id=f"scene-{i:02d}",
                order=i,
                duration_seconds=8,
                environment=f"setup {i % 2}",
                subject_action=f"action {i}",
                visual_change=f"change {i}",
            ),
            product_placement="lamp on right third",
        )
        for i in range(1, 8)
    )
    script_56s = ScriptScenePlanV2.create(
        target_duration_seconds=56,
        brief_digest=brief_50s.digest(),
        scenes=scenes_56s,
        prompt_version="script-scene-plan-v3",
    )
    with pytest.raises(CreativeValidationError, match="between 16 and 50 seconds"):
        validate_script(dossier, brief_50s, script_56s)

