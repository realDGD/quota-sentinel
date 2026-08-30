#!/bin/zsh

set -euo pipefail

readonly PI_BIN="/opt/homebrew/bin/pi"
readonly CODEXBAR_BIN="/opt/homebrew/bin/codexbar"
readonly CURL_BIN="/usr/bin/curl"
readonly JQ_BIN="/opt/homebrew/bin/jq"
readonly SECURITY_BIN="/usr/bin/security"
readonly SHLOCK_BIN="/usr/bin/shlock"
readonly SLEEP_BIN="/bin/sleep"
readonly PI_AUTH_FILE="/Users/__USER__/.pi/agent/auth.json"
readonly SCRIPT_DIR="${0:A:h}"
readonly SCRIPT_NAME="${0:t}"
readonly CODEX_QUOTA_EXTENSION="$SCRIPT_DIR/capture-codex-quota.ts"
readonly ANTIGRAVITY_QUOTA_EXTENSION="$SCRIPT_DIR/capture-antigravity-quota.ts"
readonly ANTIGRAVITY_PROVIDER_EXTENSION="/Users/__USER__/.pi/agent/npm/node_modules/pi-antigravity/src/index.ts"
readonly KEYCHAIN_ACCOUNT="quota-sentinel"
readonly FEISHU_API_BASE="https://open.feishu.cn/open-apis"
readonly FEISHU_APP_ID_SERVICE="com.example.quota-sentinel.feishu-app-id"
readonly FEISHU_APP_SECRET_SERVICE="com.example.quota-sentinel.feishu-app-secret"
readonly FEISHU_USER_ID_SERVICE="com.example.quota-sentinel.feishu-user-id"
readonly STATE_DIR="${QUOTA_SENTINEL_STATE_DIR:-/Users/__USER__/Library/Application Support/quota-sentinel}"
readonly NEXT_DUE_FILE="$STATE_DIR/next-due-at"
readonly LAST_TASK_FILE="$STATE_DIR/last-task-at"
readonly LAST_TRIGGERED_WINDOW_FILE="$STATE_DIR/last-triggered-window"
readonly RUN_LOCK_FILE="$STATE_DIR/run.lock"
readonly QUOTA_LOCK_FILE="$STATE_DIR/quota.lock"
readonly PI_CODEX_SNAPSHOT_FILE="$STATE_DIR/pi-codex-quota.json"
readonly PI_ANTIGRAVITY_SNAPSHOT_FILE="$STATE_DIR/pi-antigravity-quota.json"
readonly RUN_INTERVAL_SECONDS=18060      # 5 hours 01 minute
readonly FRESH_WINDOW_SECONDS=17700      # 4 hours 55 minutes
readonly RESET_BUFFER_SECONDS=240        # 4 minutes after reset
readonly MAX_WINDOW_FUTURE_SECONDS=21600 # 6 hours

typeset -g LAST_TEMP_DIR=""
typeset -g CODEX_AGENT_DIR=""
typeset -g CODEX_STDOUT_FILE=""
typeset -g CODEX_STDERR_FILE=""
typeset -g CODEX_QUOTA_FILE=""
typeset -g ANTIGRAVITY_AGENT_DIR=""
typeset -g ANTIGRAVITY_STDOUT_FILE=""
typeset -g ANTIGRAVITY_STDERR_FILE=""
typeset -g ANTIGRAVITY_QUOTA_FILE=""
typeset -g CODEXBAR_CODEX_RAW_FILE=""
typeset -g CODEXBAR_ANTIGRAVITY_RAW_FILE=""
typeset -g CODEX_QUOTA_NORMALIZED_FILE=""
typeset -g ANTIGRAVITY_QUOTA_NORMALIZED_FILE=""
typeset -g CODEX_QUOTA_IS_FRESH=0
typeset -g ANTIGRAVITY_QUOTA_IS_FRESH=0
typeset -g RUN_STARTED_AT=0
typeset -gi RUN_LOCK_HELD=0
typeset -gi QUOTA_LOCK_HELD=0

usage() {
  print -r -- "Usage: $SCRIPT_NAME [check|wait|run|usage|discover-feishu-user|status]"
}

die() {
  print -u2 -r -- "Error: $*"
  exit 1
}

cleanup() {
  release_run_lock
  release_quota_lock
  if [[ -n "$LAST_TEMP_DIR" ]] &&
    [[ "$LAST_TEMP_DIR" == /private/tmp/quota-sentinel.* ]] &&
    [[ -d "$LAST_TEMP_DIR" ]]; then
    rm -rf -- "$LAST_TEMP_DIR"
  fi
}

ensure_temp_dir() {
  if [[ -z "$LAST_TEMP_DIR" ]]; then
    LAST_TEMP_DIR="$(mktemp -d /private/tmp/quota-sentinel.XXXXXX)"
  fi
  CODEXBAR_CODEX_RAW_FILE="$LAST_TEMP_DIR/codexbar-codex.json"
  CODEXBAR_ANTIGRAVITY_RAW_FILE="$LAST_TEMP_DIR/codexbar-antigravity.json"
  CODEX_QUOTA_NORMALIZED_FILE="$LAST_TEMP_DIR/codex-effective-quota.json"
  ANTIGRAVITY_QUOTA_NORMALIZED_FILE="$LAST_TEMP_DIR/antigravity-effective-quota.json"
}

read_next_due() {
  local value
  [[ -r "$NEXT_DUE_FILE" ]] || return 1
  value="$(<"$NEXT_DUE_FILE")"
  [[ "$value" =~ ^[0-9]+$ ]] || return 1
  print -r -- "$value"
}

write_next_due() {
  local epoch="$1"
  local temp_file="$NEXT_DUE_FILE.tmp.$$"
  [[ "$epoch" =~ ^[0-9]+$ ]] || die "Invalid next-run timestamp: $epoch"
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  print -r -- "$epoch" >"$temp_file"
  chmod 600 "$temp_file"
  mv -f "$temp_file" "$NEXT_DUE_FILE"
}

