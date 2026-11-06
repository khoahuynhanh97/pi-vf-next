"""Small offline preparation/consumer fixtures; no external services are loaded."""
import hashlib
import json
from dataclasses import replace

import pytest
from PIL import Image

from support.creative_agents_fakes import FakeRoles, save_qc
from test_creative_resume import config
from video_factory.automation.coordinator import ProductVideoCoordinator
from video_factory.creative.agents.contracts import BackendProfile
from video_factory.creative.agents.service import AgenticCreativeGenerator
from video_factory.creative.contracts import ResearchDossier
from video_factory.handlers.common import _locked_research_dossier
from video_factory.qc.fingerprint import IMAGE_RUBRIC_VERSION
from video_factory.qc.models import QCStage
from video_factory.qc.repository import QCReportRepository
from video_factory.storage_infra.sqlite import connect


def prepared_source(coord, root, key='fixture-1'):
    root.mkdir(parents=True, exist_ok=True)
    assets = []
    for index, color in enumerate(('gray', 'silver')):
        path = root / f'source-{index}.png'
        Image.new('RGB', (90, 160), color).save(path)
        assets.append({'asset_id':f'a{index}', 'local_path':str(path),
                       'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    pack = {'resource_pack_id':'pack-'+key, 'version':1, 'product_name':'Fixture Synthetic1',
            'product_identity':{'brand':'Fixture', 'model':'Synthetic1', 'display_name':'Fixture Synthetic1'},
            'assets':assets}
    workflow_id = 'prep-'+key
    project_id = 'project-'+key
    report = save_qc(coord.connection, workflow_id, project_id, QCStage.IMAGE, pack)
    findings=dict(report.findings)
    findings['items']=[{**item,'findings':{'eligible':True,'verdict':'pass','hard_failure':False,
        'source_branding_detected':False,'identity_mismatch':False,'risk_flags':[]}}
        for item in findings['items']]
    findings['eligible_reference_manifest']=findings['items']
    report=replace(report,findings=findings)
    report = replace(report, provider_mode='configured', rubric_version=IMAGE_RUBRIC_VERSION,
                     model_identifier='offline-fixture-vision', reference_digests=[a['sha256'] for a in assets])
    QCReportRepository(coord.connection).save_report(report)
    cfg = config()
    agg = {'state':'AwaitingSceneApproval', 'owner_user_id':'local-owner', 'project_id':project_id,
           'product_key':key, 'resource_pack':pack, 'locked_pack':pack,
           'research_result':{'raw_result':{'snapshot_id':'snapshot-'+key}},
           'source_context':{'canonical_product_key':key, 'source_product_id':key},
           'creative_policy':{**cfg, 'creative_mode':'four_agent', 'auto_export':True,
                              'preparation_only':True, 'preparation_id':'prepare-'+key}}
    dossier = ResearchDossier.from_dict(_locked_research_dossier(workflow_id=workflow_id,
        aggregate=agg, locked_pack=pack, connection=coord.connection))
    model = FakeRoles()
    generator = AgenticCreativeGenerator(model, profile=BackendProfile.from_dict(cfg['profile']),
        category=cfg['category'], visible_details=cfg['visible_details'])
    brief = generator.create_direction(dossier)
    script = generator.create_script(dossier, brief)
    bundle = generator.create_scene_prompts(dossier, script)
    scene = {**script.to_dict(), 'creative_mode':'four_agent', 'creative_brief':brief.to_dict(),
             'research_dossier':dossier.to_dict(), 'scene_prompts':bundle.to_dict(),
             'backend_profile':generator.profile.to_dict(), 'project_id':project_id}
    agg.update(creative_brief=brief.to_dict(), scene_plan=scene, latest_artifact=scene)
    coord.workflow_repo.commit_workflow(workflow_id, json.dumps(agg), [], expected_version=0)
    return workflow_id


def test_publish_reuses_current_receipts_and_stops_before_generation(tmp_path, monkeypatch):
    conn = connect(tmp_path/'worker.db')
    coord = ProductVideoCoordinator(conn, db_path=tmp_path/'worker.db')
    ref = prepared_source(coord, tmp_path/'source')
    monkeypatch.setattr('video_factory.automation.coordinator.run_worker',
                        lambda **kwargs:pytest.fail('prepared publication must not run a worker'))
    first = coord.advance_preparation(ref)
    assert first['action'] == 'prepared'
    again = coord.advance_preparation(ref)
    assert first['prepared_digest'] == again['prepared_digest']
    assert conn.execute('SELECT COUNT(*) FROM workflow_outbox').fetchone()[0] == 0
    saved = coord.workflow_repo.get_workflow(ref)[0]['prepared_product']
    assert saved['source_qc_receipts'][0]['report_id'].startswith('fake-image-')
    assert saved['artifact_refs']['source_workflow_id'] == ref
    conn.close()


def test_selection_is_durable_and_excludes_stale_blocked_completed_and_claimed(tmp_path, monkeypatch):
    db = tmp_path/'worker.db'
    conn = connect(db)
    coord = ProductVideoCoordinator(conn, db_path=db)
    refs = [prepared_source(coord, tmp_path/str(i), str(i)) for i in range(5)]
    for ref in refs:
        coord.advance_preparation(ref)
    for ref, state in ((refs[1], 'WaitingForOperator'), (refs[2], 'completed')):
        agg, version = coord.workflow_repo.get_workflow(ref)
        agg['state'] = state
        coord.workflow_repo.commit_workflow(ref, json.dumps(agg), [], expected_version=version)
    agg, version = coord.workflow_repo.get_workflow(refs[3])
    agg['research_result']['raw_result']['snapshot_id'] = 'changed'
    coord.workflow_repo.commit_workflow(refs[3], json.dumps(agg), [], expected_version=version)
    monkeypatch.setattr(coord, 'start_fresh_creative', lambda **kwargs:pytest.fail('no PI acquisition'))
    result = coord.produce_from_catalog(run_id='batch', prepared_refs=refs, limit=3, run_config=config())
    assert [i['prepared_ref'] for i in result['selected']] == [refs[0], refs[4]]
    assert result['shortfall'] == 1
    assert len(result['skipped']) == 3
    assert conn.execute('SELECT COUNT(*) FROM workflow_outbox').fetchone()[0] == 0
    conn.close()
    conn = connect(db)
    coord = ProductVideoCoordinator(conn, db_path=db)
    assert coord.produce_from_catalog(run_id='batch', prepared_refs=refs, limit=3,
                                     run_config=config())['reused']
    other = coord.produce_from_catalog(run_id='other', prepared_refs=refs, limit=3, run_config=config())
    assert other['selected'] == []
    with pytest.raises(ValueError, match='different input'):
        coord.produce_from_catalog(run_id='batch', prepared_refs=list(reversed(refs)), limit=3, run_config=config())
    conn.close()


def test_same_production_run_can_append_a_preparation_resolved_after_first_claim(tmp_path):
    conn = connect(tmp_path/'worker.db')
    coord = ProductVideoCoordinator(conn, db_path=tmp_path/'worker.db')
    refs = [prepared_source(coord, tmp_path/str(i), str(i)) for i in range(2)]
    for ref in refs:
        coord.advance_preparation(ref)

    first = coord.produce_from_catalog(run_id='resumed-batch', prepared_refs=refs[:1],
                                       limit=None, run_config=config())
    assert [item['prepared_ref'] for item in first['selected']] == [refs[0]]

    resumed = coord.produce_from_catalog(run_id='resumed-batch', prepared_refs=refs,
                                         limit=None, run_config=config())

    assert [item['prepared_ref'] for item in resumed['selected']] == refs
    assert resumed['reused'] is False
    assert conn.execute("SELECT COUNT(*) FROM workflow WHERE workflow_id=?", (refs[0],)).fetchone()[0] == 1
    assert coord.workflow_repo.get_workflow(refs[0])[0]['production_run_id'] == 'pv-production-resumed-batch'
    assert coord.workflow_repo.get_workflow(refs[1])[0]['production_run_id'] == 'pv-production-resumed-batch'
    conn.close()


def test_profile_change_is_explicitly_stale_and_bytes_are_rechecked(tmp_path):
    conn = connect(tmp_path/'worker.db')
    coord = ProductVideoCoordinator(conn, db_path=tmp_path/'worker.db')
    ref = prepared_source(coord, tmp_path/'source')
    coord.advance_preparation(ref)
    cfg = config()
    cfg['profile']['version'] = 'different-backend-contract'
    cfg['profile'].pop('digest')
    result = coord.produce_from_catalog(run_id='changed', prepared_refs=[ref], limit=1, run_config=cfg)
    assert result['selected'] == [] and 'PROFILE' in result['skipped'][0]['reason']
    (tmp_path/'source'/'source-0.png').write_bytes(b'changed')
    result = coord.produce_from_catalog(run_id='bytes', prepared_refs=[ref], limit=1, run_config=config())
    assert result['selected'] == [] and result['shortfall'] == 1
    conn.close()


def test_catalog_selection_all_mode_selects_every_eligible_prepared_item(tmp_path):
    conn=connect(tmp_path/'worker.db')
    coord=ProductVideoCoordinator(conn,db_path=tmp_path/'worker.db')
    refs=[prepared_source(coord,tmp_path/str(i),str(i)) for i in range(5)]
    from types import SimpleNamespace
    from pathlib import Path
    coord.importer.find_latest_ready_batch=lambda target_date:Path(tmp_path/'inbox'/'2026-10-24'/'run_batch')
    coord.importer.load_manifest=lambda batch_dir:SimpleNamespace(batch_id='run_batch')
    for ref in refs:
        agg,version=coord.workflow_repo.get_workflow(ref)
        agg['source_context']['source_date']='2026-10-23' if ref=='prep-3' else '2026-10-24'
        agg['source_context']['source_batch_id']='run_other' if ref=='prep-4' else 'run_batch'
        coord.workflow_repo.commit_workflow(ref,json.dumps(agg),[],expected_version=version)
        coord.advance_preparation(ref)
    result=coord.produce_from_catalog(run_id='all',target_date='2026-10-24',limit=None,run_config=config())
    assert [item['prepared_ref'] for item in result['selected']]==refs[:3]
    assert result['requested']=='all' and result['shortfall']==0
    conn.close()


def test_selection_excludes_product_with_current_qc_export_across_runs(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import video_factory.creative.agents.video_review as video_review
    import video_factory.integrations.composer as composer
    conn=connect(tmp_path/'worker.db')
    coord=ProductVideoCoordinator(conn,db_path=tmp_path/'worker.db')
    refs=[prepared_source(coord,tmp_path/str(i),str(i)) for i in range(2)]
    for ref in refs:
        coord.advance_preparation(ref)
    final=tmp_path/'accepted.mp4'
    final.write_bytes(b'current accepted bytes')
    sha=hashlib.sha256(final.read_bytes()).hexdigest()
    agg,version=coord.workflow_repo.get_workflow(refs[0])
    agg.update(state='completed',render_result={'file_path':str(final),'sha256':sha},
        export_result={'payload':json.dumps({'file_path':str(final)})})
    coord.workflow_repo.commit_workflow(refs[0],json.dumps(agg),[],expected_version=version)
    monkeypatch.setattr(video_review,'require_final_pass',lambda artifact:None)
    monkeypatch.setattr(composer,'probe_video',lambda *args,**kwargs:SimpleNamespace(sha256=sha))
    result=coord.produce_from_catalog(run_id='new-run',prepared_refs=refs,limit=2,run_config=config())
    assert [item['prepared_ref'] for item in result['selected']]==[refs[1]]
    assert any(item['reason']=='CURRENT_ACCEPTED_FINAL_EXISTS' for item in result['skipped'])
    conn.close()


def test_selection_blocks_same_identity_variant_with_active_different_profile(tmp_path):
    conn=connect(tmp_path/'worker.db')
    coord=ProductVideoCoordinator(conn,db_path=tmp_path/'worker.db')
    refs=[prepared_source(coord,tmp_path/str(i),str(i)) for i in range(2)]
    for ref in refs:
        coord.advance_preparation(ref)
    active,version=coord.workflow_repo.get_workflow(refs[1])
    active['state']='rendering'
    active['source_context']['canonical_product_key']='0'
    active['creative_policy']['profile']['version']='different-profile'
    coord.workflow_repo.commit_workflow(refs[1],json.dumps(active),[],expected_version=version)
    result=coord.produce_from_catalog(run_id='active-duplicate',prepared_refs=[refs[0]],limit=1,run_config=config())
    assert result['selected']==[]
    assert any('ACTIVE_IDENTITY_VARIANT' in item['reason'] for item in result['skipped'])
    conn.close()


def test_production_refuses_explicit_refs_spanning_ready_batches(tmp_path):
    conn=connect(tmp_path/'worker.db')
    coord=ProductVideoCoordinator(conn,db_path=tmp_path/'worker.db')
    refs=[prepared_source(coord,tmp_path/str(i),str(i)) for i in range(2)]
    for index,ref in enumerate(refs):
        agg,version=coord.workflow_repo.get_workflow(ref)
        agg['source_context'].update(source_date='2026-10-24',source_batch_id=f'run_{index}')
        coord.workflow_repo.commit_workflow(ref,json.dumps(agg),[],expected_version=version)
        coord.advance_preparation(ref)
    with pytest.raises(ValueError,match='span multiple source batches'):
        coord.produce_from_catalog(run_id='mixed-batches',prepared_refs=refs,limit=2,run_config=config(),
            target_date='2026-10-24')
    conn.close()


def test_prepare_command_and_production_command_do_not_require_query():
    from video_factory.cli import build_parser
    parser = build_parser()
    prepare = parser.parse_args(['creative-prepare', '--source-workflow-id', 'selected',
                                 '--preparation-id', 'prep', '--profile', 'config.json'])
    produce = parser.parse_args(['creative-produce', '--run-id', 'batch', '--prepared-ref', 'selected',
                                 '--limit', '1', '--profile', 'config.json'])
    assert prepare.func.__name__ == 'cmd_creative_prepare'
    assert produce.func.__name__ == 'cmd_creative_produce'
    all_products=parser.parse_args(['creative-produce','--run-id','daily','--all','--profile','config.json'])
    assert all_products.limit is None and all_products.select_all is True
