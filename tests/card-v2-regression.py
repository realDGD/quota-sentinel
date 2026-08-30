#!/usr/bin/env python3
import json
import os
import subprocess
import tempfile
import unittest

class TestFeishuCardV2AndLinearProgressChart(unittest.TestCase):
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
    # Case 1 & 2: Linear Progress Chart value mapping & colors
    # -------------------------------------------------------------
    def test_case_1_and_2_chart_linear_progress_and_colors(self):
        # 5-hour chart with #57D0FB
        out_5h = self.run_zsh_fn('build_linear_progress_chart 77 "#57D0FB"')
        chart_5h = json.loads(out_5h)
        self.assertEqual(chart_5h["tag"], "chart")
        self.assertEqual(chart_5h["height"], "26px")
        self.assertEqual(chart_5h["aspect_ratio"], "16:9")
        self.assertEqual(chart_5h["chart_spec"]["type"], "linearProgress")
        self.assertEqual(chart_5h["chart_spec"]["color"], ["#57D0FB"])
        self.assertEqual(chart_5h["chart_spec"]["progress"]["style"]["fill"], "#57D0FB")
        self.assertEqual(chart_5h["chart_spec"]["data"]["values"][0]["value"], 0.77)
        self.assertFalse(chart_5h["preview"])

        # Weekly chart with #54A6FD
        out_w = self.run_zsh_fn('build_linear_progress_chart 86 "#54A6FD"')
        chart_w = json.loads(out_w)
        self.assertEqual(chart_w["chart_spec"]["type"], "linearProgress")
        self.assertEqual(chart_w["chart_spec"]["color"], ["#54A6FD"])
        self.assertEqual(chart_w["chart_spec"]["progress"]["style"]["fill"], "#54A6FD")
        self.assertEqual(chart_w["chart_spec"]["data"]["values"][0]["value"], 0.86)

    # -------------------------------------------------------------
    # Case 3 & 4: Boundary values and clamping
    # -------------------------------------------------------------
    def test_case_3_and_4_boundary_and_clamping(self):
        out_0 = self.run_zsh_fn('build_linear_progress_chart 0 "#57D0FB"')
        self.assertEqual(json.loads(out_0)["chart_spec"]["data"]["values"][0]["value"], 0)

        out_100 = self.run_zsh_fn('build_linear_progress_chart 100 "#57D0FB"')
        self.assertEqual(json.loads(out_100)["chart_spec"]["data"]["values"][0]["value"], 1)

        out_neg = self.run_zsh_fn('build_linear_progress_chart -10 "#57D0FB"')
        self.assertEqual(json.loads(out_neg)["chart_spec"]["data"]["values"][0]["value"], 0)

        out_over = self.run_zsh_fn('build_linear_progress_chart 150 "#57D0FB"')
        self.assertEqual(json.loads(out_over)["chart_spec"]["data"]["values"][0]["value"], 1)

        out_invalid = self.run_zsh_fn('build_linear_progress_chart "" "#57D0FB"')
        self.assertEqual(json.loads(out_invalid)["chart_spec"]["data"]["values"][0]["value"], 1.0)

    # -------------------------------------------------------------
    # Case 5: Provider elements structure (2 charts per provider, divider)
    # -------------------------------------------------------------
    def test_case_5_provider_elements_structure(self):
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
        self.assertEqual(len(elems), 10)
        # Element 0: Title
        self.assertEqual(elems[0]["content"], "**GPT-5.6 Luna**")
        # Element 1: Source (no arrow)
        self.assertEqual(elems[1]["content"], "来源　Native · codex app-server")
        # Element 2: Status
        self.assertEqual(elems[2]["content"], "🟢 **发送成功**")
        # Element 3: 5h Title & Percent
        self.assertEqual(elems[3]["content"], "**5 小时**　剩余 100%")
        # Element 4: 5h Chart (#57D0FB)
        self.assertEqual(elems[4]["tag"], "chart")
        self.assertEqual(elems[4]["chart_spec"]["color"], ["#57D0FB"])
        # Element 5: 5h Reset Info (no arrow, space instead of colon)
        self.assertIn("距离重置　", elems[5]["content"])
        self.assertIn("重置时间　", elems[5]["content"])
        self.assertNotIn("↳", elems[5]["content"])
        # Element 6: Official Divider
        self.assertEqual(elems[6]["tag"], "hr")
        # Element 7: Weekly Title & Percent
        self.assertEqual(elems[7]["content"], "**周额度**　剩余 84%")
        # Element 8: Weekly Chart (#54A6FD)
        self.assertEqual(elems[8]["tag"], "chart")
        self.assertEqual(elems[8]["chart_spec"]["color"], ["#54A6FD"])
        # Element 9: Weekly Reset Info
        self.assertIn("距离重置　", elems[9]["content"])
        self.assertIn("重置时间　", elems[9]["content"])
        self.assertNotIn("↳", elems[9]["content"])

    # -------------------------------------------------------------
    # Case 6: Card width_mode is default
    # -------------------------------------------------------------
    def test_case_6_card_width_mode_is_default(self):
        out = self.run_zsh_fn('card_preview both')
        payload = json.loads(out)
        card = json.loads(payload["content"])
        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(card["config"]["width_mode"], "default")

    # -------------------------------------------------------------
    # Case 7: Absolute absence of '↳' in user-visible text
    # -------------------------------------------------------------
    def test_case_7_no_arrow_symbols(self):
        out = self.run_zsh_fn('card_preview both')
        payload = json.loads(out)
        card = json.loads(payload["content"])

        def check_no_arrows(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in ("content", "text") and isinstance(v, str):
                        self.assertNotIn("↳", v, f"Arrow '↳' found in '{k}': {repr(v)}")
                    else:
                        check_no_arrows(v)
            elif isinstance(obj, list):
                for item in obj:
                    check_no_arrows(item)

        check_no_arrows(card)

    # -------------------------------------------------------------
    # Case 8: Absence of literal \n in text
    # -------------------------------------------------------------
    def test_case_8_no_literal_backslash_n(self):
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
    # Case 9 & 10: Single Provider Scope (no empty column)
    # -------------------------------------------------------------
    def test_case_9_codex_only_single_column(self):
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

    def test_case_10_antigravity_only_single_column(self):
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
    # Case 11: Dual Provider Responsive Columns
    # -------------------------------------------------------------
    def test_case_11_dual_provider_responsive_columns(self):
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
    # Case 12: /usage Dual Provider & No Status Lines
    # -------------------------------------------------------------
    def test_case_12_usage_card_structure(self):
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
    # Case 13: Stale warning preservation
    # -------------------------------------------------------------
    def test_case_13_stale_warning_preservation(self):
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
        self.assertIn("来源　CodexBar · cached", out_c)
        self.assertIn("⚠️ 可能不是最新", out_c)

        out_s = self.run_zsh_fn(f'build_provider_v2_elements "GPT-5.6 Luna" "发送成功" "{f_snapshot}"')
        self.assertIn("来源　Pi 快照", out_s)
        self.assertIn("⚠️ 可能不是最新", out_s)

    # -------------------------------------------------------------
    # Case 14: Unicode quota_bar fallback preservation
    # -------------------------------------------------------------
    def test_case_14_unicode_fallback_preservation(self):
        out_bar = self.run_zsh_fn('quota_bar 77')
        self.assertEqual(out_bar, "■■■■■■■■□□")

if __name__ == "__main__":
    unittest.main()
