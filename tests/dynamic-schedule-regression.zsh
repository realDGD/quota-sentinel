#!/bin/zsh

set -euo pipefail

readonly TEST_TEMP_DIR="$(mktemp -d /private/tmp/quota-sentinel-dynamic-test.XXXXXX)"
export QUOTA_SENTINEL_STATE_DIR="$TEST_TEMP_DIR/state"
export PI_SOURCE_ONLY=1
source "${0:A:h}/../quota-sentinel.sh"
LAST_TEMP_DIR="$TEST_TEMP_DIR"
trap cleanup EXIT
ensure_temp_dir

typeset -ga LAST_ATTEMPTED=()
typeset -gi TOTAL_RUNS=0
typeset -g MOCK_CODEX_RESET=0
typeset -g MOCK_ANTIGRAVITY_RESET=0
typeset -g MOCK_CODEX_FRESH=1
typeset -g MOCK_ANTIGRAVITY_FRESH=1
typeset -g LAST_DISPATCHED_MESSAGE=""

run_selected_providers() {
  LAST_ATTEMPTED=("$@")
  (( TOTAL_RUNS += 1 ))
  local now
  now="$(/bin/date '+%s')"
  for p in "$@"; do
    case "$p" in
      codex)
        CODEX_RUN_RESULT="发送成功"
        write_provider_last_task "codex" "$now"
        write_provider_last_window "codex" "$MOCK_CODEX_RESET"
        write_provider_next_due "codex" $(( now + RUN_INTERVAL_SECONDS ))
        ;;
      antigravity)
        ANTIGRAVITY_RUN_RESULT="发送成功"
        write_provider_last_task "antigravity" "$now"
        write_provider_last_window "antigravity" "$MOCK_ANTIGRAVITY_RESET"
        write_provider_next_due "antigravity" $(( now + RUN_INTERVAL_SECONDS ))
        ;;
    esac
  done
  LAST_DISPATCHED_MESSAGE="$(task_notification_message "$@")"
}

collect_effective_quotas() {
  ensure_temp_dir
  CODEX_QUOTA_IS_FRESH="$MOCK_CODEX_FRESH"
  ANTIGRAVITY_QUOTA_IS_FRESH="$MOCK_ANTIGRAVITY_FRESH"
  if (( MOCK_CODEX_FRESH == 1 )); then
    print -r -- '{"source":"CodexBar · codex-cli","fiveHour":{"remainingPercent":80,"resetAt":'$MOCK_CODEX_RESET'},"weekly":{"remainingPercent":90,"resetAt":'$(( MOCK_CODEX_RESET + 500000 ))'}}' >"$CODEX_QUOTA_NORMALIZED_FILE"
  else
    print -r -- '{"source":"Pi 响应头快照","fiveHour":{"remainingPercent":50,"resetAt":'$MOCK_CODEX_RESET'},"weekly":{"remainingPercent":50,"resetAt":'$(( MOCK_CODEX_RESET + 500000 ))'}}' >"$CODEX_QUOTA_NORMALIZED_FILE"
  fi
  if (( MOCK_ANTIGRAVITY_FRESH == 1 )); then
    print -r -- '{"source":"CodexBar · cli","fiveHour":{"remainingPercent":95,"resetAt":'$MOCK_ANTIGRAVITY_RESET'},"weekly":{"remainingPercent":90,"resetAt":'$(( MOCK_ANTIGRAVITY_RESET + 500000 ))'}}' >"$ANTIGRAVITY_QUOTA_NORMALIZED_FILE"
  else
    print -r -- '{"source":"Pi Antigravity API 快照","fiveHour":{"remainingPercent":50,"resetAt":'$MOCK_ANTIGRAVITY_RESET'},"weekly":{"remainingPercent":50,"resetAt":'$(( MOCK_ANTIGRAVITY_RESET + 500000 ))'}}' >"$ANTIGRAVITY_QUOTA_NORMALIZED_FILE"
  fi
}

base_now="$(/bin/date '+%s')"

# -------------------------------------------------------------
# Case 1: Only Antigravity due -> Runs only Antigravity
# -------------------------------------------------------------
LAST_ATTEMPTED=()
TOTAL_RUNS=0
# Codex: existing window from earlier, reset in 3h46m (13560s), already executed
write_provider_last_task "codex" "$base_now"
write_provider_last_window "codex" "$(( base_now + 13560 ))"
MOCK_CODEX_RESET=$(( base_now + 13560 ))
# Antigravity: new window, reset in 4h59m (17940s), not executed
write_provider_last_task "antigravity" "$(( base_now - RUN_INTERVAL_SECONDS - 10 ))"
write_provider_last_window "antigravity" "$(( base_now - 1000 ))"
MOCK_ANTIGRAVITY_RESET=$(( base_now + 17940 ))