read_last_task_at() {
  local value
  [[ -r "$LAST_TASK_FILE" ]] || return 1
  value="$(<"$LAST_TASK_FILE")"
  [[ "$value" =~ ^[0-9]+$ ]] || return 1
  print -r -- "$value"
}

write_last_task_at() {
  local epoch="$1"
  local temp_file="$LAST_TASK_FILE.tmp.$$"
  [[ "$epoch" =~ ^[0-9]+$ ]] || die "Invalid last-task timestamp: $epoch"
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  print -r -- "$epoch" >"$temp_file"
  chmod 600 "$temp_file"
  mv -f "$temp_file" "$LAST_TASK_FILE"
}

read_last_triggered_window() {
  local value
  [[ -r "$LAST_TRIGGERED_WINDOW_FILE" ]] || return 1
  value="$(<"$LAST_TRIGGERED_WINDOW_FILE")"
  [[ -n "$value" ]] || return 1
  print -r -- "$value"
}

write_last_triggered_window() {
  local window_id="$1"
  local temp_file="$LAST_TRIGGERED_WINDOW_FILE.tmp.$$"
  [[ -n "$window_id" ]] || return 1
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  print -r -- "$window_id" >"$temp_file"
  chmod 600 "$temp_file"
  mv -f "$temp_file" "$LAST_TRIGGERED_WINDOW_FILE"
}

reserve_next_run() {
  local now
  now="$(/bin/date '+%s')"
  RUN_STARTED_AT="$now"
  schedule_next_after_run "$now"
}

schedule_next_after_run() {
  local now="$1"
  [[ "$now" =~ ^[0-9]+$ ]] || die "Invalid current timestamp"
  write_next_due $(( now + RUN_INTERVAL_SECONDS ))
}

schedule_next_from_resets() {
  local baseline="$1"
  local due="$baseline" reset

  if (( CODEX_QUOTA_IS_FRESH == 1 )); then
    reset="$("$JQ_BIN" -r '.fiveHour.resetAt // empty' "$CODEX_QUOTA_NORMALIZED_FILE" 2>/dev/null)"
    if [[ "$reset" =~ ^[0-9]+$ ]] && (( reset + RESET_BUFFER_SECONDS > due )); then
      due=$(( reset + RESET_BUFFER_SECONDS ))
    fi
  fi
  if (( ANTIGRAVITY_QUOTA_IS_FRESH == 1 )); then
    reset="$("$JQ_BIN" -r '.fiveHour.resetAt // empty' "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE" 2>/dev/null)"
    if [[ "$reset" =~ ^[0-9]+$ ]] && (( reset + RESET_BUFFER_SECONDS > due )); then
      due=$(( reset + RESET_BUFFER_SECONDS ))
    fi
  fi

  write_next_due "$due"
}

is_due() {
  local now="$1"
  local next_due
  if ! next_due="$(read_next_due)"; then
    return 0
  fi
  (( now >= next_due ))
}

acquire_run_lock() {
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  if ! "$SHLOCK_BIN" -p "$$" -f "$RUN_LOCK_FILE"; then
    return 1
  fi
  chmod 600 "$RUN_LOCK_FILE"
  RUN_LOCK_HELD=1
}

acquire_quota_lock() {
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  if ! "$SHLOCK_BIN" -p "$$" -f "$QUOTA_LOCK_FILE"; then
    return 1
  fi
  chmod 600 "$QUOTA_LOCK_FILE"
  QUOTA_LOCK_HELD=1
}

release_run_lock() {
  if (( RUN_LOCK_HELD == 1 )); then
    rm -f -- "$RUN_LOCK_FILE"
    RUN_LOCK_HELD=0
  fi
}

release_quota_lock() {
  if (( QUOTA_LOCK_HELD == 1 )); then
    rm -f -- "$QUOTA_LOCK_FILE"
    QUOTA_LOCK_HELD=0
  fi
}

require_executable() {
  [[ -x "$1" ]] || die "Required executable not found: $1"
}

keychain_read() {
  local service="$1"
  "$SECURITY_BIN" find-generic-password \
    -a "$KEYCHAIN_ACCOUNT" \
    -s "$service" \
    -w 2>/dev/null
}

keychain_has() {
  keychain_read "$1" >/dev/null 2>&1
}

feishu_app_id() {
  if [[ -n "${FEISHU_APP_ID:-}" ]]; then
    print -r -- "$FEISHU_APP_ID"
  else
    keychain_read "$FEISHU_APP_ID_SERVICE" ||
      die "Feishu App ID is missing from Keychain service: $FEISHU_APP_ID_SERVICE"
  fi
}

feishu_app_secret() {
  if [[ -n "${FEISHU_APP_SECRET:-}" ]]; then
    print -r -- "$FEISHU_APP_SECRET"
  else
    keychain_read "$FEISHU_APP_SECRET_SERVICE" ||
      die "Feishu App Secret is missing from Keychain service: $FEISHU_APP_SECRET_SERVICE"
  fi
}

feishu_user_id() {
  if [[ -n "${FEISHU_USER_ID:-}" ]]; then
    print -r -- "$FEISHU_USER_ID"
  else
    keychain_read "$FEISHU_USER_ID_SERVICE" ||
      die "Feishu user ID is missing. Run: $SCRIPT_NAME discover-feishu-user <email-or-mobile>"
  fi
}

feishu_ready() {
  { [[ -n "${FEISHU_APP_ID:-}" ]] || keychain_has "$FEISHU_APP_ID_SERVICE"; } &&
    { [[ -n "${FEISHU_APP_SECRET:-}" ]] || keychain_has "$FEISHU_APP_SECRET_SERVICE"; } &&
    { [[ -n "${FEISHU_USER_ID:-}" ]] || keychain_has "$FEISHU_USER_ID_SERVICE"; }
}

