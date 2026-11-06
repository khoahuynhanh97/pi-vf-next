import json
from argparse import Namespace
import pytest

from video_factory.automation import creative_cli


def test_advance_until_continues_normal_worker_ticks_through_terminal_state():
    class Coordinator:
        results=iter([
            {'state':'creating_brief','action':'worker_tick'},
            {'state':'creating_scene_plan','action':'worker_tick'},
            {'state':'completed','action':'stop'},
        ])
        calls=0
        def advance(self, workflow_id):
            self.calls+=1
            return next(self.results)
    coordinator=Coordinator()
    result, complete=creative_cli._advance_until(coordinator,'wf',advance=coordinator.advance,
        success_action='completed',max_ticks=1)
    assert complete and result['state']=='completed'
    assert coordinator.calls==3


def test_advance_until_times_out_worker_ticks_without_checkpoint_progress(tmp_path, monkeypatch):
    import time
    from video_factory.storage_infra.sqlite import connect
    from video_factory.handlers import start_product_video_workflow
    from video_factory.workflows.repository import WorkflowRepository

    conn=connect(tmp_path/'worker-ticks.db')
    repo=WorkflowRepository(conn)
    start_product_video_workflow(repo, workflow_id='wf-stuck-ticks', product_name='Product',
        keywords=['product'], project_id='project')
    class Coordinator:
        connection=conn
        workflow_repo=repo
        calls=0
        def advance(self, workflow_id):
            self.calls+=1
            return {'workflow_id':workflow_id,'state':'creating_brief','action':'worker_tick'}
    coordinator=Coordinator()
    monkeypatch.setattr(creative_cli, 'CREATIVE_NO_PROGRESS_TIMEOUT_SECONDS', 0.02, raising=False)
    monkeypatch.setattr(creative_cli, '_wait_until', lambda _value:time.sleep(0.025))
    result, complete=creative_cli._advance_until(coordinator,'wf-stuck-ticks',advance=coordinator.advance,
        success_action='completed',max_ticks=25)
    assert not complete and result['action']=='unresolved'
    assert result['code']=='WORKFLOW_NO_PROGRESS_TIMEOUT'
    assert coordinator.calls >= 2
    conn.close()


def test_pending_without_durable_progress_times_out_and_persists_deadline(tmp_path, monkeypatch):
    import time
    from video_factory.storage_infra.sqlite import connect
    from video_factory.handlers import start_product_video_workflow
    from video_factory.workflows.repository import WorkflowRepository

    db = tmp_path / 'creative-wait.db'
    conn = connect(db)
    repo = WorkflowRepository(conn)
    start_product_video_workflow(repo, workflow_id='wf-stuck', product_name='Product',
        keywords=['product'], project_id='project')
    conn.execute('CREATE TABLE video_factory_scene_operation (workflow_id TEXT, operation_id TEXT, status TEXT)')
    conn.execute("INSERT INTO video_factory_scene_operation VALUES ('wf-stuck','op-stuck','running')")
    coordinator = type('Coordinator', (), {'connection':conn, 'workflow_repo':repo})()
    calls = []
    monkeypatch.setattr(creative_cli, 'CREATIVE_NO_PROGRESS_TIMEOUT_SECONDS', 0.02, raising=False)
    monkeypatch.setattr(creative_cli, '_wait_until', lambda _value:time.sleep(0.025))

    result, complete = creative_cli._advance_until(coordinator, 'wf-stuck',
        advance=lambda _wf:(calls.append(1) or {'workflow_id':'wf-stuck','state':'creating_brief','action':'pending'}),
        success_action='completed', max_ticks=999)

    assert not complete
    assert result['action'] == 'unresolved'
    assert result['code'] == 'WORKFLOW_NO_PROGRESS_TIMEOUT'
    assert result['workflow_id'] == 'wf-stuck'
    assert result['operation_ids'] == ['op-stuck']
    saved = repo.get_workflow('wf-stuck')[0]['creative_no_progress']
    from datetime import datetime
    assert (datetime.fromisoformat(saved['deadline_at']) - datetime.fromisoformat(saved['detected_at'])).total_seconds() == pytest.approx(0.02, abs=0.002)
    deadline = saved['deadline_at']
    result, complete = creative_cli._advance_until(coordinator, 'wf-stuck',
        advance=lambda _wf:(calls.append(1) or {'workflow_id':'wf-stuck','state':'creating_brief','action':'pending'}),
        success_action='completed', max_ticks=999)
    assert not complete and result['code'] == 'WORKFLOW_NO_PROGRESS_TIMEOUT'
    assert repo.get_workflow('wf-stuck')[0]['creative_no_progress']['deadline_at'] == deadline
    conn.close()


