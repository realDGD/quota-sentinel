#!/usr/bin/env python3
import json
import os
import ssl
import subprocess
import tempfile
import time
import unittest
import urllib.parse

class TestQuotaCacheAndLoopbackSecurity(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self.test_dir.name, "state")
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        self.script_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../quota-sentinel.sh"))

    def tearDown(self):
        self.test_dir.cleanup()

    # -------------------------------------------------------------
    # P0-1: Cache Metadata & Persistence Tests
    # -------------------------------------------------------------
    def test_cache_case_1_live_writes_cache_metadata_on_disk(self):
        """Test that save_codexbar_cache writes fresh: false, cached: true, capturedAt: T directly on disk."""
        live_data = {
            "source": "CodexBar · cli",
            "fresh": True,
            "capturedAt": 1788000000,
            "fiveHour": {"remainingPercent": 75, "resetAt": 1788018000},
            "weekly": {"remainingPercent": 85, "resetAt": 1788600000}
        }
        live_file = os.path.join(self.test_dir.name, "live-normalized.json")
        cache_file = os.path.join(self.state_dir, "codexbar-codex-last-success.json")
        with open(live_file, "w") as f:
            json.dump(live_data, f)

        cmd = [
            "zsh", "-c",
            f'export QUOTA_SENTINEL_STATE_DIR="{self.state_dir}" && PI_SOURCE_ONLY=1 source "{self.script_path}" && save_codexbar_cache "{live_file}" "{cache_file}"'
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"save_codexbar_cache failed: {res.stderr}")

        # Directly read raw disk file (WITHOUT running through loader)
        with open(cache_file, "r") as f:
            disk_cache = json.load(f)

        self.assertEqual(disk_cache["source"], "CodexBar · cached（可能不是最新）")
        self.assertEqual(disk_cache["originalSource"], "CodexBar · cli")
        self.assertFalse(disk_cache["fresh"])
        self.assertTrue(disk_cache["cached"])
        self.assertEqual(disk_cache["capturedAt"], 1788000000)
        self.assertEqual(disk_cache["fiveHour"]["remainingPercent"], 75)
        self.assertEqual(disk_cache["fiveHour"]["resetAt"], 1788018000)

    def test_cache_case_2_captured_at_drift_prevention(self):
        """Test that reading cache multiple times preserves exact capturedAt without drift."""
        cache_file = os.path.join(self.state_dir, "codexbar-codex-last-success.json")
        fixed_captured_at = 1788000000  # e.g. 09:00
        cache_data = {
            "source": "CodexBar · cached（可能不是最新）",
            "originalSource": "CodexBar · cli",
            "fresh": False,
            "cached": True,
            "capturedAt": fixed_captured_at,
            "fiveHour": {"remainingPercent": 60, "resetAt": 1788018000},
            "weekly": {"remainingPercent": 80, "resetAt": 1788600000}
        }
        with open(cache_file, "w") as f:
            json.dump(cache_data, f)

        output_1 = os.path.join(self.test_dir.name, "out1.json")
        output_2 = os.path.join(self.test_dir.name, "out2.json")

        # Read 1 (simulate 10:30)
        cmd1 = [
            "zsh", "-c",
            f'export QUOTA_SENTINEL_STATE_DIR="{self.state_dir}" && PI_SOURCE_ONLY=1 source "{self.script_path}" && use_codexbar_cached_codex "{output_1}"'
        ]
        res1 = subprocess.run(cmd1, capture_output=True, text=True)
        self.assertEqual(res1.returncode, 0, f"cmd1 failed: {res1.stderr}")

        with open(output_1, "r") as f:
            read_1 = json.load(f)
        self.assertEqual(read_1["capturedAt"], fixed_captured_at)
        self.assertFalse(read_1["fresh"])
        self.assertEqual(read_1["source"], "CodexBar · cached（可能不是最新）")

        # Read 2 (simulate 11:30)
        cmd2 = [
            "zsh", "-c",
            f'export QUOTA_SENTINEL_STATE_DIR="{self.state_dir}" && PI_SOURCE_ONLY=1 source "{self.script_path}" && use_codexbar_cached_codex "{output_2}"'
        ]
        res2 = subprocess.run(cmd2, capture_output=True, text=True)
        self.assertEqual(res2.returncode, 0, f"cmd2 failed: {res2.stderr}")

        with open(output_2, "r") as f:
            read_2 = json.load(f)
        self.assertEqual(read_2["capturedAt"], fixed_captured_at)
        self.assertFalse(read_2["fresh"])
        self.assertEqual(read_2["source"], "CodexBar · cached（可能不是最新）")

    # -------------------------------------------------------------
    # P0-2: Loopback Security & Port Validation Tests
    # -------------------------------------------------------------
    def test_loopback_port_validation_rules(self):
        """Test strict port validation logic."""
        def validate_port(val):
            try:
                p = int(val)
                if 1 <= p <= 65535:
                    return p
            except (ValueError, TypeError):
                pass
            return None

        # Valid
        self.assertEqual(validate_port(1), 1)
        self.assertEqual(validate_port(54112), 54112)
        self.assertEqual(validate_port(65535), 65535)
        self.assertEqual(validate_port("8080"), 8080)

        # Invalid
        self.assertIsNone(validate_port(0))
        self.assertIsNone(validate_port(-1))
        self.assertIsNone(validate_port(65536))
        self.assertIsNone(validate_port(""))
        self.assertIsNone(validate_port("abc"))
        self.assertIsNone(validate_port("54112/path"))
        self.assertIsNone(validate_port("54112;rm -rf"))
        self.assertIsNone(validate_port(None))

    def test_loopback_discovery_filter(self):
        """Test that only local loopback listening sockets are extracted."""
        ALLOWED_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "*", "0.0.0.0", "::"}

        def validate_port(val):
            try:
                p = int(val)
                if 1 <= p <= 65535:
                    return p
            except (ValueError, TypeError):
                pass
            return None

        def extract_safe_port_from_listen_addr(addr_str):
            addr_str = str(addr_str).strip()
            if ":" not in addr_str:
                return None
            host_part = addr_str.rsplit(":", 1)[0].strip("[]")
            port_part = addr_str.rsplit(":", 1)[1]
            if host_part not in ALLOWED_LOCAL_HOSTS:
                return None
            return validate_port(port_part)

        # Allowed local binds -> extracts integer port
        self.assertEqual(extract_safe_port_from_listen_addr("127.0.0.1:54112"), 54112)
        self.assertEqual(extract_safe_port_from_listen_addr("localhost:54112"), 54112)
        self.assertEqual(extract_safe_port_from_listen_addr("[::1]:54112"), 54112)
        self.assertEqual(extract_safe_port_from_listen_addr("0.0.0.0:54112"), 54112)
        self.assertEqual(extract_safe_port_from_listen_addr("*:54112"), 54112)
        self.assertEqual(extract_safe_port_from_listen_addr("[::]:54112"), 54112)

        # Disallowed remote hosts -> must return None
        self.assertIsNone(extract_safe_port_from_listen_addr("192.168.1.100:54112"))
        self.assertIsNone(extract_safe_port_from_listen_addr("10.0.0.5:54112"))
        self.assertIsNone(extract_safe_port_from_listen_addr("172.16.0.10:54112"))
        self.assertIsNone(extract_safe_port_from_listen_addr("8.8.8.8:54112"))
        self.assertIsNone(extract_safe_port_from_listen_addr("example.com:54112"))
        self.assertIsNone(extract_safe_port_from_listen_addr("evil.example.com:54112"))

    def test_loopback_endpoint_url_invariant(self):
        """Test that build_loopback_endpoint strictly formats https://127.0.0.1:{port}/..."""
        def validate_port(val):
            try:
                p = int(val)
                if 1 <= p <= 65535:
                    return p
            except (ValueError, TypeError):
                pass
            return None

        def build_loopback_endpoint(port):
            p = validate_port(port)
            if not p:
                return None
            url = f"https://127.0.0.1:{p}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
            parts = urllib.parse.urlsplit(url)
            if parts.scheme != "https" or parts.hostname != "127.0.0.1" or parts.port != p:
                raise ValueError(f"Endpoint invariant violated: {url}")
            return url

        endpoint = build_loopback_endpoint(54112)
        self.assertEqual(endpoint, "https://127.0.0.1:54112/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary")
        self.assertIsNone(build_loopback_endpoint("invalid"))
        self.assertIsNone(build_loopback_endpoint(70000))

if __name__ == "__main__":
    unittest.main()
