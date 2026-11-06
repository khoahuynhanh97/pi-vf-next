import json
import pytest
from test_agent_contracts import profile
from video_factory.storage_infra.sqlite import connect
from video_factory.automation.coordinator import ProductVideoCoordinator


def config():
    return {'profile':profile().to_dict(),'category':'gadget_demo','visible_details':['visible_controls'],
            'max_products':1,'max_scenes':4,'image_call_limit':24,'video_call_limit':12}


def test_duplicate_start_and_restart_create_only_one_acquisition_outbox(tmp_path):
    db=tmp_path/'worker.db'
    conn=connect(db)
    coord=ProductVideoCoordinator(conn,db_path=db)
    first=coord.start_fresh_creative(run_id='test1',query='Synthetic product test only',run_config=config())
    count=conn.execute('SELECT COUNT(*) FROM workflow_outbox').fetchone()[0]
    conn.close()
    conn=connect(db)
    coord=ProductVideoCoordinator(conn,db_path=db)
    second=coord.start_fresh_creative(run_id='test1',query='Synthetic product test only',run_config=config())
    assert first['workflow_id']==second['workflow_id'] and second['reused']
    assert conn.execute('SELECT COUNT(*) FROM workflow_outbox').fetchone()[0]==count==1
    with pytest.raises(ValueError,match='different input'):
        coord.start_fresh_creative(run_id='test1',query='Changed query',run_config=config())
    conn.close()


def test_completed_rerun_does_not_call_worker_or_provider(tmp_path,monkeypatch):
    conn=connect(tmp_path/'worker.db')
    coord=ProductVideoCoordinator(conn,db_path=tmp_path/'worker.db')
    result=coord.start_fresh_creative(run_id='done',query='Synthetic complete',run_config=config())
    data,version=coord.workflow_repo.get_workflow(result['workflow_id'])
    data['state']='completed'
    coord.workflow_repo.commit_workflow(result['workflow_id'],json.dumps(data),[],expected_version=version)
    monkeypatch.setattr('video_factory.automation.coordinator.run_worker',lambda **kwargs:pytest.fail('must not run worker'))
    assert coord.advance_creative(result['workflow_id'],provider_mode='fake')=={'state':'completed','action':'stop'}
    assert coord.start_fresh_creative(run_id='done',query='Synthetic complete',run_config=config())['reused']
    conn.close()
