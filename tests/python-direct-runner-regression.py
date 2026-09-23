#!/usr/bin/env python3
"""Direct transport parity: exact requests, replies, snapshots and redaction.

Everything external is a fake curl, so these tests never touch the network and
never read a real Keychain. The credential arrives through an environment key
(the same override production supports), which also makes it provable that the
key never reaches an argument list.
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

from quota_sentinel.runtime.direct import (  # noqa: E402
    DIRECT_PROVIDERS, DirectProvider, DirectRunner,
)

PLAIN_COMPLETION = {
    "choices": [{"finish_reason": "stop",
                 "message": {"role": "assistant", "content": "1"}}],
    "usage": {"prompt_tokens": 15, "completion_tokens": 1, "total_tokens": 16,
              "completion_tokens_details": {"reasoning_tokens": 0},
              "cost": 0.00000285},
}
OPENCODE_USAGE = {"usage": {
    "rolling": {"status": "ok", "percent": 13,
                "resetsAt": "2026-09-23T14:09:42.587Z"},
    "weekly": {"status": "ok", "percent": 50,
               "resetsAt": "2026-09-30T09:09:42Z"},
    "monthly": {"status": "ok", "percent": 65,
                "resetsAt": "2026-10-23T09:09:42Z"},
}}
CLINEPASS_USAGE = {"success": True, "data": {"limits": [
    {"type": "five_hour", "percentUsed": 9,
     "resetsAt": "2026-09-23T14:09:42.819795817Z"},
    {"type": "weekly", "percentUsed": 3,
     "resetsAt": "2026-09-30T09:09:42Z"},
    {"type": "monthly", "percentUsed": 1,
     "resetsAt": "2026-10-23T09:09:42Z"},
]}}

FAKE_CURL = """\
import json, os, sys
PLAIN = json.loads(__PLAIN__)
OPENCODE_USAGE = json.loads(__OPENCODE_USAGE__)
CLINEPASS_USAGE = json.loads(__CLINEPASS_USAGE__)
argv = sys.argv[1:]
stdin = sys.stdin.read()
url = argv[-1]
body = None
if "--data-binary" in argv:
    spec = argv[argv.index("--data-binary") + 1]
    if spec.startswith("@"):
        with open(spec[1:], encoding="utf-8") as handle:
            body = handle.read()
with open(os.environ["QS_CURL_LOG"], "a", encoding="utf-8") as out:
    out.write(json.dumps({"argv": argv, "stdin": stdin, "body": body, "url": url}) + "\\n")

mode = os.environ.get("QS_FAKE_CURL_MODE", "ok")
envelope = os.environ.get("QS_FAKE_CURL_PROVIDER") == "clinepass"
suffix = "\\nQSHTTPSTATUS:"

if mode == "timeout":
    import time
    time.sleep(30)

if not url.endswith("/chat/completions"):
    usage = CLINEPASS_USAGE if "cline.bot" in url else OPENCODE_USAGE
    sys.stdout.write(json.dumps(usage) + suffix + "200")
    sys.exit(0)


def with_content(text):
    document = json.loads(json.dumps(PLAIN))
    document["choices"][0]["message"]["content"] = text
    return document


MODES = {
    "ok": ("200", PLAIN),
    "bad_reply": ("200", with_content("1 extra")),
    "crlf": ("200", with_content("1\\r\\n")),
    "no_choices": ("200", {"usage": {"total_tokens": 3}}),
    "http_401": ("401", {"error": {"code": 401, "message": "invalid key"}}),
    "secret_error": ("500", {"error": {"code": 500,
        "message": "upstream said Authorization: Bearer very-private-token"}}),
}
if mode == "not_json":
    sys.stdout.write("<html>gateway</html>" + suffix + "200")
    sys.exit(0)
status, document = MODES.get(mode, MODES["ok"])
if envelope:
    payload = ({"success": False, "data": None} if mode == "envelope_false"
               else {"success": True, "data": document})
else:
    payload = document
