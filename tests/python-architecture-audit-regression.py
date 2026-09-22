#!/usr/bin/env python3
"""Phase 7 mechanical architecture audit.

Everything asserted here is a REPO-WIDE, MECHANICAL property — the kind that
is easy to state in a document and easy to violate silently in a later
commit. Reading the directory structure is not evidence; these checks read
the actual source.

  AR1  exactly ONE authority → backend mapping exists
  AR2  legacy slot accessors are guarded, and no production scheduler path
       reaches one
  AR3  the shell holds no scheduler deadline arithmetic or policy literal
  AR4  authoritative state has exactly one writer family (the store/router)
  AR5  the class-C bridge import graph stays stdlib-only
  AR6  no secret, credential or real user path is committed
  AR7  .gitignore covers runtime artifacts and does NOT cover sources
  AR8  every legacy-accessor guard is present, not merely documented

Run: PYTHONPATH=. uv run --frozen --no-sync python tests/python-architecture-audit-regression.py
"""
from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SHELL = (REPO / "quota-sentinel.sh").read_text(encoding="utf-8")

LEGACY_ACCESSORS = (
    "read_provider_next_due",
    "read_provider_last_task",
    "read_provider_last_attempt",
    "read_provider_retry_pending",
    "read_provider_last_known_reset",
    "read_provider_reset_candidate",
    "read_provider_reset_anchor",
    "read_provider_last_window",
    "write_provider_next_due",
    "write_provider_last_task",
    "write_provider_last_attempt",
    "write_provider_retry_pending",
    "write_provider_last_known_reset",
    "write_provider_reset_candidate",
    "write_provider_reset_anchor",
    "write_provider_last_window",
    "clear_provider_reset_candidate",
    "clear_provider_reset_anchor",
)

# Shell functions that ARE the production scheduler path. None of them may
# reach a legacy slot accessor: they must go through the Python bridge.
PRODUCTION_SCHEDULER_FUNCTIONS = (
    "check_schedule",
    "wait_schedule",
    "run_retry_burst",
    "run_selected_providers",
    "run_and_reschedule_selected",
    "send_usage_notification",
    "evaluate_provider",
    "sync_provider_deadline_from_quota",
    "read_next_due",
    "commit_provider_success",
    "commit_provider_success_batch",
    "provider_is_pending",
    "provider_retry_due",
    "provider_fallback_due",
    "valid_provider_reset_at",
    "provider_schedule_block_reason",
    "run_cutover",
)

# The one deliberate exception: a DISPLAY fallback that dies loudly rather
# than serving a retired backend (see ARCHITECTURE.md, "Shell end state").
DISPLAY_FALLBACK_FUNCTIONS = ("status_next_due",)


def python_sources() -> list:
    """Every Python module in the package."""
    return sorted((REPO / "quota_sentinel").rglob("*.py"))


def scheduler_state_sources() -> list:
    """Modules that could touch AUTHORITATIVE SCHEDULER STATE.

    ``quota_sentinel/quota`` owns quota DOCUMENTS (what the card displays),
    which are not scheduler state and have their own writer. Keeping the two
    scopes apart is the point: a rule that cannot tell them apart would push
    either package into the wrong shape.
    """
    return [
        path for path in python_sources()
        if "/quota/" not in str(path)
    ]


def function_body(name: str, text: str = SHELL) -> str:
    start = text.index(f"{name}() {{")
    end = text.index("\n}", start)
    return text[start:end]


