"""Background agy keeps working, cannot launch a browser, and fails promptly."""
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HELPER = ROOT / 'quota_sentinel/helpers/run_with_timeout.py'


@unittest.skipUnless(sys.platform == 'darwin', 'native macOS browser boundary')
class BackgroundAuthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def cli(self, body):
        path = self.root / 'agy-fixture.py'
        path.write_text('import sys, os, json, time, subprocess\nfrom pathlib import Path\n' + body)
        return [sys.executable, str(path)]

    def guarded(self, command, timeout=4):
        return subprocess.run([sys.executable, str(HELPER), '--agy-auth-guard',
            '--timeout', str(timeout), '--kill-grace', '.1', '--', *command],
            cwd=self.root, capture_output=True, timeout=timeout+4)

    def test_success_preserves_output_and_account_environment(self):
        command = self.cli("print(json.dumps({'status':'SUCCESS','accountHome':os.environ['HOME'],'num_turns':0}))\n")
        result = self.guarded(command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'status':'SUCCESS','accountHome':os.environ['HOME'],'num_turns':0})

    def test_system_browser_launcher_is_denied_without_opening_a_page(self):
        # --help cannot open a page even if a regression removes the sandbox.
        command = self.cli("try:\n subprocess.run(['/usr/bin/open','--help'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n blocked=False\nexcept PermissionError:\n blocked=True\nprint(json.dumps({'blocked':blocked}))\n")
        result = self.guarded(command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'blocked':True})

    def test_auth_failure_returns_before_timeout_without_echoing_log_secrets(self):
        command = self.cli("log=Path(sys.argv[sys.argv.index('--log-file')+1])\nlog.write_text('fixture-secret\\nPrint mode: silent auth failed\\nPrint mode: triggering interactive OAuth\\n')\nwhile True: time.sleep(.05)\n")
        started = time.monotonic()
        result = self.guarded(command)
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertLess(time.monotonic()-started, 2)
        self.assertIn(b'background authentication required', result.stderr)
        self.assertNotIn(b'fixture-secret', result.stdout+result.stderr)

    def test_auth_abort_reaps_owned_detached_descendant(self):
        pidfile = self.root / 'child.pid'
        child = 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(120)'
        command = self.cli("child=subprocess.Popen([sys.executable,'-c'," + repr(child) + "],start_new_session=True)\nPath(" + repr(str(pidfile)) + ").write_text(str(child.pid))\nlog=Path(sys.argv[sys.argv.index('--log-file')+1])\nlog.write_text('Print mode: triggering interactive OAuth\\n')\nwhile True: time.sleep(.05)\n")
        result = self.guarded(command)
        self.assertEqual(result.returncode, 78, result.stderr)
        pid = int(pidfile.read_text())
        deadline = time.monotonic()+2
        while time.monotonic()<deadline:
            try: os.kill(pid, 0)
            except ProcessLookupError: break
            time.sleep(.02)
        else:
            os.kill(pid, signal.SIGKILL)
            self.fail('owned detached authentication child survived')

    def test_auth_marker_is_checked_when_command_exits_immediately(self):
        command = self.cli("log=Path(sys.argv[sys.argv.index('--log-file')+1])\nlog.write_text('Print mode: triggering interactive OAuth\\n')\n")
        result = self.guarded(command)
        self.assertEqual(result.returncode, 78, result.stderr)

    def test_missing_os_guard_refuses_to_start_command(self):
        from unittest.mock import patch
        from quota_sentinel.platform.background_auth import AgyAuthGuard
        with patch('quota_sentinel.platform.background_auth.SANDBOX', self.root/'absent'):
            with self.assertRaisesRegex(ValueError, 'background_auth_guard_unavailable'):
                with AgyAuthGuard():
                    self.fail('guard admitted an unprotected command')

    def test_outer_output_abort_removes_private_cli_log(self):
        from quota_sentinel.helpers import antigravity_usage
        marker = self.root/'log-location'
        command = self.cli("log=Path(sys.argv[sys.argv.index('--log-file')+1])\nlog.write_text('fixture-secret\\n')\nPath(" + repr(str(marker)) + ").write_text(str(log))\ntime.sleep(.1)\nprint('x'*100000,flush=True)\ntime.sleep(120)\n")
        with self.assertRaisesRegex(antigravity_usage.QuotaError, 'output_too_large'):
            antigravity_usage.run_bounded(command, self.root, 4, 1024)
        log = Path(marker.read_text())
        if log.parent.exists():
            self.addCleanup(shutil.rmtree, log.parent)
        self.assertFalse(log.exists(), 'outer cancellation left private authentication log')
        self.assertFalse(log.parent.exists(), 'outer cancellation left temporary directory')

    def test_codexbar_only_chain_also_blocks_browser_launcher(self):
        from quota_sentinel.runtime.quota_probe import QuotaCollector
        marker = self.root/'bar-browser-blocked'
        bar = self.root/'codexbar'
        bar.write_text('#!' + sys.executable + '\nimport subprocess\nfrom pathlib import Path\n'
            + "try:\n subprocess.run(['/usr/bin/open','--help'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n blocked=False\nexcept PermissionError:\n blocked=True\n"
            + 'Path(' + repr(str(marker)) + ").write_text(str(blocked))\nprint('{}')\n")
        bar.chmod(0o700)
        collector = QuotaCollector(self.root/'state', self.root, codexbar_bin=bar,
            providers=('antigravity',), tier_chains={'antigravity':('codexbar-live',)})
        collector.collect()
        self.assertEqual(marker.read_text(), 'True')

    def test_unavailable_os_guard_keeps_configured_model_fallback(self):
        from unittest.mock import Mock, patch
        from quota_sentinel.runtime.agy_exec import AgyExecConfig, AgyExecRunner
        from quota_sentinel.runtime.models import AttemptResult
        agy = self.root/'agy'
        agy.write_text('#!' + sys.executable + "\nprint('1.2.16')\n")
        agy.chmod(0o700)
        fallback = Mock()
        fallback.run.return_value = AttemptResult(True, 0, False, 0,
            self.root/'out', self.root/'err', self.root/'quota', '')
        runner = AgyExecRunner(AgyExecConfig(agy_bin=agy, state_dir=self.root/'state',
            preflight=False), fallback=fallback)
        with patch('quota_sentinel.platform.background_auth.SANDBOX', self.root/'absent'):
            result = runner.run('antigravity', self.root/'work', 'initial', 1, 1)
        self.assertTrue(result.success)
        fallback.run.assert_called_once_with('antigravity', self.root/'work', 'initial', 1, 1)

    def test_native_auth_failure_defers_codexbar_and_recovers_next_collection(self):
        from quota_sentinel.runtime.quota_probe import QuotaCollector
        from quota_sentinel.quota import Tier
        auth_bad = self.root/'auth-bad'
        auth_bad.touch()
        marker = self.root/'codexbar-ran'
        report = {'status':'SUCCESS', 'num_turns':0,
            'usage':{'input_tokens':0, 'output_tokens':0, 'total_tokens':0},
            'command':{'name':'usage', 'data':{'groups':[
                {'name':'Claude and GPT models', 'buckets':[]},
                {'name':'Gemini Models', 'buckets':[
                    {'window':'5h', 'remaining_fraction':.755, 'reset_time':'2026-10-04T13:08:03Z'},
                    {'window':'weekly', 'remaining_fraction':.9, 'reset_time':'2026-10-11T08:08:03Z'}]}]}}}
        agy = self.root/'agy'
        agy.write_text('#!' + sys.executable + '\nimport sys,time\nfrom pathlib import Path\n'
            + "if '--version' in sys.argv: print('1.2.16');sys.exit(0)\n"
            + 'if Path(' + repr(str(auth_bad)) + ").exists():\n log=Path(sys.argv[sys.argv.index('--log-file')+1]);log.write_text('Print mode: silent auth failed\\n')\n while True:time.sleep(.05)\n"
            + 'print(' + repr(json.dumps(report)) + ')\n')
        agy.chmod(0o700)
        bar = self.root/'codexbar'
        bar.write_text('#!' + sys.executable + '\nfrom pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n')
        bar.chmod(0o700)
        logs = []
        collector = QuotaCollector(self.root/'state', self.root, agy_bin=agy,
            codexbar_bin=bar, providers=('antigravity',),
            tier_chains={'antigravity':('native','codexbar-live')}, logger=logs.append,
            antigravity_usage_helper=ROOT/'quota_sentinel/helpers/antigravity_usage.py',
            antigravity_native_timeout=4)
        first = collector.collect()['antigravity']
        self.assertIsNone(first.quota)
        self.assertFalse(marker.exists(), 'same failed CLI auth was retried through CodexBar')
        self.assertTrue(any('auth_required' in line for line in logs))
        auth_bad.unlink()
        second = collector.collect()['antigravity']
        self.assertEqual(second.tier, Tier.NATIVE)
        self.assertTrue(second.fresh)
        self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()
