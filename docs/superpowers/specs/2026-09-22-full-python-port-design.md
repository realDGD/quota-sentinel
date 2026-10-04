# Full Python port of Quota Sentinel

## Goal

The Python project must provide every operator and daemon behavior currently
provided by `quota-sentinel.sh` on `main`: `check`, `wait`, `run`, `usage`,
`status`, quota collection, model attempts, retries, Feishu notification
rendering and delivery, and auxiliary card/user commands. After parity is
verified, move this repository from `Script/Shell/quota-sentinel` to
`Script/Python/quota-sentinel` and update the installed LaunchAgents.

## Existing foundation

The current branch already implements authoritative state, scheduler policy,
quota normalization and provider tier planning, and notification selection in
`quota_sentinel/`. `feishu_listener.py` and `task_orchestrator.py` are Python,
but they call the shell for `usage` and `check`. The shell still performs
process execution, quota probing and fallback, card rendering and transport,
and public command orchestration. Existing zsh regressions describe the old
behavior and must remain green until replaced by equivalent Python black-box
tests.

## Target architecture

- `quota_sentinel/runtime/`: provider command execution, bounded subprocess
  groups, configuration and keychain access, quota tier probes and caches,
  Feishu rendering and transport. Use explicit result objects and inject
  process/time/network boundaries for tests.
- `quota_sentinel/app.py`: public operations. It takes the scheduler's
  `run.lock` where required, uses the existing scheduler service for every
  decision and state transition, and coordinates attempts, quota readings,
  and one notification per event.
- `quota_sentinel/__main__.py`: public CLI with `check|wait|run|usage|status`
  and existing state diagnostics/lifecycle verbs. No public command invokes
  zsh. The listener and orchestrator call this Python entry point.
- `install-launchagents.sh` can remain an installation helper, but the
  installed agent and the system's normal operation must invoke Python only.
  A temporary zsh compatibility entry may exist during migration and is
  removed or reduced to a command forwarding shim at completion.

## Behavioral contracts

1. Preserve provider roster and names, quota fallback order and freshness,
   deadlines, reset calibration, retry debt, parallel provider attempts,
   timeouts, and all persisted state formats.
2. Preserve public command outcomes and Feishu card contents and layout,
   including busy `/usage`, recovery deduplication, and credential handling.
3. Preserve isolation of child processes and ensure timeouts kill detached
   descendants, including nested probe helpers.
4. Keep JSON backend ownership explicit. A missing authority manifest is an
   error, never an inferred legacy backend. The port must not silently
   bootstrap or cut over live state.
5. Fix the declared runtime floor: code must work on Python 3.9, or change
   `requires-python` and the lockfile to the actual minimum supported version.
6. Installation and migration must be reversible. The old service keeps
   running from the old checkout until the new files and environment are
   verified; switching LaunchAgents is the final operational step. Preserve
   `.git`, local logs, and untracked local files when moving the checkout.

## Verification and cutover

Use a temp state directory and mocked external CLIs/HTTP for black-box
comparison. Run all Python tests and the existing zsh regressions while the
compatibility script exists. Replace shell-only assertions with Python
equivalents before removing implementation code. Verify `uv` runtime from
launchd's foreign working directory. On this host, the live state lacks
`backend-authority.json`; obtain an explicit verified ownership assertion or
restore the manifest before changing the installed agent. Re-render/reload
the LaunchAgents only after the repository is in the Python directory.
