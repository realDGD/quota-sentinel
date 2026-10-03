"""Atomic activation metadata; never acknowledge a newer configuration edit."""
import hashlib
import json
from pathlib import Path
from .store import _publish_atomic

PROVIDERS=('codex','antigravity','opencode','clinepass')
def _provider_list(value):
    return isinstance(value,list) and all(isinstance(p,str) and p in PROVIDERS for p in value) and len(value)==len(set(value))

def read_runtime_providers(path):
    try:record=json.loads(path.read_bytes())
    except FileNotFoundError:return None
    if (not isinstance(record,dict) or type(record.get('schema_version')) is not int or record['schema_version']!=1
            or not _provider_list(record.get('enabled')) or not _provider_list(record.get('awaiting_resume'))):
        raise ValueError('invalid provider activation metadata')
    return record


def publish_bytes(path, payload):
    _publish_atomic(path, payload)


def read_journal(path):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return set(), None
    record = json.loads(raw)
    if (not isinstance(record, dict) or type(record.get('schema_version')) is not int or record['schema_version'] != 1
            or not _provider_list(record.get('pending'))):
        raise ValueError('invalid activation journal')
    if 'source_journal' in record or 'source_revision' in record:
        source = record.get('source_journal')
        revision = record.get('source_revision')
        if (not isinstance(source, str) or not source or '\x00' in source
                or not Path(source).is_absolute()
                or Path(source).resolve().parent == path.resolve().parent
                or 'source_revision' not in record
                or revision is not None and (not isinstance(revision, str)
                    or len(revision) != 64 or any(c not in '0123456789abcdef' for c in revision))):
            raise ValueError('invalid applied activation journal')
    return set(record['pending']), hashlib.sha256(raw).hexdigest()


def persist_activation(state_dir, opening, awaiting, journal, observed_revision):
    from quota_sentinel.config.edit_lock import configuration_lock
    payload = json.dumps({'schema_version': 1, 'enabled': list(opening),
                          'awaiting_resume': sorted(awaiting)}).encode()
    _publish_atomic(state_dir / 'runtime-providers.json', payload)
    with configuration_lock(journal.parent / 'configuration.lock'):
        pending, current = read_journal(journal)
        if current is not None and current == observed_revision:
            record = json.loads(journal.read_bytes())
            record['pending'] = sorted(pending - (set(opening) - set(awaiting)))
            acknowledged = pending - set(record['pending'])
            if acknowledged and 'source_journal' in record:
                source = Path(record['source_journal'])
                with configuration_lock(source.parent / 'configuration.lock'):
                    source_pending, source_revision = read_journal(source)
                    if source_revision is not None and source_revision == record['source_revision']:
                        saved = json.loads(source.read_bytes())
                        saved['pending'] = sorted(source_pending - acknowledged)
                        saved_raw = json.dumps(saved).encode()
                        _publish_atomic(source, saved_raw)
                        # Keep the CAS token current as providers resume on
                        # different ticks within this applied generation.
                        record['source_revision'] = hashlib.sha256(saved_raw).hexdigest()
            raw = json.dumps(record).encode()
            _publish_atomic(journal, raw)
            return hashlib.sha256(raw).hexdigest()
    return observed_revision
