#!/usr/bin/env python3
"""Installer regression: the upgrade entry point's real behaviour.

`install-launchagents.sh` is the one shell script the port deliberately keeps
(it is a setup helper, not a runtime component), and it is also the script
that will touch a live deployment: it renders the LaunchAgents, refuses to
guess an owner, and starts the listener. The retired zsh suite tested that
behaviour by copying the installer into a fake repository; this is the same
approach in Python, with fake `uv` and `launchctl` so nothing real is
installed and no agent is ever loaded.

Pinned here:
  I1  render-only mode renders every agent and never calls launchctl;
  I2  a failed `uv sync --locked` gates the whole install;
  I3  a missing authority manifest refuses, before any render or load;
  I4  the manifest is validated, never created, never rewritten, never
      cut over;
  I5  --load retires the legacy schedulers BEFORE bootstrapping the listener;
  I6  rendered agents run the Python console script, never a shell;
  I7  an unknown argument is a usage error.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from quota_sentinel.state import bootstrap_legacy_authority, read_authority

INSTALLER = "install-launchagents.sh"
TEMPLATES = (
    "quota-sentinel.feishu-listener.plist.template",
    "quota-sentinel.plist.template",
    "quota-sentinel.timer.plist.template",
)
LABELS = ("quota-sentinel.feishu-listener", "quota-sentinel", "quota-sentinel.timer")
RETIRED = ("quota-sentinel", "quota-sentinel.timer")


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="qs-installer-")
        self.addCleanup(self.temp.cleanup)
        # macOS hands out /var/folders/... which is a symlink to /private/var:
        # the installer resolves its own path, so compare resolved paths.
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        (self.home / "Library").mkdir(parents=True)
        self.state = self.root / "state"
        self.state.mkdir()

        # A fake repository: the installer derives REPO_DIR from its own path,
        # so this is what keeps the suite away from the real checkout.
        self.repo = self.root / "repo"
        self.repo.mkdir()
        shutil.copy2(REPO / INSTALLER, self.repo / INSTALLER)
        for name in TEMPLATES:
            shutil.copy2(REPO / name, self.repo / name)
        # The authority check imports the package with PYTHONPATH=$REPO_DIR.
        (self.repo / "quota_sentinel").symlink_to(REPO / "quota_sentinel", target_is_directory=True)

        self.log = self.root / "calls.log"
        self.uv = self._binary("uv", """
import os, sys
Path = __import__('pathlib').Path
with Path(os.environ['QS_LOG']).open('a') as out:
    out.write('uv ' + ' '.join(sys.argv[1:]) + '\\n')
sys.exit(int(os.environ.get('QS_UV_RC', '0')))
""")
        self.launchctl = self._binary("launchctl", """
import os, sys
from pathlib import Path
with Path(os.environ['QS_LOG']).open('a') as out:
    out.write('launchctl ' + ' '.join(sys.argv[1:]) + '\\n')
