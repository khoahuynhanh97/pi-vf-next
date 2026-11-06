import hashlib
import json
import os
from argparse import Namespace
from pathlib import Path

import pytest
from PIL import Image

from test_creative_resume import config
from video_factory.automation.creative_cli import cmd_creative_batch
from video_factory.automation.importer import publish_daily_run_to_exchange
from video_factory.automation.coordinator import ProductVideoCoordinator
from video_factory.constants import get_hermes_db_path
from video_factory.storage_infra.sqlite import connect


def test_cli_and_studio_db_binding_precedence(tmp_path, monkeypatch):
    configured_home = tmp_path / 'configured-home'
    explicit_db = tmp_path / 'explicit' / 'hermes.db'
    hermes_home = Path(r'D:\vf-test-hermes-home') if os.name == 'nt' else tmp_path / 'hermes-home'
    monkeypatch.setenv('VIDEO_FACTORY_HOME', str(configured_home))
    monkeypatch.setenv('HERMES_HOME', str(hermes_home))

    assert get_hermes_db_path(explicit_db) == explicit_db.resolve()
    from video_factory.cli import build_parser
    args = build_parser().parse_args(['creative-batch', '--db-path', str(explicit_db)])
    assert get_hermes_db_path(args.db_path) == explicit_db.resolve()
    assert get_hermes_db_path() == (configured_home / 'hermes.db').resolve()

    monkeypatch.delenv('VIDEO_FACTORY_HOME')
    assert get_hermes_db_path() == (hermes_home / 'hermes.db').resolve()

    monkeypatch.setenv('HERMES_HOME', r'C:\Users\ninak\AppData\Local\hermes')
    repo_home = Path(__file__).resolve().parents[1] / 'data'
    assert get_hermes_db_path() == (repo_home / 'hermes.db').resolve()


@pytest.mark.parametrize('level', [None, 'middle', 'high'])
def test_batch_entrypoint_pins_ready_batch_and_ordered_product_keys(tmp_path, monkeypatch, level):
    date = '2026-10-24'
    source = tmp_path / 'source'
    source.mkdir()
    images=[]
    for i, color in enumerate(('red','blue')):
        path=source/f'{i}.png'
        Image.new('RGB',(120,160),color).save(path)
        images.append({'asset_id':f'image-{i}','absolute_path':str(path)})
    (source/'products.json').write_text(json.dumps({'date':date,'products':[
        {'id':'sku-1','name':'Fixture Headphones','brand':'Fixture','model':'Headphones',
         'variant':'standard','images':images},
        {'id':'sku-bibigo','name':'Bibigo snack','brand':'Bibigo','model':'Seaweed Snack',
         'category':'food','images':images},
    ]}),encoding='utf-8')
    inbox=tmp_path/'inbox'
    batch=publish_daily_run_to_exchange(source,inbox,batch_id='run_01')
    profile=tmp_path/'profile.json'
    run_config=config()
    run_config['max_products']=0  # legacy pilot ceiling is not a batch selection limit
    profile.write_text(json.dumps(run_config),encoding='utf-8')
    db=tmp_path/'worker.db'
    monkeypatch.setattr(ProductVideoCoordinator,'advance_preparation',
        lambda self,workflow_id,**kwargs:{'workflow_id':workflow_id,'state':'AwaitingSceneApproval','action':'prepared'})
    monkeypatch.setattr(ProductVideoCoordinator,'produce_from_catalog',
        lambda self,**kwargs:{'selected':[],'skipped':[],'shortfall':0})
    args=Namespace(profile=profile,db_path=db,inbox_root=inbox,run_id='daily-run',date=date,
        all=True,limit=None,product_key=None,owner_user_id='local-owner',max_ticks=1, qc_level=level)

    assert cmd_creative_batch(args)==2  # Empty production selection is unresolved, not success.
    conn=connect(db)
    coordinator=ProductVideoCoordinator(conn,db_path=db,inbox_root=inbox)
    selection_id='pv-creative-batch-'+hashlib.sha256(b'daily-run').hexdigest()[:24]
    selection=coordinator.workflow_repo.get_workflow(selection_id)[0]
    assert selection['batch_id']=='run_01'
    assert selection['qc_level']==(level or 'high')
    assert selection['batch_dir']==str(batch)
    assert len(selection['ordered_product_keys'])==1
    assert len(selection['prepared_refs'])==1
    assert any(item['reason'].startswith('PRODUCT_VARIANT_REQUIRED') for item in selection['skipped'])
    # Older selections omit the pinned QC field; their child policies remain authoritative.
    selection.pop('qc_level')
    _, version = coordinator.workflow_repo.get_workflow(selection_id)
    coordinator.workflow_repo.commit_workflow(selection_id,json.dumps(selection),[],expected_version=version)
    second_source=tmp_path/'source-2'
    second_source.mkdir()
    (second_source/'products.json').write_text(json.dumps({'date':date,'products':[{
        'id':'sku-2','name':'Fixture Keyboard','brand':'Fixture','model':'Keyboard',
        'images':images}]}),encoding='utf-8')
    publish_daily_run_to_exchange(second_source,inbox,batch_id='run_02')
    resumed_args = Namespace(**{**vars(args), 'qc_level':None})
    assert cmd_creative_batch(resumed_args)==2
    assert resumed_args.qc_level == (level or 'high')
    selection=coordinator.workflow_repo.get_workflow(selection_id)[0]
    assert selection['batch_id']=='run_01'
    conn.close()