def test_no_progress_snapshot_tracks_scene_operations_by_project_id(tmp_path, monkeypatch):
    import time
    from video_factory.storage_infra.sqlite import connect
    from video_factory.handlers import start_product_video_workflow
    from video_factory.integrations.scene_renderer import SceneRenderOperationStore
    from video_factory.workflows.repository import WorkflowRepository

    conn = connect(tmp_path / 'scene-project-scope.db')
    repo = WorkflowRepository(conn)
    start_product_video_workflow(repo, workflow_id='wf-project-scope', product_name='Product',
        keywords=['product'], project_id='project-scope')
    SceneRenderOperationStore(conn)
    conn.execute(
        """INSERT INTO video_factory_scene_operation
           (operation_id, owner_user_id, project_id, scene_id, request_digest, status)
           VALUES ('op-scene', 'local-owner', 'project-scope', 'scene-1', 'digest', 'submitted')"""
    )
    coordinator = type('Coordinator', (), {'connection': conn, 'workflow_repo': repo})()
    monkeypatch.setattr(creative_cli, 'CREATIVE_NO_PROGRESS_TIMEOUT_SECONDS', 0.02, raising=False)
    monkeypatch.setattr(creative_cli, '_wait_until', lambda _value: time.sleep(0.025))

    result, complete = creative_cli._advance_until(
        coordinator, 'wf-project-scope',
        advance=lambda workflow_id: {
            'workflow_id': workflow_id, 'state': 'rendering', 'action': 'pending'
        },
        success_action='completed', max_ticks=1,
    )

    assert not complete
    assert result['code'] == 'WORKFLOW_NO_PROGRESS_TIMEOUT'
    assert result['operation_ids'] == ['op-scene']
    conn.close()


def test_durable_operation_progress_prevents_false_timeout(tmp_path, monkeypatch):
    from video_factory.storage_infra.sqlite import connect
    from video_factory.handlers import start_product_video_workflow
    from video_factory.workflows.repository import WorkflowRepository

    conn = connect(tmp_path / 'creative-progress.db')
    repo = WorkflowRepository(conn)
    start_product_video_workflow(repo, workflow_id='wf-progress', product_name='Product',
        keywords=['product'], project_id='project')
    conn.execute('CREATE TABLE video_factory_scene_operation (workflow_id TEXT, operation_id TEXT, status TEXT)')
    conn.execute("INSERT INTO video_factory_scene_operation VALUES ('wf-progress','op-progress','submitted')")
    coordinator = type('Coordinator', (), {'connection':conn, 'workflow_repo':repo})()
    calls = []
    monkeypatch.setattr(creative_cli, 'CREATIVE_NO_PROGRESS_TIMEOUT_SECONDS', 3600, raising=False)
    def advance(_wf):
        calls.append(len(calls)+1)
        if len(calls) == 2:
            conn.execute("UPDATE video_factory_scene_operation SET status='running' WHERE operation_id='op-progress'")
        if len(calls) == 3:
            return {'workflow_id':'wf-progress','state':'completed','action':'completed'}
        return {'workflow_id':'wf-progress','state':'creating_brief','action':'pending'}
    monkeypatch.setattr(creative_cli, '_wait_until', lambda _value:None)

    result, complete = creative_cli._advance_until(coordinator, 'wf-progress', advance=advance,
        success_action='completed', max_ticks=1)

    assert complete and result['state'] == 'completed'
    assert calls == [1,2,3]
    conn.close()


