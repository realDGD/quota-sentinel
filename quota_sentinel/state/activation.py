"""Atomic activation metadata; never acknowledge a newer configuration edit."""
import hashlib
import json
from .store import _publish_atomic


def publish_bytes(path, payload):
    _publish_atomic(path, payload)


def read_journal(path):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return set(), None
    record = json.loads(raw)
    if (not isinstance(record, dict) or record.get('schema_version') != 1
            or not isinstance(record.get('pending'), list)
            or any(p not in ('codex', 'antigravity', 'opencode', 'clinepass') for p in record['pending'])):
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
