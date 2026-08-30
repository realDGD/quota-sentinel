#!/bin/zsh

set -euo pipefail

readonly TEST_TEMP_DIR="$(mktemp -d /private/tmp/quota-sentinel.XXXXXX)"
export QUOTA_SENTINEL_STATE_DIR="$TEST_TEMP_DIR/state"
export PI_SOURCE_ONLY=1
source "${0:A:h}/../quota-sentinel.sh"
LAST_TEMP_DIR="$TEST_TEMP_DIR"
trap cleanup EXIT

is_due 100
write_next_due 200
[[ "$(read_next_due)" == "200" ]]
[[ "$(stat -f '%Lp' "$(provider_next_due_file "codex")")" == "600" ]]
[[ "$(stat -f '%Lp' "$(provider_next_due_file "antigravity")")" == "600" ]]

if is_due 199; then
  print -u2 -r -- "schedule regression: future timestamp was treated as due"
  exit 1
fi
is_due 200
is_due 201

schedule_next_after_run 201
[[ "$(read_next_due)" == "18261" ]]
(( $(read_next_due) - 201 == RUN_INTERVAL_SECONDS ))

schedule_next_after_run 18261
[[ "$(read_next_due)" == "36321" ]]
(( $(read_next_due) - 18261 == RUN_INTERVAL_SECONDS ))

acquire_run_lock
(( RUN_LOCK_HELD == 1 ))
[[ -f "$RUN_LOCK_FILE" ]]
[[ "$(stat -f '%Lp' "$RUN_LOCK_FILE")" == "600" ]]
release_run_lock
[[ ! -e "$RUN_LOCK_FILE" ]]

typeset -gi RUN_COUNT=0
run_selected_providers() {
  (( RUN_COUNT += 1 ))
  local now
  now="$(/bin/date '+%s')"
  RUN_STARTED_AT="$now"
  for p in "$@"; do
    write_provider_last_task "$p" "$now"
    write_provider_last_window "$p" "$(( now + 17900 ))"
    write_provider_next_due "$p" $(( now + RUN_INTERVAL_SECONDS ))
  done
}

collect_effective_quotas() {
  ensure_temp_dir
  CODEX_QUOTA_IS_FRESH=1
  ANTIGRAVITY_QUOTA_IS_FRESH=1
  local now
  now="$(/bin/date '+%s')"
  print -r -- '{"source":"CodexBar · cli","fiveHour":{"remainingPercent":90,"resetAt":'$(( now + 17900 ))'},"weekly":{"remainingPercent":90,"resetAt":'$(( now + 500000 ))'}}' >"$CODEX_QUOTA_NORMALIZED_FILE"
  print -r -- '{"source":"CodexBar · cli","fiveHour":{"remainingPercent":90,"resetAt":'$(( now + 17900 ))'},"weekly":{"remainingPercent":90,"resetAt":'$(( now + 500000 ))'}}' >"$ANTIGRAVITY_QUOTA_NORMALIZED_FILE"
}

before_check="$(/bin/date '+%s')"
write_next_due $(( before_check - 1 ))
check_schedule
after_check="$(/bin/date '+%s')"
scheduled_due="$(read_next_due)"
(( RUN_COUNT == 1 ))
(( scheduled_due >= before_check + RUN_INTERVAL_SECONDS ))
(( scheduled_due <= after_check + RUN_INTERVAL_SECONDS ))

check_schedule
(( RUN_COUNT == 1 ))

print -r -- "schedule regression: ok"
