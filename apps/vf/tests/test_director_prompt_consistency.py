"""Offline regressions at the Director and repair prompt boundaries."""
import json
from dataclasses import replace

import pytest

from test_agent_contracts import profile
from test_agent_repair_policy import fixture as repair_fixture
from test_creative_handler_wiring import _brief, _dossier, _script
from video_factory.creative.agents.contracts import ScenePrompt, ScenePromptBundle
from video_factory.creative.agents.director import VisualDirector, bind_identity_lock
from video_factory.creative.agents.repair import RepairPlanner
from video_factory.creative.generator import StructuredGenerationError
from video_factory.creative.ports import StructuredModelResult
from video_factory.creative.validation import CreativeValidationError, validate_script


def camera_script():
    dossier = _dossier()
    brief = _brief(dossier)
    script = _script(brief)
    first = replace(script.scenes[0],
        subject_action='The camera moves slowly closer to the stationary item.',
        visual_change='The same item fills more of the frame with its entire silhouette visible.',
        product_interaction='The item stays stationary.', interaction_risk='low')
    return dossier, brief, replace(script, scenes=(first, *script.scenes[1:]))


def test_four_agent_validation_allows_low_risk_camera_action():
    dossier, brief, script = camera_script()
    validate_script(dossier, brief, script, allow_camera_motion=True)


def test_camera_motion_can_keep_the_product_centered():
    dossier, brief, script = camera_script()
    script = replace(script, scenes=(replace(script.scenes[0],
        visual_change='Product remains centered as the camera moves closer.'), *script.scenes[1:]))
    validate_script(dossier, brief, script, allow_camera_motion=True)


def test_legacy_validation_still_rejects_camera_action_even_with_director_version():
    dossier, brief, script = camera_script()
    script = replace(script, prompt_version='four-agent-director-v1')
    with pytest.raises(CreativeValidationError, match='meaningful action'):
        validate_script(dossier, brief, script)


@pytest.mark.parametrize('risk,action', [
    ('medium', 'The camera moves slowly closer.'),
    ('high', 'The camera moves slowly closer.'),
    ('low', 'Demonstrate product'),
    ('low', 'Rotate product image'),
    ('low', 'Product remains centered'),
])
def test_camera_opt_in_preserves_risk_and_generic_action_rejection(risk, action):
    dossier, brief, script = camera_script()
    script = replace(script, scenes=(replace(script.scenes[0],
        subject_action=action, interaction_risk=risk), *script.scenes[1:]))
    with pytest.raises(CreativeValidationError, match='meaningful action'):
        validate_script(dossier, brief, script, allow_camera_motion=True)


@pytest.mark.parametrize('missing', [None, 'product_interaction', 'start_state', 'end_state',
    'environment', 'subject_action'])
def test_director_uses_camera_contract_at_actual_parse_boundary(missing):
    dossier, brief, script = camera_script()
    scenes = tuple(replace(scene, narration='', overlay_text='',
        product_placement='Centered with margin', lighting='Soft daylight',
        continuity_from_previous='Independent cut to this setting',
        continuity_to_next='Settle before the next cut', product_identity_cues=('same shape',),
        required_visible_details=('whole silhouette',), reference_requirements=('approved source',),
        claim_refs=('claim-1',)) for scene in script.scenes)
    script = replace(script, scenes=scenes)
    calls = []

    class Model:
        def generate_json(self, **kwargs):
            calls.append(kwargs)
            data = script.to_dict()
            if missing:
                del data['scenes'][0][missing]
            return StructuredModelResult(data, json.dumps(data), 'fake', 'offline', retry_allowed=False)

        generate_media_json = generate_json

    director = VisualDirector(Model(), recipe={'digest':'playbook'}, profile=profile(), source_verifier=None)
    if missing:
        with pytest.raises(StructuredGenerationError, match=f'DIRECTOR_REQUIRED_FIELD_MISSING.*{missing}'):
            director.create_script(dossier, brief)
        return
    result = director.create_script(dossier, brief)
    assert result.scenes[0].subject_action == 'The camera moves slowly closer to the stationary item.'
    assert result.scenes[0].product_interaction == 'The item stays stationary.'
    assert len(calls) == 1
    system, user = calls[0]['system_prompt'], calls[0]['user_prompt']
    assert 'Do not design scenes whose only action is zooming' not in system
    assert 'Each scene must contain an observable subject action, product interaction' not in system
    assert 'Reject the draft if camera movement is the only motion' not in user
    assert 'one purposeful subtle camera movement' in system
    assert 'REQUIRED IN EVERY SCENE' in user and 'non-empty string' in user
    for field in ('product_interaction', 'start_state', 'end_state', 'environment', 'subject_action'):
        assert f'- {field}:' in user