check_schedule
if [[ "${LAST_ATTEMPTED[*]}" != "antigravity" ]] || (( TOTAL_RUNS != 1 )); then
  print -u2 -r -- "Case 1 failed: Expected only antigravity to run, got ${LAST_ATTEMPTED[*]}"
  exit 1
fi
if [[ "$LAST_DISPATCHED_MESSAGE" != *"Gemini 3.7 Flash · Low"* ]] ||
   [[ "$LAST_DISPATCHED_MESSAGE" == *"GPT-5.6 Luna"* ]]; then
  print -u2 -r -- "Case 1 failed: Card scope leaked Codex!"
  exit 1
fi
print -r -- "Case 1 (Only Antigravity due -> runs and renders only Gemini): passed"

# -------------------------------------------------------------
# Case 2: Only Codex due -> Runs only Codex
# -------------------------------------------------------------
LAST_ATTEMPTED=()
TOTAL_RUNS=0
# Antigravity: current window already executed
write_provider_last_task "antigravity" "$base_now"
write_provider_last_window "antigravity" "$(( base_now + 13560 ))"
MOCK_ANTIGRAVITY_RESET=$(( base_now + 13560 ))
# Codex: new window, reset in 4h59m (17940s), not executed
write_provider_last_task "codex" "$(( base_now - RUN_INTERVAL_SECONDS - 10 ))"
write_provider_last_window "codex" "$(( base_now - 1000 ))"
MOCK_CODEX_RESET=$(( base_now + 17940 ))

check_schedule
if [[ "${LAST_ATTEMPTED[*]}" != "codex" ]] || (( TOTAL_RUNS != 1 )); then
  print -u2 -r -- "Case 2 failed: Expected only codex to run, got ${LAST_ATTEMPTED[*]}"
  exit 1
fi
if [[ "$LAST_DISPATCHED_MESSAGE" != *"GPT-5.6 Luna"* ]] ||
   [[ "$LAST_DISPATCHED_MESSAGE" == *"Gemini 3.7 Flash · Low"* ]]; then
  print -u2 -r -- "Case 2 failed: Card scope leaked Gemini!"
  exit 1
fi
print -r -- "Case 2 (Only Codex due -> runs and renders only Luna): passed"

# -------------------------------------------------------------
# Case 3: Both due -> Runs both in one combined execution
# -------------------------------------------------------------
LAST_ATTEMPTED=()
TOTAL_RUNS=0
write_provider_last_task "codex" "$(( base_now - RUN_INTERVAL_SECONDS - 10 ))"
write_provider_last_window "codex" "$(( base_now - 1000 ))"
MOCK_CODEX_RESET=$(( base_now + 17940 ))
write_provider_last_task "antigravity" "$(( base_now - RUN_INTERVAL_SECONDS - 10 ))"
write_provider_last_window "antigravity" "$(( base_now - 1000 ))"
MOCK_ANTIGRAVITY_RESET=$(( base_now + 17940 ))

check_schedule
if [[ "${LAST_ATTEMPTED[*]}" != "codex antigravity" ]] || (( TOTAL_RUNS != 1 )); then
  print -u2 -r -- "Case 3 failed: Expected both providers to run, got ${LAST_ATTEMPTED[*]}"
  exit 1
fi
if [[ "$LAST_DISPATCHED_MESSAGE" != *"GPT-5.6 Luna"* ]] ||
   [[ "$LAST_DISPATCHED_MESSAGE" != *"Gemini 3.7 Flash · Low"* ]] ||
   [[ "$LAST_DISPATCHED_MESSAGE" != *"────────────"* ]]; then
  print -u2 -r -- "Case 3 failed: Card did not combine both sections properly!"
  exit 1
fi
print -r -- "Case 3 (Both due -> combined run and card): passed"

# -------------------------------------------------------------
# Case 4: Codex just ran (10 min ago), Antigravity due -> Antigravity runs
# -------------------------------------------------------------
LAST_ATTEMPTED=()
TOTAL_RUNS=0
write_provider_last_task "codex" "$(( base_now - 600 ))"
write_provider_last_window "codex" "$(( base_now + 17400 ))"
MOCK_CODEX_RESET=$(( base_now + 17400 ))
write_provider_last_task "antigravity" "$(( base_now - RUN_INTERVAL_SECONDS - 10 ))"
write_provider_last_window "antigravity" "$(( base_now - 1000 ))"
MOCK_ANTIGRAVITY_RESET=$(( base_now + 17940 ))

check_schedule
if [[ "${LAST_ATTEMPTED[*]}" != "antigravity" ]] || (( TOTAL_RUNS != 1 )); then
  print -u2 -r -- "Case 4 failed: Antigravity was blocked by Codex's recent run!"
  exit 1
