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
| 7 | bootstrap `migrate_legacy_state` (main, state-touching commands only) | seeds absent per-provider files from legacy single-provider files | none — idempotent, writes only files that do not exist, legacy sources are never consumed |

Reads (`read_provider_*`) are pure: no lazy migration, no repair writes. The
legacy upgrade runs once at command entry (#7).

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
