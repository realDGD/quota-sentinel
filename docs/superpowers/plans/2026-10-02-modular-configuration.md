# Modular Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not use DSH. Execution method remains for the user to choose.

**Goal:** Deliver selectable features/providers and explicit opening/query chains on the current macOS installation, preserve its personal configuration, and provide independent fresh-user defaults.

**Architecture:** A versioned configuration produces one command-specific runtime plan. Independent runners have no internal fallback; one chain executor controls order. A generic daemon hosts the selected scheduler/listener, with optional dependencies loaded only when selected.

**Tech Stack:** Python >=3.9, stdlib dataclasses/JSON/unittest, uv optional extras, existing native Codex/agy and direct runners; selected Pi provider plugins.

**Spec:** `docs/superpowers/specs/2026-10-02-modular-config-and-platforms-design.md`, sections 1–6, 8.3, 9. This is plan 1; plan 2 adds `pi-live`, plan 3 provides native platform adapters and wheel deployment.

## Global Constraints

- New installation: only Codex, automatic opening and independent quota queries on, both chains native-only, both Feishu features off, no fallback.
- Current personal profile: four providers; Codex Pi→Codex, Antigravity agy→Pi, OpenCode/ClinePass direct; native→CodexBar live→CodexBar cache→Pi snapshot for queries.
- Preserve current timings, limits, credential references, backend authority, history and macOS shlock interoperability.
- Configuration and core import graph use the standard library; optional dependency installation occurs only at setup, never runtime.
- Only fresh quota observations anchor scheduling; caches remain stale regardless of chain position.
- Missing/incompatible Pi plugins never select the default Pi provider/model. Antigravity and ClinePass Pi require visible plugin guidance, including when Pi is a fallback.
- No real supplier/model calls during automated tests; no service restart, credential import, main merge or push as a side effect of implementation.

## Review Focus

- Existing directory with a missing authority manifest: never treat it as a fresh deployment; pinned in task 2.
- Interrupted or concurrent configuration edit: retain a complete prior file and reject a stale editor; pinned in task 1.
- Disabled provider with retry debt: do not replay its old debt on re-enable; pinned in task 4.
- Cached reading listed before live reading: display may use cache, scheduling must continue to a listed live source; pinned in task 4.
- Selected plugin is globally installed but disabled in the isolated Pi invocation: resolve/load its explicit entry and verify the registered provider/model; pinned in task 5.

## File Structure and Shared Types

Create `quota_sentinel/config/{__init__,types,defaults,store,edit_lock,migration}.py` for pure settings, independent defaults, atomic persistence, serialized editing and legacy capture. Create `runtime/selection.py` for dependency selection, `runtime/chains.py` for execution order, `runtime/pi_plugins.py` for plugin requirements, `configure.py` for the user flow, `daemon.py` for hosting, and `state/new_installation.py` for explicit fresh provisioning. Keep the existing provider state/policy APIs.

The following names are fixed across all three plans:

- `CredentialReference(kind: str, locator: str, account: str = "quota-sentinel")`: reference only; kinds `system`, `environment`, `file`.
- `FeatureSettings(automatic_opening: bool, quota_queries: bool, feishu_push: bool, feishu_listener: bool)`.
- `ProviderSettings(enabled: bool, opening_enabled: bool, opening_chain: Tuple[str, ...], quota_chain: Tuple[str, ...])`.
- `SoftwareConfig(schema_version: int, origin: str, features: FeatureSettings, providers: Mapping[str, ProviderSettings], app: Mapping[str, int], budgets: Mapping[str, Mapping[str, float]], clients: Mapping[str, str], credentials: Mapping[str, CredentialReference])`.
- `EffectiveConfig(settings: SoftwareConfig, sources: Mapping[str, str], revision: Optional[str])`; revision is the SHA-256 of saved bytes for edit conflict detection.
- `RuntimePlan(command: str, active_providers: Tuple[str, ...], opening_providers: Tuple[str, ...], probe_providers: Tuple[str, ...], opening_chains: Mapping[str, Tuple[str, ...]], quota_chains: Mapping[str, Tuple[str, ...]], dependency_ids: FrozenSet[str], notify: bool, start_scheduler: bool, start_listener: bool)`.
- Existing `PreparedPaths`, `AttemptResult`, `QuotaReading`, `ProviderQuota` and `AppConfig` remain the execution/state boundaries.

## Execution Preconditions