def test_outbox_defer_counters_do_not_reset_no_progress_deadline(tmp_path):
    from video_factory.storage_infra.sqlite import connect
    from video_factory.handlers import start_product_video_workflow
    from video_factory.workflows.repository import WorkflowRepository

    conn = connect(tmp_path / "counter-only-progress.db")
    repo = WorkflowRepository(conn)
    start_product_video_workflow(repo, workflow_id="wf-counter-only", product_name="Product",
        keywords=["product"], project_id="project")

    class Coordinator:
        connection = conn
        workflow_repo = repo

    coordinator = Coordinator()
    from video_factory.automation.creative_cli import _durable_progress_snapshot
    before, _, _ = _durable_progress_snapshot(coordinator, "wf-counter-only")
    conn.execute("UPDATE workflow_outbox SET attempt_count=attempt_count+1, last_error='deferred again' WHERE workflow_id=?",
        ("wf-counter-only",))
    after, _, _ = _durable_progress_snapshot(coordinator, "wf-counter-only")

    assert after == before
    conn.close()


def test_advance_until_treats_terminal_skip_as_resolved():
    result, complete=creative_cli._advance_until(None,'wf',
        advance=lambda workflow_id:{'state':'skipped','action':'skipped'},
        success_action='prepared',max_ticks=1)
    assert complete and result['state']=='skipped'


def test_creative_run_stops_after_quarantined_job(monkeypatch):
    class Connection:
        def close(self): pass
    class Coordinator:
        calls=0
        def start_fresh_creative(self, **kwargs): return {'workflow_id':'wf'}
        def advance_creative(self, workflow_id):
            self.calls+=1
            return {'workflow_id':workflow_id,'state':'building_resource_pack','action':'quarantined'}
    coordinator=Coordinator()
    monkeypatch.setattr(creative_cli,'_catalog_coordinator',lambda args:(Connection(),coordinator,{}))
    args=Namespace(profile='profile.json',db_path=None,inbox_root=None,run_id='run',query='Anker A1290',
        owner_user_id='local-owner',max_ticks=100)

    result=creative_cli.cmd_creative_run(args)

    assert result==2
    assert coordinator.calls==1


def test_creative_prepare_waits_for_scheduled_retry_before_next_worker_tick(monkeypatch):
    retry_at='2026-10-24T04:00:00+00:00'
    waits=[]
    class Connection:
        def close(self): pass
    class Coordinator:
        calls=0
        def prepare_catalog(self, **kwargs):
            return {'workflow_id':'product-1','action':'prepare'}
        def advance_preparation(self, workflow_id):
            self.calls+=1
            return ({'state':'building_scene_plan','action':'retry_scheduled','next_attempt_at':retry_at,'failed':1}
                    if self.calls==1 else {'state':'AwaitingSceneApproval','action':'prepared'})
    coordinator=Coordinator()
    monkeypatch.setattr(creative_cli,'_catalog_coordinator',lambda args:(Connection(),coordinator,{}))
    monkeypatch.setattr(creative_cli,'_wait_until',waits.append,raising=False)
    args=Namespace(profile='profile.json',db_path=None,inbox_root=None,preparation_id='prep',product_key='sku',
        source_workflow_id=None,date=None,owner_user_id='local-owner',max_ticks=2)
    assert creative_cli.cmd_creative_prepare(args)==0
    assert waits==[retry_at]
    assert coordinator.calls==2


def test_creative_produce_waits_for_scheduled_retry_before_next_worker_tick(monkeypatch):
    retry_at='2026-10-24T04:00:00+00:00'
    waits=[]
    class Connection:
        def close(self): pass
    class Coordinator:
        calls=0
        def produce_from_catalog(self, **kwargs):
            return {'selected':[{'workflow_id':'product-1'}],'shortfall':0}
        def advance_creative(self, workflow_id):
            self.calls+=1
            return ({'state':'building_scene_plan','action':'retry_scheduled','next_attempt_at':retry_at,'failed':1}
                    if self.calls==1 else {'state':'completed','action':'worker_tick'})
    coordinator=Coordinator()
    monkeypatch.setattr(creative_cli,'_catalog_coordinator',lambda args:(Connection(),coordinator,{}))
    monkeypatch.setattr(creative_cli,'_wait_until',waits.append,raising=False)
    args=Namespace(profile='profile.json',db_path=None,inbox_root=None,run_id='run',prepared_ref=[],limit=1,
        owner_user_id='local-owner',max_ticks=2)
    assert creative_cli.cmd_creative_produce(args)==0
    assert waits==[retry_at]
    assert coordinator.calls==2


