import threading

from video_factory.storage_infra.sqlite import connect
from video_factory.workflows import worker
from video_factory.workflows.outbox_dispatcher import DispatchReport
from video_factory.workflows.repository import WorkflowRepository


def _workflows(conn):
    repo = WorkflowRepository(conn)
    for workflow_id in ("wf-A", "wf-B"):
        repo.commit_workflow(workflow_id, '{"state":"running"}', [], expected_version=0)


def test_item_pause_scope_excludes_only_paused_workflow(tmp_path):
    conn = connect(tmp_path / "item-pause.db")
    _workflows(conn)
    pause = {"code": "PRODUCT_REJECTED", "workflow_id": "wf-A"}

    assert worker._pause_dispatch_scope(conn, ["wf-A", "wf-B"], None, pause) == ["wf-B"]
    assert worker._pause_dispatch_scope(conn, None, None, pause) == ["wf-B"]
    conn.close()


def test_capacity_pause_keeps_active_operations_dispatchable_for_unfiltered_and_mixed_workers(tmp_path):
    conn = connect(tmp_path / "capacity-scope.db")
    _workflows(conn)
    conn.execute("CREATE TABLE video_factory_scene_operation (workflow_id TEXT, operation_id TEXT, status TEXT)")
    conn.execute("INSERT INTO video_factory_scene_operation VALUES ('wf-A','op-active','running')")
    pause = {"code": "PROVIDER_CAPACITY_WAIT", "workflow_id": "wf-B"}

    assert worker._should_pause_worker(pause, None, connection=conn) is False
    assert worker._should_pause_worker(pause, ["wf-A", "wf-B"], connection=conn) is False
    assert worker._capacity_dispatch_scope(conn, None, None) == ["wf-A"]
    assert worker._capacity_dispatch_scope(conn, ["wf-A", "wf-B"], None) == ["wf-A"]
    conn.close()


def test_background_worker_passes_explicit_workflow_filter_to_dispatcher(tmp_path, monkeypatch):
    database = tmp_path / "background-filter.db"
    conn = connect(database)
    _workflows(conn)
    conn.close()
    stopped = threading.Event()
    calls = []

    class Dispatcher:
        def __init__(self, **kwargs): pass
        def reclaim_expired(self): pass
        def process_batch(self, **kwargs):
            calls.append(kwargs)
            stopped.set()
            return DispatchReport(claimed=0, succeeded=0, failed=0, quarantined=0)

    monkeypatch.setattr(worker, "OutboxDispatcher", Dispatcher)
    monkeypatch.setattr(worker, "_tick_ui_maintenance", lambda _connection: None)

    worker.run_worker(database, poll_interval=0, stop_event=stopped, topic_handlers={}, delivery_fn=lambda *_: None,
        provider_mode="fake", once=False, workflow_ids=["wf-B"], workflow_id_prefix="wf-")

    assert len(calls) == 1
    assert calls[0]["workflow_ids"] == ["wf-B"]
    assert calls[0]["workflow_id_prefix"] is None
