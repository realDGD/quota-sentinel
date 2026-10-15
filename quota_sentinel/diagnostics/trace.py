"""Private process attribution with bounded JSONL retention.

Only approved metadata is retained. Raw argv, URLs, CLI logs and credentials
never enter this journal. Its own fd lock is independent of scheduler locks.
"""
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import ctypes
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import uuid

TRACE_DIR = 'QUOTA_SENTINEL_TRACE_DIR'
TRACE_ROOT = 'QUOTA_SENTINEL_TRACE_ROOT'
TRACE_CALL = 'QUOTA_SENTINEL_TRACE_CALL'
TRACE_PARENT = 'QUOTA_SENTINEL_TRACE_PARENT'
TRACE_KIND = 'QUOTA_SENTINEL_TRACE_KIND'
TRACE_TRIGGER = 'QUOTA_SENTINEL_TRACE_TRIGGER'
_CONTEXT = ContextVar('quota_sentinel_trace', default={})
_FILE = re.compile(r'trace-(\d{8})-(\d{6})\.jsonl\Z')
_ID = re.compile(r'[0-9a-f]{32}\Z')
_FIELDS = frozenset(('call_id', 'root_id', 'parent_id', 'trigger', 'kind',
    'target_pid', 'target_identity', 'requested_executable', 'exit_code',
    'elapsed', 'marker', 'reason', 'action', 'guard', 'phase', 'attempt',
    'limit', 'attribution', 'caller_pid', 'caller_identity', 'emitter_pid',
    'event_kind', 'url_class', 'observer', 'coverage', 'source_timestamp',
    'ancestors', 'children', 'task_run_id', 'fresh', 'count', 'dropped_before', 'previous_failure_call'))
_IDENTITY_FIELDS = ('pid', 'ppid', 'executable', 'start_time', 'identity_known', 'observed_at')


def process_identity(pid=None):
    """Resolve native identity without reading command arguments."""
    pid = os.getpid() if pid is None else int(pid)
    result = {'pid': pid, 'ppid': None, 'executable': None,
              'start_time': None, 'identity_known': False}
    try:
        if sys.platform == 'darwin':
            from quota_sentinel.platform.posix_process import _mac_info
            info = _mac_info(pid)
            if info is None:
                return result
            library = ctypes.CDLL('/usr/lib/libproc.dylib')
            buffer = ctypes.create_string_buffer(136)
            library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
            if library.proc_pidinfo(pid, 3, 0, buffer, 136) != 136:
                return result
            import struct
            result['ppid'] = struct.unpack_from('=I', buffer.raw, 16)[0]
            path = ctypes.create_string_buffer(4096)
            if library.proc_pidpath(pid, path, len(path)) > 0:
                result['executable'] = os.fsdecode(path.value)
            result['start_time'] = '%d.%06d' % info[1]
            after = _mac_info(pid)
            if after is None or after[1] != info[1]:
                return dict(pid=pid, ppid=None, executable=None, start_time=None, identity_known=False)
        elif sys.platform.startswith('linux'):
            directory = Path('/proc')/str(pid)
            fields = (directory/'stat').read_text().rsplit(')', 1)[1].split()
            result['ppid'] = int(fields[1])
            result['start_time'] = fields[19]
            result['executable'] = os.readlink(directory/'exe')
        elif pid == os.getpid():
            result['ppid'] = os.getppid()
            result['executable'] = sys.executable
        result['identity_known'] = result['start_time'] is not None
    except (OSError, ValueError, IndexError):
        pass
    return result


def ancestors():
    result = []
    pid = os.getppid()
    seen = {os.getpid()}
    while pid > 0 and pid not in seen and len(result) < 8:
        seen.add(pid)
        identity = process_identity(pid)
        result.append(identity)
        pid = identity['ppid'] or 0
    return result


def _metadata(value):
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:1024]
    if isinstance(value, dict):
        return {k: _metadata(value[k]) for k in _IDENTITY_FIELDS if k in value}
    if isinstance(value, (list, tuple)):
        return [_metadata(item) for item in value[:8] if isinstance(item, dict)]
    return None


