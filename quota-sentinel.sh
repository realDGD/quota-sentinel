#!/bin/zsh

set -euo pipefail

readonly PI_BIN="/opt/homebrew/bin/pi"
readonly CODEX_BIN="/opt/homebrew/bin/codex"
readonly AGY_BIN="/opt/homebrew/bin/agy"
readonly CODEXBAR_BIN="/opt/homebrew/bin/codexbar"
readonly PYTHON3_BIN="/usr/bin/python3"
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
readonly RUN_LOCK_FILE="$STATE_DIR/run.lock"
readonly QUOTA_LOCK_FILE="$STATE_DIR/quota.lock"
readonly CODEXBAR_CODEX_CACHE_FILE="$STATE_DIR/codexbar-codex-last-success.json"
readonly CODEXBAR_ANTIGRAVITY_CACHE_FILE="$STATE_DIR/codexbar-antigravity-last-success.json"
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
typeset -g CODEX_RUN_RESULT=""
typeset -g ANTIGRAVITY_RUN_RESULT=""
typeset -g RUN_STARTED_AT=0
typeset -gi RUN_LOCK_HELD=0
typeset -gi QUOTA_LOCK_HELD=0

usage() {
  print -r -- "Usage: $SCRIPT_NAME [check|wait|run [codex|antigravity|all]|usage|discover-feishu-user|status]"
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

provider_next_due_file() {
  local provider="$1"
  print -r -- "$STATE_DIR/${provider}-next-due-at"
}

provider_last_task_file() {
  local provider="$1"
  print -r -- "$STATE_DIR/${provider}-last-task-at"
}

provider_last_known_reset_file() {
  local provider="$1"
  print -r -- "$STATE_DIR/${provider}-last-known-reset-at"
}

provider_last_window_file() {
  local provider="$1"
  print -r -- "$STATE_DIR/${provider}-last-triggered-window"
}

migrate_legacy_state() {
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"

  # Migrate last-task-at
  if [[ -r "$STATE_DIR/last-task-at" ]]; then
    local legacy_task
    legacy_task="$(<"$STATE_DIR/last-task-at")"
    if [[ "$legacy_task" =~ ^[0-9]+$ ]]; then
      if [[ ! -r "$STATE_DIR/codex-last-task-at" ]]; then
        print -r -- "$legacy_task" >"$STATE_DIR/codex-last-task-at"
        chmod 600 "$STATE_DIR/codex-last-task-at"
      fi
      if [[ ! -r "$STATE_DIR/antigravity-last-task-at" ]]; then
        print -r -- "$legacy_task" >"$STATE_DIR/antigravity-last-task-at"
        chmod 600 "$STATE_DIR/antigravity-last-task-at"
      fi
    fi
  fi

  # Migrate next-due-at
  if [[ -r "$STATE_DIR/next-due-at" ]]; then
    local legacy_due
    legacy_due="$(<"$STATE_DIR/next-due-at")"
    if [[ "$legacy_due" =~ ^[0-9]+$ ]]; then
      if [[ ! -r "$STATE_DIR/codex-next-due-at" ]]; then
        print -r -- "$legacy_due" >"$STATE_DIR/codex-next-due-at"
        chmod 600 "$STATE_DIR/codex-next-due-at"
      fi
      if [[ ! -r "$STATE_DIR/antigravity-next-due-at" ]]; then
        print -r -- "$legacy_due" >"$STATE_DIR/antigravity-next-due-at"
        chmod 600 "$STATE_DIR/antigravity-next-due-at"
      fi
    fi
  fi

  # Migrate last-triggered-window
  if [[ -r "$STATE_DIR/last-triggered-window" ]]; then
    local legacy_window
    legacy_window="$(<"$STATE_DIR/last-triggered-window")"
    if [[ "$legacy_window" == *:* ]]; then
      local c_win="${legacy_window%%:*}"
      local a_win="${legacy_window##*:}"
      if [[ -n "$c_win" && ! -r "$STATE_DIR/codex-last-triggered-window" ]]; then
        print -r -- "$c_win" >"$STATE_DIR/codex-last-triggered-window"
        chmod 600 "$STATE_DIR/codex-last-triggered-window"
      fi
      if [[ -n "$a_win" && ! -r "$STATE_DIR/antigravity-last-triggered-window" ]]; then
        print -r -- "$a_win" >"$STATE_DIR/antigravity-last-triggered-window"
        chmod 600 "$STATE_DIR/antigravity-last-triggered-window"
      fi
      if [[ -n "$c_win" && ! -r "$STATE_DIR/codex-last-known-reset-at" ]]; then
        print -r -- "$c_win" >"$STATE_DIR/codex-last-known-reset-at"
        chmod 600 "$STATE_DIR/codex-last-known-reset-at"
      fi
      if [[ -n "$a_win" && ! -r "$STATE_DIR/antigravity-last-known-reset-at" ]]; then
        print -r -- "$a_win" >"$STATE_DIR/antigravity-last-known-reset-at"
        chmod 600 "$STATE_DIR/antigravity-last-known-reset-at"
      fi
    fi
  fi
}

read_provider_next_due() {
  local provider="$1" file value
  migrate_legacy_state
  file="$(provider_next_due_file "$provider")"
  [[ -r "$file" ]] || return 1
  value="$(<"$file")"
  [[ "$value" =~ ^[0-9]+$ ]] || return 1
  print -r -- "$value"
}

write_provider_next_due() {
  local provider="$1" epoch="$2" file temp_file
  [[ "$epoch" =~ ^[0-9]+$ ]] || die "Invalid next-run timestamp: $epoch"
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  file="$(provider_next_due_file "$provider")"
  temp_file="${file}.tmp.$$"
  print -r -- "$epoch" >"$temp_file"
  chmod 600 "$temp_file"
  mv -f "$temp_file" "$file"
}

read_provider_last_task() {
  local provider="$1" file value
  migrate_legacy_state
  file="$(provider_last_task_file "$provider")"
  [[ -r "$file" ]] || return 1
  value="$(<"$file")"
  [[ "$value" =~ ^[0-9]+$ ]] || return 1
  print -r -- "$value"
}

write_provider_last_task() {
  local provider="$1" epoch="$2" file temp_file
  [[ "$epoch" =~ ^[0-9]+$ ]] || die "Invalid last-task timestamp: $epoch"
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  file="$(provider_last_task_file "$provider")"
  temp_file="${file}.tmp.$$"
  print -r -- "$epoch" >"$temp_file"
  chmod 600 "$temp_file"
  mv -f "$temp_file" "$file"
}

read_provider_last_known_reset() {
  local provider="$1" file value
  migrate_legacy_state
  file="$(provider_last_known_reset_file "$provider")"
  [[ -r "$file" ]] || return 1
  value="$(<"$file")"
  [[ "$value" =~ ^[0-9]+$ ]] || return 1
  print -r -- "$value"
}

write_provider_last_known_reset() {
  local provider="$1" epoch="$2" file temp_file
  [[ "$epoch" =~ ^[0-9]+$ ]] || die "Invalid last-known-reset timestamp: $epoch"
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  file="$(provider_last_known_reset_file "$provider")"
  temp_file="${file}.tmp.$$"
  print -r -- "$epoch" >"$temp_file"
  chmod 600 "$temp_file"
  mv -f "$temp_file" "$file"
}

read_provider_last_window() {
  local provider="$1" file value
  migrate_legacy_state
  file="$(provider_last_window_file "$provider")"
  [[ -r "$file" ]] || return 1
  value="$(<"$file")"
  [[ -n "$value" ]] || return 1
  print -r -- "$value"
}

write_provider_last_window() {
  local provider="$1" window_id="$2" file temp_file
  [[ -n "$window_id" ]] || return 1
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  file="$(provider_last_window_file "$provider")"
  temp_file="${file}.tmp.$$"
  print -r -- "$window_id" >"$temp_file"
  chmod 600 "$temp_file"
  mv -f "$temp_file" "$file"
}

read_next_due() {
  local c_due a_due min_due=""
  c_due="$(read_provider_next_due "codex" || true)"
  a_due="$(read_provider_next_due "antigravity" || true)"

  if [[ "$c_due" =~ ^[0-9]+$ ]] && [[ "$a_due" =~ ^[0-9]+$ ]]; then
    min_due=$(( c_due < a_due ? c_due : a_due ))
  elif [[ "$c_due" =~ ^[0-9]+$ ]]; then
    min_due="$c_due"
  elif [[ "$a_due" =~ ^[0-9]+$ ]]; then
    min_due="$a_due"
  else
    return 1
  fi
  print -r -- "$min_due"
}

write_next_due() {
  local epoch="$1"
  write_provider_next_due "codex" "$epoch"
  write_provider_next_due "antigravity" "$epoch"
}

read_last_task_at() {
  read_provider_last_task "codex"
}

write_last_task_at() {
  local epoch="$1"
  write_provider_last_task "codex" "$epoch"
}

read_last_triggered_window() {
  read_provider_last_window "codex"
}

write_last_triggered_window() {
  local window_id="$1"
  write_provider_last_window "codex" "$window_id"
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
  local message_or_payload="$4"
  local token payload response

  if [[ "${FEISHU_DRY_RUN:-0}" == "1" ]]; then
    print -r -- "$message_or_payload"
    return 0
  fi

  token="$(feishu_tenant_token "$app_id" "$app_secret")" || return 1

  if [[ "$message_or_payload" =~ '^[[:space:]]*\{' ]] && print -r -- "$message_or_payload" | "$JQ_BIN" -e '.receive_id and .msg_type' >/dev/null 2>&1; then
    payload="$message_or_payload"
  else
    payload="$(feishu_message_payload "$user_id" "$message_or_payload" "quota-sentinel-$(/bin/date '+%s')")"
  fi

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

build_linear_progress_chart() {
  local percent="$1"
  local color_hex="${2:-#57D0FB}"
  local val="1.0"

  if [[ "$percent" =~ ^-?[0-9]+$ ]]; then
    (( percent < 0 )) && percent=0
    (( percent > 100 )) && percent=100
    val="$("$JQ_BIN" -n --argjson p "$percent" '$p / 100.0')"
  else
    val="1.0"
  fi

  "$JQ_BIN" -n \
    --argjson val "$val" \
    --arg color "$color_hex" \
    '{
      tag: "chart",
      aspect_ratio: "16:9",
      height: "26px",
      preview: false,
      chart_spec: {
        type: "linearProgress",
        data: {
          values: [
            {
              type: "quota",
              value: $val
            }
          ]
        },
        direction: "horizontal",
        xField: "value",
        yField: "type",
        seriesField: "type",
        color: [$color],
        progress: {
          style: {
            fill: $color,
            cornerRadius: 4
          }
        },
        track: {
          style: {
            cornerRadius: 4
          }
        },
        bandWidth: 10,
        axes: [
          { orient: "left", visible: false },
          { orient: "bottom", visible: false }
        ],
        legends: { visible: false },
        tooltip: { visible: false },
        padding: { top: 0, bottom: 0, left: 0, right: 0 }
      }
    }'
}

build_provider_v2_elements() {
  local title="$1" result="$2" quota_file="$3"
  local five_remaining="0" five_reset="0" weekly_remaining="0" weekly_reset="0" raw_source="未知"
  local five_duration="未知" weekly_duration="未知" five_reset_time="未知" weekly_reset_time="未知"
  local now="${CURRENT_FORMAT_TIME:-$(/bin/date '+%s')}"

  if [[ -s "$quota_file" ]]; then
    five_remaining="$("$JQ_BIN" -r '.fiveHour.remainingPercent // empty' "$quota_file")"
    five_reset="$("$JQ_BIN" -r '.fiveHour.resetAt // empty' "$quota_file")"
    weekly_remaining="$("$JQ_BIN" -r '.weekly.remainingPercent // empty' "$quota_file")"
    weekly_reset="$("$JQ_BIN" -r '.weekly.resetAt // empty' "$quota_file")"
    raw_source="$("$JQ_BIN" -r '.source // "未知"' "$quota_file")"
  fi

  if [[ ! "$five_remaining" =~ ^[0-9]+$ ]] ||
     [[ ! "$five_reset" =~ ^[0-9]+$ ]] ||
     [[ ! "$weekly_remaining" =~ ^[0-9]+$ ]] ||
     [[ ! "$weekly_reset" =~ ^[0-9]+$ ]]; then
    five_remaining=0
    five_reset=0
    weekly_remaining=0
    weekly_reset=0
    raw_source="不可用"
    five_duration="未知"
    five_reset_time="未知"
    weekly_duration="未知"
    weekly_reset_time="未知"
  else
    five_duration="$(format_duration $(( five_reset - now )))"
    weekly_duration="$(format_duration $(( weekly_reset - now )))"
    five_reset_time="$(format_reset_time "$five_reset")"
    weekly_reset_time="$(format_reset_time "$weekly_reset")"
  fi

  local title_md="**${title}**"
  local source_md=""
  if [[ "$raw_source" == *"cached"* ]]; then
    source_md="来源　CodexBar · cached\n⚠️ 可能不是最新"
  elif [[ "$raw_source" == *"快照"* ]]; then
    source_md="来源　Pi 快照\n⚠️ 可能不是最新"
  else
    source_md="来源　${raw_source}"
  fi

  local status_md=""
  if [[ -n "$result" ]]; then
    if [[ "$result" == "发送成功" ]]; then
      status_md="🟢 **发送成功**"
    else
      status_md="🔴 **发送失败**"
    fi
  fi

  local chart_5h chart_weekly
  chart_5h="$(build_linear_progress_chart "$five_remaining" "#57D0FB")" || return 1
  chart_weekly="$(build_linear_progress_chart "$weekly_remaining" "#54A6FD")" || return 1

  "$JQ_BIN" -n \
    --arg title "$title_md" \
    --arg src "$source_md" \
    --arg stat "$status_md" \
    --arg f_rem "$five_remaining" \
    --argjson chart_5h "$chart_5h" \
    --arg f_dur "$five_duration" \
    --arg f_res "$five_reset_time" \
    --arg w_rem "$weekly_remaining" \
    --argjson chart_weekly "$chart_weekly" \
    --arg w_dur "$weekly_duration" \
    --arg w_res "$weekly_reset_time" \
    '[
      { tag: "markdown", content: $title },
      { tag: "markdown", content: $src }
    ] +
    (if $stat != "" then [{ tag: "markdown", content: $stat }] else [] end) +
    [
      { tag: "markdown", content: ("**5 小时**　剩余 " + $f_rem + "%") },
      $chart_5h,
      {
        tag: "markdown",
        content: ("距离重置　" + $f_dur + "\n重置时间　" + $f_res)
      },
      { tag: "hr" },
      { tag: "markdown", content: ("**周额度**　剩余 " + $w_rem + "%") },
      $chart_weekly,
      {
        tag: "markdown",
        content: ("距离重置　" + $w_dur + "\n重置时间　" + $w_res)
      }
    ]'
}