- Preserve and commit the previously verified three fixes as their own change before isolating feature work; do not discard the fourteen existing modified files. Use the worktree skill at execution time; a new worktree does not automatically contain uncommitted fixes.
- Capture installed nonsecret settings before changing defaults. The existing private migration observation is evidence, not an active configuration or a public fixture.
- Record the baseline with the repository's 22 unittest scripts using a bounded harness; previously 680 tests passed. Task 9 adds the permanent portable dispatcher; later counts naturally increase.

### Task 1: Validated configuration and independent defaults

**Files:** Create `quota_sentinel/config/{__init__,types,defaults,store,edit_lock}.py`; create `tests/python-config-regression.py`.

**Interfaces:** Produce `new_user_defaults() -> SoftwareConfig`, `parse_config(document: Mapping[str, object]) -> SoftwareConfig`, `read_config(path: Path) -> EffectiveConfig`, `save_config(path: Path, config: SoftwareConfig, *, expected_revision: Optional[str]) -> str`, `configuration_lock(path: Path, *, timeout: float = 15) -> ContextManager[None]`, and `ConfigurationError`. The missing-file case is handled by migration resolution, not by silently defaulting inside `read_config`.

- [ ] Write unittest cases `test_fresh_defaults`, `test_invalid_chain_and_budget`, `test_unknown_schema`, `test_atomic_edit_failure`, `test_stale_editor`. Assert only Codex enabled, chains `('codex',)`/`('native',)`, both Feishu flags false; reject booleans as numeric budgets, NaN, infinity, duplicate/unknown/unsupported channels and enabled empty chains. Inject failure before replace: saved bytes/revision unchanged. An edit using an older revision raises `ConfigurationError`.
- [ ] Run `.venv/bin/python tests/python-config-regression.py`; confirm red assertions/imports caused by missing configuration behavior.
- [ ] Implement the interfaces with immutable dataclasses and a private complete-file atomic write; serialize credential references only. Coordinate compare-and-save through `configuration.lock` using bounded shlock on this first macOS deliverable; plan 3 changes its backend together with the state locks. Disabled unlisted providers remain disabled; enabling another provider uses native-only defaults. Keep new defaults separate from legacy migration.
- [ ] Run the configuration suite; require all cases green, including two competing editors and a read during publication.
- [ ] Commit the new configuration module and its regression script.

### Task 2: Legacy profile capture and explicit new installation

**Files:** Create `config/migration.py`, `state/new_installation.py`, `tests/python-config-migration-regression.py`; modify `state/authority.py` documentation and `tests/python-authority-regression.py` without changing legacy lifecycle semantics.

**Interfaces:** Consume task 1 types; produce `resolve_config(path: Path, state_dir: Path, environment: Mapping[str, str], *, installed_preferences: Optional[Mapping[str, str]] = None, command_overrides: Optional[Mapping[str, object]] = None) -> EffectiveConfig`, `capture_legacy_config(environment: Mapping[str, str], *, installed_preferences: Mapping[str, str]) -> SoftwareConfig`, `initialize_new_installation(state_dir: Path, config: SoftwareConfig) -> BackendAuthority`. Resolution is read-only; persistence happens in configure/install.

- [ ] Add `test_personal_profile_preserved`, `test_existing_config_wins`, `test_missing_authority_not_repaired`, `test_fresh_directory_only`, `test_failed_provision_does_not_start`. Use public fixtures with the observed chains, app values 3/2/30/780/20/60 and the stored runner/probe budgets. Assert no `pi-live`, no copied token values, no authority/epoch changes in legacy resolution; corrupt saved config raises instead of using defaults.
- [ ] Run `.venv/bin/python tests/python-config-migration-regression.py`; require the new contracts to fail before implementation.
- [ ] Implement allowlisted legacy environment capture and precedence (explicit command/env → saved config → fresh defaults). Fresh provisioning must exclusively create a previously nonexistent state directory and explicitly publish `BackendAuthority('json', 0)` there; never invoke removed `initialize_authority` or infer ownership from a missing manifest. An interrupted provision leaves a reported incomplete installation, never a silently repaired deployment. Existing-config migration previews unresolved install preferences instead of guessing them.
- [ ] Run migration and authority regression scripts; compare the personal profile to the private observation locally without committing personal paths/values. No credentials are read for this comparison.
- [ ] Commit migration/provisioning and tests; leave installed services untouched.

### Task 3: Runtime selection and explicit opening chains

