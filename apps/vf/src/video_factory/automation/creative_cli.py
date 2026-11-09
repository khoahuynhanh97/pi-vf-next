"""Bounded single-product command using the existing coordinator and worker."""
import json
import os
import re
import time
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path


# One render poll can run for 15 minutes. Keep a small margin over that
# existing bound while preventing an unchanged pending/deferred job looping.
CREATIVE_NO_PROGRESS_TIMEOUT_SECONDS = 1200


def _catalog_coordinator(args):
    from video_factory.automation.coordinator import ProductVideoCoordinator
    from video_factory.storage_infra.sqlite import connect
    from video_factory.constants import get_hermes_db_path

    profile = getattr(args, 'profile', None) or os.environ.get('VF_CREATIVE_RUN_CONFIG')
    if not profile:
        raise ValueError('Set VF_CREATIVE_RUN_CONFIG once or pass --profile to choose the approved creative profile.')
    config_path = Path(profile).resolve()
    config = json.loads(config_path.read_text(encoding='utf8'))
    db = get_hermes_db_path(args.db_path)
    os.environ['VF_CREATIVE_MODE'] = 'four_agent'
    os.environ['VF_CREATIVE_RUN_CONFIG'] = str(config_path)
    os.environ['HERMES_APPROVAL_DB_PATH'] = str(db)
    os.environ['HERMES_VIDEO_FACTORY_DB_PATH'] = str(db)
    conn = connect(db)
    inbox = Path(args.inbox_root).resolve() if getattr(args, 'inbox_root', None) else None
    return conn, ProductVideoCoordinator(conn, db_path=db, inbox_root=inbox), config


def _wait_until(value):
    if not value:
        time.sleep(1)
        return
    raw=str(value)
    if raw.endswith('Z'):
        raw=raw[:-1]+'+00:00'
    target=datetime.fromisoformat(raw)
    if target.tzinfo is None:
        target=target.replace(tzinfo=timezone.utc)
    delay=(target-datetime.now(timezone.utc)).total_seconds()
    if delay>0:
        time.sleep(delay)


def _durable_progress_snapshot(coordinator, workflow_id):
    connection = getattr(coordinator, 'connection', None)
    repository = getattr(coordinator, 'workflow_repo', None)
    current = repository.get_workflow(workflow_id) if repository else None
    aggregate = dict(current[0]) if current else {}
    aggregate.pop('creative_no_progress', None)
    durable_jobs = []
    if connection is not None:
        for table in ('video_factory_keyframe_operation', 'video_factory_scene_operation'):
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                continue
            columns = {str(row[1]) for row in connection.execute(f'PRAGMA table_info({table})')}
            scope_column = 'workflow_id' if 'workflow_id' in columns else (
                'project_id' if table == 'video_factory_scene_operation' and 'project_id' in columns else None
            )
            scope_value = workflow_id if scope_column == 'workflow_id' else aggregate.get('project_id')
            stable = [name for name in (
                'operation_id','status','provider_operation_id','external_operation_id',
                'output_sha256','result_sha256','error_code','error_message','output_path',
            ) if name in columns]
            if stable and scope_column and scope_value:
                selected = ','.join(stable)
                rows = connection.execute(
                    f'SELECT {selected} FROM {table} WHERE {scope_column}=? ORDER BY {selected}',
                    (scope_value,)).fetchall()
                durable_jobs.append((table, [tuple(row) for row in rows]))
    payload = json.dumps({'aggregate':aggregate,'durable_jobs':durable_jobs},sort_keys=True,
                         separators=(',',':'),default=str)
    return hashlib.sha256(payload.encode('utf8')).hexdigest(), aggregate, current


def _check_no_progress_deadline(coordinator, workflow_id):
    digest, aggregate, current = _durable_progress_snapshot(coordinator, workflow_id)
    repository = getattr(coordinator, 'workflow_repo', None)
    connection = getattr(coordinator, 'connection', None)
    if not repository or not current:
        return None
    now = datetime.now(timezone.utc)
    prior = (current[0] if current else {}).get('creative_no_progress')
    if not isinstance(prior, dict) or prior.get('progress_digest') != digest:
        deadline = now + timedelta(seconds=CREATIVE_NO_PROGRESS_TIMEOUT_SECONDS)
        wait = {'progress_digest':digest,'detected_at':now.isoformat(),'deadline_at':deadline.isoformat()}
        repository.commit_workflow(workflow_id, json.dumps({**aggregate,'creative_no_progress':wait}),
            [], expected_version=current[1])
        return None
    raw = str(prior.get('deadline_at') or '')
    try:
        deadline = datetime.fromisoformat(raw[:-1] + '+00:00' if raw.endswith('Z') else raw)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
    except ValueError:
        deadline = now
    if now < deadline:
        return None
    operation_ids = []
    if connection is not None:
        for table in ('video_factory_keyframe_operation', 'video_factory_scene_operation'):
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            columns = ({str(row[1]) for row in connection.execute(f'PRAGMA table_info({table})')}
                       if exists else set())
            scope_column = 'workflow_id' if 'workflow_id' in columns else (
                'project_id' if table == 'video_factory_scene_operation' and 'project_id' in columns else None
            )
            scope_value = workflow_id if scope_column == 'workflow_id' else aggregate.get('project_id')
            if scope_column and scope_value and 'operation_id' in columns and 'status' in columns:
                operation_ids.extend(str(row[0]) for row in connection.execute(
                    f"SELECT operation_id FROM {table} WHERE {scope_column}=? AND status IN ('submitting','submitted','running','unknown')",
                    (scope_value,)))
    return {'workflow_id':workflow_id,'action':'unresolved','code':'WORKFLOW_NO_PROGRESS_TIMEOUT',
        'state':aggregate.get('state'),'operation_ids':operation_ids,
        'reason':f'No durable workflow, operation, checkpoint, or output progress for '
                 f'{CREATIVE_NO_PROGRESS_TIMEOUT_SECONDS}s; workflow={workflow_id}; '
                 f'operations={operation_ids or "none"}'}


