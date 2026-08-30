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
        if (( MOCK_CODEX_FRESH == 1 )) && (( MOCK_CODEX_RESET > now )); then
          write_provider_last_known_reset "codex" "$MOCK_CODEX_RESET"
          write_provider_last_window "codex" "$MOCK_CODEX_RESET"
          write_provider_next_due "codex" $(( MOCK_CODEX_RESET + RUN_INTERVAL_SECONDS ))
        else
          local prev_due
          prev_due="$(read_provider_next_due "codex" || true)"
          if [[ "$prev_due" =~ ^[0-9]+$ ]]; then
            write_provider_next_due "codex" $(( prev_due + RUN_INTERVAL_SECONDS ))
          else
            write_provider_next_due "codex" $(( now + RUN_INTERVAL_SECONDS ))
          fi
        fi
        ;;
      antigravity)
        ANTIGRAVITY_RUN_RESULT="发送成功"
        write_provider_last_task "antigravity" "$now"
        if (( MOCK_ANTIGRAVITY_FRESH == 1 )) && (( MOCK_ANTIGRAVITY_RESET > now )); then
          write_provider_last_known_reset "antigravity" "$MOCK_ANTIGRAVITY_RESET"
          write_provider_last_window "antigravity" "$MOCK_ANTIGRAVITY_RESET"
          write_provider_next_due "antigravity" $(( MOCK_ANTIGRAVITY_RESET + RUN_INTERVAL_SECONDS ))
        else
          local prev_due
          prev_due="$(read_provider_next_due "antigravity" || true)"
          if [[ "$prev_due" =~ ^[0-9]+$ ]]; then
            write_provider_next_due "antigravity" $(( prev_due + RUN_INTERVAL_SECONDS ))
          else
            write_provider_next_due "antigravity" $(( now + RUN_INTERVAL_SECONDS ))
          fi
        fi
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
# Case 1: Probe success calibrates deadline to reset_at + 5h01m
# -------------------------------------------------------------
MOCK_CODEX_FRESH=1
MOCK_CODEX_RESET=$(( base_now + 3600 )) # Reset in 1 hour
MOCK_ANTIGRAVITY_FRESH=1
MOCK_ANTIGRAVITY_RESET=$(( base_now + 1800 )) # Reset in 30 mins
check_schedule

codex_due="$(read_provider_next_due codex)"
anti_due="$(read_provider_next_due antigravity)"
if (( codex_due != MOCK_CODEX_RESET + RUN_INTERVAL_SECONDS )); then
  print -u2 -r -- "Case 1 failed: Codex next_due ($codex_due) != $(( MOCK_CODEX_RESET + RUN_INTERVAL_SECONDS ))"
  exit 1
fi
if (( anti_due != MOCK_ANTIGRAVITY_RESET + RUN_INTERVAL_SECONDS )); then
  print -u2 -r -- "Case 1 failed: Antigravity next_due ($anti_due) != $(( MOCK_ANTIGRAVITY_RESET + RUN_INTERVAL_SECONDS ))"
  exit 1
fi
print -r -- "Case 1 (Probe success calibrates deadline to reset + 5h01m): passed"

# -------------------------------------------------------------
# Case 2: Probe failure preserves deadline without drift
# -------------------------------------------------------------
saved_codex_due="$codex_due"
saved_anti_due="$anti_due"
MOCK_CODEX_FRESH=0
MOCK_ANTIGRAVITY_FRESH=0
check_schedule

if (( $(read_provider_next_due codex) != saved_codex_due )) ||
   (( $(read_provider_next_due antigravity) != saved_anti_due )); then
  print -u2 -r -- "Case 2 failed: Probe failure caused deadline drift!"
  exit 1
fi
print -r -- "Case 2 (Probe failure preserves deadline without drift): passed"

# -------------------------------------------------------------
# Case 3: Multiple consecutive probe failures (no drift over time)
# -------------------------------------------------------------
check_schedule
check_schedule
check_schedule
if (( $(read_provider_next_due codex) != saved_codex_due )) ||
   (( $(read_provider_next_due antigravity) != saved_anti_due )); then
  print -u2 -r -- "Case 3 failed: Consecutive probe failures shifted deadline!"
  exit 1
