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
  `run.lock`), full validation + final-bytes serialization before any
  filesystem operation, then ONE temp/fsync/atomic-replace publish.
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
