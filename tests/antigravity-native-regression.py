"""Offline tests: metadata authenticity, bounded children, shell tier wiring."""

import copy
import ast
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
import antigravity_usage as quota


def report():
    return {
        "status": "SUCCESS", "num_turns": 0,
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "command": {"name": "usage", "data": {"groups": [
            {"name": "Claude and GPT models", "buckets": []},
            {"name": "Gemini Models", "buckets": [
                {"window": "5h", "remaining_fraction": 0.755,
                 "reset_time": "2026-09-16T13:08:03Z"},
                {"window": "weekly", "remaining_fraction": 0.9,
                 "reset_time": "2026-09-23T08:08:03Z"},
            ]},
        ]}},
    }


class NativeQuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def binary(self, body, name="agy"):
        path = self.directory / name
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o700)
        return path

    def agy(self, version="1.2.4"):
        return self.binary(
            "import json,os,sys\n"
            f"if sys.argv[1:]==['--version']: print({version!r})\n"
            "else:\n"
            " assert sys.argv[1:]==['-p','/usage','--output-format','json']\n"
            " assert not os.listdir('.')\n"
            " assert os.stat('.').st_mode & 0o777 == 0o700\n"
            f" print({json.dumps(report())!r})\n"
        )

    def test_gemini_both_windows_fresh_absolute_reset_and_rounding(self):
        result = quota.normalize_report(report(), 1789530000)
        self.assertEqual(result["source"], "Native · agy /usage")
        self.assertTrue(result["fresh"])
        self.assertEqual(result["capturedAt"], 1789530000)
        self.assertEqual(result["fiveHour"]["remainingPercent"], 76)
        self.assertEqual(result["weekly"]["remainingPercent"], 90)
        self.assertEqual(result["fiveHour"]["resetAt"], 1789564083)
        self.assertEqual(quota.normalize_report(report(), 1789530900)["fiveHour"], result["fiveHour"])

    def test_fraction_endpoints_and_unknown_values(self):
        for fraction, expected in [(0, 0), (1, 100)]:
            data = report()
            data["command"]["data"]["groups"][1]["buckets"][0]["remaining_fraction"] = fraction
            self.assertEqual(quota.normalize_report(data, 1)["fiveHour"]["remainingPercent"], expected)
        for value in (None, True, "0.5", -0.1, 1.1, float("nan"), float("inf")):
            with self.subTest(value=value):
                data = report()
                data["command"]["data"]["groups"][1]["buckets"][0]["remaining_fraction"] = value
                with self.assertRaises(quota.QuotaError):
                    quota.normalize_report(data, 1)

    def test_missing_disabled_unknown_and_duplicate_windows_rejected(self):
        for mutation in ("missing", "enabled", "usage_known", "duplicate"):
            data = report()
            buckets = data["command"]["data"]["groups"][1]["buckets"]
            if mutation == "missing":
                buckets.pop()
            elif mutation == "duplicate":
                buckets.append(copy.deepcopy(buckets[0]))
            else:
                buckets[0][mutation] = False
            with self.assertRaises(quota.QuotaError):
                quota.normalize_report(data, 1)
        data = report()
        data["command"]["data"]["groups"][1]["enabled"] = False
        with self.assertRaises(quota.QuotaError):
            quota.normalize_report(data, 1)

    def test_reset_missing_invalid_or_timezone_naive_rejected(self):
        for reset in (None, "garbage", "2026-09-16T13:08:03", "1960-01-01T00:00:00Z"):
            data = report()
            data["command"]["data"]["groups"][1]["buckets"][0]["reset_time"] = reset
            with self.assertRaises(quota.QuotaError):
                quota.normalize_report(data, 1)

    def test_wrong_command_status_group_or_inference_rejected(self):
        variations = []
        for key, value in (("status", "ERROR"), ("num_turns", 1), ("num_turns", False), ("error", "secret")):
            data = report(); data[key] = value; variations.append(data)
        data = report(); data["command"]["name"] = "model"; variations.append(data)
        data = report(); data["command"]["data"]["groups"].pop(); variations.append(data)
        data = report(); data["command"]["data"]["groups"].append(copy.deepcopy(data["command"]["data"]["groups"][1])); variations.append(data)
        for field in ("input_tokens", "output_tokens", "total_tokens", "thinking_tokens", "cache_read_tokens"):
            data = report(); data["usage"][field] = 1; variations.append(data)
        data = report(); del data["usage"]["total_tokens"]; variations.append(data)
        for data in variations:
            with self.assertRaises(quota.QuotaError):
                quota.normalize_report(data, 1)

    def test_supported_binary_only_builtin_command_private_empty_cwd(self):
        self.assertTrue(quota.fetch_quota(self.agy(), 3)["fresh"])

    def test_old_version_never_invokes_print_or_model(self):
        marker = self.directory / "unexpected-call"
        binary = self.binary("import pathlib,sys\nif sys.argv[1:]==['--version']: print('1.1.10')\nelse: pathlib.Path(" + repr(str(marker)) + ").touch()\n")
        with self.assertRaisesRegex(quota.QuotaError, "unsupported_agy_version"):
            quota.fetch_quota(binary, 3)
        self.assertFalse(marker.exists())
        self.assertTrue(quota.version_supported(b"1.1.11\n"))
        self.assertFalse(quota.version_supported(b"unknown"))

    def test_output_and_nonzero_exit_are_bounded(self):
        with self.assertRaisesRegex(quota.QuotaError, "output_too_large"):
            quota.run_bounded([sys.executable, "-c", "print('x'*1000)"], self.directory, 2, 100)
        with self.assertRaisesRegex(quota.QuotaError, "command_failed"):
            quota.run_bounded([sys.executable, "-c", "raise SystemExit(7)"], self.directory, 2, 100)

    def test_timeout_reaps_child_does_not_touch_external_process(self):
        pid_file = self.directory / "child.pid"
        body = "import pathlib,subprocess,sys,time\np=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\npathlib.Path(" + repr(str(pid_file)) + ").write_text(str(p.pid))\ntime.sleep(60)\n"
        binary = self.binary(body)
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

    def test_cli_logs_fixed_reason_not_subprocess_secret(self):
        binary = self.binary("import sys\nif sys.argv[1:]==['--version']: print('1.2.4')\nelse: print('app_secret=TOPSECRET',file=sys.stderr); raise SystemExit(7)\n")
        result = subprocess.run([sys.executable, "-B", str(ROOT / "antigravity_usage.py"), "--agy", str(binary)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("command_failed", result.stderr)
        self.assertNotIn("TOPSECRET", result.stderr)

    def test_cli_sigterm_cleans_up_owned_quota_process(self):
        marker = self.directory / "quota.pid"
        binary = self.binary("import os,pathlib,sys,time\nif sys.argv[1:]==['--version']: print('1.2.4')\nelse:\n pathlib.Path(" + repr(str(marker)) + ").write_text(str(os.getpid()))\n time.sleep(60)\n")
        helper = subprocess.Popen([sys.executable, "-B", str(ROOT / "antigravity_usage.py"), "--agy", str(binary)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
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

    def test_default_outer_budget_includes_quota_kill_grace_and_delivery(self):
        tree = ast.parse((ROOT / "feishu_listener.py").read_text())
        bound = next(node.value.value for node in tree.body if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == "USAGE_COMMAND_TIMEOUT_SECONDS" for target in node.targets))
        acquisition = 20 + 15 + 2 * (20 + 10) + 20 + 1 + 35 + 10
        delivery = 45 + 3 * 45 + 3
        self.assertGreater(bound, acquisition + delivery)

    def test_shell_native_uses_uv_and_does_not_mutate_scheduler(self):
        uv = self.directory / "uv"
        uv.write_text(f"#!/bin/zsh\n[[ $1 == run && $2 == --offline && $3 == --no-project && $4 == --no-config && $5 == python ]] || exit 9\nshift 5\nexec {sys.executable} \"$@\"\n")
        uv.chmod(0o700)
        state = self.directory / "state"; state.mkdir()
        due = state / "antigravity-next-due-at"; due.write_text("1789565000\n")
        output = self.directory / "quota.json"
        env = dict(os.environ, QUOTA_SENTINEL_AGY_BIN=str(self.agy()), QUOTA_SENTINEL_UV_BIN=str(uv), QUOTA_SENTINEL_STATE_DIR=str(state), QUOTA_SENTINEL_LOG_DIR=str(self.directory / "logs"))
        result = subprocess.run(["/bin/zsh", "-c", 'PI_SOURCE_ONLY=1 source "$1"; fetch_native_antigravity_quota "$2"', "test", str(ROOT / "quota-sentinel.sh"), str(output)], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(output.read_text())["fresh"])
        self.assertEqual(due.read_text(), "1789565000\n")
        self.assertEqual(list(state.iterdir()), [due])

    def test_shell_antigravity_codexbar_uses_independent_budget(self):
        windows = [{"id": "gemini-" + cadence, "window": {"windowMinutes": minutes, "usedPercent": 20, "resetsAt": reset}}
                   for cadence, minutes, reset in (("5h", 300, "2026-09-16T13:08:03Z"), ("weekly", 10080, "2026-09-23T08:08:03Z"))]
        rows = [{"provider": "antigravity", "source": "cli", "usage": {"extraRateWindows": windows}}]
        codexbar = self.binary("import time\ntime.sleep(0.5)\nprint(" + repr(json.dumps(rows)) + ")\n", "codexbar")
        output = self.directory / "quota.json"
        env = dict(os.environ, QUOTA_SENTINEL_CODEXBAR_BIN=str(codexbar), QUOTA_SENTINEL_CODEXBAR_TIMEOUT="0.1", QUOTA_SENTINEL_ANTIGRAVITY_CODEXBAR_TIMEOUT="3", QUOTA_SENTINEL_STATE_DIR=str(self.directory / "state"), QUOTA_SENTINEL_LOG_DIR=str(self.directory / "logs"))
        result = subprocess.run(["/bin/zsh", "-c", 'PI_SOURCE_ONLY=1 source "$1"; ensure_temp_dir; fetch_codexbar_antigravity_quota "$2"; cleanup', "test", str(ROOT / "quota-sentinel.sh"), str(output)], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(output.read_text())["fresh"])
        cache = json.loads((self.directory / "state" / "codexbar-antigravity-last-success.json").read_text())
        self.assertFalse(cache["fresh"])


if __name__ == "__main__":
    unittest.main()
