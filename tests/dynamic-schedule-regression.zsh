#!/bin/zsh

set -euo pipefail

readonly TEST_TEMP_DIR="$(mktemp -d /private/tmp/quota-sentinel.XXXXXX)"
export QUOTA_SENTINEL_STATE_DIR="$TEST_TEMP_DIR/state"
export QUOTA_SENTINEL_LOG_DIR="$TEST_TEMP_DIR/logs"
export PI_SOURCE_ONLY=1
export FEISHU_APP_ID="test-app"
export FEISHU_APP_SECRET="test-secret"
export FEISHU_USER_ID="test-user"
export FEISHU_DISABLE_CHART=1
source "${0:A:h}/../quota-sentinel.sh"
LAST_TEMP_DIR="$TEST_TEMP_DIR"
trap cleanup EXIT
ensure_temp_dir

typeset -ga LAST_ATTEMPTED=()
typeset -gi TOTAL_RUNS=0
typeset -g MOCK_CODEX_RESET=0
typeset -g MOCK_ANTIGRAVITY_RESET=0
typeset -g MOCK_CODEX_FRESH=0
typeset -g MOCK_ANTIGRAVITY_FRESH=0
typeset -g CAPTURED_USAGE_MSG=""

reset_scheduler_state() {
  rm -rf "$QUOTA_SENTINEL_STATE_DIR"
  mkdir -p "$QUOTA_SENTINEL_STATE_DIR"
  LAST_ATTEMPTED=()
  TOTAL_RUNS=0
}

collect_effective_quotas() {
  ensure_temp_dir
  CODEX_QUOTA_IS_FRESH="$MOCK_CODEX_FRESH"
  ANTIGRAVITY_QUOTA_IS_FRESH="$MOCK_ANTIGRAVITY_FRESH"
  if (( MOCK_CODEX_FRESH == 1 )); then
    print -r -- '{"source":"Native · codex app-server","fresh":true,"fiveHour":{"remainingPercent":80,"resetAt":'$MOCK_CODEX_RESET'},"weekly":{"remainingPercent":90,"resetAt":'$(( MOCK_CODEX_RESET + 500000 ))'}}' >"$CODEX_QUOTA_NORMALIZED_FILE"
  else
    print -r -- '{"source":"Pi 快照（可能不是最新）","fresh":false,"fiveHour":{"remainingPercent":50,"resetAt":'$MOCK_CODEX_RESET'},"weekly":{"remainingPercent":50,"resetAt":'$(( MOCK_CODEX_RESET + 500000 ))'}}' >"$CODEX_QUOTA_NORMALIZED_FILE"
  fi
  if (( MOCK_ANTIGRAVITY_FRESH == 1 )); then
    print -r -- '{"source":"Native · agy local service","fresh":true,"fiveHour":{"remainingPercent":95,"resetAt":'$MOCK_ANTIGRAVITY_RESET'},"weekly":{"remainingPercent":90,"resetAt":'$(( MOCK_ANTIGRAVITY_RESET + 500000 ))'}}' >"$ANTIGRAVITY_QUOTA_NORMALIZED_FILE"
  else
    print -r -- '{"source":"CodexBar · cached（可能不是最新）","fresh":false,"fiveHour":{"remainingPercent":50,"resetAt":'$MOCK_ANTIGRAVITY_RESET'},"weekly":{"remainingPercent":50,"resetAt":'$(( MOCK_ANTIGRAVITY_RESET + 500000 ))'}}' >"$ANTIGRAVITY_QUOTA_NORMALIZED_FILE"
  fi
}

run_selected_providers() {
  LAST_ATTEMPTED=("$@")
  (( TOTAL_RUNS += 1 ))
  local now provider reset_at
  now="$(/bin/date '+%s')"
  for provider in "$@"; do
    # Mirror the production burst for a successful attempt: the debt is
    # marked, the attempt recorded, and only commit_provider_success writes
    # last_task_at / re-seeds next_due.
    write_provider_retry_pending "$provider" 1
    write_provider_last_attempt "$provider" "$now"
    commit_provider_success "$provider" "$now"
    if reset_at="$(valid_provider_reset_at "$provider" "$now" 2>/dev/null)"; then
      write_provider_last_window "$provider" "$reset_at"
      sync_provider_deadline_from_quota "$provider" "$now"
    fi
  done
}

