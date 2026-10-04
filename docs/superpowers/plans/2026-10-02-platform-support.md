# Native Platform Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not use DSH.

**Goal:** Run selected quota-sentinel functionality on macOS, Linux and native Windows, including bounded credential access, process-tree cleanup, locking, packaged helpers and background startup.

**Architecture:** A small platform boundary supplies paths/launchers, private file publication, locks, owned processes, credentials and service installation. Existing runner/state APIs call that boundary and retain their provider policy. Native tests determine the published platform support matrix.

**Tech Stack:** Python >=3.9 stdlib and ctypes, POSIX locks/signals, Windows Job Objects and Credential Manager, optional Linux keyring backends, launchd/systemd-user/Task Scheduler, unittest/CI.

**Spec:** `docs/superpowers/specs/2026-10-02-modular-config-and-platforms-design.md`, section 8 and cross-platform acceptance criteria. Depends on plan 1 interfaces and optionally integrates plan 2's live-query helper when it is implemented.

## Global Constraints

- Keep existing macOS paths, Keychain service/account names, shlock protocol and durable backend authority compatible.
- Linux/Windows new-user defaults remain Codex-only/native-only with Feishu and fallback off.
- Credential configuration contains references only; secrets never enter logs, command arguments or service templates.
- Core imports are stdlib-only; Linux password-service libraries and Feishu libraries are optional selected extras.
- Every external call is bounded. Windows child execution starts only after Job Object ownership is established; cleanup must include descendants after leader exit.
- No shared state directory can be used by different lock protocols. Disabling features does not delete their history or credentials.
- Verification targets: current macOS, Ubuntu 22.04/24.04 and Windows 11. Fixtures/CI are distinct from supplier-authenticated smoke checks.
- Service installation/application is explicit and separate from source/config edits; current user's running service is not restarted during development.

## Review Focus

- Windows path contains spaces, Unicode or shell metacharacters: preserve exact argv and avoid shell expansion; pinned in tasks 1–2.
- Runner already belongs to a Windows CI/service Job Object: support nested jobs or fail before child execution; pinned in task 2.
- Password service is locked/unavailable, or Windows user session lacks credentials: bound the call, give a useful error and never downgrade to insecure storage; pinned in task 4.
- Stale lock file or incompatible lock protocol: recover only through the selected protocol and reject mixed use; pinned in task 3.
- Installed wheel runs outside the repository with no root helper files: all selected helpers must still resolve; pinned in task 5.

## File Structure and Shared Interfaces

Create `quota_sentinel/platform/` with focused modules `paths.py`, `files.py`, `locks.py`, `process.py`, `posix_process.py`, `windows_process.py`, `credentials.py`, `credential_worker.py`, `services.py`, `launchd.py`, `systemd.py`, `windows_tasks.py` and `__init__.py`. OS-only imports stay inside their selected implementation.

- `default_state_dir(system: Optional[str] = None, environment: Optional[Mapping[str, str]] = None) -> Path`.
- `resolve_launcher(name: str, explicit: Optional[Path] = None) -> Tuple[str, ...]`: command prefix, not a shell string.
- `private_open(path: Path, mode: str) -> BinaryIO`, `publish_private(path: Path, payload: bytes) -> None`.
- `acquire_lock(path: Path, *, timeout: float, protocol: str) -> ContextManager[None]`.
- `CommandResult(stdout: bytes, stderr: bytes, returncode: int, timed_out: bool)`.
- `OwnedProcess`: `pid`, `stdin`, `stdout`, `stderr`, `poll()`, `wait(timeout)`, `stop(grace)`, `close()`; close affects only its owned execution tree.
- `spawn_owned(argv: Sequence[str], *, cwd: Path, environment: Mapping[str, str], stdin: object, stdout: object, stderr: object) -> OwnedProcess`.
- `run_bounded(argv: Sequence[str], *, cwd: Path, environment: Mapping[str, str], input_data: Optional[bytes], timeout: float, kill_grace: float, max_bytes: int = 1048576) -> CommandResult`.
- `CredentialStore.read(reference: CredentialReference, *, timeout: float) -> str`, `.write(reference, value, *, timeout: float) -> None`; reference type comes from plan 1.
- `CredentialUnavailable(RuntimeError)` identifies missing, locked, timed-out or unsupported stores without secret values; existing Mac `keychain.read` maps it to its existing empty-string contract.
- `ServiceDefinition(name: str, argv: Tuple[str, ...], cwd: Path, environment: Mapping[str, str], stop_timeout: float)` and `ServiceManager.render/install/start/stop/remove(definition)`; installation never changes authority.