def test_identity_refresh_replaces_only_old_json_and_retains_repair_after_it():
    dossier = _dossier()
    source = dossier.eligible_assets[0]
    serving = replace(source, asset_id='serving', visible_details=('white bowl',), visual_observations=())
    packshot = replace(source, asset_id='packshot', visible_details=('silver lid',), visual_observations=())
    dossier = replace(dossier, eligible_assets=(serving, packshot))
    prompt = ScenePrompt('s1', 'START: bowl with margin.', 'END: settled bowl.',
        'Motion: reveal the whole bowl.', '', ('packshot',))
    stale = bind_identity_lock(ScenePromptBundle('script', 'backend', 'playbook', (prompt,)), dossier)
    stale_prompt = stale.for_scene('s1')
    suffix = '\nRepair: keep the rim visible. {Do not trim this prose}. The hand settles.'
    rebound = replace(stale, prompts=(replace(stale_prompt, reference_asset_ids=('serving',),
        start_prompt=stale_prompt.start_prompt + suffix,
        end_prompt=stale_prompt.end_prompt + suffix,
        video_prompt=stale_prompt.video_prompt + suffix),))

    refreshed = bind_identity_lock(rebound, dossier)
    for value, opening in ((refreshed.prompts[0].start_prompt, prompt.start_prompt),
                           (refreshed.prompts[0].end_prompt, prompt.end_prompt),
                           (refreshed.prompts[0].video_prompt, prompt.video_prompt)):
        assert value.startswith(opening)
        assert value.endswith(suffix)
        assert 'white bowl' in value
        assert 'silver lid' not in value
        assert value.count('Reference identity lock; match attached source pixels.') == 1
    assert bind_identity_lock(refreshed, dossier) == refreshed


def test_identity_refresh_removes_observations_no_longer_present_in_selected_asset():
    dossier = _dossier()
    source = replace(dossier.eligible_assets[0], visible_details=('outdated lid',))
    dossier = replace(dossier, eligible_assets=(source,))
    prompt = ScenePrompt('s1', 'Bowl', '', 'Slow reveal', '', (source.asset_id,))
    bound = bind_identity_lock(ScenePromptBundle('script', 'backend', 'playbook', (prompt,)), dossier)
    dossier = replace(dossier, eligible_assets=(replace(source, visible_details=(), visual_observations=()),))
    refreshed = bind_identity_lock(bound, dossier)
    assert 'outdated lid' not in refreshed.prompts[0].video_prompt
    assert refreshed.prompts[0].end_prompt == ''


@pytest.mark.parametrize('malformed', [
    {}, {'scope':'prompt'}, {'changes':[]}, {'finding_ids':'f1'},
    {'scene_ids':[{}]}, {'recheck_stages':[{}]}, {'diagnosis':[]}, {'extra':True},
])
def test_malformed_repair_reply_raises_clean_value_error(malformed):
    review, proposal, invariants = repair_fixture()
    valid = json.loads(json.dumps(proposal.to_dict()))
    valid.pop('digest')
    valid.pop('evaluator_receipt')
    data = malformed if malformed == {} or malformed == {'scope':'prompt'} else {**valid, **malformed}

    class Model:
        def generate_json(self, **kwargs):
            return StructuredModelResult(data, json.dumps(data), 'fake', 'offline')

    with pytest.raises(ValueError, match='invalid repair proposal JSON'):
        RepairPlanner(Model()).propose(review=review, context={
            'current_digest':review.input_digest, 'protected_invariants':invariants})
