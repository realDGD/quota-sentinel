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
  * that guard is bounded TWICE, and the two bounds are ordered: the wrapper's
    deadline is strictly shorter than the parent's last-resort deadline, so the
    wrapper — which owns the CLI's process group — is what kills and reaps the
    CLI on a timeout. A CLI that ignores SIGTERM must not outlive the guard, and
    a wedged wrapper must not orphan the CLI it started in its own session;
  * the pre-turn `Eligibility check failed` handshake is retried and costs
    nothing, and a retry must not be mistaken for a malformed reply;
  * that retry is fenced twice: only the CURRENT turn's stderr is read (the
    file is append-only, so a marker from an earlier turn would otherwise make a
    later, unrelated failure look free), and the turn must have spent nothing (a
    handshake fails before the turn starts, so a failure that reported tokens is
    not a free retry);
  * success is read from the parsed JSON (status, response, usage) rather than
    from the log format, and a SUCCESS that cannot report `input_tokens >= 1` is
    a functional failure: the input ceiling is the only structural proof that
    the minimal profile rather than the stock agent answered;
  * a functional failure falls back to Pi; a cost regression does NOT — the
    reply was already delivered and the window anchored, so re-delivering it
    would spend more quota to reach the same fact, and a regressed profile is
    not recorded as verified, so the next attempt re-tests instead of trusting
    it;
  * the attempt environment carries the subscription credential only: the
    Gemini API key, which bills a different product and would not anchor the
    same window, is removed.

Run: PYTHONPATH=. uv run --frozen --no-sync python tests/python-agy-exec-regression.py
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
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
import json, os, signal, subprocess, sys, time

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
    if mode == "agents-parent-exits":
        pid_file = os.environ["QS_GRANDCHILD_PID"]
        subprocess.Popen(
            [sys.executable, "-c",
             'import os,signal,sys,time; '
             'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
             'open(sys.argv[1], "w").write(str(os.getpid())); time.sleep(60)',
             pid_file],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        while not os.path.exists(pid_file):
            time.sleep(0.01)
        time.sleep(60)
    if mode == "agents-ignore-term":
        # A wedged CLI: it answers nothing and refuses SIGTERM, so only a
        # SIGKILL — the wrapper's escalation, or the parent's last resort — can
        # end it. Nothing below this line is reached.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(60)
    if mode == "agents-unavailable":
        print("the backend is unreachable", file=sys.stderr)
        print("not json at all")
        raise SystemExit(0)
    if mode == "missing-agent":
        agents = []
    elif mode == "other-agent-only":
        # The list answered; this agent is genuinely not on it. That is False,
        # which is a different fact from the None of an unreadable list.
        agents = ["another-agent"]
    else:
        agents = ["quota-primer", "another-agent"]
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

if mode == "stderr-transient-once" and turns() <= 1:
    # The same free handshake, but reported on the CLI's own stderr instead of
    # in the JSON `error` field. It must still be recognised — from the bytes
    # THIS turn wrote, which is the slice the transport now reads.
    print("Eligibility check failed: EOF", file=sys.stderr)
    print(json.dumps({transient}))
    raise SystemExit(1)

if mode == "stale-marker":
    if turns() <= 1:
        print("Eligibility check failed: EOF", file=sys.stderr)
        print(json.dumps({transient}))
        raise SystemExit(1)
    # A later, unrelated failure that spends nothing and reports no marker of
    # its own. The marker above is still sitting in the appended stderr file.
    print(json.dumps({{
        "status": "ERROR", "response": "", "error": "unrelated backend refusal",
        "num_turns": 0,
        "usage": {{"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
                   "cache_read_tokens": 0, "total_tokens": 0}},
    }}))
    raise SystemExit(1)

if mode == "marker-with-spend":
    # The handshake marker IS in this turn's own stderr, but the turn reports
    # 564 input tokens: it cannot have failed before it started.
    print("Eligibility check failed: EOF", file=sys.stderr)
    print(json.dumps({{
        "status": "ERROR", "response": "", "error": "unrelated backend refusal",
        "num_turns": 0,
        "usage": {{"input_tokens": 564, "output_tokens": 0, "thinking_tokens": 0,
                   "cache_read_tokens": 0, "total_tokens": 564}},
    }}))
    raise SystemExit(1)

if mode == "no-usage":
    # A SUCCESS turn that reports nothing about what it spent: the input
    # ceiling cannot be evaluated, so nothing proves which profile answered.
    print(json.dumps({{"status": "SUCCESS", "response": "1"}}))
    raise SystemExit(0)

if mode == "zero-usage":
    # Present but zero: the shape a free read-only command has. `0` is no more
    # verified than absent, because the ceiling cannot judge it either.
    print(json.dumps({{"status": "SUCCESS", "response": "1", "num_turns": 0,
                       "usage": {{"input_tokens": 0, "output_tokens": 0,
                                  "thinking_tokens": 0, "cache_read_tokens": 0,
                                  "total_tokens": 0}}}}))
    raise SystemExit(0)

if mode == "error-no-usage":
    # Non-SUCCESS keeps its existing handling: no usage to report is normal
    # here, so the attempt is judged on its status alone.
    print(json.dumps({{"status": "ERROR", "response": "", "error": "boom"}}))
    raise SystemExit(1)

input_tokens = 22311 if mode == "stock-cost" else 564
thinking = 30 if mode == "thinking" else 0
output = 30 if mode == "thinking" else 1
reply = {{"success": "1", "stock-cost": "1", "thinking": "1",
          "transient-once": "1", "stderr-transient-once": "1", "missing-agent": "1",
          "agents-unavailable": "1"}}.get(mode, "Sure, what would you like me to do?")
print(json.dumps({{
    "conversation_id": "c-1", "status": "SUCCESS", "response": reply,
    "duration_seconds": 3, "num_turns": 1,
    "usage": {{"input_tokens": input_tokens, "output_tokens": output,
               "thinking_tokens": thinking, "cache_read_tokens": 0,
               "total_tokens": input_tokens + output}},
}}))
'''.format(python=sys.executable, transient=json.dumps(TRANSIENT_BODY))

# A stand-in for run_with_timeout.py itself, in the one state no inner deadline
# can save: the wrapper wedges and never gets around to killing anything. It
# starts its child the same way the real helper does — in a session of its own —
# so the parent's last resort cannot reach that child by killing the wrapper's
# process group, only through a process-tree snapshot. It publishes both PIDs so
# the test can check what survived.
WEDGED_WRAPPER = '''#!{python}
import json, os, subprocess, sys, time

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=True
)
with open(os.environ["QS_WEDGE_RECORD"], "w") as handle:
    handle.write(json.dumps({{"wrapper": os.getpid(), "child": child.pid}}))
