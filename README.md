# Quota Sentinel + Gemini + DeepSeek (Feishu enterprise app)

Runs GPT-5.6 Luna, Gemini 3.7 Flash (Low) and DeepSeek V4 Flash (OpenCode Go)
in parallel, sends one combined status message through a Feishu enterprise
self-built app, and saves no Pi sessions.

Feishu receives an interactive card with a green header when both model calls
succeed and a red header when either one fails. Its body is equivalent to:

```text
GPT-5.6 Luna
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
     CodexBar cached result, then the Pi session snapshot. The 5-hour rolling
     window drives the schedule exactly like the other providers; the monthly
     cap is carried for display only and never participates in scheduling.

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

The OpenCode Go quota credential is separate from the push credentials and is
read only by the Native quota tier:

| Credential | Environment override | Keychain service |
| --- | --- | --- |
| OpenCode Go API key | `OPENCODE_API_KEY` | `quota-sentinel.opencode-go-api-key` |

```bash
security add-generic-password -U -a quota-sentinel \
  -s quota-sentinel.opencode-go-api-key -w '<OpenCode Go API key>'
```

Without it the provider still runs and still reports quota: the Native tier is
skipped and CodexBar's `opencodego` provider (which keeps its own copy of the
key) takes over. A **model** run needs no key here at all — it authenticates
from Pi's own `opencode-go` entry in `auth.json`.

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

5. Run `./quota-sentinel.sh discover-feishu-user <personal-email-or-mobile>`
   to look up your user_id and store it in Keychain.

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

Push credentials never appear in process arguments or URLs: the Feishu app
secret travels through the request body on stdin, and the tenant token through
an Authorization header supplied to curl over stdin.

## Commands

```bash
./quota-sentinel.sh check
./quota-sentinel.sh wait
./quota-sentinel.sh usage
./quota-sentinel.sh status
./quota-sentinel.sh discover-feishu-user <email-or-mobile>
./quota-sentinel.sh run [codex|antigravity|opencode|all]
./quota-sentinel.sh card-preview [all|both|codex|antigravity|opencode|usage]
./quota-sentinel.sh send-test-card [all|both|codex|antigravity|opencode|usage|progress]
```

- `check`: 15-minute watchdog probe. Independently evaluates each provider's 5h quota state without model invocations, and triggers only the provider(s) due for execution.
- `usage`: Instant quota check sent to Feishu for every provider without triggering model tasks.
- `run [codex|antigravity|opencode|all]`: Runs specified provider (or all three) and updates its schedule.
- `wait`: Legacy standalone precision timer, retained for manual rollback. The
  normal installation uses the local task orchestrator instead.

The Python state store has its own read/bootstrap verbs, equivalent under
either entry point (parity is test-pinned):

```bash
uv run quota-sentinel --help
uv run python -m quota_sentinel --help
uv run quota-sentinel next-due codex
uv run quota-sentinel dump codex
uv run quota-sentinel json-dump codex
uv run quota-sentinel migrate
```

`migrate` is the explicit Phase 2/3A bootstrap that seeds shadow JSON
documents from the authoritative legacy slot files; the scheduler itself
still runs entirely in the shell.

## Local Task Orchestrator

The Feishu listener process also hosts a lightweight local task orchestrator.
It replaces the two independent scheduling LaunchAgents with one durable
control loop while leaving all deadline policy inside `quota-sentinel.sh`:

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
5h01 fallback, pending debt, retries, and successful-task state commits remain
implemented exclusively by the shell scheduler and are unchanged.

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
| Model task (`run_codex` / `run_antigravity` / `run_opencode`) | `QUOTA_SENTINEL_MODEL_TIMEOUT` (default 300s) + kill grace `QUOTA_SENTINEL_MODEL_KILL_GRACE` (10s) | Provider marked 失败 (🔴), card shows red, scheduler keeps the seeded fallback |
| Antigravity Native `/usage` | `QUOTA_SENTINEL_ANTIGRAVITY_NATIVE_TIMEOUT` (default 20s, including version check) + cleanup up to 1s | Tier ① failed → CodexBar Live; fixed reason code logged |
| OpenCode Go Native `/usage` API | `QUOTA_SENTINEL_OPENCODE_NATIVE_TIMEOUT` (default 15s, incl. connect timeout) | Tier ① failed → CodexBar Live; fixed reason code logged, never a response body |
| CodexBar Live query | Codex: `QUOTA_SENTINEL_CODEXBAR_TIMEOUT` (20s); Antigravity: `QUOTA_SENTINEL_ANTIGRAVITY_CODEXBAR_TIMEOUT` (35s); OpenCode: `QUOTA_SENTINEL_OPENCODE_CODEXBAR_TIMEOUT` (20s); + kill grace 10s | Tier ② treated as failed → Cache → Pi Snapshot |
| Feishu WebSocket listener outer bound | 480s | Subprocess group terminated (default quota acquisition ≈ 207s + existing Feishu auth/send retries ≈ 183s + margin) |
| Local orchestrator `check` outer bound | `QUOTA_SENTINEL_CHECK_TIMEOUT` (default 2100s) | The shell and every nested detached process group are terminated |

Model and CodexBar timeouts run through `run_with_timeout.py`: the child gets its own session,
SIGTERM goes to the whole process group, escalates to SIGKILL after the grace
period, and reaps the group so no orphans remain. Exit code 124 marks a
timeout; the child's own exit code is otherwise propagated unchanged.

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
   ↓ (fail)
② CodexBar Live (FRESH)
   codexbar usage --provider <codex|antigravity> --source cli
   codexbar usage --provider opencodego --source api
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
second scheduling entry point, even though the shell run lock still prevents
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
install. The Python side lives in the project's uv environment
(`pyproject.toml` + `uv.lock`; the only third-party dependency is
`lark-oapi`, needed by the Feishu listener suite). After the installer (or
a manual `uv sync --locked`), the canonical runs from the repository root
are:

```bash
for t in tests/*.zsh; do zsh "$t" || echo "FAIL $t"; done
for t in tests/*.py; do PYTHONPATH=. uv run --frozen --no-sync python "$t" || echo "FAIL $t"; done
```

The stdlib-only Python suites also pass under plain system `python3`
(`PYTHONPATH=. python3 tests/...`); `tests/feishu-listener-regression.py`
must run in the project environment so it exercises the real `lark_oapi`
imports instead of any globally installed copy. `tests/uv-project-
regression.py` is the environment guard itself: lock check, CLI/module
entry-point parity, LaunchAgent-style `--project --frozen --no-sync`
startup from a foreign working directory, and an offline runtime proof.

## License

MIT — see [LICENSE](LICENSE).

