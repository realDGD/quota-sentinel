# Full Python Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the operational `quota-sentinel.sh` implementation with Python, preserve observable behavior, then move the live repository into `Script/Python/quota-sentinel`.

**Architecture:** Keep the existing Python state, scheduler, quota normalization and notification policy packages. Add Python adapters for process execution, quota acquisition, Feishu rendering/transport and a public application coordinator. Make the listener and orchestrator invoke the Python CLI directly, then replace the Shell entry with a compatibility shim or remove it after tests have equivalent Python coverage.

**Tech Stack:** Python 3.9+ or an explicitly raised supported minimum, stdlib subprocess/urllib/json/threading, existing `lark-oapi`, macOS `shlock` and launchd, uv.

**Spec:** `docs/superpowers/specs/2026-09-22-full-python-port-design.md`

## Global Constraints

- State directories, slot and JSON document formats, lock names and authority rules stay compatible.
- Normal operation must not execute zsh; the installer may remain a shell setup helper.
- Preserve the three-provider roster, fallback order, freshness, retry limits and timing from the existing policy.
- Keep all credentials in Keychain or environment; never log secrets or introduce committed machine paths.
- Use temp state directories and fake vendor commands for runtime tests. Never exercise live model calls or Feishu delivery during tests.

## Review Focus

- A provider timeout must kill its process group and record a retry debt without committing success.
- A stale quota cache must remain display-only and must not reschedule a provider.
- A missing authority manifest must stop writes without silently treating legacy slots as authoritative.
- Concurrent `check` and `/usage` must respect separate run and quota locks and give the busy response.
- An installed LaunchAgent must start from `/private/tmp` using the moved repository and its locked uv environment.

---

### Task 1: Provider model runner

**Files:** Create `quota_sentinel/runtime/models.py`; modify `run_with_timeout.py` only if reuse requires a callable API; test in `tests/python-model-runner-regression.py`.

**Interfaces:** `ModelRunner(config).prepare(provider, workspace)` copies auth/settings and yields paths; `ModelRunner.run(provider, workspace, phase, attempt, limit) -> AttemptResult(success, exit_code, timed_out, elapsed, stdout_path, stderr_path, quota_path)`. The provider command options and environment must match `run_codex`, `run_antigravity` and `run_opencode` in `quota-sentinel.sh`.

- [ ] Write failing tests using a fake Pi executable for exact argv/env, an exact `1` success, non-exact output failure, timeout and secret-redacted diagnostics.
- [ ] Run `UV_CACHE_DIR=/private/tmp/qs-uv-cache uv run --frozen --no-sync python tests/python-model-runner-regression.py` and observe the expected missing implementation failure.
- [ ] Implement the runner with `subprocess` and the existing process-group timeout contract; use `pathlib` for per-provider output and quota files.
- [ ] Re-run its suite and `tests/run-with-timeout-regression.py`, then inspect the diff.

### Task 2: Quota tier acquisition

**Files:** Create `quota_sentinel/runtime/quota_probe.py`; use existing `quota_sentinel/quota/{adapters,normalize,models}.py`; test in `tests/python-quota-probe-regression.py`.

**Interfaces:** `QuotaProbe(config).collect(providers, workspace, snapshots) -> dict[str, QuotaReading]`, where each reading contains the canonical normalized document, tier, freshness and error. The caller can save Pi snapshots, collect after a run and collect for `/usage`. File/cache names remain identical to the shell.

- [ ] Write failing fake-probe tests for native success, CodexBar live fallback, cached fallback, Pi snapshot fallback, malformed payload, timeout and stale freshness.
- [ ] Run the focused test to observe the expected missing implementation failure.
- [ ] Port the shell's `fetch_*`, `use_*`, `save_*` and `collect_effective_quotas` process steps without duplicating normalization or tier policy.
- [ ] Run focused tests and the existing quota regressions; inspect fallback order and file permissions.

### Task 3: Feishu rendering and transport

