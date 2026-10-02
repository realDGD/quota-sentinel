#!/usr/bin/env python3
"""Hermetic quota ladder tests: fake executables, real files and normalizers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quota_sentinel.quota import Tier
from quota_sentinel.runtime import factory as factory_module
from quota_sentinel.runtime import probe_budget
from quota_sentinel.runtime.factory import quota_probe_options
from quota_sentinel.runtime.quota_probe import QuotaCollector, _run_bounded


WINDOWS = {
    "fiveHour": {"remainingPercent": 81, "resetAt": 1790000000},
    "weekly": {"remainingPercent": 92, "resetAt": 1790500000},
}


class QuotaProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.work = self.root / "work"
        self.work.mkdir()
        self.missing = self.root / "missing"

    def binary(self, name: str, body: str) -> Path:
        path = self.root / name
        path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
        path.chmod(0o700)
        return path

    def collector(self, **overrides):
        args = dict(
            state_dir=self.state,
            workspace=self.work,
            codex_bin=self.missing,
            agy_bin=self.missing,
            uv_bin=self.missing,
            codexbar_bin=self.missing,
            opencode_usage_helper=self.missing,
            antigravity_usage_helper=self.missing,
            clinepass_usage_helper=self.missing,
            curl_bin=self.missing,
            opencode_api_key_getter=lambda: "",
        )
        args.update(overrides)
        return QuotaCollector(**args)

    def stubborn_descendant(self):
        """A ready child ignores TERM and holds none of the probe's pipes."""
        pid_file = self.root / "stubborn-child.pid"

        def cleanup_child():
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

        self.addCleanup(cleanup_child)
        body = f"""
import subprocess, sys, time
from pathlib import Path
subprocess.Popen(
    [sys.executable, '-c',
     'import os,signal,sys,time; '
     'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
     'open(sys.argv[1], "w").write(str(os.getpid())); time.sleep(30)',
     {str(pid_file)!r}],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
while not Path({str(pid_file)!r}).exists():
    time.sleep(0.01)
"""
        return body, pid_file

    def assert_child_gone(self, pid_file):
        child_pid = int(pid_file.read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail("grandchild survived after probe parent exited on SIGTERM")

    # ---- Per-tier isolation: the shell runs every rung in its own process,
    # so one rung's failure is an rc, never an exception that ends the probe.
    def test_a_crashing_tier_fails_only_that_rung_and_the_ladder_continues(self):
        cache = self.state / "codexbar-codex-last-success.json"
        self.state.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(dict(WINDOWS, source="CodexBar · cli",
                                         fresh=False, cached=True,
                                         capturedAt=1790000000)),
                         encoding="utf-8")

        for error in (RuntimeError("probe crashed"), BrokenPipeError(32, "Broken pipe")):
            with self.subTest(error=type(error).__name__):
                def explode(_provider, _error=error):
                    raise _error

                collector = self.collector()
                collector._native = explode  # type: ignore[assignment]
                readings = collector.collect()
                # Codex degrades to its cache instead of aborting the roster…
                self.assertEqual(readings["codex"].tier, Tier.CODEXBAR_CACHE)
                # …and the other providers are still collected independently.
                self.assertEqual(sorted(readings), ["antigravity", "clinepass", "codex", "opencode"])

    def test_a_codex_app_server_that_dies_immediately_does_not_escape_collect(self):
        codex = self.binary("codex", """
import sys
sys.stdin.close()
""")
        readings = self.collector(codex_bin=codex).collect()
        self.assertEqual(sorted(readings), ["antigravity", "clinepass", "codex", "opencode"])

    def test_out_of_range_captured_at_in_a_persisted_file_fails_only_that_tier(self):
        # A zero year is a plain ValueError out of the timestamp parser, not a
        # normalization error: it must still stay inside the tier boundary.
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "codexbar-codex-last-success.json").write_text(
            json.dumps(dict(WINDOWS, source="CodexBar · cli", fresh=False,
                            cached=True, capturedAt="0000-01-01T00:00:00Z")),
            encoding="utf-8",
        )
        snapshot_source = {
            "source": "Pi 快照", "fresh": False, "cached": True,
            "capturedAt": 1790000000,
        }
        (self.state / "pi-codex-quota.json").write_text(
            json.dumps(dict(WINDOWS, **snapshot_source)), encoding="utf-8"
        )
        readings = self.collector().collect()
        self.assertEqual(readings["codex"].tier, Tier.PI_SNAPSHOT)

    def test_an_unknown_tier_is_refused_rather_than_served_as_a_snapshot(self):
        collector = self.collector()
        with mock.patch(
            "quota_sentinel.runtime.quota_probe.tier_plan",
            return_value=("not-a-tier",),
        ):
            with self.assertRaises(ValueError):
                collector.collect()

    def test_native_helper_output_is_used_verbatim_when_it_is_a_document(self):
        """The native antigravity branch reaches its helper and uses it.

        The helper is the producer of a final document; the port must accept
        a live reading from it rather than re-deriving one.
        """
        helper = self.binary("antigravity_usage.py", """
import json
print(json.dumps({"source": "Native · agy local service", "fresh": True,
                  "capturedAt": 1790000000,
                  "fiveHour": {"remainingPercent": 77, "resetAt": 1790000000},
                  "weekly": {"remainingPercent": 86, "resetAt": 1790500000}}))
""")
        # The helper runs through `uv run ... python -B <helper>`; stand in a
        # uv that drops its own flags and executes the helper directly.
        fake_uv = self.binary("uv", """
import os, sys
args = sys.argv[1:]
os.execv(sys.executable, [sys.executable] + args[args.index('python') + 1:])
""")
        readings = self.collector(
            antigravity_usage_helper=helper,
            agy_bin=self.binary("agy", "print('agy')"),
            uv_bin=fake_uv,
        ).collect()
        self.assertEqual(readings["antigravity"].tier, Tier.NATIVE)
        self.assertTrue(readings["antigravity"].fresh)

    def test_native_antigravity_bound_leaves_room_for_uv_startup(self):
        """The shell has NO outer bound on the antigravity helper.

        The port's outer bound must therefore sit well above the helper's own
        deadline: uv + python start-up happens BEFORE the helper's internal
        timeout starts counting, so a tight outer bound would SIGTERM probes
        the shell accepts and silently fall back to CodexBar.
        """
        seen = {}

        def fake_run(command, timeout, grace=1.0, stdin=None, **options):
            seen["timeout"] = timeout
            return type("Result", (), {
                "returncode": 1, "stdout": b"", "stderr": b"", "timed_out": False,
            })()

        collector = self.collector(
            agy_bin=self.binary("agy", "print('agy')"),
            uv_bin=self.binary("uv", "print('uv')"),
            antigravity_native_timeout=20,
        )
        with mock.patch(
            "quota_sentinel.runtime.quota_probe._run_bounded",
            side_effect=fake_run,
        ):
            collector._native_helper("antigravity")
        self.assertGreaterEqual(seen["timeout"], 25)

    def test_native_codex_wins_without_running_codexbar(self):
        codex = self.binary("codex", """
import json, sys
for line in sys.stdin:
    req = json.loads(line)
    if req['method'] == 'initialize':
        answer = {'jsonrpc': '2.0', 'id': req['id'], 'result': {}}
    else:
        answer = {'jsonrpc': '2.0', 'id': req['id'], 'result': {'rateLimits': {
            'primary': {'windowDurationMins': 300, 'usedPercent': 19, 'resetsAt': 1790000000},
            'secondary': {'windowDurationMins': 10080, 'usedPercent': 8, 'resetsAt': 1790500000}}}}
    print(json.dumps(answer), flush=True)
""")
        marker = self.root / "codexbar-ran"
        bar = self.binary("codexbar", f"""import sys
from pathlib import Path
if sys.argv[sys.argv.index('--provider') + 1] == 'codex':
    Path({str(marker)!r}).write_text('ran')
""")
        result = self.collector(codex_bin=codex, codexbar_bin=bar).collect()["codex"]
        self.assertEqual(result.tier, Tier.NATIVE)
        self.assertTrue(result.fresh)
        self.assertEqual(result.quota.five_hour.remaining_percent, 81)
        self.assertEqual(result.quota.weekly.reset_at, 1790500000)
        self.assertEqual(result.quota.source, "Native · codex app-server")
        self.assertFalse(marker.exists())

    def test_native_codex_cleanup_reaps_a_stubborn_descendant(self):
        body, pid_file = self.stubborn_descendant()
        codex = self.binary("codex", body + """
import json
for line in sys.stdin:
    req = json.loads(line)
    if req['method'] == 'initialize':
        result = {}
    else:
        result = {'rateLimits': {
            'primary': {'windowDurationMins': 300, 'usedPercent': 19, 'resetsAt': 1790000000},
            'secondary': {'windowDurationMins': 10080, 'usedPercent': 8, 'resetsAt': 1790500000}}}
    print(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'result': result}), flush=True)
""")
        result = self.collector(codex_bin=codex)._native_codex()
        self.assertIsNotNone(result)
        self.assertEqual(result.five_hour.remaining_percent, 81)
        self.assert_child_gone(pid_file)

    def test_codexbar_retries_oauth_after_bad_cli_and_writes_stale_cache(self):
        calls = self.root / "calls"
        rows = [{"provider": "codex", "source": "oauth", "usage": {
            "primary": {"windowMinutes": 300, "usedPercent": 19, "resetsAt": 1790000000},
            "secondary": {"windowMinutes": 10080, "usedPercent": 8, "resetsAt": 1790500000}}}]
        bar = self.binary("codexbar", f"""
import json, sys
from pathlib import Path
if sys.argv[sys.argv.index('--provider') + 1] == 'codex':
    with Path({str(calls)!r}).open('a') as f: f.write(sys.argv[sys.argv.index('--source') + 1] + '\\n')
if sys.argv[sys.argv.index('--source') + 1] == 'cli': print('{{}}')
else: print(json.dumps({rows!r}))
""")
        result = self.collector(codexbar_bin=bar).collect()["codex"]
        self.assertEqual(result.tier, Tier.CODEXBAR_LIVE)
        self.assertTrue(result.fresh)
        self.assertEqual(calls.read_text(), "cli\noauth\n")
        cache_path = self.state / "codexbar-codex-last-success.json"
        cache = json.loads(cache_path.read_text())
        self.assertEqual(cache["source"], "CodexBar · cached（可能不是最新）")
        self.assertEqual(cache["originalSource"], "CodexBar · oauth")
        self.assertFalse(cache["fresh"])
        self.assertTrue(cache["cached"])
        self.assertEqual(cache["capturedAt"], result.quota.captured_at)
        self.assertEqual(cache_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)

    def test_cache_fallback_preserves_capture_time_and_cannot_claim_fresh(self):
        self.state.mkdir()
        cache_path = self.state / "codexbar-codex-last-success.json"
        cache_path.write_text(json.dumps(dict(
            source="CodexBar · cached（可能不是最新）", originalSource="CodexBar · cli",
            fresh=True, cached=False, capturedAt="2026-08-29T11:21:38.870Z", **WINDOWS)))
        result = self.collector().collect()["codex"]
        self.assertEqual(result.tier, Tier.CODEXBAR_CACHE)
        self.assertFalse(result.fresh)
        self.assertFalse(result.quota.fresh)
        self.assertTrue(result.quota.cached)
        self.assertEqual(result.quota.captured_at, 1788002498)

    def test_pi_current_capture_precedes_saved_snapshot_and_is_stale(self):
        self.state.mkdir()
        saved = dict(source="Pi 快照（可能不是最新）", fresh=False, cached=True,
                     capturedAt=100, fiveHour={"remainingPercent": 2, "resetAt": 200},
                     weekly={"remainingPercent": 3, "resetAt": 300})
        (self.state / "pi-codex-quota.json").write_text(json.dumps(saved))
        raw = self.work / "codex-quota.json"
        raw.write_text((ROOT / "tests/quota-fixture.json").read_text())
        result = self.collector().collect(pi_raw={"codex": raw})["codex"]
        self.assertEqual(result.tier, Tier.PI_SNAPSHOT)
        self.assertFalse(result.fresh)
        self.assertEqual(result.quota.five_hour.remaining_percent, 48)
        self.assertEqual(result.quota.five_hour.reset_at, 1788012274)

    def test_save_pi_snapshots_keeps_previous_file_after_bad_capture(self):
        collector = self.collector()
        raw = self.work / "codex-quota.json"
        raw.write_text((ROOT / "tests/quota-fixture.json").read_text())
        collector.save_pi_snapshots({"codex": raw})
        path = self.state / "pi-codex-quota.json"
        self.assertEqual(json.loads(path.read_text())["fiveHour"]["remainingPercent"], 48)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        raw.write_text("not json")
        collector.save_pi_snapshots({"codex": raw})
        self.assertEqual(json.loads(path.read_text())["fiveHour"]["remainingPercent"], 48)

    def test_malformed_native_and_live_payload_fall_through_without_stale_output(self):
        helper = self.root / "opencode_helper.py"
        helper.write_text("print('{bad json')\n")
        rows = [{"provider": "opencodego", "source": "api", "usage": {
            "primary": {"windowMinutes": 300, "usedPercent": 10, "resetsAt": 1790000000},
            "secondary": {"windowMinutes": 10080, "usedPercent": 10, "resetsAt": 1790500000}}}]
        bar = self.binary("codexbar", f"print({json.dumps(rows)!r})\n")
        result = self.collector(opencode_usage_helper=helper, opencode_api_key_getter=lambda: "secret",
                                codexbar_bin=bar).collect()["opencode"]
        self.assertIsNone(result.quota)
        self.assertIsNone(result.tier)
        self.assertFalse(result.fresh)
        self.assertEqual(result.error, "all tiers unavailable")

    def test_opencode_native_receives_key_on_stdin_not_argv(self):
        record = self.root / "key-record.json"
        helper = self.root / "opencode_helper.py"
        quota = dict(source="Native · opencode-go /usage", fresh=True, capturedAt=1788002498, **WINDOWS)
        helper.write_text(f"""import json, pathlib, sys
pathlib.Path({str(record)!r}).write_text(json.dumps({{'argv':sys.argv[1:], 'stdin':sys.stdin.read()}}))
print(json.dumps({quota!r}))
""")
        result = self.collector(opencode_usage_helper=helper,
                                opencode_api_key_getter=lambda: "sk-secret").collect()["opencode"]
        self.assertEqual(result.tier, Tier.NATIVE)
        self.assertTrue(result.fresh)
        seen = json.loads(record.read_text())
        self.assertEqual(seen["stdin"], "sk-secret\n")
        self.assertNotIn("sk-secret", " ".join(seen["argv"]))

    def test_clinepass_native_receives_key_on_stdin_not_argv(self):
        record = self.root / "clinepass-key-record.json"
        helper = self.root / "clinepass_helper.py"
        quota = dict(source="Native · clinepass /plan/usage-limits", fresh=True,
                     capturedAt=1788002498, **WINDOWS)
        helper.write_text(f"""import json, pathlib, sys
pathlib.Path({str(record)!r}).write_text(json.dumps({{'argv':sys.argv[1:], 'stdin':sys.stdin.read()}}))
print(json.dumps({quota!r}))
""")
        with mock.patch.dict(os.environ, {"CLINE_API_KEY": "cline-secret"}):
            result = self.collector(clinepass_usage_helper=helper).collect()["clinepass"]
        self.assertEqual(result.tier, Tier.NATIVE)
        self.assertTrue(result.fresh)
        seen = json.loads(record.read_text())
        self.assertEqual(seen["stdin"], "cline-secret\n")
        self.assertNotIn("cline-secret", " ".join(seen["argv"]))

    def test_factory_honors_all_probe_timeout_overrides(self):
        options = quota_probe_options({
            "QUOTA_SENTINEL_CODEXBAR_TIMEOUT": "1.5",
            "QUOTA_SENTINEL_ANTIGRAVITY_CODEXBAR_TIMEOUT": "2.5",
            "QUOTA_SENTINEL_OPENCODE_CODEXBAR_TIMEOUT": "3.5",
            "QUOTA_SENTINEL_CLINEPASS_CODEXBAR_TIMEOUT": "3.75",
            "QUOTA_SENTINEL_ANTIGRAVITY_NATIVE_TIMEOUT": "4.5",
            "QUOTA_SENTINEL_OPENCODE_NATIVE_TIMEOUT": "5.5",
            "QUOTA_SENTINEL_CLINEPASS_NATIVE_TIMEOUT": "5.75",
            "QUOTA_SENTINEL_CODEXBAR_KILL_GRACE": "0.5",
        })
        for name, expected in (
            ("codexbar_timeout", 1.5),
            ("antigravity_codexbar_timeout", 2.5),
            ("opencode_codexbar_timeout", 3.5),
            ("clinepass_codexbar_timeout", 3.75),
            ("antigravity_native_timeout", 4.5),
            ("opencode_native_timeout", 5.5),
            ("clinepass_native_timeout", 5.75),
            ("codexbar_kill_grace", 0.5),
        ):
            with self.subTest(name=name):
                self.assertEqual(options[name], expected)

    def test_detached_pipe_holder_cannot_extend_probe_deadline(self):
        code = (
            "import subprocess,sys,time; "
            "subprocess.Popen([sys.executable,'-c',"
            "'import time; time.sleep(1.5)'],start_new_session=True); "
            "time.sleep(5)"
        )
        started = time.monotonic()
        result = _run_bounded([sys.executable, "-c", code], timeout=0.1, grace=0.1)
        self.assertTrue(result.timed_out)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_timeout_reaps_a_stubborn_child_when_probe_exits_on_sigterm(self):
        body, pid_file = self.stubborn_descendant()
        probe = self.binary("probe", body + "time.sleep(30)\n")
        result = _run_bounded([str(probe)], timeout=1, grace=0.5)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.returncode, 124)
        self.assert_child_gone(pid_file)

    def test_timeout_falls_to_cache_and_reaps_codexbar_descendant(self):
        self.state.mkdir()
        (self.state / "codexbar-antigravity-last-success.json").write_text(json.dumps(dict(
            source="CodexBar · cached（可能不是最新）", fresh=False, cached=True,
            capturedAt=100, **WINDOWS)))
        pid_file = self.root / "child.pid"
        bar = self.binary("codexbar", f"""
import subprocess, sys, time
from pathlib import Path
if sys.argv[sys.argv.index('--provider') + 1] != 'antigravity': sys.exit(1)
p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
Path({str(pid_file)!r}).write_text(str(p.pid))
time.sleep(30)
""")
        result = self.collector(codexbar_bin=bar, antigravity_codexbar_timeout=0.5,
                                codexbar_kill_grace=0.1).collect()["antigravity"]
        self.assertEqual(result.tier, Tier.CODEXBAR_CACHE)
        self.assertFalse(result.fresh)
        self.assertEqual(result.quota.captured_at, 100)
        self.assertTrue(pid_file.exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)