### Task 1: Paths, launchers and private file publication

**Files:** Create `platform/{__init__,paths,files}.py`, `tests/python-platform-paths-regression.py`, `tests/python-platform-files-regression.py`; modify CLI defaults, runner temporary directories, config/state atomic writers.

**Interfaces:** Produce `default_state_dir`, `resolve_launcher`, `private_open` and `publish_private` above. Keep explicit environment/config state-dir overrides highest-priority and preserve current Mac state location.

- [ ] Add `test_platform_state_dirs`, `test_explicit_override`, `test_spaces_unicode_and_metacharacters`, `test_selected_client_only`, `test_private_permissions`, `test_failed_publish_keeps_prior_state`, `test_reader_observes_complete_file`. Expected directories: macOS current Library location; Linux `$XDG_STATE_HOME/quota-sentinel` or `~/.local/state/quota-sentinel`; Windows `%LOCALAPPDATA%/Quota-Sentinel`.
- [ ] Run the path/file scripts; require failures for currently hardcoded directories and missing private-publication behavior.
- [ ] Use platform temp directories and PATH executable lookup. Resolve explicit Python helpers through `sys.executable`; resolve supported npm shims to their declared Node entry rather than interpreting arbitrary shell text. Validate launcher prefixes and keep argv separate. POSIX files use owner-only permissions; Windows uses a restricted owner/SYSTEM/administrator DACL for secret-bearing files. Atomic replace/file flushing follows platform APIs, with documented limits of directory durability rather than pretending POSIX directory fsync works on Windows.
- [ ] Run new suites and config/state regressions; native tests verify actual permissions, publication and path quoting on each OS.
- [ ] Commit paths/file adaptation and tests.

### Task 2: Owned processes and Windows containment

**Files:** Create `platform/{process,posix_process,windows_process}.py`, `tests/python-platform-process-regression.py`, `tests/fixtures/process-tree.py`; modify `run_with_timeout.py` as a delegating compatibility entry.

**Interfaces:** Produce `OwnedProcess`, `spawn_owned` and `run_bounded`; both POSIX and Windows satisfy the same runner-facing contract. Output reading uses bounded worker threads/queues usable with Windows anonymous pipes.

- [ ] Add `test_timeout_kills_term_ignoring_child`, `test_leader_exit_keeps_tree_owned`, `test_parent_exit_cleans_job`, `test_nested_job`, `test_assign_failure_before_resume`, `test_stalled_or_flooded_pipes`, `test_argv_roundtrip`, `test_unrelated_process_survives`. Tests enumerate child/grandchild survivors instead of checking only wrapper exit; synthetic fixtures have no provider calls.
- [ ] Run platform process tests; require red Windows behavior or missing interface before implementation. POSIX-only cases remain explicitly categorized.
- [ ] Preserve POSIX new-session group ownership and escalate after bounded grace even after leader exit. Windows uses documented `CreateProcessW` with a suspended primary thread, a Job Object with `KILL_ON_JOB_CLOSE`, assignment before `ResumeThread`, and controlled inherited pipe handles. Do not permit breakaway; assignment failure terminates the suspended child and closes all handles. Support nested jobs on the target Windows version. Timeout/stop closes the owned tree; pipe readers/writers have deadline/output ceilings and are joined with a bound.
- [ ] Run native Windows and POSIX process suites; confirm release of process/job/thread/pipe handles on launch failure, success, timeout and parent death. Existing helper exit-code/reply contracts remain compatible.
- [ ] Commit the process boundary and helper delegation.

### Task 3: Portable locks with protocol separation

**Files:** Create `platform/locks.py`, `tests/python-platform-lock-regression.py`; modify `state/runlock.py`, `runtime/locks.py`, configuration edit-lock integration and fresh installation metadata.

**Interfaces:** Produce `acquire_lock`; protocol IDs are `macos-shlock-v1`, `posix-fd-v1`, `windows-range-v1`. Fresh provisioning writes `lock-protocol.json`; an old Mac installation without it uses only its existing shlock path, never guesses a Linux/Windows protocol.

