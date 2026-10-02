# Architecture

Short reference for the invariants this project relies on. Tests enforce the
behavioral parts; this file names the boundaries so future changes keep them.

## Ownership

```text
quota_sentinel/                = the system's brain AND its hands (Python)
  state/                       authoritative scheduler state + backend authority
  scheduler/                   scheduler policy, transitions, decisions
  quota/                       provider quota adapters and normalisation
  runtime/                     model runner, quota probes, Feishu cards +
                               transport, locks, run log (class C: stdlib-only)
  __main__.py                  the CLI, and the single production entrypoint
                               (console script `quota-sentinel`)

task_orchestrator.py           = when (wake scheduling + durable run history)
  task-orchestrator.sqlite3    = history / observability, NEVER authoritative
```

No zsh runs in normal operation. The retired `quota-sentinel.sh` is preserved
in git history for reference only; every verb it used to dispatch now lives in
`quota_sentinel` and is reached through the console script:

```text
uv run --frozen --no-sync quota-sentinel <verb>            (from the repo root)
uv run --project <repo> --frozen --no-sync quota-sentinel  (launchd form)
```

Authoritative scheduler state must never move into the SQLite history DB, and
must never exist in two places: exactly one backend owns it at any instant,
and a single durable fact says which one (see *State backend authority*).

The Python CLI owns the process side too — model execution, quota probing,
notification transport, locks and temp dirs — and every scheduler decision and
state transition is a function call inside the same package; there is no
second implementation to drift against. The class-C boundary is what keeps
that safe for the hot paths: `quota_sentinel/runtime/*` (and the
scheduler/state graph it sits on) stays stdlib-only, so it is importable by
`/usr/bin/python3 -S` without the project environment.
`tests/python-architecture-audit-regression.py` (AR5) and
`tests/uv-project-regression.py` (UV11) fail if a third-party import enters
that graph.

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
* **REQUIRED once the deployment has an owner, and absence means UNKNOWN —
  never `legacy`.** The only thing that materializes it is an operator
  asserting, explicitly, that the deployment predates the protocol:
  `quota-sentinel bootstrap-authority --assume-legacy` (library primitive
  `bootstrap_legacy_authority`). The earlier protocol let the installer
  create it as `legacy epoch 0` when absent, and read absence as "never cut
  over"; that made a deleted manifest silently resurrect stale legacy state
  (retry debt, deadlines, candidates, anchors) out from under an advanced
  JSON backend — the installer would even re-legitimize it on the next
  upgrade. Nothing automatic touches absence any more: not the installer, not
  an update path, not a runtime command.
* A *damaged* manifest is never defaulted to either side: `read_authority`
  raises, mutations fail closed, readers fail loudly. Guessing here would let
  a legacy writer create a second, diverging source of truth.
* `epoch` increments on every durable switch, so "the same authority" is
  distinguishable from "a different authority that looks similar".

### Asserting ownership, and why it is not a runtime side effect

```text
uninitialized / manifest-lost state dir
   │  OPERATOR: bootstrap-authority --assume-legacy   (the only entry point)
   ▼
backend-authority.json = {backend: legacy, epoch: 0}
   │  every later command
   ▼
manifest present  ──►  normal operation
manifest missing  ──►  loud AuthorityMissingError, and NOTHING creates one
```

The distinction that matters is not "has this file existed before" — that is
not knowable from the state directory — but **whether an operator has
established that this deployment predates the protocol**. Two situations are
indistinguishable on disk:

```text
A. the deployment predates the protocol (no fact was ever written)
B. the deployment cut over and the manifest was lost
```

so the decision cannot be inferred and must be ASSERTED. That is why the
primitive is named `bootstrap_legacy_authority`, why the CLI verb requires
`--assume-legacy`, and why the installer only READS the fact: an automatic
caller in case B is a silent downgrade, and in case A the operator is the
only one who can say so.

Three consequences, all deliberate:

* `bootstrap_legacy_authority` is idempotent (an existing manifest is
  returned untouched, never rewritten — a JSON manifest is never downgraded)
  and refuses to publish over a corrupt one: writing over corruption would
  destroy the only ownership fact.
* Its PRECONDITIONS are the caller's: hold `run.lock`, and have established
  case A. Nothing in the library can verify either, so the name, the
  docstring and the mandatory flag carry them instead of a fake check.
