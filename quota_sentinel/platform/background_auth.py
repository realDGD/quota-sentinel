"""macOS background CLI authentication without launching desktop apps.

The sandbox blocks browser launch before a log observer can react. The
observer only reads the private log of this invocation, never shared logs.
Credentials, HOME, and the caller's foreground CLI remain unchanged.
"""
import os
from pathlib import Path
import sys
import tempfile
import time

AUTH_REQUIRED_EXIT = 78
SANDBOX = Path('/usr/bin/sandbox-exec')
PROFILE = r'''(version 1)
(allow default)
(deny process-exec (literal "/usr/bin/open") (literal "/usr/bin/osascript"))
(deny mach-lookup
    (global-name "com.apple.coreservices.launchservicesd")
    (global-name "com.apple.coreservices.appleevents")
    (global-name-regex #"^com\.apple\.lsd([\.].*)?$"))
'''


def no_browser_command(command):
    if sys.platform != 'darwin':
        return list(command)
    if not os.access(SANDBOX, os.X_OK):
        raise ValueError('background_auth_guard_unavailable')
    return [str(SANDBOX), '-p', PROFILE, *command]


def agy_background_command(command, *, timeout, kill_grace, log_path=None):
    if sys.platform != 'darwin':
        return list(command)
    from quota_sentinel.helpers import resource_path
    log_option = [] if log_path is None else ['--agy-log-file', str(log_path)]
    return [sys.executable, str(resource_path('run_with_timeout.py')),
            '--agy-auth-guard', '--timeout', str(timeout),
            '--kill-grace', str(kill_grace), *log_option, '--', *command]


class AgyAuthGuard:
    def __init__(self, log_path=None):
        self.active = sys.platform == 'darwin'
        self._directory = None
        self._offset = 0
        self._tail = b''
        self._next_read = 0
        self.log = None if log_path is None else Path(log_path)
        self.auth_marker = None

    def __enter__(self):
        if self.active:
            # Refuse before starting agy if the OS protection is unavailable.
            no_browser_command([])
            from quota_sentinel.platform.files import private_open
            if self.log is None:
                self._directory = tempfile.TemporaryDirectory(prefix='quota-agy-auth.')
                os.chmod(self._directory.name, 0o700)
                self.log = Path(self._directory.name) / 'cli.log'
                with private_open(self.log, 'xb'):
                    pass
            else:
                # Internal callers own this private log across wrapper kills.
                if not self.log.is_absolute():
                    raise ValueError('background_auth_guard_unavailable')
                with private_open(self.log, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    if info.st_mode & 0o077:
                        raise ValueError('background_auth_guard_unavailable')
        return self

    def command(self, command):
        if not self.active:
            return list(command)
        return no_browser_command([*command, '--log-file', str(self.log)])

    def auth_required(self, *, force=False):
        if not self.active or (not force and time.monotonic() < self._next_read):
            return False
        self._next_read = time.monotonic() + .05
        try:
            with self.log.open('rb') as stream:
                stream.seek(self._offset)
                while True:
                    chunk = stream.read(65536)
                    self._offset = stream.tell()
                    data = self._tail + chunk
                    self._tail = data[-256:]
                    for marker, name in (
                        (b'Print mode: silent auth failed', 'silent_auth_failed'),
                        (b'Print mode: triggering interactive OAuth', 'interactive_oauth'),
                        (b'Starting OAuth authentication flow', 'oauth_started'),
                    ):
                        if marker in data:
                            self.auth_marker = name
                            return True
                    if not force or not chunk:
                        return False
        except FileNotFoundError:
            return False

    def __exit__(self, *args):
        if self._directory is not None:
            self._directory.cleanup()