- [ ] Add `test_two_process_mutual_exclusion`, `test_crashed_holder_releases`, `test_dead_pid_file_macos`, `test_release_does_not_delete_new_owner`, `test_protocol_mismatch_refused`, `test_stale_config_editor_serialized`. Use child processes and assert no overlapping protected mutation, including after an intentional crash.
- [ ] Run platform/runtime-lock tests; require failures for unsupported native OS/protocol behavior.
- [ ] Keep shlock's PID file behavior on existing Mac directories. POSIX uses a held advisory file descriptor; Windows uses a held file-range lock. Those lock files are not unlinked on release. Record/check protocol separately from authority and use the same selected backend for run/quota/configuration locks. Lock acquisition/polling and any shlock process have finite timeouts. Refuse mixed-protocol state access before reading or mutating scheduler facts.
- [ ] Run native lock tests plus authority/cutover/store regression suites; old Mac shell/Python shlock holders still exclude each other.
- [ ] Commit locks/protocol metadata and tests.

### Task 4: Bounded credential backends

**Files:** Create `platform/{credentials,credential_worker}.py`, `tests/python-platform-credentials-regression.py`; modify `runtime/{keychain,feishu,direct,factory}.py`, `feishu_listener.py`, native usage helpers, pyproject/lock and credential tests.

**Interfaces:** Produce `CredentialStore`; use plan 1 `CredentialReference`. OS backends run in a bounded isolated worker with service/reference arguments and secret data only over private stdin/stdout pipes. Existing `keychain.read/present` wrappers retain compatibility for Mac callers.

- [ ] Add `test_macos_service_names_unchanged`, `test_windows_roundtrip_and_missing_session`, `test_linux_locked_backend_bounded`, `test_no_insecure_backend_fallback`, `test_environment_reference_only`, `test_file_owner_permissions`, `test_secret_never_in_logs_or_argv`, `test_codex_only_no_store_calls`, `test_native_cli_auth_without_auth_json`. Missing optional libraries and unreadable/locked stores return typed availability errors; write failures are reported, not silently ignored. Native-client login stored in its own system backend remains usable without this software reading/copying a native auth file.
- [ ] Run credential scripts; verify failures for absent non-Mac backends and unbounded worker scenarios.
- [ ] macOS keeps current generic-password service/account names; use existing bounded reads and a secret-safe native write interface. Windows uses `CredReadW/CredWriteW/CredDeleteW` and `CredFree`; entry keys map the logical service/account deterministically. Linux explicitly selects supported Secret Service/KWallet backend classes inside the worker, not arbitrary auto-selected plaintext backends. Headless env/file references are explicit and permissions checked through task 1. Optional extras `secret-service` and `kwallet` resolve Python-3.9-compatible dependencies with OS markers/public lock sources. Unavailable credential sources never launch an interactive model/login path.
- [ ] Run fixture suites everywhere; on native OS test ephemeral synthetic credentials with cleanup, never real user entries. Assert stopped/locked workers leave no owned descendants and the current Mac service names/values are not modified.
- [ ] Commit backends, selected dependency extras and call-site migration.

### Task 5: Migrate external boundaries and package helpers

**Files:** Modify `runtime/{models,codex_exec,agy_exec,direct,quota_probe,budgets}.py`, native usage helpers, listener/orchestrator; create `quota_sentinel/helpers/`, package helper resources and `tests/python-wheel-runtime-regression.py`; modify build metadata and root compatibility wrappers.

**Interfaces:** All process sites use task 2 launcher/owned-process boundaries; all private/auth/output files use task 1 publication. `quota_sentinel.helpers.resource_path(name: str) -> Path` provides packaged resources for both source and wheel installation. Root scripts remain thin wrappers around packaged implementations. Plan 2's helper and JS modules are included when present.

- [ ] Add `test_each_runner_uses_platform_boundary`, `test_native_codex_interactive_rpc`, `test_agy_usage_pipe_on_windows`, `test_wheel_foreign_cwd`, `test_helpers_without_repo_files`, `test_cleanup_bounds_recomputed`. Use platform-independent Python fake CLIs via command prefixes, avoiding shebang/chmod assumptions on Windows.
- [ ] Run runner/quota/wheel suites before adapting call sites; verify foreign-wheel/path and pipe failures.
- [ ] Move implementations/resources into the installed package and preserve old entry names as wrappers. Replace unselected hardcoded binary checks; selected direct API may still use its existing bounded curl transport, resolved by PATH/config with secrets on stdin. Codex app-server uses `OwnedProcess` for its persistent exchange; agy/helper streams use Windows-compatible bounded pipe readers. Align every outer budget with actual platform cleanup; include filesystem, credential and helper startup costs where they spend deadlines.
- [ ] Run existing model/direct/Codex/agy/quota/helper/listener/orchestrator tests on each native OS with portable fixtures; run core `-S` imports and wheel execution outside the repository. No post-install network resolution is allowed.
- [ ] Commit packaged helpers and migrated boundaries.

