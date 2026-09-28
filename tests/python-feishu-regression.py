#!/usr/bin/env python3
"""Behavioral parity for Python Feishu cards and delivery (no live network)."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib import error

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quota_sentinel.runtime.cards import (
    progress_chart,
    render_busy_card,
    render_progress_test_card,
    render_task_card,
    render_task_text_card,
    render_usage_card,
)
from quota_sentinel.runtime import feishu as feishu_module
from quota_sentinel.runtime.feishu import FeishuClient, FeishuError, FeishuNotifier, FeishuTimeout, UrllibHttp, lookup_payload


NOW = 1788000000


def quota(source="Native · codex app-server", monthly=False):
    value = {
        "source": source,
        "fresh": True,
        "cached": False,
        "capturedAt": NOW,
        "fiveHour": {"remainingPercent": 77, "resetAt": NOW + 3600},
        "weekly": {"remainingPercent": 86, "resetAt": NOW + 86400},
    }
    if monthly:
        value["monthly"] = {"remainingPercent": 98, "resetAt": NOW + 2592000}
    return value


def card(payload):
    assert payload["msg_type"] == "interactive"
    assert isinstance(payload["content"], str)
    return json.loads(payload["content"])


class CardTests(unittest.TestCase):
    def test_accepts_quota_reading_object_from_probe(self):
        class Reading:
            document = quota()

        payload = render_task_card(["codex"], {"codex": "发送成功"}, {"codex": Reading()}, "u", "id", now=NOW)
        self.assertEqual(card(payload)["body"]["elements"][1]["content"], "Native · codex app-server")

    def test_single_card_has_horizontal_windows_and_exact_chart_spec(self):
        payload = render_task_card(
            ["codex"], {"codex": "发送成功"}, {"codex": quota()},
            "user-1", "uuid-1", now=NOW,
        )
        self.assertEqual((payload["receive_id"], payload["uuid"]), ("user-1", "uuid-1"))
        body = card(payload)
        self.assertEqual(body["schema"], "2.0")
        self.assertEqual(body["config"], {"width_mode": "default"})
        self.assertEqual(body["header"]["template"], "green")
        elements = body["body"]["elements"]
        self.assertEqual(len(elements), 4)
        self.assertIn("GPT-6 Luna", elements[0]["columns"][0]["elements"][0]["content"])
        self.assertEqual(elements[1]["content"], "Native · codex app-server")
        windows = elements[2]
        self.assertEqual(len(windows["columns"]), 2)
        self.assertEqual(windows["columns"][0]["elements"][-1]["tag"], "hr")
        chart5 = windows["columns"][0]["elements"][1]
        self.assertEqual(chart5["height"], "24px")
        self.assertEqual(chart5["chart_spec"]["cornerRadius"], 5)
        self.assertEqual(chart5["chart_spec"]["data"]["values"][0]["value"], 0.77)
        self.assertEqual(chart5["chart_spec"]["color"], ["#57D0FB"])
        self.assertNotIn("roundCap", chart5["chart_spec"])
        self.assertIn("Pi 自动任务", elements[3]["content"])

    def test_original_pair_uses_two_columns_and_failure_red(self):
        payload = render_task_card(
            ["codex", "antigravity"],
            {"codex": "发送成功", "antigravity": "发送失败"},
            {"codex": quota(), "antigravity": quota("Native · agy local service")},
            "u", "id", now=NOW,
        )
        body = card(payload)
        self.assertEqual(body["header"]["template"], "red")
        columns = body["body"]["elements"][0]["columns"]
        self.assertEqual(len(columns), 2)
        self.assertIn("🔴 **失败**", json.dumps(columns[1], ensure_ascii=False))

    def test_opencode_only_and_all_three_use_stacked_v2_with_monthly_once(self):
        readings = {"codex": quota(), "antigravity": quota(), "opencode": quota("Native · opencode-go /usage", monthly=True)}
        for providers in (["opencode"], ["codex", "antigravity", "opencode"]):
            with self.subTest(providers=providers):
                payload = render_task_card(providers, {p: "发送成功" for p in providers}, readings, "u", "id", now=NOW)
                body = card(payload)
                text = json.dumps(body["body"], ensure_ascii=False)
                self.assertEqual(text.count("本月度"), 1)
                self.assertEqual(
                    text.count("DeepSeek V4.1 Flash · OpenCode Go"), 1)
                self.assertEqual(len([e for e in body["body"]["elements"] if e.get("tag") == "column_set" and e.get("flex_mode") == "none"]), len(providers))

    def test_usage_covers_full_roster_without_status_and_handles_missing_quota(self):
        payload = render_usage_card(
            ["codex", "antigravity", "opencode"],
            {"codex": quota(), "opencode": quota(monthly=True)},
            "u", "id", now=NOW,
        )
        body = card(payload)
        text = json.dumps(body["body"], ensure_ascii=False)
        self.assertIn("即时配额查询", text)
        self.assertIn("Gemini 3.7 Flash · Low", text)
        self.assertIn("不可用", text)
        self.assertNotIn("🟢 **成功**", text)
        self.assertNotIn("🔴 **失败**", text)

    def test_busy_card_is_vague_v1_and_escapes_unicode(self):
        payload = render_busy_card("u", "busy-id")
        body = card(payload)
        self.assertEqual(body["config"], {"wide_screen_mode": True})
        self.assertEqual(body["elements"][0]["text"]["content"], "⏳ 配额正在刷新，请稍后再试")
        self.assertNotIn("lock", json.dumps(body))

    def test_cached_warning_and_progress_boundaries(self):
        payload = render_task_card(["codex"], {"codex": "发送成功"}, {"codex": quota("CodexBar · cached（可能不是最新）")}, "u", "id", now=NOW)
        source = card(payload)["body"]["elements"][1]["content"]
        self.assertEqual(source, "CodexBar · cached\n⚠️ 可能不是最新")
        for given, expected in [(-10, 0), (0, 0), (1, 0.01), (100, 1), (150, 1), ("bad", 1.0)]:
            with self.subTest(given=given):
                self.assertEqual(progress_chart(given)["chart_spec"]["data"]["values"][0]["value"], expected)
        values = [e["chart_spec"]["data"]["values"][0]["value"] for e in card(render_progress_test_card("u", "id"))["body"]["elements"] if e.get("tag") == "chart"]
        self.assertEqual(values, [0, 0.01, 0.5, 0.77, 0.99, 1])

    def test_chart_disabled_fallback_has_failure_text_and_quota_bar(self):
        payload = render_task_text_card(
            ["codex"], {"codex": "发送失败"}, {"codex": quota()},
            "u", "id", now=NOW,
        )
        body = card(payload)
        # The Shell V1 wrapper selects red only for literal "发送失败"; the
        # formatted fallback replaces that phrase with the status icon.
        self.assertEqual(body["header"]["template"], "green")
        text = body["elements"][0]["text"]["content"]
        self.assertIn("🔴 **失败**", text)
        self.assertIn("■■■■■■■■□□ 剩余 77%", text)
        self.assertNotIn("来源：", text)


class FakeCredentials:
    def __init__(self):
        self.saved = None

    def get(self, name):
        return {"app_id": "app-id", "app_secret": "secret!", "user_id": "u"}[name]

    def save_user_id(self, value):
        self.saved = value


class FakeHttp:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def post(self, url, body, headers, connect_timeout, total_timeout):
        self.calls.append((url, json.loads(body), dict(headers), connect_timeout, total_timeout))
        item = next(self.responses)
        if isinstance(item, Exception):
            raise item
        return item


class TransportTests(unittest.TestCase):
    def test_validate_ready_checks_all_credentials_without_network_or_secret_leak(self):
        class MissingCredentials:
            def __init__(self):
                self.seen = []

            def get(self, name):
                self.seen.append(name)
                return "" if name == "user_id" else "sensitive-value"

        credentials = MissingCredentials()
        http = FakeHttp([])
        notifier = FeishuNotifier(FeishuClient(credentials, http=http))
        with self.assertRaises(FeishuError) as caught:
            notifier.validate_ready()
        self.assertEqual(credentials.seen, ["app_id", "app_secret", "user_id"])
        self.assertEqual(http.calls, [])
        self.assertNotIn("sensitive-value", str(caught.exception))

    def test_send_acquires_token_and_posts_envelope(self):
        http = FakeHttp([{"code": 0, "tenant_access_token": "token-value"}, {"code": 0}])
        client = FeishuClient(FakeCredentials(), http=http)
        payload = render_busy_card("u", "id")
        client.send(payload)
        self.assertEqual(len(http.calls), 2)
        self.assertIn("tenant_access_token/internal", http.calls[0][0])
        self.assertEqual(http.calls[0][1], {"app_id": "app-id", "app_secret": "secret!"})
        self.assertIn("im/v1/messages?receive_id_type=user_id", http.calls[1][0])
        self.assertEqual(http.calls[1][2]["Authorization"], "Bearer token-value")
        self.assertEqual(http.calls[1][3:], (15, 45))
        self.assertEqual(http.calls[1][1]["uuid"], "id")

    def test_total_timeout_covers_response_headers(self):
        def slow_open(*args, **kwargs):
            time.sleep(0.3)
            raise TimeoutError("late headers")

        started = time.monotonic()
        with mock.patch.object(feishu_module.request, "urlopen", side_effect=slow_open):
            with self.assertRaises(FeishuTimeout):
                UrllibHttp().post("https://example.invalid", b"{}", {}, 15, 0.05)
        self.assertLess(time.monotonic() - started, 0.2)

    def test_urllib_worker_returns_a_normal_json_response(self):
        class Response:
            def __init__(self):
                self.chunks = iter((b'{"code":0}', b""))

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self, size):
                return next(self.chunks)

        with mock.patch.object(feishu_module.request, "urlopen", return_value=Response()):
            self.assertEqual(
                UrllibHttp().post("https://example.invalid", b"{}", {}, 15, 45),
                {"code": 0},
            )

    def test_urllib_worker_preserves_http_error_details(self):
        def rejected(*args, **kwargs):
            raise error.HTTPError(
                "https://example.invalid", 403, "Forbidden", {},
                io.BytesIO(b'{"code": 230001, "msg": "app not in chat"}'),
            )

        with mock.patch.object(feishu_module.request, "urlopen", side_effect=rejected):
            with self.assertRaises(error.HTTPError) as caught:
                UrllibHttp().post("https://example.invalid", b"{}", {}, 15, 45)
        try:
            self.assertIn("app not in chat", FeishuClient._http_error_detail(caught.exception))
        finally:
            caught.exception.close()

    def test_total_timeout_retries_a_message_send(self):
        http = FakeHttp([FeishuTimeout("budget exceeded"), {"code": 0}])
        client = FeishuClient(FakeCredentials(), http=http, sleep=lambda _: None)
        self.assertEqual(client._post("im/v1/messages", {}, retries=1), {"code": 0})
        self.assertEqual(len(http.calls), 2)

    def test_send_retries_transient_transport_only(self):
        http = FakeHttp([{"code": 0, "tenant_access_token": "token"}, TimeoutError(), TimeoutError(), {"code": 0}])
        FeishuClient(FakeCredentials(), http=http, sleep=lambda _: None).send(render_busy_card("u", "id"))
        self.assertEqual(len(http.calls), 4)
        http = FakeHttp([{"code": 0, "tenant_access_token": "token"}, {"code": 999, "msg": "rejected"}])
        with self.assertRaisesRegex(FeishuError, "rejected"):
            FeishuClient(FakeCredentials(), http=http).send(render_busy_card("u", "id"))
        self.assertEqual(len(http.calls), 2)

    def test_lookup_payload_and_discovery_save_only_matched_user(self):
        self.assertEqual(lookup_payload("user@example.com"), {"emails": ["user@example.com"]})
        self.assertEqual(lookup_payload("+8613011111111"), {"mobiles": ["13011111111"]})
        self.assertEqual(lookup_payload("+85212345678"), {"mobiles": ["+85212345678"]})
        with self.assertRaises(ValueError):
            lookup_payload("bad identifier")
        creds = FakeCredentials()
        http = FakeHttp([{"code": 0, "tenant_access_token": "token"}, {"code": 0, "data": {"user_list": [{"user_id": "matched"}]}}])
        self.assertEqual(FeishuClient(creds, http=http).discover_user("user@example.com"), "matched")
        self.assertEqual(creds.saved, "matched")

    def test_notifier_uses_full_roster_and_one_card_per_app_event(self):
        class CapturingClient:
            credentials = FakeCredentials()

            def __init__(self):
                self.sent = []

            def send(self, payload):
                self.sent.append(payload)

        client = CapturingClient()
        notifier = FeishuNotifier(client, clock=lambda: NOW)
        notifier.task(["opencode"], {"opencode": "发送成功"}, {"opencode": quota(monthly=True)}, NOW)
        notifier.usage({"codex": quota()}, NOW)
        notifier.busy(NOW)
        self.assertEqual(len(client.sent), 3)
        self.assertEqual(client.sent[0]["receive_id"], "u")
        self.assertIn("DeepSeek V4.1 Flash · OpenCode Go", client.sent[0]["content"])
        usage_text = json.dumps(card(client.sent[1]), ensure_ascii=False)
        self.assertIn("GPT-6 Luna", usage_text)
        self.assertIn("Gemini 3.7 Flash · Low", usage_text)
        self.assertIn("DeepSeek V4.1 Flash · OpenCode Go", usage_text)
        self.assertIn("配额正在刷新", client.sent[2]["content"])


class KeychainBoundTests(unittest.TestCase):
    """`security` is an external process: both directions are bounded.

    The read defaulted to "no timeout" and the write had no budget at all, so a
    wedged Keychain (an unanswered prompt, stuck IPC) could block the readiness
    gate, the direct key lookup and `discover-feishu-user` forever.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qs-keychain-")
        self.addCleanup(self.temp.cleanup)
        self.security = Path(self.temp.name) / "security"
        self.security.write_text(
            "#!%s\nimport time\nwhile True:\n    time.sleep(1)\n" % sys.executable
        )
        self.security.chmod(0o755)

    def test_a_wedged_read_is_bounded_without_an_explicit_budget(self):
        from quota_sentinel.runtime import keychain as keychain_module
        with mock.patch.object(keychain_module, "DEFAULT_READ_TIMEOUT_SECONDS", 1):
            started = time.monotonic()
            value = keychain_module.read(
                "quota-sentinel.test-service", security_bin=str(self.security)
            )
            elapsed = time.monotonic() - started
        # A timeout is "no credential", exactly like a missing item.
        self.assertEqual(value, "")
        self.assertLess(elapsed, 5.0)

    def test_a_wedged_write_is_a_refusal_not_a_wait(self):
        with mock.patch.object(feishu_module, "KEYCHAIN_WRITE_TIMEOUT_SECONDS", 1):
            store = feishu_module.KeychainCredentials(
                environment={}, security_bin=str(self.security)
            )
            started = time.monotonic()
            with self.assertRaises(FeishuError):
                store.save_user_id("ou_test")
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
