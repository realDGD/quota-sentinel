# Architecture

Short reference for the invariants this project relies on. Tests enforce the
behavioral parts; this file names the boundaries so future changes keep them.

## Ownership

```text
task_orchestrator.py          = when
  watchdog, precise deadline wake, sleep recovery, durable run history
  (task-orchestrator.sqlite3 — observability only)

quota-sentinel.sh             = scheduling policy / how
  deadline calculation, Fresh/Stale, retry debt, reset trust gate,
  candidate/anchor confirmation, provider execution

provider state files          = scheduler authoritative state
task-orchestrator.sqlite3     = history / observability, never authoritative
```

Authoritative scheduler state must never move into the SQLite history DB.
`task_orchestrator.py` reads state files for wake scheduling but does not own
them.

## Provider capability vs scheduler policy

Provider capability (per adapter): the quota native source, fallback chain,
and the set of quota windows (OpenCode additionally has a monthly window that
is display-only and never participates in deadline math).

Scheduler policy (shared, provider-generic): Fresh/Stale authority, the reset
trust gate, candidate-reset confirmation, the reset anchor and near-movement
accumulation guard, matured-debt preservation, reset-buffer blocking, and
retry debt. These live in `sync_provider_deadline_from_quota` /
`evaluate_provider` and apply identically to every provider. Do not express
them as per-provider capability flags.

## Lock responsibilities and order

```text
run.lock     = model execution + the serialization boundary for all
               authoritative scheduler-state mutations
quota.lock   = quota acquisition serialization (probes, caches, snapshots)
```

Sanctioned nesting order: **run → quota** (a holder of run.lock may take
quota.lock). **Never wait for run.lock while holding quota.lock** — that
inverts the order and can ABBA-deadlock with `check_schedule`. All run.lock
acquisition from opportunistic paths (e.g. `/usage`) is non-blocking: busy
means skip, never queue.

`quota.lock` is deliberately NOT a scheduler-state lock: the retry phase of
`check_schedule` commits state while holding only run.lock, so that a quota
probe can never block debt repayment.

## Persistence invariant: at-least-once

```text
duplicate execution   acceptable
lost execution        NOT acceptable
```

Crash and ambiguity must resolve toward re-running, never toward silently
dropping a due task. The mechanisms that encode this:

* `retry_pending=1` is written *before* the first attempt, so a crash
  mid-burst leaves a repayable debt.
* Failure never advances `last_task_at` and never re-seeds the fallback
  deadline.
* A matured deadline is a committed debt: fresh quota data may not move it
  into the future in the same round.
* `commit_provider_success` orders its writes so any crash point leaves the
  provider looking due-again (re-run), not done-but-never-run.
* The candidate/anchor pair shares write ordering such that a crash can lose a
  promotion opportunity but can never fabricate a trusted reset.

Any refactor that collapses these writes (e.g. a future one-document state
store) must preserve the direction: reachable-after-crash states may demand
extra work, never permit skipping due work.

## Scheduler state slots (8 per provider)

```text
last_attempt_at   last_task_at        next_due_at          retry_pending
last_known_reset  last_triggered_window
reset_anchor      reset_candidate
```

`reset_candidate` is transient: the file's absence means "no candidate
pending". A future JSON state document must represent that explicitly
(`"reset_candidate": null` for a definite no-candidate; a *missing key* is a
schema/migration error, never business state).

## State write-path registry

Every path that writes authoritative scheduler state, and the lock it must
hold while doing so. **New writers must be added here and covered by a
regression case.**

| # | Write path | State written | Locks at write time |
|---|---|---|---|
| 1 | `check` → Phase A `run_retry_burst` → `commit_provider_success` | attempt, task, retry_pending, next_due, anchor/candidate (clear) | run |
| 2 | `check` → Phase B quota collection | (quota caches only, not scheduler state) | run + quota |
| 3 | `check` → Phase C `evaluate_provider` → `sync_provider_deadline_from_quota` / fallback seed | last_known_reset, next_due, anchor, candidate | run + quota |
| 4 | `run` → initial `run_retry_burst` | as #1 | run |
| 5 | `run` → post-run sync (`last_window` + sync) | last_window + as #3 | run + quota (quota busy → sync skipped, fallback stands) |
| 6 | `usage` → opportunistic sync after collection | as #3 | quota (collect) → **released** → run (non-blocking; busy → skip) |
| 7 | bootstrap `migrate_legacy_state` (main, state-touching commands only) | seeds absent per-provider files from legacy single-provider files | none — via `seed_provider_state_file` (temp + `link(2)` EEXIST publish): creation is atomic and non-destructive, so a cold-start migration racing a live writer can only skip, never clobber; legacy sources are never consumed |