class FactoryProbeBudgetAgreement(unittest.TestCase):
    """The collector's options and the derived probe bound are ONE table.

    They were two copies once: `factory.quota_probe_options` owned the eight
    names and defaults, while `task_orchestrator` bounded one probe phase with a
    fixed 300s and a comment claiming an operator's per-tier override was
    unknowable. Raising `QUOTA_SENTINEL_CODEXBAR_TIMEOUT` to 4000 then moved the
    collector's real budget while the outer `check` bound stayed put, which is
    how a legal probe gets killed by its own watchdog. These pins fail the day
    the two sides disagree again.
    """

    # One DISTINCT value per budget, so a name that silently moves from one
    # option to another cannot pass by coincidence.
    CRAFTED = {
        "QUOTA_SENTINEL_CODEXBAR_TIMEOUT": "1.5",
        "QUOTA_SENTINEL_ANTIGRAVITY_CODEXBAR_TIMEOUT": "2.5",
        "QUOTA_SENTINEL_OPENCODE_CODEXBAR_TIMEOUT": "3.5",
        "QUOTA_SENTINEL_CLINEPASS_CODEXBAR_TIMEOUT": "3.75",
        "QUOTA_SENTINEL_ANTIGRAVITY_NATIVE_TIMEOUT": "4.5",
        "QUOTA_SENTINEL_OPENCODE_NATIVE_TIMEOUT": "5.5",
        "QUOTA_SENTINEL_CLINEPASS_NATIVE_TIMEOUT": "5.75",
        "QUOTA_SENTINEL_CODEXBAR_KILL_GRACE": "0.5",
    }

    def test_pb1_every_timeout_is_the_same_value_on_both_sides(self):
        options = quota_probe_options(self.CRAFTED)
        timeouts = probe_budget.quota_probe_timeouts(self.CRAFTED)
        self.assertEqual(
            sorted(timeouts),
            sorted(spec.option for spec in probe_budget.PROBE_TIMEOUTS),
        )
        for spec in probe_budget.PROBE_TIMEOUTS:
            with self.subTest(option=spec.option):
                self.assertIn(spec.option, options)
                self.assertEqual(options[spec.option], timeouts[spec.option])
                # ...and the crafted value is the one the operator wrote, so a
                # renamed variable on either side cannot hide behind a default.
                self.assertEqual(
                    options[spec.option],
                    float(self.CRAFTED[spec.env]),
                    "%s no longer configures %s" % (spec.env, spec.option),
                )

    def test_pb2_every_operator_name_moves_both_sides(self):
        """The eight names are a contract: each moves options AND the bound."""
        baseline = probe_budget.worst_case_probe_phase_seconds({})
        for spec in probe_budget.PROBE_TIMEOUTS:
            raw = "%.5f" % (float(spec.default) + 3.25)
            env = {spec.env: raw}
            with self.subTest(variable=spec.env):
                self.assertEqual(quota_probe_options(env)[spec.option], float(raw))
                self.assertEqual(
                    probe_budget.quota_probe_timeouts(env)[spec.option], float(raw)
                )
                # A one-phase bound that ignores the raised budget is exactly
                # the defect these pins exist for.
                self.assertGreaterEqual(
                    probe_budget.worst_case_probe_phase_seconds(env) - baseline,
                    3.25,
                )

    def test_pb3_the_tolerance_is_the_shared_one(self):
        """`${VAR:-default}`: empty, junk, negative, non-finite → default."""
        raws = ("", "abc", "-5", "inf", "nan", "  ", "0", "12.5")
        crafted = {
            spec.env: raw
            for spec, raw in zip(probe_budget.PROBE_TIMEOUTS, raws)
        }
        expected = (
            ("codexbar_timeout", 20),                # empty → default
            ("antigravity_codexbar_timeout", 35),    # junk → default
            ("opencode_codexbar_timeout", 20),       # negative → default
            ("clinepass_codexbar_timeout", 20),      # +inf → default
            ("antigravity_native_timeout", 20),      # nan → default
            ("opencode_native_timeout", 15),         # whitespace → default
            ("clinepass_native_timeout", 15),        # zero: NOT allowed here
            ("codexbar_kill_grace", 12.5),           # the one accepted value
        )
        # The zip above is positional; keep the two orders locked together.
        self.assertEqual(
            [option for option, _ in expected],
            [spec.option for spec in probe_budget.PROBE_TIMEOUTS],
        )
        options = quota_probe_options(crafted)
        timeouts = probe_budget.quota_probe_timeouts(crafted)
        for option, value in expected:
            with self.subTest(option=option):
                self.assertEqual(options[option], value)
                self.assertEqual(timeouts[option], value)
        # Zero is a real value for the kill grace alone, on both sides.
        zero = {"QUOTA_SENTINEL_CODEXBAR_KILL_GRACE": "0"}
        self.assertEqual(quota_probe_options(zero)["codexbar_kill_grace"], 0.0)
        self.assertEqual(
            probe_budget.quota_probe_timeouts(zero)["codexbar_kill_grace"], 0.0
        )
        # The composition root does not own a second table: building the
        # options asks `probe_budget` for all eight budgets in one call, and its
        # own single-budget reader delegates to the shared parser.
        with mock.patch.object(
            probe_budget, "quota_probe_timeouts",
            wraps=probe_budget.quota_probe_timeouts,
        ) as shared:
            quota_probe_options(crafted)
        self.assertEqual(shared.call_count, 1)
        with mock.patch.object(
            probe_budget, "seconds_override", wraps=probe_budget.seconds_override
        ) as parser:
            self.assertEqual(
                factory_module._seconds_override({"X": "2.5"}, "X", 1.0), 2.5
            )
        self.assertEqual(parser.call_count, 1)


