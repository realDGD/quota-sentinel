"""Atomic activation metadata; never acknowledge a newer configuration edit."""
import hashlib
import json
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
            raw = json.dumps(record).encode()
            _publish_atomic(journal, raw)
            return hashlib.sha256(raw).hexdigest()
    return observed_revision