Reads (`read_provider_*`) are pure: no lazy migration, no repair writes. The
legacy upgrade runs once at command entry (#7).

> Note (Phase 2): shadow JSON document creation (`python3 -m
> quota_sentinel migrate`) is deliberately NOT in this registry — it does
> not write authoritative state. It needs no lock because it is
> seed-if-absent only: it can create a missing shadow document and can
> never overwrite any document, authoritative or otherwise. If/when JSON
> becomes authoritative, its writer paths join this table.

## Python state store (strangler Phase 1)

`quota_sentinel.state` provides the first Python-side view of scheduler
state:

* `ProviderState` models the eight slots structurally (transient
  `reset_candidate` as an explicit `None`-able value); models know no file
  names and do not re-validate what the persistence boundary checks.
* `FileStateStore` reads and writes exactly the shell's per-provider file
  layout. `load()` is pure — a missing, unparsable, or corrupt-encoding
  slot reads as `None`, unsets only itself, and is never repaired.
* `commit(old → new)` is plan-then-execute: the stale check, the provider
  name validation, the legality of every requested change (only
  `reset_candidate`/`reset_anchor` may be cleared) and the SERIALIZATION
  of every value to its final UTF-8 bytes complete BEFORE the first
  filesystem mutation. An invalid transition leaves the disk
  byte-for-byte untouched — the publish stage can fail only on
  filesystem errors, never on business values. Values are validated
  explicitly (no `assert` on persistence paths — `python -O` must not
  weaken them) and `bool` is rejected wherever an epoch int is required.
* The store owns the persistence-directory invariant on its WRITE path
  only: `commit` creates/normalizes `state_dir` to mode 0700 before its
  first publish and writes files 0600; `load` never creates anything and
  a no-op commit publishes/creates nothing.
* **The `old_state` stale check is defensive misuse detection only.** It
  is NOT a compare-and-swap and NOT cross-process serialization: a
  writer that mutates disk after the check but before the publishes will
  be silently overwritten (`test_stale_check_is_defensive_not_cas` pins
  this). **Every future production StateStore writer must execute inside
  the existing `run.lock` serialization boundary.** No such writer is
  wired today, which is why none is listed in the write-path registry.
* The canonical publish order mirrors `commit_provider_success` with
  `next_due_at` last, so crash prefixes of the SUCCESS transition stay
  at-least-once directional — and that is currently all the order is
  proven for. Other scheduler transitions (generation init, far-reset
  promotion, retry-debt creation/repayment, candidate lifecycle) must be
  individually reviewed and crash-tested before being moved behind
  `commit()`; the global slot order must not be assumed to cover them.
* Only ONE shell path is wired through it today: the `status` next-due
  display (`status_next_due`), a read with the shell getter as automatic
  fallback. Scheduler policy, quota logic and **every state write** still
  run in the shell unchanged.
* Shell↔store agreement holds for every value project writers produce.
  Two pathological-content divergences (multi-colon `reset_candidate`
  where the shell slices first:last, and zero-padded epochs where the
  shell echoes raw bytes) are deliberate strictness/canonicalization
  decisions, enumerated case by case in
  `tests/state-store-parity-regression.zsh` (SP4) and the store's Python
  suite; the parity wording is intentionally not "100% slot-for-slot on
  arbitrary bytes".
* `task_orchestrator.ScheduleState` is deliberately NOT unified onto the
  store yet: it owns cross-provider aggregation (min due, exclude
  pending, snapshot, roster) and its epoch parser is looser than the
  shell's (`int()` accepts `+5`, `5_0`, padded digits). The contract the
  future unification must preserve is pinned by
  `ScheduleStateContractSpecTests`; wiring changes wait for their own
  phase.

## JSON document backend (Phase 2 — shadow representation)

`quota_sentinel.state.schema` + `json_store` + `migration` add a second
durable representation: one `<provider>-state.json` per provider.

**Ownership during Phase 2 (explicit):** the shell's per-slot files remain
the AUTHORITATIVE runtime state — every scheduler write, read and decision
uses them. JSON documents are SHADOW snapshots created only by the
explicit bootstrap `python3 -m quota_sentinel migrate` (library:
`migration.migrate_all`). They are NOT re-synced when the shell writes
after seeding: a shadow document freezes the legacy state at migration
time (later drift is expected and harmless because nothing authoritative
reads the shadow yet). Declaring JSON authoritative requires moving
writer ownership — a later phase, registered in the write-path table at
that moment. `status` still reads slot files via `FileStateStore`.

Schema v1 (`schema.py`):

* flat document, `schema_version: 1` plus all eight slot keys ALWAYS
  materialized;
* explicit `null` = business unset; **missing key = corruption**;
  **unknown key = corruption**; unsupported/absent version = refusal,
  never guessing;
* strict types matching the persistence boundary: plain non-negative ints
  (bool excluded), plain bool `retry_pending`, single-line non-empty
  window string or null, `reset_candidate` null or exactly
  `{reset_at, observed_at}`;
* deterministic bytes (sorted keys, fixed layout, strict UTF-8) so
  serialize is reproducible and unrepresentable text fails pre-filesystem.

`JsonStateStore`:

* `load` pure and LOUD: absent document → `MissingStateDocumentError`
  (absence is bootstrap state, never defaulted); corrupt bytes/JSON →
  `DocumentCorruptError`; schema violation → `SchemaError`. A whole
  authoritative document is never silently read as all-unset, and nothing
  is auto-repaired;
* `commit` = whole-document transaction: defensive stale check (NOT a
  CAS — same rule as the file store: production writers must run inside
  `run.lock`), full validation + final-bytes serialization before the
  first filesystem mutation (the stale check reads; reads are pure),
  then ONE temp/fsync/atomic-replace publish.
  Business-value failure ⇒ old document byte-identical; crash or
  filesystem failure ⇒ readers see old-complete or new-complete, never
  half JSON. That single-unit atomicity is precisely what the per-slot
  file backend could not offer;
* write-side directory ownership identical (0700 dir, 0600 doc, created
  only on write paths);
* `commit` never creates a document: creation belongs to migration.

Migration (`migration.py`): reads legacy strictly through
`FileStateStore` (no second parser; unset-on-garbage semantics carry
over, nothing "repaired"); publishes via seed-if-absent
(`os.link`/EEXIST — mirrors the shell's `seed_provider_state_file`), so
JSON that exists always wins and a race can only skip. Reruns are
no-ops. Legacy slot files are never moved/deleted: rollback = stop
reading JSON. `DEFAULT_PROVIDERS` must equal the shell's `PROVIDERS`
roster — asserted against the real array by the parity suite (SP5).

Crash-proof scope is unchanged by this phase: the at-least-once
crash-prefix proof covers the success transition; and while whole-document
atomicity removes multi-slot partial-commit risk for FUTURE migrated
transitions, each transition still needs individual review and behavior
tests before its writer moves behind `commit()`.

Later phases (scheduler decisions, quota adapters) grow out of these
seams one boundary at a time — see the migration order in the project
log, never a big-bang rewrite.

## Cutover preparation (Phase 3A)

`quota_sentinel.state.cutover.prepare_provider_cutover` refreshes a
provider's shadow JSON from the CURRENT authoritative legacy state and
verifies semantic equality. It is preparation for a future cutover, not
the cutover itself.

Source of truth is absolute: the legacy slot files as read at call time.
A pre-existing shadow document — stale, all-null or corrupt — is
replaced, never trusted, never timestamp-compared and never "resolved"
against legacy (there is deliberately no conflict-resolution algorithm).
This is the exact opposite of Phase 2 `migrate_provider`
(seed-if-absent, existing JSON wins) and the two operations must not be
conflated:

| | migrate (Phase 2) | prepare (Phase 3A) |
|---|---|---|
| existing JSON | always wins, skip | replaced if semantically different |
| corrupt JSON | blocks seeding | rebuilt from legacy |
| purpose | freeze a shadow snapshot | make the shadow current |
| requires run.lock context | no (skip-only) | YES (precondition, see below) |

Contract:

* PRECONDITION: the caller already holds `run.lock` (serialization
  against authoritative writers). The primitive does not and must not
  verify or fake it: a lock file existing does not imply the caller owns it.
  Mechanical proof comes with Phase 3B wiring, where the caller is
  the lock holder.
* Validation pipeline (single rule source, no drift), one contiguous
  contract phrase: validate_state -> state_to_document -> validate_document -> deterministic UTF-8 bytes — the whole preflight completes before the first filesystem mutation. (Pure READS of legacy and shadow happen earlier, by design; "before any filesystem operation" would be false.)
* POSTCONDITION on success: legacy slot files byte-identical; the
  document decodes (through the ordinary loud loader) to EXACTLY the
  legacy ProviderState; idempotent re-runs write nothing.
* Failure contract — the safety invariant is "legacy stays untouched
  and authoritative", NOT "the shadow keeps its old value". Branch by
  whether the publish itself succeeded:
  1. in every failure, legacy remains untouched and authoritative;
  2. in every failure, the shadow never becomes authoritative;
  3. failure before the successful publish (domain/schema/serialization,
     or a filesystem failure inside the atomic replace): the shadow
     remains old-complete or absent;
  4. failure AFTER a successful publish (verification mismatch): the
     shadow may hold the newly-published complete document — that is
     not an ownership change and needs no rollback (do NOT add shadow
     rollback to satisfy older wording);
  5. no failure may ever leave torn JSON;
  6. rollback of authoritative ownership is unnecessary, because
     ownership never changed from legacy.

Phase 3A PREPARED is NOT JSON AUTHORITATIVE. Ownership after a
successful prepare is exactly ownership before it: legacy files
authoritative, JSON a semantically-current shadow. No scheduler
transition executes here; the at-least-once semantics of the shell are
untouched. Phase 3A only prepares a semantically current JSON document;
it does not alter scheduler execution semantics.

Authoritative JSON load-failure policy (binding on Phase 3B, when
production reads may start hitting JSON errors): the three typed
failures — MissingStateDocumentError, DocumentCorruptError, SchemaError —
must FAIL CLOSED on state mutation: never default an unreadable document
to ProviderState(), never mark work completed, never advance a deadline,
never drop a retry debt or pending candidate; surface loudly and keep
the existing durable state intact. Silent defaults are the one failure
mode that directly violates at-least-once. Recovery mechanics (who
rebuilds from what, how operators intervene) are Phase 3B design work;
the prohibition is absolute now.

Durability scope, stated precisely: Atomic-visibility guarantees cover process crash and concurrent readers (temp + fsync + atomic rename — readers always see old-complete or new-complete). Power-loss durability (post-rename persistence after a hard reboot: parent-directory fsync, macOS F_FULLFSYNC) is NOT claimed and NOT implemented. Making JSON
authoritative does not by itself change this envelope — today's slot
files have exactly the same property — but if a future design requires
power-loss durability, it needs evidence and its own design review
first, not an opportunistic fsync pile-on.

Phase 3B ownership-switch design questions (must be answered BEFORE any
reader/writer flips; no production switch was implemented in 3A):

1. Which durable fact expresses "JSON is authoritative"? Candidate
   designs must be argued, not assumed — adding a new state file
   (backend-owner / cutover-complete flag) is disallowed without
   justifying against double-truth risk, atomicity, recovery, and
   versioning. An alternative with no new state: keep legacy slot files
   as the marker itself (their presence/absence or content IS the
   signal) — to be evaluated.
2. How are reader and writer prevented from disagreeing mid-switch
   (reader=JSON/writer=legacy or the reverse)? Likely answer: both
   flips happen inside one run.lock-held critical section, derived per
   invocation from durable facts, never cached across processes.
3. Switch order: refresh-and-verify (this phase's prepare) must be
   proven current INSIDE the same locked section that flips the writer.
4. Crash after any partial point of the switch: recovery must be
   mechanically derivable from durable state alone (idempotent prepare +
   legacy-readable-until-final-step).
5. Rollback path: while legacy files remain present, byte-complete and
   still-written, rollback = flip derivation back; this is why 3B must
   NOT delete or freeze-consume legacy files.
6. When do legacy files stop being authoritative (final writer flip)?
7. When — if ever — may legacy files be deleted? (Answer is very likely
   "not in Phase 3B"; removal needs its own reviewed step.)

## Python runtime / dependency ownership (Phase 3A.5)

Packaging/runtime only: the same code and behavior, now with a managed
project environment. No scheduler, state, or ownership semantics live here.

```text
pyproject.toml   = project metadata + direct dependency source of truth
uv.lock          = committed resolution (reproducible; verified via uv lock
                   --check and uv sync --locked)
uv               = environment/runtime manager for the PROJECT class below
PEP 723          = retired: no inline metadata blocks remain anywhere
Homebrew/system
Python           = NOT a dependency owner for the project package
requires-python  = >=3.9, an evidence floor (system 3.9.6 runs every suite);
                   no .python-version pin: uv chooses the interpreter
```

Interpreter strategy — three deliberate classes, not one uniform rule:

| Class | Runner | Members | Why |
|---|---|---|---|
| A. project | `uv run --frozen --no-sync` | `feishu_listener.py` (+ in-process `task_orchestrator`), `quota-sentinel` console script / `python -m quota_sentinel`, Python test suites | the only third-party dependency (`lark-oapi`) lives here; daemon must never resolve/sync/network at start |
| B. isolated | `uv run --offline --no-project --no-config python -B …` | `antigravity_usage.py` | deliberate supply-chain boundary: must stay outside the project even now that a root pyproject exists — `--no-project` is load-bearing and pinned by tests/antigravity-native-regression.py |
| C. system | `/usr/bin/python3` (`PYTHON3_BIN`, retained) | `run_with_timeout.py`, `opencode_usage.py`, native-probe python check, `python -m quota_sentinel` status seam | stdlib-only, invoked on shell/scheduler hot paths; must not gain uv startup latency, cache, or environment coupling; the >=3.9 floor keeps class C and the uv project env behaviorally identical for this code |

LaunchAgent lifecycle: setup phase (`install-launchagents.sh`) verifies uv
and runs `uv sync --locked` — the ONLY network-capable step; the agent
runtime then runs `uv run --project <repo> --frozen --no-sync` (explicit
`--project` because launchd's `WorkingDirectory` is `/private/tmp`, which
would not discover the repo; `--frozen` forbids lock re-resolution,
`--no-sync` forbids environment mutation). A missing environment therefore
fails loudly at daemon start (err log) instead of self-healing — by design.
Cache: production uses the default user cache; `UV_CACHE_DIR` overrides are
test/sandbox-local only.

`quota-sentinel.sh` diff-zero for logic: the only allowed shell changes from
this phase onward are thin runtime call-throughs; none was needed (class C
stays system Python by choice, see table).

Registry / index policy (Phase 3A.5 residual): the committed `uv.lock` is
resolved exclusively from public PyPI (`pypi.org` /
`files.pythonhosted.org`). **Invariant: committed dependency metadata must
not depend on untracked user-local uv configuration** — a generator's
personal `~/.config/uv/uv.toml` mirror must never ride into the repository
(initial leak: user-local cernet-first index bled 330 registry lines into
the lock; relocked with `uv lock --no-config`, package versions verified
100% identical). User mirrors remain a legitimate *deployment-time*
override (`UV_INDEX`, personal uv.toml) and are not project policy.
`pyproject.toml` carries no `[tool.uv]` index config at all, which also
keeps the class-B antigravity isolation (`--no-project --no-config`) free
of any project registry influence. `tests/uv-project-regression.py` UV8
scans the committed lock and pyproject for mirror/private-registry/user-
path fingerprints and fails on drift.

Build backend reproducibility: uv does NOT record `build-system.requires`
in `uv.lock`, so an unconstrained `hatchling` would float a fresh resolve
on every editable build. The pin `hatchling==1.32.4` in `[build-system]`
is therefore load-bearing (UV9 pins its presence) — 1.32.4 is the version
this project has actually built with, verified by a clean-room
`uv sync --locked`. It stays a build dependency — never moved into runtime
`dependencies`.

Runtime sync contract: `--no-sync` means the daemon runtime never checks
or repairs a stale environment (UV10 proves the byte-signature of `.venv`
is stable across a runtime launch). Dependency change ⇒ the operator must
re-run `./install-launchagents.sh` (which does `uv sync --locked`);
runtime failing on a broken env is the designed fail-loud behavior, not a
bug to self-heal around.

## Shell freeze

`quota-sentinel.sh` is in functional freeze: bug fixes, compatibility fixes,
provider-specific model-runner adjustments, and thin call-through layers to new
Python adapters are allowed. New long-lived subsystems, new scheduler state
files, large new quota-parsing blocks, per-provider copies of the quota
pipeline, and new scheduler policy branches are not.

## Testing doctrine

The `.zsh` regression suites are the behavioral contract for the scheduling
layer; keep them passing as black-box tests when moving logic between layers
(strangler migrations preserve the entry-point API and its observable
contract). The Feishu card layout, log wording, and internal call order are
*not* part of the contract; tests assert on return codes, state-file
transitions, and dispatched message content only.
