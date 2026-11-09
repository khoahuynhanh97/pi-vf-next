from dataclasses import replace
import json
import pytest

from test_agent_contracts import profile
from test_creative_handler_wiring import _dossier, _brief, _script
from video_factory.creative.agents.contracts import ScenePrompt, ScenePromptBundle
from video_factory.creative.agents.director import validate_bundle, VisualDirector, bind_scene_bible
from video_factory.creative.contracts import SceneBible, ScriptScenePlanV2
from video_factory.creative.ports import StructuredModelResult


def test_director_action_and_exact_reference_order_are_required():
    script = _script(_brief(_dossier()))
    prompts = tuple(ScenePrompt(s.scene_id, s.start_state, '', s.subject_action, '', s.reference_asset_ids) for s in script.scenes)
    bundle = ScenePromptBundle(script.digest(), profile().digest, 'playbook', prompts)
    validate_bundle(bundle, script, profile())
    changed = replace(bundle, prompts=(replace(prompts[0], video_prompt='generic zoom'), *prompts[1:]))
    with pytest.raises(ValueError, match='action missing'):
        validate_bundle(changed, script, profile())
    with pytest.raises(ValueError, match='stale'):
        validate_bundle(replace(bundle, script_digest='stale'), script, profile())


def test_scene_prompts_bind_verified_brand_variant_and_visible_details():
    import json
    from video_factory.creative.ports import StructuredModelResult

    dossier=_dossier()
    script=_script(_brief(dossier))
    p=profile()
    class Model:
        def generate_media_json(self, **kwargs):
            payload=json.loads(kwargs['user_prompt'])
            prompts=[]
            for scene in script.scenes:
                prompts.append({'scene_id':scene.scene_id,'start_prompt':'Match source image',
                    'end_prompt':'','video_prompt':scene.subject_action,'negative_prompt':'',
                    'reference_asset_ids':list(scene.reference_asset_ids)})
            data={'script_digest':script.digest(),'backend_profile_digest':p.digest,
                'playbook_digest':'playbook','prompts':prompts}
            return StructuredModelResult(data,json.dumps(data),'fake','fake-director')
    director=VisualDirector(Model(),recipe={'digest':'playbook'},profile=p,source_verifier=None)
    bundle=director.create_scene_prompts(dossier,script)
    for prompt in bundle.prompts:
        assert 'Baseus' in prompt.start_prompt and 'Bowie WM02' in prompt.start_prompt
        assert 'Baseus' in prompt.video_prompt and 'Bowie WM02' in prompt.video_prompt
        scene=next(s for s in script.scenes if s.scene_id==prompt.scene_id)
        assert scene.subject_action in prompt.video_prompt


