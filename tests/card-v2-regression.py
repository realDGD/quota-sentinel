#!/usr/bin/env python3
import json
import os
import subprocess
import tempfile
import unittest

class TestFeishuCardV2AndCircularProgressChart(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.script_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../quota-sentinel.sh"))

    def tearDown(self):
        self.test_dir.cleanup()

    def run_zsh_fn(self, zsh_code: str) -> str:
        full_cmd = f'PI_SOURCE_ONLY=1 source "{self.script_path}" && {zsh_code}'
        res = subprocess.run(["zsh", "-c", full_cmd], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"ZSH command failed: {res.stderr}\nCode: {zsh_code}")
        return res.stdout.strip()

    # -------------------------------------------------------------
    # Case 1: Circular Progress Chart value mapping (5h=77, weekly=86)
    # -------------------------------------------------------------
    def test_case_1_chart_percentage_mapping(self):
        out = self.run_zsh_fn('build_circular_quota_chart 77 86')
        chart_obj = json.loads(out)
        self.assertEqual(chart_obj["tag"], "chart")
        self.assertEqual(chart_obj["height"], "140px")
        self.assertEqual(chart_obj["aspect_ratio"], "1:1")
        self.assertEqual(chart_obj["chart_spec"]["type"], "circularProgress")
        self.assertFalse(chart_obj["preview"])

        values = chart_obj["chart_spec"]["data"]["values"]
        self.assertEqual(len(values), 2)
        # Inner ring = Weekly (Index 0)
        self.assertEqual(values[0]["type"], "周额度")
        self.assertEqual(values[0]["value"], 0.86)
        # Outer ring = 5-Hour (Index 1)
        self.assertEqual(values[1]["type"], "5小时")
        self.assertEqual(values[1]["value"], 0.77)

    # -------------------------------------------------------------
    # Case 2: Boundary value mapping (0% and 100%)
    # -------------------------------------------------------------
    def test_case_2_boundary_mapping(self):
        out_0 = self.run_zsh_fn('build_circular_quota_chart 0 0')
        vals_0 = json.loads(out_0)["chart_spec"]["data"]["values"]
        self.assertEqual(vals_0[0]["value"], 0)
        self.assertEqual(vals_0[1]["value"], 0)

        out_100 = self.run_zsh_fn('build_circular_quota_chart 100 100')
        vals_100 = json.loads(out_100)["chart_spec"]["data"]["values"]
        self.assertEqual(vals_100[0]["value"], 1)
        self.assertEqual(vals_100[1]["value"], 1)

    # -------------------------------------------------------------
    # Case 3: Invalid values clamping and fallback
    # -------------------------------------------------------------
    def test_case_3_invalid_values_clamping_and_fallback(self):
        out_neg = self.run_zsh_fn('build_circular_quota_chart -10 -5')
        vals_neg = json.loads(out_neg)["chart_spec"]["data"]["values"]
        self.assertEqual(vals_neg[0]["value"], 0)
        self.assertEqual(vals_neg[1]["value"], 0)

        out_over = self.run_zsh_fn('build_circular_quota_chart 150 200')
        vals_over = json.loads(out_over)["chart_spec"]["data"]["values"]
        self.assertEqual(vals_over[0]["value"], 1)
        self.assertEqual(vals_over[1]["value"], 1)

        out_invalid = self.run_zsh_fn('build_circular_quota_chart "" "abc"')
        vals_invalid = json.loads(out_invalid)["chart_spec"]["data"]["values"]
        self.assertEqual(vals_invalid[0]["value"], 1.0)
        self.assertEqual(vals_invalid[1]["value"], 1.0)

    # -------------------------------------------------------------
    # Case 4: Element decomposition (Title, Source, Status, Chart, 5h, Divider, Weekly)
    # -------------------------------------------------------------
    def test_case_4_provider_elements_structure(self):
        fixture_file = os.path.join(self.test_dir.name, "quota-test.json")
        with open(fixture_file, "w") as f:
            json.dump({
                "source": "Native · codex app-server",
                "fresh": True,
                "capturedAt": 1788000000,
                "fiveHour": {"remainingPercent": 100, "resetAt": 1788018000},
                "weekly": {"remainingPercent": 84, "resetAt": 1788600000}
            }, f)

        out = self.run_zsh_fn(f'build_provider_v2_elements "GPT-5.6 Luna" "发送成功" "{fixture_file}"')
        elems = json.loads(out)
        self.assertEqual(len(elems), 7)
        # Element 0: Title
        self.assertEqual(elems[0]["tag"], "markdown")
        self.assertEqual(elems[0]["content"], "**GPT-5.6 Luna**")
        # Element 1: Source
        self.assertEqual(elems[1]["tag"], "markdown")
        self.assertEqual(elems[1]["content"], "↳ 来源　Native · codex app-server")
        # Element 2: Status
        self.assertEqual(elems[2]["tag"], "markdown")
        self.assertEqual(elems[2]["content"], "🟢 **发送成功**")
        # Element 3: Chart
        self.assertEqual(elems[3]["tag"], "chart")
        self.assertEqual(elems[3]["chart_spec"]["type"], "circularProgress")
        # Element 4: 5h Details
        self.assertEqual(elems[4]["tag"], "markdown")
        self.assertIn("100%", elems[4]["content"])
        self.assertIn("距离重置：", elems[4]["content"])
        # Element 5: Official Divider
        self.assertEqual(elems[5]["tag"], "hr")
        # Element 6: Weekly Details
        self.assertEqual(elems[6]["tag"], "markdown")
        self.assertIn("84%", elems[6]["content"])
        self.assertIn("重置时间：", elems[6]["content"])

    # -------------------------------------------------------------
    # Case 5: Literal \n Prevention Check
    # -------------------------------------------------------------
    def test_case_5_no_literal_backslash_n_in_text(self):
        out = self.run_zsh_fn('card_preview both')
        payload = json.loads(out)
        card = json.loads(payload["content"])

        def check_text_nodes(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in ("content", "text") and isinstance(v, str):
                        self.assertNotIn(r"\n", v, f"Literal \\n found in field '{k}': {repr(v)}")
                    else:
                        check_text_nodes(v)
            elif isinstance(obj, list):
                for item in obj:
                    check_text_nodes(item)

        check_text_nodes(card)

    # -------------------------------------------------------------
    # Case 6 & 7: Single Provider Scope (no empty column)
    # -------------------------------------------------------------
    def test_case_6_codex_only_single_column(self):
        fixture_file = os.path.join(self.test_dir.name, "codex-quota.json")
        with open(fixture_file, "w") as f:
            json.dump({
                "source": "Native · codex app-server",
                "fresh": True,
                "capturedAt": 1788000000,
                "fiveHour": {"remainingPercent": 100, "resetAt": 1788018000},
                "weekly": {"remainingPercent": 84, "resetAt": 1788600000}
            }, f)

        out = self.run_zsh_fn(f'CODEX_QUOTA_NORMALIZED_FILE="{fixture_file}" CODEX_RUN_RESULT="发送成功" build_feishu_v2_task_payload "user1" "uuid1" codex')
        payload = json.loads(out)
        card = json.loads(payload["content"])
        self.assertEqual(card["schema"], "2.0")
        tags = [e["tag"] for e in card["body"]["elements"]]
        self.assertNotIn("column_set", tags)
        self.assertEqual(card["header"]["template"], "green")
        self.assertIn("GPT-5.6 Luna", card["body"]["elements"][0]["content"])
        self.assertNotIn("Gemini", json.dumps(card))

    def test_case_7_antigravity_only_single_column(self):
        fixture_file = os.path.join(self.test_dir.name, "antigravity-quota.json")
        with open(fixture_file, "w") as f:
            json.dump({
                "source": "Native · agy local service",
                "fresh": True,
                "capturedAt": 1788000000,
                "fiveHour": {"remainingPercent": 77, "resetAt": 1788018000},
                "weekly": {"remainingPercent": 86, "resetAt": 1788600000}
            }, f)

        out = self.run_zsh_fn(f'ANTIGRAVITY_QUOTA_NORMALIZED_FILE="{fixture_file}" ANTIGRAVITY_RUN_RESULT="发送成功" build_feishu_v2_task_payload "user1" "uuid1" antigravity')
        payload = json.loads(out)
        card = json.loads(payload["content"])
        self.assertEqual(card["schema"], "2.0")
        tags = [e["tag"] for e in card["body"]["elements"]]
        self.assertNotIn("column_set", tags)
        self.assertIn("Gemini 3.7 Flash · Low", card["body"]["elements"][0]["content"])
        self.assertNotIn("Luna", json.dumps(card))

    # -------------------------------------------------------------
    # Case 8: Dual Provider Responsive Columns
    # -------------------------------------------------------------
    def test_case_8_dual_provider_responsive_columns(self):
        f_codex = os.path.join(self.test_dir.name, "codex.json")
        f_anti = os.path.join(self.test_dir.name, "anti.json")
        with open(f_codex, "w") as f:
            json.dump({
                "source": "Native · codex app-server",
                "fresh": True,
                "capturedAt": 1788000000,
                "fiveHour": {"remainingPercent": 100, "resetAt": 1788018000},
                "weekly": {"remainingPercent": 84, "resetAt": 1788600000}
            }, f)
        with open(f_anti, "w") as f:
            json.dump({
                "source": "Native · agy local service",
                "fresh": True,
                "capturedAt": 1788000000,
                "fiveHour": {"remainingPercent": 77, "resetAt": 1788018000},
                "weekly": {"remainingPercent": 86, "resetAt": 1788600000}
            }, f)

        out = self.run_zsh_fn(f'CODEX_QUOTA_NORMALIZED_FILE="{f_codex}" ANTIGRAVITY_QUOTA_NORMALIZED_FILE="{f_anti}" CODEX_RUN_RESULT="发送成功" ANTIGRAVITY_RUN_RESULT="发送成功" build_feishu_v2_task_payload "user1" "uuid1" codex antigravity')
        payload = json.loads(out)
        card = json.loads(payload["content"])
        self.assertEqual(card["schema"], "2.0")
        col_set = card["body"]["elements"][0]
        self.assertEqual(col_set["tag"], "column_set")
        self.assertEqual(col_set["flex_mode"], "stretch")
        self.assertEqual(len(col_set["columns"]), 2)
        self.assertEqual(col_set["columns"][0]["width"], "weighted")
        self.assertEqual(col_set["columns"][0]["weight"], 1)
        self.assertEqual(col_set["columns"][1]["width"], "weighted")
        self.assertEqual(col_set["columns"][1]["weight"], 1)

    # -------------------------------------------------------------
    # Case 9: /usage Dual Provider & No Status Lines
    # -------------------------------------------------------------
    def test_case_9_usage_card_structure(self):
        f_codex = os.path.join(self.test_dir.name, "codex.json")
        f_anti = os.path.join(self.test_dir.name, "anti.json")
        with open(f_codex, "w") as f:
            json.dump({
                "source": "Native · codex app-server",
                "fresh": True,
                "capturedAt": 1788000000,
                "fiveHour": {"remainingPercent": 100, "resetAt": 1788018000},
                "weekly": {"remainingPercent": 84, "resetAt": 1788600000}
            }, f)
        with open(f_anti, "w") as f:
            json.dump({
                "source": "Native · agy local service",
                "fresh": True,
                "capturedAt": 1788000000,
                "fiveHour": {"remainingPercent": 77, "resetAt": 1788018000},
                "weekly": {"remainingPercent": 86, "resetAt": 1788600000}
            }, f)

        out = self.run_zsh_fn(f'CODEX_QUOTA_NORMALIZED_FILE="{f_codex}" ANTIGRAVITY_QUOTA_NORMALIZED_FILE="{f_anti}" build_feishu_v2_usage_payload "user1" "uuid1"')
        payload = json.loads(out)
        card = json.loads(payload["content"])
        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(card["body"]["elements"][0]["tag"], "markdown")
        self.assertIn("即时配额查询", card["body"]["elements"][0]["content"])
        card_str = json.dumps(card)
        self.assertNotIn("发送成功", card_str)
        self.assertNotIn("发送失败", card_str)

    # -------------------------------------------------------------
    # Case 10: Cached & Pi Snapshot Warning Preservation
    # -------------------------------------------------------------
    def test_case_10_stale_warning_preservation(self):
        f_cached = os.path.join(self.test_dir.name, "cached.json")
        f_snapshot = os.path.join(self.test_dir.name, "snapshot.json")
        with open(f_cached, "w") as f:
            json.dump({
                "source": "CodexBar · cached（可能不是最新）",
                "fresh": False,
                "cached": True,
                "capturedAt": 1788000000,
                "fiveHour": {"remainingPercent": 60, "resetAt": 1788018000},
                "weekly": {"remainingPercent": 70, "resetAt": 1788600000}
            }, f)
        with open(f_snapshot, "w") as f:
            json.dump({
                "source": "Pi 快照（可能不是最新）",
                "fresh": False,
                "cached": True,
                "capturedAt": 1788000000,
                "fiveHour": {"remainingPercent": 50, "resetAt": 1788018000},
                "weekly": {"remainingPercent": 60, "resetAt": 1788600000}
            }, f)

        out_c = self.run_zsh_fn(f'build_provider_v2_elements "Gemini 3.7 Flash · Low" "发送成功" "{f_cached}"')
        self.assertIn("CodexBar · cached（可能不是最新）", out_c)

        out_s = self.run_zsh_fn(f'build_provider_v2_elements "GPT-5.6 Luna" "发送成功" "{f_snapshot}"')
        self.assertIn("Pi 快照（可能不是最新）", out_s)

    # -------------------------------------------------------------
    # Case 11: Unicode quota_bar fallback preservation
    # -------------------------------------------------------------
    def test_case_11_unicode_fallback_preservation(self):
        out_bar = self.run_zsh_fn('quota_bar 77')
        self.assertEqual(out_bar, "■■■■■■■■□□")

    # -------------------------------------------------------------
    # Case 12: Full JSON Parseability
    # -------------------------------------------------------------
    def test_case_12_json_parseability(self):
        out = self.run_zsh_fn('card_preview both')
        payload = json.loads(out)
        self.assertIn("receive_id", payload)
        self.assertEqual(payload["msg_type"], "interactive")
        card = json.loads(payload["content"])
        self.assertEqual(card["schema"], "2.0")

if __name__ == "__main__":
    unittest.main()