def test_creative_produce_persists_product_skip_and_continues_batch(monkeypatch):
    class Connection:
        def close(self): pass
    class Coordinator:
        visited=[]
        def produce_from_catalog(self, **kwargs):
            return {'selected':[{'workflow_id':'product-1'},{'workflow_id':'product-2'}], 'shortfall':0}
        def advance_creative(self, workflow_id):
            self.visited.append(workflow_id)
            if workflow_id == 'product-1':
                return {'workflow_id':workflow_id,'state':'skipped','action':'skipped','reason':'known QC failure'}
            return {'workflow_id':workflow_id,'state':'completed','action':'worker_tick'}
    coordinator=Coordinator()
    monkeypatch.setattr(creative_cli,'_catalog_coordinator',lambda args:(Connection(),coordinator,{}))
    args=Namespace(profile='profile.json',db_path=None,inbox_root=None,run_id='run',prepared_ref=[],limit=None,
        owner_user_id='local-owner',max_ticks=2)
    assert creative_cli.cmd_creative_produce(args)==0
    assert coordinator.visited==['product-1','product-2']


def test_creative_produce_does_not_report_success_when_every_product_is_skipped(monkeypatch):
    class Connection:
        def close(self): pass
    class Coordinator:
        def produce_from_catalog(self, **kwargs):
            return {'selected':[{'workflow_id':'product-1'},{'workflow_id':'product-2'}], 'shortfall':0}
        def advance_creative(self, workflow_id):
            return {'workflow_id':workflow_id,'state':'skipped','action':'skipped','reason':'known product failure'}
    coordinator=Coordinator()
    monkeypatch.setattr(creative_cli,'_catalog_coordinator',lambda args:(Connection(),coordinator,{}))
    args=Namespace(profile='profile.json',db_path=None,inbox_root=None,run_id='all-skipped',prepared_ref=[],limit=None,
        owner_user_id='local-owner',max_ticks=2)

    assert creative_cli.cmd_creative_produce(args)==2


def test_creative_produce_records_blocker_and_continues_same_pinned_selection(monkeypatch):
    class Connection:
        def close(self): pass
    class Coordinator:
        visited=[]
        def produce_from_catalog(self, **kwargs):
            return {'selected':[{'workflow_id':'product-1'},{'workflow_id':'product-2'}], 'shortfall':0}
        def advance_creative(self, workflow_id):
            self.visited.append(workflow_id)
            if workflow_id == 'product-1':
                return {'workflow_id':workflow_id,'state':'AwaitingSceneApproval','action':'blocker',
                        'code':'KEYFRAME_CONTENT_UNKNOWN','reason':'scene_02-start requires reconciliation'}
            return {'workflow_id':workflow_id,'state':'completed','action':'completed'}
    coordinator=Coordinator()
    monkeypatch.setattr(creative_cli,'_catalog_coordinator',lambda args:(Connection(),coordinator,{}))
    args=Namespace(profile='profile.json',db_path=None,inbox_root=None,run_id='run',prepared_ref=[],limit=None,
        owner_user_id='local-owner',max_ticks=2)

    result=creative_cli.cmd_creative_produce(args)

    assert result==2
    assert coordinator.visited==['product-1','product-2']