**Files:** Create `runtime/selection.py`, `runtime/chains.py`, `tests/python-selection-regression.py`, `tests/python-chain-regression.py`; modify `runtime/{factory,dispatch,models,codex_exec,agy_exec}.py` and existing runner tests.

**Interfaces:** Produce `build_runtime_plan(config: SoftwareConfig, command: str, *, requested: Sequence[str] = ()) -> RuntimePlan`. Produce `AttemptChainRunner(chains: Mapping[str, Tuple[str, ...]], runner_factory: Callable[[str], object])` exposing existing `prepare(provider, workspace) -> PreparedPaths` and `run(provider, workspace, phase, attempt, limit) -> AttemptResult`. Only selected names reach `runner_factory`.

- [ ] Add `test_single_channel_no_fallback`, `test_both_pi_codex_orders`, `test_functional_failure_advances`, `test_cost_success_stops`, `test_unselected_factory_never_called`, `test_query_plan_has_no_model_dependencies`. Capture exact invocation order and assert successful cost regression is not replayed. Explicit disabled-provider requests are refused; an empty active roster stays empty.
- [ ] Run the selection/chain scripts; verify these contracts fail against the existing hardcoded routing.
- [ ] Remove runner-internal fallback and implement chain-owned preparation/order. Primary prepare is reused within the burst; fallback preparation occurs only when actually reached. Build selected components lazily. Add an empty notifier with the existing notifier methods; disabled Feishu means no credential lookup. Readiness reports unavailable selected channels; a chain with another explicitly selected usable channel may continue, but a chain with none fails before model/state attempt start. Native authentication checks use bounded client metadata/status, not assumptions that an `auth.json` file must exist.
- [ ] Run chain, selection, app and existing model/Codex/agy suites; ensure existing reply/completion/usage/cost verdicts and timeout cleanup tests remain green.
- [ ] Commit runtime selection/chains and adapted regression contracts together.

### Task 4: Active roster, query order and safe re-enable

**Files:** Modify `app.py`, `runtime/quota_probe.py`, `quota/adapters.py`, `scheduler/service.py`; create `tests/python-feature-pruning-regression.py`; modify quota/app/scheduler tests.

**Interfaces:** Add `Application(..., runtime_plan: Optional[RuntimePlan] = None)` and `QuotaCollector(..., providers: Sequence[str], tier_chains: Mapping[str, Sequence[Tier]])`; `collect(*, purpose: str = 'display') -> Dict[str, QuotaReading]` accepts `display` or `schedule`. Legacy direct library callers retain explicit legacy behavior; production CLI supplies the resolved plan. Add `service.resume_provider(state_dir: Path, provider: str, observation: QuotaObservation, now: int) -> DecisionResult` to clear obsolete disabled-period retry debt only after a fresh reanchor.

- [ ] Add `test_disabled_dependencies_never_called`, `test_cached_first_is_display_only`, `test_scheduler_skips_cached_result`, `test_reenable_does_not_replay_debt`, `test_only_opening_internal_observation`, `test_bot_disabled_command_refused`. Assert retained historical provider documents, no disabled attempts/probes/cards, and no anchor mutation on stale/failed readings.
- [ ] Run `.venv/bin/python tests/python-feature-pruning-regression.py`; confirm red failures from global roster/hardcoded tiers.
- [ ] Replace runtime global-roster loops with plan rosters while preserving the state inventory. Filter next-due/retry decisions before claiming work. Store enable/disable transitions separately from authority; re-enable is gated by fresh observation before resume. Display queries stop at first displayable listed tier; scheduling follows the same list but accepts only live tiers. Opening-only mode explicitly retains its required internal observation; query-only mode never prepares model runners.
- [ ] Run pruning, quota, app, scheduler and cutover regression scripts; assert low-level state inventory/authority behavior unchanged.
- [ ] Commit active-roster/query-chain integration and tests.

### Task 5: Plugin guidance and ClinePass Pi opening

**Files:** Create `runtime/pi_plugins.py`, `tests/python-pi-plugin-regression.py`; modify `runtime/models.py`, `runtime/factory.py`, model/entrypoint tests and README.