* It reads NO scheduler state and ignores whether JSON documents exist.
  Phase 2 shadow documents may predate the protocol by months, so their
  presence cannot be evidence of a cutover.

`AuthoritativeStateStore` is the only authority → backend mapping in the
codebase. Production code never branches on the backend itself.

* Writers (`commit`) hold `run.lock`; the lifecycle verbs acquire it
  themselves (see below), so a flip cannot interleave with a mutation. The
  router still re-reads the fact after committing and raises
  `ConcurrentAuthorityChangeError` if it moved — a precondition violation
  must be loud.
* Readers (`load`, `load_all`) mostly do NOT hold the lock (`status`, the
  timer's deadline scan, `/usage`). They use a generation guard: read the
  fact, read the state, read the fact again; accept only when both agree, else
  retry, else fail loudly. `load_all` applies the guard to the whole roster so
  a caller never mixes providers from two different backends.
* No caching layer exists: a long-lived process (the precision timer loops for
  days) must not pin a backend selection at start-up.

### Cutover and rollback

Both are OPERATOR verbs, both acquire `run.lock` themselves, and both are
**WHOLE-ROSTER**: the function signatures take no provider argument at all
(`cutover_to_json(state_dir, *, checkpoint=None)`), the roster is the shared
`DEFAULT_PROVIDERS` constant, and naming a provider after either verb is an
argparse usage error
raised before any state is touched. Authority is one global fact, so a
provider-scoped switch is not a smaller switch — it is a switch that hands the
unprepared providers to the retired backend, silently discarding the deadlines
and retry debt their documents own. The property is pinned structurally
(`tests/python-architecture-audit-regression.py`, AR13/AR14: no `providers`
parameter, no positional in either CLI surface) and behaviorally
(`tests/python-authority-regression.py`, W1-W4: every provider is prepared and
verified, and a divergence or failure in the LAST provider still blocks the
global flip).

The public entry points (`quota-sentinel cutover` / `quota-sentinel
rollback`, and the same verbs through `python -m quota_sentinel`) take the
lock through `quota_sentinel.state.runlock`, which executes
**`/usr/bin/shlock` with the same arguments on the same `run.lock` file** as
the retired shell implementation did. That matters for exactly one reason
now: an upgraded deployment may still have a process from the previous
version running, and the two implementations must exclude each other through
the same durable protocol while it drains.

The `scheduler-*` bridge verbs deliberately do NOT acquire the lock — the
runtime calls them while it already holds it, and acquiring it again from a
different process would deadlock against its own caller. They are labelled
`INTERNAL BRIDGE API` in `--help`, are not documented as operator commands,
and `tests/python-architecture-audit-regression.py` (AR10) fails if that
changes.
A CLI cannot verify a lock it did not take, so no such check is faked: the
safety comes from the public surface not needing one.

`cutover` runs under `run.lock`:

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
mechanically, and — since the owner must already be recorded before the
cutover begins — it resolves from a manifest that is always present: before
the flip it reads `legacy` at epoch N (refreshed documents are harmless
shadows), and the flip itself is one atomic replacement, so readers see the
complete old or the complete new manifest. `cutover` also refuses outright
when the deployment has no owner: absence must be resolved by an operator
assertion, and can never be read as "legacy" by a command that was only trying
to switch backends.
`tests/python-authority-regression.py` injects a crash at every checkpoint,
including inside the manifest publish, and asserts that ownership is always
determinable, that no legacy byte ever changes, and that no torn document or
temp file survives. The operator-visible lifecycle — including the refusal to
start while another writer holds `run.lock` — is pinned end-to-end through
the real CLI in the same suite.

Legacy files are **never deleted, moved or rewritten by the cutover**; after
the flip they are a rollback artifact and no production path reads or writes
them. The retired backend's slot accessors were guarded in the shell (`die`
rather than touch a retired backend); in the port that guard is structural:
`FileStateStore` is reachable only through the router when the manifest says
`legacy`.

`rollback` is the inverse switch and refuses unless it is a PURE UNDO (every
document still equal to its legacy file). Once JSON has advanced, "roll back"
would discard authoritative state — a human decision with a human-sized
backup, not an automatic one. Deleting the legacy files destroys that
artifact permanently, so `rollback` becomes impossible; the README says so
where the cleanup is documented.

`migrate` (the Phase 2 seed) carries the same rule one layer down: it refuses
under JSON authority, because "repair" a missing document from the retired
legacy files would resurrect exactly the state the missing-manifest rule
exists to protect.

