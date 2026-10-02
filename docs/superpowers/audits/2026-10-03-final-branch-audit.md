# Implementation audit and fix record — 2026-10-03

The approved configuration, Pi metadata and platform plans are implemented on `codex/modular-platforms`. The sole fresh reviewer checked `0a382f2..b1b67c5` and found 15 actual defects (P1 × 1, P2 × 10, P3 × 4). Every finding entered one implementer fix pass; no second reviewer or DSH was used. This record preserves the original findings below, followed by fix evidence and all implementation rulings. The original readiness verdict describes the **reviewed baseline**, before these corrections.

## Fix evidence

Every named regression failed on the unfixed implementation before its fix passed. The first complete fix pass passed **49/49** scripts with required current-macOS native gates. Integration then caught and fixed integer overflow in malformed environment counts, app counts and installed stop metadata, with additional observed RED→GREEN cases. Final frozen-revision evidence and sanitized logs are local artifacts under `.zcode/audits/`; run the command below to recreate the report on another host.

| Finding | Corrected behavior | Regression |
| --- | --- | --- |
| 1 | Inspect actual Windows file handle, reject foreign owner/reparse/directory, protect DACL before bytes | `python-windows-file-boundary`: three owner/object/order cases |
| 2 | Require unique explicit 300/10080-minute periods; allow reversed order | `python-quota-probe`: native period cases |
| 3 | Opening and query share application override → CODEX_HOME → user default | `python-runtime-budget`: standard Codex home |
| 4 | Pi auth, SDK and plugin coordinates honor supplied HOME/PATH and tilde paths | `python-pi-live`: supplied-home case |
| 5 | Validated effective settings overwrite raw compatibility variables | `python-runtime-budget`: empty/invalid/nonfinite/valid environment cases |
| 6 | Disabled usage has zero command budget; listener still imports, runs and stops | `python-runtime-budget`, `python-daemon`, `python-platform-service` |
| 7 | Stop/remove use installed identity and timeout, independently of configuration/authority health | `python-platform-service`: broken/missing configuration and authority |
| 8 | Cancellation state/generation persists across spawn registration; late process stopped; explicit start resets | `task-orchestrator`: deterministic spawn/cancel and before-run cases |
| 9 | Installation/diagnostics union enabled serve/run/usage dependencies | `python-platform-service`: manual Secret Service/KWallet cases |
| 10 | Test card uses saved credential references and selected recipient/transport | `python-config-cli`: saved account refs |
| 11 | Integer counts/ceilings and 0/1 preflight validated; zero transient retries preserved | `python-config`, `python-config-migration` |
| 12 | Nonobject credentials rejected through ConfigurationError | `python-config`: malformed containers |
| 13 | Shared strict journal/runtime provider schemas reject bad elements before publication | `python-config`: journal preserves prior bytes and runtime metadata |
| 14 | Keep first usable stale reading for display after listed live failures; fresh=False prevents anchors | `python-quota-probe`: cache retention |
| 15 | Editor offers full inventory when valid minimal config omits disabled providers | `python-config-cli`: minimal profile |
| Integration hardening | Unrepresentable app count yields typed config error; oversized compatibility count ignored; corrupt installed timeout retains bounded stop identity | `python-config`, `python-config-migration`, `python-platform-service`: overflow cases |

```sh
.venv/bin/python tests/run-regressions.py --platform current --require-native --report .zcode/audits/final-macos-report.json --log-dir .zcode/audits/final-suite
```

These results cover the current Mac and platform-independent fixtures. Windows 11 and Ubuntu native files/processes/stores/services remain **UNVERIFIED**; Windows API fixtures are not native proof. Supplier smoke is **NOT RUN**. Existing personal configuration and running services were preserved, and no supplier/model request, main integration or remote push was performed. No optional polish findings were deferred.

Stop/remove read bounded owned installed metadata; absent/corrupt metadata uses a conservative 300-second cleanup timeout. They never infer or repair scheduler authority. Install/start retain strict profile/authority checks. A disabled listener usage command does not acquire its client/credential dependencies.

## Rulings I made (complete; identical shared preflight ruling shown once)