def test_creative_batch_cli_accepts_all_limit_or_explicit_list():
    from video_factory.cli import build_parser
    parser=build_parser()
    base=['creative-batch','--run-id','daily','--date','2026-10-24','--profile','profile.json']
    assert parser.parse_args(base+['--all']).func.__name__=='cmd_creative_batch'
    assert parser.parse_args(base+['--limit','2']).limit==2
    assert parser.parse_args(base+['--product-key','one','--product-key','two']).product_key==['one','two']
    assert parser.parse_args(base+['--all']).qc_level is None  # resolved from saved policy or fresh high
    assert parser.parse_args(base+['--all','--qc-level','low']).qc_level == 'low'
    assert parser.parse_args(base+['--all','--qc-level','middle']).qc_level == 'middle'


def test_creative_batch_cli_needs_no_internal_run_binding_or_selection_flags():
    from video_factory.cli import build_parser
    args=build_parser().parse_args(['creative-batch'])
    assert args.run_id is None
    assert args.date is None
    assert args.profile is None
    assert args.all is False and args.limit is None and args.product_key is None


def test_creative_batch_reports_missing_profile_as_a_clear_technical_error(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv('VF_CREATIVE_RUN_CONFIG', raising=False)
    args=Namespace(profile=None,db_path=tmp_path/'unused.db',inbox_root=tmp_path/'inbox',run_id=None,date=None,
        all=False,limit=None,product_key=None,owner_user_id='local-owner',max_ticks=1)
    assert cmd_creative_batch(args)==2
    output=capsys.readouterr().out
    assert 'VF_CREATIVE_RUN_CONFIG' in output
    assert 'Tổng kết: thành công 0, BLOCKED 0, lỗi kỹ thuật 1.' in output


def test_creative_batch_rejects_food_with_gadget_profile_before_pinning_or_preparation(
        tmp_path, monkeypatch, capsys):
    date = '2026-10-24'
    source = tmp_path / 'source'
    source.mkdir()
    images = []
    for index, color in enumerate(('red', 'blue')):
        image = source / f'snack-{index}.png'
        Image.new('RGB', (120, 160), color).save(image)
        images.append({'asset_id': f'snack-{index}', 'absolute_path': str(image)})
    (source / 'products.json').write_text(json.dumps({'date': date, 'products': [{
        'id': 'sku-snack', 'name': 'Bibigo snack', 'brand': 'Bibigo', 'model': 'Seaweed Snack',
        'variant': 'assorted 25 g',
        'category': 'đồ ăn vặt',
        'images': images,
    }]}), encoding='utf-8')
    inbox = tmp_path / 'inbox'
    batch = publish_daily_run_to_exchange(source, inbox, batch_id='run_food')
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps(config()), encoding='utf-8')  # gadget_demo
    db = tmp_path / 'worker.db'
    monkeypatch.setattr(ProductVideoCoordinator, 'prepare_catalog',
                        lambda *args, **kwargs: pytest.fail('preparation must not start with mismatched profile'))
    args = Namespace(profile=profile, db_path=db, inbox_root=inbox, run_id='food-run', date=date,
        all=True, limit=None, product_key=None, owner_user_id='local-owner', max_ticks=1)

    assert cmd_creative_batch(args) == 2
    assert 'food_pov' in capsys.readouterr().out
    conn = connect(db)
    selection_id = 'pv-creative-batch-' + hashlib.sha256(b'food-run').hexdigest()[:24]
    assert ProductVideoCoordinator(conn, db_path=db, inbox_root=inbox).workflow_repo.get_workflow(selection_id) is None
    conn.close()


