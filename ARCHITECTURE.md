# Architecture

Short reference for the invariants this project relies on. Tests enforce the
behavioral parts; this file names the boundaries so future changes keep them.

## Ownership

```text
quota_sentinel/                = the system's brain (Python)
  state/                       authoritative scheduler state + backend authority
  scheduler/                   scheduler policy, transitions, decisions
  quota/                       provider quota adapters and normalisation
  __main__.py                  the CLI the shell and operators call

quota-sentinel.sh              = thin compatibility + system boundary (zsh)
  model runner                 provider CLI invocation, process groups, timeouts
  quota tier execution         the vendor probes, under the shared timeout helper
  Feishu transport, cards      rendering and delivery
  locks, install glue          run.lock / quota.lock, launchd-facing bootstrap

task_orchestrator.py           = when (wake scheduling + durable run history)
  task-orchestrator.sqlite3    = history / observability, NEVER authoritative
```

Authoritative scheduler state must never move into the SQLite history DB, and
must never exist in two places: exactly one backend owns it at any instant,
and a single durable fact says which one (see *State backend authority*).

The shell no longer holds scheduler policy. It owns processes, locks, model
execution, quota probing and notifications, and asks Python for every decision
and every transition. `tests/python-scheduler-regression.py` fails if a policy
value or a deadline algorithm reappears in the shell.

## Provider capability vs scheduler policy

Provider capability (per adapter, `quota_sentinel.quota.adapters`): the quota
native source, the fallback ladder, and the set of quota windows (OpenCode
additionally has a monthly window that is display-only and never participates
in deadline math).

Scheduler policy (shared, provider-generic): Fresh/Stale authority, the reset
trust gate, candidate-reset confirmation, the reset anchor and near-movement
accumulation guard, matured-debt preservation, reset-buffer blocking, and
retry debt. These live in `quota_sentinel.scheduler.policy` and apply
identically to every provider. Do not express them as per-provider capability
flags: `monthly_display_only` is a fact about OpenCode's vendor API, while
"only Fresh data may move a deadline" is a rule about ours.

## Lock responsibilities and order

```text
run.lock     = model execution + the serialization boundary for all
               authoritative scheduler-state mutations + the backend cutover
quota.lock   = quota acquisition serialization (probes, caches, snapshots)
```

Sanctioned nesting order: **run → quota** (a holder of run.lock may take
quota.lock). **Never wait for run.lock while holding quota.lock** — that
inverts the order and can ABBA-deadlock with `check`. All run.lock acquisition
from opportunistic paths (e.g. `/usage`) is non-blocking: busy means skip,
never queue.

`quota.lock` is deliberately NOT a scheduler-state lock: the retry phase of
`check` commits state while holding only run.lock, so that a quota probe can
never block debt repayment.

```text
run.lock ──▶ quota.lock        sanctioned
quota.lock ──✗──▶ run.lock     forbidden (ABBA with check)
```

No third lock may be introduced to shortcut either rule.

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
* The success commit is a single transition; on the document backend it is a
  single atomic replacement, and on the legacy backend its slot order keeps
  `next_due_at` last, so any crash point leaves the provider looking
  due-again (re-run) rather than done-but-never-run.
* The candidate/anchor pair changes such that a crash can lose a promotion
  opportunity but can never fabricate a trusted reset.

Any refactor that collapses these writes must preserve the direction:
reachable-after-crash states may demand extra work, never permit skipping due
work.

## Scheduler state slots (8 per provider)

```text
last_attempt_at   last_task_at        next_due_at          retry_pending
last_known_reset  last_triggered_window
reset_anchor      reset_candidate
```

`reset_candidate` is transient. A v1 document encodes that explicitly
(`"reset_candidate": null` for a definite no-candidate) and every key is
always materialized; a *missing key* is a schema error, never business state.
On the legacy backend the file's absence carries that meaning.

## State backend authority (Phase 3B)

Two durable representations of the same slots exist, and exactly one is
authoritative at any instant:

```text
legacy backend   <state_dir>/<provider>-<slot>       per-slot files
json backend     <state_dir>/<provider>-state.json   one v1 document per provider
```

**The single fact** is `<state_dir>/backend-authority.json`:

```json
{"backend": "json", "epoch": 1, "schema_version": 1}
```

* Atomic (temp-write/fsync/rename), versioned, and strict: an unknown version
  or backend is a loud error.
* **Absent manifest = this deployment was never cut over = legacy is
  authoritative.** That is a bootstrap rule with a mechanical trigger, not an
  inference from content.