### Failure policy on the authoritative backend

```text
MissingStateDocumentError   no document for this provider
DocumentCorruptError        bytes are not UTF-8 JSON
SchemaError                 parsed but violates v1
```

None of these may be answered with a default `ProviderState()`, and neither
may an ABSENT manifest (`AuthorityMissingError`, a
`FAIL CLOSED on state mutation` condition like the rest). Mutations fail
closed, tasks are not marked complete, deadlines do not advance, and the
failure is loud. Recovery is deliberately minimal: restore the document (from
a backup, or by re-running the cutover from the untouched legacy files) —
there is no automatic repair subsystem, because a repair heuristic over the
scheduler's source of truth is exactly how a lost debt becomes invisible.

## Document backend contracts (Phase 2 / 3A, preserved)

The contracts the JSON backend and the cutover preparation were built against
still hold, and the phases that consumed them (3B, 3C) did not weaken any of
them. They are restated here because a migration that quietly drops its own
safety wording is how the safety itself gets lost. Each sentence below is a
CONTRACT pinned by `tests/python-state-cutover-regression.py`, not prose.

**Preparation is not ownership.**

```text
Phase 3A PREPARED is NOT JSON AUTHORITATIVE
```

Phase 3A only prepares a semantically current JSON document for a later,
explicit switch — which is exactly what `cutover` now performs. Preparing a
document does not move ownership.

**Source of truth is absolute**, and the lock precondition is declared rather
than faked:

```text
Source of truth is absolute: the ProviderState read from the legacy slot
files AT CALL TIME. A pre-existing shadow is comparison material at best.
a lock file existing does not imply the caller owns it
```

This is the exact opposite of Phase 2 `migrate_provider`, whose skip-if-exists
rule is right for a bootstrap seed and wrong for a source-of-truth refresh —
the two are deliberately different operations and neither borrows the other's
rules.

**Validation is one pipeline, run before any mutation:**

```text
validate_state -> state_to_document -> validate_document -> deterministic UTF-8 bytes
```

The plan-then-execute boundary means the whole preflight completes before the first filesystem mutation, so a business-value failure cannot leave a half-written document.

**Failures are fail-closed and their branches are distinguished, not blurred.**
The load/schema failures named above — `MissingStateDocumentError`,
`DocumentCorruptError`, `SchemaError` — all FAIL CLOSED on state mutation: no
default document, no advanced deadline, no completed task.

```text
failure AFTER a successful publish (verification mismatch)
```

is a different animal: the newly-published complete document may legitimately
be on disk, no ownership changed, and rollback of authoritative ownership is unnecessary. Adding a shadow rollback there would be "fixing" correct behavior
into a more fragile one, which is why the rule is written down rather than
left to taste.

**Durability scope is stated, not implied:**

```text
Atomic-visibility guarantees cover process crash and concurrent readers
Power-loss durability is NOT claimed and NOT implemented
```

A publish is a temp-write/fsync/rename, so a reader observes the complete old
document or the complete new one, and a process killed at any point leaves no
torn document. What that does NOT buy: the file's contents are fsynced before
the rename, but the containing directory entry is not, so an operating-system
or hardware failure may lose the rename even though no process ever observed a
torn document. Closing that gap would need a directory fsync on every publish;
nobody has asked for that guarantee, and claiming it without the fsync would
be worse than saying so.

