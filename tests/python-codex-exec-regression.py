#!/usr/bin/env python3
"""The codex transport: the official CLI profile, its cost, and its fallback.

This suite never starts a real Codex and never spends a token. A fake `codex`
executable records the exact argv and environment it was given and replays one
scenario, so every claim the transport makes can be checked:

  * the minimal profile is byte-for-byte the one measured at ~1,687 tokens
    (a renamed feature flag or a dropped override must fail here, not in
    production at 5x the cost);
  * success is read from the parsed stream (final message, turn.completed and
    its usage, reasoning) rather than from the log format: a turn that never
    completed, or completed without reporting input tokens, is a functional
    failure even when its reply was exactly right;
  * a functional failure falls back to Pi; a cost regression does not — the
    reply was already delivered and the window anchored, so it is accepted as
    success, logged as a regression, and *not* recorded as a verified profile,
    which keeps the next attempt testing instead of trusting the regression;
  * the attempt environment carries the CLI's own CODEX_HOME and none of the
    API keys that would silently route a ChatGPT model to the platform API.

Run: PYTHONPATH=. uv run --frozen --no-sync python tests/python-codex-exec-regression.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from quota_sentinel.runtime.codex_exec import (
    CODEX_INPUT_CEILING,
    CODEX_OUTPUT_CEILING,
    PROFILE_DISABLED_FEATURES,
    PROFILE_OVERRIDES,
    SYSTEM_INSTRUCTIONS,
    USER_PROMPT,
    CodexExecConfig,
    CodexExecRunner,
    parse_events,
    profile_fingerprint,
)
from quota_sentinel.runtime.models import AttemptResult
from quota_sentinel.platform.locks import initialize_protocol

FAKE_CODEX = '''#!{python}
import json, os, sys, time

record = os.environ.get("QS_FAKE_CODEX_RECORD")
if record:
    with open(record, "a") as handle:
        handle.write(json.dumps({{
            "argv": sys.argv[1:],
            "codex_home": os.environ.get("CODEX_HOME"),
            "openai_api_key": os.environ.get("OPENAI_API_KEY"),
            "codex_api_key": os.environ.get("CODEX_API_KEY"),
            "cwd": os.getcwd(),
        }}) + "\\n")

if "--version" in sys.argv:
    print("codex-cli 9.9.9-fake")
    raise SystemExit(0)

mode = os.environ.get("QS_FAKE_CODEX_MODE", "success")
if mode == "timeout":
    time.sleep(30)
    raise SystemExit(0)
if mode == "exit1":
    print("boom", file=sys.stderr)
    raise SystemExit(3)

usage = {{
    "input_tokens": 3200 if mode == "regression-input" else 1682,
    "cached_input_tokens": 0,
    "output_tokens": 60 if mode == "regression-output" else 5,
    "reasoning_output_tokens": 7 if mode == "reasoning" else 0,
}}
reply = {{"success": "1", "reasoning": "1", "regression-input": "1",
          "regression-output": "1", "no-completion": "1"}}.get(
    mode, "Could you clarify what you'd like me to do?")
if mode == "no-completion":
    # A correct reply and no turn.completed: the stream a killed or truncated
    # CLI leaves behind. Only the completion event says the turn finished.
    print(json.dumps({{"type": "item.completed",
                       "item": {{"type": "agent_message", "text": reply}}}}))
    raise SystemExit(0)
if mode == "unverified-usage":
    # A finished turn whose usage omits the one number the ceilings are about.
    print(json.dumps({{"type": "item.completed",
                       "item": {{"type": "agent_message", "text": "1"}}}}))
    print(json.dumps({{"type": "turn.completed", "usage": {{"output_tokens": 5}}}}))
    raise SystemExit(0)
if mode == "zero-input-usage":
    # The same, with the field present but zero: a reported nothing, not a
    # measurement of nothing.
    print(json.dumps({{"type": "item.completed",
                       "item": {{"type": "agent_message", "text": "1"}}}}))
    print(json.dumps({{"type": "turn.completed",
                       "usage": {{"input_tokens": 0, "output_tokens": 5}}}}))
    raise SystemExit(0)
print(json.dumps({{"type": "thread.started", "thread_id": "t"}}))
print("not json at all")
print(json.dumps({{"type": "item.completed",
                   "item": {{"type": "agent_message", "text": "stale"}}}}))
print(json.dumps({{"type": "item.completed",
                   "item": {{"type": "agent_message", "text": reply}}}}))
print(json.dumps({{"type": "turn.completed", "usage": usage}}))
'''


class _FakePi:
    """Stands in for the Pi runner: records the delegation, returns its own result."""

    def __init__(self):
        self.prepared = []
        self.calls = []

    def prepare(self, provider, workspace):
        self.prepared.append((provider, Path(workspace)))

    def run(self, provider, workspace, phase, attempt, limit):
        self.calls.append((provider, phase, attempt, limit))
        return AttemptResult(
            success=True, exit_code=0, timed_out=False, elapsed=1.5,
            stdout_path=Path(workspace) / "pi-stdout",
            stderr_path=Path(workspace) / "pi-stderr",
            quota_path=Path(workspace) / "pi-quota.json",
            error_summary="",
        )


class _FailingPi:
    """A Pi runner that fails on purpose.

    Used to prove a delegation that must not happen: if the transport hands an
    already-delivered attempt to Pi, a failing stub turns the whole run into a
    reported failure, so the bug cannot hide behind a successful fallback.
    """

    def __init__(self):
        self.prepared = []
        self.calls = []

    def prepare(self, provider, workspace):
        self.prepared.append((provider, Path(workspace)))

    def run(self, provider, workspace, phase, attempt, limit):
        self.calls.append((provider, phase, attempt, limit))
        return AttemptResult(
            success=False, exit_code=1, timed_out=False, elapsed=0.5,
            stdout_path=Path(workspace) / "pi-stdout",
            stderr_path=Path(workspace) / "pi-stderr",
            quota_path=Path(workspace) / "pi-quota.json",
            error_summary="pi-failed-on-purpose",
        )


class CodexExecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="quota-sentinel-codex-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "work"
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        initialize_protocol(self.state_dir)
        self.record = self.root / "calls.jsonl"
        self.codex = self.root / "codex"
        self.codex.write_text(FAKE_CODEX.format(python=sys.executable))
        self.codex.chmod(0o755)
        self.lines: list = []
        self.pi = _FakePi()

    def runner(self, *, mode="success", fallback=None, timeout=30, environment=None) -> CodexExecRunner:
        env = {"QS_FAKE_CODEX_RECORD": str(self.record), "QS_FAKE_CODEX_MODE": mode}
        env.update(environment or {})
        config = CodexExecConfig(
            codex_bin=self.codex,
            codex_home=self.root / "codex-home",
            state_dir=self.state_dir,
            timeout=timeout,
            kill_grace=1,
            environment=env,
        )
        return CodexExecRunner(
            config, logger=self.lines.append, fallback=fallback
        )

    def calls(self) -> list:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines() if line.strip()]

    def exec_calls(self) -> list:
        """Only the `codex exec` invocation: the version probe is recorded too."""
        return [call for call in self.calls() if call["argv"][:1] == ["exec"]]

    def smoke_record(self) -> dict:
        path = self.state_dir / "codex-exec-profile.json"
        return json.loads(path.read_text()) if path.exists() else {}

    # ------------------------------------------------------------- the profile
    def test_argv_is_exactly_the_measured_minimal_profile(self):
        self.runner().run("codex", self.workspace, "initial", 1, 3)
        argv = self.exec_calls()[0]["argv"]
        self.assertEqual(argv[0], "exec")
        for flag in ("--json", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check"):
            self.assertIn(flag, argv)
        overrides = [argv[i + 1] for i, item in enumerate(argv) if item == "-c"]
        for override in PROFILE_OVERRIDES:
            self.assertIn(override, overrides)
        disabled = [argv[i + 1] for i, item in enumerate(argv) if item == "--disable"]
        self.assertEqual(disabled, list(PROFILE_DISABLED_FEATURES))
        instructions = [o for o in overrides if o.startswith("model_instructions_file=")]
        self.assertEqual(len(instructions), 1)
        # The prompt is the last argument: an override must never displace it.
        self.assertEqual(argv[-1], USER_PROMPT)
        # Only the declared profile may appear: an extra -c is a silent cost.
        self.assertEqual(len(overrides), len(PROFILE_OVERRIDES) + 1)

    def test_profile_fingerprint_tracks_the_lists(self):
        first = profile_fingerprint()
        self.assertEqual(first, profile_fingerprint())
        self.assertEqual(len(first), 16)

    def test_environment_uses_the_cli_home_and_drops_api_keys(self):
        runner = self.runner(environment={"OPENAI_API_KEY": "sk-should-vanish",
                                          "CODEX_API_KEY": "also-vanish"})
        runner.run("codex", self.workspace, "initial", 1, 3)
        call = self.exec_calls()[0]
        self.assertEqual(call["codex_home"], str(self.root / "codex-home"))
        self.assertIsNone(call["openai_api_key"])
        self.assertIsNone(call["codex_api_key"])
        # An empty cwd: no AGENTS.md, no repository, no project config.
        self.assertTrue(call["cwd"].endswith("codex-codex/cwd"))

    def test_prepare_writes_private_instructions_and_an_empty_cwd(self):
        paths = self.runner().prepare("codex", self.workspace)
        self.assertEqual((self.workspace).stat().st_mode & 0o777, 0o700)
        self.assertEqual(paths.agent_dir.stat().st_mode & 0o777, 0o700)
        instructions = self.workspace / "codex-instructions.txt"
        self.assertEqual(instructions.read_text(), SYSTEM_INSTRUCTIONS + "\n")
        self.assertEqual(instructions.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list((paths.agent_dir / "cwd").iterdir()), [])

    # -------------------------------------------------------------- the verdict
    def test_success_reads_the_parsed_stream_and_records_usage(self):
        result = self.runner().run("codex", self.workspace, "initial", 1, 3)
        self.assertTrue(result.success)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("result=success", " ".join(self.lines))
        self.assertIn("input=1682", " ".join(self.lines))
        self.assertIn("output=5", " ".join(self.lines))
        record = self.smoke_record()
        self.assertEqual(record["version"], "codex-cli 9.9.9-fake")
        self.assertEqual(record["fingerprint"], profile_fingerprint())
        self.assertEqual(record["usage"]["input"], 1682)

    def test_stale_then_final_message_uses_the_last_one(self):
        # The fake prints a stale agent_message before the real reply.
        final, usage, events, completed = parse_events(
            '{"type":"item.completed","item":{"type":"agent_message","text":"stale"}}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"1"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":2}}\n'
        )
        self.assertEqual(final, "1")
        self.assertEqual(usage, {"input_tokens": 1, "output_tokens": 2})
        self.assertEqual(events, 3)
        self.assertTrue(completed)
        self.assertTrue(self.runner().run("codex", self.workspace, "initial", 1, 3).success)

    def test_parser_reports_completion_separately_from_usage(self):
        # The two facts are independent, and the verdict needs both: a turn can
        # finish without reporting tokens, and a stream can report tokens
        # (from a thread event, say) without ever finishing.
        final, usage, events, completed = parse_events(
            '{"type":"item.completed","item":{"type":"agent_message","text":"1"}}\n'
            '{"type":"turn.completed"}\n'
        )
        self.assertEqual(final, "1")
        self.assertIsNone(usage)
        self.assertEqual(events, 2)
        self.assertTrue(completed)
        final, usage, events, completed = parse_events(
            '{"type":"item.completed","item":{"type":"agent_message","text":"1"}}\n'
        )
        self.assertEqual(final, "1")
        self.assertIsNone(usage)
        self.assertFalse(completed)

    def test_wrong_reply_falls_back_to_pi(self):
        result = self.runner(mode="wrong-reply", fallback=self.pi).run(
            "codex", self.workspace, "initial", 1, 3
        )
        self.assertEqual(self.pi.calls, [("codex", "initial", 1, 3)])
        self.assertTrue(self.pi.prepared)
        self.assertTrue(result.success)  # the Pi attempt's own verdict
        self.assertIn("fallback=pi", " ".join(self.lines))
        self.assertIn("functional:reply=", " ".join(self.lines))

    def test_reasoning_tokens_are_a_functional_failure(self):
        self.runner(mode="reasoning", fallback=self.pi).run("codex", self.workspace, "initial", 1, 3)
        self.assertIn("functional:reasoning=7", " ".join(self.lines))

    def test_nonzero_exit_falls_back(self):
        self.runner(mode="exit1", fallback=self.pi).run("codex", self.workspace, "initial", 1, 3)
        self.assertIn("functional:exit=3", " ".join(self.lines))

    def test_missing_turn_completed_is_a_functional_failure(self):
        # The reply is exactly `1`, so the wrong-reply rule cannot be what fails
        # this attempt: the missing completion event alone has to. A stream that
        # ends after an agent message is a killed, truncated or reworded CLI,
        # and judging it a success would certify a turn nobody saw finish.
        result = self.runner(mode="no-completion", fallback=self.pi).run(
            "codex", self.workspace, "initial", 1, 3
        )
        joined = " ".join(self.lines)
        self.assertIn("functional:no turn.completed", joined)
        self.assertNotIn("reply=", joined)  # the reply itself was right
        self.assertIn("input=0", joined)  # no usage was reported at all
        self.assertIn("fallback=pi", joined)
        self.assertTrue(result.success)  # the Pi attempt's own verdict
        # An unverified turn must not be written down as a verified profile.
        self.assertEqual(self.smoke_record(), {})

    def test_completion_without_input_usage_is_usage_unverified(self):
        # `input_tokens` is the number CODEX_INPUT_CEILING is stated against.
        # A completion that reports none — absent, or present and zero — cannot
        # be checked against it, so it is a functional failure, not a free run.
        for mode in ("unverified-usage", "zero-input-usage"):
            self.lines.clear()
            before = len(self.pi.calls)
            result = self.runner(mode=mode, fallback=self.pi).run(
                "codex", self.workspace, "initial", 1, 3
            )
            joined = " ".join(self.lines)
            self.assertIn("functional:usage=unverified", joined, mode)
            self.assertNotIn("reply=", joined, mode)  # the reply itself was right
            self.assertIn("input=0", joined, mode)
            self.assertIn("fallback=pi", joined, mode)
            self.assertTrue(result.success, mode)  # the Pi attempt's own verdict
            self.assertEqual(len(self.pi.calls), before + 1, mode)
        self.assertEqual(self.smoke_record(), {})

    def test_functional_failures_still_hand_the_attempt_to_pi(self):
        # The cost-regression change must not touch the functional path: these
        # attempts verified nothing as delivered, so Pi still owns the attempt.
        for mode in ("wrong-reply", "no-completion", "unverified-usage",
                     "reasoning", "exit1"):
            self.lines.clear()
            before = len(self.pi.calls)
            self.runner(mode=mode, fallback=self.pi).run(
                "codex", self.workspace, "initial", 1, 3
            )
            joined = " ".join(self.lines)
            self.assertIn("functional:", joined, mode)
            self.assertIn("fallback=pi", joined, mode)
            self.assertEqual(len(self.pi.calls), before + 1, mode)

    def test_timeout_falls_back(self):
        self.runner(mode="timeout", fallback=self.pi, timeout=1).run(
            "codex", self.workspace, "initial", 1, 3
        )
        self.assertIn("functional:timeout", " ".join(self.lines))

    # ----------------------------------------------------------- cost regressions
    def test_input_cost_regression_warns_and_does_not_record_the_profile(self):
        result = self.runner(mode="regression-input", fallback=self.pi).run(
            "codex", self.workspace, "initial", 1, 3
        )
        joined = " ".join(self.lines)
        self.assertIn("result=cost-regression", joined)
        self.assertIn("cost regression", joined)
        self.assertIn("input=%s>=%s" % (3200, CODEX_INPUT_CEILING), joined)
        # Accepted, not re-delivered: the reply was already returned and the
        # window already anchored, so a Pi run would spend a second window on a
        # turn that already succeeded.
        self.assertNotIn("fallback=pi", joined)
        self.assertEqual(self.pi.calls, [])
        self.assertTrue(result.success)
        self.assertEqual(result.error_summary, "")
        # A regression must never be written down as a verified profile: the
        # next attempt has to test again.
        self.assertEqual(self.smoke_record(), {})

    def test_output_cost_regression_warns(self):
        result = self.runner(mode="regression-output", fallback=self.pi).run(
            "codex", self.workspace, "initial", 1, 3
        )
        joined = " ".join(self.lines)
        self.assertIn("output=%s>=%s" % (60, CODEX_OUTPUT_CEILING), joined)
        # The output ceiling is an alarm about the profile too, not a second
        # delivery, and not a verified-profile record.
        self.assertIn("result=cost-regression", joined)
        self.assertNotIn("fallback=pi", joined)
        self.assertEqual(self.pi.calls, [])
        self.assertTrue(result.success)
        self.assertEqual(self.smoke_record(), {})

    def test_cost_regression_is_not_re_delivered_even_when_pi_would_fail(self):
        # The regression defect in one assertion: the codex turn replied `1` and
        # exited 0, so handing the same attempt to a Pi runner that fails would
        # report the whole attempt as a failure — and the scheduler would then
        # retry the provider, spending a third window on work already delivered.
        failing = _FailingPi()
        result = self.runner(mode="regression-input", fallback=failing).run(
            "codex", self.workspace, "initial", 1, 3
        )
        self.assertTrue(result.success)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.error_summary, "")
        self.assertEqual(failing.calls, [])
        self.assertEqual(failing.prepared, [])
        joined = " ".join(self.lines)
        self.assertIn("result=cost-regression", joined)
        self.assertIn("input=3200", joined)  # the accepted turn's own numbers
        self.assertNotIn("fallback=pi", joined)
        self.assertEqual(self.smoke_record(), {})

    def test_success_still_records_below_the_ceilings(self):
        self.runner().run("codex", self.workspace, "initial", 1, 3)
        self.assertTrue(self.smoke_record())

    # ------------------------------------------------------------ smoke on change
    def test_version_or_profile_change_is_announced(self):
        path = self.state_dir / "codex-exec-profile.json"
        path.write_text(json.dumps({"version": "codex-cli 0.0.1-old",
                                    "fingerprint": "deadbeefdeadbeef"}))
        self.runner().run("codex", self.workspace, "initial", 1, 3)
        joined = " ".join(self.lines)
        self.assertIn("profile changed", joined)
        self.assertIn("0.0.1-old", joined)
        self.assertIn("deadbeefdeadbeef", joined)
        self.assertEqual(self.smoke_record()["version"], "codex-cli 9.9.9-fake")

    def test_unchanged_profile_is_not_announced(self):
        self.runner().run("codex", self.workspace, "initial", 1, 3)
        self.lines.clear()
        self.runner().run("codex", self.workspace, "initial", 1, 3)
        self.assertNotIn("profile changed", " ".join(self.lines))

    # ------------------------------------------------------------------ failures
    def test_without_a_fallback_the_attempt_fails_with_the_reason(self):
        result = self.runner(mode="wrong-reply").run("codex", self.workspace, "initial", 1, 3)
        self.assertFalse(result.success)
        self.assertIn("functional:reply=", result.error_summary)

    def test_other_providers_are_refused(self):
        runner = self.runner()
        with self.assertRaises(ValueError):
            runner.prepare("antigravity", self.workspace)
        with self.assertRaises(ValueError):
            runner.run("antigravity", self.workspace, "initial", 1, 3)

    def test_events_parser_ignores_noise(self):
        final, usage, events, completed = parse_events(
            "garbage\n\n[1,2]\n{\"type\":\"turn.started\"}\n"
        )
        self.assertIsNone(final)
        self.assertIsNone(usage)
        self.assertEqual(events, 1)
        self.assertFalse(completed)


class TransportPriorityTests(unittest.TestCase):
    """Pi is the shipped path; the official CLI is its one-hop fallback.

    The priority is a deployment decision, so it is pinned where it is
    composed rather than only where it is declared: a future edit that let the
    two runners call each other would be an infinite loop, and one that
    silently promoted the expensive transport is exactly the change this test
    exists to refuse.
    """

    def setUp(self) -> None:
        from quota_sentinel.state import bootstrap_legacy_authority

        self.tmp = tempfile.TemporaryDirectory(prefix="quota-sentinel-priority-")
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir(mode=0o700)
        initialize_protocol(self.state)
        bootstrap_legacy_authority(self.state)
        self.env = {
            "FEISHU_DRY_RUN": "1",
            "FEISHU_APP_ID": "cli-app-id",
            "FEISHU_APP_SECRET": "cli-app-secret",
            "FEISHU_USER_ID": "cli-user",
            "HOME": str(Path.home()),
        }

    def router(self, **extra):
        from quota_sentinel.runtime.factory import create_application

        env = dict(self.env)
        env.update(extra)
        return create_application(self.state, environment=env).model_runner

    def test_default_keeps_pi_primary_with_codex_as_its_fallback(self):
        router = self.router()
        self.assertEqual(router.transport("codex"), "pi")
        pi_runner = router.runner_for("codex")
        fallback = pi_runner.fallback_for["codex"]
        self.assertEqual(fallback.TRANSPORT, "codex")
        # One hop, then stop: the fallback has no fallback of its own.
        self.assertIsNone(fallback.fallback)

    def test_selecting_codex_upgrades_the_path_without_looping(self):
        router = self.router(QUOTA_SENTINEL_TRANSPORT="codex=codex")
        self.assertEqual(router.transport("codex"), "codex")
        codex_runner = router.runner_for("codex")
        self.assertEqual(codex_runner.fallback.TRANSPORT, "pi")
        self.assertEqual(codex_runner.fallback.fallback_for, {})

    def test_other_providers_keep_their_transports(self):
        router = self.router()
        # Antigravity's priority runs the other way (agy primary, Pi fallback);
        # it is pinned in tests/python-agy-exec-regression.py.
        self.assertEqual(router.transport("antigravity"), "agy")
        self.assertEqual(router.transport("opencode"), "direct")
        self.assertEqual(router.transport("clinepass"), "direct")


if __name__ == "__main__":
    unittest.main(verbosity=2)
