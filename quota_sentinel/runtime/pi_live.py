"""Bounded metadata helper and strict nonce/identity/freshness verification."""
from __future__ import annotations
import json
import os
from pathlib import Path
import secrets
import shutil
import tempfile
import time
from .quota_probe import QuotaReading, _run_bounded
from quota_sentinel.quota import Tier
from quota_sentinel.quota.pi_live import normalize_pi_live
from quota_sentinel.helpers import resource_path
from quota_sentinel.platform.paths import user_home,expand_path

MAX_BYTES = 1048576
ERROR_CODES = frozenset(('unsupported_pi_sdk','unsupported_pi_plugin','unsupported_provider',
    'auth_unavailable','auth_expired','auth_scope_unavailable','account_scope_mismatch',
    'auth_timeout','metadata_timeout','metadata_route_denied','metadata_redirect_denied',
    'metadata_http_error','metadata_invalid_response','metadata_invalid_json','metadata_oversized',
    'invalid_request','invalid_usage'))

def selected_sdk_root(config,*,environment=None):
    env=os.environ if environment is None else environment
    if config.clients.get('pi_sdk'):
        return expand_path(config.clients['pi_sdk'],env).resolve()
    executable = config.clients.get('pi') or shutil.which('pi',path=env.get('PATH',''))
    if not executable:
        return None
    path = expand_path(executable,env)
    if not path.is_file() and not any(s in str(path) for s in ('/','\\')):
        found=shutil.which(str(path),path=env.get('PATH',''))
        if not found:return None
        path=Path(found)
    path=path.resolve()
    for parent in path.parents:
        for candidate in (parent, parent / 'libexec/lib/node_modules/@earendil-works/pi-coding-agent',
                          parent / 'node_modules/@earendil-works/pi-coding-agent'):
            try:
                manifest = json.loads((candidate / 'package.json').read_bytes())
                if manifest.get('name') == '@earendil-works/pi-coding-agent':
                    return candidate
            except (OSError, ValueError, AttributeError):
                pass
    return None

def selected_plugin_root(config,*,environment=None):
    env=os.environ if environment is None else environment
    entry = config.clients.get('antigravity_plugin')
    if entry:
        path = expand_path(entry,env).resolve()
        for parent in (path, *path.parents):
            try:
                manifest = json.loads((parent / 'package.json').read_bytes())
                if manifest.get('name') == 'pi-antigravity':
                    return parent
            except (OSError, ValueError, AttributeError):
                pass
        return None
    return expand_path(config.clients.get('pi_auth',str(user_home(env)/'.pi/agent/auth.json')),env).resolve().parent/'npm/node_modules/pi-antigravity'

class PiLiveQuotaClient:
    def __init__(self, config, *, helper_path=None, node_bin=None, run_bounded=_run_bounded,
                 environment=None, clock=time.time):
        self.config = config
        self.environment = os.environ if environment is None else environment
        self.helper = expand_path(helper_path or resource_path('pi_quota_query.mjs'),self.environment).resolve()
        self.node = str(expand_path(node_bin or config.clients.get('node') or shutil.which('node',path=self.environment.get('PATH','')) or 'node',self.environment))
        self.run_bounded = run_bounded
        self.clock = clock

    def query(self, provider):
        def failed(error):
            return QuotaReading(None, Tier.PI_LIVE, False, error)
        selected = self.config.providers.get(provider)
        if not selected or not selected.enabled or 'pi-live' not in selected.quota_chain:
            return failed('tier_not_selected')
        if provider not in ('codex', 'antigravity', 'opencode'):
            return failed('unsupported_provider')
        nonce = secrets.token_hex(32)
        started = int(self.clock())
        budget = self.config.budgets['pi_live']
        request = dict(protocol_version=1, request_id=nonce, provider=provider,
            sdk_path=str(selected_sdk_root(self.config,environment=self.environment) or ''),
            auth_path=str(expand_path(self.config.clients.get('pi_auth',str(user_home(self.environment)/'.pi/agent/auth.json')),self.environment).resolve()),
            timeout_seconds=budget['timeout'])
        if provider == 'antigravity':
            request['plugin_path'] = str(selected_plugin_root(self.config,environment=self.environment) or '')
        allowed = ('PATH','HOME','USERPROFILE','SYSTEMROOT','WINDIR','TEMP','TMP','TMPDIR',
                   'HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY','http_proxy','https_proxy',
                   'all_proxy','no_proxy','SSL_CERT_FILE','SSL_CERT_DIR')
        env = {k:self.environment[k] for k in allowed if k in self.environment}
        if provider == 'opencode' and 'OPENCODE_API_KEY' in self.environment:
            env['OPENCODE_API_KEY'] = self.environment['OPENCODE_API_KEY']
        env['PI_OFFLINE'] = '1'
        try:
            with tempfile.TemporaryDirectory(prefix='quota-pi-live.') as temp:
                from quota_sentinel.platform.files import private_directory
                private_directory(temp)
                result = self.run_bounded([self.node,str(self.helper)], budget['timeout'],
                    budget['kill_grace'], json.dumps(request).encode(), environment=env, cwd=temp)
            if result.timed_out:
                return failed('pi_live_timeout')
            if result.returncode != 0 or len(result.stdout) > MAX_BYTES:
                return failed('pi_live_unavailable')
            record = json.loads(result.stdout.decode('utf8'))
            if (not isinstance(record, dict) or type(record.get('protocol_version')) is not int
                    or record['protocol_version'] != 1 or record.get('request_id') != nonce
                    or record.get('provider') != provider):
                return failed('invalid_helper_response')
            if record.get('status') == 'error':
                code = record.get('error_code')
                return failed(code if isinstance(code,str) and code in ERROR_CODES else 'pi_live_unavailable')
            if record.get('status') != 'ok' or set(record) != {'protocol_version','request_id','provider','status','account_scope','queried_at','payload'}:
                return failed('invalid_helper_response')
            scope = record['account_scope']
            if not isinstance(scope,str) or not scope.startswith('pi:'+provider+':') or len(scope) != len('pi:'+provider+':')+64 or any(c not in '0123456789abcdef' for c in scope.rsplit(':',1)[-1]):
                return failed('invalid_helper_response')
            stamp = record['queried_at']
            if type(stamp) is not int or not started-1 <= stamp <= int(self.clock())+1:
                return failed('invalid_helper_response')
            quota = normalize_pi_live(provider,record['payload'],captured_at=stamp)
            return QuotaReading(quota,Tier.PI_LIVE,True)
        except (OSError, ValueError, TypeError, AttributeError):
            # Discard arbitrary helper output/errors, including token strings.
            return failed('pi_live_unavailable')
