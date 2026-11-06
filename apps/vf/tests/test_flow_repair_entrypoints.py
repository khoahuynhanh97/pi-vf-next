import json
import sqlite3
from types import SimpleNamespace

from video_factory.maintenance.events import observe_failure


def test_observer_records_only_ui_failure_and_never_model_or_generation(tmp_path):
    conn = sqlite3.connect(tmp_path / 'worker.db')
    claim = SimpleNamespace(workflow_id='wf', outbox_id=3, message=json.dumps({'payload': {'job_name': 'product_video.storyboard'}}))
    assert observe_failure(conn, claim, RuntimeError('QC_RESPONSE_INVALID'), 'quarantined') is None
    result = observe_failure(conn, claim, RuntimeError('GOOGLE_FLOW_SETTINGS_SELECTOR_NOT_UNIQUE'), 'quarantined')
    assert result['error_code'] == 'GOOGLE_FLOW_SETTINGS_SELECTOR_NOT_UNIQUE'
    assert result['details']['operation_phase'] == 'uncertain'
    assert result['attempts'] == []
    assert observe_failure(conn, claim, RuntimeError('GOOGLE_FLOW_SETTINGS_SELECTOR_NOT_UNIQUE'), 'quarantined')['incident_id'] == result['incident_id']


def test_cli_status_does_not_launch_browser(tmp_path, capsys):
    from video_factory.cli import main
    assert main(['--db-path', str(tmp_path / 'worker.db'), 'flow-repair', 'status']) == 0
    assert json.loads(capsys.readouterr().out)['incidents'] == []
