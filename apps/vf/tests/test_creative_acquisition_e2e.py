"""Offline integration of fresh acquisition checkpoint; all transports are explicit fakes."""
from unittest.mock import MagicMock
import json
from pathlib import Path
import pytest

from test_creative_resume import config
from video_factory.storage_infra.sqlite import connect
from video_factory.automation.coordinator import ProductVideoCoordinator
from video_factory.handlers import (
    JobContext,
    JobFailed,
    JobSucceeded,
    MCPProductIntelligenceAdapter,
    ProductVideoJobHandlers,
)


def test_fresh_pi_receipt_is_preserved_and_restart_reuses_without_recrawl(tmp_path):
    db=tmp_path/'worker.db'
    conn=connect(db)
    coord=ProductVideoCoordinator(conn,db_path=db)
    started=coord.start_fresh_creative(run_id='fresh',query='Synthetic fixture',run_config=config())
    raw={'research_id':'fake-producer-run','snapshot_id':'fake-snapshot','persistence_status':'saved',
         'canonical_products':[{'canonical_product_id':'synthetic-product'}]}
    pi=MagicMock()
    pi.research.return_value={'raw_result':raw}
    handlers=ProductVideoJobHandlers(pi_client=pi,video_client=MagicMock(),workflow_repository=coord.workflow_repo)
    # Binding/state validation is separately covered by existing workflow tests.
    handlers._validate_state_and_version=lambda *args,**kwargs:None
    ctx=JobContext(started['workflow_id'],'research-job',idempotency_key='one-acquisition')
    first=handlers.research_job(ctx,'Synthetic fixture',[])
    assert isinstance(first,JobSucceeded)
    receipt=first.artifact_ref['acquisition_receipt']
    assert receipt['producer_run_id']=='fake-producer-run'
    assert receipt['snapshot_id']=='fake-snapshot'
    handlers=ProductVideoJobHandlers(pi_client=pi,video_client=MagicMock(),workflow_repository=coord.workflow_repo)
    handlers._validate_state_and_version=lambda *args,**kwargs:None
    second=handlers.research_job(ctx,'Synthetic fixture',[])
    assert isinstance(second,JobSucceeded) and second.artifact_ref==first.artifact_ref
    assert pi.research.call_count==1
    conn.close()


def test_crash_after_acquisition_dispatch_cannot_issue_another_request(tmp_path):
    db=tmp_path/'worker.db'
    conn=connect(db)
    coord=ProductVideoCoordinator(conn,db_path=db)
    started=coord.start_fresh_creative(run_id='uncertain',query='Synthetic fixture',run_config=config())
    data,version=coord.workflow_repo.get_workflow(started['workflow_id'])
    data['fresh_acquisition_status']='submitting'
    coord.workflow_repo.commit_workflow(started['workflow_id'],json.dumps(data),[],expected_version=version)
    pi=MagicMock()
    handlers=ProductVideoJobHandlers(pi_client=pi,video_client=MagicMock(),workflow_repository=coord.workflow_repo)
    handlers._validate_state_and_version=lambda *args,**kwargs:None
    result=handlers.research_job(JobContext(started['workflow_id'],'restart'),'Synthetic fixture',[])
    assert isinstance(result,JobFailed) and result.code=='PI_ACQUISITION_UNCERTAIN'
    pi.research.assert_not_called()
    conn.close()


