# Pi Live Quota Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not use DSH.

**Goal:** Add explicitly selectable Pi-backed live quota queries for Codex, Antigravity and OpenCode without any model invocation, including all failure paths.

**Architecture:** A bounded metadata-only helper uses the selected Pi installation's authentication context and provider usage APIs. A Python adapter verifies its protocol and converts live responses into existing quota objects; it never re-labels a saved snapshot as live. The configurable query chain from plan 1 decides whether this adapter is called.

**Tech Stack:** Python >=3.9 stdlib, Node ESM with the user's supported Pi SDK/plugin installation, unittest and Node built-in tests. No new mandatory Python or npm dependency.

**Spec:** `docs/superpowers/specs/2026-10-02-modular-config-and-platforms-design.md`, section 7 and related chain/freshness requirements. Depends on `2026-10-02-modular-configuration.md` tasks 1–7; packaging and platform cleanup are completed in plan 3.

## Global Constraints

- `pi-live` is opt-in; new and migrated profiles keep their previous query lists.
- No `prompt`, `/usage` submitted as a prompt, `session.prompt`, `stream`, `complete`, model endpoint, or opening runner on this path.
- Codex, Antigravity and OpenCode are the initial implemented providers; ClinePass Pi opening does not imply ClinePass Pi live quota capability.
- Auth and usage calls are bounded and output-limited, secrets remain inside the helper, and unsupported SDK/plugins fail before inference.
- No shared official-client token refresh/copy/write; only the selected credential source's verified authentication owner may refresh it.
- Existing Pi snapshots remain stale; only a successful live request yields a fresh reading.
- Tests use isolated fake auth/plugin modules and deny real network access.

## Review Focus

- Slash command/extension missing: never fall through to a prompt; pinned in task 1 with throwing model exports.
- Package import performs unexpected model/network work: inject the request boundary before importing supported modules and reject model endpoints; pinned in tasks 1–2.
- Selected account differs from returned quota account: reject or report unavailable rather than reanchor another account; pinned in task 2.
- Replayed or truncated helper result: verify nonce/provider/protocol and size before trusting freshness; pinned in task 3.
- Unsupported standalone Pi binary has no compatible SDK/Node runtime: show query-only unavailability and continue only to explicitly listed next tier; pinned in task 4.

## File Structure and Interfaces

Create `quota_sentinel/runtime/pi_live.py` for subprocess/protocol validation and `quota_sentinel/quota/pi_live.py` for provider normalization. Create root `pi_quota_query.mjs` plus focused `pi_quota/{auth,providers,network}.mjs`; these move into package resources in plan 3 while retaining root compatibility wrappers. Add Python and Node fixture tests without installing providers in the current user's Pi profile.

Use plan 1 `SoftwareConfig`, `RuntimePlan`, `CredentialReference`, plugin requirements and budget selection. Reuse current `QuotaReading` and `ProviderQuota`; do not extend the persisted provider-quota schema with authentication fields.

Protocol v1:

- stdin request: `protocol_version`, unpredictable `request_id`, `provider`, selected package/auth/plugin paths and finite `timeout_seconds`; no plaintext credential value.
- stdout response: `protocol_version`, matching `request_id`, matching `provider`, `status` (`ok`/`error`), nonsecret `account_scope`, `queried_at`, and provider metadata payload or a stable `error_code`. Exactly one JSON record; maximum 1 MiB.
- Network boundary `metadataRequest(url: URL, options: object, deadline: number) -> Promise<object>` permits provider usage endpoints and explicitly verified owner-auth endpoints, denies model routes and arbitrary redirects. Endpoint base overrides must be explicit supported configuration, not inferred from untrusted metadata.
- Auth boundary `resolvePiAuth(provider: string, options: object, deadline: number) -> Promise<AuthContext>` returns token/key/account information only inside the JS process. `AuthContext` is never serialized or logged.
- `AuthContext` fields: `provider`, `kind` (`oauth`/`api_key`), `accountScope`, `owner` (`pi`/`foreign-readonly`), and applicable `accessToken`, `apiKey`, `accountId`; a foreign-readonly source cannot refresh or write its tokens.
- Python boundary `PiLiveQuotaClient.query(provider: str) -> QuotaReading` returns failure as an unavailable tier, not an exception that starts another kind of work.

### Task 1: Auth-only helper and no-model guard

**Files:** Create `pi_quota_query.mjs`, `pi_quota/auth.mjs`, `pi_quota/network.mjs`, `tests/pi-live-helper-regression.mjs`, `tests/fixtures/pi-live/` public fake SDK modules.

**Interfaces:** Produce the protocol v1 entry and `resolvePiAuth`/`metadataRequest` signatures above. Request/module loaders are injected in tests; production defaults point only to selected installed components. No `createAgentSession` is needed.

- [ ] Write `testNoModelCallsOnImportOrAuth`, `testUnsupportedSdkFailsClosed`, `testMissingPluginNeverPrompts`, `testAuthTimeout`, `testNoSecretOutput`. Fake SDK prompt/stream/complete methods throw; model-route requests throw; stdout/stderr must not contain fixture access or refresh token strings.
- [ ] Run `node --test tests/pi-live-helper-regression.mjs`; verify failure because helper/auth contracts are absent.
- [ ] Implement a compatible auth-only SDK loader based on inspected Pi 0.99.2 `ModelRuntime`/auth interfaces and narrow plugin auth exports. Set the metadata request guard before provider module loading. No npm install/update, interactive login, credential exchange with unknown owners, or model invocation. Standalone installations lacking required exports return `unsupported_pi_sdk`. Apply one deadline across loading, auth and usage, with parent timeout protection supplied by task 3.
- [ ] Run Node helper tests; test missing exports, import error, auth failure and deadline expiry as well as successful fake auth. Verify no native Codex/agy auth files are modified.
- [ ] Commit helper/auth/network guard and tests.