fi
print -r -- "Case 3 (Multiple consecutive probe failures do not drift deadline): passed"

# -------------------------------------------------------------
# Case 4: Subsequent probe success re-anchors deadline immediately
# -------------------------------------------------------------
MOCK_ANTIGRAVITY_FRESH=1
MOCK_ANTIGRAVITY_RESET=$(( base_now + 7200 )) # New reset in 2 hours
MOCK_CODEX_FRESH=0 # Codex still failing
check_schedule

if (( $(read_provider_next_due antigravity) != MOCK_ANTIGRAVITY_RESET + RUN_INTERVAL_SECONDS )); then
  print -u2 -r -- "Case 4 failed: Antigravity did not re-anchor to new reset!"
  exit 1
fi
if (( $(read_provider_next_due codex) != saved_codex_due )); then
  print -u2 -r -- "Case 4 failed: Codex deadline was unexpectedly modified!"
  exit 1
fi
print -r -- "Case 4 (Subsequent probe success re-anchors deadline immediately): passed"

# -------------------------------------------------------------
# Case 5: When now >= next_due_at, executes provider once
# -------------------------------------------------------------
LAST_ATTEMPTED=()
TOTAL_RUNS=0
# Set Antigravity due in past (now reached), Codex far in future
write_provider_next_due "antigravity" $(( base_now - 10 ))
write_provider_next_due "codex" $(( base_now + 10000 ))
MOCK_ANTIGRAVITY_FRESH=0 # probe fails post-run, should advance fallback
MOCK_CODEX_FRESH=0
check_schedule

if [[ "${LAST_ATTEMPTED[*]}" != "antigravity" ]] || (( TOTAL_RUNS != 1 )); then
  print -u2 -r -- "Case 5 failed: Expected only antigravity to run, got ${LAST_ATTEMPTED[*]}"
  exit 1
fi
print -r -- "Case 5 (Reaching next_due_at triggers execution): passed"

# -------------------------------------------------------------
# Case 6: Continuous failure post-run advances fallback deadline (+5h01m)
# -------------------------------------------------------------
new_anti_due="$(read_provider_next_due antigravity)"
if (( new_anti_due != base_now - 10 + RUN_INTERVAL_SECONDS )); then
  print -u2 -r -- "Case 6 failed: Post-run fallback not advanced by 5h01m (got $new_anti_due)"
  exit 1
fi
# Next immediate check should not repeat
check_schedule
if (( TOTAL_RUNS != 1 )); then
  print -u2 -r -- "Case 6 failed: Task repeated unexpectedly!"
  exit 1
fi
print -r -- "Case 6 (Post-run failure advances fallback by 5h01m without repeat): passed"

# -------------------------------------------------------------
# Case 7: API recovery after fallback execution re-anchors to real reset
# -------------------------------------------------------------
MOCK_ANTIGRAVITY_FRESH=1
MOCK_ANTIGRAVITY_RESET=$(( base_now + 15000 ))
check_schedule

if (( $(read_provider_next_due antigravity) != MOCK_ANTIGRAVITY_RESET + RUN_INTERVAL_SECONDS )); then
  print -u2 -r -- "Case 7 failed: Recovery did not re-anchor deadline to real reset"
  exit 1
fi
print -r -- "Case 7 (API recovery re-anchors fallback deadline to real reset): passed"

# -------------------------------------------------------------
# Case 8: Codex and Antigravity independent deadlines
# -------------------------------------------------------------
LAST_ATTEMPTED=()
TOTAL_RUNS=0
write_provider_next_due "codex" $(( base_now - 10 )) # Codex due
write_provider_next_due "antigravity" $(( base_now + 5000 )) # Antigravity not due
MOCK_CODEX_FRESH=0 # probe failing, fallback triggers
MOCK_ANTIGRAVITY_FRESH=0 # probe failing, keeps 5000

check_schedule
if [[ "${LAST_ATTEMPTED[*]}" != "codex" ]] || (( TOTAL_RUNS != 1 )); then
  print -u2 -r -- "Case 8 failed: Expected only codex to run, got ${LAST_ATTEMPTED[*]}"
  exit 1
