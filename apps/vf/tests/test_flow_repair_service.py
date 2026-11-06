import sqlite3
from types import SimpleNamespace

import pytest

from video_factory.maintenance.ledger import RepairLedger
from video_factory.maintenance.service import RepairService


def setup_service(tmp_path, *, evidence=None, model=None):
    ledger = RepairLedger(sqlite3.connect(tmp_path / 'worker.db'))
    inc = ledger.incident(workflow_id='wf', operation_id='op', profile_id='profile',
        error_code='GOOGLE_FLOW_SETTINGS_SELECTOR_NOT_UNIQUE', fingerprint='ui')
    calls = []
    def inspect(incident):
        calls.append('inspect')
        return evidence or {'fingerprint': 'observed', 'contract_verified': False, 'controls': ['Settings']}
    service = RepairService(ledger, tmp_path, tmp_path / 'artifacts', inspect=inspect, model=model)
    return ledger, service, inc['incident_id'], calls


def grant(incident_id):
    return {'authorization_id': 'maintenance-only', 'scope': 'flow_ui_maintenance',
        'incident_id': incident_id, 'profile_id': 'profile', 'max_calls': 2,
        'expires_at': '2099-01-01T00:00:00+00:00'}


def test_no_grant_collects_evidence_without_model_call(tmp_path):
    class NeverModel:
        def generate_json(self, **kwargs):
            raise AssertionError('no coding permission')
    ledger, service, incident, calls = setup_service(tmp_path, model=NeverModel())
    result = service.run(incident)
    assert result['status'] == 'authorization_required'
    assert calls == ['inspect']
    assert ledger.get(incident)['attempts'] == []


def test_model_timeout_is_durable_and_restart_does_not_retry(tmp_path):
    class Model:
        def generate_json(self, **kwargs):
            raise TimeoutError('sent but response missing')
    ledger, service, incident, _ = setup_service(tmp_path, model=Model())
    first = service.run(incident, grant(incident))
    assert first['status'] == 'coding_uncertain'
    second = service.run(incident, grant(incident))
    assert second['status'] == 'coding_uncertain'
    assert len(ledger.get(incident)['attempts']) == 1


@pytest.mark.parametrize('layout', ['new-role-layout', 'moved-settings-panel'])
def test_two_recipes_stage_proposal_and_do_not_activate_without_isolation(tmp_path, layout):
    events = []
    class Model:
        def generate_json(self, **kwargs):
            events.append('model')
            return SimpleNamespace(parsed={'files': [{'path': 'src/video_factory/integrations/flow_ui_recipe.py',
                'before_sha256': '', 'content': 'RECIPE = '+repr(layout)+'\n'}], 'diagnosis': layout},
                parse_error='', route_receipt={'actual_model': 'fake'}, provider='fake', model='fake')
    ledger, service, incident, _ = setup_service(tmp_path, model=Model())
    result = service.run(incident, grant(incident))
    assert result['status'] == 'validation_unavailable'
    assert not (tmp_path / 'src/video_factory/integrations/flow_ui_recipe.py').exists()
    assert service.run(incident, grant(incident))['status'] == 'validation_unavailable'
    assert events == ['model']


def test_unsafe_proposal_is_rejected_without_touching_workflow(tmp_path):
    class Model:
        def generate_json(self, **kwargs):
            return SimpleNamespace(parsed={'files': [{'path': '../config.yaml', 'before_sha256': '', 'content': 'oops'}]},
                parse_error='', route_receipt={}, provider='fake', model='fake')
    ledger, service, incident, _ = setup_service(tmp_path, model=Model())
    assert service.run(incident, grant(incident))['status'] == 'patch_rejected'
    assert ledger.connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='workflow'").fetchone()[0] == 0


def test_existing_source_hash_and_long_code_survive_response_restart(tmp_path):
    import hashlib
    name = 'src/video_factory/integrations/google_flow_provider.py'
    path = tmp_path / name
    path.parent.mkdir(parents=True)
    path.write_text("SELECTOR = 'old'\n", encoding='utf8')
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    content = "SELECTOR = 'new'\n" + '# diagnostic source only\n' * 1000
    class Model:
        def generate_json(self, **kwargs):
            return SimpleNamespace(parsed={'files': [{'path': name, 'before_sha256': before, 'content': content}]},
                parse_error='', route_receipt={}, provider='fake', model='fake')
    ledger, service, incident, _ = setup_service(tmp_path, model=Model())
    assert service.run(incident, grant(incident))['status'] == 'validation_unavailable'
    persisted = ledger.get(incident)['details']['proposal']['files'][0]
    assert persisted['before_sha256'] == before
    assert persisted['content'] == content
    assert service.run(incident, grant(incident))['status'] == 'validation_unavailable'
    assert len(ledger.get(incident)['attempts']) == 1


def test_plain_validation_result_never_calls_authenticated_preflight(tmp_path):
    class Model:
        def generate_json(self, **kwargs):
            return SimpleNamespace(parsed={'files': [{'path': 'src/video_factory/integrations/flow_ui_recipe.py',
                'before_sha256': None, 'content': 'RECIPE = {}\n'}]}, parse_error='', route_receipt={}, provider='fake', model='fake')
    _, service, incident, _ = setup_service(tmp_path, model=Model())
    service.validator = SimpleNamespace(validate=lambda *args: {'status': 'passed', 'executed': True})
    service.preflight = lambda *args: pytest.fail('must not reach authenticated browser')
    assert service.run(incident, grant(incident))['status'] == 'policy_review_required'