- keep legacy library factory behavior behind explicit absence of a runtime plan, while production CLI resolves a plan — preserves old callers/tests and switches configured users deliberately — cost if wrong: legacy API users retain their old eager dependencies.
- introduce the optional Application runtime_plan constructor now — selected factory construction consumes it before task 4 adds behavior — cost if wrong: constructor addition must be moved, no live activation.
- fresh provisioning also seeds the full provider inventory before authority publication — integration exposed missing-state errors in task 2 — cost if wrong: four empty history documents exist for disabled providers; no runtime/dependency use.
- make opening-time snapshot capture selectable — direct/agy snapshot hooks otherwise issue unselected usage requests and spend uncounted deadlines — cost if wrong: profiles omitting pi-snapshot no longer produce that unused artifact; migrated profiles retain capture.
- plist regression reads/render-parses committed templates — clean worktrees do not contain ignored personal plist output — cost if wrong: installed deployment drift remains a separate native service check.
- unsaved legacy installations retain legacy runtime until migration is saved/applied — incomplete service preferences cannot silently activate new composition — cost if wrong: pruning requires saving a profile first.
- legacy launchagent installer explicitly requests the feishu extra — its only active entry is the Feishu listener; generic selected installer is plan 3 — cost if wrong: users should use the new generic installer for non-Feishu services.
- retain the existing legacy plist and add a selected-service template — the unsaved personal installation must keep its entry; plan 3 renders the new host only after explicit application — cost if wrong: two clearly separated service templates need maintenance.
- persistence metadata uses the state writer family, with versioned activation CAS — architecture audit and interleaved edit fixture exposed duplicate publication and lost activation — cost if wrong: interrupted edits conservatively require another fresh probe.
- legacy entrypoint tests pin all external clients/auth to private fixtures; budget test reads the exported bound instead of a literal AST — private HOME exposed hidden reliance on operator clients, credentials and literal budget representation — cost if wrong: native real-client smoke remains a separate explicit gate.
- Antigravity expired credentials fail with auth_expired for now — its installed 0.9.0 refresh exports use independent Undici fetch, which would bypass the global metadata guard; only verified guarded owner refresh may be added — cost if wrong: an expired selected Pi Antigravity account requires refreshing in Pi before this query tier can succeed.
- validate intercepted raw Antigravity quota-summary before plugin normalization — plugin 0.9.0 fills absent fractions with zero and clamps invalid values — cost if wrong: unsupported/ambiguous server groups are unavailable and only listed fallback can continue.
- introduce Tier.PI_LIVE before task 4 dispatch — the task 3 QuotaReading producer needs the typed enum; legacy ladder and freshness membership remain unchanged until wiring — cost if wrong: one enum declaration moves between task commits.
- the byte-only platform/files.py primitive sits below the state writer family and is excluded from the semantic writer audit, while all other platform modules remain audited — required for native private publication without moving scheduler policy — cost if wrong: misuse of this primitive could evade a filename-only audit; authority routing and transition audits remain in place.
- migrate the Pi refresh parent to spawn_owned now, ahead of task 5 — the helper cleanup adds startup/reap time, and the old parent snapshot lost a stubborn child after Pi exited on TERM; the existing orphan regression reproduced this during the full suite — cost if wrong: early consumer migration could change refresh diagnostics; all 32 model cases pass.
- fresh configuration-only directories may initialize their native lock protocol before the first editor lock; existing scheduler directories without metadata remain refused on Linux/Windows, and old Mac directories keep shlock — configuration can be saved before authority provisioning without guessing scheduler ownership — cost if wrong: a configuration-only directory transferred across systems needs deliberate native reprovisioning.
- native Mac reads first use noninteractive Security.framework, with the existing bounded security reader retained for older CLI-trusted entries — synthetic roundtrip exposed a CLI permission wait for new native writes; references and legacy service/account names are preserved — cost if wrong: an old ACL can still refuse a native write; that failure is explicit and never changes the credential silently.
- installed implementations and resources live under quota_sentinel/helpers; root entries alias the packaged modules — preserves legacy imports and monkeypatches while wheel installations no longer depend on a checkout — cost if wrong: external callers relying on implementation __file__ see the package path.
- runner wrappers get an owned parent deadline after their inner timeout, kill grace and canonical 7-second cleanup allowance — the old no-outer-deadline assertion could not prove bounded cleanup on Windows; CLI arguments and inner exit contracts are retained — cost if wrong: cleanup exceeding the derived allowance is cut off explicitly.
- native AGY metadata invokes the installed Python helper directly, retaining the old uv_bin argument only for compatibility — the helper is stdlib-only and needs no environment resolution — cost if wrong: callers expecting uv startup side effects no longer get them.
- test homes remain isolated while an explicit public build-cache coordinate is passed to offline wheel verification — a fresh HOME hid pinned hatchling artifacts and falsely failed the installation gate — cost if wrong: the offline gate requires its documented build-cache bootstrap.
- private_directory also protects task history and runner directories with inherited Windows DACLs — chmod alone cannot protect SQLite WAL/SHM or CLI-created files on Windows — cost if wrong: unsafe or foreign-owned existing directories are refused instead of silently adopted.
- service install validates the saved profile and authority and prepares current-user entries; start is the explicit application step — configuration/save must never restart the existing deployment — cost if wrong: users need the separate start command after reviewing/installing a change.
- Windows uses a restricted nonsecret launch manifest and an in-process host to apply PATH/session coordinates — Task Scheduler Exec XML has no per-action environment dictionary; arbitrary shell text and credential values remain excluded — cost if wrong: transferring a registered task without its manifest requires reinstalling that task.
- the legacy Mac installer delegates saved profiles to the selected service and synchronizes only their extras, while unsaved legacy deployments retain the existing Feishu listener entry — current personal installation remains unchanged until an explicit saved-profile application — cost if wrong: an unsaved deployment keeps its historical listener dependency.
- fixtures and dedicated native gates have separate evidence classes; native exit 77 is UNVERIFIED, and --require-native makes incomplete capabilities fail — missing desktop/user sessions cannot be silently reported as support — cost if wrong: strict hosted jobs may remain incomplete until their synthetic session is provisioned.
- retain hatchling 1.32.4 for Python >=3.10 and pin 1.27.0 for Python 3.9 — public metadata and an actual /usr/bin/python3 build reproduced the declared-3.9 installation failure (>=3.10-only backend); the conditional build now installs and runs away from the repository — cost if wrong: the older backend must keep producing the same packaged-resource contract, pinned by the installed-wheel gate.
- Ubuntu/Windows native service gates are checked in, while unavailable real OS/session proof remains outstanding — this Mac cannot certify those platform implementations; the plan explicitly permits delivery with accurate UNVERIFIED capabilities — cost if wrong: those systems may reveal additional integration fixes before support is promoted.
- unavailable Windows 11/Ubuntu native proof — preserve explicit UNVERIFIED capability records; approved Task 7 allows this evidence boundary — cost if wrong: native integration may require further fixes before promotion.
- supplier-authenticated compatibility — preserve NOT RUN smoke evidence because real supplier/model/credential use is not authorized — cost if wrong: upstream client/API behavior may need a later explicitly authorized smoke fix.
- intentionally rapid POSIX daemonization — owned groups/observed descendants remain the documented scope; ordinary spawn/cancel race is a finding and will be fixed — cost if wrong: an intentionally detached unobserved process can escape that scope.
- service secret environment values — retain explicit session/file/store sources and exclude secrets from templates — cost if wrong: a service session without its selected environment credential fails explicitly until configured.
- unsaved legacy eagerness — retain compatibility until saved-profile application, as approved; configured paths retain pruning — cost if wrong: unsaved legacy callers still need historical dependencies.
- arbitrary hostile third-party SDK networking — retain inspected compatible SDK/plugin boundary and metadata guard; unsupported builds return unavailable, no general hostile-code sandbox claim — cost if wrong: an unverified plugin update must be refused or audited before use.