feishu_error_detail() {
  local response="$1"
  local detail=""
  detail="$(print -r -- "$response" |
    "$JQ_BIN" -r 'if .msg then "code \(.code): \(.msg)" else . end' 2>/dev/null)" || detail=""
  print -r -- "${detail:-$response}"
}

feishu_tenant_token() {
  local app_id="$1"
  local app_secret="$2"
  local response token

  # The app secret never enters process arguments or a URL; it is piped into
  # curl through stdin. No --retry here: a stdin body cannot be rewound.
  if ! response="$(printf '{"app_id":"%s","app_secret":"%s"}' "$app_id" "$app_secret" |
    "$CURL_BIN" \
      --request POST \
      --silent \
      --show-error \
      --fail-with-body \
      --connect-timeout 15 \
      --max-time 45 \
      --header "Content-Type: application/json; charset=utf-8" \
      --data-binary @- \
      "$FEISHU_API_BASE/auth/v3/tenant_access_token/internal")"; then
    print -u2 -r -- "Feishu token request failed: $(feishu_error_detail "$response")"
    return 1
  fi

  token="$(print -r -- "$response" | "$JQ_BIN" -r '.tenant_access_token // empty')"
  if [[ -z "$token" ]]; then
    print -u2 -r -- "Feishu token endpoint rejected the request: $(feishu_error_detail "$response")"
    return 1
  fi
  print -r -- "$token"
}

feishu_api() {
  local token="$1"
  local path="$2"
  shift 2

  # Feed the token through stdin so it is not exposed in process arguments.
  printf 'header = "Authorization: Bearer %s"\n' "$token" |
    "$CURL_BIN" \
      --config - \
      --silent \
      --show-error \
      --fail-with-body \
      --connect-timeout 15 \
      --max-time 45 \
      --retry 2 \
      --header "Content-Type: application/json; charset=utf-8" \
      "$FEISHU_API_BASE/$path" \
      "$@"
}

feishu_message_payload() {
  local user_id="$1"
  local message="$2"
  local request_uuid="$3"
  local header_template="green"

  [[ "$message" == *"发送失败"* ]] && header_template="red"

  # Feishu expects the interactive-card object itself to be JSON-encoded in
  # the message's `content` string.
  "$JQ_BIN" -n \
    --arg receive_id "$user_id" \
    --arg text "$message" \
    --arg template "$header_template" \
    --arg uuid "$request_uuid" \
    '{
      receive_id: $receive_id,
      msg_type: "interactive",
      content: ({
        config: {wide_screen_mode: true},
        header: {
          template: $template,
          title: {tag: "plain_text", content: "AI 模型运行与配额"}
        },
        elements: [
          {tag: "div", text: {tag: "lark_md", content: $text}},
          {tag: "hr"},
          {tag: "note", elements: [
            {tag: "plain_text", content: "Pi 自动任务 · 间隔至少 5 小时 01 分"}
          ]}
        ]
      } | tostring),
      uuid: $uuid
    }'
}

feishu_lookup_payload() {
  local identifier="$1"

  if [[ "$identifier" == *"@"* ]]; then
    "$JQ_BIN" -cn --arg email "$identifier" '{emails: [$email]}'
  elif [[ "$identifier" =~ '^\+?[0-9]+$' ]]; then
    # Mainland numbers are matched without the country code.
    identifier="${identifier#+86}"
    "$JQ_BIN" -cn --arg mobile "$identifier" '{mobiles: [$mobile]}'
  else
    return 1
  fi
}

send_feishu_message() {
  local app_id="$1"
  local app_secret="$2"
  local user_id="$3"
  local message="$4"
  local token payload response

  if [[ "${FEISHU_DRY_RUN:-0}" == "1" ]]; then
    print -r -- "$message"
    return 0
  fi

  token="$(feishu_tenant_token "$app_id" "$app_secret")" || return 1
  # The uuid makes curl-level retries idempotent: Feishu drops duplicate
  # sends that reuse it within an hour.
  payload="$(feishu_message_payload "$user_id" "$message" "quota-sentinel-$(/bin/date '+%s')")"

  if ! response="$(feishu_api "$token" "im/v1/messages?receive_id_type=user_id" \
    --data-binary "$payload")"; then
    print -u2 -r -- "Feishu message request failed: $(feishu_error_detail "$response")"
    return 1
  fi

  if ! print -r -- "$response" | "$JQ_BIN" -e '.code == 0' >/dev/null; then
    print -u2 -r -- "Feishu rejected the message: $(feishu_error_detail "$response")"
    return 1
  fi
}

dispatch_notification() {
  local message="$1"
  feishu_ready || die "Feishu enterprise-app credentials are not configured"
  send_feishu_message \
    "$(feishu_app_id)" \
    "$(feishu_app_secret)" \
    "$(feishu_user_id)" \
    "$message" || die "Feishu push failed"
}

prepare_run() {
  ensure_temp_dir
  CODEX_AGENT_DIR="$LAST_TEMP_DIR/codex-agent"
  CODEX_STDOUT_FILE="$LAST_TEMP_DIR/codex-stdout"
  CODEX_STDERR_FILE="$LAST_TEMP_DIR/codex-stderr"
  CODEX_QUOTA_FILE="$LAST_TEMP_DIR/codex-quota.json"
  ANTIGRAVITY_AGENT_DIR="$LAST_TEMP_DIR/antigravity-agent"
  ANTIGRAVITY_STDOUT_FILE="$LAST_TEMP_DIR/antigravity-stdout"
  ANTIGRAVITY_STDERR_FILE="$LAST_TEMP_DIR/antigravity-stderr"
  ANTIGRAVITY_QUOTA_FILE="$LAST_TEMP_DIR/antigravity-quota.json"

  mkdir -p "$CODEX_AGENT_DIR" "$ANTIGRAVITY_AGENT_DIR"

  # Refresh Codex once, then give both providers isolated copies of the Pi
  # credential store. Neither one writes to the user's real profile afterward.
  "$PI_BIN" auth print-bearer-token --provider openai-codex \
    >/dev/null 2>"$CODEX_STDERR_FILE" || true
  cp -p "$PI_AUTH_FILE" "$CODEX_AGENT_DIR/auth.json"
  cp -p "$PI_AUTH_FILE" "$ANTIGRAVITY_AGENT_DIR/auth.json"
  print -r -- '{"transport":"sse"}' >"$CODEX_AGENT_DIR/settings.json"
  print -r -- '{}' >"$ANTIGRAVITY_AGENT_DIR/settings.json"
}