* A *damaged* manifest is never defaulted to either side: `read_authority`
  raises, mutations fail closed, readers fail loudly. Guessing here would let
  a legacy writer create a second, diverging source of truth.
* `epoch` increments on every durable switch, so "the same authority" is
  distinguishable from "a different authority that looks similar".

`AuthoritativeStateStore` is the only authority → backend mapping in the
codebase. Production code never branches on the backend itself.

* Writers (`commit`) hold `run.lock`; the cutover holds the same lock, so a
  flip cannot interleave with a mutation. The router still re-reads the fact
  after committing and raises `ConcurrentAuthorityChangeError` if it moved —
  a precondition violation must be loud.
* Readers (`load`, `load_all`) mostly do NOT hold the lock (`status`, the
  timer's deadline scan, `/usage`). They use a generation guard: read the
  fact, read the state, read the fact again; accept only when both agree, else
  retry, else fail loudly. `load_all` applies the guard to the whole roster so
  a caller never mixes providers from two different backends.
* No caching layer exists: a long-lived process (the precision timer loops for
  days) must not pin a backend selection at start-up.

### Cutover and rollback

`cutover` (the explicit operator verb) runs under `run.lock`:

```text
read the durable fact            already json -> idempotent no-op
read CURRENT legacy state        the source of truth, never a frozen shadow
refresh + verify every document  one atomic publish each, semantic equality
re-read every document as a set
publish the authority fact       <- the single commit point
re-read the fact and confirm
```

The whole roster switches in ONE epoch; there is no state in which Codex is
JSON while antigravity is still legacy. Every crash prefix resolves
mechanically: before the flip the authority is still legacy (refreshed
documents are harmless shadows), and the flip itself is one atomic
replacement, so readers see the complete old or the complete new manifest.
`tests/python-authority-regression.py` injects a crash at every checkpoint,
including inside the manifest publish, and asserts that ownership is always
determinable, that no legacy byte ever changes, and that no torn document or
temp file survives. `tests/state-authority-regression.zsh` proves the
operator-visible lifecycle through the real shell.

Legacy files are **never deleted, moved or rewritten by the cutover**; after
the flip they are a rollback artifact and no production path reads or writes
them. The shell's legacy accessors are guarded: they `die` rather than touch
the retired backend once JSON owns the state.

`rollback` is the inverse switch and refuses unless it is a PURE UNDO (every
document still equal to its legacy file). Once JSON has advanced, "roll back"
would discard authoritative state — a human decision with a human-sized
backup, not an automatic one.

### Failure policy on the authoritative backend

```text
MissingStateDocumentError   no document for this provider
DocumentCorruptError        bytes are not UTF-8 JSON
SchemaError                 parsed but violates v1
```

None of these may be answered with a default `ProviderState()`. Mutations fail
closed, tasks are not marked complete, deadlines do not advance, and the
failure is loud. Recovery is deliberately minimal: restore the document (from
a backup, or by re-running the cutover from the untouched legacy files) —
there is no automatic repair subsystem, because a repair heuristic over the
scheduler's source of truth is exactly how a lost debt becomes invisible.

## State write-path registry

Every path that writes authoritative scheduler state, and the lock it must
hold while doing so. **New writers must be added here and covered by a
regression case.**

All entries funnel through `quota_sentinel.scheduler.service` →
`AuthoritativeStateStore.commit` → the selected backend.

| # | Write path | State written | Locks at write time |
|---|---|---|---|
| 1 | `check` → Phase A `run_retry_burst` | attempt + debt (`begin_attempt`), later attempts (`record_attempt`), success commit per provider | run |
| 2 | `check` → Phase B quota collection | quota caches only (not scheduler state) | run + quota |
| 3 | `check` → Phase C `scheduler-decide-all` | last_known_reset, next_due, anchor, candidate, fallback seed | run + quota |
| 4 | `run` → initial `run_retry_burst` | as #1 | run |
| 5 | `run` → post-run sync (`scheduler-last-window` + `scheduler-sync`) | last_window + as #3 | run + quota (quota busy → sync skipped, fallback stands) |
| 6 | `usage` → opportunistic sync after collection | as #3 | quota (collect) → **released** → run (non-blocking; busy → skip) |
| 7 | `cutover` → `scheduler-ensure-authority` | the authority manifest, after refreshing every document | run |
| 8 | bootstrap `migrate_legacy_state` (state-touching commands only) | seeds absent per-provider legacy files | none — `seed_provider_state_file` publishes by `link(2)` EEXIST, so creation is atomic and non-destructive; skipped entirely once JSON is authoritative |

Reads are pure: no lazy migration, no repair writes. `/usage` never runs a
model and never touches `last_task`/`last_window`; its opportunistic deadline
sync runs only when the scheduler is idle.

## Python scheduler (Phase 3C)

```text
quota_sentinel/scheduler/policy.py        pure transitions, no I/O
quota_sentinel/scheduler/models.py        Decision, SyncAction, QuotaObservation,
                                          Transition, RunOutcome
quota_sentinel/scheduler/observation.py   one normalised quota probe -> evidence
quota_sentinel/scheduler/service.py       the ONLY place policy meets a store
quota_sentinel/scheduler/cli.py           the bridge the shell calls
```

`policy` is `new_state = f(old_state, inputs, now)`. It performs no network
call, runs no model, takes no lock, touches no filesystem and never mutates a
second provider. It is a transcription of the shell policy that preceded it —
constants, branch order and tie-breaking are unchanged, and the zsh suites
remain the black-box compatibility proof.

Branch order is load-bearing and pinned by tests:

1. **matured debt protection** — a deadline that has matured is a committed
   obligation; a fresh probe arriving at due time may not re-anchor the window
   forward (live regression 2026-08-30: 18:11:35 → 23:15:38);
2. **fresh calibration** — otherwise a Fresh observation may move the deadline
   earlier or later, but only before the scheduled reset;
3. **no-quota fallback** — with still no usable deadline, seed
   `last_task + RUN_INTERVAL` and persist it.

Deadline calibration itself: establish the generation anchor from the first
Fresh reset, accept near-window movement inside the anchor tolerance while
never moving the anchor, and require two stable far observations before
promotion. The anchor is what closes the cumulative-near-movement loophole: a
sequence of individually-small movements cannot walk the deadline forward.

Transitions exist as first-class values, each individually proven:
`begin_attempt`, `record_attempt`, `commit_success`, `record_last_window`,
`sync_deadline` (7 branches), `evaluate_due`, `retry_blocked`.

`Transition.publish` names slots a transition must MATERIALIZE even when the
value is unchanged. The success commit uses it for `retry_pending=0`: the
pre-migration shell always left an explicit "no debt" file behind, and
"absent" and "0" being equal to every reader does not make changing the
on-disk contract acceptable.

`Transition.writes` (changed OR forced) is what decides whether the backend is
touched at all, so a no-op branch — Stale quota, reset-buffer blocking, an
unpaid debt — is provably non-writing rather than accidentally writing
identical bytes.

### Shell ↔ Python boundary

The shell calls the domain through `scheduler_bridge`
(`python3 -S -m quota_sentinel --state-dir …`), on the system interpreter
because the bridge's import graph is stdlib-only (pinned by
`tests/uv-project-regression.py`). `-S` skips site processing; nothing on this
path needs it.

