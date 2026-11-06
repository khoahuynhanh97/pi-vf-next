from dataclasses import replace
import sqlite3
import pytest

from video_factory.creative.agents.contracts import CreativeReview, ReviewFinding, RepairProposal
from video_factory.creative.agents.repair import validate_repair
from video_factory.qc.retry_policy import claim_qc_attempt, QCQuotaExhaustedError


def fixture():
    review = CreativeReview('keyframe','FAIL','digest',(
        ReviewFinding('f1','s1','extra_fingers',True,'five fingers','six fingers',('source',),('s1-start:hand',),.9),),{}, {})
    invariants = {'product_truth':'sha', 'logical_scene_ids':['s1','s2'], 'audio_policy':'silent'}
    proposal = RepairProposal('digest',('f1',),'keyframe',('s1',),'simplify grip',
        {'start_prompt':'Use a relaxed two-finger grip'}, invariants, ('preproduction','keyframe','storyboard'))
    return review, proposal, invariants


def test_targeted_repair_rejects_stale_unknown_scope_and_invariant_changes():
    review, proposal, invariants = fixture()
    validate_repair(proposal, current_digest='digest', review=review, protected_invariants=invariants)
    for bad in (replace(proposal, base_digest='stale'), replace(proposal, finding_ids=('f2',)),
                replace(proposal, scene_ids=('s2',)), replace(proposal, protected_invariants={'product_truth':'changed'})):
        with pytest.raises(ValueError):
            validate_repair(bad,current_digest='digest', review=review, protected_invariants=invariants)
    with pytest.raises(ValueError):
        validate_repair(proposal,current_digest='digest',review=replace(review,verdict='UNKNOWN'))


def test_durable_logical_frame_quota_survives_changed_prompts_and_restart(tmp_path):
    path = tmp_path/'test.db'
    conn = sqlite3.connect(path)
    for i in range(3):
        result = claim_qc_attempt(conn,workflow_id='run',retry_type='creative_keyframe', item_id='s1-start',
                                 operation_id=f'op-{i}',input_hash=f'prompt-{i}',max_recoveries=2)
        assert result['attempt_number'] == i+1
    conn.close()
    conn = sqlite3.connect(path)
    assert claim_qc_attempt(conn,workflow_id='run',retry_type='creative_keyframe',item_id='s1-start',
                           operation_id='op-2',input_hash='prompt-2',max_recoveries=2)['reused']
    with pytest.raises(QCQuotaExhaustedError):
        claim_qc_attempt(conn,workflow_id='run',retry_type='creative_keyframe',item_id='s1-start',
                         operation_id='op-3',max_recoveries=2)
    conn.close()


def test_video_send_cap_is_atomic_and_keeps_uncertain_submissions():
    from video_factory.integrations.scene_renderer import SceneRenderOperationStore
    from video_factory.integrations.ports import VideoGenerationRequest
    conn=sqlite3.connect(':memory:')
    store=SceneRenderOperationStore(conn)
    for i in range(3):
        request=VideoGenerationRequest(f'op-{i}','owner','project','same-logical-scene',f'actual delta {i}',8,
            provider_options={'creative_mode':'four_agent','creative_video_call_limit':12})
        store.mark_planned(request)
        assert store.claim_provider_for_submission(request,'google_flow').can_submit
    fourth=replace(request,request_id='op-4',prompt='different again')
    store.mark_planned(fourth)
    assert store.claim_provider_for_submission(fourth,'google_flow').status=='send_limit_exceeded'
    assert conn.execute("SELECT COUNT(*) FROM video_factory_scene_operation WHERE status='submitting'").fetchone()[0]==3
    conn.close()


def test_video_generation_quota_survives_scene_id_revision(tmp_path):
    from video_factory.integrations.scene_renderer import SceneRenderOperationStore
    from video_factory.integrations.ports import VideoGenerationRequest
    conn=sqlite3.connect(tmp_path/'scene-quota.db')
    store=SceneRenderOperationStore(conn)
    for attempt in range(3):
        request=VideoGenerationRequest(f'op-{attempt}','owner','project',f'scene-{attempt}',
            f'prompt revision {attempt}',8,provider_options={
                'creative_mode':'four_agent','creative_video_call_limit':12})
        store.mark_planned(request,render_batch_id=f'batch-{attempt}',scene_order=1)
        assert store.claim_provider_for_submission(request,'google_flow').can_submit
    changed=VideoGenerationRequest('op-renamed','owner','project','renamed-scene','new prompt',8,
        provider_options={'creative_mode':'four_agent','creative_video_call_limit':12})
    store.mark_planned(changed,render_batch_id='batch-renamed',scene_order=1)
    assert store.claim_provider_for_submission(changed,'google_flow').status=='send_limit_exceeded'
    conn.close()