prepare_quota_probe() {
  ensure_temp_dir
  CODEX_QUOTA_IS_FRESH=0
  ANTIGRAVITY_QUOTA_IS_FRESH=0
}

run_codex() {
  local output exit_code=0
  (
    cd /private/tmp
    PI_CODING_AGENT_DIR="$CODEX_AGENT_DIR" \
    PI_CODEX_QUOTA_FILE="$CODEX_QUOTA_FILE" \
    PI_OFFLINE=1 \
    "$PI_BIN" \
      --provider openai-codex \
      --model gpt-5.6-luna \
      --thinking off \
      --mode text \
      --print \
      --no-session \
      --no-tools \
      --no-extensions \
      --no-skills \
      --no-prompt-templates \
      --no-themes \
      --no-context-files \
      --no-approve \
      --offline \
      --system-prompt "忽略上下文" \
      --extension "$CODEX_QUOTA_EXTENSION" \
      -- "不用思考，只回复我 1"
  ) >"$CODEX_STDOUT_FILE" 2>>"$CODEX_STDERR_FILE" || exit_code=$?

  output="$(<"$CODEX_STDOUT_FILE")"
  (( exit_code == 0 )) && [[ "$output" == "1" ]]
}

run_antigravity() {
  local output exit_code=0
  (
    cd /private/tmp
    PI_CODING_AGENT_DIR="$ANTIGRAVITY_AGENT_DIR" \
    PI_ANTIGRAVITY_QUOTA_FILE="$ANTIGRAVITY_QUOTA_FILE" \
    PI_OFFLINE=1 \
    ANTIGRAVITY_NO_PREWARM=1 \
    "$PI_BIN" \
      --provider antigravity \
      --model gemini-3.7-flash \
      --thinking low \
      --mode text \
      --print \
      --no-session \
      --no-tools \
      --no-extensions \
      --no-skills \
      --no-prompt-templates \
      --no-themes \
      --no-context-files \
      --no-approve \
      --offline \
      --system-prompt "忽略上下文" \
      --extension "$ANTIGRAVITY_PROVIDER_EXTENSION" \
      --extension "$ANTIGRAVITY_QUOTA_EXTENSION" \
      -- "不用思考，只回复我 1"
  ) >"$ANTIGRAVITY_STDOUT_FILE" 2>>"$ANTIGRAVITY_STDERR_FILE" || exit_code=$?

  output="$(<"$ANTIGRAVITY_STDOUT_FILE")"
  (( exit_code == 0 )) && [[ "$output" == "1" ]]
}

format_reset_time() {
  TZ=Asia/Shanghai /bin/date -r "$1" '+%Y-%m-%d %H:%M:%S %Z'
}

format_duration() {
  local seconds="$1"
  local days hours minutes
  if (( seconds <= 0 )); then
    print -r -- "即将重置"
    return 0
  fi
  days=$(( seconds / 86400 ))
  hours=$(( (seconds % 86400) / 3600 ))
  minutes=$(( (seconds % 3600) / 60 ))
  if (( days > 0 )); then
    print -r -- "${days}天 ${hours}小时 ${minutes}分"
  elif (( hours > 0 )); then
    print -r -- "${hours}小时 ${minutes}分"
  else
    print -r -- "${minutes}分"
  fi
}

quota_bar() {
  local percent="$1"
  local filled empty i bar=""
  filled=$(( (percent + 5) / 10 ))
  (( filled < 0 )) && filled=0
  (( filled > 10 )) && filled=10
  empty=$(( 10 - filled ))
  for (( i = 0; i < filled; i++ )); do bar+="■"; done
  for (( i = 0; i < empty; i++ )); do bar+="□"; done
  print -r -- "$bar"
}

quota_failure_message() {
  printf '%s\n' \
    "5 小时：□□□□□□□□□□ 获取失败" \
    "重置：未知" \
    "周额度：□□□□□□□□□□ 获取失败" \
    "重置：未知" \
    "来源：不可用"
}

format_quota_message() {
  local five_remaining="$1"
  local five_reset="$2"
  local weekly_remaining="$3"
  local weekly_reset="$4"
  local source="$5"
  local now="${6:-$(/bin/date '+%s')}"
  local five_duration weekly_duration

  five_duration="$(format_duration $(( five_reset - now )))"
  weekly_duration="$(format_duration $(( weekly_reset - now )))"

  printf '%s\n' \
    "5 小时：$(quota_bar "$five_remaining") 剩余 ${five_remaining}%" \
    "重置：$(format_reset_time "$five_reset")（剩余 $five_duration）" \
    "周额度：$(quota_bar "$weekly_remaining") 剩余 ${weekly_remaining}%" \
    "重置：$(format_reset_time "$weekly_reset")（剩余 $weekly_duration）" \
    "来源：$source"
}