**Files:** Create `quota_sentinel/runtime/feishu.py` and `quota_sentinel/runtime/cards.py`; use `quota_sentinel/notifications/plan.py`; test in `tests/python-feishu-regression.py`.

**Interfaces:** `render_task_card`, `render_usage_card`, `render_busy_card` take explicit provider results/readings; `FeishuClient.send(payload)` handles credentials, token acquisition, user lookup, retries and API errors. Rendered payloads are JSON serializable and preserve existing card layouts and fields.

- [ ] Write failing golden tests for single, two-column and stacked cards, failure text, monthly display, busy response and recovery deduplication.
- [ ] Run the focused test to observe the expected missing implementation failure.
- [ ] Port shell card builders and transport; use stdlib JSON/HTTP or the already locked SDK, preserving timeout and retry behavior.
- [ ] Run focused tests plus existing `card-v2-regression.py` and `feishu-regression.zsh`; inspect error logging for credential leaks.

### Task 4: Public application coordinator and locks

**Files:** Create `quota_sentinel/app.py` and `quota_sentinel/runtime/locks.py`; test in `tests/python-app-regression.py`.

**Interfaces:** `Application.check()`, `.run(providers)`, `.usage()`, `.status()` and `.wait()` coordinate Tasks 1-3 with `quota_sentinel.scheduler.service` and `quota_sentinel.state`. All mutating scheduler operations hold `run.lock`; quota collection holds `quota.lock`, and the latter is released before model execution.

- [ ] Write failing black-box tests with injected runner/probe/notifier/clock for due, no-due, retry recovery, busy `/usage`, post-run calibration, and independent provider failures.
- [ ] Run the focused test to observe the expected missing implementation failure.
- [ ] Implement coordinator phases A/B/C and parallel retry rounds from `check_schedule`, `run_retry_burst` and `run_selected_providers`.
- [ ] Run focused tests and compare against existing zsh scheduler regressions in isolated temp state directories.

### Task 5: CLI and daemon conversion

**Files:** Modify `quota_sentinel/__main__.py`, `feishu_listener.py`, `task_orchestrator.py`, `quota-sentinel.feishu-listener.plist.template`, `install-launchagents.sh`, `README.md`, `ARCHITECTURE.md`, `pyproject.toml` and `uv.lock`; test in `tests/python-entrypoint-regression.py`.

**Interfaces:** `uv run --frozen --no-sync quota-sentinel check|wait|run|usage|status` and card/user commands map to the application coordinator. The listener and orchestrator launch this CLI without `/bin/zsh`.

- [ ] Write failing entrypoint and LaunchAgent template tests, including foreign working directory startup and Python version floor.
- [ ] Run focused tests to observe the expected unsupported-command and stale-shell-path failures.
- [ ] Wire the CLI and daemon, update setup templates/docs, and replace Shell implementation with a thin compatibility launcher only after Python black-box parity is covered.
- [ ] Run all Python and zsh tests, resolving tests that source the retired Shell implementation by replacing each assertion with equivalent Python coverage.

### Task 6: Live repository migration

**Files:** Move the repository directory, including `.git` and ignored local files, from the Shell path to `Script/Python/quota-sentinel` (the migration target on this host); regenerate the local uv environment and rendered LaunchAgent files.

- [x] Verify all tests and the new CLI on a temp state, `git diff --check`, and git status before moving.
- [ ] Confirm the live authority manifest's ownership from a trusted source; restore it or obtain the operator's explicit verified pre-protocol assertion if absent. Do not infer legacy from missing JSON files.
- [ ] Stop the old LaunchAgent, move the checkout atomically on the same filesystem, sync uv from the lock, render and load the new agent.
- [ ] Verify new plist paths, `launchctl` state and CLI status; ensure no active service still points to the Shell directory.

---

## Shell retirement: coverage mapping