def test_batch_identity_filter_blocks_active_variant_across_runs(tmp_path):
    import json
    conn=connect(tmp_path/'worker.db')
    coordinator=ProductVideoCoordinator(conn,db_path=tmp_path/'worker.db')
    coordinator.workflow_repo.commit_workflow('other-profile-run',json.dumps({
        'state':'rendering','source_context':{'canonical_product_key':'anker:a1290','variant':'black'},
        'creative_policy':{'profile_digest':'different'}}),[],expected_version=0)

    assert coordinator.catalog_product_outcome('ANKER:A1290','BLACK') == {
        'workflow_id':'other-profile-run','reason':'ACTIVE_IDENTITY_VARIANT:other-profile-run'}
    assert coordinator.catalog_product_outcome('anker:a1290','white') is None
    conn.close()


def test_received_invalid_creative_stage_is_resumable_for_same_product_variant(tmp_path):
    conn=connect(tmp_path/'worker.db')
    coordinator=ProductVideoCoordinator(conn,db_path=tmp_path/'worker.db')
    coordinator.workflow_repo.commit_workflow('creative-invalid-active',json.dumps({
        'state':'WaitingForOperator','failure_code':'CREATIVE_RESPONSE_INVALID',
        'operator_action':'creative_review','operator_stage':'script',
        'source_context':{'canonical_product_key':'xiaomi:lamp-lite','variant':'white'}}),[],expected_version=0)

    assert coordinator.catalog_product_outcome('xiaomi:lamp-lite','white') == {
        'workflow_id':'creative-invalid-active','reason':'RESUMABLE_CREATIVE_FAILURE'}
    conn.close()


def test_preparation_reuses_known_invalid_creative_checkpoint(tmp_path):
    conn=connect(tmp_path/'worker.db')
    coordinator=ProductVideoCoordinator(conn,db_path=tmp_path/'worker.db')
    coordinator.workflow_repo.commit_workflow('creative-invalid-checkpoint',json.dumps({
        'state':'WaitingForOperator','failure_code':'CREATIVE_RESPONSE_INVALID',
        'operator_action':'creative_review','operator_stage':'script','owner_user_id':'local-owner',
        'source_context':{'canonical_product_key':'xiaomi:lamp-lite','variant':'white'}}),[],
        outbox_message=json.dumps({'workflow_id':'creative-invalid-checkpoint','job_name':'product_video.scene_plan'}),
        expected_version=0)
    conn.execute("UPDATE workflow_outbox SET state='quarantined',attempt_count=1,max_attempts=5,last_error=? WHERE workflow_id=?",
        ('CREATIVE_RESPONSE_INVALID: response already received','creative-invalid-checkpoint'))
    conn.commit()

    result=coordinator.prepare_catalog(preparation_id='daily-run',run_config=config(),
        source_workflow_id='creative-invalid-checkpoint',owner_user_id='local-owner')

    assert result['workflow_id']=='creative-invalid-checkpoint'
    assert result['state']=='WaitingForOperator'
    assert result['action']=='prepare'
    conn.close()


