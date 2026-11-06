"""Offline checks of new-production defaults and saved-run policy preservation."""
import hashlib
import json
from argparse import Namespace
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from automation import cli
from automation.pipelines import spark_pipeline
from automation.scheduler import daily_trigger


@pytest.mark.parametrize('command, extra', [('run', []), ('trigger', []), ('resume', ['--run-id', 'saved'])])
@pytest.mark.parametrize('level', [None, 'low', 'middle'])
def test_unified_cli_defaults_new_runs_to_high_and_preserves_resume_omission(monkeypatch, command, extra, level):
    received = []
    monkeypatch.setattr(cli, 'cmd_' + command, lambda args: received.append(args) or 0)
    args = [command, *extra]
    if level:
        args.extend(['--qc-level', level])
    assert cli.main(args) == 0
    assert received[0].qc_level == (level or (None if command == 'resume' else 'high'))


@pytest.mark.parametrize('level, expected', [(None, 'high'), ('low', 'low'), ('middle', 'middle')])
def test_daily_trigger_forwards_high_default_and_explicit_levels(tmp_path, monkeypatch, level, expected):
    received = []
    monkeypatch.setattr(daily_trigger, 'prepare_sheet_intake', lambda *a, **kw: {})
    monkeypatch.setattr(daily_trigger, 'LEDGER_PATH', tmp_path / 'ledger.json')
    def run(**kwargs):
        received.append(kwargs)
        return spark_pipeline.PipelineRunResult('run', 'waiting', '2026-11-05', '', '', 4, 1, 'fake')
    monkeypatch.setattr(daily_trigger, 'run_pipeline', run)
    kwargs = {} if level is None else {'qc_level': level}
    result = daily_trigger.trigger_daily_run(date='2026-11-05', **kwargs)
    assert result.status == 'waiting'
    assert received[0]['qc_level'] == expected
    assert json.loads((tmp_path / 'ledger.json').read_text())[-1]['status'] == 'waiting'


@pytest.mark.parametrize('saved_level', ['high', 'middle', None])
@pytest.mark.parametrize('source', ['children', 'pinned'])
def test_pipeline_omitted_resume_policy_uses_saved_level(tmp_path, monkeypatch, saved_level, source):
    db = tmp_path / 'vf.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE workflow (workflow_id TEXT, aggregate_json TEXT)')
        selection = {'prepared_refs': ['wf']} if source == 'children' else {}
        if source == 'pinned' and saved_level:
            selection['qc_level'] = saved_level
        policy = {} if saved_level is None else {'qc_level': saved_level}
        conn.execute('INSERT INTO workflow VALUES (?,?)', (
            'pv-creative-batch-' + hashlib.sha256(b'saved').hexdigest()[:24], json.dumps(selection)))
        conn.execute('INSERT INTO workflow VALUES (?,?)', ('wf', json.dumps({'creative_policy': policy})))
    _assert_pipeline_level(tmp_path, monkeypatch, db, {'qc_level': None}, saved_level or 'middle')


@pytest.mark.parametrize('level, expected', [(None, 'high'), ('middle', 'middle'), ('low', 'low')])
def test_pipeline_new_run_defaults_high_and_preserves_explicit_override(tmp_path, monkeypatch, level, expected):
    _assert_pipeline_level(tmp_path, monkeypatch, tmp_path / 'new.db',
        {} if level is None else {'qc_level': level}, expected)


def _assert_pipeline_level(tmp_path, monkeypatch, db, policy_kwargs, expected):
    batch = tmp_path / 'batch'
    batch.mkdir()
    (batch / 'READY').write_text('READY')
    profile = tmp_path / 'profile.json'
    profile.write_text('{}')
    monkeypatch.setattr(spark_pipeline, 'ensure_runtime_dirs', lambda: None)
    monkeypatch.setattr(spark_pipeline, 'PI_RUNS_DIR', tmp_path / 'runs')
    monkeypatch.setattr(spark_pipeline, '_prepare_scene_profile', lambda *a: profile)
    monkeypatch.setattr(spark_pipeline, '_inspect_run_workflows', lambda *a, **kw: ([], []))
    commands = []
    def execute(cmd, **kwargs):
        commands.append(cmd)
        return SimpleNamespace(stdout='', stderr='offline terminal blocker', returncode=2)
    monkeypatch.setattr(spark_pipeline.subprocess, 'run', execute)
    result = spark_pipeline.run_pipeline(run_id='saved', db_path=db, profile_path=profile,
        provider_mode='fake', sheet_intake={'status': 'success', 'exchange_batch_dir': str(batch)},
        **policy_kwargs)
    assert result.status == 'failed'
    assert len(commands) == 1
    assert commands[0][commands[0].index('--qc-level') + 1] == expected


