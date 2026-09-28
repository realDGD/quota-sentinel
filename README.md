# Quota Sentinel + Gemini + DeepSeek (Feishu enterprise app)

Runs GPT-6 Luna, Gemini 3.7 Flash (Low) and DeepSeek V4 Flash (OpenCode Go)
in parallel, sends one combined status message through a Feishu enterprise
self-built app, and saves no Pi sessions.

Feishu receives an interactive card with a green header when both model calls
succeed and a red header when either one fails. Its body is equivalent to:

```text
GPT-6 Luna
🟢 发送成功
5 小时：■■■■■□□□□□ 剩余 48%
↳ 重置　2026-08-29 22:04:34 CST（剩余 1小时 14分）
周额度：■■■■■■■□□□ 剩余 65%
↳ 重置　2026-09-04 08:01:57 CST（剩余 5天 11小时 11分）
↳ 来源　CodexBar · codex-cli

────────────

Gemini 3.7 Flash · Low
🟢 发送成功
5 小时：■■■■■■■■■■ 剩余 100%
↳ 重置　2026-08-30 00:21:14 CST（剩余 3小时 31分）
周额度：■■■■■■■■■□ 剩余 91%
↳ 重置　2026-09-03 16:41:58 CST（剩余 4天 19小时 51分）
↳ 来源　CodexBar · cli

────────────

DeepSeek V4 Flash · Off
🟢 发送成功
5 小时：■■■■■■■■■□ 剩余 88%
↳ 重置　2026-08-30 00:21:14 CST（剩余 3小时 31分）
周额度：■■■■■■■■■■ 剩余 95%
↳ 重置　2026-09-03 16:41:58 CST（剩余 4天 19小时 51分）
↳ 来源　Native · opencode-go /usage
本月度　剩余 98%　重置 2026-10-17 11:04:30 CST

■ 剩余　□ 已用
🕒 2026-08-29 20:30:00 CST
```

The layout depends on which providers ran: one provider fills the card with its
two quota windows side by side, the original two providers keep their
two-column card, and any message that includes OpenCode stacks one full-width
block per provider. OpenCode Go is the only plan with a third window, so its
monthly cap is appended to its own block as a grey display-only line.

Each provider is successful only when Pi exits normally and its model replies
exactly `1`. Quota collection is reported independently.

## Architecture at a glance

The Python CLI is the single production entrypoint: it owns the scheduler's
decisions *and* the process side — model runs, quota probes, notification
transport and locks. See [ARCHITECTURE.md](ARCHITECTURE.md) for the
invariants.

```text
quota_sentinel/ (Python)
  state/       authoritative scheduler state + the durable backend authority
  scheduler/   due decisions, deadline calibration, retry debt, transitions
  quota/       provider adapters: fallback ladder, capabilities, normalisation
  runtime/     model runner, quota probe execution, Feishu cards + transport
  __main__.py  the single CLI entrypoint (console script `quota-sentinel`)
```

Every verb is one command — `uv run --frozen --no-sync quota-sentinel <verb>`
from the repository root, or the launchd form
`uv run --project <repo> --frozen --no-sync quota-sentinel <verb>`. Two durable
state backends exist — the historical per-slot files and one versioned JSON
document per provider — and a single durable fact
(`backend-authority.json`) says which one is authoritative. A deployment that
has never run `cutover` keeps using the slot files exactly as before. No zsh
runs in normal operation.

## Quota Data Sources & Architecture

1. **Codex Quota**:
   - **Primary**: Native Codex App Server RPC (`account/rateLimits/read`).
   - **Fallbacks**: CodexBar Live (`cli`, then `oauth`), CodexBar cached
     result, then the Pi provider response-hook snapshot.
2. **Antigravity Quota**:
   - **Primary**: Native built-in `agy -p /usage --output-format json`
     (agy >= 1.1.11, structured metadata only; no model prompt).
   - **Fallbacks**: CodexBar Live, CodexBar cached result, then the Pi
     Antigravity API snapshot.
3. **OpenCode Go Quota**:
   - **Primary**: Native `GET https://opencode.ai/zen/go/v1/usage` with the
     API key (`opencode_usage.py`, metadata only; no model prompt).
   - **Fallbacks**: CodexBar Live (`--provider opencodego --source api`),
     CodexBar cached result, then the direct-transport snapshot. The 5-hour
     rolling window drives the schedule exactly like the other providers; the
     monthly cap is carried for display only and never participates in
     scheduling.
4. **ClinePass Quota**:
   - **Primary**: Native `GET https://api.cline.bot/api/v1/users/me/plan/usage-limits`
     with the API key (`clinepass_usage.py`, metadata only; no model prompt).
     This endpoint is not in Cline's public API reference — it is the one the
     Cline CLI, CodexBar and the community usage tools read — and the helper
     identifies itself with the documented `X-Title` header.
   - **Fallbacks**: CodexBar Live (`--provider clinepass --source api`),
     CodexBar cached result, then the direct-transport snapshot.
     ClinePass reports three limits (5-hour, weekly, monthly) that all share
     one anchor; the monthly cap is display only. Both live tiers reject a
     window without `resetsAt`, which is what an account with no open window
     returns — an idle plan has no boundary to schedule from, and a fabricated
     one would become a deadline.

All live quota calls are metadata-only: they consume no prompt tokens and no
inference turns.

## Feishu `/usage` Command & Long Connection Listener

You can send `/usage` in Feishu bot private chat at any time. The bot connects
via Feishu WebSocket long connection (长连接, no public IP or webhook required):
- Fetches real-time structured quota through the four-tier hierarchy above.
- Responds with the formatted quota card immediately in Feishu.
- Does **not** invoke LLMs or run Pi agent tasks. Fresh Native/CodexBar Live
  results also synchronize `last_known_reset_at` and `next_due_at`; stale cache
  and Pi snapshots remain display-only. `/usage` never changes `last_task_at`
  or `last_triggered_window`.
- If the quota probe is busy, replies with a lightweight
  “⏳ 配额正在刷新，请稍后再试” card instead of staying silent. The busy reply
  performs no quota fetch, no model call, and no scheduler write.
- Filtered against bot loops (ignores non-user messages) and deduplicated.
- Accepts commands only from the configured recipient `user_id`.