def _advance_until(coordinator, workflow_id, *, advance, success_action, max_ticks, emit_progress=True):
    # Keep max_ticks in the call contract for older CLI callers; normal worker
    # progress is no longer a reason to abandon an admitted product.
    del max_ticks
    result = {}
    while True:
        repository = getattr(coordinator, 'workflow_repo', None)
        result = advance(workflow_id)
        if emit_progress:
            print(json.dumps({'workflow_id':workflow_id, **result}, ensure_ascii=False))
        if (result.get('action') == success_action or
                (success_action == 'completed' and result.get('state') == 'completed')):
            return result, True
        if result.get('state') == 'skipped':
            return result, True
        if result.get('state') == 'WaitingForOperator':
            current = coordinator.workflow_repo.get_workflow(workflow_id)
            aggregate = current[0] if current else {}
            stage = aggregate.get('operator_stage')
            if (aggregate.get('failure_code') == 'CREATIVE_RESPONSE_INVALID'
                    and aggregate.get('operator_action') == 'creative_review'
                    and stage in {'direction', 'script', 'storyboard'}):
                connection = coordinator.connection
                recovery_count = 0
                for row in connection.execute(
                        "SELECT payload_json FROM workflow_event WHERE workflow_id=? "
                        "AND event_type='CreativeStageRecovered'", (workflow_id,)):
                    try:
                        if json.loads(row[0]).get('stage') == stage:
                            recovery_count += 1
                    except (TypeError, ValueError, AttributeError):
                        continue
                outbox = connection.execute(
                    "SELECT attempt_count,max_attempts FROM workflow_outbox WHERE workflow_id=? "
                    "AND state='quarantined' ORDER BY outbox_id DESC LIMIT 1", (workflow_id,)).fetchone()
                exhausted = bool(outbox and int(outbox[0]) >= int(outbox[1]))
                if not exhausted and recovery_count < 2:
                    from video_factory.application.operator_handler import recover_creative_stage
                    recover_creative_stage(connection, workflow_id=workflow_id,
                        owner_user_id=aggregate.get('owner_user_id') or 'local-owner',
                        note=f"Automatic bounded retry for received CREATIVE_RESPONSE_INVALID at {stage}.")
                    continue
                from video_factory.workflows.runtime_control import skip_unresolved_workflow
                reason = (f"CREATIVE_RESPONSE_INVALID: {stage or 'creative stage'} retry quota exhausted")
                detail = aggregate.get('operator_reason') or aggregate.get('failure_reason') or ''
                if 'DIRECTOR_REQUIRED_FIELD_MISSING:' in detail:
                    validation_reason = detail.split('DIRECTOR_REQUIRED_FIELD_MISSING:', 1)[1]
                    validation_reason = validation_reason.splitlines()[0].split('; raw:', 1)[0]
                    reason += ' (DIRECTOR_REQUIRED_FIELD_MISSING:' + validation_reason[:160] + ')'
                skip_unresolved_workflow(connection, workflow_id, reason, known_product_failure=True)
                return {'workflow_id':workflow_id, 'state':'skipped', 'action':'skipped',
                    'reason':reason, 'evidence':aggregate.get('operator_reason') or aggregate.get('failure_reason')}, True
        if result.get('action') in {'retry_scheduled', 'running', 'pending'}:
            timed_out = _check_no_progress_deadline(coordinator, workflow_id)
            if timed_out:
                if emit_progress:
                    print(json.dumps(timed_out, ensure_ascii=False))
                return timed_out, False
            _wait_until(result.get('next_attempt_at'))
            continue
        if result.get('action') == 'worker_tick':
            timed_out = _check_no_progress_deadline(coordinator, workflow_id)
            if timed_out:
                if emit_progress:
                    print(json.dumps(timed_out, ensure_ascii=False))
                return timed_out, False
            _wait_until(None)
            continue
        return result, False