def test_unresolved_qc_attempt_only_blocks_its_selected_product(tmp_path):
    from video_factory.qc.retry_policy import claim_qc_attempt
    conn=connect(tmp_path/'worker.db')
    coordinator=ProductVideoCoordinator(conn,db_path=tmp_path/'worker.db')
    coordinator.workflow_repo.commit_workflow('other-product',json.dumps({
        'state':'rendering','source_context':{'canonical_product_key':'other:product'}}),[],expected_version=0)
    claim_qc_attempt(conn,workflow_id='other-product',retry_type='creative_final_recheck',
        item_id='final',input_hash='digest',operation_id='op-unknown',max_recoveries=0)

    assert coordinator._unresolved_qc_attempt({'selected:product'}) is None
    assert coordinator._unresolved_qc_attempt({'other:product'})['workflow_id']=='other-product'
    conn.close()


def test_limit_applies_after_active_identity_is_filtered(tmp_path, monkeypatch):
    date='2026-10-24'
    source=tmp_path/'source'
    source.mkdir()
    images=[]
    for i,color in enumerate(('red','blue')):
        path=source/f'{i}.png'
        Image.new('RGB',(120,160),color).save(path)
        images.append({'asset_id':f'image-{i}','absolute_path':str(path)})
    products=[{'id':f'sku-{i}','name':f'Fixture Product {i}','brand':'Fixture','model':f'Product {i}',
        'variant':'standard',
        'images':images} for i in range(2)]
    (source/'products.json').write_text(json.dumps({'date':date,'products':products}),encoding='utf-8')
    inbox=tmp_path/'inbox'
    batch=publish_daily_run_to_exchange(source,inbox,batch_id='run_limit')
    profile=tmp_path/'profile.json'
    profile.write_text(json.dumps(config()),encoding='utf-8')
    db=tmp_path/'worker.db'
    conn=connect(db)
    coordinator=ProductVideoCoordinator(conn,db_path=db,inbox_root=inbox)
    manifest=coordinator.importer.load_manifest(batch)
    first,second=manifest.products
    coordinator.workflow_repo.commit_workflow('other-profile-active',json.dumps({
        'state':'rendering','source_context':{'canonical_product_key':first.canonical_product_key,
                                               'variant':first.variant}}),[],expected_version=0)
    conn.close()
    monkeypatch.setattr(ProductVideoCoordinator,'advance_preparation',
        lambda self,workflow_id,**kwargs:{'workflow_id':workflow_id,'state':'AwaitingSceneApproval','action':'prepared'})
    monkeypatch.setattr(ProductVideoCoordinator,'produce_from_catalog',
        lambda self,prepared_refs,**kwargs:{'selected':[{'prepared_ref':ref,'workflow_id':ref} for ref in prepared_refs],
            'skipped':[],'shortfall':0})
    def finish_selected(self, workflow_id, **kwargs):
        from video_factory.workflows.repository import WorkflowRepository
        repo=WorkflowRepository(self.connection)
        aggregate,version=repo.get_workflow(workflow_id)
        output=tmp_path/'selected.mp4'
        output.write_bytes(b'fixture-mp4')
        aggregate.update(state='completed',export_result={'payload':json.dumps({'file_path':str(output)})})
        repo.commit_workflow(workflow_id,json.dumps(aggregate),[],expected_version=version)
        return {'workflow_id':workflow_id,'state':'completed','action':'completed'}
    monkeypatch.setattr(ProductVideoCoordinator,'advance_creative',finish_selected)
    args=Namespace(profile=profile,db_path=db,inbox_root=inbox,run_id='limit-after-filter',date=date,
        all=False,limit=1,product_key=None,owner_user_id='local-owner',max_ticks=1)

    assert cmd_creative_batch(args)==2
    conn=connect(db)
    coordinator=ProductVideoCoordinator(conn,db_path=db,inbox_root=inbox)
    selection_id='pv-creative-batch-'+hashlib.sha256(b'limit-after-filter').hexdigest()[:24]
    selection=coordinator.workflow_repo.get_workflow(selection_id)[0]
    assert selection['ordered_product_keys']==[second.product_key]
    assert any(item['reason'].startswith('ACTIVE_IDENTITY_VARIANT:') for item in selection['skipped'])
    conn.close()


