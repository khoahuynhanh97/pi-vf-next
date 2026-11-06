"""Explicit Flow maintenance inspection/status/repair without acquisition."""
import hashlib
import json
from pathlib import Path


def cmd_flow_repair(args):
    from video_factory.constants import get_hermes_home
    from video_factory.storage_infra.sqlite import connect
    from .browser import inspect_flow, sanitize
    from .ledger import RepairLedger
    from .service import RepairService
    from .patching import DockerValidator
    db = Path(args.db_path) if args.db_path else get_hermes_home() / 'hermes.db'
    connection = connect(db)
    try:
        ledger = RepairLedger(connection)
        if args.repair_action == 'status':
            result = {'incidents': ledger.list_incidents()}
        else:
            if not args.profile_path or not args.url:
                raise ValueError('--profile-path and --url required for inspection')
            root = Path(__file__).resolve().parents[3]
            output = Path(args.output) if args.output else get_hermes_home() / 'flow_ui_maintenance'
            profile_id = hashlib.sha256(str(Path(args.profile_path).resolve()).encode()).hexdigest()
            if args.incident_id:
                incident = ledger.get(args.incident_id)
                if incident['profile_id'] != profile_id:
                    raise ValueError('profile does not match incident')
                if incident['details'].get('profile_directory', 'Default') != args.profile_directory:
                    raise ValueError('profile directory does not match incident')
            else:
                incident = ledger.incident(workflow_id='maintenance-inspection', operation_id='not-submitted',
                    profile_id=profile_id, error_code=args.error_code, fingerprint=args.error_code)
                ledger.update(incident['incident_id'], profile_directory=args.profile_directory)
            grant = json.loads(Path(args.grant).read_text(encoding='utf8')) if args.grant and args.repair_action == 'repair' else None
            service = RepairService(ledger, root, output,
                inspect=lambda i: inspect_flow(args.profile_path, args.profile_directory, args.url, output / i['incident_id']),
                validator=DockerValidator(root, image=args.validation_image))
            result = service.run(incident['incident_id'], grant, revise=args.revise)
        # Status contains operational hashes/proposal references, never raw source.
        if 'incidents' in result:
            result['incidents'] = [{k: i[k] for k in ('incident_id', 'workflow_id', 'operation_id', 'status', 'error_code')}
                for i in result['incidents']]
        public = sanitize(result)
        if 'incident_id' in result:
            public['incident_id'] = result['incident_id']
        if 'incidents' in result:
            public['incidents'] = result['incidents']
        print(json.dumps(public, ensure_ascii=False))
        return 0
    except (ValueError, KeyError, OSError, TypeError) as exc:
        print(json.dumps({'status': 'blocked', 'error': sanitize(str(exc))}, ensure_ascii=False))
        return 2
    finally:
        connection.close()


def add_parser(subparsers):
    parser = subparsers.add_parser('flow-repair', help='Inspect/status/repair Flow UI without new product acquisition')
    parser.add_argument('repair_action', choices=['inspect', 'status', 'repair'])
    parser.add_argument('--incident-id')
    parser.add_argument('--profile-path')
    parser.add_argument('--profile-directory', default='Default')
    parser.add_argument('--url')
    parser.add_argument('--output')
    parser.add_argument('--grant', help='Explicit maintenance-only model-request grant JSON')
    parser.add_argument('--revise', action='store_true', help='One bounded corrective coding attempt after an invalid/failed patch')
    parser.add_argument('--validation-image', help='Already provisioned digest-pinned offline validation image')
    parser.add_argument('--error-code', default='GOOGLE_FLOW_UI_UNVERIFIED')
    parser.set_defaults(func=cmd_flow_repair)