class InstrumentationTests(unittest.TestCase):
    """Per-tier outcome, elapsed time and the selected tier reach the log.

    Without these lines a native tier that hangs until its timeout and one
    that fails in 20ms look identical from outside: the run log shows only
    the check's total duration, and the tier that actually served the
    reading is invisible. The lines must stay sanitized — provider, tier,
    outcome, seconds — so nothing from the vendor CLI's output can ride
    along.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.work = self.root / "work"
        self.work.mkdir()
        self.missing = self.root / "missing"
        self.lines = []

    def collector(self, **overrides):
        args = dict(
            state_dir=self.state, workspace=self.work,
            codex_bin=self.missing, agy_bin=self.missing, uv_bin=self.missing,
            codexbar_bin=self.missing, opencode_usage_helper=self.missing,
            antigravity_usage_helper=self.missing, curl_bin=self.missing,
            opencode_api_key_getter=lambda: "",
            logger=self.lines.append,
        )
        args.update(overrides)
        return QuotaCollector(**args)

    def binary(self, name: str, body: str) -> Path:
        path = self.root / name
        path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
        path.chmod(0o700)
        return path

    def test_every_attempted_tier_and_the_selected_tier_are_logged(self):
        self.state.mkdir()
        (self.state / "codexbar-codex-last-success.json").write_text(
            json.dumps(dict(WINDOWS, source="CodexBar · cached", fresh=False,
                            cached=True, capturedAt=1790000000)),
            encoding="utf-8",
        )
        reading = self.collector().collect()["codex"]
        text = "\n".join(self.lines)

        # Every rung the ladder walked is named, with its outcome…
        for tier in ("native", "codexbar-live", "codexbar-cache"):
            self.assertIn("quota codex: %s" % tier, text)
        # …and every ATTEMPT line carries a duration.
        attempts = [
            line for line in self.lines
            if line.startswith("quota codex: ")
            and " selected " not in line and " raised " not in line
        ]
        self.assertGreaterEqual(len(attempts), 3, attempts)
        for line in attempts:
            self.assertRegex(line, r"\d+\.\d+s$", line)
        # The tier that actually served the reading is explicit.
        self.assertIn("quota codex: selected codexbar-cache", text)
        # And the choice itself is unchanged by the instrumentation.
        self.assertEqual(reading.tier, Tier.CODEXBAR_CACHE)
        self.assertFalse(reading.fresh)

    def test_a_native_success_is_logged_as_selected_native(self):
        codex = self.binary("codex", """