### Task 6: Selected background service installation

**Files:** Create `platform/{services,launchd,systemd,windows_tasks}.py`, `quota_sentinel/install.py`, `tests/python-platform-service-regression.py`; modify CLI, legacy installer/templates and daemon lifecycle tests.

**Interfaces:** Produce `ServiceDefinition/ServiceManager` above and CLI `service install`, `service start`, `service stop`, `service uninstall`; install is separate from start. Definition invokes the installed interpreter/console entry and explicit config path, includes no credential values, and derives stop time from selected runtime budgets.

- [ ] Add `test_selected_components_only`, `test_render_no_secrets`, `test_spaces_unicode_service_paths`, `test_macos_no_duplicate_scheduler`, `test_systemd_user_unavailable_foreground_guidance`, `test_windows_user_identity`, `test_uninstall_keeps_state`, `test_stop_cleans_execution_tree`. Capture installer command arguments and status; no automatic startup from configure.
- [ ] Run service/installer suites; confirm only-Mac listener templates fail the new platform/component cases.
- [ ] Render LaunchAgent for selected Mac components, systemd user unit for Linux, and Task Scheduler XML for Windows. Windows default uses the same current user and an interactive-token logon task; document that logged-out operation is not promised without an explicitly supported alternate credential/logon setup. No system-wide/root service default. Set restart/stop policies and selected extras from configuration; install checks authority but never repairs it. On apply, retire only this app's old duplicate scheduling entries. Non-systemd Linux can run foreground `serve`.
- [ ] Run native install/start/stop/remove tests with isolated temporary service names, synthetic config and fake clients; ensure temporary services removed and no user's installed services touched. Confirm current Mac deployment application remains pending.
- [ ] Commit service adapters/CLI/templates and tests.

### Task 7: Native verification matrix and support documentation

**Files:** Create `.github/workflows/platform-regressions.yml`, `docs/PLATFORMS.md`; modify `tests/run-regressions.py`, portability fixtures, README/ARCHITECTURE and package description.

**Interfaces:** Dispatcher reports `platform-independent`, `native-platform`, and `supplier-smoke` evidence separately. Native platform checks required for a supported feature cannot pass by a silent skip.

- [ ] Add dispatcher tests proving a skipped required native test is reported incomplete; pin wheel/core imports, process cleanup, lock recovery, credential-store status and service lifecycle entries in the matrix.
- [ ] Run dispatcher tests; confirm missing matrix/gating behavior is red.
- [ ] Configure `ubuntu-22.04`, `ubuntu-24.04`, hosted macOS and hosted Windows jobs with Python 3.9 and 3.13, core/selected-extra installs, synthetic credentials and no supplier tokens. Hosted Windows Server results do not count as Windows 11 evidence: the Windows 11 native gate uses an explicitly configured self-hosted `quota-sentinel-win11` runner or a recorded manual run on that OS. Current Mac verification also remains identifiable separately from hosted Mac results. Job failures retain sanitized logs. Linux Secret Service native integration uses an explicit test D-Bus/keyring session; unavailable OS credential/service environments are reported as unverified capabilities, not full support. Document actual platform/feature results and headless/user-session limits, selected install commands and explicit service migration.
- [ ] Run available native suites and collect CI results for the tested commit. If an OS environment is unavailable, finish the adapters/tests/docs but leave that platform's native verification outstanding; never claim all platforms pass based on Mac mocks. Run compile checks and `git diff --check`.
- [ ] Commit verification matrix and accurate platform docs; main integration/current service activation remains a separate user decision.

## Primary References

- Windows containment and lifecycle: [Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects), [CreateProcessW](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw).
- Windows file-range locking: [Python msvcrt](https://docs.python.org/3/library/msvcrt.html).
- Credential APIs and Linux session prerequisites are linked in the shared spec.

## Handoff

Review alongside plans 1–2 and choose execution method. Platform support is delivered by working selected components plus honest native verification, rather than by adding OS names to packaging metadata.