fi
print -r -- "Case 4 (Codex recently ran -> Antigravity independent run): passed"

# -------------------------------------------------------------
# Case 5: Antigravity just ran (10 min ago), Codex due -> Codex runs
# -------------------------------------------------------------
LAST_ATTEMPTED=()
TOTAL_RUNS=0
write_provider_last_task "antigravity" "$(( base_now - 600 ))"
write_provider_last_window "antigravity" "$(( base_now + 17400 ))"
MOCK_ANTIGRAVITY_RESET=$(( base_now + 17400 ))
write_provider_last_task "codex" "$(( base_now - RUN_INTERVAL_SECONDS - 10 ))"
write_provider_last_window "codex" "$(( base_now - 1000 ))"
MOCK_CODEX_RESET=$(( base_now + 17940 ))

check_schedule
if [[ "${LAST_ATTEMPTED[*]}" != "codex" ]] || (( TOTAL_RUNS != 1 )); then
  print -u2 -r -- "Case 5 failed: Codex was blocked by Antigravity's recent run!"
  exit 1
fi
print -r -- "Case 5 (Antigravity recently ran -> Codex independent run): passed"

# -------------------------------------------------------------
# Case 6: Same Codex window repeated watchdog -> Does not run
# -------------------------------------------------------------
LAST_ATTEMPTED=()
TOTAL_RUNS=0
MOCK_CODEX_RESET=$(( base_now + 15000 ))
write_provider_last_window "codex" "$MOCK_CODEX_RESET"
write_provider_last_task "codex" "$(( base_now - 3000 ))"
MOCK_ANTIGRAVITY_RESET=$(( base_now + 15000 ))
write_provider_last_window "antigravity" "$MOCK_ANTIGRAVITY_RESET"
write_provider_last_task "antigravity" "$(( base_now - 3000 ))"

check_schedule
if (( TOTAL_RUNS != 0 )); then
  print -u2 -r -- "Case 6 failed: Ran on duplicate window"
  exit 1
fi
print -r -- "Case 6 (Duplicate window deduplication): passed"

# -------------------------------------------------------------
# Case 7: Antigravity missed-reset recovery -> Only recovers Antigravity
# -------------------------------------------------------------
LAST_ATTEMPTED=()
TOTAL_RUNS=0
# Antigravity: new window, remaining 4h40m (16800s), unexecuted
write_provider_last_task "antigravity" "$(( base_now - RUN_INTERVAL_SECONDS - 100 ))"
write_provider_last_window "antigravity" "$(( base_now - 2000 ))"
MOCK_ANTIGRAVITY_RESET=$(( base_now + 16800 ))
# Codex: already executed
write_provider_last_task "codex" "$base_now"
write_provider_last_window "codex" "$(( base_now + 12000 ))"
MOCK_CODEX_RESET=$(( base_now + 12000 ))

check_schedule
if [[ "${LAST_ATTEMPTED[*]}" != "antigravity" ]] || (( TOTAL_RUNS != 1 )); then
  print -u2 -r -- "Case 7 failed: Antigravity missed-reset recovery failed!"
  exit 1
fi
# Re-check should not repeat
check_schedule
if (( TOTAL_RUNS != 1 )); then
  print -u2 -r -- "Case 7 failed: Antigravity recovery repeated!"
  exit 1
fi
print -r -- "Case 7 (Antigravity missed-reset recovery isolated and non-repeating): passed"

# -------------------------------------------------------------
# Case 8: Codex missed-reset recovery -> Only recovers Codex
# -------------------------------------------------------------
LAST_ATTEMPTED=()
TOTAL_RUNS=0
# Codex: new window, remaining 4h40m (16800s), unexecuted
write_provider_last_task "codex" "$(( base_now - RUN_INTERVAL_SECONDS - 100 ))"
write_provider_last_window "codex" "$(( base_now - 2000 ))"
MOCK_CODEX_RESET=$(( base_now + 16800 ))
# Antigravity: already executed
write_provider_last_task "antigravity" "$base_now"
write_provider_last_window "antigravity" "$(( base_now + 12000 ))"
MOCK_ANTIGRAVITY_RESET=$(( base_now + 12000 ))

check_schedule
if [[ "${LAST_ATTEMPTED[*]}" != "codex" ]] || (( TOTAL_RUNS != 1 )); then
  print -u2 -r -- "Case 8 failed: Codex missed-reset recovery failed!"
  exit 1
fi
check_schedule
if (( TOTAL_RUNS != 1 )); then
  print -u2 -r -- "Case 8 failed: Codex recovery repeated!"
  exit 1
fi
print -r -- "Case 8 (Codex missed-reset recovery isolated and non-repeating): passed"

