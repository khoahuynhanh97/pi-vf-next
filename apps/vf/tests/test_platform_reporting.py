from argparse import Namespace

import pytest

from automation import cli
from automation.pipelines.spark_pipeline import PipelineRunResult, _inspect_run_workflows


def test_inspection_reports_corrupt_workflow_db(tmp_path, caplog):
    path = tmp_path / 'corrupt.db'
    path.write_bytes(b'not a database')
    errors = []
    assert _inspect_run_workflows(path, 'run', inspection_errors=errors) == ([], [])
    assert 'DatabaseError' in errors[0]
    assert 'run' in caplog.text


@pytest.mark.parametrize('status,expected', [('success', 0), ('partial', 1), ('failed', 1)])
def test_trigger_exit_code(monkeypatch, status, expected):
    result = PipelineRunResult('run', status, '2026-01-01', '', '', 4, 1, 'fake')
    monkeypatch.setattr(cli, 'trigger_daily_run', lambda **kwargs: result)
    args = Namespace(daemon=False, csv=None, limit=1, scenes=4, provider_mode='fake',
                     force=False, date=None, qc_level='high', acquire=False)
    assert cli.cmd_trigger(args) == expected


@pytest.mark.parametrize('raw', [b'{broken', b'{"status":"wrong shape"}', b'\xff'])
def test_corrupt_scheduler_ledger_is_preserved_and_stops(tmp_path, monkeypatch, raw):
    from automation.scheduler import daily_trigger

    ledger = tmp_path / 'scheduler_ledger.json'
    ledger.write_bytes(raw)
    monkeypatch.setattr(daily_trigger, 'LEDGER_PATH', ledger)
    monkeypatch.setattr(daily_trigger, 'prepare_sheet_intake',
                        lambda *a, **kw: pytest.fail('must stop before acquiring input'))
    with pytest.raises(RuntimeError, match='Corrupt scheduler ledger'):
        daily_trigger.trigger_daily_run(force=True)
    assert ledger.read_bytes() == raw
    backups = list(tmp_path.glob('scheduler_ledger.json.corrupt-*'))
    assert len(backups) == 1 and backups[0].read_bytes() == raw


def test_ledger_replaces_existing_file_only_after_complete_write(tmp_path, monkeypatch):
    from automation.scheduler import daily_trigger

    ledger = tmp_path / 'scheduler_ledger.json'
    ledger.write_text('[]', encoding='utf8')
    monkeypatch.setattr(daily_trigger, 'LEDGER_PATH', ledger)
    from pathlib import Path
    real_replace = Path.replace
    def checked_replace(source, target):
        assert ledger.read_text(encoding='utf8') == '[]'
        assert source.parent == ledger.parent
        assert '"run_id": "new"' in source.read_text(encoding='utf8')
        return real_replace(source, target)
    monkeypatch.setattr(Path, 'replace', checked_replace)
    daily_trigger.save_ledger([{'run_id': 'new'}])
    assert daily_trigger.load_ledger() == [{'run_id': 'new'}]



def test_daily_report_joins_pi_skips_and_vf_observations(tmp_path, monkeypatch):
    import hashlib
    import json
    import sqlite3
    from automation import daily_report

    intake = tmp_path / 'pi'
    intake.mkdir()
    (intake / 'intake.json').write_text(json.dumps({'rows': [
        {'row_id': 'row_0001', 'candidate_id': 'a', 'name': 'A', 'status': 'skipped',
         'reason_code': 'browser_error', 'retryable': False},
        {'row_id': 'row_0002', 'candidate_id': 'b', 'name': 'B', 'status': 'completed'},
    ]}), encoding='utf8')
    db = tmp_path / 'vf.db'
    selection = 'pv-creative-batch-' + hashlib.sha256(b'run').hexdigest()[:24]
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE workflow(workflow_id, aggregate_json)')
        conn.execute('CREATE TABLE workflow_event(workflow_id, event_type, payload_json)')
        conn.execute('INSERT INTO workflow VALUES (?, ?)', (selection, json.dumps({'prepared_refs': ['wf']})))
        conn.execute('INSERT INTO workflow VALUES (?, ?)', ('wf', json.dumps({
            'state': 'completed', 'render_result': {'final_review': {'evaluator_receipt': {'route': {
                'attempt_id': 'final-1', 'cost_status': 'recorded', 'cost_usd': 0.25}}}},
            'source_context': {'source_product_id': 'row_0002',
                                                    'creative_input': {'candidate_id': 'b'}},
        })))
        conn.execute('INSERT INTO workflow_event VALUES (?, ?, ?)', ('wf', 'PipelineStep', json.dumps({
            'step_name': 'EXPORT', 'status': 'completed',
        })))
    monkeypatch.setattr(daily_report, '_notify_telegram', lambda *args: None)
    monkeypatch.setattr(daily_report, 'preproduction_summary', lambda artifact: {
        'verdict': 'FAIL', 'scores': {'identity_claims': 20}, 'total_tokens': 72})
    result = PipelineRunResult('run', 'partial', '2026-01-01', '', '', 4, 2, 'configured',
                               intake_result={'run_dir': str(intake)})
    path = daily_report.write_daily_report(result, db_path=db, root=tmp_path / 'reports')
    payload = json.loads(path.read_text(encoding='utf8'))
    products = payload['runs']['configured:run']['products']
    assert len(products) == 2
    assert products[0]['reason_code'] == 'browser_error'
    assert products[1]['stages']['EXPORT']['status'] == 'completed'
    assert products[1]['preproduction']['total_tokens'] == 72
    assert payload['cost']['recorded_usd'] == 0.25 and payload['cost']['complete'] is False
    daily_report.write_daily_report(result, db_path=db, root=tmp_path / 'reports')
    assert json.loads(path.read_text(encoding='utf8'))['cost']['recorded_usd'] == 0.25