def _print_batch_outcomes(coordinator, manifest, product_keys, started_by_key, skipped, unresolved,
                         shared_blocker=None):
    """Print only product-facing outcomes; durable workflow IDs stay in the ledger."""
    import json

    products = {item.product_key: item for item in manifest.products}
    details_by_key = {}
    workflow_by_key = dict(started_by_key)
    technical_product_keys = set()
    for item in [*skipped, *unresolved]:
        key = str(item.get('product_key') or '')
        workflow_id = str(item.get('workflow_id') or '')
        if item in unresolved and key:
            technical_product_keys.add(key)
        if key:
            details_by_key.setdefault(key, []).append(item)
        if workflow_id:
            current = coordinator.workflow_repo.get_workflow(workflow_id)
            source = (current[0].get('source_context') or {}) if current else {}
            key = key or str(source.get('product_key') or (current[0].get('product_key') if current else '') or '')
            if key:
                workflow_by_key.setdefault(key, workflow_id)
                if item in unresolved:
                    technical_product_keys.add(key)
                if item not in details_by_key.setdefault(key, []):
                    details_by_key[key].append(item)

    ordered_keys = list(dict.fromkeys([*product_keys, *details_by_key]))
    counts = {'completed': 0, 'blocked': 0, 'technical_errors': 0}
    technical_reasons = []
    active_reason_prefixes = (
        'ACTIVE_', 'BLOCKED_UNCERTAIN', 'ALREADY_CLAIMED', 'PREPARED_CHANGED',
        'PREPARED_STALE', 'PROFILE_STALE', 'PREPARED_NOT_PUBLISHED',
        'WORKFLOW_NO_PROGRESS_TIMEOUT', 'PROVIDER_', 'RECONCILIATION_REQUIRED',
    )

    def operator_reason(value):
        text = str(value or 'Không thể hoàn tất sản phẩm.')
        if text.startswith('ACTIVE_IDENTITY_VARIANT:'):
            return 'Sản phẩm này đang được xử lý ở lượt khác; pipeline không gửi yêu cầu trùng.'
        if text.startswith('BLOCKED_UNCERTAIN_OR_COMPLETED') or 'workflow=' in text or 'operations=' in text:
            return 'Kết quả operation chưa xác nhận; yêu cầu được giữ để đối soát, không gửi lại.'
        return text

    for key in ordered_keys:
        product = products.get(key)
        workflow_id = workflow_by_key.get(key)
        current = coordinator.workflow_repo.get_workflow(workflow_id) if workflow_id else None
        aggregate = current[0] if current else {}
        name = (product.name if product else '') or str(
            (aggregate.get('source_context') or {}).get('name')
            or (aggregate.get('research_result') or {}).get('product_name')
            or 'Sản phẩm từ PI'
        )
        if aggregate.get('state') == 'completed':
            exported = aggregate.get('export_result') or {}
            raw = exported
            if isinstance(exported, dict) and isinstance(exported.get('payload'), str):
                try:
                    raw = json.loads(exported['payload'])
                except (TypeError, ValueError):
                    raw = {}
            path = str(raw.get('file_path') or raw.get('video_path') or '') if isinstance(raw, dict) else ''
            if path and Path(path).is_file():
                print(f'{name}: Thành công — {path}')
                counts['completed'] += 1
                continue
            technical_reasons.append(f'{name}: MP4 đã hoàn tất nhưng không đọc được đường dẫn export hiện hành.')
            counts['technical_errors'] += 1
            continue

        item_details = details_by_key.get(key, [])
        reason = operator_reason(
            aggregate.get('failure_reason') or aggregate.get('skip_reason')
            or aggregate.get('operator_reason') or aggregate.get('failure_code')
            or next((entry.get('reason') for entry in item_details if entry.get('reason')), '')
        )
        if key in technical_product_keys or any(
            str(entry.get('reason') or '').startswith(active_reason_prefixes) for entry in item_details
        ):
            technical_reasons.append(f'{name}: {reason}')
            counts['technical_errors'] += 1
        elif aggregate.get('state') in {'skipped', 'WaitingForOperator', 'contract_broken'} or item_details:
            action = ('Thay ảnh nguồn rõ hơn hoặc sửa tên/variant đã xác minh trong PI, rồi chạy lại pipeline.'
                      if any(token in reason.upper() for token in ('IMAGE', 'IDENTITY', 'VARIANT', 'PRODUCT'))
                      else 'Kiểm tra thông tin và ảnh nguồn trong PI, sửa phần sai rồi chạy lại pipeline.')
            print(f'{name}: BLOCKED — {reason}. Cách sửa: {action}')
            counts['blocked'] += 1
        else:
            technical_reasons.append(f'{name}: {reason}')
            counts['technical_errors'] += 1

    if shared_blocker:
        technical_reasons.append(operator_reason(shared_blocker))
    technical_reasons = list(dict.fromkeys(technical_reasons))
    counts['technical_errors'] = len(technical_reasons)
    for reason in technical_reasons:
        print(f'Lỗi kỹ thuật: {reason}')
    print('Tổng kết: '
          f"thành công {counts['completed']}, BLOCKED {counts['blocked']}, "
          f"lỗi kỹ thuật {counts['technical_errors']}.")
    return counts


def cmd_creative_prepare(args):
    """Explicit source/preparation job; stops before image/video submission."""
    from datetime import date
    conn, coordinator, config = _catalog_coordinator(args)
    try:
        source_id = args.source_workflow_id
        if not source_id and args.product_key:
            imported = coordinator.import_repo.find_by_target(args.product_key)
            source_id = imported['workflow_id'] if imported else None
        saved = coordinator.workflow_repo.get_workflow(source_id) if source_id else None
        prior_policy = (saved[0].get('creative_policy') or {}) if saved else {}
        if 'qc_level' not in config:
            if prior_policy.get('preparation_id'):
                if prior_policy.get('qc_level'):
                    config['qc_level'] = prior_policy['qc_level']
            else:
                config['qc_level'] = 'high'
        result = coordinator.prepare_catalog(preparation_id=args.preparation_id, run_config=config,
            product_key=args.product_key, source_workflow_id=args.source_workflow_id,
            owner_user_id=args.owner_user_id, target_date=date.fromisoformat(args.date) if args.date else None)
        print(json.dumps(result, ensure_ascii=False))
        if result.get('action') == 'stop':
            return 2
        wf_id = result.get('workflow_id') or args.source_workflow_id
        _, complete = _advance_until(coordinator, wf_id, advance=coordinator.advance_preparation,
                                     success_action='prepared', max_ticks=args.max_ticks)
        return 0 if complete else 2
    finally:
        conn.close()