class TraceJournal:
    def __init__(self, directory, *, retention_days=14, max_bytes=50*1024*1024,
                 chunk_bytes=2*1024*1024, clock=time.time):
        self.directory = Path(directory)
        if retention_days < 1 or max_bytes < 512 or chunk_bytes < 512:
            raise ValueError('invalid diagnostic retention limits')
        self.retention_days, self.max_bytes = retention_days, max_bytes
        self.chunk_bytes, self.clock = min(chunk_bytes, max_bytes), clock
        self.dropped = 0
        self._warned = False

    @contextmanager
    def _locked(self):
        from quota_sentinel.platform.files import private_directory
        from quota_sentinel.platform.locks import acquire_lock
        private_directory(self.directory)
        protocol = 'windows-range-v1' if os.name == 'nt' else 'posix-fd-v1'
        with acquire_lock(self.directory/'trace.lock', protocol=protocol, timeout=.25, poll=.005):
            yield

    def _files(self):
        result = []
        if not self.directory.is_dir():
            return result
        import stat
        for path in self.directory.iterdir():
            if not _FILE.fullmatch(path.name):
                continue
            try: info = path.lstat()
            except FileNotFoundError: continue
            if stat.S_ISREG(info.st_mode) and (os.name == 'nt' or info.st_uid == os.getuid()):
                result.append(path)
        return sorted(result)

    def _prune(self, now):
        self._expire_auth_state(now)
        cutoff = (datetime.fromtimestamp(now, timezone.utc)-timedelta(days=self.retention_days-1)).strftime('%Y%m%d')
        removed = 0
        files = self._files()
        for path in files[:]:
            if _FILE.fullmatch(path.name)[1] < cutoff:
                path.unlink();files.remove(path);removed += 1
        total = sum(path.stat().st_size for path in files)
        while total > self.max_bytes and files:
            path = files.pop(0)
            total -= path.stat().st_size
            path.unlink();removed += 1
        return {'removed_files': removed, 'retained_bytes': total,
                'retained_files': len(files)}

    def _expire_auth_state(self, now):
        from quota_sentinel.platform.files import private_open
        path = self.directory/'auth-state.json'
        try:
            with private_open(path, 'rb') as stream:
                state = json.loads(stream.read(4096))
            failed_at = state.get('failed_at') if isinstance(state, dict) else None
            if not isinstance(failed_at, (int, float)) or now-failed_at >= self.retention_days*86400:
                path.unlink()
        except FileNotFoundError:
            pass
        except ValueError:
            path.unlink()

    def _append(self, record, now):
        line = (json.dumps(record, ensure_ascii=True, separators=(',', ':'))+'\n').encode()
        if len(line) > self.max_bytes or len(line) > 8192:
            raise ValueError('diagnostic event exceeds limit')
        day = datetime.fromtimestamp(now, timezone.utc).strftime('%Y%m%d')
        files = [path for path in self._files() if _FILE.fullmatch(path.name)[1] == day]
        path = files[-1] if files else self.directory/('trace-'+day+'-000000.jsonl')
        if path.exists() and path.stat().st_size+len(line) > self.chunk_bytes:
            index = int(_FILE.fullmatch(path.name)[2])+1
            path = self.directory/('trace-'+day+'-%06d.jsonl' % index)
        from quota_sentinel.platform.files import private_open
        with private_open(path, 'ab') as stream:
            stream.write(line)

    def _authentication_state(self, record, now):
        from quota_sentinel.platform.files import private_open, publish_private
        path = self.directory/'auth-state.json'
        if record['event'] == 'auth_required':
            publish_private(path, json.dumps({'call_id':record.get('call_id'), 'failed_at':now}).encode())
        elif record['event'] in ('quota_validated', 'model_validated') and record.get('fresh') is True:
            self._expire_auth_state(now)
            try:
                with private_open(path, 'rb') as stream: state = json.loads(stream.read(4096))
            except (FileNotFoundError, ValueError): return
            previous = state.get('call_id') if isinstance(state, dict) else None
            if isinstance(previous, str) and _ID.fullmatch(previous):
                self._append(dict(record, event='auth_recovered', previous_failure_call=previous), now)
            path.unlink()

    def cleanup(self):
        if not self.directory.exists():
            return {'removed_files': 0, 'retained_bytes': 0, 'retained_files': 0}
        with self._locked():
            return self._prune(self.clock())

    def emit(self, event, **fields):
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,47}', event):
            raise ValueError('invalid diagnostic event')
        now = self.clock()
        record = {'schema_version': 1, 'timestamp': datetime.fromtimestamp(now, timezone.utc).isoformat(),
            'event': event, 'pid': os.getpid(), 'ppid': os.getppid(),
            'process_identity': process_identity()}
        record.update({key: _metadata(value) for key, value in fields.items() if key in _FIELDS})
        if self.dropped:
            record['dropped_before'] = self.dropped
        try:
            with self._locked():
                self._append(record, now)
                self._authentication_state(record, now)
                self._prune(now)
            self.dropped = 0
            return True
        except (OSError, ValueError, RuntimeError):
            self.dropped += 1
            if not self._warned:
                print('auth-trace: record unavailable; diagnostic coverage incomplete', file=sys.stderr)
                self._warned = True
            return False

    def records(self, *, limit=1000, call_id=None):
        if not 1 <= limit <= 1000:
            raise ValueError('trace limit must be between 1 and 1000')
        if call_id is not None and not _ID.fullmatch(call_id):
            raise ValueError('call ID must contain 32 lowercase hex characters')
        result = deque(maxlen=limit)
        from quota_sentinel.platform.files import private_open
        for path in self._files():
            try:
                with private_open(path, 'rb') as stream:
                    while True:
                        line = stream.readline(8193)
                        if not line: break
                        if len(line) > 8192:
                            while line and not line.endswith(b'\n'):
                                line = stream.readline(8193)
                            continue
                        try: record = json.loads(line)
                        except ValueError: continue
                        if isinstance(record, dict) and (call_id is None or call_id in (record.get('call_id'), record.get('parent_id'), record.get('root_id'))):
                            result.append(record)
            except FileNotFoundError:
                continue  # A concurrent retention pass removed this chunk.
        return list(result)


