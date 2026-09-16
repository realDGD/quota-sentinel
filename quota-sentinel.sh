#!/bin/zsh

set -euo pipefail

# Binary paths are env-overridable so the regression suite can exercise the
# real run/fetch paths against mock commands without editing this file.
readonly PI_BIN="${QUOTA_SENTINEL_PI_BIN:-/opt/homebrew/bin/pi}"
readonly CODEX_BIN="/opt/homebrew/bin/codex"
readonly AGY_BIN="${QUOTA_SENTINEL_AGY_BIN:-/opt/homebrew/bin/agy}"
readonly UV_BIN="${QUOTA_SENTINEL_UV_BIN:-/opt/homebrew/bin/uv}"
readonly CODEXBAR_BIN="${QUOTA_SENTINEL_CODEXBAR_BIN:-/opt/homebrew/bin/codexbar}"
readonly PYTHON3_BIN="/usr/bin/python3"
readonly CURL_BIN="/usr/bin/curl"
readonly JQ_BIN="/opt/homebrew/bin/jq"
readonly SECURITY_BIN="/usr/bin/security"
readonly SHLOCK_BIN="/usr/bin/shlock"
readonly SLEEP_BIN="/bin/sleep"
readonly PI_AUTH_FILE="${QUOTA_SENTINEL_PI_AUTH_FILE:-/Users/__USER__/.pi/agent/auth.json}"
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
readonly RUN_INTERVAL_SECONDS=18060              # 5 hours 01 minute
readonly RESET_BUFFER_SECONDS=240                # 4 minutes after reset
readonly RESET_NEAR_MOVEMENT_SECONDS=300         # immediate same-window jitter
readonly RESET_CONFIRM_MIN_AGE_SECONDS=60        # independent observation gap
readonly RESET_CONFIRM_MATCH_SECONDS=30          # stable timestamp tolerance
readonly MAX_WINDOW_FUTURE_SECONDS=21600         # 6 hours
readonly QUOTA_LOCK_WAIT_SECONDS=20
readonly TIMER_RECHECK_SECONDS=60
# Hard execution bounds: a hung model task or quota probe must never hold the
# run/quota locks forever. Include kill grace when budgeting /usage: lock 20s
# + Native Codex ~15s + CodexBar Codex 2×(20+10)s + Native agy ~21s
# + CodexBar agy (35+10)s ≈ 161s, before Feishu delivery. The listener's
# outer timeout is 360s, including Feishu auth/send retries (up to ~183s).
# Provider-specific query bounds do not change cadence.
readonly MODEL_TASK_TIMEOUT_SECONDS="${QUOTA_SENTINEL_MODEL_TIMEOUT:-300}"
readonly MODEL_TASK_KILL_GRACE_SECONDS="${QUOTA_SENTINEL_MODEL_KILL_GRACE:-10}"
readonly CODEXBAR_TIMEOUT_SECONDS="${QUOTA_SENTINEL_CODEXBAR_TIMEOUT:-20}"
readonly ANTIGRAVITY_CODEXBAR_TIMEOUT_SECONDS="${QUOTA_SENTINEL_ANTIGRAVITY_CODEXBAR_TIMEOUT:-35}"
readonly ANTIGRAVITY_NATIVE_TIMEOUT_SECONDS="${QUOTA_SENTINEL_ANTIGRAVITY_NATIVE_TIMEOUT:-20}"
readonly CODEXBAR_KILL_GRACE_SECONDS="${QUOTA_SENTINEL_CODEXBAR_KILL_GRACE:-10}"
# Retry policy: a model task only counts when it truly succeeds. A due task is
# marked retry_pending BEFORE its first attempt and repaid by bursts — the
# initial burst makes at most INITIAL_ATTEMPT_LIMIT total attempts, later
# watchdog bursts at most WATCHDOG_ATTEMPT_LIMIT total attempts, with a fixed
# interval between attempts. WATCHDOG_RETRY_GAP_SECONDS spaces watchdog bursts
# apart (780s < the 15-min launchd grid, so no grid point is ever skipped).
readonly RETRY_INTERVAL_SECONDS="${QUOTA_SENTINEL_RETRY_INTERVAL:-30}"
readonly INITIAL_ATTEMPT_LIMIT="${QUOTA_SENTINEL_INITIAL_ATTEMPTS:-3}"
readonly WATCHDOG_ATTEMPT_LIMIT="${QUOTA_SENTINEL_WATCHDOG_ATTEMPTS:-2}"
readonly WATCHDOG_RETRY_GAP_SECONDS="${QUOTA_SENTINEL_WATCHDOG_RETRY_GAP:-780}"
readonly RUN_WITH_TIMEOUT_HELPER="$SCRIPT_DIR/run_with_timeout.py"
readonly ANTIGRAVITY_USAGE_HELPER="$SCRIPT_DIR/antigravity_usage.py"
readonly LOG_DIR="${QUOTA_SENTINEL_LOG_DIR:-$SCRIPT_DIR/logs}"

typeset -g LAST_TEMP_DIR=""
typeset -gi MAIN_T0=0
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
typeset -gi RUN_LOCK_HELD=0
typeset -gi QUOTA_LOCK_HELD=0

usage() {
  print -r -- "Usage: $SCRIPT_NAME [check|wait|run [codex|antigravity|all]|usage|discover-feishu-user|status]"
}

die() {
  log_error "fatal: $*"
  print -u2 -r -- "Error: $*"
  exit 1
}

# Append-only run log with timing, one file per day under LOG_DIR. Logging is
# best-effort: it must never break the command it observes. Lines carry the
# PID so concurrent watchdog/timer/usage writers stay attributable. No tokens
# or secrets are ever passed to these functions.
log_line() {
  local level="$1" file
  shift
  file="$LOG_DIR/$(TZ=Asia/Shanghai /bin/date '+%Y-%m-%d').log"
  mkdir -p "$LOG_DIR" 2>/dev/null || return 0
  chmod 700 "$LOG_DIR" 2>/dev/null || true
  printf '[%s] [%s] [%d] %s\n' \
    "$(TZ=Asia/Shanghai /bin/date '+%Y-%m-%d %H:%M:%S')" "$level" "$$" "$*" \
    >>"$file" 2>/dev/null || return 0
  chmod 600 "$file" 2>/dev/null || true
}

log_info() { log_line INFO "$*"; }
log_warn() { log_line WARN "$*"; }
log_error() { log_line ERROR "$*"; }

# Durations use whole seconds via /bin/date; every timed operation logs
# "what / outcome / elapsed". now_epoch is a tiny readability helper.
now_epoch() { /bin/date '+%s'; }

# Retry-policy sanity: refuse configs that would never attempt or would spin.
[[ "$RETRY_INTERVAL_SECONDS" =~ ^[1-9][0-9]*$ ]] ||
  die "QUOTA_SENTINEL_RETRY_INTERVAL must be a positive integer"
[[ "$INITIAL_ATTEMPT_LIMIT" =~ ^[1-9][0-9]*$ ]] ||
  die "QUOTA_SENTINEL_INITIAL_ATTEMPTS must be a positive integer"
[[ "$WATCHDOG_ATTEMPT_LIMIT" =~ ^[1-9][0-9]*$ ]] ||
  die "QUOTA_SENTINEL_WATCHDOG_ATTEMPTS must be a positive integer"
[[ "$WATCHDOG_RETRY_GAP_SECONDS" =~ ^[0-9]+$ ]] ||
  die "QUOTA_SENTINEL_WATCHDOG_RETRY_GAP must be a non-negative integer"

cleanup() {
  release_run_lock
  release_quota_lock
  if (( MAIN_T0 > 0 )); then
    log_info "command: finished ($(( $(now_epoch) - MAIN_T0 ))s)"
  fi
  if [[ -n "$LAST_TEMP_DIR" ]] &&
    [[ "$LAST_TEMP_DIR" == /private/tmp/quota-sentinel.* ]] &&
    [[ -d "$LAST_TEMP_DIR" ]]; then
    rm -rf -- "$LAST_TEMP_DIR"
  fi
}

