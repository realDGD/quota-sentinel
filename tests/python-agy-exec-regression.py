#!/usr/bin/env python3
"""The agy transport: the minimal agent, its cost, its free guards, its fallback.

This suite never starts a real Antigravity CLI and never spends a token. A fake
`agy` executable records the exact argv, cwd and environment it was given and
replays one scenario, so every claim the transport makes can be checked:

  * the profile is byte-for-byte the agent document that measured 564 input
    tokens against the stock agent's 22,311 (an edited frontmatter key or a
    dropped --agent must fail here, not in production at 40x the cost);
  * the agent is confirmed with the CLI's own `/agents` command BEFORE the turn,
    because an unresolvable agent is not an error at all — the default agent
    answers, at 40x;
  * the pre-turn `Eligibility check failed` handshake is retried and costs
    nothing, and a retry must not be mistaken for a malformed reply;
  * success is read from the parsed JSON (status, response, usage) rather than
    from the log format;
  * a functional failure falls back to Pi, and so does a cost regression — the
    second one without recording the profile as verified, so the next attempt
    re-tests instead of trusting a regression;
  * the attempt environment carries the subscription credential only: the
    Gemini API key, which bills a different product and would not anchor the
    same window, is removed.

Run: PYTHONPATH=. uv run --frozen --no-sync python tests/python-agy-exec-regression.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from quota_sentinel.runtime import agy_exec
from quota_sentinel.runtime.agy_exec import (
    AGENT_NAME,
    AGY_EFFORT,
    AGY_INPUT_CEILING,
    AGY_MODEL,
    AGY_OUTPUT_CEILING,
    SYSTEM_PROMPT_BODY,
    USER_PROMPT,
    AgyExecConfig,
    AgyExecRunner,
    agent_document,
    is_transient_error,
    parse_result,
    profile_fingerprint,
)
from quota_sentinel.runtime.models import AttemptResult

TRANSIENT_BODY = {
    "conversation_id": "",
    "status": "ERROR",
    "response": "",
    "error": "Eligibility check failed: Get \"https://www.googleapis.com/oauth2/v2/userinfo\": EOF",
    "duration_seconds": 0,
    "num_turns": 0,
    "usage": {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
              "cache_read_tokens": 0, "total_tokens": 0},
}

FAKE_AGY = '''#!{python}
import json, os, sys, time

record = os.environ.get("QS_FAKE_AGY_RECORD")
argv = sys.argv[1:]
mode = os.environ.get("QS_FAKE_AGY_MODE", "success")


def note(kind):
    if not record:
        return
    with open(record, "a") as handle:
        handle.write(json.dumps({{
            "kind": kind,
            "argv": argv,
            "cwd": os.getcwd(),
            "gemini_api_key": os.environ.get("GEMINI_API_KEY"),
        }}) + "\\n")


def turns():
    if not record or not os.path.exists(record):
        return 0
    with open(record) as handle:
        return handle.read().count('"kind": "turn"')


if "--version" in argv:
    print("1.2.12-fake")
    raise SystemExit(0)

if "/agents" in argv:
    note("agents")
    if mode == "agents-unavailable":
        print("the backend is unreachable", file=sys.stderr)
        print("not json at all")
        raise SystemExit(0)
    agents = [] if mode == "missing-agent" else ["quota-primer", "another-agent"]
    print(json.dumps({{
        "conversation_id": "", "status": "SUCCESS", "response": "",
        "num_turns": 0,
        "usage": {{"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
                   "cache_read_tokens": 0, "total_tokens": 0}},
        "command": {{"name": "agents", "data": {{"agents": agents}}}},
    }}))
    raise SystemExit(0)

note("turn")

if mode == "exit3":
    print("boom", file=sys.stderr)
    raise SystemExit(3)

if mode == "timeout":
    time.sleep(30)
    raise SystemExit(0)

if mode in ("transient-once", "transient-always") and (mode == "transient-always" or turns() <= 1):
    print(json.dumps({transient}))
    raise SystemExit(1)

input_tokens = 22311 if mode == "stock-cost" else 564
thinking = 30 if mode == "thinking" else 0
output = 30 if mode == "thinking" else 1
reply = {{"success": "1", "stock-cost": "1", "thinking": "1",
          "transient-once": "1", "missing-agent": "1",
          "agents-unavailable": "1"}}.get(mode, "Sure, what would you like me to do?")
print(json.dumps({{
    "conversation_id": "c-1", "status": "SUCCESS", "response": reply,
    "duration_seconds": 3, "num_turns": 1,
    "usage": {{"input_tokens": input_tokens, "output_tokens": output,
               "thinking_tokens": thinking, "cache_read_tokens": 0,
               "total_tokens": input_tokens + output}},
}}))
'''.format(python=sys.executable, transient=json.dumps(TRANSIENT_BODY))


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


class AgyExecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="quota-sentinel-agy-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "work"
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.record = self.root / "calls.jsonl"
        self.agy = self.root / "agy"
        self.agy.write_text(FAKE_AGY)
        self.agy.chmod(0o755)
        self.lines: list = []
        self.pi = _FakePi()

    def runner(self, *, mode="success", fallback=None, timeout=30, environment=None,
               preflight=True) -> AgyExecRunner:
        env = {"QS_FAKE_AGY_RECORD": str(self.record), "QS_FAKE_AGY_MODE": mode}
        env.update(environment or {})
        config = AgyExecConfig(
            agy_bin=self.agy,
            state_dir=self.state_dir,
            timeout=timeout,
            kill_grace=1,
            transient_retries=3,
            preflight=preflight,
            environment=env,
        )
        return AgyExecRunner(config, logger=self.lines.append, fallback=fallback)

    def calls(self, kind=None) -> list:
        if not self.record.exists():
            return []
        records = [json.loads(line) for line in self.record.read_text().splitlines() if line.strip()]
        return [item for item in records if kind is None or item["kind"] == kind]

    def log(self) -> str:
        return " ".join(self.lines)

    def smoke_record(self):
        path = self.state_dir / "agy-exec-profile.json"
        return json.loads(path.read_text()) if path.exists() else None

    def agent_file(self) -> Path:
        return (
            self.workspace / "antigravity-agy" / "cwd" / ".agents" / "agents"
            / AGENT_NAME / "agent.md"
        )

    # ------------------------------------------------------------------ profile
    def test_profile_is_the_measured_minimal_agent(self):
        document = agent_document()
        for line in ("excludeDefaultComponents: true",
                     "inheritCustomizations: false",
                     "tools: []",
                     "name: %s" % AGENT_NAME):
            self.assertIn(line, document)
        self.assertIn(SYSTEM_PROMPT_BODY, document)

    def test_prepare_writes_the_agent_into_an_empty_cwd(self):
        paths = self.runner().prepare("antigravity", self.workspace)
        agent = self.agent_file()
        self.assertTrue(agent.is_file())
        self.assertEqual(agent.read_text(), agent_document())
        self.assertEqual(oct(agent.stat().st_mode & 0o777), "0o600")
        # The cwd holds nothing but the agent: no rules, skills or repo can be
        # discovered beside it.
        cwd = Path(paths.agent_dir) / "cwd"
        self.assertEqual(sorted(p.name for p in cwd.iterdir()), [".agents"])
        self.assertFalse(paths.quota_path.exists())

    def test_command_is_the_measured_profile(self):
        runner = self.runner()
        runner.run("antigravity", self.workspace, "initial", 1, 3)
        turn = self.calls("turn")[0]
        argv = turn["argv"]
        self.assertEqual(argv[:2], ["--agent", AGENT_NAME])
        self.assertEqual(argv[2:4], ["--model", AGY_MODEL])
        self.assertEqual(argv[4:6], ["--effort", AGY_EFFORT])
        self.assertEqual(argv[6], "--print-timeout")
        # A bare number is a hard usage error in agy; the unit is required.
        self.assertTrue(argv[7].endswith("s"))
        self.assertEqual(argv[8:10], ["-p", USER_PROMPT])
        self.assertEqual(argv[10:12], ["--output-format", "json"])

    def test_turn_runs_in_the_private_cwd_and_never_in_the_repo(self):
        self.runner().run("antigravity", self.workspace, "initial", 1, 3)
        turn = self.calls("turn")[0]
        self.assertEqual(Path(turn["cwd"]).resolve(),
                         (self.workspace / "antigravity-agy" / "cwd").resolve())

    def test_gemini_api_key_is_removed_from_the_attempt_environment(self):
        runner = self.runner(environment={"GEMINI_API_KEY": "sk-should-not-travel"})
        runner.run("antigravity", self.workspace, "initial", 1, 3)
        for call in self.calls():
            self.assertIsNone(call["gemini_api_key"])

    # ------------------------------------------------------------------ success
    def test_success_reads_usage_and_records_the_smoke_profile(self):
        result = self.runner().run("antigravity", self.workspace, "initial", 1, 3)
        self.assertTrue(result.success)
        self.assertEqual(result.error_summary, "")
        self.assertIn("input=564", self.log())
        self.assertIn("thinking=0", self.log())
        smoke = self.smoke_record()
        self.assertEqual(smoke["version"], "1.2.12-fake")
        self.assertEqual(smoke["fingerprint"], profile_fingerprint())
        self.assertEqual(smoke["usage"]["input_tokens"], 564)

    def test_a_successful_ignition_is_one_greppable_line(self):
        # The operator's question is "did the window get anchored cheaply, and
        # by which profile" — answerable from one line without arithmetic.
        AGY_STOCK = agy_exec.AGY_STOCK_INPUT_TOKENS
        self.runner().run("antigravity", self.workspace, "initial", 1, 3)
        ignition = [line for line in self.lines if "ignition" in line]
        self.assertEqual(len(ignition), 1)
        line = ignition[0]
        self.assertIn("model antigravity transport=agy ignition", line)
        self.assertIn("input=564", line)
        self.assertIn("total=565", line)
        self.assertIn("baseline=%s" % AGY_STOCK, line)
        self.assertIn("saved=%.1f%%" % (100.0 * (AGY_STOCK - 564) / AGY_STOCK), line)
        self.assertIn("profile=%s" % profile_fingerprint(), line)
        self.assertIn("version=1.2.12-fake", line)

    def test_the_preflight_result_is_logged_when_it_passes(self):
        # "The guard ran and passed" has to be distinguishable from "the guard
        # never ran": the refusal path used to be the only one that spoke.
        self.runner().run("antigravity", self.workspace, "initial", 1, 3)
        self.assertIn("preflight agent=%s listed=yes" % AGENT_NAME, self.log())

    def test_the_preflight_result_is_logged_when_it_is_unknown(self):
        self.runner(mode="agents-unavailable").run(
            "antigravity", self.workspace, "initial", 1, 3
        )
        self.assertIn("preflight agent=%s listed=unknown" % AGENT_NAME, self.log())

    def test_the_preflight_result_is_logged_when_it_is_skipped(self):
        self.runner(preflight=False).run("antigravity", self.workspace, "initial", 1, 3)
        self.assertIn("preflight agent=%s listed=skipped" % AGENT_NAME, self.log())

    def test_version_or_profile_change_is_announced(self):
        path = self.state_dir / "agy-exec-profile.json"
        path.write_text(json.dumps({"version": "1.2.0-old", "fingerprint": "deadbeefdeadbeef"}))
        self.runner().run("antigravity", self.workspace, "initial", 1, 3)
        self.assertIn("profile changed", self.log())
        self.assertIn("1.2.0-old", self.log())
        self.assertEqual(self.smoke_record()["version"], "1.2.12-fake")

    def test_unchanged_profile_is_not_announced(self):
        self.runner().run("antigravity", self.workspace, "initial", 1, 3)
        self.lines.clear()
        self.runner().run("antigravity", self.workspace, "initial", 1, 3)
        self.assertNotIn("profile changed", self.log())

    def test_fingerprint_follows_the_profile(self):
        before = profile_fingerprint()
        original = agy_exec.AGENT_NAME
        try:
            agy_exec.AGENT_NAME = "quota-primer-renamed"
            self.assertNotEqual(profile_fingerprint(), before)
        finally:
            agy_exec.AGENT_NAME = original
        self.assertEqual(profile_fingerprint(), before)

    # ------------------------------------------------------------- free preflight
    def test_missing_agent_refuses_before_the_turn(self):
        result = self.runner(mode="missing-agent", fallback=self.pi).run(
            "antigravity", self.workspace, "initial", 1, 3
        )
        # No turn was ever started: the whole point of the guard.
        self.assertEqual(self.calls("turn"), [])
        self.assertEqual(self.calls("agents")[0]["argv"][:2], ["-p", "/agents"])
        self.assertIn("agent-missing", self.log())
        self.assertEqual(self.pi.calls, [("antigravity", "initial", 1, 3)])
        self.assertTrue(result.success)  # delivered by the fallback

    def test_missing_agent_without_a_fallback_fails_with_the_reason(self):
        result = self.runner(mode="missing-agent").run(
            "antigravity", self.workspace, "initial", 1, 3
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error_summary, "functional:agent-missing")
        self.assertEqual(self.calls("turn"), [])

    def test_an_unreadable_agent_list_is_not_evidence_of_a_missing_agent(self):
        # A network blip must not turn into a permanent fallback.
        runner = self.runner(mode="agents-unavailable")
        paths = runner.prepare("antigravity", self.workspace)
        self.assertIsNone(runner.agent_listed(paths))
        result = runner.run("antigravity", self.workspace, "initial", 1, 3)
        self.assertTrue(result.success)

    def test_preflight_can_be_switched_off(self):
        runner = self.runner(mode="missing-agent", preflight=False)
        result = runner.run("antigravity", self.workspace, "initial", 1, 3)
        self.assertTrue(result.success)
        self.assertEqual(self.calls("agents"), [])
        self.assertEqual(len(self.calls("turn")), 1)

    # ---------------------------------------------------------- transient retry
    def test_transient_handshake_is_retried_without_replaying_the_turn(self):
        result = self.runner(mode="transient-once").run(
            "antigravity", self.workspace, "initial", 1, 3
        )
        self.assertTrue(result.success)
        self.assertEqual(len(self.calls("turn")), 2)
        self.assertIn("transient handshake failure; retry 1/3", self.log())
        # The retry's stdout must be the success document, not two objects.
        self.assertIn("input=564", self.log())

    def test_exhausted_transient_failures_are_reported(self):
        result = self.runner(mode="transient-always").run(
            "antigravity", self.workspace, "initial", 1, 3
        )
        self.assertFalse(result.success)
        self.assertEqual(len(self.calls("turn")), 4)  # one try plus three retries
        self.assertIn("status=ERROR", result.error_summary)

    def test_transient_markers_are_narrow(self):
        self.assertTrue(is_transient_error("Eligibility check failed: EOF"))
        self.assertTrue(is_transient_error(None, "Service Unavailable"))
        self.assertFalse(is_transient_error("reply='maybe'", "some other failure"))

    # ------------------------------------------------------------------ cost
    def test_stock_agent_cost_is_a_regression_not_a_success(self):
        result = self.runner(mode="stock-cost", fallback=self.pi).run(
            "antigravity", self.workspace, "initial", 1, 3
        )
        self.assertIn("cost-regression:input=22311>=%s" % AGY_INPUT_CEILING, self.log())
        self.assertIn("input=22311", self.log())
        self.assertEqual(self.pi.calls, [("antigravity", "initial", 1, 3)])
        self.assertTrue(result.success)
        # A regression must NOT be recorded as a verified profile.
        self.assertIsNone(self.smoke_record())

    def test_cost_regression_without_a_fallback_still_fails_the_attempt(self):
        result = self.runner(mode="stock-cost").run(
            "antigravity", self.workspace, "initial", 1, 3
        )
        self.assertFalse(result.success)
        self.assertTrue(result.error_summary.startswith("cost-regression:"))
        self.assertIsNone(self.smoke_record())

    def test_thinking_tokens_are_reported_but_not_policed(self):
        # Identical invocations of this profile measured 0 AND 34 thinking
        # tokens: at `--effort low` the model decides. Failing on that would
        # hand a fifth of all attempts to the fallback for nothing.
        result = self.runner(mode="thinking").run(
            "antigravity", self.workspace, "initial", 1, 3
        )
        self.assertIn("thinking=30", self.log())
        self.assertFalse(any("thinking" in line for line in [result.error_summary]))
        self.assertTrue(result.success)

    def test_a_runaway_reply_is_a_cost_regression(self):
        runner = self.runner()
        runner.config.output_ceiling = 1
        result = runner.run("antigravity", self.workspace, "initial", 1, 3)
        self.assertFalse(result.success)
        self.assertIn("output=", result.error_summary)
        self.assertIsNone(self.smoke_record())

    # ------------------------------------------------------------------ failures
    def test_wrong_reply_falls_back_to_pi(self):
        result = self.runner(mode="wrong-reply", fallback=self.pi).run(
            "antigravity", self.workspace, "initial", 1, 3
        )
        self.assertIn("functional:reply=", self.log())
        self.assertEqual(self.pi.prepared, [("antigravity", self.workspace)])
        self.assertTrue(result.success)

    def test_nonzero_exit_is_a_functional_failure(self):
        result = self.runner(mode="exit3").run(
            "antigravity", self.workspace, "initial", 1, 3
        )
        self.assertFalse(result.success)
        self.assertIn("exit=3", result.error_summary)

    def test_timeout_is_reported_as_a_timeout(self):
        result = self.runner(mode="timeout", timeout=1).run(
            "antigravity", self.workspace, "initial", 1, 3
        )
        self.assertFalse(result.success)
        self.assertTrue(result.timed_out)
        self.assertIn("timeout", result.error_summary)

    def test_other_providers_are_refused(self):
        runner = self.runner()
        with self.assertRaises(ValueError):
            runner.prepare("codex", self.workspace)
        with self.assertRaises(ValueError):
            runner.run("codex", self.workspace, "initial", 1, 3)

    def test_parser_ignores_everything_that_is_not_the_result_object(self):
        self.assertEqual(parse_result("")[3], ["no output"])
        self.assertEqual(parse_result("not json")[3], ["stdout is not JSON"])
        self.assertEqual(parse_result("[1, 2]")[3], ["stdout is not a JSON object"])
        status, response, usage, problems = parse_result(
            json.dumps({"status": "SUCCESS", "response": "1",
                        "usage": {"input_tokens": 564}})
        )
        self.assertEqual((status, response, usage["input_tokens"], problems),
                         ("SUCCESS", "1", 564, []))


class TransportPriorityTests(unittest.TestCase):
    """Antigravity ships agy-first with Pi as its one-hop fallback.

    The priority is a deployment decision, so it is pinned where it is
    composed rather than only where it is declared: a future edit that let the
    two runners call each other would be an infinite loop, and one that
    silently demoted the cheap transport is exactly the change this test
    exists to refuse.
    """

    def setUp(self) -> None:
        from quota_sentinel.state import bootstrap_legacy_authority

        self.tmp = tempfile.TemporaryDirectory(prefix="quota-sentinel-agy-priority-")
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
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

    def test_default_runs_agy_first_with_pi_as_its_fallback(self):
        router = self.router()
        self.assertEqual(router.transport("antigravity"), "agy")
        agy_runner = router.runner_for("antigravity")
        self.assertEqual(agy_runner.TRANSPORT, "agy")
        fallback = agy_runner.fallback
        self.assertEqual(fallback.TRANSPORT, "pi")
        # One hop, then stop: the fallback has no fallback of its own.
        self.assertEqual(fallback.fallback_for, {})

    def test_selecting_pi_reverts_the_priority(self):
        router = self.router(QUOTA_SENTINEL_TRANSPORT="antigravity=pi")
        self.assertEqual(router.transport("antigravity"), "pi")
        pi_runner = router.runner_for("antigravity")
        self.assertNotIn("antigravity", pi_runner.fallback_for)

    def test_codex_keeps_its_own_priority(self):
        router = self.router()
        self.assertEqual(router.transport("codex"), "pi")
        self.assertEqual(router.runner_for("codex").fallback_for["codex"].TRANSPORT, "codex")

    def test_missing_agy_binary_refuses_before_any_attempt(self):
        from quota_sentinel.runtime.factory import readiness_problems

        env = dict(self.env)
        env["QUOTA_SENTINEL_AGY_BIN"] = str(self.state / "does-not-exist")
        problems = readiness_problems(self.state, environment=env, providers=("antigravity",))
        self.assertTrue(any("agy is not executable" in problem for problem in problems), problems)

    def test_pi_is_required_as_the_agy_fallback(self):
        from quota_sentinel.runtime.factory import readiness_problems

        env = dict(self.env)
        env["QUOTA_SENTINEL_AGY_BIN"] = sys.executable
        env["QUOTA_SENTINEL_PI_BIN"] = str(self.state / "does-not-exist")
        problems = readiness_problems(self.state, environment=env, providers=("antigravity",))
        self.assertTrue(any("pi is not executable" in problem for problem in problems), problems)

    def test_a_pi_only_roster_does_not_demand_agy(self):
        from quota_sentinel.runtime.factory import readiness_problems

        env = dict(self.env)
        env["QUOTA_SENTINEL_TRANSPORT"] = "antigravity=pi"
        env["QUOTA_SENTINEL_AGY_BIN"] = str(self.state / "does-not-exist")
        problems = readiness_problems(self.state, environment=env, providers=("antigravity",))
        self.assertFalse(any("agy is not executable" in problem for problem in problems), problems)


if __name__ == "__main__":
    unittest.main(verbosity=2)