@pytest.mark.parametrize('saved', [False, True])
@pytest.mark.parametrize('level', [None, 'low', 'middle'])
def test_fresh_creative_cli_defaults_only_new_workflows_to_high(tmp_path, monkeypatch, saved, level):
    from test_creative_resume import config
    from video_factory.automation import creative_cli
    from video_factory.automation.importer import ExchangeImporter, publish_daily_run_to_exchange
    from video_factory.automation.coordinator import ProductVideoCoordinator
    from video_factory.storage_infra.sqlite import connect
    cfg = config()
    if level:
        cfg['qc_level'] = level
    db = tmp_path / 'fresh.db'
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps(cfg))
    if saved:
        conn = connect(db)
        ProductVideoCoordinator(conn, db_path=db).start_fresh_creative(
            run_id='fresh', query='Synthetic fixture', run_config=cfg)
        conn.close()
    monkeypatch.setenv('VF_CREATIVE_MODE', 'four_agent')
    monkeypatch.setenv('VF_CREATIVE_RUN_CONFIG', str(profile))
    monkeypatch.setattr(creative_cli, '_advance_until', lambda *a, **kw: ({}, True))
    assert creative_cli.cmd_creative_run(Namespace(profile=profile, db_path=db,
        inbox_root=None, run_id='fresh', query='Synthetic fixture', owner_user_id='local-owner', max_ticks=1)) == 0
    conn = connect(db)
    agg = ProductVideoCoordinator(conn, db_path=db).workflow_repo.get_workflow('pv-creative-fresh')[0]
    assert agg['creative_policy'].get('qc_level') == (level or (None if saved else 'high'))
    conn.close()
    # A default-high run must also resume from the same unchanged profile.
    assert creative_cli.cmd_creative_run(Namespace(profile=profile, db_path=db,
        inbox_root=None, run_id='fresh', query='Synthetic fixture', owner_user_id='local-owner', max_ticks=1)) == 0
    cfg['qc_level'] = 'low' if level != 'low' else 'middle'
    profile.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match='different input'):
        creative_cli.cmd_creative_run(Namespace(profile=profile, db_path=db,
            inbox_root=None, run_id='fresh', query='Synthetic fixture', owner_user_id='local-owner', max_ticks=1))


def test_fresh_prepare_defaults_high_and_produce_inherits_prepared_policy(tmp_path, monkeypatch):
    from video_factory.automation.importer import publish_daily_run_to_exchange
    from video_factory.automation.importer import ExchangeImporter
    from test_creative_resume import config
    from test_studio_creative_batch import _ready_two_product_batch
    from video_factory.automation import creative_cli
    from video_factory.automation.coordinator import ProductVideoCoordinator
    from video_factory.storage_infra.sqlite import connect
    inbox, keys = _ready_two_product_batch(tmp_path)
    source = tmp_path / 'source'
    products = json.loads((source / 'products.json').read_text())
    for product in products['products']:
        product['variant'] = 'standard'
    (source / 'products.json').write_text(json.dumps(products))
    inbox = tmp_path / 'fresh-inbox'
    batch = publish_daily_run_to_exchange(source, inbox, batch_id='run_fresh')
    keys = {product.brand: product.product_key for product in ExchangeImporter(inbox).load_manifest(batch).products}
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps(config()))
    db = tmp_path / 'prepare.db'
    monkeypatch.setattr(creative_cli, '_advance_until', lambda *a, **kw: ({'state':'completed'}, True))
    args = Namespace(profile=profile, db_path=db, inbox_root=inbox, preparation_id='new-prep',
        product_key=keys["Lay's"], source_workflow_id=None, owner_user_id='local-owner', date='2026-10-25', max_ticks=1)
    assert creative_cli.cmd_creative_prepare(args) == 0
    conn = connect(db)
    coordinator = ProductVideoCoordinator(conn, db_path=db, inbox_root=inbox)
    row = coordinator.import_repo.find_by_target(keys["Lay's"])
    agg = coordinator.workflow_repo.get_workflow(row['workflow_id'])[0]
    assert agg['creative_policy']['qc_level'] == 'high'
    # Resume the same preparation with an unchanged level-less profile.
    assert creative_cli.cmd_creative_prepare(args) == 0
    conn.close()


@pytest.mark.parametrize('level', [None, 'middle', 'high'])
def test_prepared_source_policy_survives_prepare_and_produce_cli(tmp_path, monkeypatch, level):
    from test_creative_resume import config
    from test_prepared_catalog_production import prepared_source
    from video_factory.automation import creative_cli
    from video_factory.automation.coordinator import ProductVideoCoordinator
    from video_factory.storage_infra.sqlite import connect
    db = tmp_path / 'prepared.db'
    conn = connect(db)
    coordinator = ProductVideoCoordinator(conn, db_path=db)
    ref = prepared_source(coordinator, tmp_path / 'source')
    agg, version = coordinator.workflow_repo.get_workflow(ref)
    if level:
        agg['creative_policy']['qc_level'] = level
        coordinator.workflow_repo.commit_workflow(ref, json.dumps(agg), [], expected_version=version)
    coordinator.advance_preparation(ref)
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps(config()))
    monkeypatch.setattr(creative_cli, '_advance_until', lambda *a, **kw: ({'state':'completed'}, True))
    args = Namespace(profile=profile, db_path=db, inbox_root=None, preparation_id='prepare-fixture-1',
        product_key=None, source_workflow_id=ref, owner_user_id='local-owner', date=None, max_ticks=1)
    assert creative_cli.cmd_creative_prepare(args) == 0
    assert creative_cli.cmd_creative_produce(Namespace(profile=profile, db_path=db, inbox_root=None,
        run_id='production', prepared_ref=[ref], limit=1, owner_user_id='local-owner', date=None, max_ticks=1)) == 0
    policy = coordinator.workflow_repo.get_workflow(ref)[0]['creative_policy']
    assert policy.get('qc_level') == level
    conn.close()
