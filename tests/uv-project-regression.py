#!/usr/bin/env python3
"""Phase 3A.5 environment guard: the repo IS a uv-managed project.

Pins the packaging/runtime contract from ARCHITECTURE.md ("Python runtime
/ dependency ownership") — behavior of scheduler/state code is NOT
touched here:

* UV1  pyproject.toml + uv.lock exist; PEP 723 retired everywhere;
* UV2  uv lock --check: pyproject and lock cannot have drifted;
* UV3  console-script entry (uv run quota-sentinel) works;
* UV4  console script vs python -m: identical stdout AND rc, for the
       help surface and for a real state read;
* UV5  the project environment really provides lark_oapi (no reliance on
       any globally installed copy);
* UV6  LaunchAgent-equivalent invocation from a FOREIGN cwd
       (/private/tmp, exactly launchd's WorkingDirectory):
         uv run --project <repo> --frozen --no-sync python <script>
       works and needs NO network (run under UV_OFFLINE=1) and cannot
       mutate the environment;
* UV7  the isolated antigravity boundary is still OUTSIDE the project:
       --no-project behavior unaffected by the root pyproject existing.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def find_uv() -> str | None:
    env = os.environ.get("QUOTA_SENTINEL_UV_BIN", "")
    if env and os.access(env, os.X_OK):
        return env
    for cand in ("/opt/homebrew/bin/uv", shutil.which("uv")):
        if cand and os.access(cand, os.X_OK):
            return cand
    return None


UV = find_uv()


def uv_run(*args: str, cwd: str, offline: bool = True, timeout: int = 120):
    env = dict(os.environ)
    if offline:
        env["UV_OFFLINE"] = "1"          # runtime must never need network
    return subprocess.run(
        [UV, "run", "--project", str(REPO), "--frozen", "--no-sync", *args],
        capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env,
    )


class UvProjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not UV:
            raise unittest.SkipTest("uv not installed (brew install uv)")
        if not (REPO / ".venv").exists():
            self_fail = ("project environment missing: run "
                         "'uv sync --locked' (or ./install-launchagents.sh) "
                         "in " + str(REPO))
            # Loud on purpose: this guard exists to catch broken setups.
            raise AssertionError(self_fail)

    def test_uv1_project_files_present(self) -> None:
        self.assertTrue((REPO / "pyproject.toml").is_file())
        self.assertTrue((REPO / "uv.lock").is_file())
        # single dependency source: no PEP 723 blocks anywhere in py sources
        for f in sorted(REPO.glob("*.py")) + sorted((REPO / "tests").glob("*.py")):
            head = f.read_text(encoding="utf-8")[:400]
            self.assertNotIn("# ///", head, f"PEP 723 block back in {f.name}")

    def test_uv2_lock_current(self) -> None:
        r = subprocess.run([UV, "lock", "--check", "--directory", str(REPO)],
                           capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_uv3_console_script_runs(self) -> None:
        r = uv_run("quota-sentinel", "--help", cwd=str(REPO))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("usage: quota-sentinel", r.stdout)

    def test_uv4_entry_point_parity(self) -> None:
        tmp = tempfile.mkdtemp(prefix="qs-uvp-")
        try:
            # help surface identical (same prog, same text, same rc)
            a = uv_run("quota-sentinel", "--help", cwd=str(REPO))
            b = uv_run("python", "-m", "quota_sentinel", "--help", cwd=str(REPO))
            self.assertEqual(a.returncode, b.returncode)
            self.assertEqual(a.stdout, b.stdout)
            # a real state read through both entries, same temp state dir
            c = uv_run("quota-sentinel", "--state-dir", tmp, "dump", "codex",
                       cwd=str(REPO))
            d = uv_run("python", "-m", "quota_sentinel", "--state-dir", tmp,
                       "dump", "codex", cwd=str(REPO))
            self.assertEqual(c.returncode, d.returncode)
            self.assertEqual(c.stdout, d.stdout)
            self.assertIn("next_due_at=unset", c.stdout)
            # invalid provider: typed CLI rc on both routes
            e = uv_run("quota-sentinel", "dump", "../evil", cwd=str(REPO))
            f = uv_run("python", "-m", "quota_sentinel", "dump", "../evil",
                       cwd=str(REPO))
            self.assertEqual(e.returncode, 3)
            self.assertEqual(f.returncode, 3)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_uv5_lark_from_project_env_only(self) -> None:
        r = uv_run("python", "-c", "import lark_oapi; print('lark-ok')",
                   cwd=str(REPO))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("lark-ok", r.stdout)
        # The guard itself must not pass on a globally installed copy: the
        # interpreter backing uv run must be the repo venv itself.
        # (sys.executable resolves symlinks out to the uv-managed base,
        # so compare sys.prefix — that is the venv root.)
        r2 = uv_run("python", "-c", "import sys; print(sys.prefix)",
                    cwd=str(REPO))
        self.assertEqual(
            Path(r2.stdout.strip()).resolve(), (REPO / ".venv").resolve(),
            f"uv used a non-project environment: {r2.stdout.strip()}",
        )

    def test_uv6_launchagent_style_startup_foreign_cwd_offline(self) -> None:
        # Production-import proof: import the real listener module (the
        # same import graph the daemon loads: lark_oapi + in-process
        # task_orchestrator) from launchd's WorkingDirectory, using the
        # exact plist flag set (--project --frozen --no-sync) with
        # UV_OFFLINE=1 — no network, no environment mutation at runtime.
        r = uv_run("python", "-c",
                   f"import sys; sys.path.insert(0, {str(REPO)!r}); "
                   "import feishu_listener, lark_oapi, task_orchestrator; "
                   "print('startup-env-ok')",
                   cwd="/private/tmp")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("startup-env-ok", r.stdout)

    def test_uv7_antigravity_isolation_boundary_kept(self) -> None:
        # Class B must stay outside the project even now that a root
        # pyproject exists: mirror the shell invocation's flags.
        r = subprocess.run(
            [UV, "run", "--offline", "--no-project", "--no-config",
             "python", "-B", "-c",
             "import sys; print('py', sys.version_info[:2])"],
            capture_output=True, text=True, timeout=120, cwd="/private/tmp",
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        # and the helper itself still compiles standalone
        r2 = subprocess.run(
            [UV, "run", "--offline", "--no-project", "--no-config",
             "python", "-B", "-m", "py_compile",
             str(REPO / "antigravity_usage.py")],
            capture_output=True, text=True, timeout=120, cwd="/private/tmp",
        )
        self.assertEqual(r2.returncode, 0, r2.stderr)


if __name__ == "__main__":
    unittest.main()
