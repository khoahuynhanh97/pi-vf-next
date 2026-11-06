from dataclasses import replace
import pytest

from test_agent_contracts import profile
from test_creative_handler_wiring import _dossier, _brief, _script
from video_factory.creative.agents.contracts import ScenePrompt, ScenePromptBundle
from video_factory.creative.agents.director import validate_bundle
from video_factory.creative.agents.director import VisualDirector


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