Decisions are batched per phase — one interpreter start per roster phase, not
one per provider — because the precision timer walks this path every minute.
Batching shares the PROCESS; it never shares a transaction: each provider
still gets its own transition and its own commit.

The bridge prints machine records, not prose:

```text
provider=<name>                          the records that follow belong to it
change<TAB>p<TAB>slot<TAB>old<TAB>new    one durable slot changed
log=info|warn<TAB>text                   what to log, at which level
end=<name>                               the provider's records are complete
```

The Python side emits explicit change records precisely so the run log keeps
the shape it had when the shell diffed values itself. Verbs whose shell
predecessors logged nothing but the value diff emit no `log=` record.

Exit codes are part of the contract, because the shell decides with `if` and
`case`:

```text
scheduler-decide      0 due | 1 wait | 2 unpaid debt (skipped) | 4 error
scheduler-sync        0 applied | 1 no valid quota | 2 blocked | 3 deferred
scheduler-valid-reset 0 found | 1 none
```

Policy constants (run interval, reset buffer, tolerances, retry limits and
backoff) have exactly ONE owner: `scheduler-config` prints them and the shell
assigns them at start-up, validating them there. A shell literal would be a
second source of truth for the scheduler's behavior.

## Quota adapters

`quota_sentinel/quota/` owns the provider roster, the fallback ladder, the
per-provider capability differences, and the normalisation of a probe into the
one document shape the scheduler and the cards read.

The ladder is data, not control flow:

```text
native ──▶ codexbar-live ──▶ codexbar-cache ──▶ pi-snapshot
```