## Feishu delivery

The script uses only Feishu. Each credential is read from an environment
variable first, then from these macOS Keychain services under account
`quota-sentinel`:

| Credential | Environment override | Keychain service |
| --- | --- | --- |
| App ID | `FEISHU_APP_ID` | `quota-sentinel.feishu-app-id` |
| App Secret | `FEISHU_APP_SECRET` | `quota-sentinel.feishu-app-secret` |
| Recipient user ID | `FEISHU_USER_ID` | `quota-sentinel.feishu-user-id` |

`FEISHU_DRY_RUN=1` prints the message instead of calling the API.

The two API-key providers keep their own credential, used both by the quota
ladder and by the direct model transport:

| Credential | Environment override | Keychain service |
| --- | --- | --- |
| OpenCode Go API key | `OPENCODE_API_KEY` | `quota-sentinel.opencode-go-api-key` |
| ClinePass API key | `CLINE_API_KEY` | `quota-sentinel.clinepass-api-key` |

```bash
security add-generic-password -U -a quota-sentinel \
  -s quota-sentinel.opencode-go-api-key -w '<OpenCode Go API key>'
security add-generic-password -U -a quota-sentinel \
  -s quota-sentinel.clinepass-api-key -w '<ClinePass API key>'
```

Without the OpenCode key the Native tier is skipped and CodexBar's `opencodego`
provider (which keeps its own copy of the key) takes over; without a key the
direct attempt itself fails in milliseconds with `credential missing`, before
spending a token, and `status` reports which key is missing. Both keys feed
their provider's Native tier and its direct model transport; CodexBar remains
the second rung when a key is unavailable.

Codex and Antigravity are delivered by the Pi agent and authenticated from
Pi's own entries in `auth.json`; they never read these Keychain items.

### Feishu setup

The Feishu channel uses an enterprise self-built app (企业自建应用) and delivers
to the bot's 1:1 chat with you (私聊):