# -------------------------------------------------------------
# Case 9: Anomaly protection on invalid reset timestamps
# -------------------------------------------------------------
LAST_ATTEMPTED=()
TOTAL_RUNS=0
write_provider_last_task "codex" "$(( base_now - RUN_INTERVAL_SECONDS - 10 ))"
write_provider_last_task "antigravity" "$(( base_now - RUN_INTERVAL_SECONDS - 10 ))"
MOCK_CODEX_RESET=$(( base_now - 200 )) # Past
MOCK_ANTIGRAVITY_RESET=$(( base_now + 30000 )) # > 6h in future
check_schedule
if (( TOTAL_RUNS != 0 )); then
  print -u2 -r -- "Case 9 failed: Ran on anomalous reset timestamps!"
  exit 1
fi
print -r -- "Case 9 (Anomalous reset timestamps protected): passed"

# -------------------------------------------------------------
# Case 10: Task Notification Card Scope formatting
# -------------------------------------------------------------
CODEX_RUN_RESULT="发送成功"
ANTIGRAVITY_RUN_RESULT="发送成功"
msg_codex="$(task_notification_message codex)"
[[ "$msg_codex" == *"**GPT-5.6 Luna**"* ]]
[[ "$msg_codex" != *"**Gemini 3.7 Flash · Low**"* ]]

msg_anti="$(task_notification_message antigravity)"
[[ "$msg_anti" != *"**GPT-5.6 Luna**"* ]]
[[ "$msg_anti" == *"**Gemini 3.7 Flash · Low**"* ]]

msg_both="$(task_notification_message codex antigravity)"
[[ "$msg_both" == *"**GPT-5.6 Luna**"* ]]
[[ "$msg_both" == *"**Gemini 3.7 Flash · Low**"* ]]
[[ "$msg_both" == *"────────────"* ]]
print -r -- "Case 10 (Task notification message scope strict isolation): passed"

# -------------------------------------------------------------
# Case 11: /usage query is strictly read-only and shows both providers
# -------------------------------------------------------------
write_provider_last_task "codex" 111111
write_provider_last_window "codex" "win-c"
write_provider_next_due "codex" 333333
write_provider_last_task "antigravity" 222222
write_provider_last_window "antigravity" "win-a"
write_provider_next_due "antigravity" 444444

MOCK_CODEX_RESET=$(( base_now + 10000 ))
MOCK_ANTIGRAVITY_RESET=$(( base_now + 12000 ))

typeset -g CAPTURED_USAGE_MSG=""
send_feishu_message() {
  CAPTURED_USAGE_MSG="$4"
  return 0
}
send_usage_notification
[[ "$CAPTURED_USAGE_MSG" == *"**GPT-5.6 Luna**"* ]]
[[ "$CAPTURED_USAGE_MSG" == *"**Gemini 3.7 Flash · Low**"* ]]
[[ "$CAPTURED_USAGE_MSG" == *"即时配额查询"* ]]

if [[ "$(read_provider_last_task codex)" != "111111" ]] ||
   [[ "$(read_provider_last_window codex)" != "win-c" ]] ||
   [[ "$(read_provider_next_due codex)" != "333333" ]] ||
   [[ "$(read_provider_last_task antigravity)" != "222222" ]] ||
   [[ "$(read_provider_last_window antigravity)" != "win-a" ]] ||
   [[ "$(read_provider_next_due antigravity)" != "444444" ]]; then
  print -u2 -r -- "Case 11 failed: /usage modified scheduler state!"
  exit 1
fi
print -r -- "Case 11 (/usage shows both and leaves scheduler state untouched): passed"

# -------------------------------------------------------------
# Case 12: Legacy state migration
# -------------------------------------------------------------
rm -rf "$QUOTA_SENTINEL_STATE_DIR"
mkdir -p "$QUOTA_SENTINEL_STATE_DIR"
print -r -- "987654" >"$QUOTA_SENTINEL_STATE_DIR/last-task-at"
print -r -- "876543" >"$QUOTA_SENTINEL_STATE_DIR/next-due-at"
print -r -- "100000:200000" >"$QUOTA_SENTINEL_STATE_DIR/last-triggered-window"

[[ "$(read_provider_last_task codex)" == "987654" ]]
[[ "$(read_provider_last_task antigravity)" == "987654" ]]
[[ "$(read_provider_next_due codex)" == "876543" ]]
[[ "$(read_provider_next_due antigravity)" == "876543" ]]
[[ "$(read_provider_last_window codex)" == "100000" ]]
[[ "$(read_provider_last_window antigravity)" == "200000" ]]
print -r -- "Case 12 (Legacy state migration works seamlessly): passed"

cleanup
print -r -- "dynamic schedule regression: all 12 cases passed"