The Phase 3B ownership-switch design questions — how a process decides who
owns the state after a restart, and how to keep a reader and a writer from
disagreeing — are answered by *State backend authority* above: the
double-truth risk is closed by making the durable manifest the single fact,
and by the router's generation-guarded read.

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
| 7 | `cutover` → `scheduler-cutover` | the authority manifest, after refreshing every document | run (the public verb acquires it through `runlock`; the internal bridge verb relies on its caller) |
| 7b | `rollback` → `scheduler-rollback` | the authority manifest, after proving a pure undo | run (same) |
| 7c | `bootstrap-authority --assume-legacy` → `scheduler-bootstrap-authority` | the authority manifest, ONLY for an operator-asserted pre-protocol deployment | run (same); unknown ownership is never resolved automatically |
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
quota_sentinel/scheduler/cli.py           the internal bridge verbs the runtime calls
```

`policy` is `new_state = f(old_state, inputs, now)`. It performs no network
call, runs no model, takes no lock, touches no filesystem and never mutates a
second provider. It is a transcription of the shell policy that preceded it —
constants, branch order and tie-breaking are unchanged, and
`tests/python-scheduler-regression.py` is the compatibility proof.

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

That anchor is OUR bookkeeping of a generation's boundary, not a claim about what
opens the provider's window. Measured on Antigravity, the reported boundary does
not track our turns: across 35 reset-anchor movements in `logs/` (2026-09-23
11:23:05 → 2026-09-28 07:56:43), 19 are exactly `+5h00m00s`, and in the clean
runs the probe reported the next boundary *before* the turn that then reproduced
it unchanged. The boundary is therefore provider-side, which is what lets
`reanchor_probe_only` hold a correct deadline for a provider whose model trigger
is switched off; the provider's exact rule still needs a live check
(README, **Probe-only providers**, carries the measurement and its
counter-examples).

A probe-only provider (`QUOTA_SENTINEL_PROBE_ONLY`) is the one case where the
deadline is deliberately not the product of a run: `check` skips it, clears
retry debt it could never repay and follows the observed reset instead of a
committed obligation, while `run` either refuses an explicitly named probe-only
provider (a typed CLI error, exit 3, before the run lock) or drops it from the
default whole-roster run and logs the omission. The transport, the roster entry
and the tests stay in place, so unsetting the variable restores the previous
behaviour exactly.

Transitions exist as first-class values, each individually proven:
`begin_attempt`, `record_attempt`, `commit_success`, `record_last_window`,
`sync_deadline` (7 branches), `evaluate_due`, `retry_blocked`.

`Transition.publish` names slots a transition must MATERIALIZE even when the
value is unchanged. The success commit uses it for `retry_pending=0`: the
pre-migration implementation always left an explicit "no debt" file behind, and
"absent" and "0" being equal to every reader does not make changing the
on-disk contract acceptable.

`Transition.writes` (changed OR forced) is what decides whether the backend is
touched at all, so a no-op branch — Stale quota, reset-buffer blocking, an
unpaid debt — is provably non-writing rather than accidentally writing
identical bytes.

### Scheduler domain and the internal bridge verbs

The runtime calls the domain **in-process**: `quota_sentinel.app` imports
`quota_sentinel.scheduler.policy` / `service` and `quota_sentinel.state`
directly. There is no subprocess boundary, no serialized machine protocol and
no second implementation left to drift; one `check`/`run` tick is a sequence
of function calls that each take the store's atomic publish.

The `scheduler-*` verbs registered by `quota_sentinel/scheduler/cli.py` are
retained as an INTERNAL BRIDGE API surface: they expose the same transitions
to the test suites and to operators diagnosing a deployment, and they are what
a not-yet-upgraded process from the previous (shell) version called. They run
on any interpreter — including `/usr/bin/python3 -S` — because the domain's
import graph is stdlib-only (pinned by `tests/uv-project-regression.py` UV11
and `tests/python-architecture-audit-regression.py` AR5). `-S` skips site
processing; nothing on this path needs it.

The bridge verbs can batch a phase — one process per roster phase, not one per
provider. Batching shares the PROCESS; it never shares a transaction: each
provider still gets its own transition and its own commit.

The bridge prints machine records, not prose:

```text
provider=<name>                          the records that follow belong to it
change<TAB>p<TAB>slot<TAB>old<TAB>new    one durable slot changed
log=info|warn<TAB>text                   what to log, at which level
end=<name>                               the provider's records are complete
```

The change records preserve the shape of the run log the retired shell
produced when it diffed values itself. Verbs whose shell predecessors logged
nothing but the value diff emit no `log=` record.

Exit codes are part of the contract:

```text
scheduler-decide      0 due | 1 wait | 2 unpaid debt (skipped) | 4 error
scheduler-sync        0 applied | 1 no valid quota | 2 blocked | 3 deferred
scheduler-valid-reset 0 found | 1 none
```

Policy constants (run interval, reset buffer, tolerances, retry limits and
backoff) have exactly ONE owner: `quota_sentinel.scheduler.policy`.
`scheduler-config` prints them for callers that need the values; no caller
keeps its own copy.

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
OpenCode's and ClinePass's monthly windows are `monthly_display_only=True` on
their adapters, and the scheduler's observation reader cannot even see them — a
stronger guarantee than a comment telling callers not to look. Every provider
now has a tier ① helper or adapter; ClinePass's reads the same internal
`/plan/usage-limits` endpoint the Cline CLI does and rejects a window with no
`resetsAt`, because an idle plan has no boundary to schedule from.

Vendor probes execute in `quota_sentinel.runtime.quota_probe`, which runs the
isolated helper scripts (`antigravity_usage.py`, `opencode_usage.py`) and the
CodexBar/Pi adapters under the shared process-group timeout helper, because
that is where the kill-group and orphan-reaping semantics are proven. Adding a
fourth provider means: an adapter, a roster entry, and tests — not a new arm
in a dozen `case "$provider"` statements.

## Delivery transports

A task reaches a provider by exactly one of four transports, and the choice is a
capability on the adapter (`QuotaAdapter.transport`), not a branch:

```text
codex   ──▶ runtime.codex_exec.CodexExecRunner  (fallback for codex)
agy     ──▶ runtime.agy_exec.AgyExecRunner      antigravity (fallback for it: pi)
pi      ──▶ runtime.models.ModelRunner          codex, antigravity fallback
direct  ──▶ runtime.direct.DirectRunner         opencode, clinepass
```

`runtime.dispatch.TransportRouter` presents all four as the single
`prepare`/`run -> AttemptResult` surface `Application` already used, so the
coordinator, the retry debt, the locks, the cards and the run log never learn
which one answered.

**Opening and quota-query chains are separate saved choices.** New users get only official Codex opening and native Codex metadata, with no fallback or Feishu. The preserved personal profile opens Codex through Pi then Codex, Antigravity through agy then Pi, and the two API providers directly. Its quota order remains native → CodexBar live → cache → Pi snapshot. The profile is migration data, not the new-user default.

`runtime.chains.AttemptChainRunner` owns configured fallback; selected concrete runners are terminal. Only explicitly listed channels can run, in their saved order. Functional failure advances; accepted success (including a cost warning) stops. A selected Antigravity/ClinePass Pi plugin is checked before an attempt; missing plugins provide installation guidance. Existing callers without a runtime plan retain the legacy composition until explicit migration.

The codex transport exists because a transport also decides *who the client
is*, not only what a turn costs. A default `codex exec` carries the whole Codex
agent (skills, multi-agent roles, permissions, environment context, tools) and
measured 9,658 tokens per attempt on 2026-09-27; the profile in
`runtime/codex_exec.py` is the same official CLI with those layers switched off
by official configuration keys and measured **1,682 input / 5 output tokens for
the same `1` reply**. It parses the `--json` event stream (final assistant text
plus `turn.completed` usage) instead of reading a log format, so every attempt
writes its own cost into the run log.

Two failure classes are separated on purpose. A **functional** failure (non-zero
exit, no completion event, a completion whose usage reports no input tokens, a
reply that is not `1`, reasoning tokens above zero) falls back to Pi: nothing was
verified as delivered. A **cost regression** (the turn replied `1`, has no
functional problem, and the profile no longer applies — a renamed feature flag,
or a server-side metadata change) is **accepted**, not handed over: the message
was already delivered and the window already anchored, so re-delivering the same
attempt through Pi would spend more quota for a fact that is already true, and a
Pi failure on top would turn a delivered message into a reported failure the
scheduler then retries. The regression is announced with its measured numbers and
is never recorded as a verified profile, so the next attempt tests again. The
`CODEX_EXEC_PROFILE` fingerprint covers the local half of "did the profile
change"; the per-attempt token ceilings cover the half nobody can see.

**Antigravity ships the opposite priority: the official CLI first, Pi as its
fallback.** `agy` is an agent too, and a stock turn carries its scaffolding:
measured on 2026-09-27 against agy 1.2.12 with `gemini-3.8-flash-low`, a plain
`agy -p "1"` costs 22,311 input / 28 output tokens, of which ~20.3k is the
schema of the 57 built-in tools alone. `runtime/agy_exec.py` writes one markdown
agent into an empty cwd per attempt — `excludeDefaultComponents: true` (no
default prompt sections, no built-in tools), `inheritCustomizations: false` (no
rules, skills, plugins, subagents, MCP servers) and a one-line body — and the
same turn measures **564 input / 1 output / 0 thinking**. Measured without
`excludeDefaultComponents` it is 1,997; `tools: []` changes nothing once that key
is set.

Two guards there cost zero tokens, because read-only slash commands are answered
by the CLI itself (`input_tokens` 0, `num_turns` 0): `agy -p /agents` confirms
the agent resolves *before* the turn (an unresolvable `--agent` silently falls
back to the default agent at ~40x, which the input ceiling would catch only after
paying for it), and the pre-turn `Eligibility check failed` handshake is retried
rather than reported as a delivery failure — but only when the marker is in the
CURRENT turn's own stderr *and* that turn reported no tokens, because a turn that
spent tokens is a real turn and is never replayed for free. A `SUCCESS` turn that
cannot show `input_tokens >= 1` is functional, not free: the input count is the
only structural proof that the 564-token profile and not the 22,311-token stock
agent answered, and the ceiling cannot be evaluated without it. A cost regression
with no functional problem is accepted and logged, exactly as on the codex
transport. Thinking tokens are reported and not
policed — at `--effort low` the model decides (0 and 34 were both measured on
identical input), so the integrity signal is the structural input side. This
transport writes no quota snapshot: the Pi capture file is normalized as a *Pi*
document, and tier-① already reads the same `/usage` payload for free.

The direct runner keeps the same credential discipline as
`opencode_usage.py` (the key reaches curl through its stdin config, never argv
or the environment), writes the same quota snapshot the retired capture
extension wrote, and emits the same per-attempt log lines. It disables
reasoning with `reasoning_effort: "none"`, which is measured, not cosmetic:
the same prompt costs 16 tokens with it and 86 without. `QUOTA_SENTINEL_TRANSPORT`
(`codex=pi`, `agy=pi`, `opencode=pi`, …) moves one provider onto another
transport for an A/B run without editing code.

## Notification boundary

`quota_sentinel.runtime.cards` renders and `quota_sentinel.runtime.feishu`
delivers Feishu cards; the *decision* to notify is a consequence of scheduler
transitions. `/usage` semantics are fixed and must not drift:

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
| B. isolated | `uv run --offline --no-project --no-config python -B …` | `antigravity_usage.py` | deliberate supply-chain boundary: must stay outside the project even now that a root pyproject exists — `--no-project` is load-bearing and pinned by tests/python-quota-adapter-regression.py |
| C. system | `/usr/bin/python3` | `run_with_timeout.py`, `opencode_usage.py`, native-probe python check, the internal `scheduler-*` bridge verbs, the `status` next-due seam | stdlib-only, invoked on hot paths and importable with `-S`; must not gain uv startup latency, cache, or environment coupling; the >=3.9 floor keeps class C and the uv project env behaviorally identical for this code |

Class C is a property of the import graph, not of a shell caller: the
`scheduler-*` bridge verbs and the runtime modules they reach must stay
stdlib-only so a decision keeps working while the project environment is
being rebuilt, and so `/usr/bin/python3 -S` can always import them.
`tests/uv-project-regression.py` UV11 and
`tests/python-architecture-audit-regression.py` AR5 pin that graph;
`tests/python-entrypoint-regression.py` E11 does the same for the runtime
package.

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

## Shell retirement

The zsh implementation is **deleted**. Its responsibilities moved into
`quota_sentinel` one surface at a time, and the move is complete:

1. **CLI** — `check|wait|run|usage|status`, the diagnostics
   (`next-due|state-dump|dump|json-dump|authority|migrate`), the notification
   verbs (`card-preview|send-test-card|discover-feishu-user`) and the
   authority lifecycle (`bootstrap-authority --assume-legacy`, `cutover`,
   `rollback`) are all `quota_sentinel.__main__` verbs reached through the
   `quota-sentinel` console script.
2. **Model runner** — `quota_sentinel.runtime.models` invokes the provider
   CLIs with the same environment, process groups, timeouts and kill grace.
3. **Quota tier execution** — `quota_sentinel.runtime.quota_probe` runs the
   vendor probes under the shared timeout helper.
4. **System glue** — `quota_sentinel.runtime.locks` owns `run.lock` /
   `quota.lock`; the rendered LaunchAgents invoke the console script
   (`--frozen --no-sync quota-sentinel check|wait`) or the Python listener
   through the locked uv environment, never a shell.

There is no zsh in normal operation, and no compatibility shim to keep one
alive. `tests/python-entrypoint-regression.py` asserts the shell file is gone
and that the daemons, templates and installer never mention it.

The class-C boundary is what survived the retirement, in its stronger form:
the runtime import graph is stdlib-only and importable by the system
interpreter (see *Python runtime / dependency ownership* above). That is a
property of the code, not of a caller, and the tests pin it directly.

## Migration and rollback

Upgrading an existing deployment:

```text
0. uv run --frozen --no-sync quota-sentinel bootstrap-authority --assume-legacy
                                      ONE-TIME, and only for a deployment the
                                      operator has confirmed predates the
                                      protocol; nothing automatic does this