def test_corrective_coding_attempt_is_explicit_and_capped(tmp_path):
    class Model:
        calls = 0
        def generate_json(self, **kwargs):
            self.calls += 1
            return SimpleNamespace(parsed={}, parse_error='invalid', route_receipt={}, provider='fake', model='fake')
    model = Model()
    _, service, incident, _ = setup_service(tmp_path, model=model)
    assert service.run(incident, grant(incident))['status'] == 'coding_invalid'
    assert service.run(incident, grant(incident))['reused'] is True
    assert model.calls == 1
    assert service.run(incident, grant(incident), revise=True)['status'] == 'coding_invalid'
    assert service.run(incident, grant(incident), revise=True)['status'] == 'maintenance_limit'
    assert model.calls == 2


def test_inspection_failure_can_refresh_after_login(tmp_path):
    ledger, service, incident, _ = setup_service(tmp_path)
    states = iter([{'status': 'auth_required'}, {'status': 'observed', 'fingerprint': 'new-ui'}])
    service.inspect = lambda i: next(states)
    assert service.run(incident)['status'] == 'inspection_blocked'
    assert service.run(incident)['status'] == 'authorization_required'
    assert ledger.get(incident)['details']['evidence']['fingerprint'] == 'new-ui'


def test_recheck_after_acquire_observes_completed_coding_from_other_owner(tmp_path, monkeypatch):
    ledger, service, incident, _ = setup_service(tmp_path)
    original = ledger.acquire
    def acquire(*args, **kwargs):
        result = original(*args, **kwargs)
        ledger.update(incident, status='resumed')
        return result
    monkeypatch.setattr(ledger, 'acquire', acquire)
    assert service.run(incident, grant(incident)) == {'incident_id': incident, 'status': 'resumed', 'reused': True}
    assert ledger.get(incident)['attempts'] == []


@pytest.mark.parametrize('selector', ["[role=tab]", "button[aria-label=Settings]"])
def test_full_offline_repair_activation_and_resume_reuses_operation(tmp_path, selector):
    import hashlib
    import subprocess
    from video_factory.maintenance import patching
    name = 'src/video_factory/integrations/google_flow_provider.py'
    path = tmp_path / name
    path.parent.mkdir(parents=True)
    path.write_text("SELECTOR = 'old'\n", encoding='utf8')
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    after = 'SELECTOR = '+repr(selector)+'\n'
    for test in patching.TRUSTED_TESTS:
        f = tmp_path / test
        f.parent.mkdir(exist_ok=True)
        f.write_text('def test_original():\n    assert True\n')
    calls = []
    class Model:
        def generate_json(self, **kwargs):
            calls.append('model')
            return SimpleNamespace(parsed={'files': [{'path': name, 'before_sha256': before, 'content': after}]},
                parse_error='', route_receipt={}, provider='fake', model='fake')
    def docker(args, **kwargs):
        if args[1] == 'info':
            return subprocess.CompletedProcess(args, 0, 'linux', '')
        if args[1:3] == ['image', 'inspect']:
            return subprocess.CompletedProcess(args, 0, 'sha256:'+'a'*64, '')
        return subprocess.CompletedProcess(args, 0, '3 passed in 0.1s', '')
    ledger, service, incident, _ = setup_service(tmp_path, model=Model())
    ledger.update(incident, operation_phase='not_sent')
    service.validator = patching.DockerValidator(tmp_path, image='python@sha256:'+'a'*64, runner=docker, docker_path='fake-docker')
    service.review = lambda files: patching.issue_policy_review_receipt(files,
        reviewer_id='explicit-offline-reviewer', review_reference='fixture-review', findings=[])
    hashes = {name: hashlib.sha256(after.encode()).hexdigest()}
    service.preflight = lambda files, review: patching.issue_preflight_receipt(files,
        worker_id='worker', profile_id='profile', evidence_fingerprint='offline-evidence',
        capabilities={k: 'fixture-'+k for k in patching.REQUIRED_CAPABILITIES},
        active_code_sha256=hashes, policy_review=review)
    service.idle_check = lambda: {'idle': True, 'worker_id': 'worker', 'profile_id': 'profile', 'active_code_sha256': hashes}
    def resume(current, receipt):
        assert current['operation_id'] == 'op'
        assert path.read_text() == after
        calls.append('resume_same_operation')
        return {'action': 'resume'}
    service.resume = resume
    first = service.run(incident, grant(incident), context={'unrelated_pause': {'code': 'QC_RESPONSE_INVALID'}})
    assert first['status'] == 'activated_blocked'
    assert calls == ['model']
    second = service.run(incident, grant(incident), context={'unrelated_pause': None})
    assert second['status'] == 'resumed'
    assert service.run(incident, grant(incident))['status'] == 'resumed'
    assert calls == ['model', 'resume_same_operation']
    assert len(ledger.get(incident)['attempts']) == 1
