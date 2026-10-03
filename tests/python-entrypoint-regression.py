#!/usr/bin/env python3
"""Entrypoint regression: the Python CLI is the whole production surface.

Black-box only. Every case runs the real console script through uv against a
TEMP state directory with fake provider binaries, a dry-run notifier and no
network: no model quota is consumed and nothing is ever pushed to Feishu.

What is pinned here:
  E1  the public verb surface exists on the console script;
  E2  `status` is the readiness probe the shell's status() was;
  E3  a missing push credential fails `status` WITHOUT echoing its value;
  E4  `run` executes the real model path and commits state end to end;
  E5  `run` refuses while another run holds run.lock;
  E6  `check` with nothing due is quiet and successful;
  E7  `usage` renders the full roster and answers BUSY when the lock is held;
  E8  `card-preview` renders offline, with no credentials and no delivery;
  E9  the installed LaunchAgents invoke Python only — no shell anywhere;
  E10 the entrypoint works from launchd's foreign working directory, offline;
  E11 the runtime import graph stays stdlib-only on the system interpreter,
      which is what keeps `requires-python = ">=3.9"` honest;
  E12 a transport override the chosen transport cannot serve is an argument
      error (exit 3), refused before any state, lock or model work.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from quota_sentinel.state import bootstrap_legacy_authority
from quota_sentinel.platform.locks import initialize_protocol

PROVIDERS = ("codex", "antigravity", "opencode", "clinepass")
TEMPLATES = (
    "quota-sentinel.feishu-listener.plist.template",
    "quota-sentinel.plist.template",
    "quota-sentinel.timer.plist.template",
)


def find_uv() -> str:
    env = os.environ.get("QUOTA_SENTINEL_UV_BIN", "")
    if env and os.access(env, os.X_OK):
        return env
    for candidate in ("/opt/homebrew/bin/uv", shutil.which("uv")):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    raise unittest.SkipTest("uv not installed")


UV = find_uv()


class EntrypointCase(unittest.TestCase):
    """A ready deployment in a temp directory, with nothing real behind it."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="qs-entry-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.foreign_cwd = self.root / "foreign-cwd"
        self.foreign_cwd.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        initialize_protocol(self.state)
        bootstrap_legacy_authority(self.state)

        self.auth = self.root / "auth.json"
        self.auth.write_text(json.dumps({
            "openai-codex": {"token": "codex-secret"},
            "antigravity": {"token": "agy-secret"},
            "opencode-go": {"token": "opencode-secret"},
        }))
        self.auth.chmod(0o600)

        # All legacy fallback/readiness boundaries are fixtures too. A private
        # HOME must not rely on the operator's installed clients or auth files.
        self.codex_home = self.root / 'codex-home'; self.codex_home.mkdir()
        (self.codex_home / 'auth.json').write_text('{}')
        self.plugin = self.root / 'plugin.ts'; self.plugin.write_text('// fixture')
        self.metadata = self.root / 'metadata-unavailable'
        self.metadata.write_text('#!' + sys.executable + '\nraise SystemExit(1)\n')
        self.metadata.chmod(0o700)
        self.capture = self.root / "pi-calls.jsonl"
        self.pi = self.root / "pi"
        self.pi.write_text("#!" + sys.executable + "\n" + textwrap.dedent("""\
            import json, os, sys
            from pathlib import Path
            if sys.argv[1:3] == ['auth', 'print-bearer-token']:
                print('fake-bearer-token')
                sys.exit(0)
            quota_file = None
            for key in ('PI_CODEX_QUOTA_FILE', 'PI_ANTIGRAVITY_QUOTA_FILE',
                        'PI_OPENCODE_QUOTA_FILE'):
                if os.environ.get(key):
                    quota_file = os.environ[key]
            if quota_file:
                Path(quota_file).write_text(json.dumps({
                    'source': 'Pi 快照', 'fresh': True, 'capturedAt': 1790000000,
                    'fiveHour': {'remainingPercent': 70, 'resetAt': 1790099999},
                    'weekly': {'remainingPercent': 60, 'resetAt': 1790900000},
                }))
            Path(os.environ['QS_CAPTURE_PATH']).open('a').write(
                json.dumps(sys.argv[1:]) + '\\n')
            print('1')
            """))
        self.pi.chmod(0o700)

        self.env = dict(os.environ)
        self.env.update({
            "QUOTA_SENTINEL_STATE_DIR": str(self.state),
            # Keep the suite out of the checkout's real run log.
            "QUOTA_SENTINEL_LOG_DIR": str(self.root / "logs"),
            "QUOTA_SENTINEL_PI_BIN": str(self.pi),
            "QUOTA_SENTINEL_PI_AUTH_FILE": str(self.auth),
            "QUOTA_SENTINEL_CODEX_HOME": str(self.codex_home),
            "QUOTA_SENTINEL_CODEX_BIN": str(self.metadata),
            "QUOTA_SENTINEL_AGY_BIN": str(self.metadata),
            "QUOTA_SENTINEL_CODEXBAR_BIN": str(self.metadata),
            "QUOTA_SENTINEL_CURL_BIN": str(self.metadata),
            "QUOTA_SENTINEL_ANTIGRAVITY_PLUGIN": str(self.plugin),
            "QUOTA_SENTINEL_KEYCHAIN_DISABLED": "1",
            "OPENCODE_API_KEY": "fixture-opencode-key",
            "CLINE_API_KEY": "fixture-cline-key",
            # This suite exercises the Pi transport end to end, so it pins the
            # codex provider onto its fake Pi, regardless of an inherited
            # transport override. Driving `run codex` must never start a real
            # Codex CLI or spend a real token.
            "QUOTA_SENTINEL_TRANSPORT": "codex=pi",
            "QS_CAPTURE_PATH": str(self.capture),
            # Never push, and never touch the Keychain: the dry-run client
            # validates every credential and prints the payload it WOULD send.
            "FEISHU_DRY_RUN": "1",
            "FEISHU_APP_ID": "cli-app-id",
            "FEISHU_APP_SECRET": "cli-app-secret",
            "FEISHU_USER_ID": "cli-user-id",
            "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", str(self.root / "uv-cache")),
        })


    def cli(self, *args: str, env=None, cwd=None, offline: bool = True,
            timeout: int = 180) -> subprocess.CompletedProcess:
        environment = dict(self.env)
        if env:
            environment.update(env)
        if offline:
            environment["UV_OFFLINE"] = "1"
        return subprocess.run(
            [UV, "run", "--project", str(REPO), "--frozen", "--no-sync",
             "quota-sentinel", *args],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(cwd or REPO), env=environment,
        )

    def store(self):
        from quota_sentinel.state import FileStateStore
        return FileStateStore(self.state)

    # ---- E1: verb surface -------------------------------------------------
    def test_e1_public_verbs_are_on_the_console_script(self):
        result = self.cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for verb in ("check", "wait", "run", "usage", "status",
                     "card-preview", "send-test-card", "discover-feishu-user"):
            self.assertIn(verb, result.stdout, f"{verb} missing from --help")

    # ---- E2: readiness probe ---------------------------------------------
    def test_e2_status_reports_the_readiness_contract(self):
        result = self.cli("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "ready")
        self.assertIn("channel: feishu enterprise app", lines)
        self.assertTrue(any(l.startswith("quota primary: ") for l in lines), lines)
        self.assertTrue(any(l.startswith("opencode api key: ") for l in lines), lines)
        for provider in PROVIDERS:
            self.assertTrue(
                any(l.startswith(f"next {provider} run: ") for l in lines),
                f"no next-run line for {provider}: {lines}",
            )

    # ---- E3: refuse loudly, without leaking ------------------------------
    def test_e3_missing_credential_fails_without_echoing_the_secret(self):
        result = self.cli("status", env={
            "FEISHU_USER_ID": "",
            # Force the Keychain fallback to find nothing either.
            "QUOTA_SENTINEL_KEYCHAIN_DISABLED": "1",
            "HOME": str(self.root / "empty-home"),
        })
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("cli-app-secret", result.stdout + result.stderr)
        self.assertIn("user id", result.stderr)

    # ---- E4: the real run path -------------------------------------------
    def test_e4_run_executes_and_commits(self):
        result = self.cli("run", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.capture.read_text().strip().splitlines()
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("gpt-6-luna", calls[0])
        state = self.store().load("codex")
        self.assertFalse(state.retry_pending)
        self.assertIsNotNone(state.last_task_at)
        # The dry-run notifier prints the envelope it would have delivered.
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["receive_id"], "cli-user-id")
        self.assertIn("GPT-6 Luna", payload["content"])

    # ---- E5: one run at a time -------------------------------------------
    def test_e5_run_refuses_while_another_run_holds_the_lock(self):
        from quota_sentinel.state import acquire_run_lock
        with acquire_run_lock(self.state, timeout=0):
            result = self.cli("run", "codex")
        self.assertEqual(result.returncode, 1)
        self.assertIn("already in progress", result.stderr)
        self.assertFalse(self.capture.exists())

    # ---- E6: a quiet tick -------------------------------------------------
    def test_e6_check_with_future_deadlines_is_quiet_and_successful(self):
        from quota_sentinel.state import ProviderState
        store = self.store()
        for provider in PROVIDERS:
            store.commit(provider, ProviderState(), ProviderState(next_due_at=4000000000))
        result = self.cli("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")
        self.assertFalse(self.capture.exists())

    # ---- E7: /usage, including the busy answer ---------------------------
    def test_e7_usage_renders_the_roster_and_answers_busy(self):
        result = self.cli("usage")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        content = json.loads(payload["content"])
        text = json.dumps(content, ensure_ascii=False)
        for title in ("GPT-6 Luna", "Gemini 3.7 Flash · Low",
                      "DeepSeek V4.1 Flash · OpenCode Go",
                      "DeepSeek V4.1 Flash · ClinePass"):
            self.assertIn(title, text)

        from quota_sentinel.runtime.locks import acquire_quota_lock
        with acquire_quota_lock(self.state, timeout=0):
            busy = self.cli("usage")
        self.assertEqual(busy.returncode, 0, busy.stderr)
        busy_payload = json.loads(busy.stdout.strip().splitlines()[-1])
        self.assertIn("配额正在刷新", busy_payload["content"])

    # ---- E8: preview needs nothing real ----------------------------------
    def test_e8_card_preview_renders_offline_without_credentials(self):
        result = self.cli("card-preview", "all", env={
            "FEISHU_APP_ID": "", "FEISHU_APP_SECRET": "", "FEISHU_USER_ID": "",
            "HOME": str(self.root / "empty-home"),
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["receive_id"], "mock-user-id")
        self.assertEqual(payload["msg_type"], "interactive")
        self.assertIn("DeepSeek V4.1 Flash · OpenCode Go", payload["content"])

    # ---- E9: deployment invokes Python only ------------------------------
    def test_e9_launch_agent_templates_never_invoke_a_shell(self):
        for name in TEMPLATES:
            text = (REPO / name).read_text(encoding="utf-8")
            self.assertNotIn("quota-sentinel.sh", text, f"{name} still runs the shell")
            self.assertNotIn("/bin/zsh", text, f"{name} still runs zsh")
            self.assertIn("__REPO_DIR__", text, f"{name} lost its repo placeholder")
            self.assertIn("--frozen", text, f"{name} could re-resolve the lock")
            self.assertIn("--no-sync", text, f"{name} could mutate its own env")
        installer = (REPO / "install-launchagents.sh").read_text(encoding="utf-8")
        self.assertNotIn("quota-sentinel.sh", installer)
        # The installer may stay a shell setup helper, but the thing it
        # installs must be the Python entrypoint.
        self.assertIn("quota-sentinel", installer)

    def test_e9b_the_shell_implementation_is_gone(self):
        self.assertFalse(
            (REPO / "quota-sentinel.sh").exists(),
            "the retired shell implementation is still in the repository",
        )

    # ---- E10: launchd's working directory ---------------------------------
    def test_e10_starts_from_a_foreign_working_directory_offline(self):
        result = self.cli("status", cwd=self.foreign_cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], "ready")

    # ---- E11: the runtime graph stays class C -----------------------------
    def test_e11_runtime_graph_imports_on_the_system_interpreter(self):
        """`requires-python = ">=3.9"` must be true of every module.

        The runtime graph has to stay stdlib-only (the scheduler bridge runs
        on the system interpreter), and the daemons have to be importable on
        the declared floor: an evaluated `str | None` annotation is a
        TypeError before 3.10, so this is a real claim, not a formality.
        """
        stub = Path(tempfile.mkdtemp(prefix="qs-lark-stub-"))
        self.addCleanup(shutil.rmtree, stub, ignore_errors=True)
        (stub / "lark_oapi").mkdir()
        (stub / "lark_oapi" / "__init__.py").write_text("")
        program = (
            "import sys;"
            f"sys.path.insert(0, {str(REPO)!r});"
            "import quota_sentinel.app, quota_sentinel.runtime.factory,"
            " quota_sentinel.runtime.cards, quota_sentinel.runtime.feishu,"
            " quota_sentinel.runtime.models, quota_sentinel.runtime.quota_probe,"
            " quota_sentinel.runtime.runlog, feishu_listener, task_orchestrator;"
            "third = [m for m in sys.modules if m.split('.')[0] in"
            " ('requests', 'httpx', 'anyio', 'pydantic')];"
            "print('third=' + ','.join(third))"
        )
        result = subprocess.run(
            ["/usr/bin/python3", "-S", "-c", program],
            capture_output=True, text=True, timeout=60, cwd=self.foreign_cwd,
            env=dict(os.environ, PYTHONPATH=f"{REPO}:{stub}"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "third=",
                         "the runtime graph gained a third-party import")

    # ---- E12: a bad transport override is refused before any work ---------
    def test_e12_unservable_transport_override_is_refused_up_front(self):
        """`opencode=agy` must be an argument error, not a mid-run abort.

        The pair used to be accepted here and refused later by
        ``AgyExecRunner.prepare`` — after the operator had committed to the run
        — so every verb that builds the runtime has to hit the refusal first,
        report it as one operator-facing line and exit 3 without touching the
        state, the locks or a model.
        """
        for verb in (("status",), ("run", "opencode"), ("check",)):
            with self.subTest(verb=verb):
                result = self.cli(*verb, env={
                    "QUOTA_SENTINEL_TRANSPORT": "opencode=agy",
                })
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn("invalid argument", result.stderr)
                self.assertIn("'opencode'", result.stderr)
                self.assertIn("'agy'", result.stderr)
                self.assertIn("direct, pi", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.capture.exists(), "a model was started anyway")
        self.assertIsNone(self.store().load("opencode").last_task_at)

    def test_e12b_unknown_names_are_still_ignored(self):
        """The documented tolerance survives: unknown != unservable."""
        result = self.cli("status", env={
            "QUOTA_SENTINEL_TRANSPORT": "nosuch=agy,opencode=nosuch,=agy",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], "ready")

    def test_e12c_clinepass_pi_missing_plugin_refused_before_model(self):
        for verb in (("status",), ("run", "clinepass")):
            with self.subTest(verb=verb):
                result = self.cli(*verb, env={"QUOTA_SENTINEL_TRANSPORT": "clinepass=pi"})
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("pi-clinepass-provider", result.stderr + result.stdout)
                self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.capture.exists(), "a model was started anyway")
        self.assertIsNone(self.store().load("clinepass").last_task_at)


class TransportOverrideTest(unittest.TestCase):
    """DEFECT 2 at the configuration entry: no CLI, no state, no I/O.

    The capability sets are read from the channels themselves (the runners
    declare the provider they serve), so this pins both halves: an unservable
    pair is refused with an actionable message, and the pairs a channel declares
    for itself are accepted, while names that are simply unknown stay ignored.
    """

    def mapping(self, value: str):
        from quota_sentinel.runtime.factory import provider_transports
        return provider_transports({"QUOTA_SENTINEL_TRANSPORT": value})

    def test_default_transports_keep_the_shipped_priority(self):
        self.assertEqual(self.mapping(""), {
            "codex": "pi", "antigravity": "agy",
            "opencode": "direct", "clinepass": "direct",
        })

    def test_clinepass_pi_override_has_an_implemented_capability(self):
        self.assertEqual(self.mapping("clinepass=pi")["clinepass"], "pi")

    def test_unservable_pairs_are_refused_with_their_real_options(self):
        for value, provider, transport, supported in (
            ("opencode=agy", "opencode", "agy", "direct, pi"),
            ("codex=agy", "codex", "agy", "codex, pi"),
            ("antigravity=codex", "antigravity", "codex", "agy, pi"),
            ("clinepass=codex", "clinepass", "codex", "direct"),
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    self.mapping(value)
                message = str(caught.exception)
                self.assertIn(repr(provider), message)
                self.assertIn(repr(transport), message)
                self.assertIn(supported, message)

    def test_servable_pairs_are_accepted(self):
        for value, provider, transport in (
            ("opencode=direct", "opencode", "direct"),
            ("codex=codex", "codex", "codex"),
            ("antigravity=agy", "antigravity", "agy"),
            ("codex=pi", "codex", "pi"),
            ("antigravity=pi", "antigravity", "pi"),
            ("opencode=pi", "opencode", "pi"),
        ):
            with self.subTest(value=value):
                self.assertEqual(self.mapping(value)[provider], transport)

    def test_unknown_provider_and_unknown_transport_stay_ignored(self):
        from quota_sentinel.runtime.factory import provider_transports
        default = provider_transports({})
        for value in ("nosuch=agy", "opencode=nosuch", "opencode", "=agy", " "):
            with self.subTest(value=value):
                self.assertEqual(self.mapping(value), default)

    def test_the_capability_map_comes_from_the_channels_themselves(self):
        """A channel must be able to serve the provider it ships for.

        The map is built from `CODEX_PROVIDER`, `AGY_PROVIDER` and
        `DIRECT_PROVIDERS`; this is the guard that catches a new channel (or a
        renamed provider constant) that forgets to declare itself, which would
        otherwise make the shipped default look unservable.
        """
        from quota_sentinel.runtime.agy_exec import AGY_PROVIDER
        from quota_sentinel.runtime.codex_exec import CODEX_PROVIDER
        from quota_sentinel.runtime.direct import DIRECT_PROVIDERS
        from quota_sentinel.runtime.factory import (
            TRANSPORTS, TRANSPORT_PROVIDERS, supported_transports, transport_for,
        )

        self.assertEqual(sorted(TRANSPORT_PROVIDERS), sorted(TRANSPORTS))
        self.assertEqual(TRANSPORT_PROVIDERS["codex"], frozenset({CODEX_PROVIDER}))
        self.assertEqual(TRANSPORT_PROVIDERS["agy"], frozenset({AGY_PROVIDER}))
        self.assertEqual(TRANSPORT_PROVIDERS["direct"], frozenset(DIRECT_PROVIDERS))
        for provider in PROVIDERS:
            self.assertIn(transport_for(provider), supported_transports(provider))
        for provider in ("codex", "antigravity", "opencode"):
            self.assertIn("pi", supported_transports(provider))
        self.assertEqual(supported_transports("clinepass"), ["direct", "pi"])


if __name__ == "__main__":
    unittest.main()