import json, sys
for line in sys.stdin:
    req = json.loads(line)
    if req['method'] == 'initialize':
        answer = {'jsonrpc': '2.0', 'id': req['id'], 'result': {}}
    else:
        answer = {'jsonrpc': '2.0', 'id': req['id'], 'result': {'rateLimits': {
            'primary': {'windowDurationMins': 300, 'usedPercent': 19, 'resetsAt': 1790000000},
            'secondary': {'windowDurationMins': 10080, 'usedPercent': 8, 'resetsAt': 1790500000}}}}
    print(json.dumps(answer), flush=True)
""")
        reading = self.collector(codex_bin=codex).collect()["codex"]
        codex_lines = [l for l in self.lines if l.startswith("quota codex: ")]
        text = "\n".join(codex_lines)
        self.assertIn("quota codex: native ok", text)
        self.assertIn("quota codex: selected native", text)
        # A fresh tier stops THIS provider's ladder: nothing below it runs.
        self.assertNotIn("codexbar-live", text)
        self.assertEqual(reading.tier, Tier.NATIVE)
        self.assertTrue(reading.fresh)

    def test_instrumentation_lines_carry_no_secret_and_no_paths(self):
        secret = "sk-must-never-be-logged-0123456789"
        helper = self.binary("antigravity_usage.py", """
import sys
print("antigravity_usage: command_timeout", file=sys.stderr)
print("token=%s" % sys.argv[0], file=sys.stderr)
print("Authorization: Bearer very-private-value", file=sys.stderr)
sys.exit(1)
""")
        fake_uv = self.binary("uv", """
import os, sys
args = sys.argv[1:]
os.execv(sys.executable, [sys.executable] + args[args.index('python') + 1:])
""")
        environment = dict(os.environ, OPENCODE_API_KEY=secret)
        collector = self.collector(
            antigravity_usage_helper=helper, agy_bin=self.binary("agy", "print('agy')"),
            uv_bin=fake_uv,
        )
        collector._base_environment = lambda: dict(environment)  # type: ignore[assignment]
        collector.collect()
        text = "\n".join(self.lines)
        for forbidden in (secret, "very-private-value", str(self.state),
                          str(self.work), str(Path.home())):
            self.assertNotIn(forbidden, text, text)
        # The reason code is still reported — that is the diagnostic value.
        self.assertIn("command_timeout", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