**Interfaces:** Produce `PluginRequirement(package: str, install_source: str, pi_provider: str)`, `PluginCheck(available: bool, entry: Optional[Path], reason: str)`, `plugin_requirement(provider: str) -> Optional[PluginRequirement]`, `check_pi_plugin(provider: str, config: ModelRunnerConfig) -> PluginCheck`, `plugin_guidance(provider: str) -> Tuple[str, ...]`. Replace provider tuples with `PiProviderSpec(provider_id: str, model: str, thinking: str, capture_env: Optional[str], capture_extension: Optional[Path], plugin_id: Optional[str])`; ClinePass has no capture environment/extension. Plugin checks are bounded, metadata-only, and never use `-p` or a fallback model. Extend `ModelRunnerConfig` with optional plugin-entry overrides and retain its existing constructor compatibility.

- [ ] Add `test_antigravity_primary_and_fallback_guidance`, `test_clinepass_guidance`, `test_missing_plugin_zero_model_calls`, `test_wrong_provider_registration`, `test_globally_installed_explicit_entry`, `test_direct_only_no_plugin_check`, `test_clinepass_pi_identity`. Assertions pin install guidance `pi install npm:pi-antigravity` / `pi install npm:pi-clinepass-provider`; fake Pi invocation must explicitly load the selected plugin and select provider `clinepass`, model `cline-pass/deepseek-v4.1-flash`, thinking `off`.
- [ ] Run plugin/model regression scripts; require the new ClinePass path to fail before adding capability. The baseline capability rejection test remains valid until an implementation is present.
- [ ] Resolve manifest-declared extension entry points, verify registered provider/model using supported metadata-only Pi behavior, and report missing/incompatible plugins before invoking inference. Explicitly load selected plugins in the isolated Pi directory with automatic extension discovery disabled. Add ClinePass as a Pi opening capability only with its implemented identity/preflight; it has no fabricated quota capture hook or `pi-live` claim. Missing Pi may proceed to a user-listed direct fallback. Existing ClinePass personal profile remains direct-only.
- [ ] Run plugin/model/entrypoint suites; update the former unconditional `clinepass=pi` rejection test into implemented-capability and missing-plugin checks. Keep other impossible pairs (`opencode=agy`, `codex=agy`) rejected at entry.
- [ ] Commit plugin support, capability mapping and corresponding docs/tests. Do not install plugins into the user's current Pi profile during verification.

### Task 6: Budget derivation from the selected graph

**Files:** Create `runtime/budgets.py`, `tests/python-runtime-budget-regression.py`; modify `runtime/probe_budget.py`, `task_orchestrator.py`, `feishu_listener.py`, existing budget tests.

**Interfaces:** Produce `check_budget(config: SoftwareConfig, plan: RuntimePlan) -> float` and `usage_budget(config: SoftwareConfig, plan: RuntimePlan) -> float`; return full outer limits including 10% margin. Explicit valid `QUOTA_SENTINEL_CHECK_TIMEOUT` stays highest-priority. Core budget calculation cannot import the composition root or optional libraries.

- [ ] Add `test_single_codex_excludes_pi`, `test_each_fallback_prepare_counted`, `test_selected_probe_sum`, `test_plugin_and_cleanup_bounds`, `test_personal_budget_not_understated`, `test_large_query_budget_reaches_listener`. Use auth timeout 10000 and CodexBar timeout 4000 to prove monotonic changes; every legal inner path is below its outer deadline. Personal profile's old 6142.4s is a compatibility lower-bound comparison, not a universal new default.
- [ ] Run budget/orchestrator regression scripts; verify fixed/global derivations fail these selected-graph cases.
- [ ] Sum sequential selected query tiers/providers; sum fallback execution/prepare budgets per provider and take the maximum over parallel providers. Include primary prepare passes, retries/gaps, agy guard/transient retries, plugin/auth/keychain work, two probe phases and actual parent cleanup margins, including the agy extra one second. Compute listener `/usage` and service-stop bounds from selected work plus selected notification budgets.
- [ ] Run runtime-budget, probe, orchestrator and entrypoint import-graph suites. Independently calculate fixture paths rather than asserting the production formula against itself.
- [ ] Commit derived bounds and tests.

### Task 7: Configuration user flow and optional installation

**Files:** Create `configure.py`, `tests/python-config-cli-regression.py`; modify `__main__.py`, `pyproject.toml`, `uv.lock`, `install-launchagents.sh`, README, installer/uv tests.

**Interfaces:** Produce `configure(config: EffectiveConfig, *, input_fn: Callable[[str], str], output_fn: Callable[[str], None]) -> Optional[SoftwareConfig]`, and CLI `configure`, `config show`, `config validate` with optional `--config`. Produce `selected_extras(plan: RuntimePlan) -> Tuple[str, ...]` for setup, initially only `feishu` when listener is enabled. `None` means cancellation with no save.