build_feishu_v2_task_payload() {
  local user_id="$1"
  local request_uuid="$2"
  shift 2
  local attempted=("$@")
  local header_template="green"

  local p
  for p in "${attempted[@]}"; do
    case "$p" in
      codex)
        [[ "${CODEX_RUN_RESULT:-发送成功}" != "发送成功" ]] && header_template="red"
        ;;
      antigravity)
        [[ "${ANTIGRAVITY_RUN_RESULT:-发送成功}" != "发送成功" ]] && header_template="red"
        ;;
    esac
  done

  local body_elements_json="[]"

  if (( ${#attempted[@]} == 1 )); then
    local provider="${attempted[1]}"
    local provider_elements
    case "$provider" in
      codex)
        provider_elements="$(build_provider_v2_elements "GPT-5.6 Luna" "${CODEX_RUN_RESULT:-发送成功}" "$CODEX_QUOTA_NORMALIZED_FILE")" || return 1
        ;;
      antigravity)
        provider_elements="$(build_provider_v2_elements "Gemini 3.7 Flash · Low" "${ANTIGRAVITY_RUN_RESULT:-发送成功}" "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE")" || return 1
        ;;
    esac

    body_elements_json="$("$JQ_BIN" -n \
      --argjson elems "$provider_elements" \
      '$elems + [
        { tag: "hr" },
        { tag: "markdown", content: "<font color=\"grey\">Pi 自动任务 · 间隔至少 5 小时 01 分</font>" }
      ]')"
  elif (( ${#attempted[@]} >= 2 )); then
    local luna_elements gemini_elements
    luna_elements="$(build_provider_v2_elements "GPT-5.6 Luna" "${CODEX_RUN_RESULT:-发送成功}" "$CODEX_QUOTA_NORMALIZED_FILE")" || return 1
    gemini_elements="$(build_provider_v2_elements "Gemini 3.7 Flash · Low" "${ANTIGRAVITY_RUN_RESULT:-发送成功}" "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE")" || return 1

    body_elements_json="$("$JQ_BIN" -n \
      --argjson luna "$luna_elements" \
      --argjson gemini "$gemini_elements" \
      '[
        {
          tag: "column_set",
          flex_mode: "stretch",
          horizontal_spacing: "medium",
          columns: [
            {
              tag: "column",
              width: "weighted",
              weight: 1,
              vertical_align: "top",
              elements: $luna
            },
            {
              tag: "column",
              width: "weighted",
              weight: 1,
              vertical_align: "top",
              elements: $gemini
            }
          ]
        },
        { tag: "hr" },
        { tag: "markdown", content: "<font color=\"grey\">Pi 自动任务 · 间隔至少 5 小时 01 分</font>" }
      ]')"
  else
    return 1
  fi

  "$JQ_BIN" -n \
    --arg receive_id "$user_id" \
    --arg template "$header_template" \
    --arg uuid "$request_uuid" \
    --argjson elements "$body_elements_json" \
    '{
      receive_id: $receive_id,
      msg_type: "interactive",
      content: ({
        schema: "2.0",
        config: {
          width_mode: "default"
        },
        header: {
          template: $template,
          title: {
            tag: "plain_text",
            content: "AI 模型运行与配额"
          }
        },
        body: {
          direction: "vertical",
          elements: $elements
        }
      } | tostring),
      uuid: $uuid
    }'
}