def test_daily_report_telegram_requires_destination_and_sends_document(tmp_path, monkeypatch):
    import httpx
    from automation import daily_report

    report = tmp_path / 'day.json'
    report.write_text('{}', encoding='utf8')
    monkeypatch.setattr(daily_report, 'LEGACY_TELEGRAM_ENV', tmp_path / '.env')
    monkeypatch.delenv('PRODUCT_SCOUT_TELEGRAM_BOT_TOKEN', raising=False)
    monkeypatch.delenv('PRODUCT_SCOUT_TELEGRAM_OWNER_ID', raising=False)
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'synthetic-token')
    monkeypatch.delenv('TELEGRAM_CHAT_ID', raising=False)
    sent = []

    def fake_post(url, *, data, files, timeout):
        sent.append((url, data, files['document'][1].read(), timeout))
        return httpx.Response(200, json={'ok': True})

    monkeypatch.setattr(httpx, 'post', fake_post)
    daily_report._notify_telegram(report, '2026-01-01', 'success', 2, 0)
    assert not sent
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '123')
    daily_report._notify_telegram(report, '2026-01-01', 'success', 2, 0)
    assert len(sent) == 1 and sent[0][0].endswith('/sendDocument')
    assert sent[0][1]['chat_id'] == '123' and sent[0][2] == b'{}'
    monkeypatch.delenv('TELEGRAM_CHAT_ID')
    daily_report.LEGACY_TELEGRAM_ENV.write_text(
        'PRODUCT_SCOUT_TELEGRAM_BOT_TOKEN="fixture-token"\nPRODUCT_SCOUT_TELEGRAM_OWNER_ID=321 # test\n',
        encoding='utf8')
    daily_report._notify_telegram(report, '2026-01-01', 'success', 2, 0)
    assert sent[1][1]['chat_id'] == '321'



def test_human_verdict_is_bound_to_export_and_compared_with_qc(tmp_path, capsys, monkeypatch):
    import hashlib
    import json
    import sqlite3
    from automation import cli, human_review

    monkeypatch.setattr(human_review, 'preproduction_summary', lambda _: {"total": 82, "verdict": "PASS"})
    exports = tmp_path / 'exports'
    export = exports / 'export_01'
    export.mkdir(parents=True)
    video = export / 'final.mp4'
    video.write_bytes(b'fixture bytes')
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    metadata = export / 'metadata.json'
    metadata.write_text(json.dumps({'project_id': 'project', 'version': 'v1',
        'product_identity': 'Product A', 'video': {'sha256': digest},
        'provenance': {'qc': {'video': 'report'}, 'creative': {'creative_lineage': {
            'script': {'script_digest': 'script-1'}, 'scene_prompts': {'digest': 'bundle-1'},
        }}}}), encoding='utf8')
    db = tmp_path / 'vf.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE product_video_qc_reports(report_id, workflow_id, artifact_digest, score, verdict, findings_json)')
        conn.execute('INSERT INTO product_video_qc_reports VALUES (?, ?, ?, ?, ?, ?)',
                     ('report', 'wf', digest, 91, 'pass', '{}'))
        conn.execute('CREATE TABLE workflow(workflow_id, aggregate_json)')
        conn.execute('INSERT INTO workflow VALUES (?, ?)', ('wf', json.dumps({'scene_plan': {
            'script_digest': 'script-1', 'scene_prompts': {'digest': 'bundle-1'},
        }})))
    before = metadata.read_bytes()
    with pytest.raises(SystemExit) as exc:
        cli.main(['human-verdict', str(export), '--verdict', 'reject', '--reason', 'No cause'])
    assert exc.value.code == 2 and not (export / 'human_verdict.json').exists()
    assert cli.main(['human-verdict', str(export), '--verdict', 'reject', '--cause', 'render', '--reason', 'Continuity glitch']) == 0
    assert metadata.read_bytes() == before
    saved = json.loads((export / 'human_verdict.json').read_text(encoding='utf8'))
    assert saved['verdict'] == 'reject' and saved['video_sha256'] == digest
    assert saved['cause'] == 'render'
    args = ['compare-qc', '--exports-root', str(exports), '--db-path', str(db)]
    assert cli.main(args) == 0
    output = capsys.readouterr().out
    assert '91' in output and 'reject | render | Continuity glitch' in output
    assert '82 (PASS)' in output and 'min=82, median=82, max=82, below=0' in output
    saved.pop('cause')
    (export / 'human_verdict.json').write_text(json.dumps(saved), encoding='utf8')
    assert cli.main(args) == 0
    assert 'reject | unknown | Continuity glitch' in capsys.readouterr().out
    video.write_bytes(b'changed fixture bytes')
    assert cli.main(args) == 0
    assert 'stale' in capsys.readouterr().out