def test_end_only_repair_preserves_start_and_unaffected_scene_prompts():
    from test_agent_contracts import profile
    from test_creative_handler_wiring import _dossier,_brief,_script
    from video_factory.creative.agents.contracts import ScenePrompt,ScenePromptBundle
    from video_factory.creative.agents.repair import revise_scene_prompt
    from video_factory.creative.ports import StructuredModelResult
    script=_script(_brief(_dossier()))
    script=replace(script,scenes=(replace(script.scenes[0],end_keyframe_policy='required',end_keyframe_omission_reason=''),*script.scenes[1:]))
    p=replace(profile(),supports_end_frame=True)
    prompts=tuple(ScenePrompt(s.scene_id,s.start_state,s.end_state if s.end_keyframe_policy=='required' else '',
                              s.subject_action,'',s.reference_asset_ids) for s in script.scenes)
    bundle=ScenePromptBundle(script.digest(),p.digest,'playbook',prompts)
    review=CreativeReview('keyframe','FAIL','current',(
        ReviewFinding('f','scene-1','extra_fingers',True,'simple grip','extra finger',(),('scene-1-end:hand',),.9),),
        {},{'artifact_frame_id':'scene-1-end'})
    invariants={'product_truth':'source'}
    proposal=RepairProposal('current',('f',),'keyframe',('scene-1',),'simplify END grip',
                            {'end_prompt':'relaxed two-finger grip'},invariants,('preproduction','keyframe','storyboard'))
    fixed=replace(prompts[0],end_prompt=prompts[0].end_prompt+'; relaxed two-finger grip')
    class Director:
        profile=p
        recipe={'digest':'playbook'}
        def port(self,dossier):
            class Port:
                def generate_json(self,**kwargs):
                    data=fixed.to_dict()
                    data.pop('digest',None)
                    return StructuredModelResult({'prompts':[data]},'{}','fake','fake')
            return Port()
    revised=revise_scene_prompt(director=Director(),dossier=_dossier(),script=script,bundle=bundle,
        proposal=proposal,review=review,protected_invariants=invariants)
    assert revised.prompts[0].start_prompt==prompts[0].start_prompt
    assert revised.prompts[0].end_prompt!=prompts[0].end_prompt
    assert revised.prompts[1:]==prompts[1:]


def test_new_batch_reuses_unchanged_operation_without_rewriting_old_batch(tmp_path,monkeypatch):
    import json
    from video_factory.integrations.scene_renderer import SceneRenderOperationStore
    from video_factory.integrations.ports import VideoGenerationRequest
    from video_factory.integrations import server
    conn=sqlite3.connect(':memory:')
    store=SceneRenderOperationStore(conn)
    request=VideoGenerationRequest('same-operation','owner','project','s1','same action',8)
    store.mark_planned(request,render_batch_id='batch_old',scene_order=1)
    monkeypatch.setattr(server,'_workspace_path',lambda:tmp_path)
    context=tmp_path/'creative_batches'/'batch_new.json'
    context.parent.mkdir()
    context.write_text(json.dumps({'project_id':'project','_batch_operation_ids':['same-operation']}),encoding='utf8')
    members=server._creative_batch_operations(conn,'batch_new')
    assert members[0]['operation_id']=='same-operation'
    assert store.get('same-operation')['render_batch_id']=='batch_old'
    assert conn.execute('SELECT COUNT(*) FROM video_factory_scene_operation').fetchone()[0]==1
    conn.close()