def test_creative_batch_continues_after_unresolved_product(monkeypatch):
    class Connection:
        def close(self): pass
    class Coordinator:
        visited=[]
        def produce_from_catalog(self, **kwargs):
            return {'selected':[{'workflow_id':'product-1'},{'workflow_id':'product-2'}], 'shortfall':0}
        def advance_creative(self, workflow_id):
            self.visited.append(workflow_id)
            if workflow_id == 'product-1':
                return {'workflow_id':workflow_id,'state':'rendering','action':'unresolved',
                    'code':'WORKFLOW_NO_PROGRESS_TIMEOUT','operation_ids':['op-1'],
                    'reason':'workflow stalled while operation remains active'}
            return {'workflow_id':workflow_id,'state':'completed','action':'completed'}
    coordinator=Coordinator()
    monkeypatch.setattr(creative_cli,'_catalog_coordinator',lambda args:(Connection(),coordinator,{}))
    args=Namespace(profile='profile.json',db_path=None,inbox_root=None,run_id='run',prepared_ref=[],limit=None,
        owner_user_id='local-owner',max_ticks=2)

    result=creative_cli.cmd_creative_produce(args)

    assert result==2
    assert coordinator.visited==['product-1','product-2']


def test_shared_progression_retries_received_creative_failure_and_skips_exhaustion(monkeypatch):
    from video_factory.application import operator_handler
    from video_factory.workflows import runtime_control

    class Connection:
        def __init__(self, outbox=None): self.outbox=outbox
        def execute(self, sql, params):
            if 'workflow_event' in sql:
                return []
            return type('Cursor',(),{'fetchone':lambda _self:self.outbox})()
    class Repo:
        def __init__(self, aggregate): self.aggregate=aggregate
        def get_workflow(self, workflow_id): return self.aggregate, 1

    aggregate={'state':'WaitingForOperator','failure_code':'CREATIVE_RESPONSE_INVALID',
        'operator_action':'creative_review','operator_stage':'script','owner_user_id':'owner',
        'operator_reason':'CREATIVE_RESPONSE_INVALID: bad schema'}
    retries=[]
    monkeypatch.setattr(operator_handler,'recover_creative_stage',
        lambda connection, **kwargs: retries.append(kwargs))
    coordinator=type('Coordinator',(),{})()
    coordinator.connection=Connection()
    coordinator.workflow_repo=Repo(aggregate)
    advances=iter([{'state':'WaitingForOperator','action':'stop'},
                   {'state':'completed','action':'completed'}])
    result, complete=creative_cli._advance_until(coordinator,'wf',advance=lambda _wf:next(advances),
        success_action='completed',max_ticks=2)
    assert complete and result['state']=='completed'
    assert len(retries)==1 and retries[0]['workflow_id']=='wf'

    coordinator.connection=Connection(outbox=(5,5))
    monkeypatch.setattr(runtime_control,'skip_unresolved_workflow',
        lambda connection, workflow_id, reason, **kwargs: retries.append({'skip':workflow_id,'reason':reason}))
    result, complete=creative_cli._advance_until(coordinator,'wf',
        advance=lambda _wf:{'state':'WaitingForOperator','action':'stop'},
        success_action='prepared',max_ticks=1)
    assert complete and result['state']=='skipped'
    assert retries[-1]['skip']=='wf'


def test_received_creative_failure_uses_one_initial_plus_two_retries(monkeypatch):
    from video_factory.application import operator_handler
    from video_factory.workflows import runtime_control

    class Connection:
        def __init__(self): self.recovery_events=[]
        def execute(self, sql, params):
            if 'workflow_event' in sql:
                return [(json.dumps({'stage':'script'}),) for _ in self.recovery_events]
            return type('Cursor',(),{'fetchone':lambda _self:None})()
    class Repo:
        def get_workflow(self, workflow_id):
            return ({'state':'WaitingForOperator','failure_code':'CREATIVE_RESPONSE_INVALID',
                'operator_action':'creative_review','operator_stage':'script'},1)

    connection=Connection()
    recovered=[]
    def recover(_connection, **kwargs):
        recovered.append(kwargs)
        connection.recovery_events.append(kwargs)
    monkeypatch.setattr(operator_handler,'recover_creative_stage',recover)
    skipped=[]
    monkeypatch.setattr(runtime_control,'skip_unresolved_workflow',
        lambda _connection, workflow_id, reason, **kwargs: skipped.append(workflow_id))
    coordinator=type('Coordinator',(),{})()
    coordinator.connection=connection
    coordinator.workflow_repo=Repo()

    result, complete=creative_cli._advance_until(coordinator,'wf',
        advance=lambda _wf:{'state':'WaitingForOperator','action':'stop'},
        success_action='prepared',max_ticks=3)

    assert complete and result['state']=='skipped'
    assert len(recovered)==2
    assert skipped==['wf']


