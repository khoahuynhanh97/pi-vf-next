from dataclasses import replace

import pytest

from video_factory.creative.agents.contracts import (
    BackendProfile, CreativeReview, ReviewFinding, ScenePrompt, ScenePromptBundle,
)
from video_factory.creative.contracts import (
    canonical_digest, SceneBible, ScriptScenePlanV2, ScriptSceneV2,
)


def profile():
    return BackendProfile('google_flow', 'google_flow', 'test-v1', (4,6,8),
                          ('reference',), 2, False, True, 'silent')


def test_canonical_digest_ignores_mapping_order():
    assert canonical_digest({'a': 1, 'b': 2}) == canonical_digest({'b': 2, 'a': 1})


def test_bundle_binds_prompt_profile_and_reference_order():
    prompt = ScenePrompt('s1', 'hand ready', '', 'picks exactly two pieces', '', ('a', 'b'))
    bundle = ScenePromptBundle('script', profile().digest, 'playbook', (prompt,))
    assert replace(bundle, prompts=(replace(prompt, video_prompt='puts two pieces down'),)).digest != bundle.digest
    assert replace(bundle, backend_profile_digest='changed').digest != bundle.digest
    assert replace(bundle, prompts=(replace(prompt, reference_asset_ids=('b', 'a')),)).digest != bundle.digest
    assert ScenePromptBundle.from_dict(bundle.to_dict()) == bundle


def test_review_rejects_invalid_verdict_and_unbound_pass():
    with pytest.raises(ValueError):
        CreativeReview('keyframe', 'APPROVED', 'input', (), {}, {})
    with pytest.raises(ValueError):
        CreativeReview('keyframe', 'PASS', '', (), {}, {})
    with pytest.raises(ValueError):
        CreativeReview('keyframe', 'PASS', 'input', (), {}, {})


def test_review_rejects_unknown_scene_and_pass_with_blocking_finding():
    finding = ReviewFinding('f1', 'other', 'identity', True, 'red', 'blue', (), ('frame1',), .9)
    review = CreativeReview('keyframe', 'FAIL', 'input', (finding,), {}, {})
    with pytest.raises(ValueError):
        review.validate_scenes(('s1',))
    with pytest.raises(ValueError):
        replace(review, verdict='PASS')


def test_content_reference_identity_survives_copy_but_not_changed_json_or_bytes():
    from test_creative_generator import _dossier
    dossier=replace(_dossier(),reference_identity_mode='content',dossier_digest='')
    moved=replace(dossier,eligible_assets=tuple(replace(a,path='copied/'+a.asset_id+'.png') for a in dossier.eligible_assets))
    assert moved.digest()==dossier.digest()
    assert replace(dossier,use_cases=('different intent',)).digest()!=dossier.digest()
    assert replace(dossier,eligible_assets=(replace(dossier.eligible_assets[0],sha256='f'*64),dossier.eligible_assets[1])).digest()!=dossier.digest()


def test_creative_review_from_dict_tolerates_missing_finding_id():
    raw = {
        'stage': 'clip',
        'verdict': 'FAIL',
        'input_digest': 'test_input',
        'findings': [{
            'scene_id': 'scene_2_detail',
            'code': 'framing_defect',
            'blocking': True,
            'expected': 'visible rim',
            'observed': 'cropped rim',
            'reference_ids': ['ref1'],
            'evidence_locations': ['t=1.0'],
            'confidence': 0.95,
        }],
        'coverage': {'method': 'actual_pixels'},
        'evaluator_receipt': {'prompt_version': 'test'},
    }
    rev = CreativeReview.from_dict(raw)
    assert len(rev.findings) == 1
    assert rev.findings[0].finding_id.startswith('finding-scene_2_detail-framing_defect')
    assert rev.findings[0].blocking is True


