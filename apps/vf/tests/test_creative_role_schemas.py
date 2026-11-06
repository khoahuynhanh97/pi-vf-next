"""The four roles must describe their output to schema-constrained transports."""
import hashlib
import json
from dataclasses import replace

import pytest
from PIL import Image

from test_agent_contracts import profile
from test_agent_repair_policy import fixture as repair_fixture
from test_creative_handler_wiring import _brief, _dossier, _script
from video_factory.creative.agents.contracts import ScenePrompt, ScenePromptBundle
from video_factory.creative.agents.critic import CreativeCritic
from video_factory.creative.agents.director import VisualDirector, bind_identity_lock
from video_factory.creative.agents.repair import RepairPlanner, revise_scene_prompt
from video_factory.creative.agents.strategist import CreativeStrategist
from video_factory.creative.generator import StructuredGenerationError
from video_factory.creative.ports import MediaReference, StructuredModelResult


class CaptureModel:
    def __init__(self):
        self.calls = []

    def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        return StructuredModelResult({}, '{}', 'fake', 'schema-constrained', retry_allowed=False)

    generate_media_json = generate_json


@pytest.mark.parametrize('role,required', [
    ('strategist', {'audience', 'dossier_digest', 'reason_to_believe'}),
    ('script', {'schema_version', 'brief_digest', 'scenes'}),
    ('bundle', {'script_digest', 'backend_profile_digest', 'playbook_digest', 'prompts'}),
    ('critic', {'verdict', 'findings', 'coverage'}),
    ('planner', {'base_digest', 'finding_ids', 'scope', 'scene_ids', 'diagnosis', 'changes',
                 'protected_invariants', 'recheck_stages'}),
    ('repair', {'prompts'}),
])
def test_role_request_exposes_required_output_in_schema_and_prompt(tmp_path, role, required):
    model = CaptureModel()
    dossier = _dossier()
    brief = _brief(dossier)
    script = _script(brief)
    recipe = {'digest': 'playbook'}
    director = VisualDirector(model, recipe=recipe, profile=profile(), source_verifier=None)
    review, proposal, invariants = repair_fixture()
    # Exercise actual call sites; an empty provider reply intentionally fails parsing.
    try:
        if role == 'strategist':
            CreativeStrategist(model, recipe=recipe, profile=profile(), source_verifier=None).create_direction(dossier)
        elif role == 'script':
            director.create_script(dossier, brief)
        elif role == 'bundle':
            director.create_scene_prompts(dossier, script)
        elif role == 'critic':
            path = tmp_path / 'actual.png'
            Image.new('RGB', (90, 160), 'blue').save(path)
            media = (MediaReference(str(path), hashlib.sha256(path.read_bytes()).hexdigest(), 'source'),)
            CreativeCritic(model).review(stage='preproduction', context={'scene_ids': ['s1']}, media=media)
        elif role == 'planner':
            RepairPlanner(model).propose(review=review, context={
                'current_digest': review.input_digest, 'protected_invariants': invariants})
        else:
            prompts = tuple(ScenePrompt(s.scene_id, s.start_state, '', s.subject_action, '', s.reference_asset_ids)
                            for s in script.scenes)
            bundle = ScenePromptBundle(script.digest(), profile().digest, recipe['digest'], prompts)
            proposal = replace(proposal, scene_ids=(script.scenes[0].scene_id,))
            review = replace(review, findings=(replace(review.findings[0], scene_id=script.scenes[0].scene_id),))
            revise_scene_prompt(director=director, dossier=dossier, script=script, bundle=bundle,
                                proposal=proposal, review=review, protected_invariants=invariants)
    except (ValueError, KeyError, StructuredGenerationError):
        pass
    assert len(model.calls) == 1
    call = model.calls[0]
    schema = call['json_schema']
    assert required <= set(schema.get('required', []))
    assert set(schema['required']) <= set(schema['properties'])
    # Some configured gateways ignore response_format; prose must carry the same contract.
    assert 'Output JSON schema:\n' in call['system_prompt']
    exposed = json.loads(call['system_prompt'].split('Output JSON schema:\n', 1)[1])
    assert exposed == schema
    if role in {'bundle', 'repair'}:
        item = schema['properties']['prompts']['items']
        assert {'scene_id', 'start_prompt', 'end_prompt', 'video_prompt', 'negative_prompt',
                'reference_asset_ids'} <= set(item['required'])
    elif role == 'script':
        assert 'end_keyframe_omission_reason' in schema['properties']['scenes']['items']['required']
    elif role == 'critic':
        coverage = schema['properties']['coverage']
        assert {'method', 'media_sha256', 'scene_ids', 'timestamps', 'limitations'} <= set(coverage['required'])
        assert schema['properties']['verdict']['enum'] == ['PASS', 'FAIL', 'UNKNOWN']


