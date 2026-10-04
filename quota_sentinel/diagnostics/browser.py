"""Best-effort macOS browser evidence without retaining log messages or URLs.

LaunchServices' log emitter is not necessarily the caller. Only explicit
caller fields or a sandbox denial's process PID are labeled as attribution.
Endpoint Security is not enabled here: its exec feed requires administrator
privileges. Missing/redacted system evidence remains explicitly unattributed.
"""
import json
from datetime import datetime
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

from .trace import Invocation, TraceJournal, process_identity, TRACE_DIR, TRACE_TRIGGER

PREDICATE = '(subsystem CONTAINS "LaunchServices" OR subsystem CONTAINS "sandbox" OR process == "sandboxd")'
COMMAND = ('/usr/bin/log', 'stream', '--style', 'ndjson', '--level', 'debug',
           '--timeout', '5m', '--predicate', PREDICATE)


def browser_event(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get('eventMessage'), str):
        return None
    message = raw['eventMessage']
    lower = message.lower()
    denial = re.search(r'Sandbox:\s*[^()\n]+\((\d+)\).*deny.*process-exec\s+/usr/bin/(?:open|osascript)\b', message, re.I)
    oauth = re.search(r'https?://accounts\.google\.com(?:[/?:\s]|$)', message, re.I)
    chrome = 'google chrome' in lower or 'com.google.chrome' in lower
    if not (denial or oauth or chrome or 'antigravity' in lower):
        return None
    event = {'event_kind': 'browser_launch_denied' if denial else
             'browser_request' if any(term in lower for term in ('openurl', 'openapplication', 'launch', 'activate')) else 'browser_related',
             'url_class': 'google-oauth' if oauth else 'antigravity' if 'antigravity' in lower else 'unknown',
             'attribution': 'unattributed'}
    if type(raw.get('processID')) is int:
        event['emitter_pid'] = raw['processID']
    # URL query/path payload is not a system caller field, even if its key
    # happens to be named caller_pid. Only inspect the surrounding metadata.
    metadata = re.sub(r'https?://[^\s<>"\']+', '[url]', message, flags=re.I)
    caller = re.search(r'(?:^|[\s,{;])caller(?:_pid|pid|\s+pid)\s*[:=]\s*(\d{1,10})\b', metadata, re.I)
    if denial or caller:
        pid = int((denial or caller)[1])
        event.update(caller_pid=pid, caller_identity=event_identity(pid, raw.get('timestamp')),
            attribution='system-reported-denied-process' if denial else 'system-reported-caller')
    stamp = raw.get('timestamp')
    if isinstance(stamp, str) and re.fullmatch(r'[0-9T: +.\-Z]{10,64}', stamp):
        event['source_timestamp'] = stamp
    return event


def event_identity(pid, timestamp):
    """Reject recycled PIDs; a live sample must predate this logged event."""
    unknown = dict(pid=pid, ppid=None, executable=None, start_time=None, identity_known=False)
    if sys.platform != 'darwin' or not isinstance(timestamp, str):
        return unknown
    try:
        normalized = re.sub(r'([+-]\d{2})(\d{2})$', r'\1:\2', timestamp.replace('Z', '+00:00'))
        event_time = datetime.fromisoformat(normalized)
        if event_time.tzinfo is None:
            return unknown
        identity = process_identity(pid)
        if identity['identity_known'] and float(identity['start_time']) <= event_time.timestamp():
            return identity
    except (ValueError, TypeError, OverflowError):
        pass
    return unknown


class BrowserObserver:
    """The selected service owns this thread and its finite streaming child."""
    def __init__(self, directory, *, command=None, system=None):
        self.journal = TraceJournal(directory)
        self.command = tuple(COMMAND if command is None else command)
        self.system = sys.platform if system is None else system
        self.stop_event = threading.Event()
        self.thread = None
        self._process = None
        self._guard = threading.Lock()
        self.audit = Invocation('browser-observer', {
            TRACE_DIR: str(Path(directory).absolute()), TRACE_TRIGGER: 'service:browser-observer'})

    def _status(self, active, **fields):
        from quota_sentinel.platform.files import publish_private, private_directory
        status = dict(observer='unified-log', coverage='best-effort', active=active,
            endpoint_security='requires-administrator', updated_at=time.time())
        status.update(fields)
        if active:
            status['observer_start_time'] = process_identity(status.get('observer_pid', 0))['start_time']
        try:
            private_directory(self.journal.directory)
            publish_private(self.journal.directory/'observer-status.json', (json.dumps(status)+'\n').encode())
        except (OSError, ValueError):
            self.audit.note('observer_gap', reason='status-storage-unavailable')

    def start(self):
        if self.system != 'darwin':
            return self
        self.thread = threading.Thread(target=self._run, name='quota-browser-evidence', daemon=True)
        self.thread.start()
        return self

    def _run(self):
        from quota_sentinel.platform.process import spawn_owned, BoundedPipes
        self._status(False)
        while not self.stop_event.is_set():
            process = None
            pipes = None
            incomplete = False
            try:
                self.journal.cleanup()
                process = spawn_owned(self.command, cwd=self.journal.directory,
                    environment=os.environ, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                with self._guard:
                    self._process = process
                self.audit.note('observer_started', target_pid=process.pid,
                    observer='unified-log', coverage='best-effort')
                self._status(True, observer_pid=process.pid)
                pipes = BoundedPipes(process, max_bytes=4*1024*1024)
                next_cleanup = time.monotonic()+60
                deadline = time.monotonic()+310
                while not self.stop_event.is_set() and time.monotonic() < deadline:
                    if time.monotonic() >= next_cleanup:
                        self.journal.cleanup()
                        self._status(True, observer_pid=process.pid, coverage='incomplete' if incomplete else 'best-effort')
                        next_cleanup = time.monotonic()+60
                    try:
                        line = pipes.readline(deadline=time.monotonic()+.5)
                    except subprocess.TimeoutExpired:
                        continue
                    if len(line) > 65536:
                        incomplete = True
                        self.audit.note('observer_gap', reason='system-line-too-large')
                        continue
                    try: raw = json.loads(line)
                    except ValueError: continue
                    event = browser_event(raw)
                    if event is not None:
                        self.audit.note('system_browser_event', observer='unified-log', **event)
            except EOFError:
                pass  # A finite five-minute stream completed; reconnect below.
            except (OSError, ValueError, RuntimeError):
                incomplete = True
                self.audit.note('observer_gap', reason='system-stream-unavailable', coverage='incomplete')
            finally:
                if pipes is not None:
                    pipes.close()
                elif process is not None:
                    process.close()
                with self._guard:
                    self._process = None
                if process is not None and process.poll() not in (None, 0) and not self.stop_event.is_set():
                    incomplete = True
                    self.audit.note('observer_gap', reason='system-stream-exited', exit_code=process.poll(), coverage='incomplete')
                self._status(False, coverage='incomplete' if incomplete else 'best-effort')
                self.audit.note('observer_stopped', observer='unified-log',
                    exit_code=None if process is None else process.poll())
            self.stop_event.wait(1 if process is not None and process.poll()==0 else 30)

    def stop(self):
        self.stop_event.set()
        with self._guard:
            process = self._process
        if process is not None:
            process.stop(0)
        if self.thread is not None:
            self.thread.join(timeout=3)

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.stop()