## Original independent review (frozen baseline)

# Whole-branch final review

Reviewed range: `0a382f2..b1b67c568fe31cf122ede0ecff60311e28274dd6`.
Workspace: `__REPO_DIR__` (the isolated implementation checkout).
Reviewer: sole fresh-context senior reviewer. Read-only review; this ignored report is the only checkout file written. No delegation, DSH, real credentials, supplier requests, model requests or service changes.

## Strengths and evidence

The branch implements substantive platform boundaries rather than flags alone: suspended Windows process creation and Job assignment, separate lock protocol metadata, bounded credential workers, private publication, selected runtime composition, explicit configuration CAS and durable activation journals. Pi live queries use a separate metadata helper with route filtering installed before SDK loading, bounded output, nonce/provider checks, and strict normalization. Packaged resources remove the former dependency on checkout-root helpers. Default and migration profiles are deliberately distinct.

I reviewed the three specs/plans and ledger rulings, then the touched implementation in passes: configuration/migration/selection/application, opening/query adapters and Pi JS helpers, platform process/files/locks/credentials, packaging, daemon/services, and verification boundaries. Relevant surrounding legacy call sites were inspected as part of those passes. The supplied final report records 48/48 passing scripts for the current macOS scope, no incomplete required current-system entries, and no supplier smoke test. I did not repeat the broad suite. Focused probes used temporary nonsecret JSON, injected fake protocol/process boundaries, and configuration constructors; their results are recorded below. HEAD remained the frozen commit and tracked working tree remained clean.

