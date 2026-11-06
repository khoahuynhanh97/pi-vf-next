"""Explicit fake external ports for the durable creative workflow integration test."""
import hashlib
import json
from pathlib import Path
from fractions import Fraction
from dataclasses import asdict

import av
import numpy as np
from PIL import Image

from video_factory.creative.ports import StructuredModelResult
from video_factory.qc.models import QCReport,QCStage,QCVerdict
from video_factory.qc.repository import QCReportRepository
from video_factory.qc.fingerprint import IMAGE_POLICY_VERSION,KEYFRAME_POLICY_VERSION,VIDEO_POLICY_VERSION
from video_factory.qc.normalization import resource_pack_fingerprint


def embedded(prompt,schema):
    decoder=json.JSONDecoder()
    for index,char in enumerate(prompt):
        if char=='{':
            try: value,_=decoder.raw_decode(prompt[index:])
            except ValueError: continue
            if isinstance(value,dict) and value.get('schema_version')==schema:
                return value
    raise AssertionError('missing actual role input '+schema)


class FakeRoles:
    def __init__(self,fail_stage=None):
        self.calls=[]
        self.fail_stage=fail_stage
        self.failed_once=False
        self.on_failure=lambda:None
        self.failure_generation_counts=None

    def generate_json(self,**kwargs):
        assert kwargs['schema_name']=='repair-proposal-v1'
        self.calls.append(kwargs['schema_name'])
        payload=json.loads(kwargs['user_prompt'])
        review,context=payload['review'],payload['context']
        assembly=self.fail_stage=='assembly'
        data={'base_digest':context['current_digest'],'finding_ids':[review['findings'][0]['finding_id']],
            'scope':'assembly' if assembly else ('scene' if review['stage']=='preproduction' else 'prompt'),'scene_ids':['s2'],
            'diagnosis':'Restore documented scene order using accepted clips' if assembly else 'Make the same documented handling simpler and more visible',
            'changes':{'rebuild':True} if assembly else {'action_guidance':'relaxed grip with clear hand movement'},
            'protected_invariants':context['protected_invariants'],
            'recheck_stages':['preproduction','storyboard','clip','final']}
        return StructuredModelResult(data,json.dumps(data),'fake','fake-repair-planner')

    def generate_media_json(self,**kwargs):
        self.calls.append(kwargs['schema_name'])
        for ref in kwargs['media']: ref.content()
        name=kwargs['schema_name']
        if name=='creative-brief-v2':
            dossier=embedded(kwargs['user_prompt'],'research-dossier-v2')
            claim=dossier['verified_claims'][0]
            data={'schema_version':'creative-brief-v2','audience':{'segment':'A person leaving a desk',
                'situation':'Clear a desk before leaving','desired_outcome':'Keep the item in hand'},
                'consumer_insight':'A visible item can be picked up and carried', 'problem':'The item is still on the desk',
                'creative_angle':'Follow one physical pickup','hook':'A hand approaches the item',
                'promise':'Show the documented shape during a simple pickup','reason_to_believe':[
                    {'text':claim['text'],'evidence_refs':[claim['claim_id']]}],
                'tone':'clear and neutral','story_archetype':'day_in_the_life','cta':'See product details.',
                'restrictions':['No unsupported features'],'target_duration_seconds':32,
                'prompt_version':'creative-direction-v2','dossier_digest':dossier['dossier_digest']}
        elif name=='script-scene-plan-v2':
            dossier=embedded(kwargs['user_prompt'],'research-dossier-v2')
            brief=embedded(kwargs['user_prompt'],'creative-brief-v2')
            actions=['A hand approaches the item','The hand lifts the item','The person carries the item','The hand holds the item near the door']
            if 'Finding-specific revision' in kwargs['user_prompt']:
                actions[1]='The hand lifts the item with a relaxed grip'
            scenes=[]
            for index,action in enumerate(actions):
                scenes.append({'scene_id':f's{index+1}','order':index+1,'story_role':['hook','demo','proof','cta'][index],
                    'duration_seconds':8,'narration':'','overlay_text':'','visual_objective':action,
                    'environment':'daylit desk' if index<2 else 'daylit doorway','subject_action':action,
                    'product_interaction':'The same hand handles the documented item',
                    'start_state':'The item is visible before this action','end_state':'The item stays visible after this action',
                    'visual_change':action,'camera_framing':'medium closeup','camera_movement':'locked camera',
                    'transition_out':'cut','claim_refs':[dossier['verified_claims'][0]['claim_id']],
                    'reference_asset_ids':[a['asset_id'] for a in dossier['eligible_assets']],
                    'product_identity_cues':['same documented shape'],'product_placement':'center in portrait',
                    'lighting':'soft daylight','continuity_from_previous':'same item and hand',
                    'continuity_to_next':'same item and hand','required_visible_details':['same shape'],
                    'reference_requirements':['approved source photos'],'end_keyframe_policy':'omitted',
                    'end_keyframe_omission_reason':'profile uses one START frame'})
            data={'schema_version':'script-scene-plan-v2','brief_digest':brief['brief_digest'],
                  'target_duration_seconds':32,'scenes':scenes}
        elif name=='scene-prompt-bundle-v1':
            context=json.loads(kwargs['user_prompt'])
            data={k:context[k] for k in ('script_digest','backend_profile_digest','playbook_digest')}
            data['prompts']=[{'scene_id':s['scene_id'],'start_prompt':'Portrait composition: '+s['start_state'],
                'end_prompt':'','video_prompt':s['subject_action'],'negative_prompt':'',
                'reference_asset_ids':s['reference_asset_ids']} for s in context['script']['scenes']]
        elif name=='creative-review-v1':
            context=json.loads(kwargs['user_prompt'])
            data={'verdict':'PASS','findings':[],'coverage':{'method':'actual_pixels',
                'media_sha256':[r.sha256 for r in kwargs['media']],'scene_ids':context['scene_ids'],
                'timestamps':context.get('required_timestamps',[]),'limitations':'Fake assessment on synthetic pixels'}}
            is_preproduction='script' in context and 'bundle' in context
            is_second_clip='video_sha256' in context and context['scene_ids']==['s2']
            is_final='video_sha256' in context and len(context['scene_ids'])>1
            if not self.failed_once and ((self.fail_stage=='preproduction' and is_preproduction) or
                                         (self.fail_stage=='clip' and is_second_clip) or
                                         (self.fail_stage in {'final','assembly'} and is_final)):
                self.failed_once=True
                self.failure_generation_counts=self.on_failure()
                data.update(verdict='FAIL',findings=[{'finding_id':'s2-action','scene_id':'s2',
                    'code':'action_needs_simplification','blocking':True,'expected':'simple visible pickup',
                    'observed':'handling intent is too complex or insufficiently visible',
                    'reference_ids':[],'evidence_locations':['s2:action' if is_preproduction else 's2:t=4'],
                    'confidence':.95}])
                if self.fail_stage=='assembly':
                    data['findings'][0].update(code='assembly_order',expected='joins follow scene order',
                        observed='the composition reverses the accepted clips',evidence_locations=['join:8'])
        elif name=='prompt-repair-v1':
            context=json.loads(kwargs['user_prompt'])
            prompt=next(p for p in context['prompts']['prompts'] if p['scene_id']=='s2')
            data={'prompts':[{**prompt,'video_prompt':prompt['video_prompt']+' with clear deliberate hand movement'}]}
        else:
            raise AssertionError(name)
        return StructuredModelResult(data,json.dumps(data),'fake','fake-four-roles')