def test_completed_final_revalidation_checks_bytes_without_content_critic(tmp_path, monkeypatch):
    from test_agent_contracts import profile
    from video_factory.creative.agents import video_review
    conn=connect(tmp_path/'worker.db')
    coordinator=ProductVideoCoordinator(conn,db_path=tmp_path/'worker.db')
    sha='a'*64
    final={'sha256':sha,'file_path':'final.mp4','creative_lineage':{'package_digest':'package-v1'}}
    export={'payload':json.dumps({'file_path':'final.mp4'})}
    coordinator.workflow_repo.commit_workflow('completed-final',json.dumps({
        'state':'completed','owner_user_id':'local-owner','render_result':final,'export_result':export,
        'creative_policy':{'profile':profile().to_dict(),'category':'gadget_demo',
                           'visible_details':['visible_controls']}}),[],expected_version=0)
    monkeypatch.setattr(video_review,'require_final_pass',lambda _artifact:None)
    monkeypatch.setattr(video_review,'review_video',lambda **kwargs:pytest.fail('final content critic must not run'))
    monkeypatch.setattr(ProductVideoCoordinator,'_export_sha',staticmethod(lambda exported:sha))

    result=coordinator.revalidate_completed_final('completed-final')

    assert result['status']=='accepted' and result['sha256']==sha
    assert result['reused'] is True
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='workflow_qc_retries'").fetchone()[0]==0
    conn.close()


def test_creative_batch_continues_past_per_product_blocker(tmp_path, monkeypatch, capsys):
    date='2026-10-24'
    source=tmp_path/'source'
    source.mkdir()
    images=[]
    for i,color in enumerate(('red','blue')):
        path=source/f'{i}.png'
        Image.new('RGB',(120,160),color).save(path)
        images.append({'asset_id':f'image-{i}','absolute_path':str(path)})
    products=[{'id':f'sku-{i}','name':f'Fixture Product {i}','brand':'Fixture','model':f'Product {i}',
        'variant':'standard',
        'images':images} for i in range(2)]
    products.append({'id':'sku-missing-variant','name':'Bibigo assorted snack','brand':'Bibigo',
        'model':'Seaweed Snack','category':'food','images':images})
    (source/'products.json').write_text(json.dumps({'date':date,'products':products}),encoding='utf-8')
    inbox=tmp_path/'inbox'
    publish_daily_run_to_exchange(source,inbox,batch_id='run_per_product')
    profile=tmp_path/'profile.json'
    profile.write_text(json.dumps(config()),encoding='utf-8')
    db=tmp_path/'worker.db'

    advance_prep_calls=[]
    def mock_advance_prep(self, workflow_id, **kwargs):
        advance_prep_calls.append(workflow_id)
        return {'workflow_id':workflow_id,'state':'AwaitingSceneApproval','action':'prepared'}

    produce_calls=[]
    def mock_advance_creative(self, workflow_id, **kwargs):
        produce_calls.append(workflow_id)
        if len(produce_calls) == 1:
            return {'workflow_id':workflow_id,'state':'AwaitingSceneApproval','action':'blocker',
                    'code':'KEYFRAME_CONTENT_UNKNOWN','reason':'scene_01 requires reconciliation'}
        return {'workflow_id':workflow_id,'state':'completed','action':'completed'}

    monkeypatch.setattr(ProductVideoCoordinator,'advance_preparation',mock_advance_prep)
    monkeypatch.setattr(ProductVideoCoordinator,'produce_from_catalog',
        lambda self,prepared_refs,**kwargs:{
            'selected':[{'prepared_ref':ref,'workflow_id':ref} for ref in prepared_refs],
            'skipped':[],'shortfall':0})
    monkeypatch.setattr(ProductVideoCoordinator,'advance_creative',mock_advance_creative)

    args=Namespace(profile=profile,db_path=db,inbox_root=inbox,run_id='batch-continue-test',date=date,
        all=True,limit=None,product_key=None,owner_user_id='local-owner',max_ticks=1)

    # Return code is 2 because there is an unresolved product, but both were processed
    assert cmd_creative_batch(args)==2
    assert len(produce_calls) == 2
    output=capsys.readouterr().out
    assert 'Bibigo assorted snack: BLOCKED — PRODUCT_VARIANT_REQUIRED' in output