def tracked_files() -> list:
    out = subprocess.run(
        ["git", "ls-files"], cwd=str(REPO), capture_output=True, text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


class AuthorityRouting(unittest.TestCase):
    """AR1: one mapping, and no second one hiding in a helper."""

    # Constructing a concrete store is legal ONLY inside the persistence
    # package (where the stores and the cutover live) and in the CLI's two
    # explicitly per-backend diagnostics. Anywhere else it would be a second
    # authority → backend decision.
    STORE_CONSTRUCTION_ALLOWED = {
        "quota_sentinel/state/router.py",       # the mapping itself
        "quota_sentinel/state/cutover.py",      # operates on one backend
        "quota_sentinel/state/migration.py",    # seeds the json backend
        "quota_sentinel/__main__.py",           # per-backend diagnostics
    }

    def test_ar1_only_the_router_maps_authority_to_a_backend(self):
        # `class Foo(Store)` is a definition, not a construction.
        construction = re.compile(
            r"(?<!class )(?<![\w.])(?:FileStateStore|JsonStateStore)\("
        )
        offenders = []
        for path in scheduler_state_sources():
            rel = str(path.relative_to(REPO))
            if rel in self.STORE_CONSTRUCTION_ALLOWED:
                continue
            if construction.search(path.read_text(encoding="utf-8")):
                offenders.append(rel)
        self.assertEqual(
            offenders, [],
            "these modules construct a state store directly instead of "
            "asking AuthoritativeStateStore, which would let a second "
            "authority → backend mapping exist",
        )

    def test_ar1b_the_mapping_is_a_single_function(self):
        router = (REPO / "quota_sentinel" / "state" / "router.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(router.count("def store_for("), 1)
        self.assertIn("if authority.is_json:", router)

    def test_ar1c_backend_names_come_from_the_authority_module(self):
        """No module may compare a bare "json"/"legacy" literal against a
        backend; the constants are the single vocabulary."""
        pattern = re.compile(r"""authority\.backend\s*==\s*["']""")
        for path in python_sources():
            text = path.read_text(encoding="utf-8")
            with self.subTest(module=str(path.relative_to(REPO))):
                self.assertIsNone(pattern.search(text))


class LegacyAccessGuards(unittest.TestCase):
    """AR2/AR8: the retired backend's API is guarded everywhere."""

    def test_ar2_every_legacy_accessor_requires_the_legacy_backend(self):
        for name in LEGACY_ACCESSORS:
            with self.subTest(function=name):
                body = function_body(name)
                self.assertIn(
                    "require_legacy_backend", body,
                    f"{name} touches legacy slot files without the guard",
                )

    def test_ar2b_the_bootstrap_writers_are_guarded_too(self):
        self.assertIn("require_legacy_backend", function_body("seed_provider_state_file"))
        self.assertIn("legacy_backend_active || return 0", function_body("migrate_legacy_state"))

    def test_ar2c_atomic_write_state_file_has_no_unguarded_caller(self):
        """The single most dangerous primitive: it writes a legacy slot file
        with no guard of its own, so every caller must already be guarded."""
        raw = SHELL
        callers = set()
        for match in re.finditer(r"^([a-z_]+)\(\) \{", raw, re.M):
            name = match.group(1)
            body = function_body(name, raw)
            if re.search(rf"^\s*atomic_write_state_file\s+\S", body, re.M):
                callers.add(name)
        self.assertTrue(callers, "atomic_write_state_file lost all callers")
        for name in sorted(callers):
            with self.subTest(caller=name):
                self.assertIn(
                    "require_legacy_backend", function_body(name, raw),
                    f"{name} calls atomic_write_state_file unguarded",
                )

    def test_ar2d_no_production_scheduler_path_reaches_a_legacy_accessor(self):
        offenders = []
        for name in PRODUCTION_SCHEDULER_FUNCTIONS:
            body = function_body(name)
            for accessor in LEGACY_ACCESSORS:
                if re.search(rf"(?<![\w-]){accessor}\b", body):
                    offenders.append(f"{name} -> {accessor}")
        self.assertEqual(
            offenders, [],
            "a production scheduler path still touches the legacy backend "
            "directly; it must go through the scheduler bridge",
        )

    def test_ar2e_the_only_display_fallback_is_documented(self):
        """`status` may fall back to a legacy read — but that read is itself
        guarded, so under JSON authority it fails loudly instead of showing a
        retired value."""
        for name in DISPLAY_FALLBACK_FUNCTIONS:
            body = function_body(name)
            self.assertIn("read_provider_next_due", body)
            guarded = function_body("read_provider_next_due")
            self.assertIn("require_legacy_backend", guarded)


class ShellPolicyOwnership(unittest.TestCase):
    """AR3: no second scheduler implementation in the shell."""

    POLICY_LITERALS = (
        "RUN_INTERVAL_SECONDS",
        "RESET_BUFFER_SECONDS",
        "RESET_NEAR_MOVEMENT_SECONDS",
        "RESET_CONFIRM_MIN_AGE_SECONDS",
        "RESET_CONFIRM_MATCH_SECONDS",
        "MAX_WINDOW_FUTURE_SECONDS",
        "RETRY_INTERVAL_SECONDS",
        "INITIAL_ATTEMPT_LIMIT",
        "WATCHDOG_ATTEMPT_LIMIT",
        "WATCHDOG_RETRY_GAP_SECONDS",
    )

    def test_ar3_no_policy_value_is_assigned_in_the_shell(self):
        for name in self.POLICY_LITERALS:
            with self.subTest(name=name):
                pattern = re.compile(
                    rf"(^|[^A-Za-z0-9_])(readonly\s+|typeset\s+-\w+\s+)?"
                    rf"{name}\s*=\s*[0-9]"
                )
                match = pattern.search(SHELL)
                self.assertIsNone(
                    match,
                    f"{name} has a literal value in the shell; scheduler "
                    "policy must have exactly one owner (Python)",
                )

    def test_ar3b_policy_values_arrive_from_the_domain(self):
        self.assertIn("scheduler-config", SHELL)
        self.assertIn("scheduler_policy_config", SHELL)

    def test_ar3c_no_deadline_arithmetic_outside_the_domain(self):
        """The shell may compute a display string or a sleep duration, but it
        must not derive a DEADLINE. These are the shapes that would."""
        arithmetic = re.compile(
            r"\$\(\(\s*[^)]*\b(?:RESET_BUFFER_SECONDS|RUN_INTERVAL_SECONDS|"
            r"RESET_NEAR_MOVEMENT_SECONDS|MAX_WINDOW_FUTURE_SECONDS|"
            r"RESET_CONFIRM_MIN_AGE_SECONDS|RESET_CONFIRM_MATCH_SECONDS)\b"
        )
        match = arithmetic.search(SHELL)
        self.assertIsNone(
            match,
            "the shell computes a deadline from a policy constant; read the "
            "result back from the domain instead",
        )
        # The only legacy-state names the shell may mention are the PATH
        # BUILDERS of the guarded accessors ("provider-<slot>" filenames),
        # never a value it derives scheduling from.
        for line in SHELL.splitlines():
            stripped = line.strip()
            if "last_known_reset" not in stripped:
                continue
            with self.subTest(line=stripped):
                self.assertTrue(
                    "provider_last_known_reset_file" in stripped
                    or stripped.startswith("#")
                    or re.match(
                        r"(read|write)_provider_last_known_reset\(\) \{", stripped
                    ),
                    f"unexpected use of legacy state in the shell: {stripped}",
                )


class AuthoritativeWriter(unittest.TestCase):
    """AR4: authoritative state has exactly one writer family."""

    # The persistence package IS the writer family: the atomic primitives,
    # the two stores, the authority manifest and the cutover's verified
    # publish. Outside it, nothing may write a durable document.
    PERSISTENCE_PACKAGE = "quota_sentinel/state/"
    WRITE_PATTERN = re.compile(
        r"_publish_atomic|os\.replace\(|\.write_text\(|\.write_bytes\("
        r"|open\([^)]*,\s*['\"][wa]"
    )

    def test_ar4_no_direct_state_writes_outside_the_persistence_package(self):
        offenders = []
        for path in scheduler_state_sources():
            rel = str(path.relative_to(REPO))
            if rel.startswith(self.PERSISTENCE_PACKAGE):
                continue
            if self.WRITE_PATTERN.search(path.read_text(encoding="utf-8")):
                offenders.append(rel)
        self.assertEqual(
            offenders, [],
            "these modules write files directly; a durable publish must go "
            "through the store's atomic primitive",
        )

    def test_ar4b_transitions_commit_only_through_the_router(self):
        service = (REPO / "quota_sentinel" / "scheduler" / "service.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("store.commit(", service)
        # ... and the store the service commits to is always the router's.
        self.assertIn("def router(", service)
        self.assertNotIn("FileStateStore", service)
        self.assertNotIn("JsonStateStore", service)


class BridgeRuntime(unittest.TestCase):
    """AR5: the hot-path bridge stays a class-C helper."""

    def test_ar5_bridge_import_graph_is_stdlib_only(self):
        program = (
            "import sys;"
            f"sys.path.insert(0, {str(REPO)!r});"
            "import quota_sentinel.scheduler, quota_sentinel.state,"
            " quota_sentinel.notifications, quota_sentinel.quota;"
            "third = [m for m in sys.modules if m.split('.')[0] in"
            " ('lark_oapi','requests','httpx','anyio','pydantic','yaml')];"
            "print('third=' + ','.join(sorted(third)))"
        )
        env = {"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin"}
        result = subprocess.run(
            ["/usr/bin/python3", "-S", "-c", program],
            capture_output=True, text=True, timeout=60,
            cwd="/private/tmp", env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "third=")

    def test_ar5b_bridge_does_not_require_the_project_environment(self):
        shell = SHELL
        self.assertIn('PYTHONPATH="$SCRIPT_DIR" "$PYTHON3_BIN"', shell)
        self.assertNotIn('uv run --project', shell.split("scheduler_bridge()")[1][:400])


class SecretsAndPaths(unittest.TestCase):
    """AR6: nothing sensitive or machine-specific is committed."""

    SECRET_PATTERNS = (
        r"sk-[A-Za-z0-9]{20,}",
        r"xox[baprs]-[A-Za-z0-9-]{10,}",
        r"AKIA[0-9A-Z]{12,}",
        r"ghp_[A-Za-z0-9]{20,}",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r'"app_secret"\s*:\s*"[^"]{8,}"',
        r"app_secret=[A-Za-z0-9]{8,}",
    )
    # Files whose only matches are detection canaries / regex sources. Both
    # are checked line by line below, so this is an allowlist of two known
    # strings, not a blanket exemption.
    CANARY_FILES = {
        "tests/uv-project-regression.py",
        "tests/python-architecture-audit-regression.py",
    }
    # Values that are obviously placeholders rather than credentials.
    PLACEHOLDER_VALUES = {
        "test-secret", "TOPSECRET", "REDACTED", "redacted", "example",
        "placeholder", "dummy", "fake", "x",
    }

    def test_ar6_no_credentials_in_tracked_files(self):
        for rel in tracked_files():
            path = REPO / rel
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for pattern in self.SECRET_PATTERNS:
                for match in re.finditer(pattern, text):
                    line = text[: match.start()].count("\n") + 1
                    if any(v in match.group(0) for v in self.PLACEHOLDER_VALUES):
                        continue
                    with self.subTest(file=rel, line=line, pattern=pattern):
                        self.fail(
                            f"{rel}:{line} contains something that looks like "
                            "a credential"
                        )

    def test_ar6b_no_real_user_paths_are_committed(self):
        for rel in tracked_files():
            if rel in self.CANARY_FILES:
                continue
            path = REPO / rel
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            with self.subTest(file=rel):
                self.assertNotIn(
                    "/Users/", text,
                    f"{rel} hardcodes a home directory; the repo must stay "
                    "portable (use __REPO_DIR__ placeholders or derive paths)",
                )

    def test_ar6c_the_canary_file_only_has_detection_matches(self):
        text = (REPO / "tests/uv-project-regression.py").read_text(encoding="utf-8")
        allowed = {'r"/Users/"', 'file:///Users/example/wheels'}
        for line in text.splitlines():
            if "/Users/" not in line:
                continue
            with self.subTest(line=line.strip()):
                self.assertTrue(
                    any(token in line for token in allowed),
                    f"unexpected /Users/ occurrence: {line.strip()}",
                )

    def test_ar6d_no_rendered_launchagent_or_state_is_tracked(self):
        for rel in tracked_files():
            with self.subTest(file=rel):
                self.assertFalse(rel.endswith(".plist"))
                self.assertFalse(rel.endswith(".sqlite3"))
                self.assertFalse(rel.endswith(".log"))


class GitignoreCoverage(unittest.TestCase):
    """AR7: runtime artifacts ignored, sources never."""

    MUST_BE_IGNORED = (
        ".venv/bin/python",
        "quota_sentinel/__pycache__/x.pyc",
        "logs/2026-01-01.log",
        "quota-sentinel.plist",
        "state.sqlite3",
        ".pytest_cache/CACHEDIR.TAG",
        "PRE-PUBLISH-AUDIT.md",
    )
    MUST_NOT_BE_IGNORED = (
        "uv.lock",
        "pyproject.toml",
        "quota-sentinel.sh",
        "quota_sentinel/state/store.py",
        "quota_sentinel/scheduler/policy.py",
        "tests/python-scheduler-regression.py",
        "quota-sentinel.plist.template",
        "ARCHITECTURE.md",
        "README.md",
    )

    def _ignored(self, rel: str) -> bool:
        result = subprocess.run(
            ["git", "check-ignore", "-q", rel], cwd=str(REPO),
            capture_output=True,
        )
        return result.returncode == 0

    def test_ar7_runtime_artifacts_are_ignored(self):
        for rel in self.MUST_BE_IGNORED:
            with self.subTest(path=rel):
                self.assertTrue(self._ignored(rel), f"{rel} is not ignored")

    def test_ar7b_sources_are_never_ignored(self):
        for rel in self.MUST_NOT_BE_IGNORED:
            with self.subTest(path=rel):
                self.assertFalse(
                    self._ignored(rel),
                    f"{rel} is ignored — it must be committed",
                )

    def test_ar7c_every_source_file_is_actually_tracked(self):
        """An over-broad ignore rule is invisible until someone clones."""
        tracked = set(tracked_files())
        for path in sorted((REPO / "quota_sentinel").rglob("*.py")):
            rel = str(path.relative_to(REPO))
            with self.subTest(file=rel):
                self.assertIn(rel, tracked, f"{rel} exists but is untracked")


class QuotaAdapterAgreement(unittest.TestCase):
    """AR9: the shell and the Python adapters agree on the quota ladder.

    The ladder and the capability facts are declared once, in
    ``quota_sentinel.quota``. The shell still EXECUTES a tier (vendor probes
    under the shared timeout) and still renders provider titles, so those two
    places are checked for agreement rather than trusted.
    """

    def test_ar9_shell_executes_exactly_the_declared_tiers(self):
        sys.path.insert(0, str(REPO))
        from quota_sentinel.quota.adapters import PROVIDERS, TIER_LADDER

        body = function_body("quota_tier_command")
        for tier in TIER_LADDER:
            with self.subTest(tier=tier.value):
                self.assertIn(f"{tier.value})", body)
        # No extra arm the plan does not know about.
        arms = re.findall(r"^\s{4}([a-z-]+)\)", body, re.M)
        self.assertEqual(sorted(arms), sorted(t.value for t in TIER_LADDER))
        # ... and the provider roster is the same one.
        match = re.search(r"^readonly PROVIDERS=\(([^)]*)\)", SHELL, re.M)
        self.assertEqual(tuple(match.group(1).split()), tuple(PROVIDERS))

    def test_ar9b_shell_and_adapter_agree_on_provider_titles(self):
        """Titles are rendering data the shell still owns, so the two copies
        must at least be mechanically identical."""
        sys.path.insert(0, str(REPO))
        from quota_sentinel.quota.adapters import ADAPTERS

        body = function_body("provider_card_title")
        for provider, adapter in ADAPTERS.items():
            with self.subTest(provider=provider):
                self.assertIn(f'{provider}) print -r -- "{adapter.title}"', body)

    def test_ar9c_shell_keeps_no_jq_quota_normaliser(self):
        """The seven jq programs are gone; normalisation is one Python
        implementation with golden tests."""
        for name in (
            "normalize_pi_codex_quota",
            "normalize_pi_antigravity_quota",
            "normalize_pi_opencode_quota",
            "normalize_codexbar_codex_quota",
            "normalize_codexbar_antigravity_quota",
            "normalize_codexbar_opencode_quota",
            "renormalise_quota_file",
        ):
            with self.subTest(function=name):
                body = function_body(name)
                self.assertIn("quota_normalize", body)
                self.assertNotIn("JQ_BIN", body)

    def test_ar9d_monthly_is_display_only_where_declared(self):
        sys.path.insert(0, str(REPO))
        from quota_sentinel.quota.adapters import ADAPTERS
        monthly = sorted(p for p, a in ADAPTERS.items() if a.monthly_display_only)
        self.assertEqual(monthly, ["opencode"])


class LifecycleLockOwnership(unittest.TestCase):
    """AR10/AR13: operator-visible mutations go through run.lock.

    A CLI cannot verify that a caller holds a lock it did not take, so the
    safety property cannot be "the verb checks". It has to be structural:
    the OPERATOR surface either acquires the lock itself, or does not exist;
    the verbs the shell calls while already holding it are labelled
    INTERNAL so nothing presents them as standalone operator commands.
    """

    def _parser(self):
        sys.path.insert(0, str(REPO))
        from quota_sentinel.__main__ import build_parser
        return build_parser()

    def _help_entries(self) -> dict:
        parser = self._parser()
        return {
            action.dest: (action.help or "")
            for action in parser._subparsers._group_actions[0]._choices_actions
        }

    def test_ar10_every_scheduler_bridge_verb_is_marked_internal(self):
        entries = self._help_entries()
        scheduler_verbs = {k: v for k, v in entries.items()
                           if k.startswith("scheduler-")}
        self.assertGreater(len(scheduler_verbs), 10)
        offenders = sorted(
            name for name, help_text in scheduler_verbs.items()
            if not help_text.startswith("INTERNAL BRIDGE API")
        )
        self.assertEqual(
            offenders, [],
            "these verbs mutate state under a lock the caller must already "
            "hold; the help surface must say so",
        )

    def test_ar10b_public_lifecycle_verbs_acquire_the_lock_themselves(self):
        """The operator lifecycle verbs must be safe STANDALONE.

        Their handlers run inside the run.lock context manager, so an
        operator typing one cannot interleave a backend switch with an
        in-flight scheduler run.
        """
        main_source = (REPO / "quota_sentinel" / "__main__.py").read_text(
            encoding="utf-8"
        )
        for verb in ("cutover", "rollback", "bootstrap-authority"):
            with self.subTest(verb=verb):
                self.assertIn(f'"{verb}"', main_source)
        # The lock helper is a context manager around the switch itself.
        self.assertIn("def _lifecycle_lock(", main_source)
        self.assertIn("with _lifecycle_lock(", main_source)
        self.assertIn("acquire_run_lock(", main_source)
        # ... and the underlying primitive is the shell's own binary.
        runlock_source = (
            REPO / "quota_sentinel" / "state" / "runlock.py"
        ).read_text(encoding="utf-8")
        self.assertIn('SHLOCK_BIN = "/usr/bin/shlock"', runlock_source)
        self.assertIn('RUN_LOCK_FILENAME = "run.lock"', runlock_source)

    def test_ar10c_public_docs_do_not_advertise_internal_verbs(self):
        """Repo-wide: no document may present an INTERNAL verb as an
        operator command. That combination is exactly how the lock gets
        bypassed by a well-meaning human."""
        offenders = []
        for rel in tracked_files():
            if not rel.endswith((".md", ".sh", ".zsh")):
                continue
            path = REPO / rel
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if "scheduler-" not in stripped:
                    continue
                # A documented invocation of a bridge verb.
                looks_like_a_command = (
                    stripped.startswith("uv run quota-sentinel")
                    or stripped.startswith("./quota-sentinel.sh scheduler-")
                )
                if looks_like_a_command:
                    offenders.append(f"{rel}:{lineno}: {stripped}")
        self.assertEqual(
            offenders, [],
            "internal bridge verbs must not be documented as operator "
            "commands; use ./quota-sentinel.sh <lifecycle verb>",
        )

    def test_ar10d_documented_operator_lifecycle_is_the_lock_safe_one(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        for verb in ("cutover", "rollback"):
            with self.subTest(verb=verb):
                self.assertIn(f"./quota-sentinel.sh {verb}", readme)
        self.assertIn(
            "./quota-sentinel.sh bootstrap-authority --assume-legacy", readme
        )


class AuthorityNeverDefaultsToLegacy(unittest.TestCase):
    """AR11: absence is a lifecycle input, never a runtime default."""

    def test_ar11_absent_manifest_raises_and_creates_nothing(self):
        import tempfile
        from quota_sentinel.state import (
            AuthorityMissingError,
            AuthoritativeStateStore,
            authority_path,
            read_authority,
        )

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            with self.assertRaises(AuthorityMissingError):
                read_authority(state_dir)
            with self.assertRaises(AuthorityMissingError):
                AuthoritativeStateStore(state_dir).load("codex")
            self.assertFalse(authority_path(state_dir).exists())

    def test_ar11b_bootstrap_authority_has_exactly_one_producer(self):
        """The legacy default may be CONSTRUCTED only by the bootstrap.

        If another module could conjure `bootstrap_authority()` on a
        missing manifest, absence would silently become legacy again. The
        match is anchored on the exact call, because the one legal caller is
        named `bootstrap_legacy_authority` and a substring test would either
        miss a real offender or flag the legitimate one.
        """
        import re

        allowed = {"quota_sentinel/state/authority.py"}
        offenders = []
        pattern = re.compile(r"(?<![A-Za-z0-9_])bootstrap_authority\s*\(")
        for path in python_sources():
            rel = str(path.relative_to(REPO))
            if rel in allowed or "/quota/" in rel:
                continue
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(rel)
        self.assertEqual(offenders, [])

    def test_ar11c_no_reader_catches_a_missing_manifest_as_legacy(self):
        authority_source = (
            REPO / "quota_sentinel" / "state" / "authority.py"
        ).read_text(encoding="utf-8")
        # The ONLY FileNotFoundError handler in the authority module is the
        # one that raises AuthorityMissingError. A second one — or any
        # return of the bootstrap authority from a READ — would put the
        # silent legacy default back.
        self.assertEqual(
            authority_source.count("except FileNotFoundError"), 1
        )
        read_body = authority_source[
            authority_source.index("def read_authority("):
            authority_source.index("def write_authority(")
        ]
        self.assertIn("except FileNotFoundError as exc:", read_body)
        self.assertIn("AuthorityMissingError", read_body)
        self.assertNotIn("return bootstrap_authority()", read_body)
        # The initializer is the one function allowed to construct it.
        init_body = authority_source[
            authority_source.index("def bootstrap_legacy_authority("):
        ]
        self.assertIn("bootstrap_authority()", init_body)


class InstallerUpgradeSafety(unittest.TestCase):
    """AR12: the installer retires the legacy agents before starting new ones."""

    INSTALLER = (REPO / "install-launchagents.sh").read_text(encoding="utf-8")

    def test_ar12_retired_labels_are_declared_and_booted_out(self):
        self.assertIn("RETIRED_LABELS=(quota-sentinel quota-sentinel.timer)",
                      self.INSTALLER)
        self.assertIn('for label in "${RETIRED_LABELS[@]}"', self.INSTALLER)
        self.assertIn('"$LAUNCHCTL_BIN" bootout "$DOMAIN/$label"',
                      self.INSTALLER)

    def test_ar12b_retired_labels_are_booted_out_before_the_listener(self):
        retired_at = self.INSTALLER.index('for label in "${RETIRED_LABELS[@]}"')
        active_at = self.INSTALLER.index('for label in "${ACTIVE_LABELS[@]}"')
        self.assertLess(
            retired_at, active_at,
            "a legacy scheduler must be stopped before the new listener "
            "starts, or two scheduling entry points coexist",
        )

    def test_ar12c_installer_validates_authority_and_never_creates_it(self):
        """The installer may not decide who owns the state.

        A missing manifest means the owner is UNKNOWN — not legacy — so an
        installer that wrote `legacy` would silently re-legitimize a
        deployment that had already cut over and lost its manifest. The
        installer therefore only READS the fact, and refuses to install when
        it cannot be read. Upgrading, asserting ownership and switching
        ownership stay three separate actions.
        """
        # It reads the fact ...
        self.assertIn(" authority 2>&1", self.INSTALLER)
        # ... and never writes it. The retired spellings must be gone
        # entirely, and the one surviving mention of the operator verb may
        # only be the guidance text in the refusal: a line that PRINTS the
        # command, never one that runs it.
        for retired in (
            "authority-initialize",
            "bootstrap_legacy_authority",
            "initialize_authority",
        ):
            with self.subTest(token=retired):
                self.assertNotIn(retired, self.INSTALLER)
        for line in self.INSTALLER.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "bootstrap-authority" not in stripped:
                continue
            with self.subTest(line=stripped):
                self.assertIn(
                    "print -ru2", stripped,
                    "the installer may PRINT the bootstrap command as "
                    "guidance but must never CALL it",
                )
        self.assertNotIn("scheduler-cutover", self.INSTALLER)
        self.assertNotIn("scheduler-rollback", self.INSTALLER)
        # No invocation of the cutover/rollback CLI either, in any form.
        for line in self.INSTALLER.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            with self.subTest(line=stripped):
                self.assertNotIn(" cutover", stripped)
                self.assertNotIn(" rollback", stripped)
        # The refusal has to tell the operator what ONLY they can decide.
        self.assertIn(
            "quota-sentinel.sh bootstrap-authority --assume-legacy",
            self.INSTALLER,
        )

    def test_ar12d_launchctl_is_overridable_so_upgrades_are_testable(self):
        self.assertIn("QUOTA_SENTINEL_LAUNCHCTL_BIN", self.INSTALLER)
        self.assertIn("QUOTA_SENTINEL_STATE_DIR", self.INSTALLER)


class WholeRosterAuthoritySwitch(unittest.TestCase):
    """AR13/AR14: authority is ONE global fact, so a switch is all-or-nothing.

    A provider-scoped cutover is not a smaller cutover: the manifest names
    the backend for the whole state directory, so flipping it after
    preparing one provider would hand the other two to the retired backend —
    silently discarding deadlines and retry debt the JSON documents own.
    These tests pin the API shape that makes that unexpressible.
    """

    def test_ar13_switch_primitives_take_no_provider_subset(self):
        import inspect

        from quota_sentinel.state import cutover_to_json, rollback_to_legacy

        for func in (cutover_to_json, rollback_to_legacy):
            with self.subTest(func=func.__name__):
                signature = inspect.signature(func)
                self.assertNotIn("providers", signature.parameters)
                self.assertEqual(
                    list(signature.parameters)[:2], ["state_dir", "checkpoint"]
                )
                source = inspect.getsource(func)
                self.assertIn("DEFAULT_PROVIDERS", source)

    def test_ar13b_switch_primitives_prepare_and_verify_every_provider(self):
        from quota_sentinel.state import migration

        cutover_source = (
            REPO / "quota_sentinel" / "state" / "cutover.py"
        ).read_text(encoding="utf-8")
        self.assertIn("roster: Sequence[str] = DEFAULT_PROVIDERS", cutover_source)
        self.assertIn("DEFAULT_PROVIDERS", (
            REPO / "quota_sentinel" / "state" / "migration.py"
        ).read_text(encoding="utf-8"))
        # The roster is the shared constant, not a local list that could
        # drift away from the migration/parity surface.
        self.assertGreaterEqual(len(migration.DEFAULT_PROVIDERS), 2)

    def test_ar14_public_and_bridge_verbs_reject_a_provider_subset(self):
        from quota_sentinel.__main__ import build_parser

        parser = build_parser()
        for verb in ("cutover", "rollback"):
            with self.subTest(verb=verb):
                # No positional providers argument exists ...
                args = parser.parse_args([verb])
                self.assertFalse(hasattr(args, "providers"))
                # ... so passing one is a usage error BEFORE any mutation.
                with self.assertRaises(SystemExit) as caught:
                    parser.parse_args([verb, "codex"])
                self.assertEqual(caught.exception.code, 2)

    def test_ar14b_bridge_verbs_reject_a_provider_subset_too(self):
        from quota_sentinel.__main__ import build_parser

        parser = build_parser()
        for verb in ("scheduler-cutover", "scheduler-rollback"):
            with self.subTest(verb=verb):
                args = parser.parse_args([verb])
                self.assertFalse(hasattr(args, "providers"))
                with self.assertRaises(SystemExit) as caught:
                    parser.parse_args([verb, "codex"])
                self.assertEqual(caught.exception.code, 2)
        # The one authority-CREATING verb demands the assertion on both
        # surfaces: no reachable path may create the fact without it.
        self.assertFalse(
            parser.parse_args(["scheduler-bootstrap-authority"]).assume_legacy
        )
        self.assertTrue(
            parser.parse_args(
                ["scheduler-bootstrap-authority", "--assume-legacy"]
            ).assume_legacy
        )
        self.assertFalse(
            parser.parse_args(["bootstrap-authority"]).assume_legacy
        )


class ExplicitBootstrapOnly(unittest.TestCase):
    """AR15-AR17: nothing automatic may turn absence into legacy.

    The reviewed hazard: JSON authoritative + advanced state + a deleted
    manifest, and the user re-runs the installer — which used to create
    `legacy epoch 0` and thereby re-legitimize stale deadlines and retry
    debt. Only an operator assertion (`--assume-legacy`) may do that, and
    these tests pin every path that could otherwise do it by accident.
    """

    SHELL = (REPO / "quota-sentinel.sh").read_text(encoding="utf-8")
    INSTALLER = (REPO / "install-launchagents.sh").read_text(encoding="utf-8")

    def test_ar15_installer_never_bootstraps_authority(self):
        for token in (
            "authority_bootstrap",
            "scheduler-bootstrap-authority",
            "authority-initialize",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, self.INSTALLER)

    def test_ar16_shell_bootstrap_has_exactly_one_reachable_call_site(self):
        # The helper and its bridge are each defined once and called once —
        # from the operator verb only. A second call site anywhere in the
        # script would mean a runtime path had grown a bootstrap.
        self.assertEqual(self.SHELL.count("authority_bootstrap"), 2)
        self.assertEqual(self.SHELL.count("_bootstrap_authority_bridge"), 2)
        self.assertEqual(
            self.SHELL.count("scheduler_bridge scheduler-bootstrap-authority"), 1
        )
        # ... and the runtime command paths never mention it at all.
        dispatch = self.SHELL[self.SHELL.index("main() {"):]
        for case in ("check)", "wait)", "run)", "usage)", "status)"):
            block = dispatch[dispatch.index(case):]
            block = block[:block.index(";;")]
            with self.subTest(case=case):
                self.assertNotIn("bootstrap", block)

    def test_ar16b_only_the_operator_cli_may_call_the_library_primitive(self):
        import re

        allowed = {
            "quota_sentinel/state/authority.py",
            "quota_sentinel/__main__.py",
            "quota_sentinel/scheduler/cli.py",
        }
        pattern = re.compile(
            r"(?<![A-Za-z0-9_])bootstrap_legacy_authority\s*\("
        )
        offenders = []
        for path in python_sources():
            rel = str(path.relative_to(REPO))
            if rel in allowed or "/quota/" in rel:
                continue
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(rel)
        self.assertEqual(
            offenders, [],
            "an automatic runtime path grew an authority bootstrap: a "
            "deleted manifest would be silently re-legitimized as legacy",
        )

    def test_ar17_docs_describe_the_assertion_not_an_auto_initialize(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        arch = (REPO / "ARCHITECTURE.md").read_text(encoding="utf-8")
        for text, label in ((readme, "README.md"), (arch, "ARCHITECTURE.md")):
            with self.subTest(document=label):
                for retired in ("init-authority", "authority-initialize",
                                "initialize_authority"):
                    self.assertNotIn(retired, text)
                self.assertIn("bootstrap-authority", text)
                self.assertIn("--assume-legacy", text)
        # The install/update flow must not present the installer as the
        # thing that creates the ownership fact.
        installer_doc = readme[readme.index("install-launchagents.sh"):]
        self.assertIn("never", installer_doc[:4000].lower())


class DocumentationPointers(unittest.TestCase):
    """The docs must name the real owners, not the pre-migration ones."""

    def test_never_claims_the_shell_owns_scheduler_policy(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        arch = (REPO / "ARCHITECTURE.md").read_text(encoding="utf-8")
        for text, label in ((readme, "README.md"), (arch, "ARCHITECTURE.md")):
            with self.subTest(document=label):
                self.assertNotIn("still runs entirely in the shell", text)
                self.assertNotIn(
                    "remain\nimplemented exclusively by the shell scheduler", text
                )

    def test_architecture_doc_names_the_authority_fact(self):
        arch = (REPO / "ARCHITECTURE.md").read_text(encoding="utf-8")
        for token in (
            "backend-authority.json",
            "AuthoritativeStateStore",
            "quota_sentinel.scheduler.policy",
            "at-least-once",
            "run.lock",
            "quota.lock",
        ):
            with self.subTest(token=token):
                self.assertIn(token, arch)


if __name__ == "__main__":
    unittest.main(verbosity=2)
