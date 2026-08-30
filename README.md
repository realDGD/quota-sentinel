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
   - **Primary**: CodexBar CLI (`codexbar usage --provider codex --source cli` / `oauth`). Queries official Codex App Server RPC or OAuth endpoints directly. Zero prompt tokens, zero inference turns.
   - **Fallback**: Pi provider response hook snapshot (`capture-codex-quota.ts`).
2. **Antigravity Quota**:
   - **Primary**: CodexBar Antigravity CLI (`codexbar usage --provider antigravity --source cli`). Queries Antigravity local `agy` HTTPS service (`RetrieveUserQuotaSummary`). Zero prompt tokens.
   - **Fallback**: Pi Antigravity API snapshot (`capture-antigravity-quota.ts`).

## Feishu `/usage` Command & Long Connection Listener

You can send `/usage` in Feishu bot private chat at any time. The bot connects
via Feishu WebSocket long connection (长连接, no public IP or webhook required):
- Fetches real-time structured quota via CodexBar / agy.
- Responds with the formatted quota card immediately in Feishu.
- **Strictly read-only**: Does NOT invoke LLMs, does NOT run Pi agent tasks, and does NOT alter scheduler state (`last_task_at`, `last_triggered_window`, `next_due_at`).
- Filtered against bot loops (ignores non-user messages) and deduplicated.

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
./quota-sentinel.sh run
```

- `check`: 15-minute watchdog probe. Queries live quota without model invocations, checks 5h window state, and triggers task if a new window is detected.
- `usage`: Instant quota check sent to Feishu without triggering model tasks.
- `run`: Forces one run and moves the next due time forward.
- `wait`: Precise sleep timer.

## Dynamic Scheduling & Reset Detection

- **Window Identification**: Tracks the active 5h window reset timestamps (`last_triggered_window`).
- **Reset Detection**: Triggers when a newly started 5h window is detected (`remaining >= 4h55m`) or via missed-reset recovery after sleep/startup.
- **Deduplication**: Once a window is triggered, it will never re-trigger the same 5h window.
- **Safety Grace & Minimum Interval**: Enforces a minimum interval of 5 hours 01 minute between runs and waits 4 minutes after window reset to allow server stats to settle.
- **State Persistence**: Stored under `~/Library/Application Support/quota-sentinel/` across reboots and sleep/wake cycles.

## LaunchAgents

- `com.example.quota-sentinel.plist`: Periodic 15-minute watchdog (`check`).
- `com.example.quota-sentinel.timer.plist`: Continuous precision timer (`wait`).
- `com.example.quota-sentinel.feishu-listener.plist`: Feishu WebSocket listener for `/usage`.
