"""Explicit credential references and a finite, secret-safe worker boundary."""
from dataclasses import asdict, replace
import json
import math
import os
from pathlib import Path
import platform
import re
import sys
import tempfile
from .process import run_bounded

DEFAULT_TIMEOUT = 15
MAX_SECRET_BYTES = 16384

class CredentialUnavailable(RuntimeError):
    """The selected source is missing, locked, invalid or exceeded its budget."""

def validate_reference(reference):
    if reference.kind not in ('system', 'environment', 'file'):
        raise CredentialUnavailable('invalid credential reference')
    if any(not isinstance(x, str) or not x or '\x00' in x or len(x)>4096
           for x in (reference.locator, reference.account)):
        raise CredentialUnavailable('invalid credential reference')
    if reference.kind=='environment' and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', reference.locator):
        raise CredentialUnavailable('invalid credential environment variable')
    if reference.kind=='system' and reference.locator.startswith(('secret-service:','kwallet:')) and not reference.locator.split(':',1)[1]:
        raise CredentialUnavailable('credential service name is empty')

def valid_value(value):
    try:return isinstance(value,str) and bool(value) and len(value.encode())<=MAX_SECRET_BYTES
    except UnicodeError:return False

class CredentialStore:
    def __init__(self, *, environment=None, system=None, runner=None,
                 worker_command=None, security_bin='/usr/bin/security'):
        self.environment = dict(os.environ if environment is None else environment)
        self.system = system or platform.system()
        self.runner = runner or run_bounded
        self.worker_command = worker_command or (sys.executable, '-m', 'quota_sentinel.platform.credential_worker')
        self.security_bin = str(security_bin)

    def read(self, reference, *, timeout=DEFAULT_TIMEOUT):
        validate_reference(reference)
        if reference.kind=='environment':
            value=self.environment.get(reference.locator, '')
            if not valid_value(value):
                raise CredentialUnavailable('selected credential environment variable is unavailable')
            return value
        return self._request('read', reference, timeout=timeout)

    def write(self, reference, value, *, timeout=DEFAULT_TIMEOUT):
        validate_reference(reference)
        if not valid_value(value):
            raise CredentialUnavailable('invalid credential value')
        if reference.kind=='environment':
            raise CredentialUnavailable('set the selected environment variable outside the application')
        self._request('write', reference, value=value, timeout=timeout)

    def delete(self, reference, *, timeout=DEFAULT_TIMEOUT):
        validate_reference(reference)
        if reference.kind=='environment':
            raise CredentialUnavailable('environment references cannot be deleted by the application')
        self._request('delete', reference, timeout=timeout)

    def _request(self, action, reference, *, value=None, timeout):
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or timeout<=0:
            raise CredentialUnavailable('invalid credential timeout')
        if reference.kind=='file':
            locator=reference.locator
            if locator=='~' or locator.startswith('~/') or locator.startswith('~\\'):
                home=self.environment.get('HOME') or self.environment.get('USERPROFILE') or str(Path.home())
                locator=str(Path(home)/locator[2:])
            reference=replace(reference, locator=str(Path(locator).absolute()))
        if reference.kind=='system':
            if self.environment.get('QUOTA_SENTINEL_KEYCHAIN_DISABLED')=='1':
                raise CredentialUnavailable('system credential access is disabled')
            if self.system=='Linux' and not reference.locator.startswith(('secret-service:','kwallet:')):
                raise CredentialUnavailable('select secret-service: or kwallet:, or an explicit environment/file reference')
        # Only platform/session coordinates cross the boundary. Ambient API
        # tokens must not be inherited by a worker for an unrelated source.
        allowed=('PATH','HOME','USERPROFILE','SystemRoot','SYSTEMROOT','WINDIR','TEMP','TMP',
                 'LOCALAPPDATA','APPDATA','DBUS_SESSION_BUS_ADDRESS','XDG_RUNTIME_DIR',
                 'XDG_CONFIG_HOME','LANG','LC_ALL')
        environment={k:self.environment[k] for k in allowed if k in self.environment}
        environment['PYTHONPATH']=str(Path(__file__).resolve().parents[2])
        environment['PYTHONIOENCODING']='utf-8'
        request=dict(action=action, system=self.system, reference=asdict(reference),
                     timeout=timeout, security_bin=self.security_bin)
        if value is not None:request['value']=value
        try:
            with tempfile.TemporaryDirectory(prefix='quota-sentinel-credential-') as cwd:
                result=self.runner(self.worker_command, cwd=cwd, environment=environment,
                    input_data=json.dumps(request,ensure_ascii=False).encode(),
                    timeout=timeout, kill_grace=0, max_bytes=65536)
            document=json.loads(result.stdout)
            if result.returncode or result.timed_out or not isinstance(document,dict) or document.get('status')!='ok':
                raise CredentialUnavailable('selected credential source is unavailable or timed out')
            if action=='read':
                secret=document.get('value')
                if not valid_value(secret):
                    raise CredentialUnavailable('selected credential source has no usable value')
                return secret
            return None
        except CredentialUnavailable:raise
        except (OSError,ValueError,TypeError):
            raise CredentialUnavailable('selected credential source is unavailable') from None