send_feishu_message() {
  CAPTURED_USAGE_MSG="$4"
  return 0
}

base_now="$(/bin/date '+%s')"

# 1. An active, already-triggered window waits for reset + four minutes.
reset_scheduler_state
MOCK_CODEX_FRESH=1
MOCK_CODEX_RESET=$(( base_now + 3600 ))
MOCK_ANTIGRAVITY_FRESH=0
MOCK_ANTIGRAVITY_RESET=$(( base_now + 1000 ))
write_provider_last_task codex $(( base_now - 4 * 3600 ))
write_provider_last_window codex "$MOCK_CODEX_RESET"
write_provider_next_due antigravity $(( base_now + 10000 ))
check_schedule
[[ "$(read_provider_next_due codex)" == "$(( MOCK_CODEX_RESET + RESET_BUFFER_SECONDS ))" ]]
(( TOTAL_RUNS == 0 ))
print -r -- "Case 1 (active window schedules reset + buffer): passed"

# 2. A missing fresh probe starts from last_task + 5h01. Later fresh probes
# replace that fallback, while a subsequent stale probe preserves the last
# authoritative reset + buffer deadline.
reset_scheduler_state
MOCK_CODEX_FRESH=0
MOCK_CODEX_RESET=$(( base_now + 1000 ))
MOCK_ANTIGRAVITY_FRESH=0
write_provider_last_task codex "$base_now"
write_provider_next_due antigravity $(( base_now + 10000 ))
check_schedule
fallback_due=$(( base_now + RUN_INTERVAL_SECONDS ))
[[ "$(read_provider_next_due codex)" == "$fallback_due" ]]

MOCK_CODEX_FRESH=1
MOCK_CODEX_RESET=$(( base_now + 3600 ))
check_schedule
fresh_due_1=$(( MOCK_CODEX_RESET + RESET_BUFFER_SECONDS ))
[[ "$(read_provider_next_due codex)" == "$fresh_due_1" ]]

MOCK_CODEX_RESET=$(( base_now + 3840 ))  # +240s: same-window jitter, accepted immediately
check_schedule
fresh_due_2=$(( MOCK_CODEX_RESET + RESET_BUFFER_SECONDS ))
[[ "$(read_provider_next_due codex)" == "$fresh_due_2" ]]

MOCK_CODEX_FRESH=0
MOCK_CODEX_RESET=$(( base_now + 9000 ))
check_schedule
[[ "$(read_provider_next_due codex)" == "$fresh_due_2" ]]
(( TOTAL_RUNS == 0 ))
print -r -- "Case 2 (fresh deadlines replace fallback; stale preserves latest): passed"

# 3. A fresh reset + buffer may be earlier than last_task + 5h01 because the
# latter is only a no-quota fallback, not a hard minimum interval.
reset_scheduler_state
MOCK_CODEX_FRESH=1
MOCK_CODEX_RESET=$(( base_now + 1800 ))
write_provider_last_task codex $(( base_now - 600 ))
write_provider_next_due antigravity $(( base_now + 10000 ))
check_schedule
fresh_due=$(( MOCK_CODEX_RESET + RESET_BUFFER_SECONDS ))
fallback_due=$(( base_now - 600 + RUN_INTERVAL_SECONDS ))
(( fresh_due < fallback_due ))
[[ "$(read_provider_next_due codex)" == "$fresh_due" ]]
(( TOTAL_RUNS == 0 ))
print -r -- "Case 3 (fresh reset can shorten the 5h01 fallback): passed"

# 4. An already-due authoritative deadline runs even when it is less than
# 5h01 after the previous real task.
reset_scheduler_state
MOCK_CODEX_FRESH=0
write_provider_last_task codex $(( base_now - 600 ))
write_provider_next_due codex $(( base_now - 1 ))
write_provider_next_due antigravity $(( base_now + 10000 ))
check_schedule
[[ "${LAST_ATTEMPTED[*]}" == "codex" ]]
(( TOTAL_RUNS == 1 ))
print -r -- "Case 4 (due deadline is not blocked by a hard 5h01 interval): passed"