ensure_temp_dir() {
  if [[ -z "$LAST_TEMP_DIR" ]]; then
    LAST_TEMP_DIR="$(mktemp -d /private/tmp/quota-sentinel.XXXXXX)"
    # mktemp -d already yields 0700; make the invariant explicit because this
    # directory receives a copy of the Pi auth credential during model runs.
    chmod 700 "$LAST_TEMP_DIR"
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

provider_last_attempt_file() {
  local provider="$1"
  print -r -- "$STATE_DIR/${provider}-last-attempt-at"
}

provider_retry_pending_file() {
  local provider="$1"
  print -r -- "$STATE_DIR/${provider}-retry-pending"
}

provider_last_known_reset_file() {
  local provider="$1"
  print -r -- "$STATE_DIR/${provider}-last-known-reset-at"
}

provider_reset_candidate_file() {
  local provider="$1"
  print -r -- "$STATE_DIR/${provider}-reset-candidate"
}

provider_reset_anchor_file() {
  local provider="$1"
  print -r -- "$STATE_DIR/${provider}-reset-anchor"
}

provider_last_window_file() {
  local provider="$1"
  print -r -- "$STATE_DIR/${provider}-last-triggered-window"
}

migrate_legacy_state() {
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"

  # Migrate last-task-at. Writes go through the shared atomic writer so a
  # crash mid-migration can never leave a truncated state file behind.
  if [[ -r "$STATE_DIR/last-task-at" ]]; then
    local legacy_task
    legacy_task="$(<"$STATE_DIR/last-task-at")"
    if [[ "$legacy_task" =~ ^[0-9]+$ ]]; then
      if [[ ! -r "$STATE_DIR/codex-last-task-at" ]]; then
        atomic_write_state_file "$STATE_DIR/codex-last-task-at" "$legacy_task"
      fi
      if [[ ! -r "$STATE_DIR/antigravity-last-task-at" ]]; then
        atomic_write_state_file "$STATE_DIR/antigravity-last-task-at" "$legacy_task"
      fi
    fi
  fi

  # Migrate next-due-at
  if [[ -r "$STATE_DIR/next-due-at" ]]; then
    local legacy_due
    legacy_due="$(<"$STATE_DIR/next-due-at")"
    if [[ "$legacy_due" =~ ^[0-9]+$ ]]; then
      if [[ ! -r "$STATE_DIR/codex-next-due-at" ]]; then
        atomic_write_state_file "$STATE_DIR/codex-next-due-at" "$legacy_due"
      fi
      if [[ ! -r "$STATE_DIR/antigravity-next-due-at" ]]; then
        atomic_write_state_file "$STATE_DIR/antigravity-next-due-at" "$legacy_due"
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
        atomic_write_state_file "$STATE_DIR/codex-last-triggered-window" "$c_win"
      fi
      if [[ -n "$a_win" && ! -r "$STATE_DIR/antigravity-last-triggered-window" ]]; then
        atomic_write_state_file "$STATE_DIR/antigravity-last-triggered-window" "$a_win"
      fi
      if [[ -n "$c_win" && ! -r "$STATE_DIR/codex-last-known-reset-at" ]]; then
        atomic_write_state_file "$STATE_DIR/codex-last-known-reset-at" "$c_win"
      fi
      if [[ -n "$a_win" && ! -r "$STATE_DIR/antigravity-last-known-reset-at" ]]; then
        atomic_write_state_file "$STATE_DIR/antigravity-last-known-reset-at" "$a_win"
      fi
    fi
  fi

  # Seed last-attempt-at for upgrades: the pre-retry-era last_task_at recorded
  # the most recent attempt, so it is the best conservative initial value.
  # retry_pending deliberately starts at 0 (missing file) — there is no
  # reliable evidence of an outstanding debt from before this version.
  local m_provider m_task
  for m_provider in codex antigravity; do
    if [[ ! -r "$STATE_DIR/${m_provider}-last-attempt-at" ]] &&
      [[ -r "$STATE_DIR/${m_provider}-last-task-at" ]]; then
      m_task="$(<"$STATE_DIR/${m_provider}-last-task-at")"
      if [[ "$m_task" =~ ^[0-9]+$ ]]; then
        atomic_write_state_file "$STATE_DIR/${m_provider}-last-attempt-at" "$m_task"
      fi
    fi
  done
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

# Single atomic writer for scheduler state: readers must only ever observe the
# complete old or complete new value. Every state write funnels through here.
atomic_write_state_file() {
  local file="$1" value="$2" temp_file
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  temp_file="${file}.tmp.$$"
  print -r -- "$value" >"$temp_file"
  chmod 600 "$temp_file"
  mv -f "$temp_file" "$file"
}

write_provider_next_due() {
  local provider="$1" epoch="$2" file old=""
  [[ "$epoch" =~ ^[0-9]+$ ]] || die "Invalid next-run timestamp: $epoch"
  file="$(provider_next_due_file "$provider")"
  old="$(read_provider_next_due "$provider" 2>/dev/null || true)"
  atomic_write_state_file "$file" "$epoch"
  if [[ "$old" != "$epoch" ]]; then
    log_info "state: $provider next_due_at ${old:-<unset>} -> $epoch"
  fi
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
  local provider="$1" epoch="$2" file
  [[ "$epoch" =~ ^[0-9]+$ ]] || die "Invalid last-task timestamp: $epoch"
  file="$(provider_last_task_file "$provider")"
  atomic_write_state_file "$file" "$epoch"
}

read_provider_last_attempt() {
  local provider="$1" file value
  migrate_legacy_state
  file="$(provider_last_attempt_file "$provider")"
  [[ -r "$file" ]] || return 1
  value="$(<"$file")"
  [[ "$value" =~ ^[0-9]+$ ]] || return 1
  print -r -- "$value"
}

write_provider_last_attempt() {
  local provider="$1" epoch="$2" file old=""
  [[ "$epoch" =~ ^[0-9]+$ ]] || die "Invalid last-attempt timestamp: $epoch"
  file="$(provider_last_attempt_file "$provider")"
  old="$(read_provider_last_attempt "$provider" 2>/dev/null || true)"
  atomic_write_state_file "$file" "$epoch"
  if [[ "$old" != "$epoch" ]]; then
    log_info "state: $provider last_attempt_at ${old:-<unset>} -> $epoch"
  fi
}

# retry_pending=1 means "this provider owes one due-but-unsucceeded task".
# A missing file reads as 0, so the state only exists once a debt is recorded.
read_provider_retry_pending() {
  local provider="$1" file value
  file="$(provider_retry_pending_file "$provider")"
  [[ -r "$file" ]] || return 1
  value="$(<"$file")"
  [[ "$value" == "1" ]] || return 1
  print -r -- "1"
}

write_provider_retry_pending() {
  local provider="$1" value="$2" file old="0"
  [[ "$value" == "0" || "$value" == "1" ]] || die "Invalid retry-pending value: $value"
  file="$(provider_retry_pending_file "$provider")"
  if [[ "$(read_provider_retry_pending "$provider" 2>/dev/null || true)" == "1" ]]; then
    old="1"
  fi
  atomic_write_state_file "$file" "$value"
  if [[ "$old" != "$value" ]]; then
    log_info "state: $provider retry_pending $old -> $value"
  fi
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
  local provider="$1" epoch="$2" file
  [[ "$epoch" =~ ^[0-9]+$ ]] || die "Invalid last-known-reset timestamp: $epoch"
  file="$(provider_last_known_reset_file "$provider")"
  atomic_write_state_file "$file" "$epoch"
}

# A far-later reset must survive a separate observation before it may replace
# the current trusted reset. Candidate and observation time share one atomic
# file so a crash can only lose a promotion opportunity, never create one.
read_provider_reset_candidate() {
  local provider="$1" file value reset observed
  file="$(provider_reset_candidate_file "$provider")"
  [[ -r "$file" ]] || return 1
  value="$(<"$file")"
  reset="${value%%:*}"
  observed="${value##*:}"
  [[ "$reset" =~ ^[0-9]+$ && "$observed" =~ ^[0-9]+$ ]] || return 1
  print -r -- "$reset:$observed"
}

write_provider_reset_candidate() {
  local provider="$1" reset="$2" observed="$3" file
  [[ "$reset" =~ ^[0-9]+$ ]] || die "Invalid reset candidate timestamp: $reset"
  [[ "$observed" =~ ^[0-9]+$ ]] || die "Invalid reset candidate observation: $observed"
  file="$(provider_reset_candidate_file "$provider")"
  atomic_write_state_file "$file" "$reset:$observed"
}

clear_provider_reset_candidate() {
  local provider="$1" file
  file="$(provider_reset_candidate_file "$provider")"
  rm -f -- "$file"
}

# The anchor is the independently trusted reset for this provider generation.
# Unlike last-known-reset-at it does not follow accepted near-window jitter, so
# many individually small later movements cannot accumulate into starvation.
read_provider_reset_anchor() {
  local provider="$1" file value
  file="$(provider_reset_anchor_file "$provider")"
  [[ -r "$file" ]] || return 1
  value="$(<"$file")"
  [[ "$value" =~ ^[0-9]+$ ]] || return 1
  print -r -- "$value"
}

write_provider_reset_anchor() {
  local provider="$1" reset="$2" file
  [[ "$reset" =~ ^[0-9]+$ ]] || die "Invalid reset anchor timestamp: $reset"
  file="$(provider_reset_anchor_file "$provider")"
  atomic_write_state_file "$file" "$reset"
}

clear_provider_reset_anchor() {
  local provider="$1" file
  file="$(provider_reset_anchor_file "$provider")"
  rm -f -- "$file"
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
  local provider="$1" window_id="$2" file
  [[ -n "$window_id" ]] || return 1
  file="$(provider_last_window_file "$provider")"
  atomic_write_state_file "$file" "$window_id"
}

read_next_due() {
  local c_due a_due min_due=""
  # Providers with an unpaid debt (retry_pending=1) are deliberately excluded:
  # their stale past deadline would spin the precision timer once per second.
  # Debt repayment is driven by the watchdog retry phase instead.
  if ! provider_is_pending codex; then
    c_due="$(read_provider_next_due "codex" || true)"
  fi
  if ! provider_is_pending antigravity; then
    a_due="$(read_provider_next_due "antigravity" || true)"
  fi

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

acquire_quota_lock_with_timeout() {
  local deadline now
  deadline=$(( $(/bin/date '+%s') + QUOTA_LOCK_WAIT_SECONDS ))
  while ! acquire_quota_lock; do
    now="$(/bin/date '+%s')"
    (( now < deadline )) || return 1
    "$SLEEP_BIN" 1
  done
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
            {tag: "plain_text", content: "Pi 自动任务 · Fresh 重置后 4 分钟 · 无数据时 5 小时 01 分兜底"}
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
  local token payload response t0

  if [[ "${FEISHU_DRY_RUN:-0}" == "1" ]]; then
    log_info "notify: dry-run (${#message_or_payload} bytes)"
    print -r -- "$message_or_payload"
    return 0
  fi

  t0="$(now_epoch)"
  token="$(feishu_tenant_token "$app_id" "$app_secret")" || { log_error "notify: token request failed"; return 1; }

  if [[ "$message_or_payload" =~ '^[[:space:]]*\{' ]] && print -r -- "$message_or_payload" | "$JQ_BIN" -e '.receive_id and .msg_type' >/dev/null 2>&1; then
    payload="$message_or_payload"
  else
    payload="$(feishu_message_payload "$user_id" "$message_or_payload" "quota-sentinel-$(/bin/date '+%s')")"
  fi

  if ! response="$(feishu_api "$token" "im/v1/messages?receive_id_type=user_id" \
    --data-binary "$payload")"; then
    log_error "notify: send failed ($(( $(now_epoch) - t0 ))s)"
    print -u2 -r -- "Feishu message request failed: $(feishu_error_detail "$response")"
    return 1
  fi

  if ! print -r -- "$response" | "$JQ_BIN" -e '.code == 0' >/dev/null; then
    log_error "notify: rejected ($(( $(now_epoch) - t0 ))s)"
    print -u2 -r -- "Feishu rejected the message: $(feishu_error_detail "$response")"
    return 1
  fi
  log_info "notify: sent ($(( $(now_epoch) - t0 ))s, ${#payload} bytes)"
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

  # Verified against the real Feishu client: only the top-level VChart
  # `cornerRadius` drives the rounded ends of both the progress bar and the
  # track. `roundCap` is a polar (arc-mark) option and per-mark
  # progress.style.cornerRadius / track.style.cornerRadius are ignored by
  # linearProgress rendering.
  "$JQ_BIN" -n \
    --argjson val "$val" \
    --arg color "$color_hex" \
    '{
      tag: "chart",
      aspect_ratio: "16:9",
      height: "24px",
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
        cornerRadius: 5,
        color: [$color],
        progress: {
          style: {
            fill: $color
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

# Visual regression card: renders the production linear progress spec at
# 0% / 1% / 50% / 77% / 99% / 100% so endpoint rendering can be compared on a
# real client (all non-zero bars must show rounded ends on both sides).
build_progress_test_card_payload() {
  local user_id="$1"
  local request_uuid="$2"

  local elements
  elements="$(print -r -- '[]' | "$JQ_BIN" \
    --arg intro1 "每列从上到下依次为 0% / 1% / 50% / 77% / 99% / 100%。" \
    --arg intro2 "验收目标：所有非 0% 进度条两端圆角一致，track 同样两端圆角。" \
    '. + [{ tag: "markdown", content: $intro1 }, { tag: "markdown", content: $intro2 }]')" || return 1
  local percent chart
  for percent in 0 1 50 77 99 100; do
    chart="$(build_linear_progress_chart "$percent" "#57D0FB")" || return 1
    elements="$(print -r -- "$elements" | "$JQ_BIN" \
      --arg caption "${percent}%" \
      --argjson chart "$chart" \
      '. + [{ tag: "markdown", content: $caption }, $chart]')" || return 1
  done

  "$JQ_BIN" -n \
    --arg receive_id "$user_id" \
    --arg uuid "$request_uuid" \
    --argjson elements "$elements" \
    '{
      receive_id: $receive_id,
      msg_type: "interactive",
      content: ({
        schema: "2.0",
        config: {
          width_mode: "default"
        },
        header: {
          template: "blue",
          title: {
            tag: "plain_text",
            content: "Linear Progress 圆角验收"
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

build_provider_v2_elements() {
  local title="$1" result="$2" quota_file="$3" layout_mode="${4:-dual}"
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
    source_md="CodexBar · cached\n⚠️ 可能不是最新"
  elif [[ "$raw_source" == *"快照"* ]]; then
    source_md="Pi 快照\n⚠️ 可能不是最新"
  else
    source_md="${raw_source}"
  fi

  local status_md=""
  if [[ -n "$result" ]]; then
    if [[ "$result" == "发送成功" ]]; then
      status_md="🟢 **成功**"
    else
      status_md="🔴 **失败**"
    fi
  fi

  local chart_5h chart_weekly
  chart_5h="$(build_linear_progress_chart "$five_remaining" "#57D0FB")" || return 1
  chart_weekly="$(build_linear_progress_chart "$weekly_remaining" "#54A6FD")" || return 1

  if [[ "$layout_mode" == "single" ]]; then
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
      '(if $stat != "" then [
        {
          tag: "column_set",
          flex_mode: "none",
          columns: [
            {
              tag: "column",
              width: "weighted",
              weight: 3,
              vertical_align: "center",
              elements: [{ tag: "markdown", content: $title }]
            },
            {
              tag: "column",
              width: "weighted",
              weight: 1,
              vertical_align: "center",
              elements: [{ tag: "markdown", content: $stat }]
            }
          ]
        }
      ] else [
        { tag: "markdown", content: $title }
      ] end) +
      [
        { tag: "markdown", content: $src },
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
              elements: [
                { tag: "markdown", content: ("**5 小时**　剩余 " + $f_rem + "%") },
                $chart_5h,
                {
                  tag: "markdown",
                  content: ("距离重置　" + $f_dur + "\n重置时间　" + $f_res)
                },
                { tag: "hr" }
              ]
            },
            {
              tag: "column",
              width: "weighted",
              weight: 1,
              vertical_align: "top",
              elements: [
                { tag: "markdown", content: ("**周额度**　剩余 " + $w_rem + "%") },
                $chart_weekly,
                {
                  tag: "markdown",
                  content: ("距离重置　" + $w_dur + "\n重置时间　" + $w_res)
                },
                { tag: "hr" }
              ]
            }
          ]
        }
      ]'
  else
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
      '(if $stat != "" then [
        {
          tag: "column_set",
          flex_mode: "none",
          columns: [
            {
              tag: "column",
              width: "weighted",
              weight: 3,
              vertical_align: "center",
              elements: [{ tag: "markdown", content: $title }]
            },
            {
              tag: "column",
              width: "weighted",
              weight: 1,
              vertical_align: "center",
              elements: [{ tag: "markdown", content: $stat }]
            }
          ]
        }
      ] else [
        { tag: "markdown", content: $title }
      ] end) +
      [
        { tag: "markdown", content: $src },
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
        },
        { tag: "hr" }
      ]'
  fi
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
        provider_elements="$(build_provider_v2_elements "GPT-5.6 Luna" "${CODEX_RUN_RESULT:-发送成功}" "$CODEX_QUOTA_NORMALIZED_FILE" "single")" || return 1
        ;;
      antigravity)
        provider_elements="$(build_provider_v2_elements "Gemini 3.7 Flash · Low" "${ANTIGRAVITY_RUN_RESULT:-发送成功}" "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE" "single")" || return 1
        ;;
    esac

    body_elements_json="$("$JQ_BIN" -n \
      --argjson elems "$provider_elements" \
      '$elems + [
        { tag: "markdown", content: "<font color=\"grey\">Pi 自动任务 · Fresh 重置后 4 分钟 · 无数据时 5 小时 01 分兜底</font>" }
      ]')"
  elif (( ${#attempted[@]} >= 2 )); then
    local luna_elements gemini_elements
    luna_elements="$(build_provider_v2_elements "GPT-5.6 Luna" "${CODEX_RUN_RESULT:-发送成功}" "$CODEX_QUOTA_NORMALIZED_FILE" "dual")" || return 1
    gemini_elements="$(build_provider_v2_elements "Gemini 3.7 Flash · Low" "${ANTIGRAVITY_RUN_RESULT:-发送成功}" "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE" "dual")" || return 1

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
        { tag: "markdown", content: "<font color=\"grey\">Pi 自动任务 · Fresh 重置后 4 分钟 · 无数据时 5 小时 01 分兜底</font>" }
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

  luna_elements="$(build_provider_v2_elements "GPT-5.6 Luna" "" "$CODEX_QUOTA_NORMALIZED_FILE" "dual")" || return 1
  gemini_elements="$(build_provider_v2_elements "Gemini 3.7 Flash · Low" "" "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE" "dual")" || return 1

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
      { tag: "markdown", content: "<font color=\"grey\">Pi 自动任务 · Fresh 重置后 4 分钟 · 无数据时 5 小时 01 分兜底</font>" }
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

# Masked, truncated tail of a provider's stderr for the run log. The full
# stderr stays in its 0600 temp file; the log only ever sees a redacted
# summary (no tokens, no Authorization headers, no auth.json contents).
safe_error_summary() {
  local file="$1" summary
  [[ -s "$file" ]] || { print -r -- "(no stderr output)"; return 0; }
  summary="$(tail -n 3 "$file" 2>/dev/null | tr '\n' ' ' | tr -s ' ' | cut -c1-300)"
  summary="$(print -r -- "$summary" |
    perl -pe 's/(token|secret|authorization|bearer|password|api[_-]?key|sk-)\S*(\s+\S+)?/\1***/gi' 2>/dev/null)"
  [[ -n "$summary" ]] || summary="(empty stderr)"
  print -r -- "$summary"
}

run_codex() {
  local phase="${1:-initial}" attempt="${2:-1}" limit="${3:-1}"
  local output exit_code=0 t0 elapsed
  t0="$(now_epoch)"
  (
    cd /private/tmp
    PI_CODING_AGENT_DIR="$CODEX_AGENT_DIR" \
    PI_CODEX_QUOTA_FILE="$CODEX_QUOTA_FILE" \
    PI_OFFLINE=1 \
    "$PYTHON3_BIN" "$RUN_WITH_TIMEOUT_HELPER" \
      --timeout "$MODEL_TASK_TIMEOUT_SECONDS" \
      --kill-grace "$MODEL_TASK_KILL_GRACE_SECONDS" \
      -- \
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

  elapsed=$(( $(now_epoch) - t0 ))
  output="$(<"$CODEX_STDOUT_FILE")"
  if (( exit_code == 0 )) && [[ "$output" == "1" ]]; then
    log_info "model codex phase=$phase attempt=$attempt/$limit result=success elapsed=${elapsed}s"
    return 0
  fi
  if (( exit_code == 124 )); then
    log_warn "model codex phase=$phase attempt=$attempt/$limit result=timeout rc=124 elapsed=${elapsed}s"
  else
    log_warn "model codex phase=$phase attempt=$attempt/$limit result=failed rc=$exit_code elapsed=${elapsed}s"
  fi
  log_warn "model codex attempt=$attempt error: $(safe_error_summary "$CODEX_STDERR_FILE")"
  return 1
}

run_antigravity() {
  local phase="${1:-initial}" attempt="${2:-1}" limit="${3:-1}"
  local output exit_code=0 t0 elapsed
  t0="$(now_epoch)"
  (
    cd /private/tmp
    PI_CODING_AGENT_DIR="$ANTIGRAVITY_AGENT_DIR" \
    PI_ANTIGRAVITY_QUOTA_FILE="$ANTIGRAVITY_QUOTA_FILE" \
    PI_OFFLINE=1 \
    ANTIGRAVITY_NO_PREWARM=1 \
    "$PYTHON3_BIN" "$RUN_WITH_TIMEOUT_HELPER" \
      --timeout "$MODEL_TASK_TIMEOUT_SECONDS" \
      --kill-grace "$MODEL_TASK_KILL_GRACE_SECONDS" \
      -- \
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

  elapsed=$(( $(now_epoch) - t0 ))
  output="$(<"$ANTIGRAVITY_STDOUT_FILE")"
  if (( exit_code == 0 )) && [[ "$output" == "1" ]]; then
    log_info "model antigravity phase=$phase attempt=$attempt/$limit result=success elapsed=${elapsed}s"
    return 0
  fi
  if (( exit_code == 124 )); then
    log_warn "model antigravity phase=$phase attempt=$attempt/$limit result=timeout rc=124 elapsed=${elapsed}s"
  else
    log_warn "model antigravity phase=$phase attempt=$attempt/$limit result=failed rc=$exit_code elapsed=${elapsed}s"
  fi
  log_warn "model antigravity attempt=$attempt error: $(safe_error_summary "$ANTIGRAVITY_STDERR_FILE")"
  return 1
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

# Shared capturedAt convention (kept inline per call site to avoid quoting
# hazards): normalized producers pass capturedAt through raw; the read-side
# renormalise_quota_file is the single boundary that converts it to an
# epoch-integer/null before anything enters effective quota. Invalid and
# missing values become null there — never "now" — and the conversion is
# idempotent.

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
      capturedAt: (.capturedAt // null),
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
      capturedAt: (.capturedAt // null),
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
import json, os, selectors, subprocess, sys, time

def get_codex_native(codex_bin):
    if not os.path.exists(codex_bin) or not os.access(codex_bin, os.X_OK):
        return None
    proc = None
    try:
        proc = subprocess.Popen(
            [codex_bin, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        pending = b""

        def send_and_wait(req_id, method, params):
            nonlocal pending
            req = (json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}) + "\n").encode()
            proc.stdin.write(req)
            proc.stdin.flush()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    try:
                        data = json.loads(line)
                        if data.get("id") == req_id:
                            return data
                    except Exception:
                        pass
                events = selector.select(max(0, deadline - time.monotonic()))
                if not events:
                    break
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                pending += chunk
            return None

        init_res = send_and_wait(1, "initialize", {"clientInfo": {"name": "quota-sentinel", "version": "1.0"}})
        if not init_res:
            return None
        rate_res = send_and_wait(2, "account/rateLimits/read", {})
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
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)

res = get_codex_native(sys.argv[1])
if res and res.get("fiveHour", {}).get("resetAt") and res.get("weekly", {}).get("resetAt"):
    print(json.dumps(res))
    sys.exit(0)
sys.exit(1)
' "$CODEX_BIN" >"$output" 2>/dev/null || return 1
  [[ -s "$output" ]] || return 1
}

fetch_native_antigravity_quota() {
  local output="$1" reason=""
  [[ -x "$AGY_BIN" && -x "$UV_BIN" ]] || return 1
  # agy >= 1.1.11 owns this built-in metadata command. Older local HTTPS
  # endpoints now reject tokenless probes; never send /usage as an LLM prompt.
  if "$UV_BIN" run --offline --no-project --no-config python -B "$ANTIGRAVITY_USAGE_HELPER" \
      --agy "$AGY_BIN" --timeout "$ANTIGRAVITY_NATIVE_TIMEOUT_SECONDS" \
      >"$output" 2>"${output}.stderr"; then
    [[ -s "$output" ]] || return 1
    return 0
  fi
  # Only the helper's fixed reason code may enter the run log, not raw CLI
  # stderr, OAuth credentials, CSRF tokens or the textual quota response.
  reason="$(/usr/bin/sed -n 's/^antigravity_usage: \([a-z_]*\)$/\1/p' "${output}.stderr")"
  log_warn "quota antigravity: native /usage failed (${reason:-runtime_unavailable})"
  return 1
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

# CodexBar runs under the shared process-group timeout: a hung `codexbar
# usage` (it may spawn its own codex/agy children) must not stall quota
# collection while the quota lock is held. rc=124 means the timeout fired.
fetch_codexbar_codex_quota() {
  local output="$1" rc=0
  [[ -x "$CODEXBAR_BIN" ]] || return 1
  if "$PYTHON3_BIN" "$RUN_WITH_TIMEOUT_HELPER" \
      --timeout "$CODEXBAR_TIMEOUT_SECONDS" \
      --kill-grace "$CODEXBAR_KILL_GRACE_SECONDS" \
      -- \
      "$CODEXBAR_BIN" usage --provider codex --source cli --format json --json-only --no-color \
      >"$CODEXBAR_CODEX_RAW_FILE" 2>"$LAST_TEMP_DIR/codexbar-codex.stderr"; then
    if normalize_codexbar_codex_quota "$CODEXBAR_CODEX_RAW_FILE" "$output"; then
      save_codexbar_cache "$output" "$CODEXBAR_CODEX_CACHE_FILE" || true
      return 0
    fi
  else
    rc=$?
    (( rc == 124 )) && log_warn "quota codex: codexbar-live TIMEOUT after ${CODEXBAR_TIMEOUT_SECONDS}s (source cli)"
  fi
  if "$PYTHON3_BIN" "$RUN_WITH_TIMEOUT_HELPER" \
      --timeout "$CODEXBAR_TIMEOUT_SECONDS" \
      --kill-grace "$CODEXBAR_KILL_GRACE_SECONDS" \
      -- \
      "$CODEXBAR_BIN" usage --provider codex --source oauth --format json --json-only --no-color \
      >"$CODEXBAR_CODEX_RAW_FILE" 2>"$LAST_TEMP_DIR/codexbar-codex.stderr"; then
    if normalize_codexbar_codex_quota "$CODEXBAR_CODEX_RAW_FILE" "$output"; then
      save_codexbar_cache "$output" "$CODEXBAR_CODEX_CACHE_FILE" || true
      return 0
    fi
  else
    rc=$?
    (( rc == 124 )) && log_warn "quota codex: codexbar-live TIMEOUT after ${CODEXBAR_TIMEOUT_SECONDS}s (source oauth)"
  fi
  return 1
}

fetch_codexbar_antigravity_quota() {
  local output="$1" rc=0
  [[ -x "$CODEXBAR_BIN" ]] || return 1
  if "$PYTHON3_BIN" "$RUN_WITH_TIMEOUT_HELPER" \
      --timeout "$ANTIGRAVITY_CODEXBAR_TIMEOUT_SECONDS" \
      --kill-grace "$CODEXBAR_KILL_GRACE_SECONDS" \
      -- \
      "$CODEXBAR_BIN" usage --provider antigravity --source cli --format json --json-only --no-color \
      >"$CODEXBAR_ANTIGRAVITY_RAW_FILE" 2>"$LAST_TEMP_DIR/codexbar-antigravity.stderr"; then
    if normalize_codexbar_antigravity_quota "$CODEXBAR_ANTIGRAVITY_RAW_FILE" "$output"; then
      save_codexbar_cache "$output" "$CODEXBAR_ANTIGRAVITY_CACHE_FILE" || true
      return 0
    fi
  else
    rc=$?
    (( rc == 124 )) && log_warn "quota antigravity: codexbar-live TIMEOUT after ${ANTIGRAVITY_CODEXBAR_TIMEOUT_SECONDS}s"
  fi
  return 1
}

use_codexbar_cached_codex() {
  local output="$1"
  [[ -s "$CODEXBAR_CODEX_CACHE_FILE" ]] || return 1
  local staged="$output.staged"
  renormalise_quota_file "$CODEXBAR_CODEX_CACHE_FILE" "$staged" || return 1
  "$JQ_BIN" -e '
    . + {
      source: "CodexBar · cached（可能不是最新）",
      fresh: false,
      cached: true
    }
  ' "$staged" >"$output" 2>/dev/null || { rm -f "$staged"; return 1; }
  rm -f "$staged"
  [[ -s "$output" ]] || return 1
}

use_codexbar_cached_antigravity() {
  local output="$1"
  [[ -s "$CODEXBAR_ANTIGRAVITY_CACHE_FILE" ]] || return 1
  local staged="$output.staged"
  renormalise_quota_file "$CODEXBAR_ANTIGRAVITY_CACHE_FILE" "$staged" || return 1
  "$JQ_BIN" -e '
    . + {
      source: "CodexBar · cached（可能不是最新）",
      fresh: false,
      cached: true
    }
  ' "$staged" >"$output" 2>/dev/null || { rm -f "$staged"; return 1; }
  rm -f "$staged"
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

# Read a stale quota file from disk through the normalized-schema boundary:
# validates the shape and rewrites capturedAt to the epoch-integer/null
# contract, so historical files cannot leak ISO strings into effective quota.
# This is the SINGLE home of the epoch_ts definition; every read path that
# feeds effective quota goes through here. Accepts epoch integers, ISO-8601
# with optional fractional seconds, "Z" or numeric timezone offsets; invalid
# and missing values become null — never "now". Idempotent.
renormalise_quota_file() {
  local input="$1" output="$2"
  [[ -s "$input" ]] || return 1
  "$JQ_BIN" -e '
    def epoch_ts:
      if type == "number" then floor
      elif type == "string" then
        (try
          (if test("[+-][0-9]{2}:?[0-9]{2}$") then
            capture("^(?<d>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\\.[0-9]+)?(?<sign>[+-])(?<hh>[0-9]{2}):?(?<mm>[0-9]{2})$") as $m |
            (($m.d + "Z") | fromdateiso8601) -
              (if $m.sign == "-" then -1 else 1 end) * (($m.hh | tonumber) * 3600 + (($m.mm | tonumber) * 60))
          else (sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601) end)
         catch null)
      else null end;
    select(.fiveHour.resetAt != null and .weekly.resetAt != null) |
    . + {capturedAt: ((.capturedAt // null) | epoch_ts)}
  ' "$input" >"$output" 2>/dev/null || return 1
  [[ -s "$output" ]] || return 1
}

use_pi_snapshot_codex() {
  local output="$1"
  local normalized="$LAST_TEMP_DIR/codex-pi-normalized.json"
  if normalize_pi_codex_quota "$CODEX_QUOTA_FILE" "$normalized" &&
    renormalise_quota_file "$normalized" "$output"; then
    return 0
  fi
  renormalise_quota_file "$PI_CODEX_SNAPSHOT_FILE" "$output"
}

use_pi_snapshot_antigravity() {
  local output="$1"
  local normalized="$LAST_TEMP_DIR/antigravity-pi-normalized.json"
  if normalize_pi_antigravity_quota "$ANTIGRAVITY_QUOTA_FILE" "$normalized" &&
    renormalise_quota_file "$normalized" "$output"; then
    return 0
  fi
  renormalise_quota_file "$PI_ANTIGRAVITY_SNAPSHOT_FILE" "$output"
}

use_pi_or_saved_codex_fallback() {
  use_pi_snapshot_codex "$CODEX_QUOTA_NORMALIZED_FILE"
}

use_pi_or_saved_antigravity_fallback() {
  use_pi_snapshot_antigravity "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE"
}

quota_tier() {
  local provider="$1" tier="$2" t0 rc=0 elapsed
  shift 2
  t0="$(now_epoch)"
  "$@" || rc=$?
  elapsed=$(( $(now_epoch) - t0 ))
  if (( rc == 0 )); then
    log_info "quota $provider: $tier ok (${elapsed}s)"
  elif (( rc == 124 )); then
    log_warn "quota $provider: $tier TIMEOUT (${elapsed}s)"
  else
    log_warn "quota $provider: $tier fail rc=$rc (${elapsed}s)"
  fi
  return $rc
}

collect_effective_quotas() {
  prepare_quota_probe

  # Codex 4-tier hierarchy: Native -> CodexBar Live -> CodexBar Cache -> Pi Snapshot
  if quota_tier codex native fetch_native_codex_quota "$CODEX_QUOTA_NORMALIZED_FILE"; then
    CODEX_QUOTA_IS_FRESH=1
  elif quota_tier codex codexbar-live fetch_codexbar_codex_quota "$CODEX_QUOTA_NORMALIZED_FILE"; then
    CODEX_QUOTA_IS_FRESH=1
  elif quota_tier codex codexbar-cache use_codexbar_cached_codex "$CODEX_QUOTA_NORMALIZED_FILE"; then
    CODEX_QUOTA_IS_FRESH=0
  elif quota_tier codex pi-snapshot use_pi_snapshot_codex "$CODEX_QUOTA_NORMALIZED_FILE"; then
    CODEX_QUOTA_IS_FRESH=0
  else
    CODEX_QUOTA_IS_FRESH=0
    log_error "quota codex: all tiers unavailable"
  fi

  # Antigravity 4-tier hierarchy: Native -> CodexBar Live -> CodexBar Cache -> Pi Snapshot
  if quota_tier antigravity native fetch_native_antigravity_quota "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE"; then
    ANTIGRAVITY_QUOTA_IS_FRESH=1
  elif quota_tier antigravity codexbar-live fetch_codexbar_antigravity_quota "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE"; then
    ANTIGRAVITY_QUOTA_IS_FRESH=1
  elif quota_tier antigravity codexbar-cache use_codexbar_cached_antigravity "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE"; then
    ANTIGRAVITY_QUOTA_IS_FRESH=0
  elif quota_tier antigravity pi-snapshot use_pi_snapshot_antigravity "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE"; then
    ANTIGRAVITY_QUOTA_IS_FRESH=0
  else
    ANTIGRAVITY_QUOTA_IS_FRESH=0
    log_error "quota antigravity: all tiers unavailable"
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
      formatted_result="🟢 **成功**"
    else
      formatted_result="🔴 **失败**"
    fi
  fi

  quota_message="${quota_message//5 小时：/**5 小时**　}"
  quota_message="${quota_message//周额度：/**周额度**　}"
  quota_message="${quota_message//距离重置：/距离重置　}"
  quota_message="${quota_message//重置时间：/重置时间　}"

  local lines=("${(@f)quota_message}")
  local out_lines=()
  if [[ -n "$formatted_result" ]]; then
    out_lines+=("${title}    ${formatted_result}")
  else
    out_lines+=("$title")
  fi

  local quota_body=()
  local src=""

  for line in "${lines[@]}"; do
    if [[ "$line" == "来源："* || "$line" == "来源　"* || "$line" == "来源"* ]]; then
      local raw_src="${line#来源[：　]}"
      raw_src="${raw_src#来源}"
      if [[ "$raw_src" == *"cached"* ]]; then
        src=$'CodexBar · cached\n⚠️ 可能不是最新'
      elif [[ "$raw_src" == *"快照"* ]]; then
        src=$'Pi 快照\n⚠️ 可能不是最新'
      else
        src="$raw_src"
      fi
    else
      quota_body+=("$line")
    fi
  done

  if [[ -n "$src" ]]; then
    out_lines+=("$src")
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

validate_run_requirements() {
  local providers=("$@") provider
  (( ${#providers[@]} > 0 )) || providers=(codex antigravity)
  require_executable "$PI_BIN"
  require_executable "$CURL_BIN"
  require_executable "$JQ_BIN"
  require_executable "$SECURITY_BIN"
  require_executable "$SHLOCK_BIN"
  require_executable "$SLEEP_BIN"
  [[ -r "$PI_AUTH_FILE" ]] || die "Pi OAuth credential is not readable: $PI_AUTH_FILE"
  for provider in "${providers[@]}"; do
    case "$provider" in
      codex)
        "$JQ_BIN" -e 'has("openai-codex")' "$PI_AUTH_FILE" >/dev/null ||
          die "Pi credential for Codex is missing"
        [[ -r "$CODEX_QUOTA_EXTENSION" ]] || die "Codex quota hook is not readable"
        ;;
      antigravity)
        "$JQ_BIN" -e 'has("antigravity")' "$PI_AUTH_FILE" >/dev/null ||
          die "Pi credential for Antigravity is missing"
        [[ -r "$ANTIGRAVITY_QUOTA_EXTENSION" ]] || die "Antigravity quota hook is not readable"
        [[ -r "$ANTIGRAVITY_PROVIDER_EXTENSION" ]] || die "Antigravity provider extension is not readable"
        ;;
      *) die "Unknown provider: $provider" ;;
    esac
  done
  feishu_ready || die "Feishu enterprise-app credentials are not configured"
}

status() {
  validate_run_requirements codex antigravity
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

provider_fallback_due() {
  local provider="$1" now="${2:-$(/bin/date '+%s')}" last_task
  last_task="$(read_provider_last_task "$provider" || true)"
  if [[ "$last_task" =~ ^[0-9]+$ ]]; then
    print -r -- $(( last_task + RUN_INTERVAL_SECONDS ))
  else
    print -r -- "$now"
  fi
}

# A trusted reset from before the most recent successful task belongs to the
# previous generation. A reset still in the future after that success remains
# authoritative (for example when a task was manually run before the window
# reset), so last_task alone is never used as a hard reset ceiling.
provider_current_trusted_reset() {
  local provider="$1" trusted_reset last_task
  trusted_reset="$(read_provider_last_known_reset "$provider" 2>/dev/null || true)"
  [[ "$trusted_reset" =~ ^[0-9]+$ ]] || return 1
  last_task="$(read_provider_last_task "$provider" 2>/dev/null || true)"
  if [[ "$last_task" =~ ^[0-9]+$ ]] && (( trusted_reset <= last_task )); then
    return 1
  fi
  print -r -- "$trusted_reset"
}

valid_provider_reset_at() {
  local provider="$1" now="${2:-$(/bin/date '+%s')}" quota_file reset_at
  quota_file="$(provider_normalized_quota_file "$provider")"
  provider_quota_is_fresh "$provider" || return 1
  reset_at="$(five_hour_reset_at "$quota_file")"
  [[ "$reset_at" =~ ^[0-9]+$ ]] || return 1
  (( reset_at > now && reset_at <= now + MAX_WINDOW_FUTURE_SECONDS )) || return 1
  print -r -- "$reset_at"
}

# Report why a provider's scheduler is temporarily write-blocked. A reset-based
# deadline becomes committed as soon as its reset occurs, not only after the
# four-minute buffer matures. Pending/overdue task debt remains blocked until a
# verified success writes a new fallback deadline.
provider_schedule_block_reason() {
  local provider="$1" now="${2:-$(/bin/date '+%s')}"
  local pending next_due scheduled_reset

  pending="$(read_provider_retry_pending "$provider" 2>/dev/null || true)"
  if [[ "$pending" == "1" ]]; then
    print -r -- "retry-pending"
    return 0
  fi

  next_due="$(read_provider_next_due "$provider" 2>/dev/null || true)"
  [[ "$next_due" =~ ^[0-9]+$ ]] || return 1
  if (( now >= next_due )); then
    print -r -- "overdue"
    return 0
  fi

  scheduled_reset="$(read_provider_last_known_reset "$provider" 2>/dev/null || true)"
  if [[ "$scheduled_reset" =~ ^[0-9]+$ ]] &&
     (( next_due == scheduled_reset + RESET_BUFFER_SECONDS && now >= scheduled_reset )); then
    print -r -- "reset-buffer"
    return 0
  fi

  return 1
}

# Synchronize the scheduler only from live Native/CodexBar data. Before the
# scheduled reset, every valid fresh observation may replace the deadline with
# reset + four minutes. From that reset until successful execution, scheduler
# writes are blocked; /usage may still display its newly fetched quota.
sync_provider_deadline_from_quota() {
  local provider="$1" now="${2:-$(/bin/date '+%s')}"
  local reset_at reset_due block_reason existing_reset existing_due trusted_reset anchor_reset
  local candidate_record candidate_reset candidate_observed candidate_delta confirmed_reset

  reset_at="$(valid_provider_reset_at "$provider" "$now")" || return 1
  reset_due=$(( reset_at + RESET_BUFFER_SECONDS ))

  block_reason="$(provider_schedule_block_reason "$provider" "$now" 2>/dev/null || true)"
  if [[ -n "$block_reason" ]]; then
    existing_reset="$(read_provider_last_known_reset "$provider" 2>/dev/null || true)"
    existing_due="$(read_provider_next_due "$provider" 2>/dev/null || true)"
    log_info "sched $provider: sync blocked reason=$block_reason reset=$existing_reset due=$existing_due now=$now; fresh candidate reset=$reset_at deferred until success"
    return 2
  fi

  trusted_reset="$(provider_current_trusted_reset "$provider" 2>/dev/null || true)"
  if [[ ! "$trusted_reset" =~ ^[0-9]+$ ]]; then
    # The first Fresh reset of a generation establishes a finite anchor. It may
    # legitimately be much later than last_task+5h, as observed live when a
    # provider window remained fixed after a manual early task.
    clear_provider_reset_candidate "$provider"
    clear_provider_reset_anchor "$provider"
    write_provider_last_known_reset "$provider" "$reset_at"
    write_provider_next_due "$provider" "$reset_due"
    write_provider_reset_anchor "$provider" "$reset_at"
    log_info "sched $provider: fresh reset established generation anchor reset=$reset_at due=$reset_due"
    return 0
  fi

  # Upgrade an in-flight generation without changing its current deadline.
  # Persisting the existing trusted reset (not the new observation) is what
  # closes the cumulative-near-movement loophole on the very first probe after
  # deployment.
  anchor_reset="$(read_provider_reset_anchor "$provider" 2>/dev/null || true)"
  if [[ ! "$anchor_reset" =~ ^[0-9]+$ ]]; then
    anchor_reset="$trusted_reset"
    write_provider_reset_anchor "$provider" "$anchor_reset"
    log_info "sched $provider: reset anchor initialized from trusted reset=$anchor_reset"
  fi

  # Earlier resets and movement near the generation anchor preserve the
  # original dynamic policy. The anchor deliberately does not move here:
  # otherwise repeated +N-second observations could each remain under the
  # tolerance while cumulatively pushing the deadline forever.
  if (( reset_at <= anchor_reset + RESET_NEAR_MOVEMENT_SECONDS )); then
    clear_provider_reset_candidate "$provider"
    write_provider_last_known_reset "$provider" "$reset_at"
    write_provider_next_due "$provider" "$reset_due"
    return 0
  fi

  candidate_record="$(read_provider_reset_candidate "$provider" 2>/dev/null || true)"
  if [[ "$candidate_record" == *:* ]]; then
    candidate_reset="${candidate_record%%:*}"
    candidate_observed="${candidate_record##*:}"
    candidate_delta=$(( reset_at - candidate_reset ))
    (( candidate_delta < 0 )) && candidate_delta=$(( -candidate_delta ))
    if (( candidate_delta <= RESET_CONFIRM_MATCH_SECONDS &&
          now - candidate_observed >= RESET_CONFIRM_MIN_AGE_SECONDS )); then
      # Use the later of the two stable observations so the four-minute safety
      # buffer is never shortened by small timestamp jitter.
      confirmed_reset="$candidate_reset"
      (( reset_at > confirmed_reset )) && confirmed_reset="$reset_at"
      clear_provider_reset_candidate "$provider"
      write_provider_last_known_reset "$provider" "$confirmed_reset"
      write_provider_next_due "$provider" $(( confirmed_reset + RESET_BUFFER_SECONDS ))
      write_provider_reset_anchor "$provider" "$confirmed_reset"
      log_info "sched $provider: far reset promoted after stable observations old_reset=$trusted_reset new_reset=$confirmed_reset first_seen=$candidate_observed confirmed_at=$now"
      return 0
    fi
    if (( candidate_delta <= RESET_CONFIRM_MATCH_SECONDS )); then
      log_info "sched $provider: far reset awaiting independent confirmation trusted_reset=$trusted_reset candidate=$candidate_reset observed_at=$candidate_observed now=$now"
      return 3
    fi
  fi

  write_provider_reset_candidate "$provider" "$reset_at" "$now"
  existing_reset="$(read_provider_last_known_reset "$provider" 2>/dev/null || true)"
  existing_due="$(read_provider_next_due "$provider" 2>/dev/null || true)"
  log_warn "sched $provider: far-later fresh reset deferred trusted_reset=$trusted_reset candidate=$reset_at observed_at=$now; preserved reset=$existing_reset due=$existing_due"
  return 3
}

evaluate_provider() {
  local provider="$1" now="${2:-$(/bin/date '+%s')}" next_due fallback_due fresh_candidate

  # A matured deadline is a committed debt: this cycle's fresh quota may not
  # cancel it (P1-1 starvation, reproduced live 2026-08-30 18:11 — a probe at
  # due time re-anchored the window and pushed 18:11:35 to 23:15:38). Decide
  # BEFORE any fresh sync and leave next_due_at untouched on this path.
  next_due="$(read_provider_next_due "$provider" 2>/dev/null || true)"
  if [[ "$next_due" =~ ^[0-9]+$ ]] && (( now >= next_due )); then
    fresh_candidate="$(five_hour_reset_at "$(provider_normalized_quota_file "$provider")" 2>/dev/null || true)"
    if [[ "$fresh_candidate" =~ ^[0-9]+$ ]]; then
      log_info "sched $provider: matured debt due=$next_due; fresh candidate reset=$fresh_candidate ignored this round"
    else
      log_info "sched $provider: matured debt due=$next_due; no valid fresh data"
    fi
    return 0
  fi

  # Not due: Fresh may recalibrate earlier or later only before the scheduled
  # reset. The shared sync layer blocks writes during its four-minute buffer.
  # Stale cache/snapshots never write it.
  sync_provider_deadline_from_quota "$provider" "$now" || true

  # With no usable deadline, seed the no-quota fallback from the last real task.
  # An existing deadline is preserved exactly when the current probe is stale.
  next_due="$(read_provider_next_due "$provider" 2>/dev/null || true)"
  if [[ ! "$next_due" =~ ^[0-9]+$ ]]; then
    fallback_due="$(provider_fallback_due "$provider" "$now")"
    next_due="$fallback_due"
    write_provider_next_due "$provider" "$next_due"
  fi

  (( now >= next_due ))
}

# The single success-commit path: only a verified model success may run this.
# It is the ONLY production caller of write_provider_last_task.
provider_final_result() {
  case "$1" in
    codex) print -r -- "${CODEX_RUN_RESULT:-}" ;;
    antigravity) print -r -- "${ANTIGRAVITY_RUN_RESULT:-}" ;;
  esac
}

commit_provider_success() {
  local provider="$1" success_at="$2"
  write_provider_last_attempt "$provider" "$success_at"
  write_provider_last_task "$provider" "$success_at"
  write_provider_retry_pending "$provider" 0
  clear_provider_reset_candidate "$provider"
  clear_provider_reset_anchor "$provider"
  write_provider_next_due "$provider" $(( success_at + RUN_INTERVAL_SECONDS ))
  case "$provider" in
    codex) CODEX_RUN_RESULT="发送成功" ;;
    antigravity) ANTIGRAVITY_RUN_RESULT="发送成功" ;;
  esac
  log_info "success commit $provider: last_task=$success_at fallback_due=$(( success_at + RUN_INTERVAL_SECONDS )) retry_pending=0"
}

# One retry burst for the given providers: parallel rounds, fixed interval
# between rounds, stop as soon as every provider has succeeded. Requires the
# run lock. Each provider is marked retry_pending=1 BEFORE its first attempt
# so a crash mid-burst still records the debt. Returns 0 only when every
# provider succeeded; failures leave retry_pending=1 and scheduler state
# (last_task_at / next_due_at) completely untouched.
run_retry_burst() {
  local phase="$1" limit="$2"
  shift 2
  local attempted=("$@")
  (( ${#attempted[@]} > 0 )) || return 0
  local provider round widx
  local remain=("${attempted[@]}") pids=() round_providers=() still=()

  for provider in "${attempted[@]}"; do
    # Prepare the agent env here so BOTH entry points (initial burst via
    # run_selected_providers and watchdog repayment via check_schedule) get
    # valid stdout/stderr targets before the first attempt launches.
    prepare_provider_env "$provider"
    write_provider_retry_pending "$provider" 1
    case "$provider" in
      codex) CODEX_RUN_RESULT="发送失败" ;;
      antigravity) ANTIGRAVITY_RUN_RESULT="发送失败" ;;
    esac
  done

  for (( round = 1; ${#remain[@]} > 0 && round <= limit; round++ )); do
    round_providers=("${remain[@]}")
    pids=()
    for provider in "${round_providers[@]}"; do
      write_provider_last_attempt "$provider" "$(now_epoch)"
      log_info "run: attempt $provider phase=$phase round=$round/$limit"
      case "$provider" in
        codex) run_codex "$phase" "$round" "$limit" & pids+=($!) ;;
        antigravity) run_antigravity "$phase" "$round" "$limit" & pids+=($!) ;;
      esac
    done

    still=()
    widx=1
    for provider in "${round_providers[@]}"; do
      if wait "${pids[$widx]}"; then
        commit_provider_success "$provider" "$(now_epoch)"
      else
        still+=("$provider")
      fi
      (( widx += 1 ))
    done
    remain=("${still[@]}")

    if (( ${#remain[@]} > 0 && round < limit )); then
      log_info "retry $phase: ${remain[*]} failed round $round/$limit; retrying in ${RETRY_INTERVAL_SECONDS}s"
      "$SLEEP_BIN" "$RETRY_INTERVAL_SECONDS"
    fi
  done

  if (( ${#remain[@]} > 0 )); then
    log_warn "retry $phase: ${remain[*]} exhausted $limit attempts; retry_pending stays 1"
    return 1
  fi
  return 0
}

dispatch_task_notification() {
  local attempted=("$@")
  (( ${#attempted[@]} > 0 )) || return 0
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

run_selected_providers() {
  local attempted=("$@")
  local provider reset_val probe_now
  (( ${#attempted[@]} > 0 )) || return 0

  validate_run_requirements "${attempted[@]}"
  ensure_temp_dir

  # || true: exhaustion is expected and leaves the debt pending; per-provider
  # outcomes are read from the *_RUN_RESULT globals below.
  run_retry_burst "initial" "$INITIAL_ATTEMPT_LIMIT" "${attempted[@]}" || true

  save_pi_quota_snapshots
  if acquire_quota_lock_with_timeout; then
    collect_effective_quotas
    probe_now="$(/bin/date '+%s')"
    for provider in "${attempted[@]}"; do
      if [[ "$(provider_final_result "$provider")" == "发送成功" ]]; then
        reset_val="$(valid_provider_reset_at "$provider" "$probe_now" || true)"
        if [[ "$reset_val" =~ ^[0-9]+$ ]]; then
          write_provider_last_window "$provider" "$reset_val"
          sync_provider_deadline_from_quota "$provider" "$probe_now"
          log_info "run: post-run sync $provider fresh_reset=$reset_val due=$(( reset_val + RESET_BUFFER_SECONDS ))"
        else
          log_info "run: post-run sync $provider no valid fresh reset (fallback stands)"
        fi
      else
        # Failure never re-seeds the 5h01 fallback and never advances
        # last_task_at: the debt stays retry_pending=1 for the watchdog.
        log_info "run: $provider still pending after $INITIAL_ATTEMPT_LIMIT attempts; scheduler state untouched"
      fi
    done
    release_quota_lock
  else
    log_warn "run: post-run quota.lock busy after ${QUOTA_LOCK_WAIT_SECONDS}s; fallback deadline stands"
  fi

  dispatch_task_notification "${attempted[@]}"
}

run_and_reschedule_selected() {
  local targets=("$@")
  (( ${#targets[@]} > 0 )) || targets=(codex antigravity)
  acquire_run_lock || die "Another model run is already in progress"
  run_selected_providers "${targets[@]}"
  release_run_lock
}

usage_busy_message() {
  # Deliberately vague: no lock names, PIDs, or timeout internals reach the
  # user. The busy path runs zero probes and never touches scheduler state.
  feishu_message_payload "$(feishu_user_id)" \
    "⏳ 配额正在刷新，请稍后再试" \
    "quota-sentinel-busy-$(now_epoch)"
}

send_usage_notification() {
  local codex_quota antigravity_quota notification t0
  t0="$(now_epoch)"
  log_info "usage: requested"

  if ! acquire_quota_lock_with_timeout; then
    log_warn "usage: quota busy after ${QUOTA_LOCK_WAIT_SECONDS}s; replying busy"
    dispatch_notification "$(usage_busy_message)"
    log_info "usage: busy reply delivered ($(( $(now_epoch) - t0 ))s)"
    return 0
  fi

  prepare_quota_probe
  collect_effective_quotas

  # /usage never runs a model and never touches last-task/last-window, but its
  # already-fetched live quota is authoritative enough to refresh deadlines.
  sync_provider_deadline_from_quota codex || true
  sync_provider_deadline_from_quota antigravity || true

  codex_quota="$(codex_quota_message)" || true
  antigravity_quota="$(antigravity_quota_message)" || true

  if [[ "${FEISHU_DISABLE_CHART:-0}" == "1" ]]; then
    notification="$(usage_notification_message "$codex_quota" "$antigravity_quota")"
  else
    local card_payload
    if card_payload="$(build_feishu_v2_usage_payload "$(feishu_user_id 2>/dev/null || true)" "quota-sentinel-$(/bin/date '+%s')")" && [[ -n "$card_payload" ]]; then
      notification="$card_payload"
    else
      notification="$(usage_notification_message "$codex_quota" "$antigravity_quota")"
    fi
  fi
  release_quota_lock
  dispatch_notification "$notification"
  log_info "usage: completed ($(( $(now_epoch) - t0 ))s)"
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
    progress)
      payload="$(build_progress_test_card_payload "$(feishu_user_id)" "test-progress-$(/bin/date +%s)")"
      ;;
    both|all|auto|*)
      payload="$(build_feishu_v2_task_payload "$(feishu_user_id)" "test-both-$(/bin/date +%s)" codex antigravity)"
      ;;
  esac
  dispatch_notification "$payload"
  print -r -- "Test card sent successfully (mode: $mode)"
}

provider_is_pending() {
  [[ "$(read_provider_retry_pending "$1" 2>/dev/null || true)" == "1" ]]
}

# A pending debt is repaid by watchdog bursts, spaced at least
# WATCHDOG_RETRY_GAP_SECONDS apart (measured from the last real attempt) so
# the launchd 15-min grid and the precision timer never double-burst.
provider_retry_due() {
  provider_is_pending "$1" || return 1
  local last_attempt
  last_attempt="$(read_provider_last_attempt "$1" 2>/dev/null || true)"
  [[ "$last_attempt" =~ ^[0-9]+$ ]] || return 0
  (( $(now_epoch) - last_attempt >= WATCHDOG_RETRY_GAP_SECONDS ))
}

check_schedule() {
  local due_providers=() retry_pending_list=() recovered=()
  local t0 elapsed c_due a_due p

  # Serialize the due decision and the subsequent model run. Without this,
  # watchdog and precision-timer processes can both decide from the same stale
  # deadline and run the provider twice after the first lock holder exits.
  t0="$(now_epoch)"
  acquire_run_lock || { log_info "check: run.lock busy, skipped"; return 0; }

  # Phase A — repay pending debts first: a fresh probe must never push a
  # due-but-unsucceeded task into the future. No quota lock is held here, so
  # 30s retry sleeps and model timeouts never block /usage.
  for p in codex antigravity; do
    provider_retry_due "$p" && retry_pending_list+=("$p")
  done
  if (( ${#retry_pending_list[@]} > 0 )); then
    log_info "check: pending debt on ${retry_pending_list[*]}; watchdog retry burst first"
    # || true: burst exhaustion (rc=1) is an expected outcome — the debt
    # simply stays pending. The outcome is read via the retry-pending state.
    run_retry_burst "watchdog-retry" "$WATCHDOG_ATTEMPT_LIMIT" "${retry_pending_list[@]}" || true
    save_pi_quota_snapshots
  fi

  # Phase B — normal quota acquisition (also serves freshly recovered
  # providers: their success cycle starts with this probe).
  if ! acquire_quota_lock; then
    release_run_lock
    log_info "check: quota.lock busy, skipped"
    return 0
  fi
  prepare_quota_probe
  collect_effective_quotas

  # Phase C — evaluate only providers without a pending debt. A provider that
  # already went through this round's watchdog burst (success OR failure) is
  # never re-evaluated as due in the same check.
  for p in codex antigravity; do
    if provider_is_pending "$p"; then
      log_info "check: $p pending (debt unpaid); normal due evaluation skipped"
      continue
    fi
    if evaluate_provider "$p"; then
      due_providers+=("$p")
    fi
  done
  release_quota_lock

  elapsed=$(( $(now_epoch) - t0 ))
  for p in "${retry_pending_list[@]}"; do
    [[ "$(provider_final_result "$p")" == "发送成功" ]] && recovered+=("$p")
  done
  if (( ${#recovered[@]} > 0 )); then
    # Recovery is worth one card; continued failure stays log-only so a long
    # outage never spams a card every 15 minutes.
    dispatch_task_notification "${recovered[@]}"
  fi

  if (( ${#due_providers[@]} == 0 )); then
    c_due="$(read_provider_next_due codex 2>/dev/null || true)"
    a_due="$(read_provider_next_due antigravity 2>/dev/null || true)"
    log_info "check: nothing due (codex next $(format_reset_time "$c_due" 2>/dev/null || echo unset), antigravity next $(format_reset_time "$a_due" 2>/dev/null || echo unset)) (${elapsed}s)"
    release_run_lock
    return 0
  fi

  log_info "check: due providers: ${due_providers[*]} (${elapsed}s)"
  run_selected_providers "${due_providers[@]}"
  release_run_lock
}

wait_schedule() {
  local now next_due delay
  log_info "timer: watching deadlines (recheck every ${TIMER_RECHECK_SECONDS}s)"
  while true; do
    now="$(/bin/date '+%s')"
    if next_due="$(read_next_due)" && (( now < next_due )); then
      delay=$(( next_due - now ))
      (( delay > TIMER_RECHECK_SECONDS )) && delay="$TIMER_RECHECK_SECONDS"
      "$SLEEP_BIN" "$delay"
      continue
    fi
    check_schedule
    # Avoid a tight loop: when nothing is schedulable right now (for example
    # every provider is still repaying a pending debt), back off instead of
    # re-checking once per second.
    if next_due="$(read_next_due)" && (( $(/bin/date '+%s') < next_due )); then
      "$SLEEP_BIN" 1
    else
      "$SLEEP_BIN" "$TIMER_RECHECK_SECONDS"
    fi
  done
}

main() {
  local command="${1:-run}"
  MAIN_T0="$(now_epoch)"
  log_info "command: $command ${2:-} (pid $$)"

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
