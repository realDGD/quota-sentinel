#!/usr/bin/env python3
"""Model attempt parity, using only a fake Pi executable and temp credentials."""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from quota_sentinel.runtime.models import (
    PI_AUTH_TIMEOUT_SECONDS,
    ModelRunner,
    ModelRunnerConfig,
    PreparedPaths,
)


class _ReadCounter:
    """File-object proxy that records how many bytes a read consumed."""

    def __init__(self, handle) -> None:
        self._handle = handle
        self.bytes_read = 0

    def read(self, *args, **kwargs):
        data = self._handle.read(*args, **kwargs)
        self.bytes_read += len(data)
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._handle.close()
        return False

    def __getattr__(self, name):
        return getattr(self._handle, name)


class ModelRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="quota-sentinel-model-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "work"
        self.auth = self.root / "auth.json"
        self.auth.write_text('{"openai-codex":{"token":"secret-auth"}}')
        self.auth.chmod(0o600)
        self.capture = self.root / "calls.jsonl"
        self.fake_pi = self.root / "pi"
        self.fake_pi.write_text(
            "#!" + sys.executable + "\n" + textwrap.dedent("""\
            import json, os, subprocess, sys, time
            from pathlib import Path
            call = {
                'argv': sys.argv[1:],
                'cwd': os.getcwd(),
                'env': {key: os.environ.get(key) for key in (
                    'PI_CODING_AGENT_DIR', 'PI_CODEX_QUOTA_FILE',
                    'PI_ANTIGRAVITY_QUOTA_FILE', 'PI_OPENCODE_QUOTA_FILE',
                    'PI_OFFLINE', 'ANTIGRAVITY_NO_PREWARM')},
            }
            with open(os.environ['QS_CAPTURE_PATH'], 'a') as out:
                out.write(json.dumps(call) + '\\n')
            if sys.argv[1:3] == ['auth', 'print-bearer-token']:
                print('secret-bearer-token')
                sys.exit(0)
            mode = os.environ.get('QS_FAKE_MODE', 'success')
            if mode in ('crlf', 'cr', 'unterminated'):
                sys.stdout.write({'crlf': '1\\r\\n', 'cr': '1\\r', 'unterminated': '1'}[mode])
                sys.stdout.flush()
                sys.exit(0)
            if mode == 'timeout':
                child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
                Path(os.environ['QS_CHILD_PID']).write_text(str(child.pid))
                time.sleep(30)
            if mode == 'bad_output':
                print('1 extra')
            else:
                print('1')
            if mode == 'secret_error':
                print('Authorization: Bearer very-private-token', file=sys.stderr)
                print('api_key=another-private-key', file=sys.stderr)
                print(Path(os.environ['PI_CODING_AGENT_DIR'], 'auth.json').read_text(), file=sys.stderr)
                sys.exit(3)
            if mode == 'space_secret_error':
                print('invalid token abc123def was supplied', file=sys.stderr)
                print('refresh token refresh-secret-xyz password hunter2pass', file=sys.stderr)
                print('bearer bearer-secret-abc secret secret-value-abc '
                      'api-key apikey-secret-abc sk-livekey-abc', file=sys.stderr)
                sys.exit(9)
            if mode == 'split_secret_error':
                print('before', file=sys.stderr)
                print('token', file=sys.stderr)
                print('split-secret-xyz', file=sys.stderr)
                sys.exit(9)
            if mode == 'long_secret_error':
                print('y' * 290, file=sys.stderr)
                print('token truncated-secret-xyz', file=sys.stderr)
                sys.exit(9)
            if mode == 'mock_retry_failure':
                print('mock failure attempt 1: bearer SuperSecretTokenValue123', file=sys.stderr)
                sys.exit(1)
            if mode == 'nonzero':
                sys.exit(7)
            """)
        )
        self.fake_pi.chmod(0o755)
        self.env = {
            "QS_CAPTURE_PATH": str(self.capture),
            "QS_CHILD_PID": str(self.root / "child.pid"),
        }
        self.config = ModelRunnerConfig(
            pi_bin=self.fake_pi,
            auth_file=self.auth,
            repo_dir=REPO,
            antigravity_provider_extension=self.root / "antigravity-provider.ts",
            timeout=2,
            kill_grace=0.2,
            environment=self.env,
        )
        self.runner = ModelRunner(self.config)

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.capture.read_text().splitlines()]

    def test_from_env_treats_empty_values_as_unset(self) -> None:
        """``${VAR:-default}`` substitutes for an empty value too."""
        config = ModelRunnerConfig.from_env({
            "HOME": str(self.root),
            "QUOTA_SENTINEL_PI_BIN": "",
            "QUOTA_SENTINEL_PI_AUTH_FILE": "",
            "QUOTA_SENTINEL_MODEL_TIMEOUT": "",
            "QUOTA_SENTINEL_MODEL_KILL_GRACE": "",
        })
        self.assertEqual(config.pi_bin, Path("/opt/homebrew/bin/pi"))
        self.assertEqual(config.auth_file, self.root / ".pi/agent/auth.json")
        self.assertEqual(config.timeout, 300)
        self.assertEqual(config.kill_grace, 10)

    def test_from_env_falls_back_on_non_numeric_durations(self) -> None:
        config = ModelRunnerConfig.from_env({
            "HOME": str(self.root),
            "QUOTA_SENTINEL_MODEL_TIMEOUT": "later",
            "QUOTA_SENTINEL_MODEL_KILL_GRACE": "soon",
        })
        self.assertEqual(config.timeout, 300)
        self.assertEqual(config.kill_grace, 10)

    def test_from_env_honours_valid_values(self) -> None:
        config = ModelRunnerConfig.from_env({
            "HOME": str(self.root),
            "QUOTA_SENTINEL_PI_BIN": "/tmp/fake-pi",
            "QUOTA_SENTINEL_PI_AUTH_FILE": str(self.auth),
            "QUOTA_SENTINEL_MODEL_TIMEOUT": "12.5",
            "QUOTA_SENTINEL_MODEL_KILL_GRACE": "0.25",
        })
        self.assertEqual(config.pi_bin, Path("/tmp/fake-pi"))
        self.assertEqual(config.auth_file, self.auth)
        self.assertEqual(config.timeout, 12.5)
        self.assertEqual(config.kill_grace, 0.25)
        from quota_sentinel.helpers import resource_path
        self.assertEqual(config.repo_dir, resource_path("run_with_timeout.py").parent)

    def test_prepare_copies_private_auth_and_provider_settings(self) -> None:
        for provider, settings in (("codex", '{"transport":"sse"}\n'),
                                   ("antigravity", '{}\n'), ("opencode", '{}\n')):
            with self.subTest(provider=provider):
                paths = self.runner.prepare(provider, self.workspace)
                self.assertEqual((paths.agent_dir / "auth.json").read_bytes(), self.auth.read_bytes())
                self.assertEqual((paths.agent_dir / "settings.json").read_text(), settings)
                self.assertEqual((paths.agent_dir / "auth.json").stat().st_mode & 0o777, 0o600)
                self.assertEqual(paths.agent_dir.stat().st_mode & 0o777, 0o700)
                self.assertEqual(paths.quota_path, self.workspace / (provider + "-quota.json"))
        self.assertEqual(self.calls()[0]["argv"],
                         ["auth", "print-bearer-token", "--provider", "openai-codex"])
        self.assertIsNone(self.calls()[0]["env"]["PI_OFFLINE"])
        self.assertIsNone(self.calls()[0]["env"]["PI_CODEX_QUOTA_FILE"])

    def test_missing_auth_file_degrades_without_raising(self) -> None:
        """The shell's ``cp -p`` is non-fatal; a rotated credential must not raise."""
        self.auth.unlink()
        self._assert_prepare_degrades(self.workspace)

    def test_unreadable_auth_file_degrades_without_raising(self) -> None:
        self.auth.unlink()
        self.auth.mkdir()
        self._assert_prepare_degrades(self.workspace / "unreadable")

    def _assert_prepare_degrades(self, workspace: Path) -> None:
        diagnostics = io.StringIO()
        with contextlib.redirect_stderr(diagnostics):
            paths = self.runner.prepare("opencode", workspace)
        # settings.json is still written and prepare() returns usable paths...
        self.assertEqual((paths.agent_dir / "settings.json").read_text(), "{}\n")
        self.assertFalse((paths.agent_dir / "auth.json").exists())
        # ...the cp-style diagnostic still reaches stderr...
        self.assertIn(str(self.auth), diagnostics.getvalue())
        # ...and the attempt still runs and is recorded as a normal failure.
        self.env["QS_FAKE_MODE"] = "nonzero"
        result = self.runner.run("opencode", workspace, "initial", 1, 1)
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 7)
        self.assertEqual(len(self.calls()), 1)

    def test_exact_pi_argv_and_environment_for_three_providers(self) -> None:
        common = [
            "--mode", "text", "--print", "--no-session", "--no-tools",
            "--no-extensions", "--no-skills", "--no-prompt-templates",
            "--no-themes", "--no-context-files", "--no-approve", "--offline",
            "--system-prompt", "忽略上下文",
        ]
        cases = {
            "codex": ("openai-codex", "gpt-6-luna", "off",
                      [str(REPO / "capture-codex-quota.ts")], "PI_CODEX_QUOTA_FILE"),
            "antigravity": ("antigravity", "gemini-3.7-flash", "low",
                            [str(self.root / "antigravity-provider.ts"),
                             str(REPO / "capture-antigravity-quota.ts")],
                            "PI_ANTIGRAVITY_QUOTA_FILE"),
            "opencode": ("opencode-go", "deepseek-v4.1-flash", "off",
                         [str(REPO / "capture-opencode-quota.ts")], "PI_OPENCODE_QUOTA_FILE"),
        }
        for provider, (pi_provider, model, thinking, extensions, quota_key) in cases.items():
            with self.subTest(provider=provider):
                result = self.runner.run(provider, self.workspace, "initial", 1, 3)
                self.assertTrue(result.success)
                self.assertEqual(result.exit_code, 0)
                self.assertFalse(result.timed_out)
                self.assertGreaterEqual(result.elapsed, 0)
                self.assertEqual(result.stdout_path.read_text(), "1\n")
                call = self.calls()[-1]
                self.assertEqual(call["argv"], [
                    "--provider", pi_provider, "--model", model, "--thinking", thinking,
                    *common, *(arg for extension in extensions for arg in ("--extension", extension)),
                    "--", "不用思考，只回复我 1",
                ])
                self.assertIn("quota-pi-turn.", Path(call["cwd"]).parent.name)
                self.assertFalse(Path(call["cwd"]).exists())
                self.assertEqual(call["env"]["PI_CODING_AGENT_DIR"], str(self.workspace / (provider + "-agent")))
                self.assertEqual(call["env"][quota_key], str(result.quota_path))
                self.assertEqual(call["env"]["PI_OFFLINE"], "1")
                self.assertEqual(call["env"]["ANTIGRAVITY_NO_PREWARM"],
                                 "1" if provider == "antigravity" else None)

    def test_non_exact_output_and_nonzero_exit_fail(self) -> None:
        self.env["QS_FAKE_MODE"] = "bad_output"
        result = self.runner.run("opencode", self.workspace, "retry", 2, 3)
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 0)
        self.env["QS_FAKE_MODE"] = "nonzero"
        result = self.runner.run("opencode", self.workspace, "retry", 3, 3)
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 7)

    def test_carriage_return_terminated_one_is_not_success(self) -> None:
        """Text mode would translate CRLF away; the shell compares raw bytes."""
        for mode, raw in (("crlf", b"1\r\n"), ("cr", b"1\r")):
            with self.subTest(mode=mode):
                self.env["QS_FAKE_MODE"] = mode
                result = self.runner.run("opencode", self.workspace, "initial", 1, 1)
                self.assertEqual(result.stdout_path.read_bytes(), raw)
                self.assertEqual(result.exit_code, 0)
                self.assertFalse(result.success)

    def test_newline_terminated_and_bare_one_succeed(self) -> None:
        for mode, raw in (("success", b"1\n"), ("unterminated", b"1")):
            with self.subTest(mode=mode):
                self.env["QS_FAKE_MODE"] = mode
                result = self.runner.run("opencode", self.workspace, "initial", 1, 1)
                self.assertEqual(result.stdout_path.read_bytes(), raw)
                self.assertTrue(result.success)
                self.assertEqual(result.exit_code, 0)

    def test_attempt_log_lines_match_the_shell(self) -> None:
        """run() must emit the shell's one-line-per-attempt run log."""
        lines: list[str] = []
        self.runner = ModelRunner(self.config, logger=lines.append)
        result = self.runner.run("codex", self.workspace, "initial", 1, 3)
        self.assertTrue(result.success)
        self.assertEqual(len(lines), 1)
        self.assertEqual(
            lines[0],
            "model codex phase=initial attempt=1/3 result=success elapsed=%ds"
            % int(result.elapsed),
        )

        self.env["QS_FAKE_MODE"] = "nonzero"
        lines.clear()
        result = self.runner.run("opencode", self.workspace, "retry", 2, 3)
        self.assertFalse(result.success)
        self.assertEqual(
            lines[0],
            "model opencode phase=retry attempt=2/3 result=failed rc=7 elapsed=%ds"
            % int(result.elapsed),
        )
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1], "model opencode attempt=2 error: " + result.error_summary)

    def test_timeout_log_line_reports_rc_124(self) -> None:
        lines: list[str] = []
        self.runner = ModelRunner(self.config, logger=lines.append)
        self.env["QS_FAKE_MODE"] = "timeout"
        self.runner.config.timeout = 0.5
        result = self.runner.run("antigravity", self.workspace, "initial", 1, 1)
        self.assertTrue(result.timed_out)
        self.assertEqual(
            lines[0],
            "model antigravity phase=initial attempt=1/1 result=timeout rc=124 elapsed=%ds"
            % int(result.elapsed),
        )
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].startswith("model antigravity attempt=1 error: "))

    def test_logger_defaults_to_noop_and_config_logger_is_honoured(self) -> None:
        # No seam supplied: the attempt must still run and stay silent.
        result = self.runner.run("opencode", self.workspace, "initial", 1, 1)
        self.assertTrue(result.success)
        # The seam may also be injected through the config object.
        lines: list[str] = []
        runner = ModelRunner(ModelRunnerConfig(
            pi_bin=self.config.pi_bin,
            auth_file=self.config.auth_file,
            repo_dir=self.config.repo_dir,
            antigravity_provider_extension=self.config.antigravity_provider_extension,
            timeout=self.config.timeout,
            kill_grace=self.config.kill_grace,
            environment=self.env,
            logger=lines.append,
        ))
        result = runner.run("opencode", self.workspace, "initial", 1, 1)
        self.assertEqual(
            lines[0],
            "model opencode phase=initial attempt=1/1 result=success elapsed=%ds"
            % int(result.elapsed),
        )

    def test_logged_error_line_carries_the_redacted_summary(self) -> None:
        lines: list[str] = []
        self.runner = ModelRunner(self.config, logger=lines.append)
        self.env["QS_FAKE_MODE"] = "space_secret_error"
        result = self.runner.run("opencode", self.workspace, "initial", 1, 1)
        self.assertEqual(lines[1], "model opencode attempt=1 error: " + result.error_summary)
        self.assertNotIn("abc123def", lines[1])
        self.assertIn("token***", lines[1])

    def test_error_line_matches_the_pinned_retry_regression_string(self) -> None:
        """tests/retry-regression.zsh:363 pins this exact masked summary."""
        lines: list[str] = []
        self.runner = ModelRunner(self.config, logger=lines.append)
        self.env["QS_FAKE_MODE"] = "mock_retry_failure"
        result = self.runner.run("codex", self.workspace, "initial", 1, 3)
        self.assertFalse(result.success)
        self.assertEqual(
            lines[1],
            "model codex attempt=1 error: mock failure attempt 1: bearer***",
        )

    def test_relative_workspace_still_sets_absolute_child_paths(self) -> None:
        relative = Path(os.path.relpath(self.workspace, Path.cwd()))
        result = self.runner.run("opencode", relative, "initial", 1, 1)
        self.assertTrue(result.success)
        self.assertEqual(result.quota_path, self.workspace / "opencode-quota.json")
        self.assertEqual(self.calls()[-1]["env"]["PI_CODING_AGENT_DIR"],
                         str(self.workspace / "opencode-agent"))

    def test_timeout_kills_process_group(self) -> None:
        self.env["QS_FAKE_MODE"] = "timeout"
        self.runner.config.timeout = 0.5
        result = self.runner.run("opencode", self.workspace, "initial", 1, 1)
        self.assertFalse(result.success)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.exit_code, 124)
        child_pid = int((self.root / "child.pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    def test_diagnostic_summary_redacts_secrets(self) -> None:
        self.env["QS_FAKE_MODE"] = "secret_error"
        result = self.runner.run("codex", self.workspace, "initial", 1, 1)
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 3)
        self.assertNotIn("very-private-token", result.error_summary)
        self.assertNotIn("another-private-key", result.error_summary)
        self.assertNotIn("secret-auth", result.error_summary)
        self.assertIn("Authorization", result.error_summary)
        self.assertIn("very-private-token", result.stderr_path.read_text())

    def test_space_separated_credentials_are_masked(self) -> None:
        """The shell masks a keyword plus the following token, not only k=v/k: v.

        ``perl -pe 's/(token|secret|...)\\S*(\\s+\\S+)?/\\1***/gi'`` also catches
        ``bearer X``, ``password X`` and ``token X`` shapes, which the old
        separator-only passes let through verbatim.
        """
        self.env["QS_FAKE_MODE"] = "space_secret_error"
        result = self.runner.run("opencode", self.workspace, "initial", 1, 1)
        summary = result.error_summary
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 9)
        for leaked in ("abc123def", "refresh-secret-xyz", "hunter2pass",
                       "bearer-secret-abc", "secret-value-abc",
                       "apikey-secret-abc", "sk-livekey-abc"):
            with self.subTest(leaked=leaked):
                self.assertNotIn(leaked, summary)
        self.assertIn("invalid token*** was supplied", summary)
        self.assertIn("password***", summary)
        self.assertIn("bearer***", summary)
        self.assertIn("secret***", summary)
        self.assertIn("api-key***", summary)
        self.assertIn("sk-***", summary)
        self.assertIn("sk-livekey-abc", result.stderr_path.read_text())

    def test_error_summary_reads_only_the_stderr_tail(self) -> None:
        """A huge stderr must not be slurped whole just to take its last 3 lines."""
        import quota_sentinel.runtime.models as models
        from unittest import mock

        stderr = self.root / "huge-stderr"
        stderr.write_text(("noise " * 40 + "\n") * 4000
                          + "kept-one\nkept-two token hidden-value\n")
        size = stderr.stat().st_size
        self.assertGreater(size, 100_000)
        counters: list = []
        real_open = models.Path.open

        def counting_open(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            if path == stderr:
                counter = _ReadCounter(handle)
                counters.append(counter)
                return counter
            return handle

        with mock.patch.object(models.Path, "open", counting_open):
            summary = models._safe_error_summary(stderr)
        self.assertEqual(len(counters), 1)
        self.assertLess(counters[0].bytes_read, size)
        self.assertLessEqual(counters[0].bytes_read, 8192)
        self.assertIn("kept-one", summary)
        self.assertIn("kept-two token***", summary)
        self.assertNotIn("hidden-value", summary)
        self.assertLessEqual(len(summary), 300)

    def test_redaction_survives_line_collapse_and_truncation(self) -> None:
        """Collapsing newlines must not push a keyword away from its value."""
        self.env["QS_FAKE_MODE"] = "split_secret_error"
        result = self.runner.run("opencode", self.workspace, "initial", 1, 1)
        self.assertNotIn("split-secret-xyz", result.error_summary)
        self.assertIn("token***", result.error_summary)
        self.env["QS_FAKE_MODE"] = "long_secret_error"
        result = self.runner.run("opencode", self.workspace, "initial", 2, 2)
        self.assertNotIn("truncated-secret-xyz", result.error_summary)
        self.assertNotIn("truncat", result.error_summary)
        self.assertLessEqual(len(result.error_summary), 300)

    # ---- the shipped priority: Pi is the cheap path, not the only one -----
    def _stub_fallback(self):
        class _StubFallback:
            TRANSPORT = "codex"

            def __init__(self):
                self.prepared = []
                self.runs = []

            def prepare(self, provider, workspace):
                self.prepared.append(provider)

            def run(self, provider, workspace, phase, attempt, limit):
                self.runs.append((provider, phase, attempt, limit))
                return type("Result", (), {
                    "success": True, "exit_code": 0, "timed_out": False,
                    "elapsed": 0.5,
                    "stdout_path": Path(workspace) / "fb-stdout",
                    "stderr_path": Path(workspace) / "fb-stderr",
                    "quota_path": Path(workspace) / "fb-quota.json",
                    "error_summary": "",
                })()

        return _StubFallback()

    def test_pi_failure_hands_the_attempt_to_the_configured_fallback(self) -> None:
        """A failed Pi attempt is delivered by the fallback, not reported lost."""
        stub = self._stub_fallback()
        lines: list = []
        runner = ModelRunner(self.config, logger=lines.append, fallback_for={"codex": stub})
        self.env["QS_FAKE_MODE"] = "nonzero"  # the fake Pi exits 7
        result = runner.run("codex", self.workspace, "initial", 1, 3)
        self.assertTrue(result.success)
        self.assertEqual(stub.prepared, ["codex"])
        self.assertEqual(stub.runs, [("codex", "initial", 1, 3)])
        self.assertIn("transport=pi -> fallback=codex", " ".join(lines))
        self.assertIn("reason=exit=7", " ".join(lines))

    def test_without_a_fallback_a_pi_failure_stays_a_failure(self) -> None:
        self.env["QS_FAKE_MODE"] = "nonzero"
        result = self.runner.run("codex", self.workspace, "initial", 1, 3)
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 7)

    def test_a_provider_outside_the_mapping_never_delegates(self) -> None:
        stub = self._stub_fallback()
        runner = ModelRunner(self.config, fallback_for={"codex": stub})
        self.env["QS_FAKE_MODE"] = "nonzero"
        result = runner.run("antigravity", self.workspace, "initial", 1, 3)
        self.assertFalse(result.success)
        self.assertEqual(stub.runs, [])

    def test_timeout_is_reported_as_the_fallback_reason(self) -> None:
        stub = self._stub_fallback()
        lines: list = []
        runner = ModelRunner(self.config, logger=lines.append, fallback_for={"codex": stub})
        self.env["QS_FAKE_MODE"] = "timeout"
        result = runner.run("codex", self.workspace, "initial", 1, 3)
        self.assertTrue(result.success)
        self.assertIn("reason=timeout", " ".join(lines))