## Findings

All paths below are absolute; line numbers refer to the frozen HEAD. These are actual behavior defects, not optional style suggestions.

### 1. [P1] Verify existing Windows file ownership before changing its DACL or writing

File: `quota_sentinel/platform/files.py:64` (through line 94).

For an existing writable file, `_windows_open` changes only its DACL using `0x80000004`, then opens/truncates/appends without verifying its owner. The owner specified in the creation security descriptor does not replace an existing object's owner. A file owned by another ordinary user who granted the caller write/DACL rights therefore remains foreign-owned after private bytes are written; that owner can regain read access by changing the DACL. Existing private stdout/auth/log files are callers of this primitive. POSIX and `protect_directory` already reject foreign ownership.

Fix: open the actual object without following a reparse point, validate owner and object identity on that handle before mutation, then protect the DACL and write. Add native coverage for a foreign-owned writable existing file. This finding is based on the explicit native API flags and control flow; I did not claim a Windows-native reproduction.

### 2. [P2] Reject unknown, missing and duplicate native Codex quota periods

File: `quota_sentinel/runtime/quota_probe.py:315`.

The `<=360` classification and missing-field defaults accept ambiguous windows. Two 300-minute periods produce a fresh result with the primary window used for both five-hour and weekly quota. A 720-minute/10080-minute pair uses the weekly window as the five-hour window. Missing periods are also guessed. These results can change scheduler anchors using the wrong reset period.

An independent injected fake app-server response with two 300-minute periods returned `fresh=True` and identical primary reset timestamps in both normalized windows. Validate exact supported periods (300 and 10080), their types and uniqueness, allow reversed ordering, and return unavailable for incomplete or unknown periods.

### 3. [P2] Use the same effective Codex home for opening and quota queries

File: `quota_sentinel/runtime/codex_exec.py:225`.

With standard `CODEX_HOME=/selected-account` and no application-specific home override, the collector preserves that home, while `CodexExecConfig.from_env` derives `HOME/.codex` and the opening runner forcibly sets `CODEX_HOME` to it. The app can query one account and open a window on another, or reject a valid logged-in account.

A constructor-only probe confirmed distinct query/opening homes. Resolve application-specific override, standard `CODEX_HOME`, then platform default once, and share that effective value across readiness, query and opening.

### 4. [P2] Resolve Pi live authentication paths from the supplied environment

File: `quota_sentinel/runtime/pi_live.py:77` (also SDK/plugin path helpers).

`PiLiveQuotaClient(environment=...)` passes the supplied HOME to its child but derives the auth file with ambient `Path.home()`/`expanduser()`. An isolated caller or alternate user-home environment can query the ambient user's Pi credentials instead of the selected home. Plugin discovery has the same inconsistency. No account-scope hash can detect this because the wrong auth file was selected before querying.

A fake `run_bounded` capture with a temporary HOME received that temporary HOME in the child environment but an auth path under the ambient home; no auth file was read. Resolve HOME/USERPROFILE and tilde paths using the effective environment, and use its PATH for implicit executable discovery.

### 5. [P2] Do not reintroduce rejected raw environment overrides into execution

File: `quota_sentinel/runtime/selected_factory.py:30`.

`resolve_config` ignores an invalid compatibility override and retains the saved numeric setting, but `runtime_environment` uses `setdefault`, preserving the invalid raw variable. Downstream legacy parsing then uses its own default or nonfinite value. Consequently displayed configuration and derived timeout budgets differ from execution.

Focused reproduction: saved `budgets.pi.timeout=1`, environment `QUOTA_SENTINEL_MODEL_TIMEOUT=invalid`; effective setting is 1 but `ModelRunnerConfig.timeout` is 300. The outer watchdog can kill work that the inner runner considers within budget. Serialize already-resolved effective values over the raw compatibility keys, or construct runners directly from validated settings. Cover empty, invalid, nonfinite and valid overrides.