- [ ] Add `test_edit_existing_starts_from_personal_choices`, `test_cancel_keeps_file`, `test_config_sources_redacted`, `test_pi_plugin_notice_even_as_fallback`, `test_no_runtime_install`, `test_core_install_has_no_lark`. Script inputs rather than adding a TUI dependency; assert saved order differs only in selected edits and output includes the task 5 guidance.
- [ ] Run config CLI and uv/installer scripts; verify missing CLI/options and unconditional lark install fail.
- [ ] Implement feature/provider/order selection, preview and atomic save using tasks 1–2. Remove lark from mandatory dependencies and add `[project.optional-dependencies].feishu`; update the lock with public registry settings. Selected installer extras are explicit, external CLI/plugin dependencies receive guidance, and runtime never invokes an install command. Existing deployments get previewed migration and explicit service application, not an automatic restart.
- [ ] Run CLI/installer/uv suites in core-only and `feishu` environments. Fresh setup uses task 2 provisioning; saved-config edits never touch backend authority.
- [ ] Commit configuration UX, packaging metadata and installer compatibility.

### Task 8: Independent scheduler/listener daemon

**Files:** Create `daemon.py`, `tests/python-daemon-regression.py`; modify `feishu_listener.py`, `task_orchestrator.py`, `__main__.py`, `quota-sentinel.feishu-listener.plist.template` and installer tests.

**Interfaces:** Produce `serve(config: SoftwareConfig, plan: RuntimePlan, *, scheduler_factory: Callable[[], object], listener_factory: Callable[[Optional[object]], object]) -> int`. A listener component exposes `run()` and `stop()` and receives the scheduler reference if enabled; it no longer creates its own scheduler. Existing root listener invocation remains a compatibility wrapper around the selected host.

- [ ] Add `test_opening_only_no_lark_import`, `test_listener_only_no_scheduler`, `test_both_single_scheduler`, `test_listener_reply_with_push_disabled`, `test_stop_joins_and_reaps`, `test_failed_listener_start_no_orphan_scheduler`. Capture creation/start/stop counts and assert cleanup on both signals and startup failure. A selected bot can reply to an authorized incoming command while automatic Feishu push remains disabled.
- [ ] Run daemon/listener/orchestrator suites; confirm red behavior from listener-owned scheduling.
- [ ] Extract listener construction behind lazy import and coordinate lifecycle in `serve`. Separate command replies owned by the selected bot from unsolicited notifications owned by `feishu_push`. Update macOS service rendering according to selected features without modifying authority. Runtime plans remain fixed for one process lifetime; applying saved edits requires an explicit restart and cannot hot-switch runners mid-attempt.
- [ ] Run daemon/installer/listener/orchestrator suites plus a foreign-directory `serve` fixture. Services without selected background features are not installed.
- [ ] Commit daemon ownership and installer/template changes.

### Task 9: Integration and compatibility verification

**Files:** Create `tests/run-regressions.py`; modify README and ARCHITECTURE; add end-to-end cases to config/pruning/installer suites.

**Interfaces:** Produce a regression dispatcher usable by all three plans, `python tests/run-regressions.py --platform current`; no supplier access, inherited real credentials, or unbounded child scripts. Preserve the distinction between platform-independent and native-platform verification.

- [ ] Add `test_fresh_codex_only_end_to_end`, `test_personal_migration_end_to_end`, `test_direct_pi_orders_with_plugin_failure`, `test_disabled_bot_capabilities_end_to_end`. Test discovery must include new suites and propagate any failed script/nonzero exit.
- [ ] Run the new integration cases first and confirm they expose any remaining missing wiring.
- [ ] Resolve wiring failures and document defaults, migration, plugin prerequisites, chain order, feature dependencies, explicit service application and current platform limits. Update stale statements that all providers require Pi or that ClinePass can never use it, only after its adapter passes.
- [ ] Run the complete dispatcher, `compileall` and `git diff --check`; record exact commit/tree tested. A core-only Codex fixture must have zero calls to Pi/agy/CodexBar/security/Feishu, except the platform lock component needed for scheduler writes. No real suppliers required.
- [ ] Commit integration/docs, preserving current runtime activation until the user requests application.

## Handoff

Review this plan together with plans 2 and 3. Suggested execution is native in this chat because the same configuration/runner interfaces recur across the tasks; subagent execution is an available user choice. Implementation starts only after plan review and execution-method selection required by the planning skill.
