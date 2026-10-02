#!/usr/bin/env python3
"""Run hermetic scripts with private homes, logs, and finite deadlines.

Native gates run only on their real operating system. Supplier smoke tests
are deliberately outside this dispatcher and require a separate decision.
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
MACOS = {'python-installer-regression.py', 'python-runtime-lock-regression.py',
         'python-authority-regression.py', 'python-state-cutover-regression.py',
         'python-config-regression.py', 'python-config-cli-regression.py',
         'python-config-migration-regression.py', 'python-feature-pruning-regression.py',
         'python-config-integration-regression.py'}
POSIX = {'python-daemon-regression.py', 'python-model-runner-regression.py',
         'python-codex-exec-regression.py', 'python-agy-exec-regression.py',
         'python-direct-runner-regression.py', 'python-pi-plugin-regression.py',
         'python-quota-probe-regression.py', 'python-app-regression.py',
         'python-entrypoint-regression.py', 'python-feishu-regression.py',
         'feishu-listener-regression.py', 'task-orchestrator-regression.py',
         'run-with-timeout-regression.py', 'uv-project-regression.py'}

def current_platform():
    return 'windows' if os.name == 'nt' else 'macos' if sys.platform == 'darwin' else 'linux'

def discover(platform, root=ROOT / 'tests'):
    actual = current_platform() if platform == 'current' else platform
    paths = []
    for path in sorted(root.glob('*-regression.py')):
        name = path.name
        native = ('windows' if 'native-windows' in name else
                  'linux' if 'native-linux' in name else
                  'macos' if name in MACOS or 'native-macos' in name else None)
        if native is not None and actual != native:
            continue
        if name in POSIX and actual not in ('macos', 'linux'):
            continue
        paths.append(path)
    return paths

def fixture_environment(home):
    allowed = ('PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'LANG',
               'LC_ALL', 'TMPDIR', 'TEMP', 'TMP', 'SSL_CERT_FILE', 'SSL_CERT_DIR')
    env = {k: os.environ[k] for k in allowed if k in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), PYTHONPATH=str(ROOT),
               XDG_CONFIG_HOME=str(home / '.config'), XDG_DATA_HOME=str(home / '.local/share'),
               LOCALAPPDATA=str(home / 'AppData/Local'), PYTHONUNBUFFERED='1',
               UV_PYTHON=sys.executable, UV_PYTHON_DOWNLOADS='never',
               QUOTA_SENTINEL_KEYCHAIN_DISABLED='1')
    return env

def run_script(path, log, *, timeout=360):
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='qs-regression-home-') as directory:
        with log.open('wb') as output:
            process = subprocess.Popen([sys.executable, str(path)], cwd=str(ROOT),
                env=fixture_environment(Path(directory)), stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, start_new_session=os.name != 'nt')
            try:
                process.wait(timeout=timeout)
                result = process.returncode
            except subprocess.TimeoutExpired:
                result = 124
            finally:
                if os.name != 'nt':
                    try: os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                elif process.poll() is None:
                    process.kill()
                process.wait(timeout=2)
    return result, time.monotonic() - start

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', choices=('current', 'portable', 'macos', 'linux', 'windows'), default='current')
    parser.add_argument('--log-dir', type=Path)
    args = parser.parse_args(argv)
    selected = current_platform() if args.platform == 'current' else args.platform
    if selected not in ('portable', current_platform()):
        parser.error('native verification must run on that operating system')
    log_dir = args.log_dir or Path(tempfile.mkdtemp(prefix='qs-regressions-'))
    log_dir.mkdir(parents=True, exist_ok=True)
    scripts = discover(selected)
    failed = []
    for script in scripts:
        code, elapsed = run_script(script, log_dir / (script.stem + '.log'))
        print(('PASS' if code == 0 else 'FAIL') + f' {script.name} ({elapsed:.1f}s)', flush=True)
        if code != 0:
            failed.append(script.name)
            print((log_dir / (script.stem + '.log')).read_text(errors='replace')[-4000:])
    print(f'{len(scripts)-len(failed)}/{len(scripts)} scripts passed; platform={selected}; logs={log_dir}')
    return 1 if failed else 0

if __name__ == '__main__':
    raise SystemExit(main())