build_feishu_v2_usage_payload() {
  local user_id="$1"
  local request_uuid="$2"
  local luna_elements gemini_elements

  luna_elements="$(build_provider_v2_elements "GPT-5.6 Luna" "" "$CODEX_QUOTA_NORMALIZED_FILE")" || return 1
  gemini_elements="$(build_provider_v2_elements "Gemini 3.7 Flash · Low" "" "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE")" || return 1

  local body_elements_json
  body_elements_json="$("$JQ_BIN" -n \
    --argjson luna "$luna_elements" \
    --argjson gemini "$gemini_elements" \
    '[
      {
        tag: "markdown",
        content: "**即时配额查询**　未执行模型任务"
      },
      {
        tag: "column_set",
        flex_mode: "stretch",
        horizontal_spacing: "medium",
        columns: [
          {
            tag: "column",
            width: "weighted",
            weight: 1,
            vertical_align: "top",
            elements: $luna
          },
          {
            tag: "column",
            width: "weighted",
            weight: 1,
            vertical_align: "top",
            elements: $gemini
          }
        ]
      },
      { tag: "hr" },
      { tag: "markdown", content: "<font color=\"grey\">Pi 自动任务 · 间隔至少 5 小时 01 分</font>" }
    ]')"

  "$JQ_BIN" -n \
    --arg receive_id "$user_id" \
    --arg uuid "$request_uuid" \
    --argjson elements "$body_elements_json" \
    '{
      receive_id: $receive_id,
      msg_type: "interactive",
      content: ({
        schema: "2.0",
        config: {
          width_mode: "default"
        },
        header: {
          template: "green",
          title: {
            tag: "plain_text",
            content: "AI 模型运行与配额"
          }
        },
        body: {
          direction: "vertical",
          elements: $elements
        }
      } | tostring),
      uuid: $uuid
    }'
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