def cmd_creative_produce(args):
    """Consume only prepared refs; a shortfall never triggers acquisition."""
    conn, coordinator, config = _catalog_coordinator(args)
    try:
        if 'qc_level' not in config:
            refs = list(args.prepared_ref or [])
            if not refs and getattr(args, 'date', None):
                from datetime import date
                batch_dir = coordinator.importer.find_latest_ready_batch(date.fromisoformat(args.date))
                batch_id = coordinator.importer.load_manifest(batch_dir).batch_id
                for row in conn.execute('SELECT workflow_id, aggregate_json FROM workflow'):
                    aggregate = json.loads(row['aggregate_json'])
                    context = aggregate.get('source_context') or {}
                    if (aggregate.get('prepared_product') and aggregate.get('owner_user_id') == args.owner_user_id
                            and context.get('source_date') == args.date
                            and context.get('source_batch_id') == batch_id):
                        refs.append(row['workflow_id'])
            levels = set()
            for ref in refs:
                saved = coordinator.workflow_repo.get_workflow(ref)
                if saved:
                    levels.add((saved[0].get('creative_policy') or {}).get('qc_level', 'middle'))
            if len(levels) > 1:
                raise ValueError('prepared sources have different QC policies')
            if levels and next(iter(levels)) != 'middle':
                config['qc_level'] = next(iter(levels))
        selection = coordinator.produce_from_catalog(run_id=args.run_id, prepared_refs=args.prepared_ref,
            limit=args.limit, run_config=config, owner_user_id=args.owner_user_id,
            target_date=getattr(args,'date',None))
        print(json.dumps(selection, ensure_ascii=False))
        all_complete = bool(selection['selected'])
        completed_count = 0
        skipped=[]
        unresolved=[]
        for item in selection['selected']:
            result, complete = _advance_until(coordinator, item['workflow_id'],
                advance=coordinator.advance_creative, success_action='completed', max_ticks=args.max_ticks)
            if result.get('state') == 'skipped':
                skipped.append({'workflow_id':item['workflow_id'],
                    'reason':result.get('reason') or result.get('last_error') or 'KNOWN_PRODUCT_FAILURE'})
            elif result.get('state') == 'completed' or result.get('action') == 'completed':
                completed_count += 1
            if not complete:
                unresolved.append({'workflow_id':item['workflow_id'],
                    'reason':result.get('reason') or result.get('last_error') or result.get('code') or result.get('action','tick_limit')})
            all_complete = all_complete and complete
        if unresolved:
            print(json.dumps({'unresolved':unresolved},ensure_ascii=False))
        return 0 if all_complete and completed_count > 0 and not selection['shortfall'] and not unresolved else 2
    finally:
        conn.close()