Freshness is a property of the tier, not of a caller's memory: only the two
live tiers are Fresh, and only a Fresh observation can move a deadline.
OpenCode's monthly window is `monthly_display_only=True` on its adapter, and
the scheduler's observation reader cannot even see it — a stronger guarantee
than a comment telling callers not to look.

Vendor probes still execute in the shell, under the shared process-group
timeout helper, because that is where the kill-group and orphan-reaping
semantics are already proven. Adding a fourth provider should mean: an
adapter, a roster entry, and tests — not a new arm in a dozen
`case "$provider"` statements.

## Notification boundary

The shell renders and delivers Feishu cards; the *decision* to notify is a
consequence of scheduler transitions. `/usage` semantics are fixed and must
not drift:

```text
/usage = quota fetch + card response, never a model run
fresh quota + idle scheduler -> opportunistic deadline refresh
run.lock busy                -> skip the scheduler sync, still send the card
```

The busy reply never leaks lock names, PIDs or timeout internals. Card fields,
provider order, quota source labels, recipient filtering and deduplication are
observable behavior and are pinned by the Feishu suites.

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
| C. system | `/usr/bin/python3` (`PYTHON3_BIN`, retained) | `run_with_timeout.py`, `opencode_usage.py`, native-probe python check, the scheduler bridge, the `status` next-due seam | stdlib-only, invoked on shell/scheduler hot paths; must not gain uv startup latency, cache, or environment coupling; the >=3.9 floor keeps class C and the uv project env behaviorally identical for this code |

The scheduler bridge belongs in class C by the same argument that created the
class: the precision timer tick must not pay a project start-up, and a
decision must keep working while the project environment is being rebuilt.

LaunchAgent lifecycle: setup phase (`install-launchagents.sh`) verifies uv
and runs `uv sync --locked` — the ONLY network-capable step; the agent
runtime then runs `uv run --project <repo> --frozen --no-sync` (explicit
`--project` because launchd's `WorkingDirectory` is `/private/tmp`, which
would not discover the repo; `--frozen` forbids lock re-resolution,
`--no-sync` forbids environment mutation). A missing environment therefore
fails loudly at daemon start (err log) instead of self-healing — by design.
Cache: production uses the default user cache; `UV_CACHE_DIR` overrides are
test/sandbox-local only.

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

## Shell end state

`quota-sentinel.sh` keeps exactly four responsibilities:

1. **CLI compatibility layer** — `check|wait|run|usage|cutover|status|…`
   argument handling and the legacy verb surface.
2. **Model runner adapter** — provider CLI invocation, `auth.json`/settings
   handling, provider environment, process groups, timeouts, and the retry
   burst's process management. Python decides *whether* to run and *what the
   outcome means*; the shell decides *how* to run it.
3. **Quota tier execution** — invoking the vendor probes under the shared
   timeout helper.
4. **System glue** — locks, temp dirs, launchd bootstrap, install.

It no longer contains: scheduler policy, state transition policy, deadline
algorithms, retry policy values, authoritative state persistence, or quota
normalisation policy.

The model runner staying in shell is a deliberate boundary, not a to-do. Its
interface is explicit: the scheduler asks for one attempt per provider and
receives an exit status; the Python side never reads a shell global to make a
decision.

Shell freeze (still in force): bug fixes, compatibility fixes,
provider-specific model-runner adjustments, and thin call-through layers to
Python are allowed. New long-lived subsystems, new scheduler state files,
large new quota-parsing blocks, per-provider copies of the quota pipeline, and
new scheduler policy branches are not.

## Migration and rollback

Upgrading an existing deployment:

```text
1. install the new version; the installer boots the old agent out first, so
   no process that still believes in the legacy backend can be alive
2. run `quota-sentinel.sh cutover` — under run.lock, verified, atomic
3. legacy slot files remain on disk, untouched, as the rollback artifact
```

An automatic cleanup of legacy files is deliberately NOT provided: deleting a
user's state is their decision, and the files are harmless once retired.

## Testing doctrine

* The zsh suites are the BLACK-BOX compatibility contract for behavior the
  user sees: CLI parity, scheduler decisions, retry semantics, `/usage`,
  cards. Migrating logic into Python does not retire them — a new Python suite
  is an *additional* white-box proof.
* Python suites are the white-box proof for transitions, the authority
  protocol, the schema and the store contracts.
* A test may only be changed when the CONTRACT changed, and the change must be
  explained. Timeouts and thresholds are not relaxed to reach green.
* Persistence and scheduler suites run under `python -O` too: no security or
  correctness property may depend on `assert`.