time.sleep(600)
'''.format(python=sys.executable)


def _alive(pid) -> bool:
    """Whether ``pid`` is a live process.

    A zombie that its parent has not reaped yet is not alive for the purpose of
    "did anything survive the bound": it holds no code, no descriptors and no
    quota, and the kernel is about to collect it.
    """
    completed = subprocess.run(
        ["/bin/ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True, text=True, check=False,
    )
    state = completed.stdout.strip()
    return bool(state) and not state.startswith("Z")


def _wait_for(predicate, timeout=5.0, interval=0.05):
    """Poll ``predicate`` until it is truthy or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(interval)


def _kill_pids(pids) -> None:
    """Best-effort fixture hygiene: never leave a test process behind."""
    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGKILL)
        except (OSError, TypeError, ValueError):
            pass


def _kill_matching(pattern: str) -> None:
    """Best-effort fixture hygiene by command line, for a test that failed."""
    completed = subprocess.run(
        ["pgrep", "-f", pattern], capture_output=True, text=True, check=False,
    )
    _kill_pids(completed.stdout.split())


class _FakePi:
    """Stands in for the Pi runner: records the delegation, returns its own result.

    ``success=False`` is the pessimistic stub: used to prove that an attempt is
    never handed to Pi when it does not have to be, because a fallback that
    fails would turn a delivered success into a failure the scheduler retries.
    """

    def __init__(self, success=True):
        self.prepared = []
        self.calls = []
        self.success = success

    def prepare(self, provider, workspace):
        self.prepared.append((provider, Path(workspace)))

    def run(self, provider, workspace, phase, attempt, limit):
        self.calls.append((provider, phase, attempt, limit))
        return AttemptResult(
            success=self.success, exit_code=0, timed_out=False, elapsed=1.5,
            stdout_path=Path(workspace) / "pi-stdout",
            stderr_path=Path(workspace) / "pi-stderr",
            quota_path=Path(workspace) / "pi-quota.json",
            error_summary="" if self.success else "pi:stub-failure",
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
        # Hygiene, not assertion: whatever the test proved, a fixture CLI must
        # not outlive it (registered after the tmpdir, so it runs before it).
        self.addCleanup(_kill_matching, str(self.agy))
        self.lines: list = []
        self.pi = _FakePi()

    def runner(self, *, mode="success", fallback=None, timeout=30, kill_grace=1,
               environment=None, preflight=True) -> AgyExecRunner:
        env = {"QS_FAKE_AGY_RECORD": str(self.record), "QS_FAKE_AGY_MODE": mode}
        env.update(environment or {})
        config = AgyExecConfig(
            agy_bin=self.agy,
            state_dir=self.state_dir,
            timeout=timeout,
            kill_grace=kill_grace,
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

    def survivors(self, timeout=3.0) -> list:
        """PIDs whose command line still matches this test's fake agy.

        Polled rather than sampled once: a CLI that was SIGKILLed is reparented
        and collected a moment later, and the contract is "no survivor shortly
        after the call returned", not "no survivor in the same microsecond".
        """
        def snapshot():
            completed = subprocess.run(
                ["pgrep", "-f", str(self.agy)],
                capture_output=True, text=True, check=False,
            )
            return [
                pid for pid in completed.stdout.split()
                if pid.isdigit() and int(pid) != os.getpid()
            ]

        deadline = time.monotonic() + timeout
        while True:
            pids = snapshot()
            if not pids or time.monotonic() >= deadline:
                return pids
            time.sleep(0.05)

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

    # ----------------------------------------------------------- guard cleanup
    def test_the_guard_reports_a_listed_agent(self):
        runner = self.runner()
        paths = runner.prepare("antigravity", self.workspace)
        self.assertIs(runner.agent_listed(paths), True)
        self.assertEqual(self.calls("agents")[0]["argv"][:2], ["-p", "/agents"])

    def test_the_guard_reports_an_agent_that_is_not_listed(self):
        # The list answered, and this agent is not on it: False. That is a
        # different fact from the None of a list that could not be read, and
        # the guard's whole point is the difference between the two.
        runner = self.runner(mode="other-agent-only")
        paths = runner.prepare("antigravity", self.workspace)
        self.assertIs(runner.agent_listed(paths), False)

    def test_a_guard_that_ignores_sigterm_is_bounded_and_leaves_no_survivor(self):
        # The two deadlines used to be the SAME number, so the parent killed the
        # wrapper at the very instant the wrapper began its own SIGTERM→SIGKILL
        # escalation: a CLI that ignores SIGTERM then outlived the bounded call,
        # one leaked CLI per timed-out guard. The wrapper's deadline is now the
        # shorter one, so the wrapper wins the race and reaps the group it made.
        runner = self.runner(mode="agents-ignore-term", timeout=2, kill_grace=0.5)
        paths = runner.prepare("antigravity", self.workspace)
        started = time.monotonic()
        listed = runner.agent_listed(paths)
        elapsed = time.monotonic() - started
        # Still a timeout, still non-fatal: an unreachable backend is not
        # evidence that the agent is missing.
        self.assertIsNone(listed)
        self.assertTrue(self.calls("agents"), "the fake CLI never received /agents")
        # ...and still bounded, at the wrapper's deadline (1.5s) plus its kill
        # grace (0.5s), not at an unbounded wait for a CLI that never exits.
        self.assertLess(elapsed, 2 + 0.5 + 1.0)
        self.assertEqual(self.survivors(), [], "the guard outlived itself")

    def test_guard_reaps_a_stubborn_child_when_agy_exits_on_sigterm(self):
        pid_file = self.root / "grandchild.pid"

        def cleanup_child():
            if pid_file.exists():
                _kill_pids([int(pid_file.read_text())])

        self.addCleanup(cleanup_child)
        runner = self.runner(
            mode="agents-parent-exits", timeout=2, kill_grace=0.5,
            environment={"QS_GRANDCHILD_PID": str(pid_file)},
        )
        paths = runner.prepare("antigravity", self.workspace)
        self.assertIsNone(runner.agent_listed(paths))
        child_pid = int(pid_file.read_text())
        self.assertTrue(
            _wait_for(lambda: not _alive(child_pid), timeout=2),
            "grandchild survived after agy exited on SIGTERM",
        )

    def test_the_guard_wrapper_deadline_is_strictly_shorter_than_the_parent_guard(self):
        # Both numbers must derive from the config, and the wrapper's must stay
        # strictly the shorter one: `inner == timeout` is exactly the collapse
        # that leaks a CLI. `kill_grace=0` is the trap case — `timeout -
        # kill_grace` would collapse them there.
        for timeout, kill_grace in ((30, 1), (60, 10), (2, 0.5), (4, 0)):
            runner = self.runner(timeout=timeout, kill_grace=kill_grace)
            command = runner._guard_command([str(self.agy), "-p", "/agents"])
            inner = float(command[command.index("--timeout") + 1])
            grace = float(command[command.index("--kill-grace") + 1])
            parent = runner._guard_timeout()
            with self.subTest(timeout=timeout, kill_grace=kill_grace):
                self.assertLess(inner, timeout, "the wrapper no longer wins the race")
                self.assertLessEqual(
                    inner + grace, timeout + 0.05,
                    "the wrapper's worst case outlives the guard's promise",
                )
                self.assertGreater(
                    parent, inner + grace,
                    "the parent guard can fire before the wrapper has finished",
                )
                self.assertGreater(parent, timeout)
        # The numbers follow the config, not a constant in the transport.
        slow = self.runner(timeout=60)._guard_command([str(self.agy)])
        quick = self.runner(timeout=30)._guard_command([str(self.agy)])
        self.assertEqual(
            float(slow[slow.index("--timeout") + 1])
            - float(quick[quick.index("--timeout") + 1]),
            30.0,
        )

    def test_the_turn_path_keeps_the_wrapper_as_its_only_bound(self):
        # _run_cli deliberately passes NO outer deadline: there the wrapper's own
        # deadline is the only bound, so the wrapper always outlives it and gets
        # to reap the CLI's process group itself. A second, equal deadline here
        # is exactly the guard's leak — this pins the shape so the fix cannot
        # migrate the bug to the turn path.
        runner = self.runner(timeout=30)
        paths = runner.prepare("antigravity", self.workspace)
        seen = {}
        real_run = agy_exec.subprocess.run

        def spy(command, **kwargs):
            seen["command"] = command
            seen["timeout"] = kwargs.get("timeout")
            return real_run(command, **kwargs)

        agy_exec.subprocess.run = spy
        try:
            exit_code, _elapsed, _offset = runner._run_cli(
                [str(self.agy), "-p", USER_PROMPT], paths
            )
        finally:
            agy_exec.subprocess.run = real_run
        self.assertEqual(exit_code, 0)
        self.assertIsNone(seen.get("timeout"), "the turn path gained an outer deadline")
        command = seen["command"]
        self.assertEqual(float(command[command.index("--timeout") + 1]), 30)
        self.assertEqual(float(command[command.index("--kill-grace") + 1]), 1)

    def test_the_parent_last_resort_reaps_a_tree_whose_wrapper_wedged(self):
        # The wrapper is the polite path, but it is also a Python process: if IT
        # is the thing that wedges, no inner deadline can help. The parent's last
        # resort must then end the CLI the wrapper started — which the helper
        # puts in a session of ITS OWN, so killing the wrapper's process group
        # cannot reach it. Only a tree snapshot taken while the wrapper is still
        # alive can, which is why the snapshot comes before the kill.
        record = self.root / "wedged.json"
        wrapper = self.root / "wedged-wrapper"
        wrapper.write_text(WEDGED_WRAPPER)
        wrapper.chmod(0o755)
        env = dict(os.environ)
        env["QS_WEDGE_RECORD"] = str(record)
        process = subprocess.Popen(
            [sys.executable, str(wrapper)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.assertTrue(
            _wait_for(lambda: record.exists()), "the wedged wrapper never started"
        )
        pids = json.loads(record.read_text())
        self.addCleanup(_kill_pids, list(pids.values()))
        self.assertTrue(_alive(pids["wrapper"]), pids)
        self.assertTrue(_alive(pids["child"]), pids)

        agy_exec._reap_guard_tree(process)

        self.assertEqual(
            _wait_for(lambda: not any(_alive(pid) for pid in pids.values())),
            True,
            "the parent's last resort left the tree running: %s" % pids,
        )

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

    def test_a_current_turn_stderr_marker_is_still_a_free_retry(self):
        # The fence must not be so tight that the real handshake stops being
        # retried: the marker is on stderr, this turn spent nothing, so it is
        # still free.
        result = self.runner(mode="stderr-transient-once").run(
            "antigravity", self.workspace, "initial", 1, 3
        )
        self.assertTrue(result.success)
        self.assertEqual(len(self.calls("turn")), 2)
        self.assertIn("transient handshake failure; retry 1/3", self.log())

    def test_a_stale_stderr_marker_does_not_make_a_later_failure_transient(self):
        # The marker is written by the FIRST turn, which really was the free
        # handshake. The second turn's own failure is unrelated and the marker
        # is still in the appended file, so reading the whole tail would retry
        # it three more times for nothing.
        result = self.runner(mode="stale-marker").run(
            "antigravity", self.workspace, "initial", 1, 3
        )
        self.assertFalse(result.success)
        self.assertEqual(len(self.calls("turn")), 2)
        self.assertIn("status=ERROR", result.error_summary)
        self.assertEqual(self.log().count("transient handshake failure"), 1)
        # The file itself stays append-only: only the slice is per-turn, so the
        # first turn's diagnosis is still on disk.
        stderr = (self.workspace / "antigravity-stderr").read_text()
        self.assertIn("Eligibility check failed", stderr)

    def test_a_spent_token_failure_is_not_retried_as_a_handshake(self):
        # The marker is in THIS turn's stderr, so only the second fence can
        # refuse the retry: the turn reports 564 input tokens, and a handshake
        # fails before a turn starts.
        result = self.runner(mode="marker-with-spend").run(
            "antigravity", self.workspace, "initial", 1, 3
        )
        self.assertEqual(len(self.calls("turn")), 1)
        self.assertFalse(result.success)
        self.assertIn("status=ERROR", result.error_summary)
        self.assertNotIn("transient handshake", self.log())

    def test_the_stderr_window_is_the_current_turn_only(self):
        path = self.root / "stderr-window.log"
        path.write_bytes(b"earlier turn: Eligibility check failed\n" * 20)
        offset = agy_exec._file_size(path)
        with path.open("ab") as handle:
            handle.write(b"this turn: something else\n")
        window = agy_exec._stderr_tail(path, offset)
        self.assertIn("this turn: something else", window)
        self.assertNotIn("Eligibility check failed", window)
        # Diagnosis is not lost: the whole file is still readable, and the
        # 4096-byte cap still applies to the window.
        self.assertIn("Eligibility check failed", agy_exec._stderr_tail(path))
        with path.open("ab") as handle:
            handle.write(b"x" * 5000)
        self.assertEqual(len(agy_exec._stderr_tail(path, offset)), agy_exec._STDERR_TAIL_BYTES)

    # ------------------------------------------------------------------ cost
    def test_stock_agent_cost_is_accepted_and_never_re_delivered(self):
        # The reply `1` was already delivered and the window anchored, so this
        # attempt is usable as it stands. Handing it to Pi would spend more
        # quota to reach the same fact — and Pi failing here (the stub does)
        # would turn a delivered success into a failure the scheduler pays for
        # again.
        pi = _FakePi(success=False)
        result = self.runner(mode="stock-cost", fallback=pi).run(
            "antigravity", self.workspace, "initial", 1, 3
        )
        self.assertTrue(result.success)
        self.assertEqual(result.error_summary, "")
        self.assertEqual(pi.calls, [])
        self.assertEqual(pi.prepared, [])
        self.assertIn("result=cost-regression", self.log())
        self.assertIn("cost regression (input=22311>=%s" % AGY_INPUT_CEILING, self.log())
        self.assertIn("the delivery is accepted", self.log())
        # ...but it is NOT a verified ignition, and it is not recorded as one.
        self.assertNotIn("ignition", self.log())
        self.assertIsNone(self.smoke_record())

    def test_a_cost_regression_succeeds_even_without_a_fallback(self):
        result = self.runner(mode="stock-cost").run(
            "antigravity", self.workspace, "initial", 1, 3
        )
        self.assertTrue(result.success)
        self.assertEqual(result.error_summary, "")
        self.assertIn("result=cost-regression", self.log())
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

    def test_a_runaway_reply_is_an_accepted_cost_regression(self):
        runner = self.runner(fallback=self.pi)
        runner.config.output_ceiling = 1
        result = runner.run("antigravity", self.workspace, "initial", 1, 3)
        self.assertTrue(result.success)
        self.assertEqual(result.error_summary, "")
        self.assertEqual(self.pi.calls, [])
        self.assertIn("output=1>=1", self.log())
        self.assertIn("result=cost-regression", self.log())
        self.assertIsNone(self.smoke_record())

    def test_a_cost_regression_does_not_refresh_an_existing_verified_record(self):
        # The record is what makes the next attempt skip the smoke test, so a
        # regression must leave it exactly as it was: accepted this time, but
        # the profile still needs attention.
        path = self.state_dir / "agy-exec-profile.json"
        before = {"version": "1.2.12-fake", "fingerprint": profile_fingerprint(),
                  "usage": {"input_tokens": 564}, "checked_at": 1}
        path.write_text(json.dumps(before))
        result = self.runner(mode="stock-cost").run(
            "antigravity", self.workspace, "initial", 1, 3
        )
        self.assertTrue(result.success)
        self.assertEqual(self.smoke_record(), before)
        self.assertNotIn("profile changed", self.log())

    # -------------------------------------------------------- unverified usage
    def test_a_success_without_usage_is_not_a_verified_success(self):
        # The input ceiling is the only structural proof of WHICH profile
        # answered; a SUCCESS turn that cannot report what it spent cannot be
        # checked against it, so it is not a verified ignition.
        result = self.runner(mode="no-usage").run(
            "antigravity", self.workspace, "initial", 1, 3
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error_summary, "functional:usage=unverified")
        self.assertIn("result=failed", self.log())
        self.assertIsNone(self.smoke_record())

    def test_a_success_without_usage_falls_back_to_pi(self):
        # Functional, so the Pi fallback is exactly as it is for any other
        # functional failure.
        result = self.runner(mode="no-usage", fallback=self.pi).run(
            "antigravity", self.workspace, "initial", 1, 3
        )
        self.assertIn("functional:usage=unverified", self.log())
        self.assertEqual(self.pi.prepared, [("antigravity", self.workspace)])
        self.assertEqual(self.pi.calls, [("antigravity", "initial", 1, 3)])
        self.assertTrue(result.success)  # delivered by the fallback
        self.assertIsNone(self.smoke_record())

    def test_a_success_reporting_zero_input_is_not_a_verified_success(self):
        # 0 is as unverifiable as absent: the ceiling cannot tell a free
        # read-only command from the question being answered for nothing.
        result = self.runner(mode="zero-usage").run(
            "antigravity", self.workspace, "initial", 1, 3
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error_summary, "functional:usage=unverified")

    def test_a_non_success_turn_keeps_its_own_handling_of_missing_usage(self):
        # Non-SUCCESS has no usage to report by nature; that must not become a
        # second, misleading problem on top of the status.
        result = self.runner(mode="error-no-usage").run(
            "antigravity", self.workspace, "initial", 1, 3
        )
        self.assertFalse(result.success)
        self.assertIn("status=ERROR", result.error_summary)
        self.assertNotIn("usage=unverified", result.error_summary)

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
        # A missing `usage` is not a parse problem here: `parse_result` reports
        # what the document says, and whether an unreported ignition counts as a
        # verified success is a per-turn policy decision, not a parse one.
        self.assertEqual(
            parse_result(json.dumps({"status": "SUCCESS", "response": "1"}))[3], []
        )


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