sys.stdout.write(json.dumps(payload) + suffix + status)
"""

FAKE_CURL = (
    FAKE_CURL
    .replace("__PLAIN__", repr(json.dumps(PLAIN_COMPLETION)))
    .replace("__OPENCODE_USAGE__", repr(json.dumps(OPENCODE_USAGE)))
    .replace("__CLINEPASS_USAGE__", repr(json.dumps(CLINEPASS_USAGE)))
)


class DirectRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="quota-sentinel-direct-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "work"
        self.log = self.root / "curl.jsonl"
        self.fake_curl = self.root / "curl"
        self.fake_curl.write_text("#!" + sys.executable + "\n" + FAKE_CURL)
        self.fake_curl.chmod(0o755)
        self.saved = {}
        for name, value in {
            "QS_CURL_LOG": str(self.log),
            "QS_FAKE_CURL_MODE": "ok",
            "QS_FAKE_CURL_PROVIDER": "clinepass",
        }.items():
            self.saved[name] = os.environ.get(name)
            os.environ[name] = value

        def restore():
            for name, value in self.saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore)
        self.env = {
            "OPENCODE_API_KEY": "opencode-private-key",
            "CLINE_API_KEY": "cline-private-key",
        }
        self.logs: list = []

    def runner(self, **kwargs) -> DirectRunner:
        options = dict(
            curl_bin=self.fake_curl,
            timeout=5,
            logger=self.logs.append,
            environment=self.env,
        )
        options.update(kwargs)
        return DirectRunner(**options)

    def calls(self) -> list:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def run_provider(self, provider: str, mode: str = "ok", **kwargs):
        os.environ["QS_FAKE_CURL_MODE"] = mode
        os.environ["QS_FAKE_CURL_PROVIDER"] = provider
        runner = self.runner(**kwargs)
        paths = runner.prepare(provider, self.workspace)
        result = runner.run(provider, self.workspace, "initial", 1, 3)
        return result, paths

    # -- prepare ---------------------------------------------------------
    def test_prepare_publishes_the_shared_workspace_shape(self) -> None:
        paths = self.runner().prepare("opencode", self.workspace)
        self.assertEqual(paths.quota_path, self.workspace / "opencode-quota.json")
        self.assertEqual(paths.stdout_path, self.workspace / "opencode-stdout")
        self.assertEqual(paths.stderr_path, self.workspace / "opencode-stderr")
        self.assertEqual(paths.agent_dir.stat().st_mode & 0o777, 0o700)

    def test_unknown_provider_is_a_programming_error(self) -> None:
        with self.assertRaises(ValueError):
            self.runner().prepare("nope", self.workspace)

    # -- request shape ---------------------------------------------------
    def test_exact_request_for_opencode(self) -> None:
        result, _ = self.run_provider("opencode")
        self.assertTrue(result.success)
        self.assertEqual(result.stdout_path.read_bytes(), b"1\n")
        chat = [call for call in self.calls() if call["url"].endswith("/chat/completions")]
        call = chat[0]
        self.assertEqual(call["url"], DIRECT_PROVIDERS["opencode"].endpoint)
        self.assertIn("--user-agent", call["argv"])
        self.assertEqual(call["argv"][call["argv"].index("--user-agent") + 1],
                         "quota-sentinel/1.0")
        self.assertIn('header = "x-opencode-session: quota-sentinel-opencode-main"',
                      call["stdin"])
        body = json.loads(call["body"])
        self.assertEqual(body["model"], "deepseek-v4.1-flash")
        self.assertEqual(body["stream"], False)
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(body["messages"][0]["content"], "忽略上下文")
        self.assertEqual(body["messages"][1]["content"], "不用思考，只回复我 1")

    def test_clinepass_omits_the_session_header_and_keeps_the_envelope(self) -> None:
        result, _ = self.run_provider("clinepass")
        self.assertTrue(result.success)
        chat = [call for call in self.calls() if call["url"].endswith("/chat/completions")]
        self.assertNotIn("x-opencode-session", chat[0]["stdin"])
        self.assertIn('header = "X-Title: quota-sentinel"', chat[0]["stdin"])
        self.assertEqual(json.loads(chat[0]["body"])["model"],
                         "cline-pass/deepseek-v4.1-flash")

    def test_key_never_reaches_an_argument_list(self) -> None:
        self.run_provider("opencode")
        for call in self.calls():
            for argument in call["argv"]:
                self.assertNotIn("opencode-private-key", argument)
            self.assertIn("opencode-private-key", call["stdin"])

    def test_keychain_service_is_named_when_the_key_is_missing(self) -> None:
        runner = self.runner(key_reader=lambda provider: "")
        runner.prepare("opencode", self.workspace)
        result = runner.run("opencode", self.workspace, "initial", 1, 3)
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 3)
        self.assertIn("quota-sentinel.opencode-go-api-key", result.error_summary)
        self.assertEqual(self.calls(), [])

    # -- replies and failures --------------------------------------------
    def test_only_an_exact_one_succeeds(self) -> None:
        result, _ = self.run_provider("clinepass", mode="bad_reply")
        self.assertFalse(result.success)
        self.assertEqual(result.stdout_path.read_bytes(), b"1 extra\n")
        self.assertIn("not 1", result.error_summary)

    def test_crlf_is_not_a_success(self) -> None:
        # The Pi transport's contract: the shell's command substitution strips
        # trailing newlines only, so "1\r\n" is a failure there and here.
        result, _ = self.run_provider("clinepass", mode="crlf")
        self.assertFalse(result.success)

    def test_envelope_success_false_fails(self) -> None:
        result, _ = self.run_provider("clinepass", mode="envelope_false")
        self.assertFalse(result.success)
        self.assertIn("success=false", result.error_summary)

    def test_missing_choices_fail(self) -> None:
        result, _ = self.run_provider("clinepass", mode="no_choices")
        self.assertFalse(result.success)
        self.assertIn("no choices", result.error_summary)

    def test_http_error_reports_the_status(self) -> None:
        result, _ = self.run_provider("clinepass", mode="http_401")
        self.assertFalse(result.success)
        self.assertIn("http=401", result.error_summary)

    def test_non_json_body_fails_without_leaking_it(self) -> None:
        result, _ = self.run_provider("clinepass", mode="not_json")
        self.assertFalse(result.success)
        self.assertIn("not JSON", result.error_summary)
        self.assertNotIn("<html>", result.error_summary)

    def test_timeout_is_reported_as_a_timeout(self) -> None:
        result, _ = self.run_provider("clinepass", mode="timeout")
        self.assertFalse(result.success)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.exit_code, 124)

    def test_vendor_text_is_redacted_before_persisting(self) -> None:
        result, _ = self.run_provider("clinepass", mode="secret_error")
        self.assertFalse(result.success)
        self.assertNotIn("very-private-token", result.error_summary)
        self.assertNotIn("very-private-token", result.stderr_path.read_text())

    # -- snapshots --------------------------------------------------------
    def test_snapshot_is_written_in_the_capture_shape(self) -> None:
        result, paths = self.run_provider("opencode")
        self.assertTrue(result.success)
        document = json.loads(paths.quota_path.read_text())
        self.assertEqual(set(document), {"capturedAt", "fiveHour", "weekly", "monthly"})
        self.assertEqual(document["fiveHour"]["remainingPercent"], 87)
        self.assertEqual(document["weekly"]["remainingPercent"], 50)
        self.assertEqual(document["monthly"]["remainingPercent"], 35)
        # 2026-09-23T14:09:42Z, nanoseconds dropped.
        self.assertEqual(document["fiveHour"]["resetAt"], 1790172582)
        usage_calls = [call for call in self.calls() if not call["url"].endswith("/chat/completions")]
        self.assertEqual([call["url"] for call in usage_calls],
                         [DIRECT_PROVIDERS["opencode"].quota_url])

    def test_clinepass_snapshot_maps_limits_to_windows(self) -> None:
        result, paths = self.run_provider("clinepass")
        self.assertTrue(result.success)
        document = json.loads(paths.quota_path.read_text())
        self.assertEqual(document["fiveHour"]["remainingPercent"], 91)
        self.assertEqual(document["fiveHour"]["resetAt"], 1790172582)
        self.assertEqual(document["weekly"]["remainingPercent"], 97)
        self.assertEqual(document["monthly"]["remainingPercent"], 99)

    def test_snapshot_is_absent_when_the_attempt_failed(self) -> None:
        result, paths = self.run_provider("opencode", mode="bad_reply")
        self.assertFalse(result.success)
        self.assertFalse(paths.quota_path.exists())

    # -- logging ----------------------------------------------------------
    def test_attempt_lines_match_the_pi_transport_wording(self) -> None:
        self.run_provider("clinepass")
        lines = "\n".join(self.logs)
        self.assertIn("model clinepass attempt=1 usage prompt=15 completion=1 "
                      "total=16 reasoning=0 cost=0.00000285", lines)
        self.assertRegex(
            lines,
            r"model clinepass phase=initial attempt=1/3 result=success elapsed=\d+s",
        )

    def test_failure_logs_one_error_line(self) -> None:
        self.run_provider("clinepass", mode="http_401")
        lines = "\n".join(self.logs)
        self.assertIn("result=failed rc=1", lines)
        self.assertIn("model clinepass attempt=1 error:", lines)

    # -- data-only provider table ----------------------------------------
    def test_an_extra_provider_needs_no_code_change(self) -> None:
        custom = DirectProvider(
            provider="fakeplan",
            endpoint="https://example.invalid/chat/completions",
            model="model-x",
            quota_url="https://example.invalid/usage",
            key_service="quota-sentinel.fake-key",
            env_key="FAKE_KEY",
        )
        self.env["FAKE_KEY"] = "fake-private-key"
        os.environ["QS_FAKE_CURL_MODE"] = "ok"
        os.environ["QS_FAKE_CURL_PROVIDER"] = "opencode"
        runner = self.runner(providers={**DIRECT_PROVIDERS, "fakeplan": custom})
        runner.prepare("fakeplan", self.workspace)
        result = runner.run("fakeplan", self.workspace, "initial", 1, 1)
        self.assertTrue(result.success)
        self.assertEqual(
            [call["url"] for call in self.calls() if call["url"].endswith("/chat/completions")],
            [custom.endpoint],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
