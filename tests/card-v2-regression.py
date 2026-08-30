#!/usr/bin/env python3
import json
import os
import subprocess
import tempfile
import unittest

class TestFeishuCardV2RefinedLayout(unittest.TestCase):
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
    # Case 1 & 2: Progress Roundness & Value Mapping (0%, 1%, 50%, 77%, 99%, 100%)
    # -------------------------------------------------------------
    def test_case_1_and_2_progress_roundness_and_values(self):
        for pct, expected_val in [(0, 0), (1, 0.01), (50, 0.5), (77, 0.77), (99, 0.99), (100, 1)]:
            out = self.run_zsh_fn(f'build_linear_progress_chart {pct} "#57D0FB"')
            c = json.loads(out)
            self.assertEqual(c["tag"], "chart")
            self.assertEqual(c["height"], "24px")
            spec = c["chart_spec"]
            self.assertEqual(spec["type"], "linearProgress")
            self.assertTrue(spec.get("roundCap"))
            self.assertEqual(spec["progress"]["style"]["cornerRadius"], 5)
            self.assertEqual(spec["track"]["style"]["cornerRadius"], 5)
            self.assertEqual(spec["data"]["values"][0]["value"], expected_val)
            self.assertEqual(spec["color"], ["#57D0FB"])
            self.assertEqual(spec["progress"]["style"]["fill"], "#57D0FB")

    # -------------------------------------------------------------
    # Case 3: Weekly Progress Color (#54A6FD)
    # -------------------------------------------------------------
    def test_case_3_weekly_progress_color(self):
        out = self.run_zsh_fn('build_linear_progress_chart 86 "#54A6FD"')
        c = json.loads(out)
        self.assertEqual(c["chart_spec"]["color"], ["#54A6FD"])
        self.assertEqual(c["chart_spec"]["progress"]["style"]["fill"], "#54A6FD")
        self.assertEqual(c["chart_spec"]["data"]["values"][0]["value"], 0.86)

    # -------------------------------------------------------------
    # Case 4: Clamping and invalid fallback
    # -------------------------------------------------------------
    def test_case_4_clamping_and_fallback(self):
        out_neg = self.run_zsh_fn('build_linear_progress_chart -10 "#57D0FB"')
        self.assertEqual(json.loads(out_neg)["chart_spec"]["data"]["values"][0]["value"], 0)

        out_over = self.run_zsh_fn('build_linear_progress_chart 150 "#57D0FB"')
        self.assertEqual(json.loads(out_over)["chart_spec"]["data"]["values"][0]["value"], 1)

        out_invalid = self.run_zsh_fn('build_linear_progress_chart "invalid" "#57D0FB"')
        self.assertEqual(json.loads(out_invalid)["chart_spec"]["data"]["values"][0]["value"], 1.0)

    # -------------------------------------------------------------
    # Case 5: Single Provider Quota Horizontal Side-by-Side (5h | Weekly)
    # -------------------------------------------------------------
    def test_case_5_single_provider_horizontal_quotas(self):
        fixture_file = os.path.join(self.test_dir.name, "codex-quota.json")
        with open(fixture_file, "w") as f:
            json.dump({
                "source": "Native · codex app-server",
                "fresh": True,
                "capturedAt": 1788000000,
                "fiveHour": {"remainingPercent": 100, "resetAt": 1788018000},
                "weekly": {"remainingPercent": 84, "resetAt": 1788600000}
            }, f)

        out = self.run_zsh_fn(f'build_provider_v2_elements "GPT-5.6 Luna" "发送成功" "{fixture_file}" "single"')
        elems = json.loads(out)
        self.assertEqual(len(elems), 3)

        # Element 0: Header row (column_set with Model Name + Status)
        header_row = elems[0]
        self.assertEqual(header_row["tag"], "column_set")
        self.assertEqual(len(header_row["columns"]), 2)
        self.assertIn("GPT-5.6 Luna", header_row["columns"][0]["elements"][0]["content"])
        self.assertIn("🟢 **成功**", header_row["columns"][1]["elements"][0]["content"])

        # Element 1: Source (no "来源" keyword)
        source_elem = elems[1]
        self.assertEqual(source_elem["tag"], "markdown")
        self.assertEqual(source_elem["content"], "Native · codex app-server")

        # Element 2: Quota Column Set (5h and Weekly side-by-side)
        quota_col_set = elems[2]
        self.assertEqual(quota_col_set["tag"], "column_set")
        self.assertEqual(len(quota_col_set["columns"]), 2)
        # Left column: 5h
        left_col = quota_col_set["columns"][0]["elements"]
        self.assertIn("5 小时", left_col[0]["content"])
        self.assertEqual(left_col[1]["chart_spec"]["color"], ["#57D0FB"])
        self.assertIn("距离重置　", left_col[2]["content"])
        # Right column: Weekly
        right_col = quota_col_set["columns"][1]["elements"]
        self.assertIn("周额度", right_col[0]["content"])
        self.assertEqual(right_col[1]["chart_spec"]["color"], ["#54A6FD"])
        self.assertIn("重置时间　", right_col[2]["content"])

    # -------------------------------------------------------------
    # Case 6: Dual Provider Structure & Provider-End Divider
    # -------------------------------------------------------------
    def test_case_6_dual_provider_structure_and_divider(self):
        fixture_file = os.path.join(self.test_dir.name, "anti-quota.json")
        with open(fixture_file, "w") as f:
            json.dump({
                "source": "Native · agy local service",
                "fresh": True,
                "capturedAt": 1788000000,
                "fiveHour": {"remainingPercent": 77, "resetAt": 1788018000},
                "weekly": {"remainingPercent": 86, "resetAt": 1788600000}
            }, f)

        out = self.run_zsh_fn(f'build_provider_v2_elements "Gemini 3.7 Flash · Low" "发送成功" "{fixture_file}" "dual"')
        elems = json.loads(out)
        # Elements: Header row, Source, 5h, Chart, Reset, hr, Weekly, Chart, Reset, provider-end hr
        self.assertEqual(len(elems), 10)
        self.assertEqual(elems[0]["tag"], "column_set") # Header row
        self.assertEqual(elems[1]["content"], "Native · agy local service") # Source
        self.assertEqual(elems[5]["tag"], "hr") # 5h/Weekly divider
        self.assertEqual(elems[9]["tag"], "hr") # Provider-end divider

    # -------------------------------------------------------------
    # Case 7: Card width_mode is default
    # -------------------------------------------------------------
    def test_case_7_card_width_mode_is_default(self):
        out = self.run_zsh_fn('card_preview both')
        payload = json.loads(out)
        card = json.loads(payload["content"])
        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(card["config"]["width_mode"], "default")

    # -------------------------------------------------------------
    # Case 8: Absence of '↳' and '来源' in user-visible text
    # -------------------------------------------------------------
    def test_case_8_no_arrows_and_no_source_prefix(self):
        out = self.run_zsh_fn('card_preview both')
        payload = json.loads(out)
        card = json.loads(payload["content"])

        def check_text_nodes(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in ("content", "text") and isinstance(v, str):
                        self.assertNotIn("↳", v, f"Arrow '↳' found in '{k}': {repr(v)}")
                        self.assertNotIn("来源　", v, f"Source prefix '来源　' found in '{k}': {repr(v)}")
                        self.assertNotIn("来源：", v, f"Source prefix '来源：' found in '{k}': {repr(v)}")
                    else:
                        check_text_nodes(v)
            elif isinstance(obj, list):
                for item in obj:
                    check_text_nodes(item)

        check_text_nodes(card)

    # -------------------------------------------------------------
    # Case 9: Absence of literal \n in text
    # -------------------------------------------------------------
    def test_case_9_no_literal_backslash_n(self):
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
    # Case 10: /usage mode structure (no status)
    # -------------------------------------------------------------
    def test_case_10_usage_structure(self):
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
        self.assertNotIn("成功", card_str)
        self.assertNotIn("失败", card_str)

    # -------------------------------------------------------------
    # Case 11: Stale warning preservation
    # -------------------------------------------------------------
    def test_case_11_stale_warning_preservation(self):
        f_cached = os.path.join(self.test_dir.name, "cached.json")
        with open(f_cached, "w") as f:
            json.dump({
                "source": "CodexBar · cached（可能不是最新）",
                "fresh": False,
                "cached": True,
                "capturedAt": 1788000000,
                "fiveHour": {"remainingPercent": 60, "resetAt": 1788018000},
                "weekly": {"remainingPercent": 70, "resetAt": 1788600000}
            }, f)

        out_c = self.run_zsh_fn(f'build_provider_v2_elements "Gemini 3.7 Flash · Low" "发送成功" "{f_cached}" "single"')
        elems = json.loads(out_c)
        self.assertIn("CodexBar · cached", elems[1]["content"])
        self.assertIn("⚠️ 可能不是最新", elems[1]["content"])

    # -------------------------------------------------------------
    # Case 12: Unicode fallback preservation
    # -------------------------------------------------------------
    def test_case_12_unicode_fallback_preservation(self):
        out_bar = self.run_zsh_fn('quota_bar 77')
        self.assertEqual(out_bar, "■■■■■■■■□□")

if __name__ == "__main__":
    unittest.main()