def test_director_scene_bible_mismatch_and_recovery():
    dossier = _dossier()
    brief = _brief(dossier)
    p = profile()

    class MismatchedModel:
        def __init__(self, mode="mismatch_env"):
            self.mode = mode

        def generate_media_json(self, **kwargs):
            scenes = [
                {
                    "scene_id": "s1",
                    "order": 1,
                    "story_role": "hook",
                    "duration_seconds": 6,
                    "narration": "",
                    "overlay_text": "",
                    "visual_objective": "action",
                    "environment": "different desk" if self.mode == "mismatch_env" else "studio A",
                    "subject_action": "A hand places the item",
                    "product_interaction": "handled gently",
                    "start_state": "item on table",
                    "end_state": "item held",
                    "visual_change": "lifted",
                    "camera_framing": "medium closeup",
                    "camera_movement": "static",
                    "transition_out": "cut",
                    "claim_refs": ["claim-1"],
                    "reference_asset_ids": ["asset-1"],
                    "product_identity_cues": ["cues"],
                    "product_placement": "center",
                    "lighting": "dim light" if self.mode == "mismatch_light" else "studio light",
                    "continuity_from_previous": "start",
                    "continuity_to_next": "end",
                    "required_visible_details": ["detail"],
                    "reference_requirements": ["ref"],
                    "end_keyframe_policy": "omitted",
                    "end_keyframe_omission_reason": "continuous",
                }
            ]
            data = {
                "schema_version": "script-scene-plan-v2",
                "brief_digest": brief.digest(),
                "target_duration_seconds": 24,
                "scenes": scenes * 4,
            }
            for i, s in enumerate(data["scenes"], 1):
                s["scene_id"] = f"s{i}"
                s["order"] = i
            if self.mode != "missing_bible":
                data["scene_bible"] = {
                    "environment": "studio A",
                    "lighting": "studio light",
                    "pov_hand": "right hand",
                    "camera_baseline": "eye-level 45",
                }
            return StructuredModelResult(data, json.dumps(data), "fake", "fake-director")

    # Mode 1: missing bible when scene-bible-v1 is pinned
    director_missing = VisualDirector(
        MismatchedModel(mode="missing_bible"),
        recipe={"digest": "playbook"},
        profile=p,
        source_verifier=None,
        director_config_version="scene-bible-v1",
    )
    with pytest.raises(Exception, match="DIRECTOR_SCENE_BIBLE_MISMATCH"):
        director_missing.create_script(dossier, brief)

    # Mode 2: environment mismatch
    director_env = VisualDirector(
        MismatchedModel(mode="mismatch_env"),
        recipe={"digest": "playbook"},
        profile=p,
        source_verifier=None,
        director_config_version="scene-bible-v1",
    )
    with pytest.raises(Exception, match="DIRECTOR_SCENE_BIBLE_MISMATCH"):
        director_env.create_script(dossier, brief)

    # Mode 3: lighting mismatch
    director_light = VisualDirector(
        MismatchedModel(mode="mismatch_light"),
        recipe={"digest": "playbook"},
        profile=p,
        source_verifier=None,
        director_config_version="scene-bible-v1",
    )
    with pytest.raises(Exception, match="DIRECTOR_SCENE_BIBLE_MISMATCH"):
        director_light.create_script(dossier, brief)

    # Mode 4: malformed raw values in scene_bible (e.g. non-string or unknown keys)
    class MalformedBibleModel:
        def __init__(self, raw_bible):
            self.raw_bible = raw_bible

        def generate_media_json(self, **kwargs):
            scenes = [
                {
                    "scene_id": f"s{i}",
                    "order": i,
                    "story_role": "hook" if i == 1 else "demonstration",
                    "duration_seconds": 6,
                    "narration": "",
                    "overlay_text": "",
                    "visual_objective": "action",
                    "environment": "studio A",
                    "subject_action": "A hand places the item",
                    "product_interaction": "handled gently",
                    "start_state": "item on table",
                    "end_state": "item held",
                    "visual_change": "lifted",
                    "camera_framing": "medium closeup",
                    "camera_movement": "static",
                    "transition_out": "cut",
                    "claim_refs": ["claim-1"],
                    "reference_asset_ids": ["asset-1"],
                    "product_identity_cues": ["cues"],
                    "product_placement": "center",
                    "lighting": "studio light",
                    "continuity_from_previous": "start",
                    "continuity_to_next": "end",
                    "required_visible_details": ["detail"],
                    "reference_requirements": ["ref"],
                    "end_keyframe_policy": "omitted",
                    "end_keyframe_omission_reason": "continuous",
                }
                for i in range(1, 5)
            ]
            data = {
                "schema_version": "script-scene-plan-v2",
                "brief_digest": brief.digest(),
                "target_duration_seconds": 24,
                "scenes": scenes,
                "scene_bible": self.raw_bible,
            }
            return StructuredModelResult(data, json.dumps(data), "fake", "fake-director")

    director_bad_type = VisualDirector(
        MalformedBibleModel({"environment": 123, "lighting": "light", "pov_hand": "hand", "camera_baseline": "cam"}),
        recipe={"digest": "playbook"},
        profile=p,
        source_verifier=None,
        director_config_version="scene-bible-v1",
    )
    with pytest.raises(Exception, match="DIRECTOR_SCENE_BIBLE_MISMATCH"):
        director_bad_type.create_script(dossier, brief)

    director_extra_key = VisualDirector(
        MalformedBibleModel({
            "environment": "studio A", "lighting": "studio light", "pov_hand": "hand",
            "camera_baseline": "cam", "extra": "forbidden",
        }),
        recipe={"digest": "playbook"},
        profile=p,
        source_verifier=None,
        director_config_version="scene-bible-v1",
    )
    with pytest.raises(Exception, match="DIRECTOR_SCENE_BIBLE_MISMATCH"):
        director_extra_key.create_script(dossier, brief)

    # Legacy unversioned director run does not require scene_bible and does not fail when absent
    director_legacy = VisualDirector(
        MismatchedModel(mode="missing_bible"),
        recipe={"digest": "playbook"},
        profile=p,
        source_verifier=None,
        director_config_version=None,
    )
    # In legacy director, varying environments are used because legacy gate requires distinct environments
    class LegacyValidModel:
        def generate_media_json(self, **kwargs):
            # Verify scene_bible was not in the prompt schema for legacy
            schema = kwargs.get("json_schema")
            assert "scene_bible" not in schema["properties"]
            scenes = [
                {
                    "scene_id": f"s{i}",
                    "order": i,
                    "story_role": "hook" if i == 1 else "demonstration",
                    "duration_seconds": 6,
                    "narration": "",
                    "overlay_text": "",
                    "visual_objective": "action",
                    "environment": f"studio {i}",
                    "subject_action": f"A hand places the item {i}",
                    "product_interaction": "handled gently",
                    "start_state": "item on table",
                    "end_state": "item held",
                    "visual_change": "lifted",
                    "camera_framing": "medium closeup",
                    "camera_movement": "static",
                    "transition_out": "cut",
                    "claim_refs": ["claim-1"],
                    "reference_asset_ids": ["asset-1"],
                    "product_identity_cues": ["cues"],
                    "product_placement": "center",
                    "lighting": "studio light",
                    "continuity_from_previous": "start",
                    "continuity_to_next": "end",
                    "required_visible_details": ["detail"],
                    "reference_requirements": ["ref"],
                    "end_keyframe_policy": "omitted",
                    "end_keyframe_omission_reason": "continuous",
                }
                for i in range(1, 5)
            ]
            data = {
                "schema_version": "script-scene-plan-v2",
                "brief_digest": brief.digest(),
                "target_duration_seconds": 24,
                "scenes": scenes,
            }
            return StructuredModelResult(data, json.dumps(data), "fake", "fake-director")

    director_legacy_valid = VisualDirector(
        LegacyValidModel(),
        recipe={"digest": "playbook"},
        profile=p,
        source_verifier=None,
        director_config_version=None,
    )
    legacy_script = director_legacy_valid.create_script(dossier, brief)
    assert legacy_script.scene_bible is None

    # VisualDirector with scene-bible-v1 pin passes real validate_script with identical bible environment/light
    # across scenes, at least two camera framings, and distinct meaningful actions.
    class BiblePinnedValidModel:
        def generate_media_json(self, **kwargs):
            scenes = [
                {
                    "scene_id": f"s{i}",
                    "order": i,
                    "story_role": "hook" if i == 1 else "demonstration",
                    "duration_seconds": 6,
                    "narration": "",
                    "overlay_text": "",
                    "visual_objective": "action",
                    "environment": "shared studio environment",
                    "subject_action": f"Action detail {i} demonstrating product",
                    "product_interaction": "handled gently",
                    "start_state": "item on table",
                    "end_state": "item held",
                    "visual_change": f"change {i}",
                    "camera_framing": "close-up" if i % 2 == 1 else "wide angle",
                    "camera_movement": "static",
                    "transition_out": "cut",
                    "claim_refs": ["claim-1"],
                    "reference_asset_ids": ["asset-1"],
                    "product_identity_cues": ["cues"],
                    "product_placement": "center",
                    "lighting": "unified soft lighting",
                    "continuity_from_previous": "start",
                    "continuity_to_next": "end",
                    "required_visible_details": ["detail"],
                    "reference_requirements": ["ref"],
                    "end_keyframe_policy": "omitted",
                    "end_keyframe_omission_reason": "continuous",
                }
                for i in range(1, 5)
            ]
            data = {
                "schema_version": "script-scene-plan-v2",
                "brief_digest": brief.digest(),
                "target_duration_seconds": 24,
                "scenes": scenes,
                "scene_bible": {
                    "environment": "shared studio environment",
                    "lighting": "unified soft lighting",
                    "pov_hand": "neutral right hand",
                    "camera_baseline": "eye-level 45",
                },
            }
            return StructuredModelResult(data, json.dumps(data), "fake", "fake-director")

    director_bible_valid = VisualDirector(
        BiblePinnedValidModel(),
        recipe={"digest": "playbook"},
        profile=p,
        source_verifier=None,
        director_config_version="scene-bible-v1",
    )
    bible_script = director_bible_valid.create_script(dossier, brief)
    assert bible_script.scene_bible is not None
    assert bible_script.scene_bible.environment == "shared studio environment"