def test_schema_invalid_exhaustion_uses_short_skip_reason_and_keeps_evidence(monkeypatch):
    from video_factory.workflows import runtime_control

    long_error = ('CREATIVE_RESPONSE_INVALID: DIRECTOR_REQUIRED_FIELD_MISSING: scene 1: subject_action; raw: '
                  + ('invalid response detail ' * 80))
    class Connection:
        def execute(self, sql, params):
            if 'workflow_event' in sql:
                return []
            return type('Cursor',(),{'fetchone':lambda _self:(5,5)})()
    class Repository:
        def get_workflow(self, workflow_id):
            return ({'state':'WaitingForOperator','failure_code':'CREATIVE_RESPONSE_INVALID',
                'operator_action':'creative_review','operator_stage':'script','operator_reason':long_error},1)
    saved = []
    monkeypatch.setattr(runtime_control, 'skip_unresolved_workflow',
        lambda conn, workflow_id, reason, **kwargs: saved.append(reason))
    coordinator=type('Coordinator',(),{'connection':Connection(),'workflow_repo':Repository()})()

    result, complete=creative_cli._advance_until(coordinator,'wf',
        advance=lambda _wf:{'state':'WaitingForOperator','action':'stop'},
        success_action='prepared',max_ticks=1)

    assert complete and result['state']=='skipped'
    assert saved[0].startswith('CREATIVE_RESPONSE_INVALID:')
    assert 'DIRECTOR_REQUIRED_FIELD_MISSING: scene 1: subject_action' in saved[0]
    assert len(saved[0]) <= 500
    assert result['evidence'] == long_error


def test_recovered_job_gate_progress_is_not_shadowed_by_old_quarantine(tmp_path, monkeypatch):
    from video_factory.automation import coordinator as module
    from video_factory.storage_infra.sqlite import connect
    from video_factory.workflows.repository import WorkflowRepository
    from video_factory.workflows.envelope import build_envelope
    from types import SimpleNamespace
    conn = connect(tmp_path / "gate.db")
    repo = WorkflowRepository(conn)
    agg = {"state": "creating_storyboard", "creative_policy": {"creative_mode": "four_agent"}}
    repo.commit_workflow("wf", json.dumps(agg), [], build_envelope(workflow_id="wf", topic="job.dispatch",
        idempotency_key="failed", payload={"job_name":"product_video.storyboard"}), expected_version=0)
    conn.execute("UPDATE workflow_outbox SET state='quarantined',last_error='old auth failure'")
    conn.commit()
    def worker(**kwargs):
        old, version = repo.get_workflow("wf")
        repo.commit_workflow("wf", json.dumps({**old,"state":"AwaitingStoryboardApproval"}), [], expected_version=version)
        return SimpleNamespace(failed=0)
    monkeypatch.setattr(module, "run_worker", worker)
    result = module.ProductVideoCoordinator(conn, db_path=tmp_path / "gate.db").advance_creative("wf")
    assert result["action"] == "worker_tick"
    assert result["state"] == "AwaitingStoryboardApproval"
    assert conn.execute("SELECT last_error FROM workflow_outbox").fetchone()[0] == "old auth failure"
    conn.close()


def test_batch_outcome_uses_durable_product_key_after_recovery(tmp_path, capsys):
    from types import SimpleNamespace
    final = tmp_path / "final.mp4"
    final.write_bytes(b"already-verified-export")
    class Repo:
        def get_workflow(self, ref):
            return ({"state":"completed", "product_key":"key", "source_context":{},
                "export_result":{"file_path":str(final)}}, 1)
    coordinator = SimpleNamespace(workflow_repo=Repo())
    manifest = SimpleNamespace(products=[SimpleNamespace(product_key="key",name="Product")])
    result = creative_cli._print_batch_outcomes(coordinator, manifest, ["key"], {}, [],
        [{"workflow_id":"wf", "reason":"historical failure"}])
    assert result == {"completed":1,"blocked":0,"technical_errors":0}
    assert str(final) in capsys.readouterr().out
