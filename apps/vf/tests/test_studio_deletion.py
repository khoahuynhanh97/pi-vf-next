import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_factory.application.app import create_application
from video_factory.application.studio_deletion import delete_workflows
from video_factory.integrations.keyframe_provider import KeyframeOperationStore
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.repository import WorkflowRepository


@pytest.fixture
def data(tmp_path):
    conn=connect(tmp_path/'db.sqlite')
    root=tmp_path/'workspace'; root.mkdir()
    source=tmp_path/'pi.jpg'; source.write_bytes(b'pi-source')
    for wid in ('one','two'):
        output=root/f'{wid}.mp4'; output.write_bytes(wid.encode())
        WorkflowRepository(conn).commit_workflow(wid,json.dumps({'owner_user_id':'local-owner','project_id':f'proj-{wid}',
            'state':'completed','source_context':{'images':[{'absolute_path':str(source)}]},
            'export_result':{'file_path':str(output)}, 'storyboard':{'research_dossier':{'image_path':str(source)}}}),[],expected_version=0)
    yield conn,root,source
    conn.close()


def selected(*ids):
    return [{'workflow_id':wid,'expected_version':1} for wid in ids]


def test_permanent_delete_removes_selected_files_and_workflow_only(data):
    conn,root,source=data
    result=delete_workflows(conn,selected('one'),workspace=root)
    assert result[0]['status']=='deleted'
    assert not (root/'one.mp4').exists()
    assert (root/'two.mp4').exists() and source.exists()
    assert WorkflowRepository(conn).get_workflow('one') is None
    assert WorkflowRepository(conn).get_workflow('two') is not None
    assert delete_workflows(conn,selected('one'),workspace=root)[0]['status']=='already_deleted'


@pytest.mark.parametrize('blocker',['pending','leased','unknown','stale','owner','escape','shared','pause'])
def test_deletion_guards_preserve_evidence_and_independent_item(data,blocker,tmp_path):
    conn,root,source=data
    agg,version=WorkflowRepository(conn).get_workflow('one')
    if blocker in {'pending','leased'}:
        conn.execute("INSERT INTO workflow_outbox(workflow_id,message,state,created_at) VALUES('one','{}',?,CURRENT_TIMESTAMP)",(blocker,));conn.commit()
    elif blocker=='unknown':
        KeyframeOperationStore(conn)
        conn.execute("INSERT INTO video_factory_keyframe_operation(operation_id,workflow_id,project_id,frame_id,request_digest,status) VALUES('op','one','proj-one','s1','digest','unknown')");conn.commit()
    elif blocker=='owner':
        agg['owner_user_id']='someone-else'
    elif blocker=='escape':
        agg['export_result']['file_path']=str(source)
    elif blocker=='shared':
        other,v=WorkflowRepository(conn).get_workflow('two')
        other['shared_media']={'file_path':str(root/'one.mp4')}
        WorkflowRepository(conn).commit_workflow('two',json.dumps(other),[],expected_version=v)
    elif blocker=='pause':
        from video_factory.workflows.runtime_control import pause_pipeline
        from video_factory.automation.failure_classification import FailureCategory
        pause_pipeline(conn,category=FailureCategory.QC_INFRA_PAUSED,code='QC_OUTCOME_UNCERTAIN',message='Inspect',workflow_id='one')
    if blocker in {'owner','escape'}:
        WorkflowRepository(conn).commit_workflow('one',json.dumps(agg),[],expected_version=version)
    items=selected('one')
    if blocker in {'owner','escape'}: items[0]['expected_version']=2
    if blocker=='stale': items[0]['expected_version']=99
    result=delete_workflows(conn,items,workspace=root)
    if blocker=='shared':
        assert result[0]['status']=='deleted' and result[0]['shared_files_preserved']
    else:
        assert result[0]['status']=='blocked'
        assert WorkflowRepository(conn).get_workflow('one') is not None
    assert (root/'one.mp4').exists() and (root/'two.mp4').exists() and source.exists()