def test_same_batch_resume_picks_up_repaired_item_without_rerunning_completed_item(tmp_path, monkeypatch, capsys):
    from video_factory.workflows.repository import WorkflowRepository
    from video_factory.application.operator_handler import replace_blocked_source_images

    date='2026-10-24'
    source=tmp_path/'source'
    source.mkdir()
    images=[]
    for i,color in enumerate(('red','blue')):
        path=source/f'{i}.png'
        Image.new('RGB',(120,160),color).save(path)
        images.append({'asset_id':f'image-{i}','absolute_path':str(path)})
    products=[{'id':f'sku-{i}','name':f'Fixture Product {i}','brand':'Fixture','model':f'Product {i}',
        'variant':'standard',
        'images':images} for i in range(2)]
    (source/'products.json').write_text(json.dumps({'date':date,'products':products}),encoding='utf-8')
    inbox=tmp_path/'inbox'
    publish_daily_run_to_exchange(source,inbox,batch_id='run_resume_input')
    profile=tmp_path/'profile.json'
    profile.write_text(json.dumps(config()),encoding='utf-8')
    db=tmp_path/'worker.db'
    monkeypatch.setenv('VF_CREATIVE_RUN_CONFIG',str(profile))
    args=Namespace(profile=None,db_path=db,inbox_root=inbox,run_id=None,date=None,
        all=False,limit=None,product_key=None,owner_user_id='local-owner',max_ticks=1)
    prep_calls=[]
    blocked_workflow_id=None
    completed_calls=[]
    production_calls=[]

    def prepare_catalog(self, preparation_id, run_config, source_workflow_id, **kwargs):
        nonlocal blocked_workflow_id
        prep_calls.append(source_workflow_id)
        aggregate=self.workflow_repo.get_workflow(source_workflow_id)[0]
        if source_workflow_id == blocked_workflow_id and aggregate.get('state') == 'building_resource_pack':
            return {'action':'prepare','workflow_id':source_workflow_id}
        if len(prep_calls) == 2:
            blocked_workflow_id=source_workflow_id
            aggregate['state']='skipped'
            aggregate['failure_code']='EXCHANGE_IMAGE_QC_FAILED'
            aggregate['failure_reason']='source image QC failed'
            aggregate['skip_reason']='source image QC failed'
            self.workflow_repo.commit_workflow(source_workflow_id,json.dumps(aggregate),[],
                expected_version=self.workflow_repo.get_workflow(source_workflow_id)[1])
            return {'action':'stop','skipped':'SOURCE_IMAGE_QC_FAILED'}
        return {'action':'prepare','workflow_id':source_workflow_id}

    def produce(self_ref, prepared_refs, **kwargs):
        repo=WorkflowRepository(self_ref.connection)
        selected=[]
        for ref in prepared_refs:
            current=repo.get_workflow(ref)
            aggregate,version=current
            if aggregate.get('production_run_id'):
                continue
            aggregate['production_run_id']='pv-production-'+kwargs['run_id']
            repo.commit_workflow(ref,json.dumps(aggregate),[],expected_version=version)
            selected.append({'prepared_ref':ref,'workflow_id':ref})
        production_calls.append((set(prepared_refs),set(item['workflow_id'] for item in selected)))
        return {'selected':selected,'skipped':[],'shortfall':0}

    monkeypatch.setattr(ProductVideoCoordinator,'prepare_catalog',prepare_catalog)
    monkeypatch.setattr(ProductVideoCoordinator,'advance_preparation',
        lambda self,workflow_id,**kwargs:{'workflow_id':workflow_id,'state':'AwaitingSceneApproval','action':'prepared'})
    monkeypatch.setattr(ProductVideoCoordinator,'produce_from_catalog',produce)
    def complete_product(self, workflow_id, **kwargs):
        repo=WorkflowRepository(self.connection)
        aggregate,version=repo.get_workflow(workflow_id)
        output=tmp_path/f'{workflow_id}.mp4'
        output.write_bytes(b'fixture-mp4')
        aggregate.update(state='completed',export_result={'payload':json.dumps({'file_path':str(output)})})
        repo.commit_workflow(workflow_id,json.dumps(aggregate),[],expected_version=version)
        completed_calls.append(workflow_id)
        return {'workflow_id':workflow_id,'state':'completed','action':'completed'}

    monkeypatch.setattr(ProductVideoCoordinator,'advance_creative',complete_product)

    assert cmd_creative_batch(args)==0
    assert len(completed_calls)==1
    first_output=capsys.readouterr().out
    assert 'workflow_id' not in first_output and 'selection_id' not in first_output
    assert 'Thành công' in first_output and 'BLOCKED' in first_output
    assert '.mp4' in first_output and 'Cách sửa:' in first_output
    assert 'Tổng kết: thành công 1, BLOCKED 1, lỗi kỹ thuật 0.' in first_output
    first_completed=completed_calls[0]
    assert blocked_workflow_id is not None
    correction_conn=connect(db)
    # The first preparation's source-QC job has reached its terminal blocked outcome.
    from video_factory.workflows.outbox_repository import OutboxRepository
    outbox=OutboxRepository(correction_conn)
    claims=outbox.claim('test-terminal-source-qc',workflow_ids=[blocked_workflow_id])
    for claim in claims:
        assert outbox.ack(claim.outbox_id,claim.lease_token,'test-terminal-source-qc')
    correction=replace_blocked_source_images(correction_conn,workflow_id=blocked_workflow_id,
        owner_user_id='local-owner',image_paths=[images[1]['absolute_path']],
        reason='Replaced the blocked source image after checking the product label')
    assert correction['state']=='building_resource_pack'
    correction_conn.close()
    assert cmd_creative_batch(args)==0
    second_output=capsys.readouterr().out
    assert 'workflow_id' not in second_output and 'selection_id' not in second_output
    assert 'Tổng kết: thành công 2, BLOCKED 0, lỗi kỹ thuật 0.' in second_output
    assert completed_calls[1] != first_completed
    assert completed_calls.count(first_completed)==1
    conn=connect(db)
    selection_id=conn.execute("SELECT workflow_id FROM workflow WHERE workflow_id LIKE 'pv-creative-batch-%'").fetchone()[0]
    selection=ProductVideoCoordinator(conn,db_path=db,inbox_root=inbox).workflow_repo.get_workflow(selection_id)[0]
    assert selection['skipped']==[]
    assert len(selection['prepared_refs'])==2
    assert len({row["workflow_id"] for row in conn.execute("SELECT workflow_id FROM daily_product_video_import")})==2
    conn.close()

