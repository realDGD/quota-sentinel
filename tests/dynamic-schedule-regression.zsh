#!/bin/zsh

set -euo pipefail

readonly TEST_TEMP_DIR="$(mktemp -d /private/tmp/quota-sentinel-dynamic-test.XXXXXX)"
export QUOTA_SENTINEL_STATE_DIR="$TEST_TEMP_DIR/state"
export PI_SOURCE_ONLY=1
source "${0:A:h}/../quota-sentinel.sh"
LAST_TEMP_DIR="$TEST_TEMP_DIR"
trap cleanup EXIT
ensure_temp_dir

typeset -gi RUN_COUNT=0
typeset -g MOCK_CODEX_RESET=0
typeset -g MOCK_ANTIGRAVITY_RESET=0
typeset -g MOCK_CODEX_FRESH=1
typeset -g MOCK_ANTIGRAVITY_FRESH=1

run_once() {
  (( RUN_COUNT += 1 ))
  local now
  now="$(/bin/date '+%s')"
  RUN_STARTED_AT="$now"
  write_last_task_at "$now"
  write_last_triggered_window "${MOCK_CODEX_RESET}:${MOCK_ANTIGRAVITY_RESET}"
  schedule_next_from_resets $(( now + RUN_INTERVAL_SECONDS ))
}

collect_effective_quotas() {
  ensure_temp_dir
  CODEX_QUOTA_IS_FRESH="$MOCK_CODEX_FRESH"
  ANTIGRAVITY_QUOTA_IS_FRESH="$MOCK_ANTIGRAVITY_FRESH"
  if (( MOCK_CODEX_FRESH == 1 )); then
    print -r -- '{"source":"CodexBar · cli","fiveHour":{"remainingPercent":99,"resetAt":'$MOCK_CODEX_RESET'},"weekly":{"remainingPercent":90,"resetAt":'$(( MOCK_CODEX_RESET + 500000 ))'}}' >"$CODEX_QUOTA_NORMALIZED_FILE"
  else
    print -r -- '{"source":"Pi 响应头快照","fiveHour":{"remainingPercent":50,"resetAt":'$MOCK_CODEX_RESET'},"weekly":{"remainingPercent":50,"resetAt":'$(( MOCK_CODEX_RESET + 500000 ))'}}' >"$CODEX_QUOTA_NORMALIZED_FILE"
  fi
  if (( MOCK_ANTIGRAVITY_FRESH == 1 )); then
    print -r -- '{"source":"CodexBar · cli","fiveHour":{"remainingPercent":99,"resetAt":'$MOCK_ANTIGRAVITY_RESET'},"weekly":{"remainingPercent":90,"resetAt":'$(( MOCK_ANTIGRAVITY_RESET + 500000 ))'}}' >"$ANTIGRAVITY_QUOTA_NORMALIZED_FILE"
  else
    print -r -- '{"source":"Pi Antigravity API 快照","fiveHour":{"remainingPercent":50,"resetAt":'$MOCK_ANTIGRAVITY_RESET'},"weekly":{"remainingPercent":50,"resetAt":'$(( MOCK_ANTIGRAVITY_RESET + 500000 ))'}}' >"$ANTIGRAVITY_QUOTA_NORMALIZED_FILE"
  fi
}

base_now="$(/bin/date '+%s')"

# Case 1: New window, remaining = 4h59m (17940s), not executed -> Executes.
RUN_COUNT=0
MOCK_CODEX_RESET=$(( base_now + 17940 ))
MOCK_ANTIGRAVITY_RESET=$(( base_now + 17940 ))
check_schedule
if (( RUN_COUNT != 1 )); then
  print -u2 -r -- "Case 1 failed: Expected run_once on fresh new window, got RUN_COUNT=$RUN_COUNT"
  exit 1
fi
print -r -- "Case 1 (Fresh new window triggers): passed"

# Case 2: Same reset_at, already executed -> Does not repeat.
check_schedule
if (( RUN_COUNT != 1 )); then
  print -u2 -r -- "Case 2 failed: Repeated run on identical window"
  exit 1
fi
print -r -- "Case 2 (Duplicate window prevented): passed"