def test_assembly_repair_reuses_saved_proposal_and_fourth_rewrite_is_blocked(tmp_path):
    import json
    from test_agent_contracts import profile
    from test_creative_handler_wiring import _dossier,_brief,_script
    from video_factory.creative.agents.service import AgenticCreativeGenerator
    from video_factory.creative.ports import StructuredModelResult
    class PlannerOnly:
        calls=0
        def generate_json(self,**kwargs):
            self.calls+=1
            assert kwargs['schema_name']=='repair-proposal-v1'
            data=json.loads(kwargs['user_prompt'])
            review,context=data['review'],data['context']
            proposal={'base_digest':context['current_digest'],'finding_ids':['seam'],
                'scope':'assembly','scene_ids':['scene-1'],'diagnosis':'Rebuild the documented join',
                'changes':{'rebuild':True},'protected_invariants':context['protected_invariants'],
                'recheck_stages':['final']}
            return StructuredModelResult(proposal,json.dumps(proposal),'fake','fake-planner')
        def generate_media_json(self,**kwargs):
            pytest.fail('assembly proposal must not rewrite prompts or re-evaluate unchanged media here')
    model=PlannerOnly()
    generator=AgenticCreativeGenerator(model,profile=profile(),category='gadget_demo',visible_details=['visible_controls'])
    dossier=_dossier()
    script=_script(_brief(dossier))
    scene_ref={'existing_storyboard':'preserved'}
    finding=ReviewFinding('seam','scene-1','join',True,'continuous join','assembly gap',(),('join:8',),.95)
    review=CreativeReview('final','FAIL','first-input',(finding,),{}, {'provider':'fake'})
    db=tmp_path/'retry.db'
    conn=sqlite3.connect(db)
    def repair(current):
        return generator.prepare_targeted_repair(dossier=dossier,script=script,scene_ref=scene_ref,
            review=current,connection=conn)
    first=repair(review)
    assert first['scene_ref']==scene_ref and first['assembly_repair']['changes']=={'rebuild':True}
    conn.close()
    conn=sqlite3.connect(db)
    assert repair(review)==json.loads(json.dumps(first)) and model.calls==1
    assert repair(replace(review,input_digest='second-input'))['assembly_repair']
    assert repair(replace(review,input_digest='third-input'))['assembly_repair']
    assert repair(replace(review,input_digest='fourth-input')) is None
    assert model.calls==3
    assert conn.execute("SELECT COUNT(*) FROM workflow_qc_retries WHERE retry_type='creative_rewrite'").fetchone()[0]==3
    conn.close()


def test_targeted_repair_uses_finding_stage_without_duplicate_preproduction_review(tmp_path, monkeypatch):
    from test_agent_contracts import profile
    from test_creative_handler_wiring import _dossier,_brief,_script
    from video_factory.creative.agents import critic as critic_module, repair as repair_module, service
    from video_factory.creative.agents.contracts import ScenePrompt,ScenePromptBundle

    dossier=_dossier(tmp_path)
    script=_script(_brief(dossier))
    p=profile()
    prompts=tuple(ScenePrompt(s.scene_id,s.start_state,s.end_state,s.subject_action,'',s.reference_asset_ids)
                  for s in script.scenes)
    bundle=ScenePromptBundle(script.digest(),p.digest,'playbook',prompts)
    finding=ReviewFinding('f','scene-1','wrong_action',True,'picked up','wrong action',(),('scene-1-start',),.9)
    review=CreativeReview('final','FAIL','input-digest',(finding,),{}, {'provider':'fake'})
    class Planner:
        def __init__(self, model): pass
        def propose(self, *, review, context):
            finding=review.findings[0]
            return RepairProposal(context['current_digest'],(finding.finding_id,),'keyframe',(finding.scene_id,),
                'repair action',{'start_prompt':'correct action'},context['protected_invariants'],('final',))
    critic_calls={'count':0}
    class Critic:
        def __init__(self,*args,**kwargs): pass
        def review(self,**kwargs):
            critic_calls['count']+=1
            pytest.fail('targeted repair must defer evaluation to the changed final artifact')
    monkeypatch.setattr(repair_module,'RepairPlanner',Planner)
    monkeypatch.setattr(critic_module,'CreativeCritic',Critic)
    monkeypatch.setattr(repair_module,'revise_scene_prompt',lambda **kwargs:bundle)
    generator=service.AgenticCreativeGenerator(object(),profile=p,category='gadget_demo',visible_details=['visible_controls'])
    connection=sqlite3.connect(tmp_path/'repair-attempt.db')
    scene_ref={'scene_prompts':bundle.to_dict()}
    repaired=generator.prepare_targeted_repair(dossier=dossier,script=script,scene_ref=scene_ref,
        review=review,connection=connection)
    assert len(repaired['repair_history']) == 1
    critic_count=critic_calls['count']
    replayed=generator.prepare_targeted_repair(dossier=dossier,script=script,scene_ref=scene_ref,
        review=review,connection=connection)
    assert set(replayed) == set(repaired)
    from video_factory.creative.contracts import ScriptScenePlanV2
    from video_factory.creative.agents.contracts import ScenePromptBundle
    ScriptScenePlanV2.from_dict(replayed)
    ScenePromptBundle.from_dict(replayed['scene_prompts'])
    assert 'preproduction_review' not in replayed
    assert replayed['scene_prompts']['script_digest'] == repaired['scene_prompts']['script_digest']
    assert all('Reference identity lock; match attached source pixels.' in prompt['video_prompt']
               for prompt in repaired['scene_prompts']['prompts'])
    assert critic_calls['count'] == critic_count
    rows=connection.execute("SELECT status, verdict, action_taken FROM workflow_qc_retries ORDER BY id").fetchall()
    assert rows == [('completed','pass','targeted_prompt_repair')]
    connection.close()