def save_qc(conn,workflow_id,project_id,stage,artifact):
    if stage==QCStage.IMAGE:
        digest=resource_pack_fingerprint(artifact)
        items=[{**a,'uri':a['local_path'],'eligible':True,'score':95,'verdict':'pass','hard_failure':False,
                'visible_details':['visible_controls','documented rectangular shape']} for a in artifact['assets']]
        findings={'automatic':True,'items':items,'eligible_count':len(items),'eligible_reference_manifest':items}
    elif stage==QCStage.KEYFRAME:
        digest=artifact['package_digest']
        items=[{**f,'score':95,'verdict':'pass','hard_failure':False,'reference_digests':f['reference_sha256s']}
               for f in artifact['generated_keyframes']]
        findings={'automatic':True,'items':items}
    else:
        digest=artifact['sha256']
        findings={'automatic':True,'technical_specs':{'technical_pass':True},
            'sample_coverage':{'coverage_valid':True,'expected_samples':20,'samples_per_scene':5,'total_samples':20},
            'visual_scenes':[{'verdict':'pass','score':95,'hard_failure':False,'samples_extracted':5} for _ in range(4)]}
    report=QCReport(report_id='fake-'+stage.value+'-'+digest[-12:],workflow_id=workflow_id,stage=stage,
        artifact_id=artifact.get('artifact_id') or artifact.get('resource_pack_id') or artifact.get('storyboard_id'),
        artifact_version=artifact.get('version',1),artifact_digest=digest,reference_digests=[],
        evaluator_name='offline_fake',evaluator_version='1',rubric_version='1',created_at='2026-10-23T00:00:00Z',
        score=95,verdict=QCVerdict.PASS,findings=findings,provider_mode='fake',owner_user_id='local-owner',project_id=project_id,
        policy_version={QCStage.IMAGE:IMAGE_POLICY_VERSION,QCStage.KEYFRAME:KEYFRAME_POLICY_VERSION,QCStage.VIDEO:VIDEO_POLICY_VERSION}[stage])
    QCReportRepository(conn).save_report(report)
    return report