# Case 3: Process restart / state reloaded with same window -> Still does not repeat.
if [[ "$(read_last_triggered_window)" != "${MOCK_CODEX_RESET}:${MOCK_ANTIGRAVITY_RESET}" ]]; then
  print -u2 -r -- "Case 3 failed: Window state was not persisted"
  exit 1
fi
check_schedule
if (( RUN_COUNT != 1 )); then
  print -u2 -r -- "Case 3 failed: Repeated run after reload"
  exit 1
fi
print -r -- "Case 3 (Persistence across reloads): passed"

# Case 4: reset_at changes (new window), but min interval not reached -> Waits.
MOCK_CODEX_RESET=$(( base_now + 17950 ))
MOCK_ANTIGRAVITY_RESET=$(( base_now + 17950 ))
check_schedule
if (( RUN_COUNT != 1 )); then
  print -u2 -r -- "Case 4 failed: Ran before minimum interval elapsed"
  exit 1
fi
print -r -- "Case 4 (Minimum interval respected): passed"

# Case 5: Min interval satisfied + reset_at changed -> Executes new window.
# Simulate time passing beyond RUN_INTERVAL_SECONDS
write_last_task_at $(( base_now - RUN_INTERVAL_SECONDS - 10 ))
check_schedule
if (( RUN_COUNT != 2 )); then
  print -u2 -r -- "Case 5 failed: Did not run on new window when interval satisfied"
  exit 1
fi
print -r -- "Case 5 (New window execution after interval): passed"

# Case 6: Missed 4h55m poll (e.g. system woke up at 4h40m remaining) -> Recovers and triggers once.
write_last_task_at $(( base_now - RUN_INTERVAL_SECONDS - 10 ))
MOCK_CODEX_RESET=$(( base_now + 16800 ))       # 4h40m remaining
MOCK_ANTIGRAVITY_RESET=$(( base_now + 16800 ))
check_schedule
if (( RUN_COUNT != 3 )); then
  print -u2 -r -- "Case 6 failed: Missed window recovery did not trigger"
  exit 1
fi
check_schedule
if (( RUN_COUNT != 3 )); then
  print -u2 -r -- "Case 6 failed: Missed window recovery repeated unexpectedly"
  exit 1
fi
print -r -- "Case 6 (Missed poll recovery triggered once): passed"

# Case 7: API temporary anomaly (reset in past or corrupted) -> Does not trigger.
write_last_task_at $(( base_now - RUN_INTERVAL_SECONDS - 10 ))
MOCK_CODEX_RESET=$(( base_now - 100 )) # Past reset timestamp
MOCK_ANTIGRAVITY_RESET=$(( base_now - 100 ))
check_schedule
if (( RUN_COUNT != 3 )); then
  print -u2 -r -- "Case 7 failed: Ran on invalid past reset timestamp"
  exit 1
fi
print -r -- "Case 7 (Anomalous reset protected): passed"

# Case 8: Live probe failed, fallback only -> Does not trigger task.
MOCK_CODEX_FRESH=0
MOCK_ANTIGRAVITY_FRESH=0
MOCK_CODEX_RESET=$(( base_now + 17940 ))
MOCK_ANTIGRAVITY_RESET=$(( base_now + 17940 ))
check_schedule
if (( RUN_COUNT != 3 )); then
  print -u2 -r -- "Case 8 failed: Stale fallback triggered task run"
  exit 1
fi
print -r -- "Case 8 (Stale fallback prevents false task trigger): passed"

# Case 9: Read-only /usage query does not alter scheduler state.
write_last_task_at 123456
write_last_triggered_window "saved-window"
write_next_due 999999
send_usage_notification >/dev/null 2>&1 || true
if [[ "$(read_last_task_at)" != "123456" ]] ||
   [[ "$(read_last_triggered_window)" != "saved-window" ]] ||
   [[ "$(read_next_due)" != "999999" ]]; then
  print -u2 -r -- "Case 9 failed: /usage modified scheduler state!"
  exit 1
fi
print -r -- "Case 9 (/usage is strictly read-only and does not touch scheduler): passed"

cleanup
print -r -- "dynamic schedule regression: all 9 cases passed"
