from dataclasses import replace, asdict
import hashlib
import pytest
from PIL import Image

from test_agent_contracts import profile
from test_creative_handler_wiring import _dossier, _brief, _script
from test_preproduction_review import FakeCriticModel, media_fixture
from video_factory.creative.agents.critic import CreativeCritic
from video_factory.creative.agents.contracts import ScenePromptBundle, ScenePrompt
from video_factory.creative.storyboard import bind_recovery_reference_revisions, compile_keyframe_requests


def test_individual_pass_does_not_override_board_continuity_fail(tmp_path):
    media = media_fixture(tmp_path)
    assert CreativeCritic(FakeCriticModel()).review(stage='keyframe', context={'scene_ids':['s1']}, media=media).verdict == 'PASS'
    assert CreativeCritic(FakeCriticModel('FAIL')).review(stage='storyboard', context={'scene_ids':['s1']}, media=media).verdict == 'FAIL'


def test_start_end_prompt_and_unaffected_scene_operation_scope(tmp_path):
    dossier = _dossier()
    assets=[]
    for i, asset in enumerate(dossier.eligible_assets):
        path=tmp_path/f'{i}.png'
        Image.new('RGB',(12,16), ('red','blue')[i]).save(path)
        assets.append(replace(asset,path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    dossier=replace(dossier,eligible_assets=tuple(assets))
    script=_script(_brief(dossier))
    prompts=tuple(ScenePrompt(s.scene_id, 'START '+s.start_state, '', s.subject_action,'avoid duplicates',s.reference_asset_ids) for s in script.scenes)
    bundle=ScenePromptBundle(script.digest(),profile().digest,'playbook',prompts)
    def compile(b):
        return compile_keyframe_requests(workflow_id='wf',project_id='p',dossier=dossier,script=script,
                                         prompt_bundle=b,backend_profile=profile())
    first=compile(bundle)
    assert first[0].prompt == prompts[0].start_prompt
    assert first[0].reference_image_paths == (assets[0].path,)
    changed=replace(bundle,prompts=(replace(prompts[0],start_prompt='simpler hand'),*prompts[1:]))
    revised=compile(changed)
    assert revised[0].operation_id != first[0].operation_id
    assert revised[1].operation_id == first[1].operation_id
    import sqlite3
    from video_factory.integrations.keyframe_provider import KeyframeRuntime,KeyframeOperationStore
    from video_factory.integrations.ports import KeyframeGenerationResult
    class FakeImageProvider:
        provider_kind='google_flow'
        def __init__(self): self.calls=[]
        def submit(self,request):
            self.calls.append(request)
            path=tmp_path/(request.frame_id+'.png')
            Image.new('RGB',(90,160),'green').save(path)
            return KeyframeGenerationResult(request.operation_id,'completed',request.operation_id,str(path))
    provider=FakeImageProvider()
    conn=sqlite3.connect(':memory:')
    runtime=KeyframeRuntime(store=KeyframeOperationStore(conn),provider=provider)
    assert runtime.run(first[0]).status=='completed'
    assert provider.calls[0].prompt==prompts[0].start_prompt
    assert provider.calls[0].reference_image_paths==(assets[0].path,)
    assert runtime.run(first[0]).status=='completed'
    assert len(provider.calls)==1
    copied=tmp_path/'copied-reference.png'
    copied.write_bytes(__import__('pathlib').Path(assets[0].path).read_bytes())
    assert runtime.run(replace(first[0],reference_image_paths=(str(copied),))).status=='completed'
    assert len(provider.calls)==1
    conn.close()


def test_start_and_end_are_reviewed_together_by_the_durable_storyboard_qc(tmp_path,monkeypatch):
    import sqlite3
    from unittest.mock import MagicMock
    from video_factory.creative.agents.service import AgenticCreativeGenerator
    from video_factory.integrations.keyframe_provider import KeyframeRuntime,KeyframeOperationStore
    from video_factory.integrations.ports import KeyframeGenerationResult
    from video_factory.handlers import ProductVideoJobHandlers,JobContext,JobFailed,JobSucceeded
    from video_factory.workflows.budgets import init_default_engine,BudgetPolicy
    from test_creative_handler_wiring import _persisted_storyboard_result
    monkeypatch.chdir(tmp_path)
    init_default_engine(BudgetPolicy(max_total=1000,per_channel={'video_factory':500}))
    dossier=_dossier()
    assets=[]
    for index,asset in enumerate(dossier.eligible_assets):
        path=tmp_path/f'ref-{index}.png'
        Image.new('RGB',(90,160),('red','blue')[index]).save(path)
        assets.append(replace(asset,path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    dossier=replace(dossier,eligible_assets=tuple(assets),dossier_digest='')
    dossier=replace(dossier,dossier_digest=dossier.digest())
    brief=_brief(dossier)
    script=_script(brief)
    first=replace(script.scenes[0],end_keyframe_policy='required',end_keyframe_omission_reason='')
    script=replace(script,scenes=(first,*script.scenes[1:]),script_digest='')
    script=replace(script,script_digest=script.digest())
    p=replace(profile(),supports_end_frame=True)
    prompts=tuple(ScenePrompt(s.scene_id,s.start_state,s.end_state if s.end_keyframe_policy=='required' else '',
                              s.subject_action,'',s.reference_asset_ids) for s in script.scenes)
    bundle=ScenePromptBundle(script.digest(),p.digest,'playbook',prompts)
    from video_factory.creative.agents.gates import preproduction_context
    from video_factory.creative.agents.strategist import dossier_media
    class FullCoverageFake:
        def generate_media_json(self,**kwargs):
            import json
            from video_factory.creative.ports import StructuredModelResult
            context=json.loads(kwargs['user_prompt'])
            return StructuredModelResult({'verdict':'PASS','findings':[],'coverage':{'method':'actual_pixels',
                'media_sha256':[r.sha256 for r in kwargs['media']],'scene_ids':context['scene_ids']}},'{}','fake','fake')
    model=FullCoverageFake()
    review=CreativeCritic(model).review(stage='preproduction',context=preproduction_context(dossier,script,bundle,p),media=dossier_media(dossier))
    scene_ref={**script.to_dict(),'project_id':'project-1','research_dossier':dossier.to_dict(),
        'creative_mode':'four_agent','scene_prompts':bundle.to_dict(),'backend_profile':p.to_dict(),
        'preproduction_review':review.to_dict()}
    class SquareProvider:
        provider_kind='google_flow'
        def __init__(self): self.calls=[]
        def submit(self,req):
            self.calls.append(req.frame_id)
            path=tmp_path/'invalid-square.png'
            Image.new('RGB',(100,100),'green').save(path)
            return KeyframeGenerationResult(req.operation_id,'completed',req.operation_id,str(path))
    provider=SquareProvider()
    conn=sqlite3.connect(':memory:')
    runtime=KeyframeRuntime(store=KeyframeOperationStore(conn),provider=provider)
    generator=AgenticCreativeGenerator(model,profile=p,category='gadget_demo',visible_details=('visible_controls',))
    generator.prepare_targeted_repair=lambda **kwargs:None
    video=MagicMock()
    video.create_storyboard.return_value=_persisted_storyboard_result(script)
    handlers=ProductVideoJobHandlers(video_client=video,creative_generator=generator,keyframe_runtime_factory=lambda:runtime)
    handlers._validate_state_and_version=lambda *args,**kwargs:None
    result=handlers.storyboard_job(JobContext(dossier.workflow_id,'job'),scene_ref)
    assert isinstance(result,JobSucceeded)
    assert provider.calls==['scene-1-start','scene-1-end','scene-2-start','scene-3-start']
    assert not result.artifact_ref.get('frame_reviews')
    assert 'storyboard_review' not in result.artifact_ref
    conn.close()


def test_recovery_reference_revision_is_bound_to_director_refs_script_and_sha(tmp_path):
    dossier = _dossier()
    assets = []
    for index, asset in enumerate(dossier.eligible_assets):
        path = tmp_path / f'approved-{index}.png'
        Image.new('RGB', (90, 160), ('red', 'blue')[index]).save(path)
        assets.append(replace(asset, path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    dossier = replace(dossier, eligible_assets=tuple(assets))
    script = _script(_brief(dossier))
    prompts = tuple(ScenePrompt(s.scene_id, s.start_state, '', s.subject_action, '', s.reference_asset_ids)
                    for s in script.scenes)
    bundle = ScenePromptBundle(script.digest(), profile().digest, 'playbook', prompts)
    recovery = {'frames': [{'frame_id': 'scene-1-start', 'actions': ['reselect_reference']}]}
    with pytest.raises(ValueError, match='stale or missing reference revision'):
        compile_keyframe_requests(workflow_id='wf', project_id='project', dossier=dossier, script=script,
                                  prompt_bundle=bundle, backend_profile=profile(), recovery_plan=recovery)
    bound = bind_recovery_reference_revisions(recovery, script=script, prompt_bundle=bundle, dossier=dossier)
    first = compile_keyframe_requests(workflow_id='wf', project_id='project', dossier=dossier, script=script,
                                      prompt_bundle=bundle, backend_profile=profile(), recovery_plan=bound)
    resumed = compile_keyframe_requests(workflow_id='wf', project_id='project', dossier=dossier, script=script,
                                        prompt_bundle=bundle, backend_profile=profile(), recovery_plan=bound)
    assert first[0].operation_id == resumed[0].operation_id
    assert first[0].reference_image_paths == (assets[0].path,)
    stale = {'frames': [{**bound['frames'][0], 'reference_revision': {
        **bound['frames'][0]['reference_revision'], 'script_digest': 'sha256:stale'
    }}]}
    with pytest.raises(ValueError, match='stale or missing reference revision'):
        compile_keyframe_requests(workflow_id='wf', project_id='project', dossier=dossier, script=script,
                                  prompt_bundle=bundle, backend_profile=profile(), recovery_plan=stale)
    wrong_sha = {'frames': [{**bound['frames'][0], 'reference_revision': {
        **bound['frames'][0]['reference_revision'], 'asset_sha256': {'approved-0': 'stale'}
    }}]}
    with pytest.raises(ValueError, match='reference revision SHA mismatch'):
        compile_keyframe_requests(workflow_id='wf', project_id='project', dossier=dossier, script=script,
                                  prompt_bundle=bundle, backend_profile=profile(), recovery_plan=wrong_sha)
    # Generic recovery keeps its established behavior outside the four-agent path.
    legacy = compile_keyframe_requests(workflow_id='wf', project_id='project', dossier=dossier, script=script,
                                      recovery_plan=recovery)
    assert legacy[0].reference_image_paths