class FakeVideoAPI:
    def __init__(self,root,conn,model):
        self.root,self.conn,self.model=Path(root),conn,model
        self.images=0
        self.videos=0
        self.recompositions=0
        self.rejected_final_sha=None
    def create_creative_brief(self,**kwargs):
        return {'project_id':kwargs['project_id'],'brief_id':'fake-brief','version':1}
    def create_scene_plan(self,brief,**kwargs):
        return {'project_id':kwargs['project_id'],'scene_plan_id':'fake-scenes','version':1}
    def create_storyboard(self,scene_ref,**kwargs):
        from test_creative_handler_wiring import _persisted_storyboard_result
        from video_factory.creative.contracts import ScriptScenePlanV2
        return _persisted_storyboard_result(ScriptScenePlanV2.from_dict(scene_ref),
            project_id=kwargs['project_id'],owner_user_id=kwargs['owner_user_id'])
    def render_video(self,story,**kwargs):
        from video_factory.creative.agents.gates import require_storyboard_qc_pass
        from video_factory.creative.agents.critic import CreativeCritic
        from video_factory.creative.agents.video_review import review_video
        from video_factory.integrations.composer import concatenate_videos
        from video_factory.integrations.scene_renderer import compile_grounded_scene_request,SceneRenderRuntime,SceneRenderOperationStore
        from video_factory.integrations.ports import VideoGenerationResult
        require_storyboard_qc_pass(story)
        kwargs['on_job_accepted']('render','fake-batch')
        api=self
        class Provider:
            provider_kind='google_flow'
            def get_status(self,operation_id):
                raise AssertionError('synchronous fake provider must not be polled')
            def submit(self,request):
                api.videos+=1
                output=api.root/(request.request_id+'.mp4')
                with av.open(str(output),'w') as out:
                    stream=out.add_stream('libx264',rate=30)
                    stream.width,stream.height,stream.pix_fmt=90,160,'yuv420p'
                    pixels=np.full((160,90,3),100+api.videos,dtype=np.uint8)
                    for i in range(240):
                        frame=av.VideoFrame.from_ndarray(pixels,format='rgb24')
                        frame.pts=i
                        frame.time_base=Fraction(1,30)
                        for packet in stream.encode(frame):out.mux(packet)
                    for packet in stream.encode():out.mux(packet)
                return VideoGenerationResult(request.request_id,'completed',request.request_id,str(output))
        runtime=SceneRenderRuntime(store=SceneRenderOperationStore(self.conn),provider=Provider(),
            preferred_provider_kind='google_flow',allow_pre_submit_fallback=False)
        critic=CreativeCritic(self.model,receipt_root=self.root/'reviews')
        clips=[]
        for scene in story['scenes']:
            start=next(f for f in story['generated_keyframes'] if f['scene_id']==scene['scene_id'])
            prompt=next(p for p in story['scene_prompts']['prompts'] if p['scene_id']==scene['scene_id'])
            request=compile_grounded_scene_request(owner_user_id=kwargs['owner_user_id'],workflow_id=kwargs['workflow_id'],
                project_id=kwargs['project_id'],scene=scene,keyframe=start,keyframe_qc_receipt=story['keyframe_qc_receipt'],
                package_digest=story['package_digest'],product_identity='Fixture Synthetic1',provider_model='fake-flow',scene_prompt=prompt)
            result=runtime.run(request)
            assert result.status=='completed',result
            assessment=review_video(path=result.video_path,package=story,critic=critic,stage='clip',scene_id=scene['scene_id'])
            if assessment['clip_review']['verdict']!='PASS':
                from video_factory.job_contracts import TerminalJobError
                error=TerminalJobError('Fake recorded clip content failure',code='CLIP_CONTENT_'+assessment['clip_review']['verdict'])
                error.artifact_ref={'assessment':assessment,'package':story}
                raise error
            clips.append(result.video_path)
        final=self.root/'synthetic-final.mp4'
        if kwargs.get('render_params',{}).get('force_recompose'):
            assert self.model.fail_stage=='assembly'
            self.recompositions+=1
        # Recorded assembly fault: first composition reverses accepted clip order.
        assembly_clips=list(reversed(clips)) if self.model.fail_stage=='assembly' and not self.model.failed_once else clips
        concatenate_videos(assembly_clips,final,target_width=1080,target_height=1920,target_fps=30,silent=True)
        assessment=review_video(path=final,package=story,critic=critic,stage='final')
        if assessment['final_review']['verdict']!='PASS':
            self.rejected_final_sha=assessment['sha256']
            from video_factory.job_contracts import TerminalJobError
            error=TerminalJobError('Fake recorded final content failure',code='FINAL_CONTENT_'+assessment['final_review']['verdict'])
            error.artifact_ref={'assessment':assessment,'package':story}
            raise error
        artifact={**assessment,'file_path':str(final),'video_url':'file://'+str(final),'render_id':'fake-render',
            'artifact_id':'fake-final','version':1,'artifact_version':1,'owner_user_id':kwargs['owner_user_id'],
            'project_id':kwargs['project_id'],'workflow_id':kwargs['workflow_id']}
        save_qc(self.conn,kwargs['workflow_id'],kwargs['project_id'],QCStage.VIDEO,artifact)
        return artifact
    def export_video(self,render,**kwargs):
        from video_factory.creative.agents.video_review import require_final_pass
        from video_factory.integrations.export_package import build_export_package
        from video_factory.integrations.composer import VideoExpectation
        require_final_pass(render)
        package=build_export_package(source_video=render['file_path'],exports_root=self.root/'exports',
            project_id=kwargs['project_id'],version='v1',product_identity='Fixture Synthetic1',
            provenance={'content_assessment':render['content_assessment'],'human_review':'not_performed'},
            expected=VideoExpectation(width=1080,height=1920,fps=30,duration_seconds=32))
        return {'file_path':str(package.video_path),'sha256':package.video_sha256,'export_id':'fake-export','version':1}