def trace_environment(environment, *, state_dir=None, trigger=None):
    env = dict(environment)
    env.update(_CONTEXT.get())
    if state_dir is not None:
        env.setdefault(TRACE_DIR, str(Path(state_dir).absolute()/'diagnostics'))
    if not _ID.fullmatch(env.get(TRACE_ROOT, '')):
        env[TRACE_ROOT] = uuid.uuid4().hex
    if trigger is not None:
        env[TRACE_TRIGGER] = trigger
    if not re.fullmatch(r'(?:manual|scheduler|feishu|service):[a-z0-9_+:-]{1,48}', env.get(TRACE_TRIGGER, '')):
        env[TRACE_TRIGGER] = 'manual:unknown'
    return env


@contextmanager
def trigger_context(trigger):
    token = _CONTEXT.set({TRACE_TRIGGER: trigger, TRACE_ROOT: uuid.uuid4().hex})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


class Invocation:
    def __init__(self, kind, environment, *, reuse=False, **fields):
        env = trace_environment(environment)
        parent = env.get(TRACE_CALL, '')
        self.call_id = parent if reuse and _ID.fullmatch(parent) else uuid.uuid4().hex
        previous_parent = env.get(TRACE_PARENT, '') if reuse else parent
        self.parent_id = previous_parent if _ID.fullmatch(previous_parent) and previous_parent != self.call_id else None
        self.environment = dict(env, **{TRACE_CALL: self.call_id, TRACE_PARENT: self.parent_id or '', TRACE_KIND: kind})
        self.journal = TraceJournal(env[TRACE_DIR]) if env.get(TRACE_DIR) else None
        self.fields = dict(call_id=self.call_id, parent_id=self.parent_id,
            root_id=env[TRACE_ROOT], trigger=env[TRACE_TRIGGER], kind=kind, **fields)
        self.started = time.monotonic()
        self.finished = False

    def note(self, event, **fields):
        if self.journal is not None:
            self.journal.emit(event, **dict(self.fields, **fields))

    def finish(self, **fields):
        if not self.finished:
            self.finished = True
            self.note('call_finished', elapsed=round(time.monotonic()-self.started, 3), **fields)


@contextmanager
def invocation(kind, *, environment, **fields):
    span = Invocation(kind, environment, **fields)
    if span.journal is not None:
        span.note('call_started', ancestors=ancestors())
    token = _CONTEXT.set({key: span.environment[key] for key in (TRACE_ROOT, TRACE_CALL, TRACE_PARENT, TRACE_KIND, TRACE_TRIGGER, TRACE_DIR) if key in span.environment})
    try:
        yield span
    except BaseException as exc:
        span.finish(reason=type(exc).__name__, action='exception')
        raise
    finally:
        span.finish()
        _CONTEXT.reset(token)


class TracedProcess:
    """Keep ownership semantics while recording the actual launcher's PID."""
    def __init__(self, process, argv, environment):
        self.process_owner = process
        self.audit = Invocation(environment.get(TRACE_KIND, 'owned-command'), environment, reuse=True)
        self.closed = False
        self.stopped = False
        self.audit.note('owned_process_started', target_pid=process.pid,
            target_identity=process_identity(process.pid), requested_executable=str(argv[0]))
        # Never perform journal I/O under the owner's process-tree lock. The
        # POSIX tracker keeps only a bounded in-memory identity sample.
        process._trace_children = {}
        process._trace_children_dropped = 0

    def __getattr__(self, key):
        return getattr(self.process_owner, key)

    def stop(self, grace):
        self.process_owner.stop(grace)
        self._stopped()

    def _stopped(self):
        if not self.stopped:
            self.stopped = True
            self.audit.note('owned_process_stopped', target_pid=self.pid, exit_code=self.poll())

    def close(self):
        if not self.closed:
            self.closed = True
            self.process_owner.close()
            self._stopped()
            children = list(self.process_owner._trace_children.values())
            if children:
                self.audit.note('owned_children_observed', children=children, coverage='sampled')
            if self.process_owner._trace_children_dropped:
                self.audit.note('descendant_evidence_gap', count=self.process_owner._trace_children_dropped,
                    limit=8, coverage='incomplete', reason='descendant-sample-limit')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def trace_owned_process(process, argv, environment):
    if environment.get(TRACE_DIR) and _ID.fullmatch(environment.get(TRACE_CALL, '')):
        return TracedProcess(process, argv, environment)
    return process