def test_scene_bible_prompt_binding_and_validation():
    dossier = _dossier()
    brief = _brief(dossier)
    p = profile()
    bible = SceneBible(
        environment="studio setting alpha",
        lighting="soft diffused rim lighting",
        pov_hand="neutral right hand hold",
        camera_baseline="45-degree angle eye level",
    )

    base_script = _script(brief)
    # Rebuild script with 4 scenes and scene_bible
    scenes = []
    for s in base_script.scenes:
        scenes.append(replace(s, environment=bible.environment, lighting=bible.lighting, duration_seconds=6))
    # Make total 4 scenes = 24s
    while len(scenes) < 4:
        idx = len(scenes) + 1
        scenes.append(replace(scenes[0], scene_id=f"scene_{idx}", order=idx))
    script = ScriptScenePlanV2.create(
        target_duration_seconds=24,
        brief_digest=brief.digest(),
        scenes=tuple(scenes),
        scene_bible=bible,
    )

    class ValidModel:
        def generate_media_json(self, **kwargs):
            prompts = []
            for scene in script.scenes:
                prompts.append({
                    "scene_id": scene.scene_id,
                    "start_prompt": "Portrait composition of product",
                    "end_prompt": "",
                    "video_prompt": scene.subject_action,
                    "negative_prompt": "",
                    "reference_asset_ids": list(scene.reference_asset_ids),
                })
            data = {
                "script_digest": script.digest(),
                "backend_profile_digest": p.digest,
                "playbook_digest": "playbook",
                "prompts": prompts,
            }
            return StructuredModelResult(data, json.dumps(data), "fake", "fake-director")

    director = VisualDirector(ValidModel(), recipe={"digest": "playbook"}, profile=p, source_verifier=None)
    bundle = director.create_scene_prompts(dossier, script)

    # Every prompt must contain the four raw strings exactly
    for prompt in bundle.prompts:
        assert bible.environment in prompt.start_prompt
        assert bible.lighting in prompt.start_prompt
        assert bible.pov_hand in prompt.start_prompt
        assert bible.camera_baseline in prompt.start_prompt

        assert bible.environment in prompt.video_prompt
        assert bible.lighting in prompt.video_prompt
        assert bible.pov_hand in prompt.video_prompt
        assert bible.camera_baseline in prompt.video_prompt

    # Idempotence: binding again does not duplicate the suffix
    rebount = bind_scene_bible(bundle, bible)
    for original, reb in zip(bundle.prompts, rebount.prompts):
        assert original.start_prompt == reb.start_prompt
        assert original.video_prompt == reb.video_prompt

    # validate_bundle rejects if any raw string is missing from a prompt
    tampered_prompt = replace(bundle.prompts[0], video_prompt=bundle.prompts[0].video_prompt.replace(bible.lighting, "corrupted"))
    tampered_bundle = replace(bundle, prompts=(tampered_prompt, *bundle.prompts[1:]))
    with pytest.raises(ValueError, match="Scene bible"):
        validate_bundle(tampered_bundle, script, p, "playbook")


