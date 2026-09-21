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
* UV8  committed metadata carries NO user-local registry: uv.lock sources
       are public PyPI only (no mirrors/localhost/private IPs/user paths),
       and pyproject declares no index config — a generator machine's
       personal uv.toml can never ride into the repository again
       (reproducibility invariant: committed dependency metadata must not
       depend on untracked user-local uv configuration).
 * UV8b that policy is declared in the project itself (no [tool.uv]);
 * UV8c the UV8 leak rules are canary-tested, so a future refactor cannot
       silently turn one of them into a dead regex.
* UV9  the build backend is version-constrained (uv does not record
       build-system requires in uv.lock, so the pin lives in pyproject).
* UV10 deployment contract: uv run --frozen --no-sync never mutates the
       environment (byte-signature of .venv/bin stable across a run) —
       runtime does not self-heal; re-sync belongs to the installer.
"""
from __future__ import annotations

import os
import re
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

    # ---- UV8: committed dependency metadata vs user-local configuration ----
    OFFICIAL_HOSTS = ("pypi.org", "files.pythonhosted.org", "pypi.python.org")
    LEAK_PATTERNS = (
        r"localhost",
        r"127\.0\.0\.1",
        r"0\.0\.0\.0",
        # Private ranges are matched only in host position (after a scheme
        # or as a host key): a bare \b10\.\d+ would false-positive on any
        # future dependency whose version starts with 10.x / 172.20.x.
        r"(?:https?://|host\s*=\s*\")(?:10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+"
        r"|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|127\.)",
        r"mirrors\.",           # public-but-personal CN mirrors (cernet/sustech/…)
        r"file:///",
        r"/Users/",             # user home paths
        r"/home/",
        r"\\\\",                # windows shares (future-proof)
        # uv.lock encodes the index inside an inline table
        #   source = { registry = "https://pypi.org/simple" }
        # so a line-anchored ^registry rule can never fire; match the value
        # anywhere and allow only the official simple-index roots.
        r'registry\s*=\s*"(?!https://(?:pypi\.org|files\.pythonhosted\.org'
        r'|pypi\.python\.org)(?:/|"))',
    )

    # Canaries for UV8c: the guard above must demonstrably still fire on a
    # real leak and stay quiet on legitimate metadata. Without this, a
    # refactor can silently turn a rule into a dead regex.
    LEAK_CANARIES = (
        'source = { registry = "https://mirrors.sustech.edu.cn/pypi/simple" }',
        'source = { registry = "http://10.0.0.5:8081/simple" }',
        'host = "192.168.1.9"',
        'url = "http://localhost:8080/simple"',
        'url = "file:///Users/example/wheels"',
        'url = "https://pypi.org.evil.example/simple"',
    )
    LEAK_CLEAN_SAMPLES = (
        'source = { registry = "https://pypi.org/simple" }',
        'url = "https://files.pythonhosted.org/packages/x/foo-10.2.3-py3-none-any.whl"',
        'url = "https://files.pythonhosted.org/packages/x/bar-172.20.1-py3-none-any.whl"',
    )

    def _violations(self, text: str) -> list[str]:
        """Guard decision procedure for committed dependency metadata.

        Two independent rules, both must hold:
          1. no blacklisted fingerprint (blacklist; fast, specific);
          2. every http(s) URL host is an official PyPI host (allow-list;
             this is what catches unknown mirrors and look-alike domains
             such as pypi.org.evil.example that no blacklist would know).
        """
        bad = [f"fingerprint {pat!r}" for pat in self.LEAK_PATTERNS
               if re.search(pat, text)]
        for host in re.findall(r"https?://([^/\"\s]+)", text):
            host = host.rsplit("@", 1)[-1].split(":", 1)[0].lower()
            if host not in self.OFFICIAL_HOSTS:
                bad.append(f"non-official host {host!r}")
        return bad

    def _scan(self, text: str, where: str) -> None:
        bad = self._violations(text)
        self.assertEqual(bad, [], f"{where} leaked local-registry metadata: {bad}")

    def test_uv8_committed_lock_is_registry_clean(self) -> None:
        lock = (REPO / "uv.lock").read_text(encoding="utf-8")
        self._scan(lock, "uv.lock")
        # positive: every source registry is official PyPI
        for reg in set(re.findall(r'registry = "([^"]+)"', lock)):
            self.assertTrue(
                any(h in reg for h in self.OFFICIAL_HOSTS),
                f"non-official registry in lock: {reg}",
            )
        self.assertGreater(len(re.findall(r'registry = "', lock)), 0)

    # ---- UV8c: the leak guard itself must not rot ---------------------------
    def test_uv8c_leak_guard_canaries(self) -> None:
        for canary in self.LEAK_CANARIES:
            self.assertTrue(
                self._violations(canary),
                f"leak guard missed known-bad metadata: {canary}",
            )
        for sample in self.LEAK_CLEAN_SAMPLES:
            self.assertEqual(
                self._violations(sample), [],
                f"leak guard false-positives on official metadata: {sample}",
            )

    def test_uv8b_pyproject_declares_no_index_config(self) -> None:
        # Project policy: default public PyPI. Users may set UV_INDEX /
        # their own uv.toml at INSTALL time (deployment concern, §29); the
        # repository itself must never carry index/personal registry config.
        pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        self.assertNotIn("[[tool.uv.index]]", pyproject)
        self.assertNotIn("[tool.uv]", pyproject)
        self._scan(pyproject, "pyproject.toml")

    # ---- UV9: build backend must be version-constrained --------------------
    def test_uv9_build_backend_pinned(self) -> None:
        pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r'requires\s*=\s*\[([^\]]*)\]', pyproject)
        self.assertIsNotNone(m, "build-system requires not found")
        req = m.group(1)
        self.assertIn("hatchling", req)
        self.assertRegex(
            req, r'hatchling\s*==\s*[0-9][0-9.]*',
            "hatchling must be exact-pinned (uv.lock does not record "
            "build dependencies; see ARCHITECTURE.md)",
        )

    # ---- UV10: --frozen --no-sync never mutates the environment -------------
    def test_uv10_no_sync_runtime_cannot_mutate_env(self) -> None:
        # Signature = (relpath, size) of every file under .venv EXCLUDING
        # __pycache__ (a read-only run legitimately writes fresh .pyc
        # files; that is not an environment change). Any package add /
        # remove / content-size change moves this signature; mtimes are
        # deliberately not compared, since import caching would churn them.
        def env_sig() -> list:
            root = REPO / ".venv"
            out = []
            for p in root.rglob("*"):
                rp = str(p.relative_to(root))
                if "__pycache__" in rp or not p.is_file():
                    continue
                out.append((rp, p.stat().st_size))
            return sorted(out)

        before = env_sig()
        r = uv_run("python", "-c", "import lark_oapi, quota_sentinel; print('ran')",
                   cwd=str(REPO))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(env_sig(), before,
                         "uv run --frozen --no-sync mutated the project "
                         "environment — runtime must not self-heal; the "
                         "installer's uv sync --locked owns env freshness")


if __name__ == "__main__":
    unittest.main()