def test_scene_bible_validation():
    bible = SceneBible(
        environment="clean desk setup",
        lighting="soft diffused daylight",
        pov_hand="right hand with neutral grip",
        camera_baseline="45 degree high angle steady",
    )
    assert bible.environment == "clean desk setup"
    assert bible.to_dict()["environment"] == "clean desk setup"
    assert SceneBible.from_dict(bible.to_dict()) == bible

    for field_name in ("environment", "lighting", "pov_hand", "camera_baseline"):
        invalid_kwargs = {
            "environment": "clean desk",
            "lighting": "soft light",
            "pov_hand": "neutral hand",
            "camera_baseline": "steady shot",
        }
        invalid_kwargs[field_name] = ""
        with pytest.raises(ValueError, match=field_name):
            SceneBible(**invalid_kwargs)

    # Malformed types and unknown keys in from_dict
    valid_bible_dict = {
        "environment": "clean desk",
        "lighting": "soft light",
        "pov_hand": "neutral hand",
        "camera_baseline": "steady shot",
    }
    with pytest.raises(ValueError, match="exactly the keys"):
        SceneBible.from_dict({**valid_bible_dict, "extra_key": "unexpected"})
    with pytest.raises(ValueError):
        SceneBible.from_dict({**valid_bible_dict, "environment": 123})
    with pytest.raises(ValueError):
        SceneBible.from_dict({**valid_bible_dict, "lighting": {"type": "sunlight"}})
    with pytest.raises(ValueError):
        SceneBible.from_dict("not a dict")


def test_scene_bible_script_digest_roundtrip_and_legacy_compatibility():
    from test_creative_handler_wiring import _brief, _dossier

    brief = _brief(_dossier())
    scene = ScriptSceneV2(
        scene_id="s1",
        order=1,
        story_role="hook",
        duration_seconds=6,
        narration="",
        overlay_text="",
        visual_objective="Present product",
        environment="studio setting",
        subject_action="Hand places product on desk",
        product_interaction="Hold product gently",
        start_state="Product resting on table",
        end_state="Product held in hand",
        visual_change="Product is lifted",
        camera_framing="medium close-up",
        camera_movement="static",
        transition_out="cut",
        product_placement="center",
        lighting="soft daylight",
        continuity_from_previous="start",
        continuity_to_next="end",
        product_identity_cues=("logo",),
        required_visible_details=("finish",),
        reference_requirements=("photo",),
        reference_asset_ids=("asset_1",),
        claim_refs=("claim_1",),
        end_keyframe_policy="omitted",
        end_keyframe_omission_reason="continuous motion",
    )

    # Legacy plan without scene_bible
    legacy_plan = ScriptScenePlanV2.create(
        target_duration_seconds=6,
        brief_digest=brief.digest(),
        scenes=(scene,),
    )
    assert legacy_plan.scene_bible is None
    legacy_dict = legacy_plan.to_dict()
    assert "scene_bible" not in legacy_dict
    roundtrip_legacy = ScriptScenePlanV2.from_dict(legacy_dict)
    assert roundtrip_legacy.digest() == legacy_plan.digest()
    assert roundtrip_legacy.scene_bible is None

    # New plan with scene_bible
    bible = SceneBible(
        environment="studio setting",
        lighting="soft daylight",
        pov_hand="right hand with neutral grip",
        camera_baseline="45 degree steady",
    )
    new_plan = ScriptScenePlanV2.create(
        target_duration_seconds=6,
        brief_digest=brief.digest(),
        scenes=(scene,),
        scene_bible=bible,
    )
    assert new_plan.scene_bible == bible
    new_dict = new_plan.to_dict()
    assert "scene_bible" in new_dict
    assert new_dict["scene_bible"] == bible.to_dict()
    assert new_plan.digest() != legacy_plan.digest()
    roundtrip_new = ScriptScenePlanV2.from_dict(new_dict)
    assert roundtrip_new.digest() == new_plan.digest()
    assert roundtrip_new.scene_bible == bible

    # Non-object scene_bible in from_dict rejected
    invalid_plan_dict = dict(legacy_dict)
    invalid_plan_dict["scene_bible"] = "not an object"
    with pytest.raises(ValueError, match="scene_bible"):
        ScriptScenePlanV2.from_dict(invalid_plan_dict)

    # Environment/lighting mismatch at plan canonical boundary rejected
    mismatched_scene = replace(scene, environment="completely different environment")
    with pytest.raises(ValueError, match="does not match scene_bible"):
        ScriptScenePlanV2.create(
            target_duration_seconds=6,
            brief_digest=brief.digest(),
            scenes=(mismatched_scene,),
            scene_bible=bible,
        )