def test_compare_qc_calibrates_on_preproduction_score_and_known_causes(monkeypatch, capsys):
    from automation import human_review

    Row = human_review.ComparisonRow
    rows = [Row("a", "80 (PASS)", "pass", 2, "reject", "script", "", 80),
            Row("b", "79 (FAIL)", "pass", 99, "approve", "product", "", 79),
            Row("c", "50 (FAIL)", "pass", 99, "reject", "render", "", 50),
            Row("d", "100 (PASS)", "pass", 99, "reject", "unknown", "", 100),
            Row("e", "— (UNKNOWN)", "pass", 99, "approve", "other", "", None)]
    monkeypatch.setattr(human_review, 'comparison_rows', lambda *args: iter(rows))
    assert cli.main(['compare-qc', '--threshold', '80']) == 0
    output = capsys.readouterr().out
    assert 'min=50, median=79.5, max=100, below=2' in output
    assert 'script+product: reviewed=2, approve=1, reject=1, scored=2' in output
    assert 'render/other: reviewed=2, approve=1, reject=1, scored=1' in output
    assert 'reject at/above threshold=1, approve below threshold=1' in output


def test_ledger_lock_timeout_and_release_after_process_exit(tmp_path, monkeypatch):
    import subprocess
    import sys
    from automation.scheduler import daily_trigger

    ledger = tmp_path / 'ledger.json'
    monkeypatch.setattr(daily_trigger, 'LEDGER_PATH', ledger)
    program = '''
import sys
import time
from pathlib import Path
from automation.scheduler import daily_trigger as s
s.LEDGER_PATH = Path(sys.argv[1])
with s._ledger_lock(timeout=5):
    print("locked", flush=True)
    time.sleep(20)
'''
    process = subprocess.Popen([sys.executable, '-c', program, str(ledger)], stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'locked'
        with pytest.raises(TimeoutError, match='scheduler ledger'):
            with daily_trigger._ledger_lock(timeout=0.15):
                pytest.fail('lock must not be acquired while another process holds it')
    finally:
        process.terminate()
        process.wait(timeout=5)
        process.stdout.close()
    with daily_trigger._ledger_lock(timeout=0.5):
        assert process.poll() is not None



def test_two_scheduler_processes_keep_both_updates(tmp_path, monkeypatch):
    import subprocess
    import sys
    from automation.scheduler import daily_trigger

    ledger = tmp_path / 'ledger.json'
    monkeypatch.setattr(daily_trigger, 'LEDGER_PATH', ledger)
    program = '''
import sys
from pathlib import Path
from automation.scheduler import daily_trigger as s
s.LEDGER_PATH = Path(sys.argv[1])
run_id = sys.argv[2]
if run_id == "first":
    write = s.atomic_json
    def wait_before_write(path, data):
        print("ready", flush=True)
        sys.stdin.readline()
        write(path, data)
    s.atomic_json = wait_before_write
s.record_run(s.ScheduleRecord("2026-01-01", "", run_id, "success", "", 0, 0))
'''
    command = [sys.executable, '-c', program, str(ledger)]
    with subprocess.Popen([*command, 'first'], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, text=True) as first:
        try:
            assert first.stdout.readline().strip() == 'ready'
            with subprocess.Popen([*command, 'second']) as second:
                try:
                    with pytest.raises(subprocess.TimeoutExpired):
                        second.wait(timeout=0.3)
                finally:
                    first.stdin.write('go\n')
                    first.stdin.flush()
                assert second.wait(timeout=5) == 0
        finally:
            first.stdin.close()
        assert first.wait(timeout=5) == 0
    assert {row['run_id'] for row in daily_trigger.load_ledger()} == {'first', 'second'}