def test_unknown_preproduction_recheck_replays_as_unknown_not_content_fail(tmp_path, monkeypatch):
    import json
    from test_agent_contracts import profile
    from test_creative_handler_wiring import _dossier,_brief,_script
    from video_factory.creative.agents import critic as critic_module, repair as repair_module, service
    from video_factory.creative.agents.contracts import ScenePrompt,ScenePromptBundle
    dossier=_dossier(tmp_path)
    script=_script(_brief(dossier))
    p=profile()
    bundle=ScenePromptBundle(script.digest(),p.digest,'playbook',tuple(
        ScenePrompt(s.scene_id,s.start_state,s.end_state,s.subject_action,'',s.reference_asset_ids)
        for s in script.scenes))
    finding=ReviewFinding('f','scene-1','wrong_action',True,'supported','wrong',(),('frame-1',),.9)
    initial=CreativeReview('preproduction','FAIL','initial',(finding,),{}, {'provider':'fake'})
    unknown=CreativeReview('preproduction','UNKNOWN','recheck',(),{}, {'provider':'fake'})
    class Planner:
        def __init__(self, model): pass
        def propose(self, *, review, context):
            return RepairProposal(context['current_digest'],('f',),'keyframe',('scene-1',),
                'repair',{'start_prompt':'repair'},context['protected_invariants'],('preproduction',))
    class Critic:
        def __init__(self,*args,**kwargs): pass
        def review(self,**kwargs): return unknown
    monkeypatch.setattr(repair_module,'RepairPlanner',Planner)
    monkeypatch.setattr(critic_module,'CreativeCritic',Critic)
    monkeypatch.setattr(repair_module,'revise_scene_prompt',lambda **kwargs:bundle)
    generator=service.AgenticCreativeGenerator(object(),profile=p,category='gadget_demo',visible_details=['visible_controls'])
    connection=sqlite3.connect(tmp_path/'unknown-repair.db')
    scene_ref={'scene_prompts':bundle.to_dict()}
    first=generator.prepare_targeted_repair(dossier=dossier,script=script,scene_ref=scene_ref,
        review=initial,connection=connection)
    # A later attempt's UNKNOWN must remain bound to its own ledger row when
    # replaying the earlier attempt, whose provider outcome is now a known FAIL.
    from video_factory.qc.retry_policy import complete_qc_attempt
    failed_id=first['attempt_id']
    connection.execute("UPDATE workflow_qc_retries SET status='completed',verdict='fail' WHERE id=?",(failed_id,))
    later=claim_qc_attempt(connection,workflow_id=dossier.workflow_id,retry_type='creative_rewrite',
        item_id='product',input_hash='later-input',operation_id='rewrite-later',max_recoveries=2)
    complete_qc_attempt(connection,attempt_id=later['attempt_id'],status='technical_unknown',verdict='unknown',
        action_taken='preproduction_recheck_failed',reason_text=json.dumps({'review':unknown.to_dict()}))
    replay=generator.prepare_targeted_repair(dossier=dossier,script=script,scene_ref=scene_ref,
        review=initial,connection=connection)
    assert first['repair_unknown'] is True and replay['repair_unknown'] is True
    assert replay['review']['verdict']=='UNKNOWN' and replay['attempt_id']==later['attempt_id']
    assert first['review']['verdict']=='UNKNOWN'
    rows=connection.execute("SELECT status,verdict FROM workflow_qc_retries WHERE retry_type='creative_rewrite' ORDER BY attempt_number").fetchall()
    assert rows==[('completed','fail'),('technical_unknown','unknown')]
    connection.close()
