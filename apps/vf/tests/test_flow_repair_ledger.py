import sqlite3

import pytest

from video_factory.maintenance.ledger import RepairLedger
from video_factory.maintenance.policy import is_ui_mismatch, resume_decision


def test_incident_and_uncertain_attempt_reuse_across_restart(tmp_path):
    path = tmp_path / 'worker.db'
    conn = sqlite3.connect(path)
    ledger = RepairLedger(conn)
    kwargs = dict(workflow_id='wf', operation_id='op', profile_id='profile',
                  error_code='GOOGLE_FLOW_SETTINGS_SELECTOR_NOT_UNIQUE', fingerprint='ui')
    first = ledger.incident(**kwargs)
    assert ledger.incident(**kwargs)['incident_id'] == first['incident_id']
    assert ledger.acquire(first['incident_id'], 'owner', 60)
    assert not ledger.acquire(first['incident_id'], 'other', 60)
    n = ledger.reserve_attempt(first['incident_id'], 2, authorization_id='grant', owner='owner')
    ledger.finish_attempt(first['incident_id'], n, 'uncertain', {'attempt_id': 'model-1'})
    conn.close()
    reopened = RepairLedger(sqlite3.connect(path))
    with pytest.raises(ValueError, match='uncertain'):
        reopened.reserve_attempt(first['incident_id'], 2, authorization_id='grant', owner='owner')
    assert reopened.get(first['incident_id'])['attempts'][0]['receipt']['attempt_id'] == 'model-1'


def test_shared_profile_lock_and_grant_cap_cross_incidents():
    ledger = RepairLedger(sqlite3.connect(':memory:'))
    def incident(op):
        return ledger.incident(workflow_id='wf', operation_id=op, profile_id='profile',
            error_code='GOOGLE_FLOW_UI_UNVERIFIED', fingerprint=op)['incident_id']
    first, second = incident('one'), incident('two')
    assert ledger.acquire(first, 'owner', 60)
    assert not ledger.acquire(second, 'other', 60)
    for i in range(2):
        n = ledger.reserve_attempt(first, 2, authorization_id='grant', owner='owner')
        ledger.finish_attempt(first, n, 'invalid', {})
    ledger.release(first, 'owner')
    assert ledger.acquire(second, 'other', 60)
    with pytest.raises(ValueError, match='limit'):
        ledger.reserve_attempt(second, 2, authorization_id='grant', owner='other')


@pytest.mark.parametrize('code', ['QC_RESPONSE_INVALID', 'GOOGLE_FLOW_AUTH_REQUIRED',
    'GOOGLE_FLOW_PROFILE_BUSY', 'RECONCILIATION_REQUIRED', 'TimeoutError'])
def test_non_ui_errors_do_not_launch_repair(code):
    assert not is_ui_mismatch(code)


def test_ui_match_and_resume_are_separate_from_send_and_qc():
    assert is_ui_mismatch('GOOGLE_FLOW_SETTINGS_SELECTOR_NOT_UNIQUE')
    for phase in ('uncertain', 'submitted', 'completed'):
        assert resume_decision(phase, True, True, None)['action'] != 'resume'
    assert resume_decision('not_sent', True, True, {'code': 'QC_RESPONSE_INVALID'})['action'] == 'stop'
    assert resume_decision('not_sent', True, True, None)['action'] == 'resume'
    assert resume_decision('not_sent', False, True, None)['action'] == 'stop'


def test_receipt_and_proposal_commit_as_one_sqlite_transaction(tmp_path):
    ledger = RepairLedger(sqlite3.connect(tmp_path / 'worker.db'))
    inc = ledger.incident(workflow_id='wf', operation_id='op', profile_id='p',
        error_code='GOOGLE_FLOW_UI_UNVERIFIED', fingerprint='ui')['incident_id']
    ledger.acquire(inc, 'owner', 60)
    n = ledger.reserve_attempt(inc, 2, authorization_id='grant', owner='owner')
    ledger.connection.execute("CREATE TRIGGER reject_proposal BEFORE UPDATE OF details_json ON flow_ui_incident BEGIN SELECT RAISE(ABORT,'simulated crash'); END")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.finish_attempt(inc, n, 'response_received', {}, proposal={'files': []})
    assert ledger.get(inc)['attempts'][0]['status'] == 'sent'