`quota-sentinel.sh` and the fifteen suites that drove it were deleted in this
change. The remaining `tests/*.py` suites are the contract. Earlier task steps
above that say to run a `*.zsh` suite or a `*-native-regression.py` file refer
to files that no longer exist; this section supersedes them. This table is the
audit trail: it was built by reading BOTH sides of every claim (the retiring
suite and the named Python suite) rather than by matching names, so it also
records where the original coverage map was wrong.

The retirement rule applied per file: remove the shell-parity portion, keep
every Python assertion, and where a behaviour had no Python counterpart, port
it into the closest Python suite instead of dropping it silently. Ported in
this change: the two live native-tier helpers (verbatim, into
`tests/python-quota-adapter-regression.py`), the near-movement and
earlier-movement deadline rewrites plus the service-level matured-deadline
sequence (into `tests/python-scheduler-regression.py`), and the
lock-refusal / lock-release / missing-manifest-remedy / read-purity assertions
(into `tests/python-authority-regression.py`). The app-level burst engine was
added to `tests/python-app-regression.py` by its owner in the same round.

| Deleted file | What it tested | Python suite that now covers it | Behaviour left uncovered |
| --- | --- | --- | --- |
| `tests/schedule-regression.zsh` | per-provider deadline read/write, aggregate due = min, next-due file mode, `fallback_due`, run-lock and quota-lock acquire/busy/release | `python-scheduler-regression.py` (fallback, min due), `python-state-store-regression.py` (file modes), `python-authority-regression.py` + `python-runtime-lock-regression.py` (both locks, pid semantics) | lock-file mode 0600 is not asserted (minor) |
| `tests/dynamic-schedule-regression.zsh` | 13 cases: fallback seeding, fresh replacement, same-window jitter, invalid future resets, reset-buffer blocking, cache staleness, `/usage` sync, legacy migration | `python-scheduler-regression.py` — including the newly ported near-movement deadline rewrite (`next_due == new reset + 240`), the earlier-movement rewrite, and calibration-never-touches-`last_task`/`last_window` | monolithic/unsuffixed legacy migration is a deliberately removed feature (see *Left uncovered* #9); opencode-specific calibration is only implicit (the policy is provider-generic and monthly-ignored is asserted); `scheduler-sync` rc=2 is not asserted at CLI level |
| `tests/p1-deadline-regression.zsh` | A1-S: matured-debt protection, deadline movement in both directions, far-reset promotion, reset-buffer blocking, at-least-once | `python-scheduler-regression.py` (matured debt, near/far movement; newly ported earlier-movement rewrite and service-level matured → initial burst → commit → fresh calibration), `python-app-regression.py` (fresh while debt pending cannot move the deadline; burst sequences) | `scheduler-sync` exit-code contract (rc 2 blocked / non-zero deferred) not asserted; `/usage` never marks a task is asserted at the policy level only |
| `tests/retry-regression.zsh` | RY1-RY18: multi-round bursts, stop-at-first-success, exhaustion, timeout-as-failure, watchdog repayment, provider isolation, masked error strings | `python-app-regression.py` (initial/watchdog bursts with limits, exhaustion, timeout-in-burst, two-pending-providers-in-one-tick, recovery, one-failing-provider isolation), `python-model-runner-regression.py` (rc=124, redaction, the pinned masked-error string), `python-scheduler-regression.py` (service-level sequence) | the `exhausted N attempts` log line does not exist in the port (the behaviour is asserted; the log wording was not carried over); RY15's wall-clock (<3s) bound; RY17's opencode-specific deadline assertion |
| `tests/robustness-regression.zsh` | R1-R7: CodexBar timeout → cache, orphan reaping, mixed timing-out/succeeding run, `/usage` busy, capturedAt normalisation, cache stability, legacy migration, temp-dir mode | `python-quota-probe-regression.py` (timeout → cache, descendant reaping), `python-quota-adapter-regression.py` (the full capturedAt table), `python-json-store-regression.py` (atomic writes, modes), `python-app-regression.py` (busy usage, mixed provider outcomes) | monolithic legacy migration (removed feature); whole-run end-to-end elapsed bound; `/usage` busy "leaks no internals" + byte-identical scheduler state; repeated-read capturedAt identity; workspace temp dir 0700 |
| `tests/quota-regression.zsh` | Pi fixture normalisation for all three providers + exact rendered quota-message text | `python-quota-adapter-regression.py` (fixtures byte-exact, jq parity) | the exact `quota_*_message` text (source line, bar, `距离重置：H小时 M分`, `重置时间：… CST`) — the renderer is untested, and `format_reset_time` / `_duration` have no assertion anywhere |
| `tests/feishu-regression.zsh` | envelope, v1 card, notification markers, provider card sections, cached/Pi warnings, charts, preview, lookup, readiness | `python-feishu-regression.py` (envelope, `/usage`, cached warning, lookup, readiness), `python-notification-regression.py` (plan + purity) | Pi-snapshot `⚠️ 可能不是最新` branch; v1 header green/title/hr/note and the literal `发送失败` → red rule; weekly chart `#54A6FD`/0.86; full v2 label/duration rows; `↳` / `来源` tree sweeps |
| `tests/card-v2-regression.py` | C1-C13 v2 card geometry | `python-feishu-regression.py` (schema, two-column layout, clamps, monthly-once, red failure) | `chart_spec` structural fields (`type`, `bandWidth`, zero padding, no `track`, `progress.style.*`); weekly chart colour; no-two-consecutive-`hr`; monthly-absent; opencode-failure-red; ordered block titles; right-column trailing `hr`; literal `\n` sweep |
| `tests/state-authority-regression.zsh` | AB1-AB12: absence is UNKNOWN, explicit bootstrap, cutover crash matrix, rollback pure-undo, lock-blind internal reproducer | `python-authority-regression.py` (A/B/C/D/M sections; newly ported: public `cutover`/`rollback` refuse under a live `run.lock` with a whole-dir byte signature, the failure path still releases the lock, and every read against an uninitialized deployment prints the `bootstrap-authority --assume-legacy` remedy) | `bootstrap-authority --force` (unknown arg) is not asserted (argparse rejects it; no test pins that) |
| `tests/state-concurrency-regression.zsh` | SU-M1-M4, U1-U4, C1/C2, B1: migration idempotence and races, `/usage` locking, candidate promotion | `python-json-store-regression.py` (migration idempotence, seed races, TOCTOU), `python-state-cutover-regression.py`, `python-scheduler-regression.py` (candidate logic), `python-authority-regression.py` (newly ported help/read create nothing) | whole-dir byte+mtime purity signature across all getters; run.lock-busy `/usage` (sync skipped, no write, card still sent); lock-order trace; `/usage` × writer interleaving; cross-process candidate promotion; CLI `migrate` stdout; `json-dump` == `dump` at CLI level; `status` value equality |
| `tests/state-store-parity-regression.zsh` | SP1-SP5: shell↔Python per-slot parity, read purity, `status` formatting, pathological slot values | `python-state-store-regression.py` + `python-json-store-regression.py` (every Python-side verdict and backend agreement) | all shell↔Python parity directions die with the shell by construction; CLI-level read purity and `dump`/`json-dump` equality are the portable residue (see above) |
| `tests/installer-upgrade-regression.zsh` | I1-I9: installer `launchctl` order, uv gate, manifest validation/refusal, render-only, no self-heal | `tests/python-installer-regression.py` (added in the same round, 8 tests: a fake repo with fake `uv`/`launchctl` and `HOME` in a temp dir) plus the static text audits in `python-architecture-audit-regression.py` AR12/AR12b/AR12c/AR12d/AR15, `python-entrypoint-regression.py` E9, `uv-project-regression.py` UV1/UV2/UV6/UV10 | plist `plutil -lint` failure path; the corrupt-manifest refusal *text*; the never-loaded-label `bootout` idempotence message (see *Left uncovered* #1) |
| *(replacement added in the same round)* `tests/python-installer-regression.py` | the installer's behaviour: render-only renders all three agents and never calls `launchctl`; a failed `uv sync --locked` gates the whole install; a missing manifest refuses before any render/load and never creates the manifest; an existing manifest is never rewritten and never cut over; `--load` retires the two legacy labels BEFORE bootstrapping the listener; rendered agents run the Python console script (no shell, `--frozen`/`--no-sync`, `WorkingDirectory /private/tmp`); an unknown argument is a usage error (rc 2) | — (this row *is* the coverage) | the residual paths listed in the row above |
| `tests/antigravity-native-regression.py` | 13 helper-level tests of the **live** `antigravity_usage.py` + 2 shell-tier tests | `python-quota-adapter-regression.py` — `AntigravityNativeHelperTests` (12 tests ported verbatim); the shell-tier equivalents are in `python-quota-probe-regression.py` and `uv-project-regression.py` UV7 | the antigravity-vs-codex CodexBar timeout differential (`QUOTA_SENTINEL_ANTIGRAVITY_CODEXBAR_TIMEOUT` being larger than Codex's) is not asserted |
| `tests/opencode-native-regression.py` | 12 helper-level tests of the **live** `opencode_usage.py` + 3 shell-tier + the outer-budget AST test | `python-quota-adapter-regression.py` — `OpencodeNativeHelperTests` (all helper-level tests plus the outer-budget test, 12 total); stdin-not-argv in `python-quota-probe-regression.py` | exact helper argv `--curl <bin> --timeout 15`; the collector's CodexBar `--provider opencodego --source api` mapping; the empty-key tier-skip assertion |
| `tests/cache-and-loopback-regression.py` | 2 CodexBar-cache tests + 3 loopback tests | `python-quota-probe-regression.py` (cache write/read with capturedAt preserved; covered before this change) | none for the cache half. The three loopback tests were **vacuous**: they defined their own `validate_port`/`build_loopback_endpoint`/host filter and asserted on those; no loopback or `LanguageServerService` code exists anywhere in the repo (removed in commit 0c80bee) |

### Left uncovered

Honest list of behaviour that had coverage before this change and does not now.

1. **Installer residuals.** `tests/python-installer-regression.py` now covers
   the installer's behaviour (render-only being `launchctl`-free, the failed
   `uv sync --locked` gate, missing-manifest refusal and no self-heal, never
   rewriting an existing manifest or cutting over, the `--load` label order,
   the rendered agents, and usage errors). Three paths are still unasserted:
   a `plutil -lint` failure while rendering (the refusal when a rendered plist
   is invalid), the corrupt-manifest refusal *text* (the refusal itself is
   covered, its message is not), and the message for a `bootout` of a label
   that was never loaded (idempotence is exercised, not asserted).
2. **Card and message rendering text.** The exact `quota_*_message` body
   (`来源：`, the bar, `距离重置：H小时 M分`, `重置时间：… CST`), `format_reset_time`
   and `_duration`; the Pi-snapshot warning branch; the v1 text card's header
   colour/title/`hr`/note and the literal `发送失败` → red rule; the weekly
   chart colour `#54A6FD` and value 0.86; `chart_spec` structural fields; v2
   window label/duration rows; no-two-consecutive-`hr`; the monthly line's
   absence; opencode-failure red; ordered three-provider block titles; and the
   full-tree sweeps for `↳`, `来源`, and literal `\n`.
3. **Native-helper shell wiring differentials.** The antigravity CodexBar
   budget default, the exact opencode helper argv, the collector's CodexBar
   provider/source mapping, and the empty-key native-tier skip.
4. **State-concurrency residue.** A whole-dir byte+mtime purity signature
   around every getter; `/usage` while `run.lock` is held; the lock-order trace
   (one non-blocking `run.lock` attempt strictly after `quota.lock` release);
   `/usage` interleaved with a `run.lock`-holding writer; candidate promotion
   driven from on-disk state in a fresh process; CLI `migrate` stdout;
   `json-dump` == `dump`; and `status`'s displayed next-due equalling the
   store's value.
5. **Exit-code contracts at the CLI edge.** `scheduler-sync` rc=2 (blocked) /
   non-zero (deferred) is asserted nowhere; the policy-level `SyncAction` is.
6. **Timing and mode bounds.** The parallel-repayment wall-clock bound
   (<3s), the whole-run end-to-end bound (<15s), workspace temp-dir mode 0700,
   and lock-file mode 0600.
7. **`/usage` busy hygiene.** That the busy reply leaks no lock names/PIDs and
   that scheduler state is byte-identical afterwards (only "no probe ran" and
   the busy event are asserted).
8. **The `exhausted N attempts` log line.** Behaviour is asserted; the log
   wording does not exist in the port at all, so it is not a test gap so much
   as an un-ported log string.
9. **Monolithic → per-provider legacy migration — deliberately removed, not
   lost.** The retired shell's `migrate_legacy_state` bootstrap only mattered
   for a deployment still on the pre-per-provider files (`last-task-at`,
   `next-due-at`, `last-triggered-window` with no provider prefix). No Python
   code reads unprefixed files, and the Python `state/migration.py` +
   `migrate` verb are the later per-provider-slots → v1-JSON migration
   (covered by `python-state-cutover-regression.py` and
   `python-json-store-regression.py`). A host still on monolithic files must
   run the retired shell implementation once, from the branch point in git
   history, before using the port. This is recorded as a feature removal.

### Where the original coverage map was wrong

* `retry-regression.zsh` "→ python-app-regression.py (retry rounds, debt,
  recovery)" was **optimistic**: at verification time no Python test used
  `initial_attempts > 1` or `watchdog_attempts > 1`, so the entire multi-round
  burst engine (stop-at-first-success, exhaustion, timeout-as-failure,
  watchdog fail-then-success) was uncovered. Ported: the app suite gained the
  burst assertions, and the scheduler suite gained the service-level sequence.
* `dynamic-schedule-regression.zsh` / `p1-deadline-regression.zsh` "→ covered":
  the same-window jitter's **deadline rewrite** and the **earlier-movement**
  rewrite were not asserted (Python pinned only `last_known_reset`/anchor
  bookkeeping). Ported into `python-scheduler-regression.py`.
* `robustness-regression.zsh` "→ python-app / python-model-runner /
  python-runtime-lock / python-json-store": the claim named the wrong suites.
  R2/R5/R6 are covered by `python-quota-probe-regression.py`,
  `python-quota-adapter-regression.py` and `python-state-store-regression.py`
  respectively; the items in *Left uncovered* were genuinely unasserted.
* `antigravity-native-regression.py` / `opencode-native-regression.py` "→
  python-quota-probe-regression.py" was **wrong**: that suite stubs both
  helpers with fake binaries, while the real `antigravity_usage.py` /
  `opencode_usage.py` are live production code that `quota_probe` executes and
  whose stderr reason prefix it parses. The helper-level tests were ported
  verbatim into `python-quota-adapter-regression.py`.
* `quota-regression.zsh`, `feishu-regression.zsh`, `card-v2-regression.py`
  "→ covered" was **partly** wrong; only the rendering-text items in *Left
  uncovered* #2 remain.
* `installer-upgrade-regression.zsh` "→ NOTHING YET" was **correct at the
  time**: the installer's behaviour had no Python coverage. It is now covered
  by `tests/python-installer-regression.py`, added in the same round by the
  port owner, with the three residual gaps listed under *Left uncovered* #1.
  The retirement work itself did not invent a replacement test (per
  instruction) — it reported the gap, and the owner closed it.
* `state-store-parity-regression.zsh` was **safe to delete**: every parity
  direction it asserted requires both implementations, and the Python-side
  verdicts already live in the state-store/json-store suites.
* `cache-and-loopback-regression.py` was **safe to delete**: the cache half is
  covered by the probe suite, and the loopback half tested code that no longer
  exists.