1. On the [Feishu Open Platform](https://open.feishu.cn) create a self-built
   app and enable its bot capability (机器人能力).
2. Grant permissions (权限管理):
   - 以应用的身份发消息 (`im:message:send_as_bot`) — required to send messages.
   - 接收消息 (`im:message.receive_v1`) — required for `/usage` WebSocket listener.
   - 以应用身份通过手机号或邮箱获取用户 ID (`contact:user.id:readonly`) —
     required only by `discover-feishu-user`.
3. Publish an app version (版本管理与发布) so the permissions take effect, and
   set the availability range (可用范围) to yourself.
4. Copy the App ID and App Secret from 凭证与基础信息 and store them:

   ```bash
   security add-generic-password -U -a quota-sentinel \
     -s quota-sentinel.feishu-app-id -w '<app_id>'
   security add-generic-password -U -a quota-sentinel \
     -s quota-sentinel.feishu-app-secret -w '<app_secret>'
   ```

5. Run `uv run --frozen --no-sync quota-sentinel discover-feishu-user
   <personal-email-or-mobile>` to look up your user_id and store it in
   Keychain.

## Privacy and context controls

- No saved session (`--no-session`)
- No tools or MCP (`--no-tools`, `--no-extensions`)
- No skills, plugins, prompt templates, or themes
- No `AGENTS.md` or `CLAUDE.md` context
- Minimal custom system prompt instructing the models to ignore context
- Luna and DeepSeek thinking disabled (`off`); Gemini uses its lowest supported
  level (`low`)

Discovered extensions remain disabled. The only explicitly loaded code is the
tool-free Codex quota hook, the Antigravity provider required for Gemini, a
tool-free Antigravity `agent_end` quota hook, and a tool-free OpenCode Go
`agent_end` quota hook. These do not add model context or register
model-callable tools. Antigravity and OpenCode Go quota metadata requests
consume no model tokens.

Push credentials never appear in process arguments or URLs. The app secret
travels in the token-request JSON body and the tenant token in an
`Authorization` header, both over an in-process HTTPS connection
(`quota_sentinel/runtime/feishu.py`); the OpenCode Go API key is piped to its
quota helper over stdin. Neither is ever passed as an argument or written to a
log — the model runner masks credential-shaped text in any captured stderr.

## Commands

The console script `quota-sentinel` is the single production entrypoint.
From the repository root:

```bash
uv run --frozen --no-sync quota-sentinel check
uv run --frozen --no-sync quota-sentinel wait
uv run --frozen --no-sync quota-sentinel usage
uv run --frozen --no-sync quota-sentinel status
uv run --frozen --no-sync quota-sentinel discover-feishu-user <email-or-mobile>
uv run --frozen --no-sync quota-sentinel run [codex|antigravity|opencode|clinepass|all]
uv run --frozen --no-sync quota-sentinel card-preview [all|both|codex|antigravity|opencode|clinepass|usage|progress]
uv run --frozen --no-sync quota-sentinel send-test-card [all|both|codex|antigravity|opencode|clinepass|usage|progress]

# state-backend lifecycle (each acquires the scheduler's run.lock)
uv run --frozen --no-sync quota-sentinel bootstrap-authority --assume-legacy   # ONE-TIME, see below
uv run --frozen --no-sync quota-sentinel cutover
uv run --frozen --no-sync quota-sentinel rollback
```

LaunchAgents use the explicit-project form, because launchd's
`WorkingDirectory` is `/private/tmp`:
`uv run --project <repo> --frozen --no-sync quota-sentinel <verb>`.

- `check`: 15-minute watchdog probe. Independently evaluates each provider's 5h quota state without model invocations, and triggers only the provider(s) due for execution.
- `usage`: Instant quota check sent to Feishu for every provider without triggering model tasks.
- `run [codex|antigravity|opencode|clinepass|all]`: Runs the specified provider (or the whole roster) and updates its schedule.
- `wait`: Legacy standalone precision timer, retained for manual rollback. The
  normal installation uses the local task orchestrator instead.
- `bootstrap-authority --assume-legacy`: the **only** way the ownership
  manifest is ever created, and the flag is mandatory. It asserts, out loud,
  that this deployment predates the authority protocol. Nothing automatic —
  not the installer, not `check`/`wait`/`run`, not an update — ever creates
  it for you.
- `cutover`: makes the JSON state backend authoritative **for the whole
  provider roster**. Authority is one global fact, so there is no
  per-provider switch; naming a provider is a usage error.
- `rollback`: returns ownership to the legacy backend (pure undo only, and
  likewise whole-roster).
  See [Upgrading from the legacy state backend](#upgrading-from-the-legacy-state-backend).

The same CLI also exposes the read-only diagnostics, the per-backend
inspection verbs and the one-time migration verb:

```bash
uv run --frozen --no-sync quota-sentinel --help
uv run --frozen --no-sync python -m quota_sentinel --help

# reads (follow the authoritative backend automatically)
uv run --frozen --no-sync quota-sentinel next-due codex
uv run --frozen --no-sync quota-sentinel state-dump codex
uv run --frozen --no-sync quota-sentinel authority

# per-backend diagnostics, for inspecting one side explicitly
uv run --frozen --no-sync quota-sentinel dump codex          # legacy slot files
uv run --frozen --no-sync quota-sentinel json-dump codex     # v1 JSON document

# lifecycle — these acquire run.lock themselves, so they are safe to run directly
uv run --frozen --no-sync quota-sentinel bootstrap-authority --assume-legacy   # one-time, explicit
uv run --frozen --no-sync quota-sentinel cutover
uv run --frozen --no-sync quota-sentinel rollback
uv run --frozen --no-sync quota-sentinel migrate             # legacy deployments only
```

`next-due` and `state-dump` read through whichever backend the durable
authority manifest selects, so `status` reports the live state without
knowing which backend that is. `authority` prints the manifest itself.

> **`cutover` and `rollback` are whole-roster.** They take no provider
> argument: the manifest names the backend for the entire state directory,
> so a provider-scoped switch would hand the other providers to the retired
> backend. Naming a provider after either verb is an argparse usage error,
> refused before any state is touched.

> **The `scheduler-*` verbs are an INTERNAL BRIDGE API.** They are what the
> runtime calls *while it already holds `run.lock`*, and they do not acquire
> it themselves — a second acquisition from a different process would
> deadlock against its own caller. Their `--help` says so. Use the lifecycle
> verbs above instead; the repo-wide audit fails if any document presents a
> bridge verb as an operator command.

### Upgrading from the legacy state backend

Every deployment has an **authority manifest**
(`<state-dir>/backend-authority.json`), and the runtime requires it. The
installer **never creates it** — a missing manifest means the owner of the
state is unknown, not that it is legacy — so a pre-protocol deployment
asserts it once, explicitly:

```bash
uv run --frozen --no-sync quota-sentinel bootstrap-authority --assume-legacy  # ONE-TIME assertion
./install-launchagents.sh --load   # sync, retire old agents, restart listener
uv run --frozen --no-sync quota-sentinel cutover   # one-time ownership switch (run.lock held)
```

All three steps are separate on purpose:
- `bootstrap-authority --assume-legacy` records `legacy epoch 0`. The flag is
  mandatory because "no manifest" and "a pre-protocol deployment" are
  indistinguishable from the state directory alone. A virgin deployment
  qualifies (it has no state at all); nothing else may be assumed.
- `--load` retires the legacy watchdog/timer agents and restarts the
  listener. It **reads and validates** the manifest, and fails — before any
  `launchctl` call — when it is missing or unparseable, so an upgrade can
  never invent an owner.
- `cutover` is the separate, explicit ownership switch. The installer never
  performs it, so you can upgrade the code, watch the existing backend
  behave, and move ownership when you choose.

The restart matters: a process still running the PREVIOUS version does not
know the manifest exists and would keep writing the legacy slots after the
switch. New code is safe in either order, because every state access re-reads
the durable fact.

`cutover` reads the CURRENT slot files, writes and verifies one JSON document
**per provider in the roster**, and only then publishes the ownership fact —
a single atomic replace. Every crash point is recoverable: before the flip the
slot files are still authoritative, and the flip itself is all-or-nothing. If
any provider fails to prepare or verify, ownership does not move at all; the
refreshed documents are just shadows under a still-legacy deployment. The slot
files are left byte-for-byte untouched afterwards and act as the rollback
artifact; `rollback` returns ownership to them and refuses once JSON state has
advanced, because that would discard authoritative state. It compares every
provider before refusing, so one diverged provider is enough to stop it.

All three lifecycle commands take the scheduler's `run.lock` themselves
(same `shlock` protocol, same file), so they cannot interleave with an
in-flight `check` or model run.

#### If the authority manifest goes missing

**Do not hand-create it, and do not bootstrap it to "fix" it.** A missing
manifest is not "never cut over" — it means the single ownership fact was
lost, and a deployment that cut over and then lost it looks exactly like one
that never had it. The runtime, the installer and every update path say so and
refuse to touch state, because guessing `legacy` would silently roll the
scheduler back to deadlines and retry debt the authoritative backend has
already moved past.

Recover it the way you would any lost durable fact:

1. stop the agents (`launchctl bootout gui/$(id -u)/quota-sentinel.feishu-listener`);
2. decide which backend actually holds the current state by inspecting
   `<provider>-state.json` and the legacy slot files side by side;
3. restore `backend-authority.json` from a backup if you have one;
4. only if you have confirmed the deployment **never cut over**, record that
   assertion instead:
   `uv run --frozen --no-sync quota-sentinel bootstrap-authority --assume-legacy`;
5. restart the agent.

#### Deleting the legacy files

After a cutover the per-slot files are a frozen rollback artifact. If you are
satisfied with the JSON backend you may delete them yourself — the project
deliberately does not:

```text
<provider>-last-attempt-at   <provider>-last-task-at   <provider>-next-due-at
<provider>-retry-pending     <provider>-last-known-reset-at
<provider>-reset-anchor      <provider>-reset-candidate
<provider>-last-triggered-window
```

> **Deleting them permanently destroys your rollback artifact**: `rollback`
> will then refuse (it needs both sides to prove a pure undo), and the only
> way back to the legacy backend is gone.
>
> **Never delete `backend-authority.json`.** It is the ownership fact, not a
> rollback artifact, and losing it stops the scheduler exactly as described
> above.

## Local Task Orchestrator

The Feishu listener process also hosts a lightweight local task orchestrator.
It replaces the two independent scheduling LaunchAgents with one durable
control loop, and it reads scheduler state through the authoritative backend
router rather than the legacy slot files — so a cutover cannot leave it
waking on deadlines the scheduler has already moved past:

- runs one `check` at startup, matching the previous `RunAtLoad` behavior;
- keeps the same quarter-hour watchdog grid (`:00`, `:15`, `:30`, `:45`);
- wakes at the earliest provider `next_due_at`, including immediately after a
  Mac wakes with an overdue deadline;
- re-reads external state at most 60 seconds later, matching the legacy
  precision timer's compatibility polling;
- excludes providers with `retry_pending=1` from the precision deadline wake;
  their debt remains owned by the 15-minute watchdog retry phase;
- coalesces a deadline and watchdog that become ready together into one
  `check`; and
- applies the existing 60-second due retry backoff after a check.

The orchestrator decides only **when to invoke `check`**. Fresh/stale quota
authority, reset+4 calibration, provider-specific scheduler-write blocking,
5h01 fallback, pending debt, retries, and successful-task state commits are
implemented exclusively by `quota_sentinel.scheduler`; the CLI drives the
process side of a run and hands each outcome back to the same domain.

Task executions and post-run deadline snapshots are stored in
`~/Library/Application Support/Quota-Sentinel/task-orchestrator.sqlite3`
using SQLite WAL mode. An interrupted `running` row is marked `interrupted` on
restart. Feishu `/usage` is recorded in the same history and wakes the control
loop to notice any Fresh calibration, but it still runs only the `usage`
command and never triggers a model task. Each history operation uses a short
transaction whose SQLite connection is explicitly closed, preventing DB/WAL
file descriptors from accumulating in the long-lived listener process.

## Execution Bounds & Process Hygiene

Every external process is bounded so a hang can never hold the scheduler:

| Operation | Bound | After the bound |
| --- | --- | --- |
| Model task, codex transport (Codex) | `QUOTA_SENTINEL_CODEX_TIMEOUT` (default 120s) + kill grace `QUOTA_SENTINEL_CODEX_KILL_GRACE` (10s) | A functional failure falls back to Pi; a cost regression (input ≥ `QUOTA_SENTINEL_CODEX_INPUT_CEILING`, default 2500, or output ≥ `QUOTA_SENTINEL_CODEX_OUTPUT_CEILING`, default 50) is **accepted** — the reply was already delivered — logged as `cost-regression`, and not recorded as a verified profile |
| Model task, agy transport (antigravity) | `QUOTA_SENTINEL_AGY_TIMEOUT` (default 120s) + kill grace `QUOTA_SENTINEL_AGY_KILL_GRACE` (10s) | A functional failure falls back to Pi; a cost regression (input ≥ `QUOTA_SENTINEL_AGY_INPUT_CEILING`, default 1500, or output ≥ `QUOTA_SENTINEL_AGY_OUTPUT_CEILING`, default 200) is **accepted** and logged as `cost-regression`; a missing agent is refused **before** any token is spent |
| Model task, Pi transport (codex; antigravity fallback) | `QUOTA_SENTINEL_MODEL_TIMEOUT` (default 300s) + kill grace `QUOTA_SENTINEL_MODEL_KILL_GRACE` (10s) | Provider marked 失败 (🔴), card shows red, scheduler keeps the seeded fallback |
| Model task, direct transport (opencode / clinepass) | `QUOTA_SENTINEL_DIRECT_TIMEOUT` (default 120s) | Same; a missing Keychain key fails in milliseconds with `credential missing` before any token is spent |
| Transport A/B override | `QUOTA_SENTINEL_TRANSPORT="opencode=pi"` | Moves one provider back onto the Pi agent for comparison. A name that is simply unknown is ignored; a real provider paired with a transport that cannot serve it (`opencode=agy`, `codex=agy`) is refused **at the configuration entry** — `invalid argument: provider 'opencode' does not run on the 'agy' transport …`, exit 3 — instead of aborting the run mid-burst |
| Antigravity Native `/usage` | `QUOTA_SENTINEL_ANTIGRAVITY_NATIVE_TIMEOUT` (default 20s, including version check) + cleanup up to 1s | Tier ① failed → CodexBar Live; fixed reason code logged |
| OpenCode Go Native `/usage` API | `QUOTA_SENTINEL_OPENCODE_NATIVE_TIMEOUT` (default 15s, incl. connect timeout) | Tier ① failed → CodexBar Live; fixed reason code logged, never a response body |
| ClinePass Native `/plan/usage-limits` API | `QUOTA_SENTINEL_CLINEPASS_NATIVE_TIMEOUT` (default 15s, incl. connect timeout) | Same; an idle account (no `resetsAt`) logs `missing_reset_time` and falls through |
| CodexBar Live query | Codex: `QUOTA_SENTINEL_CODEXBAR_TIMEOUT` (20s); Antigravity: `QUOTA_SENTINEL_ANTIGRAVITY_CODEXBAR_TIMEOUT` (35s); OpenCode: `QUOTA_SENTINEL_OPENCODE_CODEXBAR_TIMEOUT` (20s); + kill grace 10s | Tier ② treated as failed → Cache → Pi Snapshot |
| Feishu WebSocket listener outer bound | 480s | Subprocess group terminated (default quota acquisition ≈ 207s + existing Feishu auth/send retries ≈ 183s + margin) |
| Local orchestrator `check` outer bound | `QUOTA_SENTINEL_CHECK_TIMEOUT`; the default is **derived**, not typed: the longest legal attempt over the four transports (Pi→Codex, Codex→Pi, agy's guard + 4 turns → Pi, direct) × the Application's attempt limits, + the two quota-probe phases + 10% — ≈6,039s today, and it moves in the same commit as any channel timeout, retry count or `QUOTA_SENTINEL_*_ATTEMPTS` override | The `check` process and every nested detached process group are terminated |

Model and CodexBar timeouts run through `run_with_timeout.py`: the child gets its own session,
SIGTERM goes to the whole process group, escalates to SIGKILL after the grace
period, and reaps the group so no orphans remain. Exit code 124 marks a
timeout; the child's own exit code is otherwise propagated unchanged.

## The codex transport (official client)

Codex's five-hour window can only be started by a real request, so the provider
needs one model turn every cycle. **Pi remains the shipped path** because it is
far cheaper; OpenAI's own CLI is implemented as the transport that takes over
when Pi cannot deliver, and as an opt-in primary for A/B runs. The transport
decides not only what a turn costs but also *who the client appears to be* —
which is exactly why the official client is kept available.

Measured on 2026-09-27 against codex-cli 0.157.1, `gpt-6-luna`,
`model_reasoning_effort="none"`:

| Attempt | tokens |
| --- | ---: |
| Pi agent (shipped path) | **49 input / 5 output** |
| `codex exec` with default configuration | ~9,658 |
| the profile in `runtime/codex_exec.py` | **1,682 input / 5 output** |

Both paths reply exactly `1`, so the success criterion did not change. The
codex saving is structural: every layer of Codex's agent scaffolding (skills,
multi-agent roles, permission and environment context, and the tool set) is
switched off with official configuration keys, and every attempt reports its
own `input/cached/output/reasoning` numbers into the run log.

The priority chain is one hop deep in both directions:

```text
shipped   Pi ──fails──▶ Codex (terminal)
opt-in    Codex ──fails──▶ Pi (terminal)
```

A cost regression is not a hand-over in either direction: the turn that already
delivered stays the answer.

The composition root builds each counterpart as a terminal instance, so an
attempt can be handed over exactly once and can never bounce back.

Two failure classes are handled differently, because they mean different
things:

* **functional** — non-zero exit, no `turn.completed` event, a completion whose
  usage reports no input tokens at all (`usage=unverified`), a reply other than
  `1`, or reasoning tokens above zero: nothing was verified as delivered, so the
  attempt is handed to the counterpart transport;
* **cost regression** — the turn replied `1`, has no functional problem, and
  reports more than the measured profile (`QUOTA_SENTINEL_CODEX_INPUT_CEILING`,
  default 2500 input, or `QUOTA_SENTINEL_CODEX_OUTPUT_CEILING`, default 50
  output): it is **accepted** as a success, because the message was already
  delivered and the window already anchored. Re-delivering the same attempt
  through Pi would spend more quota for a fact that is already true, and a Pi
  failure on top would turn a delivered message into a reported failure that the
  scheduler then retries — spending a third time. The turn is logged as
  `result=cost-regression` with a warning and is **not** recorded as a verified
  profile, so the ceiling stays an operator alarm that re-fires on every attempt
  until the profile is fixed.

The profile is one canonical list (`PROFILE_OVERRIDES`, `PROFILE_DISABLED_FEATURES`)
whose hash is stored next to the state; a CLI version change *or* a profile edit
re-runs the smoke test and says so in the log. The token ceilings cover the
change nobody can see: a server-side model-metadata change shows up as a number
over the ceiling, not as a silent 5x bill.

Select either path with one variable, no code edit:

```bash
QUOTA_SENTINEL_TRANSPORT="codex=pi"    quota-sentinel run codex   # Pi primary (shipped)
QUOTA_SENTINEL_TRANSPORT="codex=codex" quota-sentinel run codex   # official CLI primary
```

## The agy transport (official Antigravity CLI)

Antigravity's five-hour boundary is not set by the turn this transport sends: a
read-only `/usage` probe costs 0 tokens, and a model turn does not move the
boundary either. Across 35 Antigravity reset-anchor movements in `logs/`
(2026-09-23 11:23:05 → 2026-09-28 07:56:43), 19 are exactly `+5h00m00s`, and in
the clean runs the probe reported the next boundary *before* the turn that then
reproduced it unchanged — so the deadline never depends on a turn of ours. The
full measurement, its counter-examples and what still needs live verification
are in **Probe-only providers**. Antigravity therefore ships the opposite
priority to codex: **`agy` first, Pi as its one-hop fallback.**

`agy` is an agent, and a stock turn carries its scaffolding. Measured on
2026-09-27 against agy 1.2.12, model `gemini-3.8-flash-low`, `--effort low`:

| Attempt | tokens |
| --- | ---: |
| the profile in `runtime/agy_exec.py` | **564 input / 1 output / 0 thinking** |
| the same profile with `--mode plan` | 1,354 input |
| stock `agy -p "1"` (default agent, 57 tools) | 22,311 input / 28 output |

Of the stock 22,311, ~20.3k is the schema of the 57 built-in tools alone. The
profile is one markdown agent written per attempt into an empty cwd:
`excludeDefaultComponents: true` drops the default prompt sections *and* the
built-in tools, `inheritCustomizations: false` keeps this machine's rules,
skills, plugins, subagents and MCP servers out, and the body is one line asking
for exactly `1`. Two of those keys are load-bearing and one is not: measured
without `excludeDefaultComponents` the same turn costs 1,997 input tokens, while
`tools: []` changes nothing once the former is set — it is kept as an explicit
statement of intent, not as a saving.

Two guards run at **zero token cost**, because read-only slash commands are
answered by the CLI itself (measured `input_tokens` 0, `num_turns` 0):

* **agent presence** — `agy -p /agents` is checked before the turn. An
  unresolvable `--agent` is not an error at all: the *default* agent answers, at
  ~40x the cost, and the input ceiling would catch that only after paying for it;
* **transient handshake** — `Eligibility check failed` happens before a turn
  starts and costs nothing (six in a row were observed during one burst), so it
  is retried instead of being reported as a delivery failure. The retry is
  narrow on purpose: the marker must come from the **current turn's own** stderr
  (the file is read from the byte offset where that turn started, so an old
  marker is never read again) *and* that turn must have reported no tokens at
  all. A turn that reported tokens was a real turn, not a handshake, and is
  never replayed for free.

Failure classes are the codex transport's, with one measured deviation:
`--effort low` leaves thinking to the model's discretion (identical invocations
measured 0 and 34 thinking tokens), so thinking is *reported* and not *policed*.
What proves the profile is intact is the **input** side, which is structural.
A functional failure (non-zero exit, a non-`SUCCESS` status, a reply that is not
`1`, a missing agent, or a `SUCCESS` turn whose usage cannot show
`input_tokens ≥ 1`) hands the attempt to Pi — the input count is the only proof
that the 564-token minimal profile and not the 22,311-token stock agent
answered, and a missing reading cannot be turned into a zero: an unverifiable
ignition is a functional failure, not a free one. A cost regression (input ≥
`QUOTA_SENTINEL_AGY_INPUT_CEILING`, default 1500, or a runaway reply ≥
`QUOTA_SENTINEL_AGY_OUTPUT_CEILING`, default 200) with no functional problem is
**accepted** exactly as the codex one is: delivered, logged as
`cost-regression`, and not recorded as a verified profile.

```bash
QUOTA_SENTINEL_TRANSPORT="antigravity=agy" quota-sentinel run antigravity  # agy primary (shipped)
QUOTA_SENTINEL_TRANSPORT="antigravity=pi"  quota-sentinel run antigravity  # Pi primary
```

This transport writes no quota snapshot: the Pi capture file is normalized as a
*Pi* document, and rebuilding that shape out of the CLI's own output would be a
different client's claim in Pi's clothing. Antigravity's tier-① native probe
reads the same `/usage` payload for free in the same tick, so the reading is not
lost — only the redundant copy is.

## Probe-only providers

`QUOTA_SENTINEL_PROBE_ONLY=antigravity` (comma/space separated) switches one or
more providers' **model trigger** off without touching anything else. A
probe-only provider:

- still has its quota probed on every check, so the `/usage` card, the tier
  ladder and freshness reporting are unchanged;
- still has its deadline calibrated — `reanchor_probe_only` follows the freshly
  observed reset with the same `reset + 4m` arithmetic a run would have
  produced, so removing the switch lands on exactly the deadline the provider
  would have had;
- is never executed: `check` — timer tick and watchdog alike — skips it, it
  cannot enter a run roster, and its retry debt is cleared rather than repaid,
  because a provider that cannot run can never repay it;
- never enters a task card, because there was no delivery to report.

The `run` verb has two shapes and both are explicit. Naming a probe-only
provider on the command line — `quota-sentinel run antigravity` with
`QUOTA_SENTINEL_PROBE_ONLY=antigravity` — is **refused before any attempt**: the
run lock is not taken, no probe runs and no token is spent, the CLI reports
`invalid argument: probe-only provider(s) cannot be run: …` and exits 3. The
default whole-roster `run` (no explicit target) instead drops probe-only
providers from the roster, logs the omission, and sends a card for the
providers that actually ran — a switched-off provider is never carded for a
delivery nobody made. Unsetting `QUOTA_SENTINEL_PROBE_ONLY` restores the
previous behaviour exactly.

Nothing is deleted to make this work: the transport, its roster entry and its
tests stay in place, and the deadline is kept strictly in the future so neither
the precision timer nor the watchdog grid can spin on a matured deadline.

Why Antigravity is the first candidate: the boundary it reports does not track
our turns. Measured over the same 35 reset-anchor movements as above
(2026-09-23 11:23:05 → 2026-09-28 07:56:43), 19 are exactly `+5h00m00s`, and in
the clean runs the boundary was already in place before the turn that was
supposed to set it:

* 2026-09-23 11:21:49 — the probe reports the next boundary `16:17:39` while the
  current window is still on `11:17:39`; the 11:22:56 turn then produces the
  same `16:17:39`. Four more turns that day reproduce `+5h00m00s` from a
  boundary they did not set (observations 16:22:06, 21:30:25, 02:22:28,
  07:22:19), holding the phase at `:17:39` while the observation time drifts by
  minutes;
* 2026-09-27 14:08:12 — the probe reports `18:45:18`; the 14:08:24 turn produces
  `18:45:18` again, not `19:08:24`. Codex is the contrast in the same tick: its
  14:08:17 turn produced exactly `19:08:17`, five hours after the turn.

The counter-examples are real and are not messages. Three long jumps
(`+7h27m25s` on 2026-09-24 14:45:52, `+8h30m00s` on 2026-09-25 09:15:24,
`+7h30m14s` on 2026-09-26 09:11:11) install a **new** phase after a long gap in
observations — the 2026-09-26 09:11:11 tick moved codex (`+7h18m14s`), opencode
(`+8h00m35s`) and clinepass (`+8h00m34s`) in the same second, so it is a
machine-wake artefact, not a trigger. And on 2026-09-27 19:30:17 → 22:00:16 the
boundary moved 13 times in steps of `+0h00m49s` … `+0h44m57s`, each new value
landing 2–9 seconds before that tick's own clock plus five hours, on both the
`native` and `codexbar-live` tiers — with the provider probe-only, 37
`trigger disabled (probe-only): not executed` lines that day and no Antigravity
model turn between 14:08:24 and 23:14:47.

So the settled position is: **the boundary is provider-side, and no message this
scheduler sends starts or moves it.** `reanchor_probe_only` derives the deadline
from the reported reset, which does not depend on a turn of ours, so switching
the trigger off cannot strand the schedule: what the switch costs is the
delivery and its card, not the boundary and not the deadline. What the
repository cannot settle, and a live check has to: whether the provider's rule
is a strict grid or a relative "5 h from now" answer whenever no window is open
(both fit the log), what caused the three phase shifts after wake gaps, and
whether any read — or an external session — can open a window. Until then the
`+5h00m00s` steps are the measured shape of the boundary, not a proof of its
mechanism.

## Run Log (`logs/`)

Every command, operation, and result is logged with elapsed time to
`logs/YYYY-MM-DD.log` (the WebSocket listener additionally writes
`logs/listener.log`). The directory is created on demand with mode 0700, log
files are 0600, and logging is best-effort — it can never break a command.
The active listener LaunchAgent also uses umask 0077, so its stdout/stderr
capture files are private from creation.

Recorded events include: command start/finish with duration, quota tier
attempts per provider (`native` / `codexbar-live` / `codexbar-cache` /
`pi-snapshot`) with outcomes and elapsed seconds, timeout warnings, model task
results, scheduler state changes (`state: codex next_due_at X -> Y`),
notification deliveries, and `/usage` busy replies. Logs contain no tokens or
secrets. Set `QUOTA_SENTINEL_LOG_DIR` to redirect the run log (the test suites do).

The task orchestrator additionally records structured task status, trigger,
scheduled time, exit code, timeout flag, elapsed time, and provider deadline
snapshots in `task-orchestrator.sqlite3`. The database and WAL sidecars are
created with mode 0600; the state directory is mode 0700.

## Quota Acquisition Hierarchy & Freshness Model

The quota acquisition pipeline strictly follows a 4-tier hierarchy for both providers:

```text
① Native Direct (FRESH)
   Codex: codex app-server JSON-RPC (account/rateLimits/read)
   Antigravity: built-in agy -p /usage --output-format json (via uv)
   OpenCode Go: GET opencode.ai/zen/go/v1/usage with the API key (via python3)
   ClinePass: GET api.cline.bot/api/v1/users/me/plan/usage-limits (via python3)
   ↓ (fail)
② CodexBar Live (FRESH)
   codexbar usage --provider <codex|antigravity> --source cli
   codexbar usage --provider opencodego --source api
   codexbar usage --provider clinepass --source api
   (On success: updates local CodexBar cache snapshot)
   ↓ (fail)
③ CodexBar Cached (STALE / DISPLAY ONLY)
   codexbar-<provider>-last-success.json
   (Tagged as "CodexBar · cached（可能不是最新）", NEVER alters scheduler deadline)
   ↓ (fail)
④ Pi Snapshot (STALE / DISPLAY ONLY)
   pi-<provider>-quota.json
   (Tagged as "Pi 快照（可能不是最新）", NEVER alters scheduler deadline)
```

- **Scheduler Freshness Rule**: Only Tier ① (Native) and Tier ②
  (CodexBar Live) are marked as `FRESH` and eligible to calibrate a provider's
  5-hour `reset_at + 4m` deadline before its currently scheduled reset occurs.
  OpenCode Go's monthly window is neither fresh-authoritative nor schedulable:
  it is display data carried alongside the two windows. Freshness is necessary but not sufficient: each provider generation
  keeps a fixed reset anchor. Earlier resets and later movement up to five
  minutes from that anchor remain dynamic; a farther-later reset must remain
  stable across an independent observation before it may replace the anchor.
  Once the scheduled reset occurs, the provider enters a scheduler-write
  block until its task succeeds.
  Tier ③ and Tier ④ are strictly `STALE` and used for UI display only.
- **Zero LLM Token Guarantee**: All quota probe tiers (Native JSON-RPC / built-in agy `/usage` / CodexBar) are metadata inspections and do not consume inference tokens or model turns. Antigravity Native requires agy >= 1.1.11 and accepts only a successful `command.name=usage` report with zero model turns/tokens and both known, enabled Gemini windows. It uses a private empty cwd, caps output at 1 MiB, bounds execution and cleans up only its own process group. Older binaries or malformed reports fall through to CodexBar; cached/Pi data remains STALE and cannot calibrate deadlines. The agy quota account is the agy CLI's account (unchanged from the previous local agy probe); it is not automatically shared with Pi OAuth.

## Armed Deadlines & Fresh Calibration (P1-1)

Fresh quota may recalibrate a reset-based deadline only before its existing
`reset_at`. From `reset_at` through the four-minute buffer, due execution, and
any retries, that provider is scheduler-write blocked. Watchdog and `/usage`
may still fetch/display quota, but cannot replace `last_known_reset_at` or
`next_due_at`; the task must succeed first. A successful task writes its 5h01
fallback, releasing the block, and the post-run Fresh probe then calibrates the
next cycle. Without this rule, a probe during the reset buffer can replace the
armed deadline with the following window and starve the current task.

The same write seam also carries the reset trust gate, which removes the
rolling-reset starvation mode: fresh data may move a deadline earlier, or
within five minutes after the generation's fixed anchor, immediately — but the
anchor itself does not follow those small movements. A reset farther beyond
the anchor is only a candidate until a second fresh probe at least 60 seconds
later reports the same timestamp (±30s tolerance). Only then does it take over
and become the new anchor. A rolling value that advances with every probe,
whether in large jumps or many individually small steps, cannot chase the
deadline forever. The first fresh reset of a generation anchors immediately;
a successful task clears the old candidate/anchor state for the next cycle.
Cache and Pi data still never write deadlines.

## Failure Retry & Pending Debt

A provider's `last_task_at` records the last **successful** model task only.
Failures and timeouts never advance it and never re-seed the 5h01 fallback.

- A task that reaches its deadline is marked `retry_pending` (per provider,
  persisted) **before** its first attempt, then repaid by a retry burst:
  - **Initial burst** (from a due decision or a manual `run`): at most 3
    total attempts, 30s between attempts, providers run in parallel rounds.
  - **Watchdog bursts** (every 15-min check, before any quota acquisition):
    at most 2 total attempts, 30s apart. Bursts are spaced at least 13 minutes
    from the last attempt, so at most one burst per provider per watchdog.
- Any attempt that exits with the model replying exactly `1` commits success:
  `last_attempt_at`/`last_task_at` = success time, `retry_pending` cleared,
  `next_due_at` = success + 5h01m as the initial fallback, then normal Fresh
  calibration (`reset_at + 4min`) takes over.
- If every attempt in a burst fails, the debt stays pending: the deadline and
  success fields remain untouched, and the next watchdog repays again.
  Fresh data observed while a debt is pending (e.g. via `/usage`) never
  cancels the pending priority.
- One burst sends at most one card (success or failure). Watchdog bursts send
  a card only on recovery; continued failures stay in `logs/`.

## Dynamic Reset Calibration & Fallback Scheduling

- **Decoupled Provider States**: Codex, Antigravity and OpenCode Go each independently maintain `last_known_reset_at`, a fixed per-generation reset anchor, `next_due_at`, `last_task_at` (successful tasks only), `last_attempt_at`, and `retry_pending`.
- **Dynamic Calibration from Quota Probes**:
  - Every 15 minutes, the watchdog probes quota via the 4-tier hierarchy.
  - Immediately after a real task starts, its provider is seeded with a
    no-quota fallback of `last_task_at + 5h01m`.
  - Before the scheduled reset, every subsequent valid `FRESH` observation
    that is earlier than, or no more than five minutes later than, the fixed
    generation anchor replaces the deadline with `reset_at + 4m`. The anchor
    does not move with ordinary jitter, so small movements cannot accumulate.
    This validation does not replace or alter either the `+1m` fallback grace
    or the `+4m` observed-reset grace.
  - A farther-later Fresh reset remains available for display but cannot write
    scheduler state until a second independent Fresh probe confirms that its
    absolute reset timestamp is stable; promotion then replaces the anchor.
  - From the scheduled reset until successful execution, that Provider's
    scheduler calibration is paused. The 15-minute Watchdog and `/usage` cannot
    overwrite its armed deadline; success releases the block and the post-run
    probe supplies the next Fresh schedule.
  - The 5h01 value is a degradation fallback, not a hard minimum between two
    real tasks.
  - `/usage` reuses the same fresh-only calibration after its live query but
    does not execute a due task; the precision timer or watchdog performs it.
  - When a probe fails or returns a stale snapshot, `next_due_at` is **preserved
    unchanged**. Cache and Pi data never overwrite the last authoritative
    Fresh deadline.
- **Graceful Fallback & Degradation**:
  - When `now >= next_due_at`, the due provider executes.
  - If all post-run probes fail, the fallback remains anchored to the actual
    attempt time (`last_task_at + 5h01m`), including after sleep/wake delays.
  - Any subsequent successful Fresh probe immediately replaces the fallback
    with its `reset_at + 4m` deadline.
- **Targeted Execution & Card Scoping**:
  - When only Antigravity reaches its deadline, only Gemini executes and only Gemini appears in the Feishu card (other providers are never marked as failed).
  - When only Codex reaches its deadline, only Luna executes and only Luna appears in the Feishu card.
  - When only OpenCode Go reaches its deadline, only DeepSeek executes and only DeepSeek appears in the Feishu card.
  - Multiple providers that reach their deadlines execute in parallel in one round. Codex + Antigravity keep the original two-column card; any card that includes OpenCode stacks one full-width block per provider.
- **State Persistence & Migration**: Stored independently per provider under `~/Library/Application Support/Quota-Sentinel/` with automatic legacy migration.

## LaunchAgents

- `quota-sentinel.feishu-listener.plist`: Active Feishu WebSocket
  listener and local task-orchestrator host.
- `quota-sentinel.plist`: Disabled legacy 15-minute watchdog,
  retained as a rollback artifact.
- `quota-sentinel.timer.plist`: Disabled legacy precision timer,
  retained as a rollback artifact.

Only the listener/orchestrator LaunchAgent may be loaded during normal
operation. Loading either legacy scheduler at the same time would create a
second scheduling entry point, even though the shared run lock still prevents
duplicate model execution.

### Installing

launchd does not expand `~` or `$HOME` inside `ProgramArguments`, so these
plists require literal absolute paths. To keep local paths out of the
repository, only `*.plist.template` files are committed, carrying
`__REPO_DIR__` and `__LOG_DIR__` placeholders; the rendered `*.plist` files
are gitignored.

Render and install them for wherever this checkout lives:

```bash
brew install uv                # once; the listener runtime needs uv
./install-launchagents.sh          # uv sync --locked + render + copy into ~/Library/LaunchAgents
./install-launchagents.sh --load   # ... and bootstrap the active listener
```

The installer first verifies uv and syncs the project environment strictly
from `uv.lock` (`uv sync --locked`) — this is the only network-using setup
step. The listener agent itself then starts with
`uv run --project <repo> --frozen --no-sync python feishu_listener.py`:
it cannot re-resolve the lock, mutate the environment, or hit the network
at runtime, and the explicit `--project` keeps startup correct even though
launchd's `WorkingDirectory` is `/private/tmp`.

The script substitutes the real paths, validates each file with `plutil
-lint`, and refuses to install anything containing an unrendered
placeholder. It is idempotent — re-run it after moving the checkout or
after dependency changes.

## Tests

The suites under `tests/` are standalone scripts with no test runner to
install. They live in the project's uv environment
(`pyproject.toml` + `uv.lock`; the only third-party dependency is
`lark-oapi`, needed by the Feishu listener suite). After the installer (or
a manual `uv sync --locked`), the canonical run from the repository root is:

```bash
for t in tests/*.py; do PYTHONPATH=. uv run --frozen --no-sync python "$t" || echo "FAIL $t"; done
```

The stdlib-only suites also pass under plain system `python3`
(`PYTHONPATH=. python3 tests/...`); `tests/feishu-listener-regression.py`
must run in the project environment so it exercises the real `lark_oapi`
imports instead of any globally installed copy. `tests/uv-project-
regression.py` is the environment guard itself: lock check, console-script
vs `python -m` entry-point parity, LaunchAgent-style
`--project --frozen --no-sync` startup from a foreign working directory, an
offline runtime proof, and the rule that the class-C runtime graph stays
stdlib-only on system Python.

Suites worth knowing by name:

| Suite | What it pins |
| --- | --- |
| `tests/python-entrypoint-regression.py` | the end-to-end CLI verbs, and that the shell implementation, daemons, templates and installer are shell-free |
| `tests/python-authority-regression.py` | the durable authority protocol, the router's read guard, the cutover crash matrix, and the lock-safe public lifecycle |
| `tests/python-scheduler-regression.py` | every scheduler transition, the observation reader, deadline calibration, and the service-level burst sequence |
| `tests/python-app-regression.py` | the `check`/`run` orchestration: retry bursts and limits, debt, recovery, notifications |
| `tests/python-quota-adapter-regression.py` | jq parity for the normalisers, the golden fixtures, and both live native-tier helpers |
| `tests/python-state-store-regression.py`, `tests/python-json-store-regression.py`, `tests/python-state-cutover-regression.py` | the two backends' value contracts, atomicity, migration and backend agreement |
| `tests/python-model-runner-regression.py` | model process bounds, redaction and process-group kill |
| `tests/python-quota-probe-regression.py` | the four-tier fallback ladder and its cache/loopback-free cache handling |
| `tests/python-feishu-regression.py` | card JSON, envelopes, lookup and delivery |
| `tests/python-architecture-audit-regression.py` | the repo-wide mechanical invariants (one authority mapping, one writer family, class-C imports, installer/authority safety) |

A new test may be added freely; an existing assertion is only changed when
the contract itself changed, and the reason is recorded in the commit.

## License

MIT — see [LICENSE](LICENSE).

