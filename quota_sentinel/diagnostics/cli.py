"""Inspect private trace metadata without initializing provider credentials."""
import json
import time

from .trace import TraceJournal, process_identity


def register(subparsers):
    command = subparsers.add_parser('trace', help='inspect authentication/process trace metadata')
    actions = command.add_subparsers(dest='trace_action', required=True)
    actions.add_parser('status')
    actions.add_parser('cleanup')
    show = actions.add_parser('show')
    show.add_argument('--limit', type=int, default=100)
    show.add_argument('--call-id')
    command.set_defaults(handler=run)


def run(args):
    journal = TraceJournal(args.state_dir/'diagnostics')
    if args.trace_action == 'show':
        for record in journal.records(limit=args.limit, call_id=args.call_id):
            print(json.dumps(record, ensure_ascii=True))
    elif args.trace_action == 'cleanup':
        print(json.dumps(journal.cleanup()))
    else:
        files = journal._files()
        sizes = []
        for path in files:
            try: sizes.append(path.stat().st_size)
            except FileNotFoundError: pass  # Concurrent retention removed it.
        observer = {'active': False, 'coverage': 'unavailable'}
        try:
            from quota_sentinel.platform.files import private_open
            with private_open(journal.directory/'observer-status.json', 'rb') as stream:
                saved = json.loads(stream.read(4096))
            if isinstance(saved, dict):
                observer = {key: saved[key] for key in ('active', 'coverage', 'updated_at', 'observer_pid', 'endpoint_security') if key in saved}
                identity = process_identity(saved.get('observer_pid', 0))
                observer['active'] = bool(saved.get('active') and time.time()-saved.get('updated_at', 0)<90
                    and identity['identity_known'] and identity['start_time']==saved.get('observer_start_time'))
                if saved.get('active') and not observer['active']:
                    observer['coverage'] = 'stale'
        except (OSError, ValueError, TypeError):
            pass
        print(json.dumps({'directory': str(journal.directory), 'retention_days': journal.retention_days,
            'max_bytes': journal.max_bytes, 'chunk_bytes': journal.chunk_bytes,
            'retained_files':len(sizes), 'retained_bytes':sum(sizes),
            'project_attribution':'owned-process-pid-and-request-chain', 'system_observer':observer,
            'endpoint_security':'requires-administrator'}))
    return 0