normalize_pi_codex_quota() {
  local input="$1" output="$2"
  [[ -s "$input" ]] || return 1
  "$JQ_BIN" -e '
    .headers as $h |
    ($h["x-codex-primary-used-percent"] | tonumber) as $primary_used |
    ($h["x-codex-primary-window-minutes"] | tonumber) as $primary_window |
    ($h["x-codex-primary-reset-at"] | tonumber) as $primary_reset |
    ($h["x-codex-secondary-used-percent"] | tonumber) as $secondary_used |
    ($h["x-codex-secondary-window-minutes"] | tonumber) as $secondary_window |
    ($h["x-codex-secondary-reset-at"] | tonumber) as $secondary_reset |
    select($primary_window == 300 and $secondary_window == 10080 and
      $primary_used >= 0 and $primary_used <= 100 and
      $secondary_used >= 0 and $secondary_used <= 100) |
    {
      source: "Pi 响应头快照",
      fetchedAt: (.capturedAt // null),
      fiveHour: {remainingPercent: (100 - $primary_used), resetAt: $primary_reset},
      weekly: {remainingPercent: (100 - $secondary_used), resetAt: $secondary_reset}
    }
  ' "$input" >"$output"
}

normalize_pi_antigravity_quota() {
  local input="$1" output="$2"
  [[ -s "$input" ]] || return 1
  "$JQ_BIN" -e '
    (.fiveHour.remainingPercent | tonumber) as $five_remaining |
    (.fiveHour.resetAt | tonumber) as $five_reset |
    (.weekly.remainingPercent | tonumber) as $weekly_remaining |
    (.weekly.resetAt | tonumber) as $weekly_reset |
    select($five_remaining >= 0 and $five_remaining <= 100 and
      $weekly_remaining >= 0 and $weekly_remaining <= 100) |
    {
      source: "Pi Antigravity API 快照",
      fetchedAt: (.capturedAt // null),
      fiveHour: {remainingPercent: $five_remaining, resetAt: $five_reset},
      weekly: {remainingPercent: $weekly_remaining, resetAt: $weekly_reset}
    }
  ' "$input" >"$output"
}

normalize_codexbar_codex_quota() {
  local input="$1" output="$2"
  [[ -s "$input" ]] || return 1
  "$JQ_BIN" -e '
    def epoch($value):
      if ($value | type) == "number" then ($value | floor)
      elif ($value | type) == "string" then ($value | fromdateiso8601)
      else empty end;
    def remaining($used): ([0, (100 - ($used | tonumber)), 100] | sort | .[1] | round);
    ([.[] | select(.provider == "codex" and (.usage | type) == "object")][0] // empty) as $row |
    select($row != null) |
    $row.usage as $usage |
    ([$usage.primary, $usage.secondary, ($usage.extraRateWindows[]?.window)] | map(select(. != null))) as $windows |
    ([$windows[] | select(.windowMinutes == 300 and .usedPercent != null)][0] // empty) as $five |
    ([$windows[] | select(.windowMinutes == 10080 and .usedPercent != null)][0] // empty) as $weekly |
    select($five != null and $weekly != null) |
    {
      source: ("CodexBar · " + ($row.source // "unknown")),
      fetchedAt: ($usage.updatedAt // null),
      fiveHour: {remainingPercent: remaining($five.usedPercent), resetAt: epoch($five.resetsAt)},
      weekly: {remainingPercent: remaining($weekly.usedPercent), resetAt: epoch($weekly.resetsAt)}
    }
  ' "$input" >"$output"
}

normalize_codexbar_antigravity_quota() {
  local input="$1" output="$2"
  [[ -s "$input" ]] || return 1
  "$JQ_BIN" -e '
    def epoch($value):
      if ($value | type) == "number" then ($value | floor)
      elif ($value | type) == "string" then ($value | fromdateiso8601)
      else empty end;
    def remaining($used): ([0, (100 - ($used | tonumber)), 100] | sort | .[1] | round);
    ([.[] | select(.provider == "antigravity" and (.usage | type) == "object")][0] // empty) as $row |
    select($row != null) |
    $row.usage as $usage |
    ($usage.extraRateWindows // []) as $windows |
    ([$windows[] | select(.window.windowMinutes == 300 and (((.id // "") | contains("gemini")) or ((.title // "") | ascii_downcase | contains("gemini"))))][0].window // empty) as $five |
    ([$windows[] | select(.window.windowMinutes == 10080 and (((.id // "") | contains("gemini")) or ((.title // "") | ascii_downcase | contains("gemini"))))][0].window // empty) as $weekly |
    select($five != null and $weekly != null) |
    {
      source: ("CodexBar · " + ($row.source // "unknown")),
      fetchedAt: ($usage.updatedAt // null),
      fiveHour: {remainingPercent: remaining($five.usedPercent), resetAt: epoch($five.resetsAt)},
      weekly: {remainingPercent: remaining($weekly.usedPercent), resetAt: epoch($weekly.resetsAt)}
    }
  ' "$input" >"$output"
}

fetch_codexbar_codex_quota() {
  [[ -x "$CODEXBAR_BIN" ]] || return 1
  if "$CODEXBAR_BIN" usage --provider codex --source cli --format json --json-only --no-color \
    >"$CODEXBAR_CODEX_RAW_FILE" 2>"$LAST_TEMP_DIR/codexbar-codex.stderr" &&
    normalize_codexbar_codex_quota "$CODEXBAR_CODEX_RAW_FILE" "$CODEX_QUOTA_NORMALIZED_FILE"; then
    return 0
  fi
  if "$CODEXBAR_BIN" usage --provider codex --source oauth --format json --json-only --no-color \
    >"$CODEXBAR_CODEX_RAW_FILE" 2>"$LAST_TEMP_DIR/codexbar-codex.stderr" &&
    normalize_codexbar_codex_quota "$CODEXBAR_CODEX_RAW_FILE" "$CODEX_QUOTA_NORMALIZED_FILE"; then
    return 0
  fi
  return 1
}

fetch_codexbar_antigravity_quota() {
  [[ -x "$CODEXBAR_BIN" ]] || return 1
  if "$CODEXBAR_BIN" usage --provider antigravity --source cli --format json --json-only --no-color \
    >"$CODEXBAR_ANTIGRAVITY_RAW_FILE" 2>"$LAST_TEMP_DIR/codexbar-antigravity.stderr" &&
    normalize_codexbar_antigravity_quota "$CODEXBAR_ANTIGRAVITY_RAW_FILE" "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE"; then
    return 0
  fi
  return 1
}

atomic_copy() {
  local input="$1" output="$2" temporary="${2}.tmp.$$"
  cp -p "$input" "$temporary"
  chmod 600 "$temporary"
  mv -f "$temporary" "$output"
}

save_pi_quota_snapshots() {
  local codex_normalized="$LAST_TEMP_DIR/codex-pi-normalized.json"
  local antigravity_normalized="$LAST_TEMP_DIR/antigravity-pi-normalized.json"
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  normalize_pi_codex_quota "$CODEX_QUOTA_FILE" "$codex_normalized" &&
    atomic_copy "$codex_normalized" "$PI_CODEX_SNAPSHOT_FILE" || true
  normalize_pi_antigravity_quota "$ANTIGRAVITY_QUOTA_FILE" "$antigravity_normalized" &&
    atomic_copy "$antigravity_normalized" "$PI_ANTIGRAVITY_SNAPSHOT_FILE" || true
}

use_pi_or_saved_codex_fallback() {
  local normalized="$LAST_TEMP_DIR/codex-pi-normalized.json"
  if normalize_pi_codex_quota "$CODEX_QUOTA_FILE" "$normalized"; then
    cp -p "$normalized" "$CODEX_QUOTA_NORMALIZED_FILE"
    CODEX_QUOTA_IS_FRESH=1
    return 0
  fi
  [[ -s "$PI_CODEX_SNAPSHOT_FILE" ]] || return 1
  cp -p "$PI_CODEX_SNAPSHOT_FILE" "$CODEX_QUOTA_NORMALIZED_FILE"
}

use_pi_or_saved_antigravity_fallback() {
  local normalized="$LAST_TEMP_DIR/antigravity-pi-normalized.json"
  if normalize_pi_antigravity_quota "$ANTIGRAVITY_QUOTA_FILE" "$normalized"; then
    cp -p "$normalized" "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE"
    ANTIGRAVITY_QUOTA_IS_FRESH=1
    return 0
  fi
  [[ -s "$PI_ANTIGRAVITY_SNAPSHOT_FILE" ]] || return 1
  cp -p "$PI_ANTIGRAVITY_SNAPSHOT_FILE" "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE"
}

collect_effective_quotas() {
  prepare_quota_probe
  if fetch_codexbar_codex_quota; then
    CODEX_QUOTA_IS_FRESH=1
  else
    use_pi_or_saved_codex_fallback || true
  fi
  if fetch_codexbar_antigravity_quota; then
    ANTIGRAVITY_QUOTA_IS_FRESH=1
  else
    use_pi_or_saved_antigravity_fallback || true
  fi
}

quota_message_from_file() {
  local quota_file="$1"
  local five_remaining five_reset weekly_remaining weekly_reset source
  [[ -s "$quota_file" ]] || { quota_failure_message; return 1; }

  five_remaining="$("$JQ_BIN" -r '.fiveHour.remainingPercent // empty' "$quota_file")"
  five_reset="$("$JQ_BIN" -r '.fiveHour.resetAt // empty' "$quota_file")"
  weekly_remaining="$("$JQ_BIN" -r '.weekly.remainingPercent // empty' "$quota_file")"
  weekly_reset="$("$JQ_BIN" -r '.weekly.resetAt // empty' "$quota_file")"
  source="$("$JQ_BIN" -r '.source // "未知"' "$quota_file")"

  if [[ ! "$five_remaining" =~ ^[0-9]+$ ]] ||
    [[ ! "$five_reset" =~ ^[0-9]+$ ]] ||
    [[ ! "$weekly_remaining" =~ ^[0-9]+$ ]] ||
    [[ ! "$weekly_reset" =~ ^[0-9]+$ ]] ||
    (( five_remaining > 100 || weekly_remaining > 100 )); then
    quota_failure_message
    return 1
  fi

  format_quota_message "$five_remaining" "$five_reset" "$weekly_remaining" "$weekly_reset" "$source" "${CURRENT_FORMAT_TIME:-}"
}

codex_quota_message() {
  quota_message_from_file "$CODEX_QUOTA_NORMALIZED_FILE"
}

antigravity_quota_message() {
  quota_message_from_file "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE"
}

notification_message() {
  local codex_result="$1"
  local codex_quota="$2"
  local antigravity_result="$3"
  local antigravity_quota="$4"
  local timestamp
  timestamp="$(TZ=Asia/Shanghai /bin/date '+%Y-%m-%d %H:%M:%S %Z')"

  [[ "$codex_result" == "发送成功" ]] &&
    codex_result="🟢 **发送成功**" || codex_result="🔴 **发送失败**"
  [[ "$antigravity_result" == "发送成功" ]] &&
    antigravity_result="🟢 **发送成功**" || antigravity_result="🔴 **发送失败**"

  codex_quota="${codex_quota//5 小时：/**5 小时**　}"
  codex_quota="${codex_quota//周额度：/**周额度**　}"
  codex_quota="${codex_quota//重置：/↳ 重置　}"
  codex_quota="${codex_quota//来源：/↳ 来源　}"
  antigravity_quota="${antigravity_quota//5 小时：/**5 小时**　}"
  antigravity_quota="${antigravity_quota//周额度：/**周额度**　}"
  antigravity_quota="${antigravity_quota//重置：/↳ 重置　}"
  antigravity_quota="${antigravity_quota//来源：/↳ 来源　}"

  printf '%s\n' \
    "**GPT-5.6 Luna**" \
    "$codex_result" \
    "$codex_quota" \
    "" \
    "────────────" \
    "" \
    "**Gemini 3.7 Flash · Low**" \
    "$antigravity_result" \
    "$antigravity_quota" \
    "" \
    "**图例**　■ 剩余　□ 已用" \
    "🕒 $timestamp"
}

usage_notification_message() {
  local codex_quota="$1"
  local antigravity_quota="$2"
  local timestamp
  timestamp="$(TZ=Asia/Shanghai /bin/date '+%Y-%m-%d %H:%M:%S %Z')"

  codex_quota="${codex_quota//5 小时：/**5 小时**　}"
  codex_quota="${codex_quota//周额度：/**周额度**　}"
  codex_quota="${codex_quota//重置：/↳ 重置　}"
  codex_quota="${codex_quota//来源：/↳ 来源　}"
  antigravity_quota="${antigravity_quota//5 小时：/**5 小时**　}"
  antigravity_quota="${antigravity_quota//周额度：/**周额度**　}"
  antigravity_quota="${antigravity_quota//重置：/↳ 重置　}"
  antigravity_quota="${antigravity_quota//来源：/↳ 来源　}"

  printf '%s\n' \
    "**即时配额查询**　未执行模型任务" \
    "" \
    "**GPT-5.6 Luna**" \
    "$codex_quota" \
    "" \
    "────────────" \
    "" \
    "**Gemini 3.7 Flash · Low**" \
    "$antigravity_quota" \
    "" \
    "**图例**　■ 剩余　□ 已用" \
    "🕒 $timestamp"
}

discover_feishu_user() {
  local identifier="${1:-}"
  local app_id app_secret token body response user_id
  [[ -n "$identifier" ]] || die "Usage: $SCRIPT_NAME discover-feishu-user <email-or-mobile>"

  app_id="$(feishu_app_id)"
  app_secret="$(feishu_app_secret)"
  token="$(feishu_tenant_token "$app_id" "$app_secret")" ||
    die "Could not obtain a Feishu tenant access token"

  body="$(feishu_lookup_payload "$identifier")" ||
    die "Expected an email address or a mobile number: $identifier"

  if ! response="$(feishu_api "$token" "contact/v3/users/batch_get_id?user_id_type=user_id" \
    --data-binary "$body")"; then
    die "Could not look up the Feishu user: $(feishu_error_detail "$response")"
  fi
  print -r -- "$response" | "$JQ_BIN" -e '.code == 0' >/dev/null ||
    die "Feishu user lookup rejected the request: $(feishu_error_detail "$response")"

  user_id="$(print -r -- "$response" | "$JQ_BIN" -r '.data.user_list[0].user_id // empty')"
  if [[ -z "$user_id" ]]; then
    die "No Feishu user matched: $identifier. The lookup only matches the personal email or mobile bound to the Feishu account; enterprise emails are not supported."
  fi

  "$SECURITY_BIN" add-generic-password \
    -U \
    -a "$KEYCHAIN_ACCOUNT" \
    -s "$FEISHU_USER_ID_SERVICE" \
    -T "$SECURITY_BIN" \
    -w "$user_id" >/dev/null
  print -r -- "Configured Feishu user ID: $user_id"
}

status() {
  require_executable "$PI_BIN"
  require_executable "$CURL_BIN"
  require_executable "$JQ_BIN"
  require_executable "$SECURITY_BIN"
  require_executable "$SHLOCK_BIN"
  require_executable "$SLEEP_BIN"
  [[ -r "$PI_AUTH_FILE" ]] || die "Pi OAuth credential is not readable: $PI_AUTH_FILE"
  "$JQ_BIN" -e 'has("openai-codex") and has("antigravity")' "$PI_AUTH_FILE" >/dev/null ||
    die "Pi credentials for Codex or Antigravity are missing"
  [[ -r "$CODEX_QUOTA_EXTENSION" ]] || die "Codex quota hook is not readable"
  [[ -r "$ANTIGRAVITY_QUOTA_EXTENSION" ]] || die "Antigravity quota hook is not readable"
  [[ -r "$ANTIGRAVITY_PROVIDER_EXTENSION" ]] || die "Antigravity provider extension is not readable"
  feishu_ready || die "Feishu enterprise-app credentials are not configured"
  print -r -- "ready"
  print -r -- "channel: feishu enterprise app"
  if [[ -x "$CODEXBAR_BIN" ]]; then
    print -r -- "quota primary: CodexBar (Codex CLI / Antigravity agy)"
  else
    print -r -- "quota primary: unavailable; last Pi snapshots may be used"
  fi
  local next_due
  if next_due="$(read_next_due)"; then
    print -r -- "next run: $(format_reset_time "$next_due")"
  else
    print -r -- "next run: due now"
  fi
}

run_once() {
  local codex_result antigravity_result codex_quota antigravity_quota
  local codex_pid antigravity_pid now codex_reset antigravity_reset

  status >/dev/null
  prepare_run

  now="$(/bin/date '+%s')"
  RUN_STARTED_AT="$now"
  write_last_task_at "$now"
  schedule_next_after_run "$now"

  run_codex &
  codex_pid=$!
  run_antigravity &
  antigravity_pid=$!

  if wait "$codex_pid"; then codex_result="发送成功"; else codex_result="发送失败"; fi
  if wait "$antigravity_pid"; then antigravity_result="发送成功"; else antigravity_result="发送失败"; fi

  save_pi_quota_snapshots
  collect_effective_quotas
  codex_quota="$(codex_quota_message)" || true
  antigravity_quota="$(antigravity_quota_message)" || true

  if (( CODEX_QUOTA_IS_FRESH == 1 && ANTIGRAVITY_QUOTA_IS_FRESH == 1 )); then
    codex_reset="$(five_hour_reset_at "$CODEX_QUOTA_NORMALIZED_FILE")"
    antigravity_reset="$(five_hour_reset_at "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE")"
    if [[ "$codex_reset" =~ ^[0-9]+$ ]] && [[ "$antigravity_reset" =~ ^[0-9]+$ ]]; then
      write_last_triggered_window "${codex_reset}:${antigravity_reset}"
    fi
  fi

  schedule_next_from_resets $(( RUN_STARTED_AT + RUN_INTERVAL_SECONDS ))

  dispatch_notification \
    "$(notification_message "$codex_result" "$codex_quota" "$antigravity_result" "$antigravity_quota")"
}

send_usage_notification() {
  local codex_quota antigravity_quota
  acquire_quota_lock || return 0
  prepare_quota_probe
  collect_effective_quotas
  codex_quota="$(codex_quota_message)" || true
  antigravity_quota="$(antigravity_quota_message)" || true
  dispatch_notification "$(usage_notification_message "$codex_quota" "$antigravity_quota")"
  release_quota_lock
}

run_and_reschedule() {
  acquire_run_lock || die "Another model run is already in progress"
  run_once
  release_run_lock
}

five_hour_reset_at() {
  "$JQ_BIN" -r '.fiveHour.resetAt // empty' "$1" 2>/dev/null
}

monitor_windows_ready() {
  local now="$1"
  local codex_reset antigravity_reset

  (( CODEX_QUOTA_IS_FRESH == 1 && ANTIGRAVITY_QUOTA_IS_FRESH == 1 )) || return 1
  codex_reset="$(five_hour_reset_at "$CODEX_QUOTA_NORMALIZED_FILE")"
  antigravity_reset="$(five_hour_reset_at "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE")"
  [[ "$codex_reset" =~ ^[0-9]+$ ]] || return 1
  [[ "$antigravity_reset" =~ ^[0-9]+$ ]] || return 1
  (( codex_reset - now >= FRESH_WINDOW_SECONDS )) || return 1
  (( antigravity_reset - now >= FRESH_WINDOW_SECONDS )) || return 1
}

monitor_alignment_due() {
  local now="$1"
  local due="$now" reset

  for reset in \
    "$(five_hour_reset_at "$CODEX_QUOTA_NORMALIZED_FILE")" \
    "$(five_hour_reset_at "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE")"; do
    if [[ "$reset" =~ ^[0-9]+$ ]] && (( reset + RESET_BUFFER_SECONDS > due )); then
      due=$(( reset + RESET_BUFFER_SECONDS ))
    fi
  done
  print -r -- "$due"
}

check_schedule() {
  local now next_due had_due=0 should_run=0
  local codex_reset antigravity_reset current_window last_window last_task
  local reset_candidate min_candidate target_due can_run_interval=1
  now="$(/bin/date '+%s')"
  if next_due="$(read_next_due)"; then
    had_due=1
  fi

  acquire_quota_lock || return 0
  prepare_quota_probe
  collect_effective_quotas

  if (( CODEX_QUOTA_IS_FRESH == 1 && ANTIGRAVITY_QUOTA_IS_FRESH == 1 )); then
    codex_reset="$(five_hour_reset_at "$CODEX_QUOTA_NORMALIZED_FILE")"
    antigravity_reset="$(five_hour_reset_at "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE")"

    if [[ "$codex_reset" =~ ^[0-9]+$ ]] && [[ "$antigravity_reset" =~ ^[0-9]+$ ]] &&
       (( codex_reset > now && codex_reset <= now + MAX_WINDOW_FUTURE_SECONDS )) &&
       (( antigravity_reset > now && antigravity_reset <= now + MAX_WINDOW_FUTURE_SECONDS )); then

      current_window="${codex_reset}:${antigravity_reset}"
      last_window="$(read_last_triggered_window || true)"
      last_task="$(read_last_task_at || true)"

      if [[ -n "$last_task" && "$last_task" =~ ^[0-9]+$ ]] && (( now < last_task + RUN_INTERVAL_SECONDS )); then
        can_run_interval=0
      fi

      if [[ -n "$last_window" && "$current_window" == "$last_window" ]]; then
        local task_base="$now"
        [[ -n "$last_task" && "$last_task" =~ ^[0-9]+$ ]] && task_base="$last_task"
        reset_candidate=$(( (codex_reset > antigravity_reset ? codex_reset : antigravity_reset) + RESET_BUFFER_SECONDS ))
        min_candidate=$(( task_base + RUN_INTERVAL_SECONDS ))
        target_due=$(( reset_candidate > min_candidate ? reset_candidate : min_candidate ))
        write_next_due "$target_due"
        should_run=0
      else
        if (( can_run_interval == 1 )); then
          should_run=1
        else
          target_due=$(( last_task + RUN_INTERVAL_SECONDS ))
          write_next_due "$target_due"
          should_run=0
        fi
      fi
    else
      if (( had_due == 0 )) || (( now >= next_due )); then
        write_next_due $(( now + 900 ))
      fi
    fi
  else
    if (( had_due == 0 )) || (( now >= next_due )); then
      write_next_due $(( now + 900 ))
    fi
  fi
  release_quota_lock

  if (( should_run == 0 )); then
    return 0
  fi

  acquire_run_lock || return 0
  run_once
  release_run_lock
}

wait_schedule() {
  local now next_due delay
  while true; do
    now="$(/bin/date '+%s')"
    if next_due="$(read_next_due)" && (( now < next_due )); then
      delay=$(( next_due - now ))
      "$SLEEP_BIN" "$delay"
      continue
    fi
    check_schedule
    # Avoid a tight loop if the watchdog currently owns the run lock.
    "$SLEEP_BIN" 1
  done
}

main() {
  local command="${1:-run}"

  case "$command" in
    check)
      check_schedule
      ;;
    wait)
      wait_schedule
      ;;
    run)
      run_and_reschedule
      ;;
    usage)
      send_usage_notification
      ;;
    discover-feishu-user)
      require_executable "$CURL_BIN"
      require_executable "$JQ_BIN"
      require_executable "$SECURITY_BIN"
      discover_feishu_user "${2:-}"
      ;;
    status)
      status
      ;;
    help|-h|--help)
      usage
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
}

if [[ "${PI_SOURCE_ONLY:-0}" != "1" ]]; then
  trap cleanup EXIT
  main "$@"
fi
