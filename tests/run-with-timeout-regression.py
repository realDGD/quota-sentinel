#!/usr/bin/env python3
"""Lifecycle tests for run_with_timeout.py: pure stdlib, no model calls.

Covers the M-series contract:
  M1 normal exit 0 with byte-exact stdout passthrough
  M2 normal non-zero exit propagates
  M3 timeout -> SIGTERM to the process group -> exits within grace
  M4 TERM-ignoring child -> SIGKILL escalation after grace
  M5 grandchildren in the group are reaped (no orphans)
 plus helper-hygiene checks: stdout stays 100% child-owned, diagnostics never
 echo the command line, misuse exits 125, unspawnable children exit 127.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "run_with_timeout.py"
PY = sys.executable


class RunWithTimeoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_helper(self, *args, timeout=60):
        return subprocess.run(
            [PY, str(HELPER), *args], capture_output=True, text=True, timeout=timeout
        )

    def write_script(self, name, body):
        path = self.dir / name
        path.write_text(textwrap.dedent(body))
        path.chmod(0o755)
        return str(path)

    # -------------------------------------------------------------
    def test_m1_normal_exit_zero_passthrough(self):
        res = self.run_helper("--timeout", "10", "--", "/bin/echo", "hello")
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "hello\n")
        self.assertEqual(res.stderr, "")

    def test_m2_normal_exit_nonzero_propagates(self):
        res = self.run_helper("--timeout", "10", "--", "/bin/sh", "-c", "exit 3")
        self.assertEqual(res.returncode, 3)

    def test_m3_timeout_term_within_grace(self):
        started = time.monotonic()
        res = self.run_helper("--timeout", "1", "--kill-grace", "5", "--", "/bin/sleep", "30")
        elapsed = time.monotonic() - started
        self.assertEqual(res.returncode, 124)
        self.assertLess(elapsed, 4.0)  # TERM landed immediately, no 5s grace wait
        self.assertIn("timed out after 1s", res.stderr)
        self.assertIn("SIGTERM", res.stderr)

    def test_m4_term_ignored_kill_after_grace(self):
        child = self.write_script(
            "term_ignore.py",
            """\
            import signal, time
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(30)
            """,
        )
        started = time.monotonic()
        res = self.run_helper(
            "--timeout", "1", "--kill-grace", "1", "--", PY, child
        )
        elapsed = time.monotonic() - started
        self.assertEqual(res.returncode, 124)
        self.assertGreaterEqual(elapsed, 1.9)  # 1s timeout + 1s grace
        self.assertLess(elapsed, 5.0)
        self.assertIn("SIGKILL", res.stderr)

    def test_m5_no_orphan_grandchildren(self):
        pid_file = self.dir / "child.pid"
        script = self.write_script(
            "parent.sh",
            f"""\
            #!/bin/zsh
            {PY} -c 'import os,time,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)' {pid_file} &
            sleep 30
            """,
        )
        res = self.run_helper("--timeout", "1", "--kill-grace", "1", "--", script)
        self.assertEqual(res.returncode, 124)
        child_pid = int(pid_file.read_text().strip())
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)  # grandchild must be gone with the group

    def test_stdout_is_child_owned_even_on_timeout(self):
        script = self.write_script(
            "chatty.sh",
            """\
            #!/bin/zsh
            print -r -- "SUPERSECRETFLAG-marker"
            sleep 30
            """,
        )
        res = self.run_helper("--timeout", "1", "--kill-grace", "1", "--", script)
        self.assertEqual(res.returncode, 124)
        # Child stdout reached the caller untouched; the diagnostic (which must
        # not echo the command) went to stderr only.
        self.assertEqual(res.stdout, "SUPERSECRETFLAG-marker\n")
        self.assertIn("timed out", res.stderr)
        self.assertNotIn("SUPERSECRETFLAG", res.stderr)

    def test_helper_misuse_and_spawn_failure(self):
        res = self.run_helper("--timeout", "1")
        self.assertEqual(res.returncode, 125)
        res = self.run_helper("--timeout", "abc", "--", "/bin/true")
        self.assertEqual(res.returncode, 125)
        res = self.run_helper("--timeout", "1", "--", "/nonexistent-binary-xyz")
        self.assertEqual(res.returncode, 127)


if __name__ == "__main__":
    unittest.main()
