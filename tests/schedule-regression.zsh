#!/bin/zsh

set -euo pipefail

readonly TEST_TEMP_DIR="$(mktemp -d /private/tmp/quota-sentinel.XXXXXX)"
export QUOTA_SENTINEL_STATE_DIR="$TEST_TEMP_DIR/state"
export QUOTA_SENTINEL_LOG_DIR="$TEST_TEMP_DIR/logs"
export PI_SOURCE_ONLY=1
source "${0:A:h}/../quota-sentinel.sh"
LAST_TEMP_DIR="$TEST_TEMP_DIR"
trap cleanup EXIT

write_provider_next_due codex 300
write_provider_next_due antigravity 200
[[ "$(read_next_due)" == "200" ]]
[[ "$(read_provider_next_due codex)" == "300" ]]
[[ "$(read_provider_next_due antigravity)" == "200" ]]
[[ "$(stat -f '%Lp' "$(provider_next_due_file codex)")" == "600" ]]
[[ "$(stat -f '%Lp' "$(provider_next_due_file antigravity)")" == "600" ]]

write_provider_last_task codex 1000
[[ "$(provider_fallback_due codex 9999)" == "$(( 1000 + RUN_INTERVAL_SECONDS ))" ]]
[[ "$(provider_fallback_due antigravity 9999)" == "9999" ]]

acquire_run_lock
(( RUN_LOCK_HELD == 1 ))
[[ -f "$RUN_LOCK_FILE" ]]
[[ "$(stat -f '%Lp' "$RUN_LOCK_FILE")" == "600" ]]
if "$SHLOCK_BIN" -p "$$" -f "$RUN_LOCK_FILE"; then
  print -u2 -r -- "schedule regression: duplicate run lock was acquired"
  exit 1
fi
release_run_lock
[[ ! -e "$RUN_LOCK_FILE" ]]

acquire_quota_lock
(( QUOTA_LOCK_HELD == 1 ))
[[ -f "$QUOTA_LOCK_FILE" ]]
release_quota_lock
[[ ! -e "$QUOTA_LOCK_FILE" ]]

cleanup
print -r -- "schedule regression: ok"