def cmd_creative_batch(args):
    """Process the latest PI READY batch; explicit bindings remain for scheduler compatibility."""
    import sqlite3
    from datetime import date
    from video_factory.creative.contracts import canonical_digest

    try:
        conn, coordinator, config = _catalog_coordinator(args)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f'Lỗi kỹ thuật: {exc}')
        print('Tổng kết: thành công 0, BLOCKED 0, lỗi kỹ thuật 1.')
        return 2
    try:
        from .creative_policy import normalize_qc_level

        requested_level = getattr(args, 'qc_level', None)
        if requested_level is None and args.run_id:
            prior_id = 'pv-creative-batch-' + hashlib.sha256(args.run_id.encode()).hexdigest()[:24]
            prior = coordinator.workflow_repo.get_workflow(prior_id)
            if prior:
                requested_level = prior[0].get('qc_level')
                if not requested_level:
                    levels = set()
                    for ref in prior[0].get('prepared_refs') or []:
                        child = coordinator.workflow_repo.get_workflow(ref)
                        if child:
                            levels.add(normalize_qc_level((child[0].get('creative_policy') or {}).get('qc_level')))
                    if len(levels) > 1:
                        raise ValueError('saved creative batch QC policies disagree')
                    requested_level = next(iter(levels), 'middle')
        args.qc_level = normalize_qc_level(requested_level or 'high')
        args.disable_keyframe_qc = bool(getattr(args, 'disable_keyframe_qc', False))
        provider_mode = getattr(args, 'provider_mode', 'configured')
        if args.disable_keyframe_qc and args.qc_level != 'low':
            raise ValueError('--disable-keyframe-qc requires --qc-level low')
        if args.qc_level == 'middle' and not args.disable_keyframe_qc:
            config.pop('qc_level', None)
            config.pop('disable_keyframe_qc', None)
        else:
            config['qc_level'] = args.qc_level
            config['disable_keyframe_qc'] = args.disable_keyframe_qc
        args.mode = ('list' if args.product_key else
                     'limit' if args.limit is not None else 'all')
        if args.mode == 'limit' and args.limit < 1:
            raise ValueError('selection limit must be positive')
        if args.mode == 'list' and not args.product_key:
            raise ValueError('specific selection requires at least one --product-key')
        target_date = date.fromisoformat(args.date) if args.date else None
        requested_batch = getattr(args, 'exchange_batch', None)
        batch_dir = requested_batch.resolve() if requested_batch is not None else None
        if batch_dir is not None and (
                not batch_dir.is_relative_to(coordinator.importer.inbox_root.resolve())
                or not (batch_dir / 'READY').is_file()):
            raise ValueError('Requested Exchange batch must be READY and inside the inbox')
        if not args.run_id or target_date is None:
            batch_dir = batch_dir or coordinator.importer.find_latest_ready_batch(target_date)
            initial_manifest = coordinator.importer.load_manifest(batch_dir)
            if not args.date:
                args.date = initial_manifest.production_date
            target_date = date.fromisoformat(args.date)
            if not args.run_id:
                manifest_digest = hashlib.sha256((batch_dir / 'manifest.json').read_bytes()).hexdigest()
                run_binding = {
                    'batch_id':initial_manifest.batch_id,
                    'manifest_sha256':manifest_digest,
                    'mode':args.mode,
                    'limit':args.limit,
                    'product_keys':list(args.product_key or []),
                }
                if args.qc_level != 'middle' or args.disable_keyframe_qc:
                    run_binding.update(qc_level=args.qc_level, disable_keyframe_qc=args.disable_keyframe_qc)
                args.run_id = 'vf-' + hashlib.sha256(canonical_digest(run_binding).encode()).hexdigest()[:32]
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,96}', args.run_id):
            raise ValueError('run ID must contain 1–96 letters, digits, underscore or hyphen')
        selection_id = 'pv-creative-batch-' + hashlib.sha256(args.run_id.encode()).hexdigest()[:24]
        resume_refs = []
        saved = coordinator.workflow_repo.get_workflow(selection_id)
        if saved:
            selection, version = saved
            fingerprint = selection['selection_input_digest']
            expected = canonical_digest({'run_id':args.run_id, 'date':args.date, 'mode':args.mode,
                'limit':args.limit, 'product_keys':args.product_key, 'config':config})
            if fingerprint != expected:
                raise ValueError('batch run ID already bound to different selection or profile')
            batch_dir = Path(selection['batch_dir'])
            if requested_batch is not None and requested_batch.resolve() != batch_dir.resolve():
                raise ValueError('Requested Exchange batch differs from the batch pinned to this run')
            product_keys = list(selection['ordered_product_keys'])
            if not (batch_dir / 'READY').is_file() or not (batch_dir / 'manifest.json').is_file():
                raise ValueError('pinned READY batch is no longer available; refusing to switch batches')
            manifest_bytes = (batch_dir / 'manifest.json').read_bytes()
            manifest = coordinator.importer.load_manifest(batch_dir)
            if (hashlib.sha256(manifest_bytes).hexdigest() != selection.get('manifest_sha256')
                    or manifest.batch_id != selection.get('batch_id')
                    or manifest.production_date != args.date):
                raise ValueError('pinned READY manifest changed; refusing to switch batch contents')
            profile_error = _profile_category_error(config, manifest.products, product_keys)
            if profile_error:
                print(f'Lỗi cấu hình: {profile_error}')
                print('Tổng kết: thành công 0, BLOCKED 0, lỗi kỹ thuật 1.')
                return 2
            refreshed_skips = []
            for item in selection.get('skipped', []):
                if item.get('workflow_id') and item.get('reason', '').startswith('TERMINAL_WORKFLOW:'):
                    from video_factory.application.operator_handler import can_resume_legacy_invalid_response_skip
                    source = coordinator.workflow_repo.get_workflow(item['workflow_id'])
                    context = (source[0].get('source_context') or {}) if source else {}
                    if (can_resume_legacy_invalid_response_skip(conn, item['workflow_id'])
                            and context.get('source_batch_id') == selection['batch_id']
                            and context.get('source_date') == args.date):
                        resume_refs.append(item['workflow_id'])
                        selection.setdefault('recovery_history', []).append({
                            **item, 'action':'resume_same_workflow_from_recorded_script_failure'})
                        continue
                if (item.get('reason') == 'COMPLETED_FINAL_NOT_CURRENT' or str(item.get('reason', '')).startswith('FINAL_TECHNICAL_REVALIDATION_FAILED')) and item.get('workflow_id'):
                    outcome = coordinator.revalidate_completed_final(item['workflow_id'])
                    reason = ('CURRENT_ACCEPTED_FINAL_EXISTS' if outcome.get('status') == 'accepted'
                              else outcome.get('reason') or outcome.get('status','FINAL_RECHECK_UNRESOLVED'))
                    refreshed_skips.append({**item, **outcome, 'reason':reason})
                elif args.mode == 'list' and item.get('workflow_id') and str(item.get('reason', '')).startswith('ACTIVE_IDENTITY_VARIANT:'):
                    resume_refs.append(item['workflow_id'])
                    selection.setdefault('recovery_history', []).append({
                        **item, 'action': 'resume_same_workflow_from_active_selection'})
                    continue
                else:
                    refreshed_skips.append(item)
            if refreshed_skips != selection.get('skipped', []) or resume_refs:
                selection['skipped'] = refreshed_skips
                selection['resume_refs'] = list(dict.fromkeys([*selection.get('resume_refs', []), *resume_refs]))
                coordinator.workflow_repo.commit_workflow(selection_id,json.dumps(selection,ensure_ascii=False),[],
                    expected_version=version)
        else:
            batch_dir = batch_dir or coordinator.importer.find_latest_ready_batch(target_date)
            manifest = coordinator.importer.load_manifest(batch_dir)
            if manifest.production_date != args.date:
                raise ValueError('READY batch production date does not match requested date')
            manifest_sha256 = hashlib.sha256((batch_dir / 'manifest.json').read_bytes()).hexdigest()
            by_key = {product.product_key:product for product in manifest.products}
            candidates = ([by_key[key] for key in args.product_key if key in by_key]
                          if args.mode == 'list' else
                          sorted(manifest.products, key=lambda item:item.priority))
            if args.mode == 'list':
                missing = [key for key in args.product_key if key not in by_key]
                candidate_skips = [{'product_key':key, 'reason':'NOT_FOUND_IN_MANIFEST'} for key in missing]
            else:
                candidate_skips = []
            eligible = []
            for product in candidates:
                prior = coordinator.catalog_product_outcome(product.canonical_product_key, product.variant)
                if prior:
                    if (prior['reason'] in {'RESUMABLE_LEGACY_CREATIVE_FAILURE','RESUMABLE_CREATIVE_FAILURE'}
                            or (args.mode == 'list' and prior['reason'].startswith('ACTIVE_IDENTITY_VARIANT:'))):
                        source = coordinator.workflow_repo.get_workflow(prior['workflow_id'])
                        context = (source[0].get('source_context') or {}) if source else {}
                        valid, reason, _ = coordinator.importer.validate_admission(
                            product, batch_dir, qc_level=args.qc_level)
                        catalog_ok, catalog_reason = coordinator.evaluate_catalog_candidate(product, batch_dir, None)
                        if (context.get('source_batch_id') == manifest.batch_id and context.get('source_date') == args.date
                                and valid and catalog_ok):
                            eligible.append(product.product_key)
                        else:
                            candidate_skips.append({'product_key':product.product_key,
                                'workflow_id':prior['workflow_id'], 'reason':reason if not valid else
                                catalog_reason or 'RESUMABLE_WORKFLOW_SOURCE_BATCH_MISMATCH'})
                        continue
                    if prior['reason'] == 'COMPLETED_FINAL_NOT_CURRENT':
                        recheck = coordinator.revalidate_completed_final(prior['workflow_id'])
                        if recheck.get('status') == 'accepted':
                            candidate_skips.append({'product_key':product.product_key,
                                'workflow_id':prior['workflow_id'], 'reason':'CURRENT_ACCEPTED_FINAL_EXISTS',
                                'sha256':recheck.get('sha256')})
                            continue
                        if recheck.get('status') == 'skipped':
                            candidate_skips.append({'product_key':product.product_key, **recheck})
                            continue
                        candidate_skips.append({'product_key':product.product_key, **recheck})
                        continue
                    candidate_skips.append({'product_key':product.product_key, **prior})
                    continue
                valid, reason, _ = coordinator.importer.validate_admission(
                    product, batch_dir, qc_level=args.qc_level)
                catalog_ok, catalog_reason = coordinator.evaluate_catalog_candidate(product, batch_dir, None)
                if not valid or not catalog_ok:
                    candidate_skips.append({'product_key':product.product_key,
                        'reason':reason if not valid else catalog_reason or 'CATALOG_NOT_ELIGIBLE'})
                    continue
                eligible.append(product.product_key)
            # Áp dụng limit chặt chẽ cho mode 'limit', kể cả khi replay
            if args.mode == 'limit' and args.limit is not None:
                product_keys = eligible[:args.limit]
            elif args.mode == 'list':
                product_keys = eligible
            else:  # 'all'
                product_keys = eligible
            profile_error = _profile_category_error(config, manifest.products, product_keys)
            if profile_error:
                print(f'Lỗi cấu hình: {profile_error}')
                print('Tổng kết: thành công 0, BLOCKED 0, lỗi kỹ thuật 1.')
                return 2
            if not product_keys:
                if not candidate_skips:
                    raise ValueError('the pinned READY batch has no selected eligible products')
            expected = canonical_digest({'run_id':args.run_id, 'date':args.date, 'mode':args.mode,
                'limit':args.limit, 'product_keys':args.product_key, 'config':config})
            selection = {'state':'pinned', 'workflow_kind':'creative_batch_selection',
                'qc_level':args.qc_level,
                'selection_input_digest':expected, 'batch_dir':str(batch_dir), 'batch_id':manifest.batch_id,
                'manifest_sha256':manifest_sha256,
                'source_date':manifest.production_date, 'ordered_product_keys':product_keys,
                'prepared_refs':[], 'resume_refs':[], 'skipped':candidate_skips}
            coordinator.workflow_repo.commit_workflow(selection_id,json.dumps(selection,ensure_ascii=False),[],
                expected_version=0)
            version = 1
        if not (batch_dir / 'READY').is_file() or not (batch_dir / 'manifest.json').is_file():
            raise ValueError('pinned READY batch is no longer available; refusing to switch batches')

        policy = coordinator._catalog_run_policy(config, preparation_id=args.run_id,
                                                  product_key=selection_id)
        recovered_refs = list(dict.fromkeys([*(selection.get('prepared_refs') or []),
                                               *(selection.get('resume_refs') or [])]))
        remaining_resume_refs = list(selection.get('resume_refs') or [])
        skipped = list(selection.get('skipped') or [])
        started_by_key = {}
        for offset in range(0, len(product_keys), 5):
            result = coordinator.start_selection(product_keys[offset:offset+5], owner_user_id=args.owner_user_id,
                target_date=target_date, execution_mode='auto', max_qc_retries=2, creative_policy=policy,
                qc_authorization=config.get('qc_authorization'), ready_batch_dir=batch_dir)
            for item in result.started:
                started_by_key[item.product_key] = item.workflow_id
            for item in result.skipped:
                if item.workflow_id and item.reason == 'ALREADY_STARTED':
                    started_by_key[item.product_key] = item.workflow_id
                else:
                    skipped.append({'product_key':item.product_key, 'workflow_id':item.workflow_id,
                                    'reason':item.reason})
            current = coordinator.workflow_repo.get_workflow(selection_id)
            selection, version = current
            batch_refs = []
            for ref in dict.fromkeys([*recovered_refs, *started_by_key.values()]):
                source = coordinator.workflow_repo.get_workflow(ref)
                context = (source[0].get('source_context') or {}) if source else {}
                if context.get('source_batch_id') != selection['batch_id'] or context.get('source_date') != args.date:
                    skipped.append({'workflow_id':ref, 'reason':'ALREADY_STARTED_OUTSIDE_PINNED_BATCH'})
                    continue
                batch_refs.append(ref)
            selection['prepared_refs'] = batch_refs
            selection['skipped'] = skipped
            coordinator.workflow_repo.commit_workflow(selection_id,json.dumps(selection,ensure_ascii=False),[],
                expected_version=version)
            recovered_refs = selection['prepared_refs']

        prepared_refs = []
        shared_blocker = None
        preparation_unresolved = []
        for ref in recovered_refs:
            current_workflow = coordinator.workflow_repo.get_workflow(ref)
            if current_workflow and current_workflow[0].get('production_run_id') == 'pv-production-' + args.run_id:
                prepared_refs.append(ref)
                remaining_resume_refs = [item for item in remaining_resume_refs if item != ref]
                continue
            if current_workflow and current_workflow[0].get('state') in {'completed','skipped','contract_broken'}:
                from video_factory.application.operator_handler import can_resume_legacy_invalid_response_skip
                if (current_workflow[0].get('state') == 'skipped'
                        and can_resume_legacy_invalid_response_skip(conn, ref)):
                    pass
                else:
                    state = current_workflow[0]
                    terminal = {'workflow_id':ref, 'reason':state.get('skip_reason') or
                        state.get('failure_code') or f"TERMINAL_WORKFLOW:{state.get('state')}"}
                    prior = next((item for item in skipped if item.get('workflow_id') == ref), None)
                    if prior:
                        prior.update(terminal)
                    else:
                        skipped.append(terminal)
                    remaining_resume_refs = [item for item in remaining_resume_refs if item != ref]
                    continue
            preparation_id = ((current_workflow[0].get('creative_policy') or {}).get('preparation_id')
                              if current_workflow else None) or args.run_id
            try:
                prepared = coordinator.prepare_catalog(preparation_id=preparation_id, run_config=config,
                    source_workflow_id=ref, owner_user_id=args.owner_user_id, target_date=target_date)
            except ValueError as exc:
                preparation_unresolved.append({'workflow_id':ref,
                    'reason':f"PREPARATION_RESUME_BLOCKED: {str(exc)[:400]}"})
                remaining_resume_refs = [item for item in remaining_resume_refs if item != ref]
                continue
            if prepared.get('action') == 'stop':
                skipped.append({'workflow_id':ref, 'reason':prepared.get('skipped') or 'PREPARATION_BLOCKED'})
                continue
            final, complete = _advance_until(coordinator, ref,
                advance=lambda wid: coordinator.advance_preparation(wid, provider_mode=provider_mode),
                success_action='prepared', max_ticks=args.max_ticks, emit_progress=False)
            if final.get('state') == 'skipped':
                terminal = {'workflow_id':ref, 'reason':final.get('reason') or 'KNOWN_PRODUCT_FAILURE'}
                prior = next((item for item in skipped if item.get('workflow_id') == ref), None)
                if prior:
                    prior.update(terminal)
                else:
                    skipped.append(terminal)
                remaining_resume_refs = [item for item in remaining_resume_refs if item != ref]
            elif complete:
                prepared_refs.append(ref)
                remaining_resume_refs = [item for item in remaining_resume_refs if item != ref]
            else:
                reason = (final.get('reason') or final.get('last_error') or final.get('code')
                          or final.get('action','PREPARATION_UNRESOLVED'))
                preparation_unresolved.append({'workflow_id':ref, 'reason':reason})
                from video_factory.workflows.runtime_control import is_shared_infra_blocker
                if final.get('action') == 'blocker' and is_shared_infra_blocker(final.get('code') or reason):
                    shared_blocker = reason
                    break
        current = coordinator.workflow_repo.get_workflow(selection_id)
        selection, version = current
        prepared_set = set(prepared_refs)
        prepared_keys = {key for key, workflow_id in started_by_key.items() if workflow_id in prepared_set}
        skipped = [item for item in skipped
                   if str(item.get('workflow_id') or '') not in prepared_set
                   and str(item.get('product_key') or '') not in prepared_keys]
        selection.update(state='prepared', prepared_refs=prepared_refs,
                         resume_refs=remaining_resume_refs, skipped=skipped,
                         unresolved=preparation_unresolved)
        coordinator.workflow_repo.commit_workflow(selection_id,json.dumps(selection,ensure_ascii=False),[],
            expected_version=version)
        if shared_blocker:
            counts = _print_batch_outcomes(coordinator, manifest, product_keys, started_by_key,
                skipped, preparation_unresolved, shared_blocker=shared_blocker)
            return 0 if counts['technical_errors'] == 0 and counts['completed'] + counts['blocked'] else 2
        if not prepared_refs:
            counts = _print_batch_outcomes(coordinator, manifest, product_keys, started_by_key,
                skipped, preparation_unresolved)
            return 0 if counts['technical_errors'] == 0 and counts['completed'] + counts['blocked'] else 2

        limit = None if args.mode == 'all' else (args.limit if args.mode == 'limit' else len(prepared_refs))
        production = coordinator.produce_from_catalog(run_id=args.run_id, prepared_refs=prepared_refs,
            limit=limit, run_config=config, owner_user_id=args.owner_user_id, target_date=args.date,
            target_batch_id=selection['batch_id'], provider_mode=provider_mode)
        unresolved = list(preparation_unresolved)
        production_skips = list(production.get('skipped') or [])
        for item in production_skips:
            reason = str(item.get('reason') or 'PRODUCTION_SELECTION_SKIPPED')
            outcome = {'workflow_id':item.get('prepared_ref'), 'reason':reason}
            if reason.startswith((
                'ACTIVE_IDENTITY_VARIANT:', 'BLOCKED_UNCERTAIN_OR_COMPLETED',
                'ALREADY_CLAIMED', 'ACTIVE_OR_UNCERTAIN_WORKFLOW', 'PREPARED_CHANGED_DURING_CLAIM',
                'PREPARED_STALE:', 'PROFILE_STALE:', 'PREPARED_NOT_PUBLISHED',
            )):
                unresolved.append(outcome)
            else:
                skipped.append(outcome)
        if not production['selected'] and not production_skips:
            unresolved.append({'workflow_id':selection_id, 'reason':'NO_ELIGIBLE_PRODUCTION_SELECTION'})
        current = coordinator.workflow_repo.get_workflow(selection_id)
        selection, version = current
        selection['state'] = 'producing'
        selection['production_selected_refs'] = [item['prepared_ref'] for item in production['selected']]
        coordinator.workflow_repo.commit_workflow(selection_id,json.dumps(selection,ensure_ascii=False),[],
            expected_version=version)
        for item in production['selected']:
            result, complete = _advance_until(coordinator, item['workflow_id'],
                advance=lambda wid: coordinator.advance_creative(wid, provider_mode=provider_mode),
                success_action='completed', max_ticks=args.max_ticks, emit_progress=False)
            if result.get('state') == 'skipped':
                skipped.append({'workflow_id':item['workflow_id'],
                    'reason':result.get('reason') or result.get('last_error') or 'KNOWN_PRODUCT_FAILURE'})
            if not complete:
                unresolved.append({'workflow_id':item['workflow_id'], 'reason':result.get('reason') or
                    result.get('last_error') or result.get('code') or result.get('action','TICK_LIMIT')})
            from video_factory.workflows.runtime_control import is_shared_infra_blocker
            if result.get('action') == 'blocker' and is_shared_infra_blocker(result.get('code') or result.get('reason')):
                break
        counts = _print_batch_outcomes(coordinator, manifest, product_keys, started_by_key,
            skipped, unresolved, shared_blocker=shared_blocker)
        return 0 if counts['technical_errors'] == 0 and counts['completed'] + counts['blocked'] else 2
    finally:
        conn.close()