def test_scene_bible_end_prompt_and_repair_binding():
    from video_factory.creative.agents.contracts import BackendProfile, CreativeReview, ReviewFinding, RepairProposal
    from video_factory.creative.agents.repair import revise_scene_prompt

    dossier = _dossier()
    brief = _brief(dossier)
    p = BackendProfile('google_flow', 'google_flow', 'test-v1', (6, 8), ('reference',), 2, True, True, 'silent')
    bible = SceneBible(
        environment="clean bright kitchen",
        lighting="diffused morning sun",
        pov_hand="two hands holding steady",
        camera_baseline="straight-on eye level",
    )
    base_script = _script(brief)
    scenes = []
    for idx, s in enumerate(base_script.scenes, 1):
        scenes.append(
            replace(
                s,
                environment=bible.environment,
                lighting=bible.lighting,
                duration_seconds=6,
                end_keyframe_policy="generated",
                end_keyframe_omission_reason="",
            )
        )
    while len(scenes) < 4:
        idx = len(scenes) + 1
        scenes.append(replace(scenes[0], scene_id=f"scene_{idx}", order=idx))

    script = ScriptScenePlanV2.create(
        target_duration_seconds=24,
        brief_digest=brief.digest(),
        scenes=tuple(scenes),
        scene_bible=bible,
    )

    class EndPromptModel:
        def generate_media_json(self, **kwargs):
            if kwargs.get("schema_name") == "prompt-repair-v1":
                target_scene = script.scenes[0]
                orig = bundle.for_scene(target_scene.scene_id)
                data = {
                    "prompts": [{
                        "scene_id": target_scene.scene_id,
                        "start_prompt": orig.start_prompt,
                        "end_prompt": "Repaired END result",
                        "video_prompt": f"Repaired movement: {target_scene.subject_action}",
                        "negative_prompt": "",
                        "reference_asset_ids": list(target_scene.reference_asset_ids),
                    }]
                }
                return StructuredModelResult(data, json.dumps(data), "fake", "fake-director")
            prompts = []
            for scene in script.scenes:
                prompts.append({
                    "scene_id": scene.scene_id,
                    "start_prompt": "START composition",
                    "end_prompt": "END result state",
                    "video_prompt": scene.subject_action,
                    "negative_prompt": "",
                    "reference_asset_ids": list(scene.reference_asset_ids),
                })
            data = {
                "script_digest": script.digest(),
                "backend_profile_digest": p.digest,
                "playbook_digest": "playbook",
                "prompts": prompts,
            }
            return StructuredModelResult(data, json.dumps(data), "fake", "fake-director")

    director = VisualDirector(EndPromptModel(), recipe={"digest": "playbook"}, profile=p, source_verifier=None)
    bundle = director.create_scene_prompts(dossier, script)

    # Applicable end prompts must contain all 4 strings
    for prompt in bundle.prompts:
        assert bible.environment in prompt.end_prompt
        assert bible.lighting in prompt.end_prompt
        assert bible.pov_hand in prompt.end_prompt
        assert bible.camera_baseline in prompt.end_prompt

    # Test prompt repair reuse: revise_scene_prompt binds scene_bible to repaired prompt
    finding = ReviewFinding("f1", script.scenes[0].scene_id, "visual_flaw", True, "clear", "flawed", (), ("t=0.5",), 0.9)
    review = CreativeReview("keyframe", "FAIL", bundle.digest, (finding,), {}, {"artifact_frame_id": f"{script.scenes[0].scene_id}-end"})
    invariants = {"protected": "invariant"}
    proposal = RepairProposal(
        bundle.digest,
        ("f1",),
        "keyframe",
        (script.scenes[0].scene_id,),
        "adjust lighting",
        {"prompt_delta": "improved"},
        invariants,
        ("keyframe",),
    )

    revised_bundle = revise_scene_prompt(
        director=director,
        dossier=dossier,
        script=script,
        bundle=bundle,
        proposal=proposal,
        review=review,
        protected_invariants=invariants,
    )
    repaired_prompt = revised_bundle.for_scene(script.scenes[0].scene_id)
    assert bible.environment in repaired_prompt.start_prompt
    assert bible.lighting in repaired_prompt.video_prompt
    assert bible.pov_hand in repaired_prompt.end_prompt
    assert bible.camera_baseline in repaired_prompt.end_prompt


