# Quota Sentinel + Gemini (Feishu enterprise app)

Runs GPT-5.6 Luna and Gemini 3.7 Flash (Low) in parallel, sends one combined
status message through a Feishu enterprise self-built app, and saves no Pi
sessions.

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

■ 剩余　□ 已用
🕒 2026-08-29 20:30:00 CST
```

Each provider is successful only when Pi exits normally and its model replies
exactly `1`. Quota collection is reported independently.

## Quota Data Sources & Architecture

1. **Codex Quota**:
   - **Primary**: Native Codex App Server RPC (`account/rateLimits/read`).
   - **Fallbacks**: CodexBar Live (`cli`, then `oauth`), CodexBar cached
     result, then the Pi provider response-hook snapshot.
2. **Antigravity Quota**:
   - **Primary**: Native local `agy` HTTPS service
     (`RetrieveUserQuotaSummary`).
   - **Fallbacks**: CodexBar Live, CodexBar cached result, then the Pi
     Antigravity API snapshot.

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
| App ID | `FEISHU_APP_ID` | `com.example.quota-sentinel.feishu-app-id` |
| App Secret | `FEISHU_APP_SECRET` | `com.example.quota-sentinel.feishu-app-secret` |
| Recipient user ID | `FEISHU_USER_ID` | `com.example.quota-sentinel.feishu-user-id` |

`FEISHU_DRY_RUN=1` prints the message instead of calling the API.

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
     -s com.example.quota-sentinel.feishu-app-id -w '<app_id>'
   security add-generic-password -U -a quota-sentinel \
     -s com.example.quota-sentinel.feishu-app-secret -w '<app_secret>'
   ```

5. Run `./quota-sentinel.sh discover-feishu-user <personal-email-or-mobile>`
   to look up your user_id and store it in Keychain.

## Privacy and context controls

- No saved session (`--no-session`)
- No tools or MCP (`--no-tools`, `--no-extensions`)
- No skills, plugins, prompt templates, or themes
- No `AGENTS.md` or `CLAUDE.md` context
- Minimal custom system prompt instructing the models to ignore context
- Luna thinking disabled; Gemini uses its lowest supported level (`low`)

Discovered extensions remain disabled. The only explicitly loaded code is the
tool-free Codex quota hook, the Antigravity provider required for Gemini, and a
tool-free Antigravity `agent_end` quota hook. These do not add model context or
register model-callable tools. Antigravity quota metadata requests consume no
model tokens.

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
./quota-sentinel.sh run [codex|antigravity|all]
```

- `check`: 15-minute watchdog probe. Independently evaluates Codex and Antigravity 5h quota states without model invocations, and triggers only the provider(s) due for execution.
- `usage`: Instant quota check sent to Feishu for both providers without triggering model tasks.
- `run [codex|antigravity|all]`: Runs specified provider (or both) and updates its schedule.
- `wait`: Precise sleep timer waking at the earliest due deadline (`min(codex, antigravity)`).

## Execution Bounds & Process Hygiene

Every external process is bounded so a hang can never hold the scheduler:

| Operation | Bound | After the bound |
| --- | --- | --- |
| Model task (`run_codex` / `run_antigravity`) | `QUOTA_SENTINEL_MODEL_TIMEOUT` (default 300s) + kill grace `QUOTA_SENTINEL_MODEL_KILL_GRACE` (10s) | Provider marked 失败 (🔴), card shows red, scheduler keeps the seeded fallback |
| CodexBar Live query | `QUOTA_SENTINEL_CODEXBAR_TIMEOUT` (default 20s) + kill grace 10s | Tier ② treated as failed → Cache → Pi Snapshot |
| Feishu WebSocket listener outer bound | 120s | Subprocess group terminated (`/usage` worst case ≈ 100s) |

Timeouts run through `run_with_timeout.py`: the child gets its own session,
SIGTERM goes to the whole process group, escalates to SIGKILL after the grace
period, and reaps the group so no orphans remain. Exit code 124 marks a
timeout; the child's own exit code is otherwise propagated unchanged.

## Run Log (`logs/`)

Every command, operation, and result is logged with elapsed time to
`logs/YYYY-MM-DD.log` (the WebSocket listener additionally writes
`logs/listener.log`). The directory is created on demand with mode 0700, log
files are 0600, and logging is best-effort — it can never break a command.

Recorded events include: command start/finish with duration, quota tier
attempts per provider (`native` / `codexbar-live` / `codexbar-cache` /
`pi-snapshot`) with outcomes and elapsed seconds, timeout warnings, model task
results, scheduler state changes (`state: codex next_due_at X -> Y`),
notification deliveries, and `/usage` busy replies. Logs contain no tokens or
secrets. Set `QUOTA_SENTINEL_LOG_DIR` to redirect the run log (the test suites do).

## Quota Acquisition Hierarchy & Freshness Model

The quota acquisition pipeline strictly follows a 4-tier hierarchy for both providers:

```text
① Native Direct (FRESH)
   Codex: codex app-server JSON-RPC (account/rateLimits/read)
   Antigravity: agy localhost HTTPS (RetrieveUserQuotaSummary)
   ↓ (fail)
② CodexBar Live (FRESH)
   codexbar usage --provider <codex|antigravity> --source cli
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
  deadline to the latest `reset_at + 4m`.
  Tier ③ and Tier ④ are strictly `STALE` and used for UI display only.
- **Zero LLM Token Guarantee**: All quota probe tiers (Native JSON-RPC / localhost RPC / CodexBar) are zero-cost metadata inspections and do not consume any inference tokens or model turns.

## Dynamic Reset Calibration & Fallback Scheduling

- **Decoupled Provider States**: Codex and Antigravity each independently maintain `last_known_reset_at`, `next_due_at`, and `last_task_at`.
- **Dynamic Calibration from Quota Probes**:
  - Every 15 minutes, the watchdog probes quota via the 4-tier hierarchy.
  - Immediately after a real task starts, its provider is seeded with a
    no-quota fallback of `last_task_at + 5h01m`.
  - Every subsequent valid `FRESH` observation replaces that deadline with the
    latest `reset_at + 4m`, whether this is earlier or later than the fallback.
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
  - When only Antigravity reaches its deadline, only Gemini executes and only Gemini appears in the Feishu card (Codex is never marked as failed).
  - When only Codex reaches its deadline, only Luna executes and only Luna appears in the Feishu card.
  - When both reach their deadlines, both execute in parallel and are rendered in a combined card.
- **State Persistence & Migration**: Stored independently per provider under `~/Library/Application Support/quota-sentinel/` with automatic legacy migration.

## LaunchAgents

- `com.example.quota-sentinel.plist`: Periodic 15-minute watchdog (`check`).
- `com.example.quota-sentinel.timer.plist`: Continuous precision timer (`wait`).
- `com.example.quota-sentinel.feishu-listener.plist`: Feishu WebSocket listener for `/usage`.