def _profile_category_error(config, products, product_keys):
    """Describe an incompatible food profile before pinning or dispatch."""
    import unicodedata

    selected = set(product_keys)
    food_markers = ('food', 'snack', 'do an', 'thuc pham', 'do uong', 'banh', 'keo', 'mi an lien', 'dac san')
    food_products = []
    for product in products:
        if product.product_key not in selected:
            continue
        category = unicodedata.normalize('NFKD', product.category.casefold().replace('đ', 'd'))
        category = category.encode('ascii', 'ignore').decode('ascii')
        if any(marker in category for marker in food_markers):
            food_products.append(product.name)
    if food_products and config.get('category') != 'food_pov':
        names = ', '.join(food_products[:3])
        return (
            f"Selected food product(s) ({names}) require a food_pov profile; "
            "pass --profile data/run_profiles/bibigo-food-pov-20261025-v2.json "
            "or set VF_CREATIVE_RUN_CONFIG before starting a new batch"
        )
    return None


def run_studio_selected_batch(*, db_path, inbox_root, profile_path, product_keys,
                              target_date=None, owner_user_id='local-owner', limit=None,
                              qc_level='high', disable_keyframe_qc=False):
    """Run Studio selections through the same durable path as creative-batch."""
    from argparse import Namespace

    selected_keys = list(product_keys or [])
    args = Namespace(
        run_id=None,
        date=target_date,
        product_key=selected_keys or None,
        limit=limit if not selected_keys else None,
        profile=Path(profile_path),
        owner_user_id=owner_user_id,
        max_ticks=100,
        db_path=Path(db_path),
        inbox_root=Path(inbox_root),
        qc_level=qc_level,
        disable_keyframe_qc=disable_keyframe_qc,
    )
    return cmd_creative_batch(args)


def cmd_creative_run(args):
    conn, coordinator, config = _catalog_coordinator(args)
    try:
        repository = getattr(coordinator, 'workflow_repo', None)
        existing = repository.get_workflow('pv-creative-' + args.run_id) if repository else None
        if not existing:
            config.setdefault('qc_level', 'high')
        elif 'qc_level' in (existing[0].get('creative_policy') or {}):
            config.setdefault('qc_level', existing[0]['creative_policy']['qc_level'])
        started=coordinator.start_fresh_creative(run_id=args.run_id,query=args.query,run_config=config,
            owner_user_id=args.owner_user_id,qc_authorization=config.get('qc_authorization'))
        _, complete = _advance_until(coordinator, started['workflow_id'], advance=coordinator.advance_creative,
            success_action='completed', max_ticks=args.max_ticks)
        return 0 if complete else 2
    finally:
        conn.close()