1. ./install-launchagents.sh --load   uv sync, retire the legacy watchdog and
                                      timer agents, render the plists,
                                      VALIDATE the authority manifest (and
                                      fail if it is missing/unreadable),
                                      restart the listener
2. uv run --frozen --no-sync quota-sentinel cutover
                                      under run.lock, verified, atomic,
                                      whole roster
3. legacy slot files remain on disk, untouched, as the rollback artifact
```

A deployment still on the pre-per-provider (monolithic) slot layout must run
the retired shell implementation **once**, from the branch point in git
history, before using the port: the port has no reader for unprefixed
`last-task-at` / `next-due-at` / `last-triggered-window` files. That
bootstrap was deliberately not ported; it is unreachable for any deployment
already on per-provider files.

The installer retires the legacy schedulers BEFORE it starts the listener, so
the post-condition is "only the listener/orchestrator is loaded" rather than
"the operator remembered to stop the old ones". It **reads** the manifest
before any agent is restarted and never writes one: a deployment whose owner
is unknown gets a failed install with zero launchctl calls, not an invented
`legacy epoch 0`. Upgrading, asserting ownership and switching ownership are
therefore three separate actions.

Step 1 must include the restart (`--load`, or an explicit `launchctl
bootout`) because the ONE unsafe state is a still-running process from the
previous version: old code does not know the authority manifest exists and
would keep writing the legacy slots after the switch. A process running the
NEW code is safe either way — every state access goes through the router,
which re-reads the durable fact on every operation — so ordering step 2
before or after the new agent starts does not matter, and `cutover` takes
`run.lock`, so it cannot interleave with an in-flight `check` or `run`.

### Losing the manifest

The recovery path is deliberately manual and loud rather than automatic:
`read_authority` raises `AuthorityMissingError` naming the situation (the
owner is UNKNOWN) and both remedies (restore from backup; or, only for a
confirmed pre-protocol deployment, assert legacy). There is no repair
subsystem and no automatic caller, because a heuristic that recreates the
ownership fact is indistinguishable from the silent legacy fallback this
design removed — and a lost-but-recreated manifest re-legitimizes exactly the
stale deadlines and retry debt the JSON documents moved past. See the README
for the operator procedure.

An automatic cleanup of legacy files is deliberately NOT provided: deleting a
user's state is their decision, and the files are harmless once retired.

## Testing doctrine

* The Python suites are the contract: `tests/python-*-regression.py` pin the
  operator-visible behaviour (CLI verbs, scheduler decisions, retry
  semantics, `/usage`, cards) and the white-box proofs (transitions, the
  authority protocol, the schema and the store contracts). The zsh suites
  that once provided the black-box half were retired with the implementation
  they drove; `docs/superpowers/plans/2026-09-22-full-python-port.md` records,
  behaviour by behaviour, what replaced each of them and what did not carry
  over.
* A test may only be changed when the CONTRACT changed, and the change must be
  explained. Timeouts and thresholds are not relaxed to reach green.
* Persistence and scheduler suites run under `python -O` too: no security or
  correctness property may depend on `assert`.

## Selected runtime configuration

`config` resolves typed, nonsecret feature/provider/channel choices. `configure` previews and saves them under a compare-and-swap edit lock. A missing authority in an existing state directory remains an error. Explicit fresh provisioning seeds all provider documents, publishes JSON authority last, and refuses an existing directory. Disabled providers retain history but cannot prepare runners, probe, notify, repay debt, or enter the deadline roster. Re-enabling waits for fresh metadata before retiring old debt.

Display queries stop at the first usable result; scheduling skips stale results and continues through the same listed chain. No fresh result means no authoritative anchor update. Selected budgets include each sequential query tier, primary preparation, per-attempt fallback preparation, notifications and cleanup.

`daemon.serve` owns scheduler/listener startup, signal handling and shutdown. The listener receives the sole scheduler reference and owns its bounded command workers. Bot replies belong to incoming authorized commands; automatic push is separately selected. Runtime plans are fixed until explicit restart. `tests/run-regressions.py` separates portable and native gates, removes inherited credentials and bounds each script.