### Task 2: Three provider metadata adapters

**Files:** Create `pi_quota/providers.mjs`, `quota_sentinel/quota/pi_live.py`; extend helper tests; create `tests/python-pi-live-normalization-regression.py` and nonsecret quota fixtures.

**Interfaces:** Produce JS `queryProviderUsage(provider: string, auth: AuthContext, options: object, deadline: number) -> Promise<object>`. Produce Python `normalize_pi_live(provider: str, payload: Mapping[str, object], *, captured_at: int) -> ProviderQuota`; supported providers exactly Codex/Antigravity/OpenCode. Live sources are named `Pi · live`; snapshot normalizers are unchanged.

- [ ] Add `testCodexAccountScope`, `testAntigravityUsageWithoutAgentEnd`, `testOpenCodeUsageWithoutAgentEnd`, `testModelEndpointRejected`, `testScopeMismatchRejected`; Python tests `test_live_windows`, `test_bad_usage_is_not_zero`, `test_old_snapshot_cannot_be_live`. Assert live five-hour/weekly fields and OpenCode monthly display-only semantics; reject missing/invalid windows, implausible timestamps, unknown/ambiguous quota groups, percentages outside accepted schema and boolean token/count fields.
- [ ] Run Node helper and Python normalization scripts; require failing missing provider/normalizer behavior.
- [ ] Implement Codex quota metadata using Pi-owned valid access credentials/account scope and the verified usage endpoint; do not refresh official `CODEX_HOME`. Antigravity calls the selected plugin's `fetchAccountUsage` metadata function independently of event hooks. OpenCode calls its existing usage endpoint with Pi-resolved key. Validate identity/window payload before fresh normalization; never infer “all quota remaining” from a missing response. Use temporary fake SDK/plugin fixtures to exercise source-version differences.
- [ ] Run tests with request recording; permit only enumerated metadata/auth routes and assert the exact supplier/model-call count is zero. Authentication-owner ambiguity must return an error and never mutate a foreign auth source.
- [ ] Commit provider adapters, normalization and public fixtures.

### Task 3: Bounded Python query adapter

**Files:** Create `runtime/pi_live.py`, `tests/python-pi-live-regression.py`; modify `runtime/probe_budget.py` or task 6 `runtime/budgets.py` to declare live-query budgets from the same source used by execution.

**Interfaces:** Produce `PiLiveQuotaClient(config: SoftwareConfig, *, helper_path: Path, node_bin: Path, run_bounded: Callable)` and `query(provider: str) -> QuotaReading`. Initial live query deadline is explicitly stored as `budgets.pi_live.timeout=30` and `kill_grace=10` when users select this tier; parent cleanup/slack is separately counted. Existing profiles do not acquire the tier merely because its budget field exists.

- [ ] Add `test_verified_live_result`, `test_nonce_and_provider_mismatch`, `test_truncated_or_oversized_result`, `test_hung_helper_no_survivors`, `test_secret_redaction`, `test_no_helper_on_unselected_tier`. A forged record cannot set `fresh=True`; throwing fixtures cover all model executors.
- [ ] Run `.venv/bin/python tests/python-pi-live-regression.py`; verify absent client/protocol failures before implementation.
- [ ] Start the helper with private cwd and bounded process-tree cleanup, pass only nonsecret references/nonce on stdin, verify response structure/identity/nonce and result size, normalize through task 2, and discard raw helper errors in favor of stable codes. Give timeout/cleanup costs to the shared budget calculation. Plan 3 later replaces platform-specific execution underneath the injected boundary.
- [ ] Run Python live-query tests plus shared timeout/budget suites; inspect survivor assertions after timeout, not just the helper exit code.
- [ ] Commit live client and budget integration.

### Task 4: Query-chain integration and end-to-end verification

**Files:** Modify `quota/adapters.py`, `quota/__init__.py`, `runtime/quota_probe.py`, `runtime/selection.py`, `configure.py`, README and ARCHITECTURE; extend config/quota/live/pruning suites and `tests/run-regressions.py`.

**Interfaces:** Add `Tier.PI_LIVE = 'pi-live'`, advertise it per implemented provider, mark it live only when the task 3 adapter has validated a successful request. Chain selection remains user-owned; capability/readiness diagnostics reference Pi SDK/Node/plugin prerequisites separately from Pi opening.

- [ ] Add `test_custom_native_pi_live_order`, `test_unavailable_pi_live_only_listed_fallback`, `test_fresh_and_personal_defaults_unchanged`, `test_missing_sdk_query_failure_zero_models`, `test_pi_live_updates_anchor_only_on_success`. Configuration must not add Pi-live to migration/default lists; ClinePass Pi opening and ClinePass Pi-live are separate capabilities.
- [ ] Run integration cases before wiring; verify missing tier/client dispatch is red.
- [ ] Connect query dispatch, freshness and configuration guidance; list any installed-runtime incompatibility without starting interactive Pi or falling back to a model. Extend the bounded regression dispatcher with Node helper tests. Document that a stored Pi quota file is distinct from a live Pi query and that unsupported Pi builds remain unavailable for this option.
- [ ] Run all Python regression scripts, Node helper tests, compile checks and `git diff --check`. Record the exact tested tree; any real-provider smoke test is a later explicit step and cannot be claimed from fixtures.
- [ ] Commit integration/docs after the zero-model cases pass.

## Handoff

This plan delivers actual live queries for compatible selected Pi installations, not a new label on the existing event-generated snapshots. Review together with plans 1 and 3 before choosing execution method.