prepare_provider_env() {
  local provider="$1"
  ensure_temp_dir
  case "$provider" in
    codex)
      CODEX_AGENT_DIR="$LAST_TEMP_DIR/codex-agent"
      CODEX_STDOUT_FILE="$LAST_TEMP_DIR/codex-stdout"
      CODEX_STDERR_FILE="$LAST_TEMP_DIR/codex-stderr"
      CODEX_QUOTA_FILE="$LAST_TEMP_DIR/codex-quota.json"
      mkdir -p "$CODEX_AGENT_DIR"
      "$PI_BIN" auth print-bearer-token --provider openai-codex \
        >/dev/null 2>"$CODEX_STDERR_FILE" || true
      cp -p "$PI_AUTH_FILE" "$CODEX_AGENT_DIR/auth.json"
      print -r -- '{"transport":"sse"}' >"$CODEX_AGENT_DIR/settings.json"
      ;;
    antigravity)
      ANTIGRAVITY_AGENT_DIR="$LAST_TEMP_DIR/antigravity-agent"
      ANTIGRAVITY_STDOUT_FILE="$LAST_TEMP_DIR/antigravity-stdout"
      ANTIGRAVITY_STDERR_FILE="$LAST_TEMP_DIR/antigravity-stderr"
      ANTIGRAVITY_QUOTA_FILE="$LAST_TEMP_DIR/antigravity-quota.json"
      mkdir -p "$ANTIGRAVITY_AGENT_DIR"
      cp -p "$PI_AUTH_FILE" "$ANTIGRAVITY_AGENT_DIR/auth.json"
      print -r -- '{}' >"$ANTIGRAVITY_AGENT_DIR/settings.json"
      ;;
  esac
}

prepare_run() {
  prepare_provider_env codex
  prepare_provider_env antigravity
}

provider_normalized_quota_file() {
  local provider="$1"
  case "$provider" in
    codex) print -r -- "$CODEX_QUOTA_NORMALIZED_FILE" ;;
    antigravity) print -r -- "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE" ;;
    *) die "Unknown provider: $provider" ;;
  esac
}