# 4b. A first fresh observation always uses reset + buffer.
reset_scheduler_state
MOCK_CODEX_FRESH=1
MOCK_CODEX_RESET=$(( base_now + 3600 ))
write_provider_next_due antigravity $(( base_now + 10000 ))
check_schedule
[[ "$(read_provider_next_due codex)" == "$(( MOCK_CODEX_RESET + RESET_BUFFER_SECONDS ))" ]]
(( TOTAL_RUNS == 0 ))
print -r -- "Case 4b (first fresh observation uses reset + buffer): passed"

# 5. Stale cache/snapshot data preserves an existing future deadline.
reset_scheduler_state
MOCK_CODEX_FRESH=0
MOCK_ANTIGRAVITY_FRESH=0
write_provider_next_due codex $(( base_now + 8000 ))
write_provider_next_due antigravity $(( base_now + 9000 ))
check_schedule
[[ "$(read_provider_next_due codex)" == "$(( base_now + 8000 ))" ]]
[[ "$(read_provider_next_due antigravity)" == "$(( base_now + 9000 ))" ]]
(( TOTAL_RUNS == 0 ))
print -r -- "Case 5 (stale quota cannot calibrate deadlines): passed"

# 6. Stale quota cannot move an existing due deadline, even when the previous
# task is recent; the deadline itself is allowed to trigger the next run.
reset_scheduler_state
MOCK_CODEX_FRESH=0
write_provider_last_task codex $(( base_now - 100 ))
write_provider_next_due codex $(( base_now - 1000 ))
write_provider_next_due antigravity $(( base_now + 9000 ))
check_schedule
[[ "${LAST_ATTEMPTED[*]}" == "codex" ]]
(( TOTAL_RUNS == 1 ))
print -r -- "Case 6 (stale quota preserves an already-due deadline): passed"

# 7. A late fallback run anchors the next deadline to its actual attempt time.
reset_scheduler_state
MOCK_CODEX_FRESH=0
write_provider_last_task codex $(( base_now - RUN_INTERVAL_SECONDS - 3600 ))
write_provider_next_due codex $(( base_now - 3600 ))
write_provider_next_due antigravity $(( base_now + 9000 ))
check_schedule
(( TOTAL_RUNS == 1 ))
[[ "${LAST_ATTEMPTED[*]}" == "codex" ]]
fallback_due="$(read_provider_next_due codex)"
(( fallback_due >= base_now + RUN_INTERVAL_SECONDS ))
(( fallback_due <= $(/bin/date '+%s') + RUN_INTERVAL_SECONDS ))
print -r -- "Case 7 (late fallback uses actual run time): passed"

# 8. /usage syncs fresh timing only; it never runs or marks a window/task.
reset_scheduler_state
MOCK_CODEX_FRESH=1
MOCK_CODEX_RESET=$(( base_now + 17940 ))
MOCK_ANTIGRAVITY_FRESH=0
MOCK_ANTIGRAVITY_RESET=$(( base_now + 2000 ))
write_provider_last_task codex "$base_now"
write_provider_last_window codex 111111
write_provider_next_due codex $(( base_now + 30000 ))
write_provider_last_task antigravity 222222
write_provider_last_window antigravity 333333
write_provider_next_due antigravity $(( base_now + 12000 ))
CAPTURED_USAGE_MSG=""
send_usage_notification
[[ "$CAPTURED_USAGE_MSG" == *"GPT-5.6 Luna"* ]]
[[ "$CAPTURED_USAGE_MSG" == *"Gemini 3.7 Flash"* ]]
[[ "$(read_provider_last_known_reset codex)" == "$MOCK_CODEX_RESET" ]]
[[ "$(read_provider_next_due codex)" == "$(( MOCK_CODEX_RESET + RESET_BUFFER_SECONDS ))" ]]
[[ "$(read_provider_last_task codex)" == "$base_now" ]]
[[ "$(read_provider_last_window codex)" == "111111" ]]
[[ "$(read_provider_next_due antigravity)" == "$(( base_now + 12000 ))" ]]
[[ "$(read_provider_last_task antigravity)" == "222222" ]]
[[ "$(read_provider_last_window antigravity)" == "333333" ]]
(( TOTAL_RUNS == 0 ))
print -r -- "Case 8 (/usage synchronizes only fresh scheduler fields): passed"