### 6. [P2] Permit a listener when independent quota queries are disabled

File: `quota_sentinel/install.py:18`; related `quota_sentinel/helpers/feishu_listener.py:66` and `:390`.

A valid profile with automatic opening off, quota queries off and listener on cannot install/start: the listener budget unconditionally builds a `usage` plan, which rejects disabled queries. The listener also computes this at import and startup despite separately gating usage commands.

An isolated `service_definition` probe produced `ConfigurationError: independent quota queries are disabled`. Derive no usage budget when that command is disabled, and keep module import independent of an enabled query plan. Test both service installation and foreground listener startup for this feature combination.

### 7. [P2] Allow service stop/uninstall when configuration or authority needs repair

File: `quota_sentinel/__main__.py:475`; related `quota_sentinel/install.py:15`.

Every lifecycle action first parses the current configuration and requires its file plus valid authority. If config JSON becomes invalid, the file is removed, or the authority manifest is lost, `service stop` and `service uninstall` fail before reaching the service manager. A running deployment then cannot be stopped through the documented CLI precisely when the operator needs to repair it. `allow_inactive` only bypasses feature selection, not these checks.

Stop/remove should resolve the installed service identity and use a conservative cleanup timeout without requiring valid scheduler facts. Keep strict configuration and authority gates on installation/start. This is a direct control-flow finding; no real service was touched.

### 8. [P2] Close the cancellation race while a scheduler subprocess is being created

File: `quota_sentinel/helpers/task_orchestrator.py:388` (and `:402`).

`SubprocessRunner.run` spawns outside the lock and only later registers `_active`; `cancel` records no persistent cancellation state. Stopping the host during that interval sees no active process and returns. The newly created check then waits/runs normally, potentially making model calls after shutdown was requested. `TaskOrchestrator.stop` eventually times out its thread join; POSIX ownership is not automatically tied to parent death.

A controlled fake spawn blocked before returning; cancellation during that interval followed by spawn release resulted in zero `stop` calls and entry into `wait`. Add cancellation/generation state synchronized with registration and immediately stop a process created after cancellation. Cover the deterministic spawn/cancel interleaving.

### 9. [P2] Select credential extras for enabled manual operations too

File: `quota_sentinel/install.py:21`; related `quota_sentinel/config/diagnostics.py:41`.

Installation and Linux optional-module diagnostics derive extras only from a `serve` plan. In a query-only/manual profile, that plan has no probes/opening, so an explicitly selected Secret Service or KWallet reference is omitted even though `usage` needs it. Core-only installs then fail credential lookup while static validation does not identify the missing selected dependency.

Probe: automatic opening off, queries on, OpenCode native-only, `secret-service:test` reference: installation extras `()`, actual usage extras `('secret-service',)`. Build the installation/validation dependency union of enabled entry points, while keeping runtime construction command-specific and excluding truly unselected features.

### 10. [P2] Honor saved credential references in send-test-card

File: `quota_sentinel/__main__.py:534`.

Unlike the migrated usage/discovery paths, `send-test-card` ignores `--config` and calls legacy `notifier_user_id`/`send_payload`. A profile using explicit file/environment-reference/Linux-store credentials is tested against legacy names instead. It can fail despite correct saved settings or send the diagnostic to the old recipient/account if legacy credentials still exist.

Resolve the effective profile and create the same selected Feishu credential transport used by configured operations. The user's explicit diagnostic command authorizes sending a test, but should not change which account/configuration is selected. This was established by inspection without sending any message.

### 11. [P2] Validate retry counts separately from positive time budgets

File: `quota_sentinel/config/store.py:14`.

The shared budget-number rule rejects `agy.transient_attempts=0`, which is the meaningful way to disable transient retries, but accepts 0.5 and 1.5, later silently truncated by the runner. Legacy override migration also ignores zero and retains default retries, so an existing zero-retry preference is not preserved.

Independent parsing probes confirmed zero rejected and both fractions accepted. Use field-specific types/ranges: nonnegative integer transient retry counts, positive finite timeouts, and the intended integer/boolean rules for count ceilings/preflight. Add a zero-retry legacy migration pin.

### 12. [P3] Validate the credential-reference container before iterating