fi
if (( $(read_provider_next_due antigravity) != base_now + 5000 )); then
  print -u2 -r -- "Case 8 failed: Antigravity deadline was altered by Codex execution!"
  exit 1
fi
print -r -- "Case 8 (Codex and Antigravity independent deadlines): passed"

# -------------------------------------------------------------
# Case 9: One probe success, one probe failure
# -------------------------------------------------------------
c_prev="$(read_provider_next_due codex)"
MOCK_CODEX_FRESH=0 # Codex probe fails
MOCK_ANTIGRAVITY_FRESH=1 # Antigravity probe succeeds
MOCK_ANTIGRAVITY_RESET=$(( base_now + 8000 ))
check_schedule

if (( $(read_provider_next_due codex) != c_prev )); then
  print -u2 -r -- "Case 9 failed: Failed Codex probe altered deadline!"
  exit 1
fi
if (( $(read_provider_next_due antigravity) != MOCK_ANTIGRAVITY_RESET + RUN_INTERVAL_SECONDS )); then
  print -u2 -r -- "Case 9 failed: Successful Antigravity probe did not calibrate deadline!"
  exit 1
fi
print -r -- "Case 9 (One probe success, one failure handled independently): passed"

# -------------------------------------------------------------
# Case 10: Anomalous reset data (in past or > 6h in future) is rejected
# -------------------------------------------------------------
a_prev="$(read_provider_next_due antigravity)"
MOCK_ANTIGRAVITY_FRESH=1
MOCK_ANTIGRAVITY_RESET=$(( base_now - 500 )) # In past
check_schedule
if (( $(read_provider_next_due antigravity) != a_prev )); then
  print -u2 -r -- "Case 10 failed: Past reset timestamp overwritten deadline!"
  exit 1
fi
MOCK_ANTIGRAVITY_RESET=$(( base_now + 30000 )) # > 6h
check_schedule
if (( $(read_provider_next_due antigravity) != a_prev )); then
  print -u2 -r -- "Case 10 failed: Excessive future reset overwritten deadline!"
  exit 1
fi
print -r -- "Case 10 (Anomalous reset data rejected): passed"

# -------------------------------------------------------------
# Case 11: Task notification card scope strict isolation
# -------------------------------------------------------------
CODEX_RUN_RESULT="发送成功"
ANTIGRAVITY_RUN_RESULT="发送成功"
msg_c="$(task_notification_message codex)"
[[ "$msg_c" == *"**GPT-5.6 Luna**"* ]]
[[ "$msg_c" != *"**Gemini 3.7 Flash · Low**"* ]]

msg_a="$(task_notification_message antigravity)"
[[ "$msg_a" != *"**GPT-5.6 Luna**"* ]]
[[ "$msg_a" == *"**Gemini 3.7 Flash · Low**"* ]]

msg_both="$(task_notification_message codex antigravity)"
[[ "$msg_both" == *"**GPT-5.6 Luna**"* ]]
[[ "$msg_both" == *"**Gemini 3.7 Flash · Low**"* ]]
[[ "$msg_both" == *"────────────"* ]]
print -r -- "Case 11 (Task notification card scope strictly isolated): passed"

# -------------------------------------------------------------
# Case 12: /usage query is strictly read-only and shows both providers
# -------------------------------------------------------------
write_provider_last_task "codex" 111111
write_provider_next_due "codex" 333333
write_provider_last_task "antigravity" 222222
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
   [[ "$(read_provider_next_due codex)" != "333333" ]] ||
   [[ "$(read_provider_last_task antigravity)" != "222222" ]] ||
   [[ "$(read_provider_next_due antigravity)" != "444444" ]]; then
  print -u2 -r -- "Case 12 failed: /usage modified scheduler state!"
  exit 1
fi
print -r -- "Case 12 (/usage is strictly read-only and does not touch scheduler): passed"

# -------------------------------------------------------------
# Case 13: Legacy state migration
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
[[ "$(read_provider_last_known_reset codex)" == "100000" ]]
[[ "$(read_provider_last_known_reset antigravity)" == "200000" ]]
print -r -- "Case 13 (Legacy state migration preserved seamlessly): passed"

cleanup
print -r -- "dynamic schedule regression: all 13 cases passed"