provider_quota_is_fresh() {
  local provider="$1" q_file
  q_file="$(provider_normalized_quota_file "$provider" 2>/dev/null || true)"
  if [[ -n "$q_file" && -s "$q_file" ]] && "$JQ_BIN" -e '.fresh == true' "$q_file" >/dev/null 2>&1; then
    return 0
  fi
  case "$provider" in
    codex) (( CODEX_QUOTA_IS_FRESH == 1 )) ;;
    antigravity) (( ANTIGRAVITY_QUOTA_IS_FRESH == 1 )) ;;
    *) return 1 ;;
  esac
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
    "来源：不可用" \
    "5 小时：□□□□□□□□□□ 获取失败" \
    "距离重置：未知" \
    "重置时间：未知" \
    "" \
    "周额度：□□□□□□□□□□ 获取失败" \
    "距离重置：未知" \
    "重置时间：未知"
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
    "来源：$source" \
    "5 小时：$(quota_bar "$five_remaining") 剩余 ${five_remaining}%" \
    "距离重置：$five_duration" \
    "重置时间：$(format_reset_time "$five_reset")" \
    "" \
    "周额度：$(quota_bar "$weekly_remaining") 剩余 ${weekly_remaining}%" \
    "距离重置：$weekly_duration" \
    "重置时间：$(format_reset_time "$weekly_reset")"
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
      source: "Pi 快照（可能不是最新）",
      fresh: false,
      cached: true,
      capturedAt: (.capturedAt // (now | floor)),
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
      source: "Pi 快照（可能不是最新）",
      fresh: false,
      cached: true,
      capturedAt: (.capturedAt // (now | floor)),
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
      source: ("CodexBar · " + ($row.source // "cli")),
      fresh: true,
      capturedAt: (now | floor),
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
      source: ("CodexBar · " + ($row.source // "cli")),
      fresh: true,
      capturedAt: (now | floor),
      fiveHour: {remainingPercent: remaining($five.usedPercent), resetAt: epoch($five.resetsAt)},
      weekly: {remainingPercent: remaining($weekly.usedPercent), resetAt: epoch($weekly.resetsAt)}
    }
  ' "$input" >"$output"
}

fetch_native_codex_quota() {
  local output="$1"
  [[ -x "$CODEX_BIN" ]] || return 1
  require_executable "$PYTHON3_BIN"
  "$PYTHON3_BIN" -c '
import json, subprocess, time, sys, os

def get_codex_native(codex_bin):
    if not os.path.exists(codex_bin) or not os.access(codex_bin, os.X_OK):
        return None
    try:
        proc = subprocess.Popen(
            [codex_bin, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        def send_and_wait(req_id, method, params):
            req = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}) + "\n"
            proc.stdin.write(req)
            proc.stdin.flush()
            t_end = time.time() + 4
            while time.time() < t_end:
                line = proc.stdout.readline()
                if not line: break
                try:
                    data = json.loads(line)
                    if data.get("id") == req_id: return data
                except Exception:
                    continue
            return None

        init_res = send_and_wait(1, "initialize", {"clientInfo": {"name": "quota-sentinel", "version": "1.0"}})
        if not init_res:
            proc.terminate(); proc.wait(); return None
        rate_res = send_and_wait(2, "account/rateLimits/read", {})
        proc.terminate(); proc.wait()
        if not rate_res: return None

        rl = rate_res.get("result", {}).get("rateLimits", {})
        p = rl.get("primary", {})
        s = rl.get("secondary", {})
        if not p or not s: return None

        five_h = p if p.get("windowDurationMins", 300) <= 360 else s
        weekly = s if s.get("windowDurationMins", 10080) > 360 else p

        now = int(time.time())
        return {
            "source": "Native · codex app-server",
            "fresh": True,
            "capturedAt": now,
            "fiveHour": {
                "remainingPercent": max(0, min(100, 100 - five_h.get("usedPercent", 0))),
                "resetAt": five_h.get("resetsAt")
            },
            "weekly": {
                "remainingPercent": max(0, min(100, 100 - weekly.get("usedPercent", 0))),
                "resetAt": weekly.get("resetsAt")
            }
        }
    except Exception:
        return None

res = get_codex_native(sys.argv[1])
if res and res.get("fiveHour", {}).get("resetAt") and res.get("weekly", {}).get("resetAt"):
    print(json.dumps(res))
    sys.exit(0)
sys.exit(1)
' "$CODEX_BIN" >"$output" 2>/dev/null || return 1
  [[ -s "$output" ]] || return 1
}

fetch_native_antigravity_quota() {
  local output="$1"
  require_executable "$PYTHON3_BIN"
  "$PYTHON3_BIN" -c '
import json, subprocess, time, ssl, urllib.request, urllib.parse, datetime, os, sys

ALLOWED_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "*", "0.0.0.0", "::"}

def validate_port(val):
    try:
        p = int(val)
        if 1 <= p <= 65535:
            return p
    except (ValueError, TypeError):
        pass
    return None

def extract_safe_port_from_listen_addr(addr_str):
    addr_str = str(addr_str).strip()
    if ":" not in addr_str:
        return None
    host_part = addr_str.rsplit(":", 1)[0].strip("[]")
    port_part = addr_str.rsplit(":", 1)[1]
    if host_part not in ALLOWED_LOCAL_HOSTS:
        return None
    return validate_port(port_part)

def build_loopback_endpoint(port):
    p = validate_port(port)
    if not p:
        return None
    url = f"https://127.0.0.1:{p}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "127.0.0.1" or parts.port != p:
        raise ValueError(f"Endpoint invariant violated: {url}")
    return url

def get_antigravity_native():
    ports = []
    agy_session_file = os.path.expanduser("~/.codexbar/antigravity/agy-session.json")
    if os.path.exists(agy_session_file):
        try:
            with open(agy_session_file, "r") as f:
                sess = json.load(f)
                pid = sess[0].get("pid")
                if pid and isinstance(pid, int) and pid > 0:
                    res = subprocess.run(["lsof", "-Pan", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"], capture_output=True, text=True)
                    for line in res.stdout.splitlines():
                        if "LISTEN" in line:
                            for part in line.split():
                                p = extract_safe_port_from_listen_addr(part)
                                if p:
                                    ports.append(p)
        except Exception:
            pass

    if not ports:
        res = subprocess.run(["lsof", "-iTCP", "-sTCP:LISTEN", "-P", "-n"], capture_output=True, text=True)
        for line in res.stdout.splitlines():
            if "agy" in line or "language_server" in line:
                for part in line.split():
                    p = extract_safe_port_from_listen_addr(part)
                    if p:
                        ports.append(p)

    ports = list(dict.fromkeys(ports))
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    data = None
    for port in ports:
        try:
            url = build_loopback_endpoint(port)
            if not url:
                continue
            req = urllib.request.Request(
                url,
                data=b"{}",
                headers={"Content-Type": "application/json", "Connect-Protocol-Version": "1"},
                method="POST"
            )
            with urllib.request.urlopen(req, context=ctx, timeout=1.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                break
        except Exception:
            continue

    if not data:
        return None

    gemini_group = None
    for g in data.get("response", {}).get("groups", []):
        if "gemini" in g.get("displayName", "").lower():
            gemini_group = g
            break
    if not gemini_group and data.get("response", {}).get("groups"):
        gemini_group = data["response"]["groups"][0]

    if not gemini_group:
        return None

    five_h_bucket = None
    weekly_bucket = None
    for b in gemini_group.get("buckets", []):
        if b.get("window") == "5h" or "5-hour" in b.get("displayName", "").lower() or "5h" in b.get("bucketId", "").lower():
            five_h_bucket = b
        elif b.get("window") == "weekly" or "weekly" in b.get("displayName", "").lower() or "weekly" in b.get("bucketId", "").lower():
            weekly_bucket = b

    if not five_h_bucket or not weekly_bucket:
        return None

    def iso_to_epoch(iso_str):
        if not iso_str: return None
        dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp())

    now = int(time.time())
    five_h_rem = round(five_h_bucket.get("remainingFraction", 0) * 100)
    weekly_rem = round(weekly_bucket.get("remainingFraction", 0) * 100)

    five_reset = iso_to_epoch(five_h_bucket.get("resetTime"))
    weekly_reset = iso_to_epoch(weekly_bucket.get("resetTime"))
    if not five_reset or not weekly_reset:
        return None

    return {
        "source": "Native · agy local service",
        "fresh": True,
        "capturedAt": now,
        "fiveHour": {
            "remainingPercent": five_h_rem,
            "resetAt": five_reset
        },
        "weekly": {
            "remainingPercent": weekly_rem,
            "resetAt": weekly_reset
        }
    }

res = get_antigravity_native()
if res and res.get("fiveHour", {}).get("resetAt") and res.get("weekly", {}).get("resetAt"):
    print(json.dumps(res))
    sys.exit(0)
sys.exit(1)
' >"$output" 2>/dev/null || return 1
  [[ -s "$output" ]] || return 1
}

save_codexbar_cache() {
  local live_normalized="$1" cache_file="$2"
  local temp_cache="${cache_file}.tmp.$$"
  [[ -s "$live_normalized" ]] || return 1
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  "$JQ_BIN" -e '
    . as $orig |
    {
      source: "CodexBar · cached（可能不是最新）",
      originalSource: ($orig.source // "CodexBar · cli"),
      fresh: false,
      cached: true,
      capturedAt: ($orig.capturedAt // (now | floor)),
      fiveHour: $orig.fiveHour,
      weekly: $orig.weekly
    }
  ' "$live_normalized" >"$temp_cache" 2>/dev/null || { rm -f "$temp_cache"; return 1; }
  chmod 600 "$temp_cache"
  mv -f "$temp_cache" "$cache_file"
}

fetch_codexbar_codex_quota() {
  local output="$1"
  [[ -x "$CODEXBAR_BIN" ]] || return 1
  if "$CODEXBAR_BIN" usage --provider codex --source cli --format json --json-only --no-color \
    >"$CODEXBAR_CODEX_RAW_FILE" 2>"$LAST_TEMP_DIR/codexbar-codex.stderr" &&
    normalize_codexbar_codex_quota "$CODEXBAR_CODEX_RAW_FILE" "$output"; then
    save_codexbar_cache "$output" "$CODEXBAR_CODEX_CACHE_FILE" || true
    return 0
  fi
  if "$CODEXBAR_BIN" usage --provider codex --source oauth --format json --json-only --no-color \
    >"$CODEXBAR_CODEX_RAW_FILE" 2>"$LAST_TEMP_DIR/codexbar-codex.stderr" &&
    normalize_codexbar_codex_quota "$CODEXBAR_CODEX_RAW_FILE" "$output"; then
    save_codexbar_cache "$output" "$CODEXBAR_CODEX_CACHE_FILE" || true
    return 0
  fi
  return 1
}

fetch_codexbar_antigravity_quota() {
  local output="$1"
  [[ -x "$CODEXBAR_BIN" ]] || return 1
  if "$CODEXBAR_BIN" usage --provider antigravity --source cli --format json --json-only --no-color \
    >"$CODEXBAR_ANTIGRAVITY_RAW_FILE" 2>"$LAST_TEMP_DIR/codexbar-antigravity.stderr" &&
    normalize_codexbar_antigravity_quota "$CODEXBAR_ANTIGRAVITY_RAW_FILE" "$output"; then
    save_codexbar_cache "$output" "$CODEXBAR_ANTIGRAVITY_CACHE_FILE" || true
    return 0
  fi
  return 1
}

use_codexbar_cached_codex() {
  local output="$1"
  [[ -s "$CODEXBAR_CODEX_CACHE_FILE" ]] || return 1
  "$JQ_BIN" -e '
    select(.fiveHour.resetAt != null and .weekly.resetAt != null) |
    . + {
      source: "CodexBar · cached（可能不是最新）",
      fresh: false,
      cached: true
    }
  ' "$CODEXBAR_CODEX_CACHE_FILE" >"$output" 2>/dev/null || return 1
  [[ -s "$output" ]] || return 1
}

use_codexbar_cached_antigravity() {
  local output="$1"
  [[ -s "$CODEXBAR_ANTIGRAVITY_CACHE_FILE" ]] || return 1
  "$JQ_BIN" -e '
    select(.fiveHour.resetAt != null and .weekly.resetAt != null) |
    . + {
      source: "CodexBar · cached（可能不是最新）",
      fresh: false,
      cached: true
    }
  ' "$CODEXBAR_ANTIGRAVITY_CACHE_FILE" >"$output" 2>/dev/null || return 1
  [[ -s "$output" ]] || return 1
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
  if normalize_pi_codex_quota "$CODEX_QUOTA_FILE" "$codex_normalized"; then
    atomic_copy "$codex_normalized" "$PI_CODEX_SNAPSHOT_FILE" || true
  fi
  if normalize_pi_antigravity_quota "$ANTIGRAVITY_QUOTA_FILE" "$antigravity_normalized"; then
    atomic_copy "$antigravity_normalized" "$PI_ANTIGRAVITY_SNAPSHOT_FILE" || true
  fi
}

use_pi_snapshot_codex() {
  local output="$1"
  local normalized="$LAST_TEMP_DIR/codex-pi-normalized.json"
  if normalize_pi_codex_quota "$CODEX_QUOTA_FILE" "$normalized"; then
    cp -p "$normalized" "$output"
    return 0
  fi
  [[ -s "$PI_CODEX_SNAPSHOT_FILE" ]] || return 1
  cp -p "$PI_CODEX_SNAPSHOT_FILE" "$output"
}

use_pi_snapshot_antigravity() {
  local output="$1"
  local normalized="$LAST_TEMP_DIR/antigravity-pi-normalized.json"
  if normalize_pi_antigravity_quota "$ANTIGRAVITY_QUOTA_FILE" "$normalized"; then
    cp -p "$normalized" "$output"
    return 0
  fi
  [[ -s "$PI_ANTIGRAVITY_SNAPSHOT_FILE" ]] || return 1
  cp -p "$PI_ANTIGRAVITY_SNAPSHOT_FILE" "$output"
}

use_pi_or_saved_codex_fallback() {
  use_pi_snapshot_codex "$CODEX_QUOTA_NORMALIZED_FILE"
}

use_pi_or_saved_antigravity_fallback() {
  use_pi_snapshot_antigravity "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE"
}

collect_effective_quotas() {
  prepare_quota_probe

  # Codex 4-tier hierarchy: Native -> CodexBar Live -> CodexBar Cache -> Pi Snapshot
  if fetch_native_codex_quota "$CODEX_QUOTA_NORMALIZED_FILE"; then
    CODEX_QUOTA_IS_FRESH=1
  elif fetch_codexbar_codex_quota "$CODEX_QUOTA_NORMALIZED_FILE"; then
    CODEX_QUOTA_IS_FRESH=1
  elif use_codexbar_cached_codex "$CODEX_QUOTA_NORMALIZED_FILE"; then
    CODEX_QUOTA_IS_FRESH=0
  elif use_pi_snapshot_codex "$CODEX_QUOTA_NORMALIZED_FILE"; then
    CODEX_QUOTA_IS_FRESH=0
  else
    CODEX_QUOTA_IS_FRESH=0
  fi

  # Antigravity 4-tier hierarchy: Native -> CodexBar Live -> CodexBar Cache -> Pi Snapshot
  if fetch_native_antigravity_quota "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE"; then
    ANTIGRAVITY_QUOTA_IS_FRESH=1
  elif fetch_codexbar_antigravity_quota "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE"; then
    ANTIGRAVITY_QUOTA_IS_FRESH=1
  elif use_codexbar_cached_antigravity "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE"; then
    ANTIGRAVITY_QUOTA_IS_FRESH=0
  elif use_pi_snapshot_antigravity "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE"; then
    ANTIGRAVITY_QUOTA_IS_FRESH=0
  else
    ANTIGRAVITY_QUOTA_IS_FRESH=0
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

format_provider_card_section() {
  local title="$1" result="$2" quota_message="$3"
  local formatted_result=""
  if [[ -n "$result" ]]; then
    if [[ "$result" == "发送成功" ]]; then
      formatted_result="🟢 **发送成功**"
    else
      formatted_result="🔴 **发送失败**"
    fi
  fi

  quota_message="${quota_message//5 小时：/**5 小时**　}"
  quota_message="${quota_message//周额度：/**周额度**　}"
  quota_message="${quota_message//距离重置：/距离重置　}"
  quota_message="${quota_message//重置时间：/重置时间　}"
  quota_message="${quota_message//来源：/来源　}"

  local lines=("${(@f)quota_message}")
  local out_lines=("$title")
  local quota_body=()
  local src=""

  for line in "${lines[@]}"; do
    if [[ "$line" == "来源"* ]]; then
      if [[ "$line" == *"cached"* ]]; then
        src=$'来源　CodexBar · cached\n⚠️ 可能不是最新'
      elif [[ "$line" == *"快照"* ]]; then
        src=$'来源　Pi 快照\n⚠️ 可能不是最新'
      else
        src="$line"
      fi
    else
      quota_body+=("$line")
    fi
  done

  if [[ -n "$src" ]]; then
    out_lines+=("$src")
  fi
  if [[ -n "$formatted_result" ]]; then
    out_lines+=("$formatted_result")
  fi
  out_lines+=("")
  out_lines+=("${quota_body[@]}")

  printf '%s\n' "${out_lines[@]}"
}

notification_message() {
  if (( $# == 4 )); then
    local codex_result="$1" codex_quota="$2" antigravity_result="$3" antigravity_quota="$4"
    local timestamp
    timestamp="$(TZ=Asia/Shanghai /bin/date '+%Y-%m-%d %H:%M:%S %Z')"

    local sec1 sec2
    sec1="$(format_provider_card_section "**GPT-5.6 Luna**" "$codex_result" "$codex_quota")"
    sec2="$(format_provider_card_section "**Gemini 3.7 Flash · Low**" "$antigravity_result" "$antigravity_quota")"
    printf '%s\n\n────────────\n\n%s\n\n%s\n%s\n' \
      "$sec1" "$sec2" "**图例**　■ 剩余　□ 已用" "🕒 $timestamp"
    return 0
  fi
  task_notification_message "$@"
}

task_notification_message() {
  local attempted=("$@")
  local sections=()
  local timestamp
  timestamp="$(TZ=Asia/Shanghai /bin/date '+%Y-%m-%d %H:%M:%S %Z')"

  for provider in "${attempted[@]}"; do
    case "$provider" in
      codex)
        sections+=("$(format_provider_card_section "**GPT-5.6 Luna**" "${CODEX_RUN_RESULT:-发送成功}" "$(codex_quota_message)")")
        ;;
      antigravity)
        sections+=("$(format_provider_card_section "**Gemini 3.7 Flash · Low**" "${ANTIGRAVITY_RUN_RESULT:-发送成功}" "$(antigravity_quota_message)")")
        ;;
    esac
  done

  local joined="" i
  for (( i = 1; i <= ${#sections[@]}; i++ )); do
    if (( i > 1 )); then
      joined+=$'\n\n────────────\n\n'
    fi
    joined+="${sections[$i]}"
  done

  printf '%s\n\n%s\n%s\n' \
    "$joined" \
    "**图例**　■ 剩余　□ 已用" \
    "🕒 $timestamp"
}

usage_notification_message() {
  local codex_quota="$1"
  local antigravity_quota="$2"
  local timestamp
  timestamp="$(TZ=Asia/Shanghai /bin/date '+%Y-%m-%d %H:%M:%S %Z')"

  local sec1 sec2
  sec1="$(format_provider_card_section "**GPT-5.6 Luna**" "" "$codex_quota")"
  sec2="$(format_provider_card_section "**Gemini 3.7 Flash · Low**" "" "$antigravity_quota")"

  printf '%s\n' \
    "**即时配额查询**　未执行模型任务" \
    "" \
    "$sec1" \
    "" \
    "────────────" \
    "" \
    "$sec2" \
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
  if [[ -x "$CODEX_BIN" ]] || [[ -x "$CODEXBAR_BIN" ]]; then
    print -r -- "quota primary: Native Direct (Codex app-server / Antigravity agy) · CodexBar fallback"
  else
    print -r -- "quota primary: unavailable; last Pi snapshots may be used"
  fi
  local codex_due antigravity_due
  codex_due="$(read_provider_next_due "codex" || true)"
  antigravity_due="$(read_provider_next_due "antigravity" || true)"

  if [[ "$codex_due" =~ ^[0-9]+$ ]]; then
    print -r -- "next codex run: $(format_reset_time "$codex_due")"
  else
    print -r -- "next codex run: due now"
  fi
  if [[ "$antigravity_due" =~ ^[0-9]+$ ]]; then
    print -r -- "next antigravity run: $(format_reset_time "$antigravity_due")"
  else
    print -r -- "next antigravity run: due now"
  fi
}

five_hour_reset_at() {
  "$JQ_BIN" -r '.fiveHour.resetAt // empty' "$1" 2>/dev/null
}

calibrate_provider_deadline() {
  local provider="$1" now quota_file reset_at
  now="$(/bin/date '+%s')"
  quota_file="$(provider_normalized_quota_file "$provider")"

  if provider_quota_is_fresh "$provider"; then
    reset_at="$(five_hour_reset_at "$quota_file")"
    if [[ "$reset_at" =~ ^[0-9]+$ ]] && (( reset_at > now && reset_at <= now + MAX_WINDOW_FUTURE_SECONDS )); then
      write_provider_last_known_reset "$provider" "$reset_at"
      write_provider_next_due "$provider" $(( reset_at + RUN_INTERVAL_SECONDS ))
      return 0
    fi
  fi
  return 1
}

evaluate_provider() {
  local provider="$1" now next_due
  now="$(/bin/date '+%s')"

  # 1. Calibrate fallback deadline with fresh valid reset data if available
  calibrate_provider_deadline "$provider" || true

  # 2. Check if reached next_due_at
  if next_due="$(read_provider_next_due "$provider")"; then
    if (( now >= next_due )); then
      return 0
    fi
  fi
  return 1
}

run_selected_providers() {
  local attempted=("$@")
  local provider pid codex_pid=0 antigravity_pid=0 now prev_due
  (( ${#attempted[@]} > 0 )) || return 0

  status >/dev/null
  ensure_temp_dir
  now="$(/bin/date '+%s')"

  for provider in "${attempted[@]}"; do
    prepare_provider_env "$provider"
    write_provider_last_task "$provider" "$now"
  done

  for provider in "${attempted[@]}"; do
    case "$provider" in
      codex)
        run_codex &
        codex_pid=$!
        ;;
      antigravity)
        run_antigravity &
        antigravity_pid=$!
        ;;
    esac
  done

  if (( codex_pid > 0 )); then
    if wait "$codex_pid"; then CODEX_RUN_RESULT="发送成功"; else CODEX_RUN_RESULT="发送失败"; fi
  fi
  if (( antigravity_pid > 0 )); then
    if wait "$antigravity_pid"; then ANTIGRAVITY_RUN_RESULT="发送成功"; else ANTIGRAVITY_RUN_RESULT="发送失败"; fi
  fi

  save_pi_quota_snapshots
  collect_effective_quotas

  for provider in "${attempted[@]}"; do
    local q_file reset_val
    q_file="$(provider_normalized_quota_file "$provider")"
    reset_val="$(five_hour_reset_at "$q_file")"
    if provider_quota_is_fresh "$provider" &&
       [[ "$reset_val" =~ ^[0-9]+$ ]] &&
       (( reset_val > now && reset_val <= now + MAX_WINDOW_FUTURE_SECONDS )); then
      write_provider_last_known_reset "$provider" "$reset_val"
      write_provider_last_window "$provider" "$reset_val"
      write_provider_next_due "$provider" $(( reset_val + RUN_INTERVAL_SECONDS ))
    else
      # If no fresh valid reset obtained post-run, advance fallback deadline by 5h01m
      prev_due="$(read_provider_next_due "$provider" || true)"
      if [[ "$prev_due" =~ ^[0-9]+$ ]] && (( prev_due > now - RUN_INTERVAL_SECONDS )); then
        write_provider_next_due "$provider" $(( prev_due + RUN_INTERVAL_SECONDS ))
      else
        write_provider_next_due "$provider" $(( now + RUN_INTERVAL_SECONDS ))
      fi
    fi
  done

  if [[ "${FEISHU_DISABLE_CHART:-0}" == "1" ]]; then
    dispatch_notification "$(task_notification_message "${attempted[@]}")"
  else
    local card_payload
    if card_payload="$(build_feishu_v2_task_payload "$(feishu_user_id 2>/dev/null || true)" "quota-sentinel-$(/bin/date '+%s')" "${attempted[@]}")" && [[ -n "$card_payload" ]]; then
      dispatch_notification "$card_payload"
    else
      dispatch_notification "$(task_notification_message "${attempted[@]}")"
    fi
  fi
}

run_once() {
  run_selected_providers codex antigravity
}

run_and_reschedule_selected() {
  local targets=("$@")
  (( ${#targets[@]} > 0 )) || targets=(codex antigravity)
  acquire_run_lock || die "Another model run is already in progress"
  run_selected_providers "${targets[@]}"
  release_run_lock
}

run_and_reschedule() {
  run_and_reschedule_selected "$@"
}

send_usage_notification() {
  local codex_quota antigravity_quota
  acquire_quota_lock || return 0
  prepare_quota_probe
  collect_effective_quotas
  codex_quota="$(codex_quota_message)" || true
  antigravity_quota="$(antigravity_quota_message)" || true

  if [[ "${FEISHU_DISABLE_CHART:-0}" == "1" ]]; then
    dispatch_notification "$(usage_notification_message "$codex_quota" "$antigravity_quota")"
  else
    local card_payload
    if card_payload="$(build_feishu_v2_usage_payload "$(feishu_user_id 2>/dev/null || true)" "quota-sentinel-$(/bin/date '+%s')")" && [[ -n "$card_payload" ]]; then
      dispatch_notification "$card_payload"
    else
      dispatch_notification "$(usage_notification_message "$codex_quota" "$antigravity_quota")"
    fi
  fi
  release_quota_lock
}

setup_mock_preview_quota() {
  ensure_temp_dir
  local now="$(/bin/date '+%s')"
  print -r -- '{"source":"Native · codex app-server","fresh":true,"capturedAt":'$now',"fiveHour":{"remainingPercent":100,"resetAt":'$(( now + 17880 ))'},"weekly":{"remainingPercent":84,"resetAt":'$(( now + 595800 ))'}}' >"$CODEX_QUOTA_NORMALIZED_FILE"
  print -r -- '{"source":"Native · agy local service","fresh":true,"capturedAt":'$now',"fiveHour":{"remainingPercent":77,"resetAt":'$(( now + 17700 ))'},"weekly":{"remainingPercent":86,"resetAt":'$(( now + 369660 ))'}}' >"$ANTIGRAVITY_QUOTA_NORMALIZED_FILE"
  CODEX_RUN_RESULT="发送成功"
  ANTIGRAVITY_RUN_RESULT="发送成功"
}

card_preview() {
  local mode="${1:-both}"
  setup_mock_preview_quota
  local payload
  case "$mode" in
    usage)
      payload="$(build_feishu_v2_usage_payload "mock-user-id" "preview-usage-$(/bin/date +%s)")"
      ;;
    single|codex)
      payload="$(build_feishu_v2_task_payload "mock-user-id" "preview-single-$(/bin/date +%s)" codex)"
      ;;
    antigravity)
      payload="$(build_feishu_v2_task_payload "mock-user-id" "preview-single-$(/bin/date +%s)" antigravity)"
      ;;
    both|all|auto|*)
      payload="$(build_feishu_v2_task_payload "mock-user-id" "preview-both-$(/bin/date +%s)" codex antigravity)"
      ;;
  esac
  print -r -- "$payload" | "$JQ_BIN" .
}

send_test_card() {
  local mode="${1:-both}"
  setup_mock_preview_quota
  local payload
  case "$mode" in
    usage)
      payload="$(build_feishu_v2_usage_payload "$(feishu_user_id)" "test-usage-$(/bin/date +%s)")"
      ;;
    single|codex)
      payload="$(build_feishu_v2_task_payload "$(feishu_user_id)" "test-single-$(/bin/date +%s)" codex)"
      ;;
    antigravity)
      payload="$(build_feishu_v2_task_payload "$(feishu_user_id)" "test-single-$(/bin/date +%s)" antigravity)"
      ;;
    both|all|auto|*)
      payload="$(build_feishu_v2_task_payload "$(feishu_user_id)" "test-both-$(/bin/date +%s)" codex antigravity)"
      ;;
  esac
  dispatch_notification "$payload"
  print -r -- "Test card sent successfully (mode: $mode)"
}

check_schedule() {
  local due_providers=()

  acquire_quota_lock || return 0
  prepare_quota_probe
  collect_effective_quotas

  if evaluate_provider "codex"; then
    due_providers+=(codex)
  fi
  if evaluate_provider "antigravity"; then
    due_providers+=(antigravity)
  fi
  release_quota_lock

  if (( ${#due_providers[@]} == 0 )); then
    return 0
  fi

  acquire_run_lock || return 0
  run_selected_providers "${due_providers[@]}"
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
      local target="${2:-all}"
      case "$target" in
        codex)
          run_and_reschedule_selected codex
          ;;
        antigravity)
          run_and_reschedule_selected antigravity
          ;;
        all|both|"")
          run_and_reschedule_selected codex antigravity
          ;;
        *)
          die "Unknown run target: $target (expected: codex, antigravity, or all)"
          ;;
      esac
      ;;
    usage)
      send_usage_notification
      ;;
    card-preview)
      card_preview "${2:-both}"
      ;;
    send-test-card)
      send_test_card "${2:-both}"
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