File: `quota_sentinel/config/store.py:46`.

`credentials: null`, an array or a string raises uncaught `AttributeError` from `.items()`. Both `read_config` and the CLI's typed error handler omit that exception. A malformed user-edited configuration produces a traceback instead of the promised actionable configuration error.

Independent probes confirmed all three shapes. Require a dictionary before iteration and reject malformed reference maps through `ConfigurationError`; cover public show/validate/runtime entry points.

### 13. [P3] Apply the same strict activation-journal validation when saving

File: `quota_sentinel/config/store.py:74`.

The write path invokes `.get` before verifying a dictionary and validates `pending` only as a list. A top-level array produces `AttributeError`; nested list entries produce `TypeError`; unknown provider strings are accepted and republished. The latter lets a disable/save report success even though `read_journal` will subsequently reject it and abort checks. `runtime-providers.json` similarly validates list containers but not provider element shapes before converting to sets.

Use one strict journal/activation schema reader at all read/write sites and wrap failures as a typed actionable error before publishing. Reject unknown providers and nonstrings, preserve prior config bytes, and test malformed record/container/element cases. The save-path findings follow directly from these operations and were independently supplied as controlled temporary-file reproductions by the executor; I verified the differing read/write validation in source.

### 14. [P3] Keep a usable cached reading for display when scheduled live tiers fail

File: `quota_sentinel/runtime/quota_probe.py:462`.

During `purpose='schedule'`, a listed usable cache is discarded while continuing to live tiers. If all listed live tiers fail, the result is an empty reading; recovered-task/post-run notifications therefore lose the available cached quota. The spec explicitly requires continuing the live search while retaining cache for display. Current freshness checks are correctly strict for anchors, but the display data is lost.

Remember the first displayable reading, prefer a valid live result for scheduling, and return/retain the stale display fallback with `fresh=False` when no live reading succeeds. Keep the no-anchor-update rule. No supplier call is needed to pin this with a cache-first fake-tier test.

### 15. [P3] Let the editor enable providers omitted from a valid minimal profile

File: `quota_sentinel/configure.py:23`.

The supported minimal JSON profile can list only Codex; omitted providers mean disabled. The editor iterates only providers already present, so reopening that valid profile never offers Antigravity/OpenCode/ClinePass. Users cannot enable them through the advertised configuration interface.

A prompt-capture probe against a Codex-only provider map offered only Codex. Build editor choices from the full supported inventory with disabled defaults for absent providers while preserving existing provider choices.

## Declined to judge

- Native Windows 11 and Ubuntu 22.04/24.04 success: no such execution environment/evidence was available in this review. The approved Task 7 boundary explicitly leaves it UNVERIFIED; macOS mocks and configured CI are not native proof. Static defects above remain actionable despite that evidence boundary.
- Supplier-authenticated live quota/opening compatibility: no real supplier, model or credential access was authorized for this review. Existing fake/inspected-SDK evidence is not claimed as a supplier smoke test.
- POSIX intentionally rapid detached daemonization escaping the observed descendant set: inspected and set aside because `docs/PLATFORMS.md` explicitly narrows the process guarantee to owned groups and observed descendants, rather than a general sandbox. This does not excuse the ordinary cancellation registration race in finding 8.
- Serializing environment credential values into service definitions: intentionally not done, because the approved security rule forbids service-template secrets and the platform documentation requires credentials to be present in the service session or use an explicit file/store reference. I do not recommend weakening that boundary.
- Unsaved legacy library/runtime eagerness: retained intentionally under the explicit ledger rulings; configured production paths are the pruning target.
- Untrusted arbitrary third-party modules bypassing global fetch via a separate network stack: the reviewed helper targets inspected compatible SDK/plugin interfaces, and unsupported builds are documented unavailable. I did not treat this as a general hostile-code sandbox claim. The selected supported code still must preserve the metadata-only contract.

## Readiness verdict

**Not ready to merge.** Fix the 15 findings above, prioritizing the Windows existing-owner disclosure risk, account/home consistency, quota-period validation, timeout/config consistency and service lifecycle defects. Then run focused regression checks for those fixes and the required project gates. The current 48/48 macOS report is useful evidence, but its covered cases do not invalidate the uncovered triggers above. Native Windows/Linux verification and supplier smoke remain separately bounded; this report authorizes neither main integration nor service activation.