def test_one_blocked_item_does_not_prevent_other_deletion(data):
    conn,root,_=data
    conn.execute("INSERT INTO workflow_outbox(workflow_id,message,state,created_at) VALUES('one','{}','pending',CURRENT_TIMESTAMP)");conn.commit()
    result=delete_workflows(conn,selected('one','two'),workspace=root)
    assert [r['status'] for r in result]==['blocked','deleted']
    assert (root/'one.mp4').exists() and not (root/'two.mp4').exists()


def test_integration_db_unknown_operation_is_checked(data,tmp_path):
    conn,root,_=data
    ledger=connect(tmp_path/'operations.sqlite')
    KeyframeOperationStore(ledger)
    ledger.execute("INSERT INTO video_factory_keyframe_operation(operation_id,workflow_id,project_id,frame_id,request_digest,status) VALUES('op','one','proj-one','s1','digest','submitting')");ledger.commit();ledger.close()
    result=delete_workflows(conn,selected('one'),workspace=root,integration_db=tmp_path/'operations.sqlite')
    assert result[0]['status']=='blocked' and (root/'one.mp4').exists()


def test_terminal_generation_ledger_is_not_reset_when_files_are_deleted(data):
    conn,root,_=data
    KeyframeOperationStore(conn)
    conn.execute("INSERT INTO video_factory_keyframe_operation(operation_id,workflow_id,project_id,frame_id,request_digest,status,output_path) VALUES('op','one','proj-one','s1','digest','completed',?)",(str(root/'one.mp4'),));conn.commit()
    result=delete_workflows(conn,selected('one'),workspace=root)
    assert result[0]['status']=='deleted'
    assert conn.execute("SELECT status FROM video_factory_keyframe_operation WHERE operation_id='op'").fetchone()[0]=='completed'
    assert not (root/'one.mp4').exists()


def test_file_failure_is_reported_without_claiming_workflow_deleted(data,monkeypatch):
    conn,root,_=data
    original=Path.unlink
    def deny_one(path,*args,**kwargs):
        if path==root/'one.mp4':
            raise PermissionError('File đang được mở')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'unlink',deny_one)
    result=delete_workflows(conn,selected('one'),workspace=root)
    assert result[0]['status']=='blocked' and 'File đang được mở' in result[0]['reason']
    assert WorkflowRepository(conn).get_workflow('one') is not None
    assert (root/'one.mp4').exists()


@pytest.mark.parametrize('project',['proj-one','proj-two'])
def test_old_workspace_export_is_bound_to_exact_project(data,tmp_path,project):
    conn,root,source=data
    old=tmp_path/'old-home'/'workspaces'/'video-factory'/'exports'/f'export_{project}_1'/'final.mp4'
    old.parent.mkdir(parents=True);old.write_bytes(b'old-export')
    agg,version=WorkflowRepository(conn).get_workflow('one')
    agg['export_result']['file_path']=str(old)
    WorkflowRepository(conn).commit_workflow('one',json.dumps(agg),[],expected_version=version)
    result=delete_workflows(conn,[{'workflow_id':'one','expected_version':2}],workspace=root)
    assert result[0]['status']==('deleted' if project=='proj-one' else 'blocked')
    assert old.exists() is (project!='proj-one') and source.exists()


def test_api_requires_explicit_confirmation_origin_and_uses_existing_handler(data,monkeypatch):
    conn,root,_=data
    from video_factory.integrations import server
    monkeypatch.setattr(server,'_workspace_path',lambda:root)
    monkeypatch.setattr(server,'_db_path',lambda:root/'missing.sqlite')
    with TestClient(create_application(kanban_connection=conn)) as client:
        body={'workflows':selected('one'),'confirmation':'XÓA VĨNH VIỄN'}
        url='/api/operator/workflows/delete'
        assert client.post(url,json=body).status_code==403
        assert client.post(url,json={**body,'confirmation':''},headers={'origin':'http://testserver'}).status_code==400
        assert (root/'one.mp4').exists()
        response=client.post(url,json=body,headers={'origin':'http://testserver'})
        assert response.status_code==200,response.text
        assert response.json()['results'][0]['status']=='deleted'