# 9. Invalid future reset data is rejected without moving the deadline.
reset_scheduler_state
MOCK_CODEX_FRESH=1
MOCK_CODEX_RESET=$(( base_now + MAX_WINDOW_FUTURE_SECONDS + 60 ))
write_provider_next_due codex $(( base_now + 6000 ))
write_provider_next_due antigravity $(( base_now + 9000 ))
check_schedule
[[ "$(read_provider_next_due codex)" == "$(( base_now + 6000 ))" ]]
(( TOTAL_RUNS == 0 ))
print -r -- "Case 9 (invalid reset is rejected): passed"

# 10. Post-run starts from 5h01 fallback but a fresh reset + buffer replaces it,
# even when the resulting deadline is earlier.
reset_scheduler_state
MOCK_CODEX_FRESH=1
MOCK_CODEX_RESET=$(( base_now + 600 ))
collect_effective_quotas
write_provider_last_task codex "$base_now"
write_provider_next_due codex $(( base_now + RUN_INTERVAL_SECONDS ))
write_provider_last_window codex "$MOCK_CODEX_RESET"
sync_provider_deadline_from_quota codex "$base_now"
[[ "$(read_provider_next_due codex)" == "$(( MOCK_CODEX_RESET + RESET_BUFFER_SECONDS ))" ]]
print -r -- "Case 10 (post-run fresh reset replaces the 5h01 fallback): passed"

# 10b. Once the scheduled reset has passed, its pending four-minute buffer is
# armed and a newer Fresh window cannot cancel that execution.
reset_scheduler_state
write_provider_last_task codex "$base_now"
old_reset=$(( base_now + 17940 ))
old_due=$(( old_reset + RESET_BUFFER_SECONDS ))
write_provider_last_known_reset codex "$old_reset"
write_provider_next_due codex "$old_due"
rollover_now=$(( base_now + 18000 ))
MOCK_CODEX_FRESH=1
MOCK_CODEX_RESET=$(( rollover_now + 17940 ))
collect_effective_quotas
sync_rc=0
sync_provider_deadline_from_quota codex "$rollover_now" || sync_rc=$?
(( sync_rc == 2 ))
[[ "$(read_provider_last_known_reset codex)" == "$old_reset" ]]
[[ "$(read_provider_next_due codex)" == "$old_due" ]]
print -r -- "Case 10b (reset buffer blocks newer Fresh calibration): passed"

# 11. Legacy state migration remains compatible.
reset_scheduler_state
print -r -- "987654" >"$QUOTA_SENTINEL_STATE_DIR/last-task-at"
print -r -- "876543" >"$QUOTA_SENTINEL_STATE_DIR/next-due-at"
print -r -- "100000:200000" >"$QUOTA_SENTINEL_STATE_DIR/last-triggered-window"
[[ "$(read_provider_last_task codex)" == "987654" ]]
[[ "$(read_provider_last_task antigravity)" == "987654" ]]
[[ "$(read_provider_next_due codex)" == "876543" ]]
[[ "$(read_provider_next_due antigravity)" == "876543" ]]
[[ "$(read_provider_last_known_reset codex)" == "100000" ]]
[[ "$(read_provider_last_known_reset antigravity)" == "200000" ]]
print -r -- "Case 11 (legacy state migration): passed"

# 12. CodexBar cache metadata remains explicitly stale.
fixture_live="$TEST_TEMP_DIR/codex-live-test.json"
fixture_out="$TEST_TEMP_DIR/codex-cached-test.json"
print -r -- '{"provider":"codex","source":"CodexBar · cli","fresh":true,"capturedAt":1788000000,"fiveHour":{"remainingPercent":70,"resetAt":1788050000},"weekly":{"remainingPercent":80,"resetAt":1788650000}}' >"$fixture_live"
save_codexbar_cache "$fixture_live" "$CODEXBAR_CODEX_CACHE_FILE"
use_codexbar_cached_codex "$fixture_out"
[[ "$(jq -r '.fresh' "$fixture_out")" == "false" ]]
[[ "$(jq -r '.cached' "$fixture_out")" == "true" ]]
[[ "$(jq -r '.capturedAt' "$fixture_out")" == "1788000000" ]]
print -r -- "Case 12 (CodexBar cache stays stale/display-only): passed"

cleanup
print -r -- "dynamic schedule regression: all cases passed"
