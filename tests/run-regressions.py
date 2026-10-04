#!/usr/bin/env python3
"""Run hermetic scripts with private homes, logs, and finite deadlines.

Native gates run only on their real operating system. Supplier smoke tests
are deliberately outside this dispatcher and require a separate decision.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
BUILD_CACHE = None
MACOS = {'python-installer-regression.py', 'python-runtime-lock-regression.py',
         'python-authority-regression.py', 'python-state-cutover-regression.py',
         'python-config-regression.py', 'python-config-cli-regression.py',
         'python-config-migration-regression.py', 'python-feature-pruning-regression.py',
         'python-config-integration-regression.py', 'python-pi-live-integration-regression.py'}
POSIX = {'python-daemon-regression.py', 'python-model-runner-regression.py',
         'python-codex-exec-regression.py', 'python-agy-exec-regression.py',
         'python-direct-runner-regression.py', 'python-pi-plugin-regression.py',
         'python-quota-probe-regression.py', 'python-app-regression.py',
         'python-entrypoint-regression.py', 'python-feishu-regression.py',
         'feishu-listener-regression.py', 'task-orchestrator-regression.py',
         'run-with-timeout-regression.py', 'uv-project-regression.py', 'python-pi-live-regression.py'}

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

def discover_node(root=ROOT / 'tests'):
    return sorted(root.glob('*-regression.mjs'))

def fixture_environment(home):
    allowed = ('PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'LANG',
               'LC_ALL', 'TMPDIR', 'TEMP', 'TMP', 'SSL_CERT_FILE', 'SSL_CERT_DIR')
    env = {k: os.environ[k] for k in allowed if k in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), PYTHONPATH=str(ROOT),
               XDG_CONFIG_HOME=str(home / '.config'), XDG_DATA_HOME=str(home / '.local/share'),
               LOCALAPPDATA=str(home / 'AppData/Local'), PYTHONUNBUFFERED='1',
               UV_PYTHON=sys.executable, UV_PYTHON_DOWNLOADS='never',
               QUOTA_SENTINEL_KEYCHAIN_DISABLED='1')
    if BUILD_CACHE:
        env['QUOTA_SENTINEL_TEST_UV_CACHE'] = BUILD_CACHE
    return env

def classify(path):
    name=path.name
    if 'supplier' in name and 'smoke' in name:return 'supplier-smoke'
    if 'native-' in name or name in MACOS or name in POSIX:return 'native-platform'
    return 'platform-independent'

def evidence_result(path,code,*,required=False):
    kind=classify(path)
    status='PASS' if code==0 else 'UNVERIFIED' if code==77 and kind=='native-platform' else 'FAIL'
    return {'script':path.name,'class':kind,'status':status,'required':required,'complete':status=='PASS'}

def windows11_evidence(name):
    return 'windows 11' in name.lower() and 'server' not in name.lower()

def os_identity():
    import platform
    if os.name!='nt':return platform.platform()
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'SOFTWARE\Microsoft\Windows NT\CurrentVersion') as key:
        product=winreg.QueryValueEx(key,'ProductName')[0]
        build=int(winreg.QueryValueEx(key,'CurrentBuildNumber')[0])
    if 'server' not in product.lower() and build>=22000:return 'Windows 11 build '+str(build)
    return product+' build '+str(build)

def run_script(path, log, *, timeout=360):
    from quota_sentinel.platform.process import run_bounded
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='qs-regression-home-') as directory:
        command = [sys.executable,str(path)]
        if path.suffix=='.mjs':
            import shutil
            node=shutil.which('node')
            if not node:
                log.write_bytes(b'Node is required to verify the optional Pi helper\n')
                return 127,time.monotonic()-start
            command=[node,'--test',str(path)]
        environment=fixture_environment(Path(directory))
        if path.name=='python-credentials-native-linux-regression.py' and os.environ.get('QUOTA_SENTINEL_TEST_LINUX_CREDENTIAL_BACKEND'):
            for key in ('QUOTA_SENTINEL_TEST_LINUX_CREDENTIAL_BACKEND','DBUS_SESSION_BUS_ADDRESS','XDG_RUNTIME_DIR'):
                if key in os.environ:environment[key]=os.environ[key]
        if path.name=='python-services-native-linux-regression.py':
            for key in ('QUOTA_SENTINEL_TEST_LINUX_SERVICES','DBUS_SESSION_BUS_ADDRESS','XDG_RUNTIME_DIR'):
                if key in os.environ:environment[key]=os.environ[key]
        result=run_bounded(command,cwd=ROOT,environment=environment,timeout=timeout,kill_grace=0,max_bytes=8*1024*1024)
        log.write_bytes(result.stdout+result.stderr)
        return result.returncode,time.monotonic()-start

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', choices=('current', 'portable', 'macos', 'linux', 'windows'), default='current')
    parser.add_argument('--log-dir', type=Path)
    parser.add_argument('--require-native',action='store_true',help='incomplete native gates fail the verification')
    parser.add_argument('--require-windows11',action='store_true',help='require an actual Windows 11 host')
    parser.add_argument('--report',type=Path,help='write separate evidence classes and OS context')
    args = parser.parse_args(argv)
    selected = current_platform() if args.platform == 'current' else args.platform
    if args.require_native and selected=='portable':
        parser.error('required native verification cannot use the portable-only subset')
    if selected not in ('portable', current_platform()):
        parser.error('native verification must run on that operating system')
    log_dir = args.log_dir or Path(tempfile.mkdtemp(prefix='qs-regressions-'))
    log_dir.mkdir(parents=True, exist_ok=True)
    global BUILD_CACHE
    import shutil
    uv = shutil.which('uv')
    if uv:
        cache = subprocess.run([uv, '--no-config', 'cache', 'dir'], capture_output=True, text=True, timeout=10)
        if cache.returncode == 0: BUILD_CACHE = cache.stdout.strip()
    scripts = discover(selected) + discover_node()
    failed = [];incomplete=[];evidence=[]
    for script in scripts:
        code, elapsed = run_script(script, log_dir / (script.stem + '.log'))
        item=evidence_result(script,code,required=args.require_native and classify(script)=='native-platform');evidence.append(item)
        print(item['status'] + f' {script.name} ({elapsed:.1f}s) [{item["class"]}]',flush=True)
        if item['status']=='UNVERIFIED':incomplete.append(script.name)
        if item['status']=='FAIL':
            failed.append(script.name)
            print((log_dir / (script.stem + '.log')).read_text(errors='replace')[-4000:])
    identity=os_identity()
    if args.require_windows11 and not windows11_evidence(identity):incomplete.append('Windows 11 native host required; hosted Server does not qualify')
    from quota_sentinel.platform.process import run_bounded
    revision=run_bounded(('git','rev-parse','HEAD'),cwd=ROOT,environment=fixture_environment(Path.home()),timeout=5,kill_grace=0,max_bytes=256).stdout.decode().strip()
    dirty=run_bounded(('git','diff','--no-ext-diff','--quiet','HEAD'),cwd=ROOT,environment=fixture_environment(Path.home()),timeout=5,kill_grace=0,max_bytes=256).returncode!=0
    report={'revision':revision,'dirty':dirty,'platform':identity,'windows11':windows11_evidence(identity),'execution_context':'CI' if os.environ.get('GITHUB_ACTIONS') else 'local','evidence':evidence,'unverified':incomplete,'complete':not failed and not incomplete,'supplier_smoke':'NOT RUN'}
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(report,indent=2)+'\n')
    print(f'{len(scripts)-len(failed)-len([x for x in evidence if x["status"]=="UNVERIFIED"])}/{len(scripts)} scripts passed; UNVERIFIED={len(incomplete)}; platform={selected}; logs={log_dir}')
    return 1 if failed else 2 if incomplete and (args.require_native or args.require_windows11) else 0

if __name__ == '__main__':
    raise SystemExit(main())
