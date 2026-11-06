import json
from types import SimpleNamespace

import pytest

from video_factory.automation.coordinator import ProductVideoCoordinator
from video_factory.storage_infra.sqlite import connect


@pytest.mark.parametrize(('error','expected_action','expected_state'),[
    ('PREPRODUCTION_FAIL: repair quota exhausted','skipped','skipped'),
    ('PROVIDER_OUTCOME_UNKNOWN: reconcile operation','quarantined','creating_scene_plan'),
])
def test_quarantined_retry_only_terminally_skips_known_product_failure(
    tmp_path, monkeypatch, error, expected_action, expected_state
):
    db=tmp_path/'worker.db'
    connection=connect(db)
    coordinator=ProductVideoCoordinator(connection,db_path=db)
    coordinator.workflow_repo.commit_workflow('wf',json.dumps({
        'state':'creating_scene_plan','owner_user_id':'local-owner',
        'creative_policy':{'creative_mode':'four_agent'}}),[],
        outbox_message=json.dumps({'workflow_id':'wf','job_name':'product_video.scene_plan'}),expected_version=0)
    connection.execute("UPDATE workflow_outbox SET state='quarantined',attempt_count=1,max_attempts=5,last_error=?",(error,))
    connection.commit()
    monkeypatch.setattr('video_factory.automation.coordinator.run_worker',lambda **kwargs:SimpleNamespace(failed=1))

    result=coordinator.advance_creative('wf')
    aggregate=coordinator.workflow_repo.get_workflow('wf')[0]
    assert result['action']==expected_action
    assert aggregate['state']==expected_state
    if expected_action=='skipped':
        assert connection.execute("SELECT 1 FROM workflow_event WHERE workflow_id='wf' AND event_type='OperatorWorkflowSkipped'").fetchone()
    else:
        assert not connection.execute("SELECT 1 FROM workflow_event WHERE workflow_id='wf' AND event_type='OperatorWorkflowSkipped'").fetchone()
    connection.close()
