"""Offline tests for the OpenCode Go native tier: response validation, the
stdin-only credential path, bounded children, and the shell tier wiring."""

import ast
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import opencode_usage as quota

# Real values observed from https://opencode.ai/zen/go/v1/usage: percent is the
# USED fraction, so remaining is its complement.
ROLLING_RESET_ISO = "2026-09-17T08:18:58.000Z"
ROLLING_RESET_EPOCH = 1789633138
WEEKLY_RESET_ISO = "2026-09-21T00:00:00.000Z"
WEEKLY_RESET_EPOCH = 1789948800
MONTHLY_RESET_ISO = "2026-10-17T03:04:30.000Z"
MONTHLY_RESET_EPOCH = 1792206270


def report():
    return {"usage": {
        "rolling": {"status": "ok", "percent": 12, "resetsAt": ROLLING_RESET_ISO},
        "weekly": {"status": "ok", "percent": 4, "resetsAt": WEEKLY_RESET_ISO},
        "monthly": {"status": "ok", "percent": 2, "resetsAt": MONTHLY_RESET_ISO},
    }}


class NativeQuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def script(self, body, name):
        path = self.directory / name
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o700)
        return path

    def curl(self, body=None, status="200", name="curl"):
        payload = json.dumps(report() if body is None else body)
        # Mirror the real invocation: body, then --write-out's "\n<status>".
        return self.script(
            "import json,pathlib,sys\n"
            "pathlib.Path(sys.argv[0] + '.argv').write_text(json.dumps(sys.argv[1:]))\n"
            "pathlib.Path(sys.argv[0] + '.stdin').write_text(sys.stdin.read())\n"
            f"sys.stdout.write({payload!r} + '\\n' + {status!r})\n",
            name,
        )

    # --- response validation -------------------------------------------------

    def test_used_percent_becomes_remaining_in_all_three_windows(self):
        result = quota.normalize_report(report(), 1789630000)
        self.assertEqual(result["source"], "Native · opencode-go /usage")
        self.assertTrue(result["fresh"])
        self.assertEqual(result["capturedAt"], 1789630000)
        self.assertEqual(result["fiveHour"], {"remainingPercent": 88, "resetAt": ROLLING_RESET_EPOCH})
        self.assertEqual(result["weekly"], {"remainingPercent": 96, "resetAt": WEEKLY_RESET_EPOCH})
        self.assertEqual(result["monthly"], {"remainingPercent": 98, "resetAt": MONTHLY_RESET_EPOCH})

    def test_monthly_is_optional_and_a_bad_one_is_dropped_not_fatal(self):
        data = report()
        del data["usage"]["monthly"]
        self.assertNotIn("monthly", quota.normalize_report(data, 1))
        for monthly in ({"status": "ok", "percent": None, "resetsAt": MONTHLY_RESET_ISO},
                        {"status": "ok", "percent": 2, "resetsAt": "garbage"},
                        "not-an-object"):
            data = report()
            data["usage"]["monthly"] = monthly
            with self.subTest(monthly=monthly):
                self.assertNotIn("monthly", quota.normalize_report(data, 1))

    def test_percent_endpoints_and_rate_limited_status(self):
        for percent, expected in ((0, 100), (100, 0)):
            data = report()
            for window in data["usage"].values():
                window["percent"] = percent
            self.assertEqual(quota.normalize_report(data, 1)["fiveHour"]["remainingPercent"], expected)
        data = report()
        data["usage"]["rolling"]["status"] = "rate-limited"
        self.assertEqual(quota.normalize_report(data, 1)["fiveHour"]["remainingPercent"], 88)

    def test_required_window_missing_or_malformed_rejected(self):
        for api_name in ("rolling", "weekly"):
            data = report()
            del data["usage"][api_name]
            with self.assertRaises(quota.QuotaError):
                quota.normalize_report(data, 1)
        variations = []
        for bad_status in ("", None, "unknown", 1):
            data = report(); data["usage"]["rolling"]["status"] = bad_status; variations.append(data)
        for bad_percent in (None, True, "12", -1, 101, float("nan"), float("inf")):
            data = report(); data["usage"]["rolling"]["percent"] = bad_percent; variations.append(data)
        for bad_reset in (None, "garbage", "2026-09-17T08:18:58", "1960-01-01T00:00:00Z", 0):
            data = report(); data["usage"]["weekly"]["resetsAt"] = bad_reset; variations.append(data)
        data = report(); variations.append({"usage": "nope"})
        data = report(); del data["usage"]["rolling"]["resetsAt"]; variations.append(data)
        for data in variations:
            with self.subTest(data=data):
                with self.assertRaises(quota.QuotaError):
                    quota.normalize_report(data, 1)

    # --- bounded subprocess handling ----------------------------------------

    def test_output_and_nonzero_exit_are_bounded(self):
        with self.assertRaisesRegex(quota.QuotaError, "output_too_large"):
            quota.run_bounded([sys.executable, "-c", "print('x'*1000)"], self.directory, 2, 100)
        with self.assertRaisesRegex(quota.QuotaError, "command_failed"):
            quota.run_bounded([sys.executable, "-c", "raise SystemExit(7)"], self.directory, 2, 100)

    def test_timeout_reaps_child_does_not_touch_external_process(self):
        pid_file = self.directory / "child.pid"
        body = ("import pathlib,subprocess,sys,time\n"
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
                "pathlib.Path(" + repr(str(pid_file)) + ").write_text(str(p.pid))\n"
                "time.sleep(60)\n")
        binary = self.script(body, "slow")
        external = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(quota.QuotaError, "command_timeout"):
                quota.run_bounded([str(binary)], self.directory, 0.5, 100)
            self.assertLess(time.monotonic() - started, 3)
            time.sleep(0.1)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)
            self.assertIsNone(external.poll())
        finally:
            external.terminate(); external.wait()

    # --- HTTP contract through the helper CLI --------------------------------

    def test_key_travels_on_stdin_into_a_curl_header_config(self):
        curl = self.curl()
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "opencode_usage.py"), "--curl", str(curl)],
            input="sk-secret-value\n", capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["fiveHour"]["remainingPercent"], 88)
        # The key reaches curl only as a stdin config header, never as an
        # argument: argv must not carry it.
        argv = json.loads(Path(str(curl) + ".argv").read_text())
        self.assertIn("--config", argv)
        self.assertNotIn("sk-secret-value", " ".join(argv))
        config = Path(str(curl) + ".stdin").read_text()
        self.assertIn('Authorization: Bearer sk-secret-value', config)

    def test_http_status_maps_to_fixed_reason_codes(self):
        for status, reason in (("401", "http_401"), ("403", "http_403"), ("500", "http_error")):
            curl = self.curl(body={"type": "error"}, status=status, name=f"curl{status}")
            with self.subTest(status=status):
                result = subprocess.run(
                    [sys.executable, "-B", str(ROOT / "opencode_usage.py"), "--curl", str(curl)],
                    input="sk-whatever\n", capture_output=True, text=True)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn(reason, result.stderr)

    def test_missing_key_and_missing_curl_fail_closed(self):
        curl = self.curl()
        for args, stdin, reason in ((["--curl", str(curl)], "\n", "auth_missing"),
                                    (["--curl", str(self.directory / "absent")], "sk-x\n", "curl_unavailable")):
            with self.subTest(reason=reason):
                result = subprocess.run(
                    [sys.executable, "-B", str(ROOT / "opencode_usage.py")] + args,
                    input=stdin, capture_output=True, text=True)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn(reason, result.stderr)

    def test_cli_logs_fixed_reason_not_response_body_secrets(self):
        curl = self.script("import sys\nsys.stdout.write('app_secret=TOPSECRET\\n401')\n", "curlLeaky")
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "opencode_usage.py"), "--curl", str(curl)],
            input="sk-real-key\n", capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("http_401", result.stderr)
        self.assertNotIn("TOPSECRET", result.stderr)
        self.assertNotIn("sk-real-key", result.stderr)

    def test_invalid_json_body_is_rejected(self):
        curl = self.script("import sys\nsys.stdout.write('not json\\n200')\n", "curlBadJson")
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "opencode_usage.py"), "--curl", str(curl)],
            input="sk-x\n", capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid_report", result.stderr)

    def test_cli_sigterm_cleans_up_owned_quota_process(self):
        marker = self.directory / "quota.pid"
        curl = self.script(
            "import os,pathlib,sys,time\n"
            "pathlib.Path(" + repr(str(marker)) + ").write_text(str(os.getpid()))\n"
            "time.sleep(60)\n", "curlSlow")
        helper = subprocess.Popen(
            [sys.executable, "-B", str(ROOT / "opencode_usage.py"), "--curl", str(curl)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            helper.stdin.write("sk-key\n")
            helper.stdin.flush()
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            helper.send_signal(signal.SIGTERM)
            out, err = helper.communicate(timeout=3)
            self.assertEqual(helper.returncode, 1)
            self.assertEqual(out, "")
            self.assertIn("command_cancelled", err)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(marker.read_text()), 0)
        finally:
            if helper.poll() is None:
                helper.terminate(); helper.wait(timeout=3)

    # --- shell tier wiring ----------------------------------------------------

    def shell(self, expression, env_extra, output):
        env = dict(os.environ, QUOTA_SENTINEL_STATE_DIR=str(self.directory / "state"),
                   QUOTA_SENTINEL_LOG_DIR=str(self.directory / "logs"), **env_extra)
        (self.directory / "state").mkdir(exist_ok=True)
        return subprocess.run(
            ["/bin/zsh", "-c", expression, "test", str(ROOT / "quota-sentinel.sh"), str(output)],
            env=env, capture_output=True, text=True)

    def test_shell_tier_passes_key_on_stdin_and_bounds_the_helper(self):
        capture = self.directory / "helper.json"
        helper = self.directory / "fake_helper.py"
        helper.write_text(
            "import json,pathlib,sys\n"
            "pathlib.Path(" + repr(str(capture)) + ").write_text(json.dumps("
            "{'argv': sys.argv[1:], 'stdin': sys.stdin.read()}))\n"
            "print(json.dumps({'source': 'Native · opencode-go /usage', 'fresh': True, 'capturedAt': 1,"
            " 'fiveHour': {'remainingPercent': 88, 'resetAt': 2}, 'weekly': {'remainingPercent': 96, 'resetAt': 3}}))\n")
        output = self.directory / "quota.json"
        result = self.shell(
            'PI_SOURCE_ONLY=1 source "$1"; fetch_native_opencode_quota "$2"',
            {"QUOTA_SENTINEL_OPENCODE_USAGE_HELPER": str(helper), "OPENCODE_API_KEY": "sk-from-env"},
            output)
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = json.loads(capture.read_text())
        self.assertEqual(seen["argv"], ["--curl", "/usr/bin/curl", "--timeout", "15"])
        self.assertEqual(seen["stdin"], "sk-from-env\n")
        self.assertTrue(json.loads(output.read_text())["fresh"])

    def test_shell_tier_degrades_without_key_and_logs_only_reason(self):
        output = self.directory / "quota.json"
        result = self.shell(
            'PI_SOURCE_ONLY=1 source "$1"; fetch_native_opencode_quota "$2" || print -r -- "rc=$?"',
            {"OPENCODE_API_KEY": "", "QUOTA_SENTINEL_OPENCODE_USAGE_HELPER": str(self.directory / "absent.py")},
            output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("rc=1", result.stdout)
        self.assertFalse(output.exists())

    def test_shell_codexbar_tier_normalizes_the_opencodego_payload(self):
        rows = [{"provider": "opencodego", "source": "api", "usage": {
            "primary": {"windowMinutes": 300, "usedPercent": 12, "resetsAt": ROLLING_RESET_ISO},
            "secondary": {"windowMinutes": 10080, "usedPercent": 4, "resetsAt": WEEKLY_RESET_ISO},
            "tertiary": {"windowMinutes": 43200, "usedPercent": 2, "resetsAt": MONTHLY_RESET_ISO}}}]
        # Any other provider's payload must not satisfy the opencodego selector.
        codex_rows = [{"provider": "codex", "source": "cli", "usage": {
            "primary": {"windowMinutes": 300, "usedPercent": 12, "resetsAt": ROLLING_RESET_ISO},
            "secondary": {"windowMinutes": 10080, "usedPercent": 4, "resetsAt": WEEKLY_RESET_ISO}}}]
        codexbar = self.script("import sys\nprint(" + repr(json.dumps(rows)) + ")\n", "codexbar")
        output = self.directory / "quota.json"
        result = self.shell(
            'PI_SOURCE_ONLY=1 source "$1"; ensure_temp_dir; fetch_codexbar_opencode_quota "$2"; cleanup',
            {"QUOTA_SENTINEL_CODEXBAR_BIN": str(codexbar)}, output)
        self.assertEqual(result.returncode, 0, result.stderr)
        normalized = json.loads(output.read_text())
        self.assertEqual(normalized["source"], "CodexBar · api")
        self.assertTrue(normalized["fresh"])
        self.assertEqual(normalized["fiveHour"]["remainingPercent"], 88)
        self.assertEqual(normalized["weekly"]["remainingPercent"], 96)
        self.assertEqual(normalized["monthly"]["remainingPercent"], 98)
        cache = json.loads((self.directory / "state" / "codexbar-opencode-last-success.json").read_text())
        self.assertFalse(cache["fresh"])

        wrong = self.script("import sys\nprint(" + repr(json.dumps(codex_rows)) + ")\n", "codexbarWrong")
        output2 = self.directory / "quota2.json"
        result = self.shell(
            'PI_SOURCE_ONLY=1 source "$1"; ensure_temp_dir; fetch_codexbar_opencode_quota "$2"; cleanup',
            {"QUOTA_SENTINEL_CODEXBAR_BIN": str(wrong)}, output2)
        self.assertNotEqual(result.returncode, 0)
        # jq creates the target before failing its selector, so the tier leaves
        # it empty rather than writing a usable quota file.
        self.assertEqual(output2.read_text() if output2.exists() else "", "")

    def test_outer_budget_covers_three_providers_and_delivery(self):
        tree = ast.parse((ROOT / "feishu_listener.py").read_text())
        bound = next(node.value.value for node in tree.body if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == "USAGE_COMMAND_TIMEOUT_SECONDS"
                             for target in node.targets))
        acquisition = 20 + 15 + 2 * (20 + 10) + 20 + 1 + 35 + 10 + 16 + (20 + 10)
        delivery = 45 + 3 * 45 + 3
        self.assertGreater(bound, acquisition + delivery)


if __name__ == "__main__":
    unittest.main()