def test_coordinator_and_handler_scene_bible_wiring():
    from video_factory.automation.coordinator import ProductVideoCoordinator
    from video_factory.handlers import JobContext, ProductVideoJobHandlers
    from unittest.mock import MagicMock

    run_config = {
        "max_scenes": 4,
        "image_call_limit": 10,
        "video_call_limit": 5,
        "category": "gadget_demo",
        "visible_details": ["visible_controls"],
        "profile": profile().to_dict(),
    }
    policy = ProductVideoCoordinator._catalog_run_policy(run_config, preparation_id="prep-1", product_key="prod-1")
    assert policy.get("director_config_version") == "scene-bible-v1"

    # Test handler _bind_creative_run picks up director_config_version
    repo = MagicMock()
    repo.get_workflow.return_value = ({"creative_policy": policy},)
    handlers = ProductVideoJobHandlers(workflow_repository=repo, provider_mode="fake")
    ctx = JobContext(workflow_id="wf-1", job_id="j-1", idempotency_key="ik-1", owner_user_id="user")
    handlers._bind_creative_run(ctx)
    assert handlers.creative_generator.director_config_version == "scene-bible-v1"
    assert handlers.creative_generator.director().director_config_version == "scene-bible-v1"

    # Test legacy stored workflow without director_config_version
    legacy_policy = dict(policy)
    del legacy_policy["director_config_version"]
    repo.get_workflow.return_value = ({"creative_policy": legacy_policy},)
    handlers_legacy = ProductVideoJobHandlers(workflow_repository=repo, provider_mode="fake")
    handlers_legacy._bind_creative_run(ctx)
    assert handlers_legacy.creative_generator.director_config_version is None
    assert handlers_legacy.creative_generator.director().director_config_version is None