sys.exit(int(os.environ.get('QS_LAUNCHCTL_RC', '0')))
""")

    def _binary(self, name: str, body: str) -> Path:
        path = self.root / name
        path.write_text("#!" + sys.executable + "\n" + body)
        path.chmod(0o700)
        return path

    def install(self, *args: str, uv_rc: int = 0, launchctl_rc: int = 0,
                manifest: bool = True):
        if manifest:
            bootstrap_legacy_authority(self.state)
        env = dict(os.environ)
        env.update({
            "HOME": str(self.home),
            "QS_LOG": str(self.log),
            "QS_UV_RC": str(uv_rc),
            "QS_LAUNCHCTL_RC": str(launchctl_rc),
            "QUOTA_SENTINEL_STATE_DIR": str(self.state),
            "QUOTA_SENTINEL_UV_BIN": str(self.uv),
            "QUOTA_SENTINEL_LAUNCHCTL_BIN": str(self.launchctl),
        })
        return subprocess.run(
            ["/bin/zsh", str(self.repo / INSTALLER), *args],
            capture_output=True, text=True, timeout=120, cwd=str(self.repo), env=env,
        )

    def calls(self) -> list:
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text().splitlines() if line]

    def rendered(self) -> list:
        return sorted(p.name for p in self.repo.glob("*.plist"))

    def agents(self) -> list:
        directory = self.home / "Library" / "LaunchAgents"
        if not directory.exists():
            return []
        return sorted(p.name for p in directory.glob("*.plist"))

    # ---- I1: render-only ------------------------------------------------
    def test_i1_render_only_renders_every_agent_and_never_calls_launchctl(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.rendered(), sorted(f"{label}.plist" for label in LABELS))
        self.assertEqual(self.agents(), sorted(f"{label}.plist" for label in LABELS))
        self.assertEqual([c for c in self.calls() if c.startswith("launchctl")], [])
        self.assertTrue(any(c.startswith("uv sync --locked") for c in self.calls()))

    # ---- I2: the environment gate ---------------------------------------
    def test_i2_a_failed_uv_sync_gates_the_whole_install(self):
        result = self.install("--load", uv_rc=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.rendered(), [])
        self.assertEqual(self.agents(), [])
        self.assertEqual(self.calls(), [f"uv sync --locked --project {self.repo}"])

    # ---- I3: unknown ownership refuses ----------------------------------
    def test_i3_a_missing_manifest_refuses_before_rendering_or_loading(self):
        result = self.install("--load", manifest=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bootstrap-authority", result.stderr)
        self.assertIn("UNKNOWN", result.stderr)
        self.assertEqual(self.rendered(), [])
        self.assertEqual(self.agents(), [])
        self.assertFalse(any(c.startswith("launchctl") for c in self.calls()))

    def test_i3b_the_installer_never_creates_the_manifest(self):
        self.install(manifest=False)
        self.assertFalse((self.state / "backend-authority.json").exists())

    # ---- I4: the manifest is read-only ----------------------------------
    def test_i4_an_existing_manifest_is_never_rewritten_or_cut_over(self):
        bootstrap_legacy_authority(self.state)
        path = self.state / "backend-authority.json"
        before = path.read_bytes()
        result = self.install("--load")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(read_authority(self.state).backend, "legacy")

    # ---- I5: retirement order -------------------------------------------
    def test_i5_load_retires_legacy_schedulers_before_bootstrapping(self):
        result = self.install("--load")
        self.assertEqual(result.returncode, 0, result.stderr)
        launchctl = [c for c in self.calls() if c.startswith("launchctl")]
        bootouts = [c for c in launchctl if " bootout " in c]
        bootstraps = [c for c in launchctl if " bootstrap " in c]
        for label in RETIRED:
            self.assertTrue(
                any(label in c for c in bootouts),
                f"{label} was not retired: {launchctl}",
            )
        self.assertEqual(len(bootstraps), 1, launchctl)
        self.assertIn("quota-sentinel.feishu-listener", bootstraps[0])
        # Retirement must come first: a second scheduler must never be alive
        # next to the new listener.
        self.assertLess(
            launchctl.index(bootouts[-1]), launchctl.index(bootstraps[0]),
        )

    # ---- I6: what actually gets installed -------------------------------
    def test_i6_rendered_agents_run_the_python_entrypoint(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in self.rendered():
            with (self.repo / name).open("rb") as handle:
                job = plistlib.load(handle)
            arguments = job["ProgramArguments"]
            joined = " ".join(arguments)
            self.assertNotIn("quota-sentinel.sh", joined, name)
            self.assertNotIn("/bin/zsh", joined, name)
            self.assertIn("--frozen", arguments, name)
            self.assertIn("--no-sync", arguments, name)
            self.assertIn(str(self.repo), arguments, name)
            self.assertEqual(job["WorkingDirectory"], "/private/tmp", name)
        listener_path = self.repo / "quota-sentinel.feishu-listener.plist"
        with listener_path.open("rb") as handle:
            listener = plistlib.load(handle)
        self.assertIn("feishu_listener.py", " ".join(listener["ProgramArguments"]))

    # ---- I7: usage ------------------------------------------------------
    def test_i7_an_unknown_argument_is_a_usage_error(self):
        result = self.install("--force")
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)
        self.assertEqual(self.rendered(), [])


if __name__ == "__main__":
    unittest.main()
