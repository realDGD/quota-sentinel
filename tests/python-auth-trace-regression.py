"""Persistent attribution, bounded retention, and secret-free auth diagnostics."""
import json
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def journal(self, **kwargs):
        from quota_sentinel.diagnostics.trace import TraceJournal
        return TraceJournal(self.root/'diagnostics', **kwargs)

    def test_retained_events_have_process_identity_and_drop_unapproved_fields(self):
        journal = self.journal()
        self.assertTrue(journal.emit('call_started', call_id='a'*32,
            root_id='b'*32, trigger='scheduler:watchdog', kind='agy-usage',
            access_token='fixture-secret', argv=['https://accounts.google.com/?code=fixture-secret']))
        records = journal.records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record['event'], 'call_started')
        self.assertEqual(record['trigger'], 'scheduler:watchdog')
        self.assertEqual(record['pid'], os.getpid())
        self.assertEqual(record['ppid'], os.getppid())
        self.assertIn('process_identity', record)
        self.assertNotIn('fixture-secret', json.dumps(records))

    def test_rotation_keeps_latest_records_inside_total_byte_limit(self):
        journal = self.journal(max_bytes=2500, chunk_bytes=700)
        for i in range(20):
            self.assertTrue(journal.emit('call_finished', call_id=('%032x' % i),
                kind='agy-usage', exit_code=i))
        records = journal.records()
        self.assertEqual(records[-1]['exit_code'], 19)
        self.assertLess(len(records), 20)
        self.assertLessEqual(sum(p.stat().st_size for p in journal.directory.glob('trace-*.jsonl')), 2500)

    def test_expiry_removes_only_owned_trace_files_and_leaves_recent_data(self):
        old = self.journal(clock=lambda: 1790000000)
        old.emit('call_started', call_id='a'*32, kind='agy-usage')
        unrelated = old.directory/'operator-notes.jsonl'
        unrelated.write_text('retain me')
        new = self.journal(clock=lambda: 1790000000+15*86400)
        new.emit('call_finished', call_id='b'*32, kind='agy-usage', exit_code=0)
        result = new.cleanup()
        self.assertEqual([r['call_id'] for r in new.records()], ['b'*32])
        self.assertEqual(unrelated.read_text(), 'retain me')

    def test_multiple_processes_append_complete_records(self):
        journal = self.journal()
        script = "from quota_sentinel.diagnostics.trace import TraceJournal;import sys; j=TraceJournal(sys.argv[1]);[j.emit('call_finished',call_id=('%032x'%n),kind='agy-usage',exit_code=n) for n in range(15)]"
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        processes = [subprocess.Popen([sys.executable,'-c',script,str(journal.directory)],env=env) for _ in range(3)]
        for process in processes:
            self.assertEqual(process.wait(timeout=15), 0)
        self.assertEqual(len(journal.records()), 45)

    def test_owned_launcher_records_pid_even_without_agy_wrapper(self):
        from quota_sentinel.diagnostics.trace import invocation
        from quota_sentinel.platform.process import spawn_owned, capture_owned
        env = dict(os.environ, QUOTA_SENTINEL_TRACE_DIR=str(self.root/'diagnostics'),
            QUOTA_SENTINEL_TRACE_TRIGGER='manual:usage')
        with invocation('codexbar-antigravity', environment=env) as span:
            process = spawn_owned([sys.executable,'-c','import os;print(os.getpid(),os.getppid())'],
                cwd=self.root, environment=span.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            launched_pid = process.pid
            result = capture_owned(process, timeout=3, kill_grace=0, max_bytes=4096)
            span.finish(exit_code=result.returncode)
        self.assertEqual(result.returncode, 0, result.stderr)
        pid, parent_pid = map(int, result.stdout.split())
        # Windows venv python.exe can be a redirector which creates the actual
        # interpreter as its child. The launch boundary owns the redirector.
        if os.name == 'nt' and pid != launched_pid:
            self.assertEqual(parent_pid, launched_pid)
        else:
            self.assertEqual(pid, launched_pid)
        records = self.journal().records()
        self.assertTrue(any(r['event']=='owned_process_started' and r.get('target_pid')==launched_pid for r in records), records)
        self.assertTrue(any(r['event']=='owned_process_stopped' and r.get('target_pid')==launched_pid for r in records), records)

    def test_context_carries_trigger_and_parent_call_without_changing_home(self):
        from quota_sentinel.diagnostics.trace import invocation
        env = dict(os.environ, QUOTA_SENTINEL_TRACE_DIR=str(self.root/'diagnostics'),
            QUOTA_SENTINEL_TRACE_TRIGGER='feishu:usage', QUOTA_SENTINEL_TRACE_ROOT='b'*32)
        with invocation('quota-native', environment=env) as parent:
            with invocation('agy-usage', environment=parent.environment) as child:
                self.assertEqual(child.environment['HOME'], env['HOME'])
                self.assertEqual(child.parent_id, parent.call_id)
                child.finish(exit_code=0)
            parent.finish(exit_code=0)
        records = self.journal().records()
        self.assertEqual({r['root_id'] for r in records}, {'b'*32})
        self.assertEqual({r['trigger'] for r in records}, {'feishu:usage'})
        self.assertTrue(any(r.get('parent_id')==parent.call_id for r in records))

    def test_missing_trace_storage_does_not_break_the_command(self):
        from quota_sentinel.diagnostics.trace import invocation
        obstruction = self.root/'file';obstruction.write_text('not a directory')
        diagnostics = io.StringIO()
        with contextlib.redirect_stderr(diagnostics):
            with invocation('agy-usage', environment=dict(os.environ,
                QUOTA_SENTINEL_TRACE_DIR=str(obstruction/'diagnostics'))) as span:
                span.finish(exit_code=0)
        self.assertEqual(obstruction.read_text(), 'not a directory')
        self.assertIn('coverage incomplete', diagnostics.getvalue())

    def test_validated_recovery_links_to_prior_auth_failure(self):
        journal = self.journal()
        journal.emit('auth_required', call_id='a'*32, kind='agy-usage', marker='silent_auth_failed')
        journal.emit('quota_validated', call_id='b'*32, kind='quota-native', fresh=True)
        recovery = [r for r in journal.records() if r['event']=='auth_recovered']
        self.assertEqual(len(recovery), 1)
        self.assertEqual(recovery[0]['previous_failure_call'], 'a'*32)
        journal.emit('quota_validated', call_id='c'*32, kind='quota-native', fresh=True)
        self.assertEqual(len([r for r in journal.records() if r['event']=='auth_recovered']), 1)

    def test_cli_can_show_status_filter_calls_and_cleanup_without_credentials(self):
        journal = self.journal()
        journal.emit('call_started', call_id='a'*32, kind='agy-usage')
        journal.emit('call_started', call_id='b'*32, kind='agy-usage')
        base = [sys.executable, '-B', '-m', 'quota_sentinel', '--state-dir', str(self.root), 'trace']
        status = subprocess.run([*base, 'status'], capture_output=True, timeout=10)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)['retention_days'], 14)
        shown = subprocess.run([*base, 'show', '--call-id', 'a'*32], capture_output=True, timeout=10)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual([json.loads(line)['call_id'] for line in shown.stdout.splitlines()], ['a'*32])
        cleaned = subprocess.run([*base, 'cleanup'], capture_output=True, timeout=10)
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr)
        self.assertIn('retained_bytes', json.loads(cleaned.stdout))

    def test_runtime_command_preserves_inherited_feishu_source(self):
        from quota_sentinel import __main__ as cli
        from quota_sentinel.diagnostics.trace import invocation, trace_environment
        env = dict(os.environ, QUOTA_SENTINEL_TRACE_TRIGGER='feishu:usage')
        factory = mock.Mock(NotReadyError=type('NotReadyError', (Exception,), {}))
        def action(_factory):
            with invocation('quota-native', environment=trace_environment(os.environ, state_dir=self.root)):
                pass
        with mock.patch.dict(os.environ, env), mock.patch.object(cli, '_runtime', return_value=factory):
            self.assertEqual(cli._run_runtime(self.root, action, 'usage'), 0)
        records = self.journal().records()
        commands = [r for r in records if r['event']=='call_started' and r['kind']=='command']
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]['trigger'], 'feishu:usage')
        self.assertTrue(any(r.get('parent_id')==commands[0]['call_id'] for r in records))

    def test_stale_auth_state_expires_with_log_retention(self):
        now = 1790000000
        journal = self.journal(clock=lambda:now)
        journal.emit('auth_required', call_id='a'*32, kind='agy-usage')
        later = self.journal(clock=lambda:now+15*86400)
        later.cleanup()
        later.emit('quota_validated', call_id='b'*32, kind='quota-native', fresh=True)
        self.assertFalse(any(r['event']=='auth_recovered' for r in later.records()))

    def test_scheduler_child_and_external_task_have_distinct_roots_and_sources(self):
        from quota_sentinel.helpers.task_orchestrator import TaskOrchestrator
        from quota_sentinel.diagnostics.trace import Invocation, trace_environment
        store = mock.Mock()
        store.begin_run.return_value = 7
        schedule = mock.Mock(state_dir=self.root)
        schedule.snapshot.return_value = {}
        script = "import os;from quota_sentinel.diagnostics.trace import Invocation;Invocation('fixture-child',os.environ,reuse=True).note('fixture_called')"
        runner = TaskOrchestrator(schedule_state=schedule, store=store,
            scheduler_command=[sys.executable, '-c', script])
        self.assertEqual(runner._run_check('watchdog', 0).exit_code, 0)
        runner.run_external_task('usage', 'feishu:private-message-id',
            lambda: Invocation('fixture-external', trace_environment(os.environ), reuse=True).note('fixture_called'))
        records = self.journal().records()
        calls = [r for r in records if r['event']=='fixture_called']
        self.assertEqual({r['trigger'] for r in calls}, {'scheduler:watchdog', 'feishu:usage'})
        self.assertEqual(len({r['root_id'] for r in calls}), 2)
        self.assertTrue(any(r.get('task_run_id')==7 for r in records))
        self.assertNotIn('private-message-id', json.dumps(records))

    def test_private_permissions_and_cleanup_preserve_symlinks(self):
        journal = self.journal()
        journal.emit('call_started', call_id='a'*32)
        if os.name == 'nt':
            return  # The platform-files suite verifies Windows user-only DACLs.
        self.assertEqual(journal.directory.stat().st_mode & 0o777, 0o700)
        for path in journal.directory.glob('trace-*.jsonl'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        target = self.root/'unrelated';target.write_text('retain me')
        link = journal.directory/'trace-20000101-000000.jsonl'
        link.symlink_to(target)
        journal.cleanup()
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(), 'retain me')

    def test_observer_stops_its_owned_stream_and_persists_reported_caller(self):
        from quota_sentinel.diagnostics.browser import BrowserObserver
        import time
        event = {'processID':99,'eventMessage':'openURL Google Chrome caller_pid=4321 https://accounts.google.com/?code=fixture-secret'}
        command = [sys.executable,'-u','-c', 'import time;print(' + repr(json.dumps(event)) + ');time.sleep(120)']
        observer = BrowserObserver(self.root/'diagnostics', command=command, system='darwin')
        with observer:
            deadline = time.monotonic()+3
            while time.monotonic() < deadline and not any(r['event']=='system_browser_event' for r in observer.journal.records()):
                time.sleep(.02)
        records = observer.journal.records()
        self.assertTrue(any(r['event']=='system_browser_event' and r.get('caller_pid')==4321 for r in records))
        self.assertFalse(observer.thread.is_alive())
        self.assertNotIn('fixture-secret', json.dumps(records))
        self.assertFalse(any(r['event']=='observer_gap' for r in records))

    def test_healthy_observer_survives_the_former_session_deadline(self):
        from quota_sentinel.diagnostics import browser
        # Advance only the observer's clock; the real child and pipe remain
        # live. A healthy stream must not be killed merely because time passed.
        offset = [0]
        clock = SimpleNamespace(time=time.time, monotonic=lambda:time.monotonic()+offset[0])
        command = [sys.executable, '-u', '-c',
            "import time\nwhile True:\n print('{}',flush=True);time.sleep(.02)\n"]
        observer = browser.BrowserObserver(self.root/'diagnostics', command=command, system='darwin')
        with mock.patch.object(browser, 'time', clock), observer:
            deadline = time.monotonic()+3
            while time.monotonic()<deadline and observer._process is None:
                time.sleep(.02)
            process = observer._process
            self.assertIsNotNone(process)
            offset[0] = 360
            time.sleep(.2)
            self.assertIsNone(process.poll(), 'healthy stream was terminated at the old session deadline')
        records = observer.journal.records()
        self.assertEqual(sum(r['event']=='observer_started' for r in records), 1)
        self.assertFalse(any(r['event']=='observer_gap' for r in records), records)
        self.assertFalse(observer.thread.is_alive())

    def test_observer_reconnects_after_a_real_failure_and_preserves_its_exit(self):
        from quota_sentinel.diagnostics.browser import BrowserObserver
        marker = self.root/'stream-started'
        event = json.dumps({'eventMessage':'openURL Google Chrome caller_pid=4321'})
        script = ("import sys,time\nfrom pathlib import Path\np=Path(sys.argv[1])\n"
                  "if not p.exists():\n p.touch();sys.exit(77)\n"
                  "print(sys.argv[2],flush=True)\ntime.sleep(120)\n")
        observer = BrowserObserver(self.root/'diagnostics',
            command=[sys.executable,'-u','-c',script,str(marker),event], system='darwin')
        with observer:
            deadline = time.monotonic()+4
            while time.monotonic()<deadline and not any(r['event']=='system_browser_event' for r in observer.journal.records()):
                time.sleep(.02)
            records = observer.journal.records()
            self.assertTrue(any(r['event']=='system_browser_event' for r in records), 'stream did not reconnect promptly')
        records = observer.journal.records()
        gaps = [r for r in records if r['event']=='observer_gap']
        self.assertEqual(len(gaps), 1, gaps)
        self.assertEqual(gaps[0]['exit_code'], 77)
        self.assertFalse(observer.thread.is_alive())

    def test_system_event_does_not_confuse_log_emitter_with_caller(self):
        from quota_sentinel.diagnostics.browser import browser_event
        raw = {'timestamp':'2026-10-04 13:00:00.000000+0000','processID':99,
            'processImagePath':'/System/Library/CoreServices/launchservicesd',
            'eventMessage':'openURL Google Chrome https://accounts.google.com/o/oauth2/auth?code=fixture-secret'}
        event = browser_event(raw)
        self.assertEqual(event['emitter_pid'], 99)
        self.assertEqual(event['attribution'], 'unattributed')
        self.assertNotIn('caller_pid', event)
        self.assertNotIn('fixture-secret', json.dumps(event))

    def test_explicit_system_caller_is_recorded_as_reported_evidence(self):
        from quota_sentinel.diagnostics.browser import browser_event
        event = browser_event({'processID':99,'eventMessage':'openURL Google Chrome caller_pid=4321 https://accounts.google.com/o/oauth2/auth?token=fixture-secret'})
        self.assertEqual(event['caller_pid'], 4321)
        self.assertEqual(event['attribution'], 'system-reported-caller')
        self.assertEqual(event['url_class'], 'google-oauth')
        self.assertNotIn('fixture-secret', json.dumps(event))

    def test_url_query_cannot_masquerade_as_a_system_caller_field(self):
        from quota_sentinel.diagnostics.browser import browser_event
        event = browser_event({'processID':99, 'eventMessage':
            'openURL Google Chrome https://accounts.google.com/o/oauth2/auth?caller_pid=4321'})
        self.assertEqual(event['attribution'], 'unattributed')
        self.assertNotIn('caller_pid', event)

    def test_recycled_pid_does_not_attribute_an_old_event_to_current_executable(self):
        from quota_sentinel.diagnostics.browser import browser_event
        event = browser_event({'timestamp':'2000-01-01T00:00:00+00:00',
            'eventMessage':'openURL Google Chrome caller_pid='+str(os.getpid())})
        self.assertEqual(event['caller_pid'], os.getpid())
        self.assertFalse(event['caller_identity']['identity_known'])
        self.assertIsNone(event['caller_identity']['executable'])

    @unittest.skipUnless(sys.platform=='darwin', 'macOS log and native start identity')
    def test_current_caller_identity_accepts_native_unified_log_timestamp(self):
        from datetime import datetime, timezone
        from quota_sentinel.diagnostics.browser import browser_event
        stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f%z')
        event = browser_event({'timestamp':stamp,
            'eventMessage':'openURL Google Chrome caller_pid='+str(os.getpid())})
        self.assertTrue(event['caller_identity']['identity_known'])
        self.assertEqual(event['caller_identity']['pid'], os.getpid())

    def test_unrelated_system_events_are_not_saved(self):
        from quota_sentinel.diagnostics.browser import browser_event
        self.assertIsNone(browser_event({'processID':99,'eventMessage':'routine filesystem metadata refresh'}))

    @unittest.skipUnless(sys.platform=='darwin', 'real macOS authentication boundary')
    def test_helper_records_exact_owned_pid_auth_marker_and_exit(self):
        fixture = self.root/'agy.py'
        fixture.write_text("import os,sys,time\nfrom pathlib import Path\nlog=Path(sys.argv[sys.argv.index('--log-file')+1]);log.write_text('fixture-secret\\nPrint mode: triggering interactive OAuth\\n');print(os.getpid(),flush=True);time.sleep(120)\n")
        env = dict(os.environ, QUOTA_SENTINEL_TRACE_DIR=str(self.root/'diagnostics'),
            QUOTA_SENTINEL_TRACE_TRIGGER='scheduler:watchdog',
            QUOTA_SENTINEL_TRACE_ROOT='b'*32)
        result = subprocess.run([sys.executable,str(ROOT/'quota_sentinel/helpers/run_with_timeout.py'),
            '--agy-auth-guard','--timeout','4','--kill-grace','.1','--',sys.executable,str(fixture)],
            env=env,capture_output=True,timeout=8)
        self.assertEqual(result.returncode, 78, result.stderr)
        pid = int(result.stdout.strip())
        records = self.journal().records()
        self.assertTrue(any(r['event']=='process_started' and r.get('target_pid')==pid for r in records))
        self.assertTrue(any(r['event']=='auth_required' and r.get('target_pid')==pid for r in records))
        self.assertTrue(any(r['event']=='process_finished' and r.get('exit_code')==78 for r in records))
        self.assertEqual({r['trigger'] for r in records}, {'scheduler:watchdog'})
        self.assertNotIn('fixture-secret', json.dumps(records))


if __name__ == '__main__':
    unittest.main()