class PiAuthRefreshBoundTests(unittest.TestCase):
    """The codex credential refresh is bounded, and it stays non-fatal.

    ``prepare("codex")`` runs ``pi auth print-bearer-token`` before every codex
    attempt. That call used to have no timeout at all, so a wedged ``pi`` (a
    locked keyring, a hung network stack) blocked prepare() -- and with it every
    codex attempt, including a manual ``run``, which has no outer guard to be
    rescued by. The refresh now runs under the repo's ``run_with_timeout.py``
    with an inner deadline of ``auth_timeout``, plus a parent-side guard of
    ``auth_timeout + kill_grace``; both paths leave exactly one diagnosable line
    in the prepared stderr file and then continue to the auth copy.
    """

    AUTH_TIMEOUT = 1.0
    KILL_GRACE = 0.2

    # A refresh that never answers and will not be asked politely: SIGTERM is
    # ignored, so only the SIGKILL escalation can end it, and the child it
    # leaves behind can only be reached through the process group.
    HANGING_PI = """\
    import json, os, signal, subprocess, sys, time
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open(os.environ['QS_CAPTURE_PATH'], 'a') as out:
        out.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')
    grandchild = subprocess.Popen(
        [sys.executable, '-c',
         'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)'])
    with open(os.environ['QS_GRANDCHILD_PID'], 'w') as out:
        out.write(str(grandchild.pid))
    # The bearer token goes to stdout, which this call discards: it must never
    # turn up in the prepared stderr file.
    print('secret-bearer-token')
    sys.stdout.flush()
    while True:
        time.sleep(0.05)
    """

    FAST_PI = """\
    import json, os, sys
    with open(os.environ['QS_CAPTURE_PATH'], 'a') as out:
        out.write(json.dumps({'argv': sys.argv[1:],
                              'env': {key: os.environ.get(key) for key in (
                                  'PI_OFFLINE', 'PI_CODEX_QUOTA_FILE')}}) + '\\n')
    print('secret-bearer-token')
    sys.exit(0)
    """

    CHATTY_PI = """\
    import os, sys
    with open(os.environ['QS_CAPTURE_PATH'], 'a') as out:
        out.write('call\\n')
    print('pi: refreshed the bearer token', file=sys.stderr)
    sys.exit(0)
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="quota-sentinel-auth-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "work"
        self.auth = self.root / "auth.json"
        self.auth.write_text('{"openai-codex":{"token":"secret-auth"}}')
        self.auth.chmod(0o600)
        self.capture = self.root / "calls.jsonl"
        self.grandchild_pid = self.root / "grandchild.pid"

    def _fake_pi(self, template: str) -> Path:
        path = self.root / "pi"
        path.write_text("#!" + sys.executable + "\n" + textwrap.dedent(template))
        path.chmod(0o755)
        return path

    def _config(self, pi: Path) -> ModelRunnerConfig:
        return ModelRunnerConfig(
            pi_bin=pi,
            auth_file=self.auth,
            repo_dir=REPO,
            antigravity_provider_extension=self.root / "antigravity-provider.ts",
            timeout=5,
            kill_grace=self.KILL_GRACE,
            auth_timeout=self.AUTH_TIMEOUT,
            environment={
                "QS_CAPTURE_PATH": str(self.capture),
                "QS_GRANDCHILD_PID": str(self.grandchild_pid),
            },
        )

    def _runner(self, pi: Path) -> ModelRunner:
        return ModelRunner(self._config(pi))

    def _calls(self) -> list:
        if not self.capture.exists():
            return []
        return [json.loads(line) for line in self.capture.read_text().splitlines()]

    def _lines(self, paths: PreparedPaths) -> list:
        return paths.stderr_path.read_text().splitlines()

    def _survivors(self, pattern: str) -> list:
        """`pgrep -f`: everything still carrying the fixture's path."""
        listed = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
        return listed.stdout.split()

    def _wait_until_gone(self, pattern: str, timeout: float = 2.0) -> list:
        deadline = time.monotonic() + timeout
        while True:
            survivors = self._survivors(pattern)
            if not survivors or time.monotonic() >= deadline:
                return survivors
            time.sleep(0.05)

    def _assert_gone(self, pid: int) -> None:
        deadline = time.monotonic() + 2.0
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            if time.monotonic() >= deadline:
                self.fail("process %d survived the bounded refresh" % pid)
            time.sleep(0.05)

    def test_a_hanging_pi_is_bounded_and_leaves_one_diagnosable_line(self) -> None:
        """prepare() returns on its own deadline instead of blocking forever."""
        pi = self._fake_pi(self.HANGING_PI)
        started = time.monotonic()
        paths = self._runner(pi).prepare("codex", self.workspace)
        elapsed = time.monotonic() - started
        # The refresh really was waited for (its deadline cannot fire early) and
        # the call came back within the legal cost of one prepare(): the inner
        # deadline plus the guard, plus the guard's own tree reap -- one process
        # table read -- whenever the guard is the path that has to escalate.
        self.assertGreaterEqual(elapsed, self.AUTH_TIMEOUT)
        self.assertLess(elapsed, self.AUTH_TIMEOUT + self.KILL_GRACE + 1.0)
        # Exactly one line, never a traceback, and never the bearer token that
        # the fixture printed on the stdout this call discards.
        lines = self._lines(paths)
        self.assertEqual(len(lines), 1)
        self.assertIn("Pi auth refresh timed out after 1s", lines[0])
        self.assertIn("continuing without a fresh credential", lines[0])
        self.assertNotIn("secret-bearer-token", lines[0])
        # The refreshing command is unchanged...
        self.assertEqual(self._calls(), [
            {"argv": ["auth", "print-bearer-token", "--provider", "openai-codex"]},
        ])
        # ...and a cut-short refresh is not fatal: the credential already on
        # disk is copied and the provider settings are written as usual.
        self.assertEqual((paths.agent_dir / "auth.json").read_bytes(), self.auth.read_bytes())
        self.assertEqual((paths.agent_dir / "settings.json").read_text(), '{"transport":"sse"}\n')

    def test_the_bounded_refresh_leaves_no_orphan_behind(self) -> None:
        """A fixture that ignores SIGTERM can only end through the SIGKILL."""
        pi = self._fake_pi(self.HANGING_PI)
        paths = self._runner(pi).prepare("codex", self.workspace)
        self.assertEqual(len(self._lines(paths)), 1)
        # The fake pi is gone shortly after the call returns...
        self.assertEqual(self._wait_until_gone(str(pi)), [])
        # ...and so is the child it spawned: the group was reaped, not merely
        # the one process this call happened to know about.
        self._assert_gone(int(self.grandchild_pid.read_text()))

    def test_refresh_reaps_a_stubborn_child_when_pi_exits_on_sigterm(self) -> None:
        template = self.HANGING_PI.replace(
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))",
            1,
        )

        def cleanup_child():
            if self.grandchild_pid.exists():
                try:
                    os.kill(int(self.grandchild_pid.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

        self.addCleanup(cleanup_child)
        paths = self._runner(self._fake_pi(template)).prepare("codex", self.workspace)
        self.assertEqual(len(self._lines(paths)), 1)
        self.assertIn("timed out", self._lines(paths)[0])
        self._assert_gone(int(self.grandchild_pid.read_text()))

    def test_a_well_behaved_pi_still_refreshes_without_a_new_stderr_line(self) -> None:
        pi = self._fake_pi(self.FAST_PI)
        paths = self._runner(pi).prepare("codex", self.workspace)
        call = self._calls()[0]
        self.assertEqual(call["argv"], ["auth", "print-bearer-token", "--provider", "openai-codex"])
        # The bound did not move the refresh: it still runs in the base
        # environment, with no quota file and no offline flag of its own.
        self.assertIsNone(call["env"]["PI_OFFLINE"])
        self.assertIsNone(call["env"]["PI_CODEX_QUOTA_FILE"])
        self.assertEqual((paths.agent_dir / "auth.json").read_bytes(), self.auth.read_bytes())
        self.assertEqual((paths.agent_dir / "settings.json").read_text(), '{"transport":"sse"}\n')
        # Nothing about the refresh reaches the prepared stderr file, exactly
        # like a direct invocation that printed nothing.
        self.assertEqual(self._lines(paths), [])

    def test_a_child_diagnostic_still_reaches_the_prepared_stderr_file(self) -> None:
        """The bound must not swallow what ``pi`` itself prints to stderr."""
        paths = self._runner(self._fake_pi(self.CHATTY_PI)).prepare("codex", self.workspace)
        self.assertEqual(self._lines(paths), ["pi: refreshed the bearer token"])

    def test_a_pi_that_cannot_start_is_still_not_fatal(self) -> None:
        """A refresh that never ran leaves a diagnostic, not an exception."""
        paths = self._runner(self.root / "missing-pi").prepare("codex", self.workspace)
        lines = self._lines(paths)
        self.assertEqual(len(lines), 1)
        self.assertIn("missing-pi", lines[0])
        self.assertNotIn("Traceback", lines[0])
        self.assertEqual((paths.agent_dir / "auth.json").read_bytes(), self.auth.read_bytes())

    def test_auth_timeout_env_is_honoured_and_unusable_values_fall_back(self) -> None:
        import quota_sentinel.runtime.models as models

        self.assertEqual(PI_AUTH_TIMEOUT_SECONDS, 30)
        self.assertIn("PI_AUTH_TIMEOUT_SECONDS", models.__all__)
        # The dataclass default is the module constant...
        self.assertEqual(
            ModelRunnerConfig(
                pi_bin=self.root / "pi",
                auth_file=self.auth,
                repo_dir=REPO,
                antigravity_provider_extension=self.root / "antigravity-provider.ts",
            ).auth_timeout,
            PI_AUTH_TIMEOUT_SECONDS,
        )
        # ...every unusable spelling falls back to it, and never raises...
        for value in ("", "later", "0", "-1", "nan", "inf"):
            with self.subTest(value=value):
                self.assertEqual(
                    ModelRunnerConfig.from_env(
                        {"QUOTA_SENTINEL_PI_AUTH_TIMEOUT": value}
                    ).auth_timeout,
                    PI_AUTH_TIMEOUT_SECONDS,
                )
        # ...while a usable value is taken as given.
        self.assertEqual(
            ModelRunnerConfig.from_env({"QUOTA_SENTINEL_PI_AUTH_TIMEOUT": "2.5"}).auth_timeout,
            2.5,
        )
        # A junk value must not fail the import or the run either: a runner
        # built from that environment still prepares, and still refreshes.
        config = ModelRunnerConfig.from_env({
            "HOME": str(self.root),
            "QUOTA_SENTINEL_PI_BIN": str(self._fake_pi(self.FAST_PI)),
            "QUOTA_SENTINEL_PI_AUTH_FILE": str(self.auth),
            "QUOTA_SENTINEL_PI_AUTH_TIMEOUT": "whenever",
            "QUOTA_SENTINEL_MODEL_TIMEOUT": "5",
            "QUOTA_SENTINEL_MODEL_KILL_GRACE": str(self.KILL_GRACE),
            "QS_CAPTURE_PATH": str(self.capture),
        })
        self.assertEqual(config.auth_timeout, PI_AUTH_TIMEOUT_SECONDS)
        paths = ModelRunner(config).prepare("codex", self.workspace)
        self.assertEqual(len(self._calls()), 1)
        self.assertEqual((paths.agent_dir / "auth.json").read_bytes(), self.auth.read_bytes())
        self.assertEqual(self._lines(paths), [])


if __name__ == "__main__":
    unittest.main()