def test_director_invalid_received_bundle_keeps_provider_receipt():
    from video_factory.creative.generator import StructuredGenerationError

    class ReceivedInvalidModel:
        def generate_json(self, **kwargs):
            return StructuredModelResult({}, 'not-json', 'vertex', 'configured-model',
                parse_error='invalid JSON', route_receipt={'usage_id':'usage-42','cost_usd':0.01})
        generate_media_json = generate_json

    dossier = _dossier()
    script = _script(_brief(dossier))
    director = VisualDirector(ReceivedInvalidModel(), recipe={'digest':'playbook'},
                              profile=profile(), source_verifier=None)

    with pytest.raises(StructuredGenerationError) as error:
        director.create_scene_prompts(dossier, script)

    assert error.value.request_sent is True
    assert error.value.route_receipt['usage_id'] == 'usage-42'
    assert error.value.route_receipt['cost_usd'] == 0.01


def test_director_non_object_received_bundle_is_not_retried_as_transport_failure():
    from video_factory.creative.generator import StructuredGenerationError

    class ReceivedArrayModel:
        def generate_json(self, **kwargs):
            return StructuredModelResult([], '[]', 'vertex', 'configured-model',
                route_receipt={'usage_id':'usage-43'})
        generate_media_json = generate_json

    dossier = _dossier()
    director = VisualDirector(ReceivedArrayModel(), recipe={'digest':'playbook'},
                              profile=profile(), source_verifier=None)

    with pytest.raises(StructuredGenerationError) as error:
        director.create_scene_prompts(dossier, _script(_brief(dossier)))

    assert error.value.request_sent is True
    assert error.value.route_receipt['usage_id'] == 'usage-43'


def test_critic_schema_labels_and_actual_pixels_reach_router_transport(tmp_path):
    import base64
    from video_factory.creative.model_adapter import NineRouterHermesStructuredCreativeModel

    path = tmp_path / 'actual-source.png'
    Image.new('RGB', (90, 160), 'green').save(path)
    pixels = path.read_bytes()
    sha = hashlib.sha256(pixels).hexdigest()

    class FakeTransport:
        def complete(self, **kwargs):
            self.payload = kwargs
            data = {'verdict': 'PASS', 'findings': [], 'coverage': {
                'method': 'actual_pixels', 'media_sha256': [sha], 'scene_ids': ['s1'],
                'timestamps': [], 'limitations': 'Synthetic reference only; no generated media reviewed.'}}
            return {'choices': [{'message': {'content': json.dumps(data)}}]}, {'upstream_model': 'fake-vision'}

    transport = FakeTransport()
    model = NineRouterHermesStructuredCreativeModel(transport)
    review = CreativeCritic(model).review(stage='preproduction', context={'scene_ids': ['s1']},
                                        media=(MediaReference(str(path), sha, 'approved_source:a'),))
    assert review.verdict == 'PASS'
    assert review.evaluator_receipt['model'] == 'fake-vision'
    request = transport.payload
    schema = request['response_format']['json_schema']['schema']
    assert set(schema['required']) == {'verdict', 'findings', 'coverage'}
    assert json.loads(request['messages'][0]['content'].split('Output JSON schema:\n')[1]) == schema
    parts = request['messages'][1]['content']
    assert parts[1]['text'] == f'Media role: approved_source:a; SHA256: {sha}'
    assert base64.b64decode(parts[2]['image_url']['url'].split(',')[1]) == pixels


def test_director_requests_action_progression_without_extra_scenes():
    model = CaptureModel()
    dossier = _dossier()
    director = VisualDirector(model, recipe={"digest":"playbook"}, profile=profile(), source_verifier=None)
    try:
        director.create_script(dossier, _brief(dossier))
    except (ValueError, KeyError, StructuredGenerationError):
        pass
    prompt = model.calls[0]["user_prompt"]
    assert "ACTION PROGRESSION v2" in prompt
    assert "0-2s setup, 2-6s action and 6-8s result" in prompt
    assert "3–5 scenes (default 4)" in prompt
    assert "No extra scene, collage or grid" in prompt
    assert "exact visible inventory" in prompt
    assert "Reserve visible framing margin" in prompt


def test_identity_lock_only_includes_the_scenes_selected_image_observations():
    dossier = _dossier()
    source = dossier.eligible_assets[0]
    serving = replace(source, asset_id="serving", visible_details=("white bowl",), visual_observations=())
    packshot = replace(source, asset_id="packshot", visible_details=("silver lid",), visual_observations=())
    dossier = replace(dossier, eligible_assets=(serving, packshot))
    prompts = (
        ScenePrompt("s1", "Bowl", "", "Slow push", "", ("serving",)),
        ScenePrompt("s2", "Jar", "", "Slow push", "", ("packshot",)),
    )
    bundle = ScenePromptBundle("script", "backend", "playbook", prompts)

    bound = bind_identity_lock(bundle, dossier)
    assert "white bowl" in bound.for_scene("s1").video_prompt
    assert "silver lid" not in bound.for_scene("s1").video_prompt
    assert "silver lid" in bound.for_scene("s2").start_prompt
    assert "white bowl" not in bound.for_scene("s2").start_prompt
