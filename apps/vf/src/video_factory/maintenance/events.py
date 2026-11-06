"""Cost-free worker failure capture. Uncertain operations are never resubmitted."""
import hashlib
import json
import re
import time

from .policy import is_ui_mismatch
from .service import record_ui_failure


def observe_failure(connection, claim, error, status):
    if status != 'quarantined':
        return None
    codes = re.findall(r'\bGOOGLE_FLOW_[A-Z0-9_]+\b', str(error))
    code = next((value for value in codes if is_ui_mismatch(value)), None)
    if not code:
        return None
    from video_factory.integrations.google_flow_keyframe_provider import GoogleFlowKeyframeProvider
    provider = GoogleFlowKeyframeProvider.from_config()
    profile_id = hashlib.sha256(str(provider.profile_path).encode()).hexdigest()
    # The outbox identity is an incident correlation key, not a new provider op.
    operation_id = f'outbox:{claim.outbox_id}'
    fingerprint = hashlib.sha256(code.encode()).hexdigest()
    incident = record_ui_failure(connection, workflow_id=claim.workflow_id, operation_id=operation_id,
        profile_id=profile_id, error_code=code, fingerprint=fingerprint)
    from .ledger import RepairLedger
    return RepairLedger(connection).update(incident['incident_id'], outbox_id=claim.outbox_id,
        profile_directory=provider.chrome_profile_directory,
        operation_phase='uncertain', reason='provider_phase_must_be_reconciled_from_authoritative_ledger')


def configured_maintenance(connection):
    """Run one eligible incident after browser release, only with explicit config."""
    from video_factory.config import load_config
    config = load_config().get('video_factory', {}).get('ui_repair', {})
    if config.get('enabled') is not True:
        return None
    from pathlib import Path
    from .browser import inspect_flow
    from .ledger import RepairLedger
    from .service import RepairService
    from .patching import DockerValidator
    ledger = RepairLedger(connection)
    pending = [i for i in ledger.list_incidents() if i['status'] in {'detected', 'profile_busy',
        'inspection_configuration_required', 'authorization_required', 'validation_unavailable', 'coding_unavailable',
        'coding_invalid', 'patch_rejected', 'validation_failed', 'inspection_blocked', 'inspection_failed'}]
    pending = [i for i in pending if i['details'].get('retry_after', 0) <= time.time()]
    if not pending:
        return None
    incident = pending[0]
    root = Path(__file__).resolve().parents[3]
    artifacts = root / 'data' / 'flow_ui_maintenance'
    profile = config.get('profile_path')
    url = config.get('inspection_url')
    if not profile or not url:
        return ledger.update(incident['incident_id'], status='inspection_configuration_required')
    profile_id = hashlib.sha256(str(Path(profile).resolve()).encode()).hexdigest()
    if (profile_id != incident['profile_id'] or config.get('profile_directory', 'Default') !=
            incident['details'].get('profile_directory', 'Default')):
        return ledger.update(incident['incident_id'], status='inspection_configuration_required', reason='incident_profile_mismatch')
    grant_path = config.get('grant_path')
    grant = json.loads(Path(grant_path).read_text(encoding='utf8')) if grant_path else None
    service = RepairService(ledger, root, artifacts,
        inspect=lambda i: inspect_flow(profile, config.get('profile_directory', 'Default'), url, artifacts / i['incident_id']),
        validator=DockerValidator(root, image=config.get('validation_image')))
    return service.run(incident['incident_id'], grant, revise=incident['status'] in {'coding_invalid', 'patch_rejected', 'validation_failed'})