@pytest.mark.parametrize('fail_stage',[None,'clip','final','assembly'])
def test_fake_pi_through_real_durable_worker_to_silent_export_and_skip(tmp_path,monkeypatch,fail_stage):
    import hashlib
    from PIL import Image
    from support.creative_agents_fakes import FakeRoles,FakeVideoAPI,save_qc
    from video_factory.creative.agents.service import AgenticCreativeGenerator
    from video_factory.creative.agents.contracts import BackendProfile
    from video_factory.integrations.keyframe_provider import KeyframeRuntime,KeyframeOperationStore
    from video_factory.integrations.ports import KeyframeGenerationResult
    from video_factory.handlers.registry import create_product_video_topic_handlers
    from video_factory.workflows.worker import run_worker as real_worker
    from video_factory.workflows.budgets import init_default_engine,BudgetPolicy
    from video_factory.workflows.engine import WorkflowEngine
    from video_factory.qc.models import QCStage
    monkeypatch.chdir(tmp_path)
    init_default_engine(BudgetPolicy(max_total=1000,per_channel={'video_factory':500,'product_intelligence':500}))
    conn=connect(tmp_path/'worker.db')
    coordinator=ProductVideoCoordinator(conn,db_path=tmp_path/'worker.db')
    cfg=config()
    started=coordinator.start_fresh_creative(run_id='offline-e2e',query='Fixture Synthetic1',run_config=cfg)
    workflow_id=started['workflow_id']
    project_id='proj_'+workflow_id
    assets=[]
    for index in range(2):
        path=tmp_path/f'source-{index}.png'
        Image.new('RGB',(90,160),('gray','silver')[index]).save(path)
        assets.append({'asset_id':f'a{index}','local_path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    class LocalPIRegistry:
        def __init__(self):
            self.calls=[]
            self.packs={}

        def get_all_tool_names(self):
            return {
                'mcp__product_intelligence__research_product',
                'mcp__product_intelligence__build_resource_pack',
                'mcp__product_intelligence__lock_resource_pack',
            }

        def dispatch(self, tool_name, payload):
            self.calls.append((tool_name, dict(payload)))
            if tool_name.endswith('research_product'):
                return {'status':'success','research_id':'fake-fresh-run','snapshot_id':'fake-snapshot',
                    'persistence_status':'saved',
                    'canonical_products':[{'canonical_product_id':'fixture-synthetic1','brand':'Fixture',
                        'model':'Synthetic1','name':'Fixture Synthetic1'}],
                    'media_assets':assets,'report':{'product_name':'Fixture Synthetic1'}}
            if tool_name.endswith('build_resource_pack'):
                pack_id='rp_'+payload['snapshot_id']
                self.packs[pack_id]=True
                return {'status':'built','resource_pack_id':pack_id,'pack_status':'ready_to_lock',
                    'canonical_product_id':'fixture-synthetic1','media_count':len(assets),
                    'product_identity':{'brand':'Fixture','model':'Synthetic1','name':'Fixture Synthetic1'},
                    'assets_mapping':{asset['asset_id']:asset['local_path'] for asset in assets}}
            if tool_name.endswith('lock_resource_pack'):
                pack_id=payload['resource_pack_id']
                if pack_id not in self.packs:
                    return {'status':'not_found','resource_pack_id':pack_id}
                return {'status':'locked','lock_id':'lock_'+pack_id,'resource_pack_id':pack_id,
                    'snapshot_id':'fake-snapshot','canonical_product_id':'fixture-synthetic1',
                    'owner_user_id':payload['owner_user_id'],'version':1,
                    'manifest_digest':'sha256:fixture-pack-lock','assets':assets}
            raise AssertionError(f'unexpected PI tool: {tool_name}')

    pi_registry=LocalPIRegistry()
    pi=MCPProductIntelligenceAdapter(pi_registry,{
        'research':'mcp__product_intelligence__research_product',
        'build_resource_pack':'mcp__product_intelligence__build_resource_pack',
        'lock_resource_pack':'mcp__product_intelligence__lock_resource_pack',
    })
    model=FakeRoles(fail_stage=fail_stage)
    api=FakeVideoAPI(tmp_path,conn,model)
    model.on_failure=lambda:(api.images,api.videos)
    class Images:
        provider_kind='google_flow'
        def submit(self,request):
            api.images+=1
            path=tmp_path/(request.operation_id+'.png')
            Image.new('RGB',(90,160),'gray').save(path)
            return KeyframeGenerationResult(request.operation_id,'completed',request.operation_id,str(path))
    runtime=KeyframeRuntime(store=KeyframeOperationStore(conn),provider=Images())
    generator=AgenticCreativeGenerator(model,profile=BackendProfile.from_dict(cfg['profile']),
        category=cfg['category'],visible_details=cfg['visible_details'])
    service=ProductVideoJobHandlers(pi_client=pi,video_client=api,creative_generator=generator,
        workflow_engine=WorkflowEngine(coordinator.workflow_repo),workflow_repository=coordinator.workflow_repo,
        keyframe_runtime_factory=lambda:runtime,provider_mode='fake')
    resource_pack_job=service.resource_pack_job
    def with_offline_image_qc(ctx,research,**kwargs):
        result=resource_pack_job(ctx,research,**kwargs)
        if isinstance(result,JobSucceeded):
            artifact=result.artifact_ref
            if not artifact.get('assets'):
                mapping=artifact.get('assets_mapping') or {}
                artifact['assets']=[{'asset_id':asset_id,'local_path':path,
                    'sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest()}
                    for asset_id,path in mapping.items() if Path(path).is_file()]
                artifact['product_references']=artifact['assets']
                artifact['media_assets']=artifact['assets']
            save_qc(conn,workflow_id,project_id,QCStage.IMAGE,artifact)
        return result
    service.resource_pack_job=with_offline_image_qc
    storyboard=service.storyboard_job
    def reviewed_storyboard(ctx,*args,**kwargs):
        result=storyboard(ctx,*args,**kwargs)
        if isinstance(result,JobSucceeded):
            report=save_qc(conn,workflow_id,project_id,QCStage.KEYFRAME,result.artifact_ref)
            receipt={'report_id':report.report_id,'workflow_id':report.workflow_id,
                'stage':report.stage.value,'verdict':report.verdict.value,'score':report.score,
                'artifact_id':report.artifact_id,'artifact_version':report.artifact_version,
                'artifact_digest':report.artifact_digest,'findings':report.findings,
                'hard_failure':False}
            result=JobSucceeded(artifact_ref={**result.artifact_ref,'keyframe_qc_receipt':receipt},
                next_command=result.next_command)
        return result
    service.storyboard_job=reviewed_storyboard
    topics=create_product_video_topic_handlers(workflow_repository=coordinator.workflow_repo,
        workflow_engine=service.workflow_engine,handlers_service=service,provider_mode='fake')
    def worker(**kwargs):
        return real_worker(**kwargs,topic_handlers=topics)
    monkeypatch.setattr('video_factory.automation.coordinator.run_worker',worker)
    states=[]
    for _ in range(25):
        step=coordinator.advance_creative(workflow_id,provider_mode='fake')
        states.append(step['state'])
        if step['state']=='completed':break
        assert not step.get('failed'),[tuple(row) for row in conn.execute(
            'SELECT state,last_error FROM workflow_outbox WHERE workflow_id=?',(workflow_id,))]
    assert states[-1]=='completed',(states,[tuple(row) for row in conn.execute(
        'SELECT state,last_error FROM workflow_outbox WHERE workflow_id=?',(workflow_id,))],model.calls)
    expected_videos=5 if fail_stage in {'clip','final'} else 4
    assert api.images==4 and api.videos==expected_videos
    assert [name.rsplit('__',1)[-1] for name,_ in pi_registry.calls]==[
        'research_product','build_resource_pack','lock_resource_pack']
    calls=len(model.calls)
    assert coordinator.advance_creative(workflow_id,provider_mode='fake')['action']=='stop'
    assert (api.images,api.videos,len(model.calls),len(pi_registry.calls))==(4,expected_videos,calls,3)
    aggregate=coordinator.workflow_repo.get_workflow(workflow_id)[0]
    assert aggregate['research_result']['acquisition_receipt']['snapshot_id']=='fake-snapshot'
    expected_product_id='fixture-synthetic1'
    assert aggregate['research_result']['acquisition_receipt']['canonical_product_id']==expected_product_id
    assert aggregate['resource_pack']['canonical_product_id']==expected_product_id
    assert aggregate['locked_pack']['canonical_product_id']==expected_product_id
    dossier=aggregate['creative_brief']['research_dossier']
    assert dossier['product_identity']['canonical_product_id']==expected_product_id
    assert {item['sha256'] for item in dossier['eligible_assets']}=={item['sha256'] for item in assets}
    assert all(asset['local_path'] in [item['local_path'] for item in assets]
        for asset in aggregate['locked_pack']['assets'])
    rendered_refs={path for frame in aggregate['render_result']['creative_lineage']['generated_keyframes']
        for path in frame.get('reference_image_paths',[])}
    assert rendered_refs=={item['local_path'] for item in assets}
    assert aggregate['render_result']['human_review']=='not_performed'
    if fail_stage:
        assert model.calls.count('repair-proposal-v1')==1
        assert conn.execute("SELECT COUNT(*) FROM workflow_qc_retries WHERE retry_type='creative_rewrite'").fetchone()[0]==1
    if fail_stage in {'clip','final'}:
        assert model.failure_generation_counts==(4,2 if fail_stage=='clip' else 4)
        assert conn.execute("SELECT COUNT(*) FROM video_factory_scene_operation WHERE scene_id='s2' AND submitted_at IS NOT NULL").fetchone()[0]==2
        assert conn.execute("SELECT COUNT(*) FROM video_factory_scene_operation WHERE scene_id='s1' AND submitted_at IS NOT NULL").fetchone()[0]==1
    if fail_stage=='assembly':
        assert model.failure_generation_counts==(4,4)
        assert api.recompositions==1
        assert aggregate['render_result']['sha256']!=api.rejected_final_sha
        assert model.calls.count('prompt-repair-v1')==0
        assert model.calls.count('script-scene-plan-v2')==1
        assert conn.execute("SELECT COUNT(*) FROM video_factory_scene_operation WHERE submitted_at IS NOT NULL").fetchone()[0]==4
    export_path=Path(aggregate['export_result']['file_path'])
    assert export_path.is_file()
    assert hashlib.sha256(export_path.read_bytes()).hexdigest()==aggregate['export_result']['sha256']
    conn.close()
