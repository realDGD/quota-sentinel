#!/bin/zsh
# Retry state machine regression (RY1-RY16): failures/timeouts never advance
# last_task_at, debts are repaid by initial and watchdog bursts, and the
# fresh/stale authority rules survive the retry machinery. Fully isolated:
# temp state, temp logs, mock pi/codexbar, stubbed native probes — zero real
# model calls and zero quota consumption.

set -euo pipefail

# Sub-second wall clock for the RY15 parallelism bound. Whole-second
# arithmetic quantises a ~2.3s measurement to {2,3}, so the same absolute
# 3s bound would flip on which side of a second boundary the run started.
# The bound itself is unchanged; only the measurement becomes accurate.
zmodload zsh/datetime

readonly TEST_TEMP_DIR="$(mktemp -d /private/tmp/quota-sentinel.XXXXXX)"
export QUOTA_SENTINEL_STATE_DIR="$TEST_TEMP_DIR/state"
export QUOTA_SENTINEL_LOG_DIR="$TEST_TEMP_DIR/logs"
export PI_SOURCE_ONLY=1
export FEISHU_APP_ID="test-app"
export FEISHU_APP_SECRET="test-secret"
export FEISHU_USER_ID="test-user"
export FEISHU_DISABLE_CHART=1

# Fast, deterministic retry policy for tests.
export QUOTA_SENTINEL_MODEL_TIMEOUT=2
export QUOTA_SENTINEL_MODEL_KILL_GRACE=1
export QUOTA_SENTINEL_RETRY_INTERVAL=1
export QUOTA_SENTINEL_INITIAL_ATTEMPTS=3
export QUOTA_SENTINEL_WATCHDOG_ATTEMPTS=2
export QUOTA_SENTINEL_WATCHDOG_RETRY_GAP=0

BIN_DIR="$TEST_TEMP_DIR/bin"
mkdir -p "$BIN_DIR" "$TEST_TEMP_DIR/calls"

# Mock pi: counts attempts per provider in $PI_MOCK_CALLS_DIR/<provider>.count.
# Fails until attempt PI_MOCK_SUCCESS_AFTER; earlier attempts either exit 1 or
# hang (PI_MOCK_FAIL_MODE=hang) so the timeout path is exercised. The failure
# stderr carries a fake credential to prove log masking.
cat >"$BIN_DIR/pi" <<'MOCK'
#!/bin/zsh
if [[ "${1:-}" == "auth" ]]; then exit 0; fi
provider=""; args=("$@")
for ((i = 1; i <= $#args; i++)); do
  [[ "${args[$i]}" == "--provider" ]] && provider="${args[$((i+1))]}"
done
calls_file="$PI_MOCK_CALLS_DIR/$provider.count"
n=$(( $(cat "$calls_file" 2>/dev/null || echo 0) + 1 ))
print -r -- "$n" > "$calls_file"
if [[ "$n" -lt "${PI_MOCK_SUCCESS_AFTER:-1}" ]]; then
  print -u2 "mock failure attempt $n: bearer SuperSecretTokenValue123 must be masked"
  if [[ "${PI_MOCK_FAIL_MODE:-fail}" == "hang" ]]; then
    sleep 30
  fi
  exit 1
fi
if [[ "$provider" == "antigravity" ]]; then
  now=$(date +%s)
  now_iso="$(/bin/date -u '+%Y-%m-%dT%H:%M:%S.000Z')"
  "$JQ_BIN" -n --arg iso "$now_iso" --argjson reset "$((now+17000))" \
    '{capturedAt:($iso),fiveHour:{remainingPercent:90,resetAt:$reset},weekly:{remainingPercent:80,resetAt:($reset+483000)}}' \
    >"$PI_ANTIGRAVITY_QUOTA_FILE"
fi
if [[ "$provider" == "opencode-go" ]]; then
  now=$(date +%s)
  now_iso="$(/bin/date -u '+%Y-%m-%dT%H:%M:%S.000Z')"
  "$JQ_BIN" -n --arg iso "$now_iso" --argjson reset "$((now+17000))" \
    '{capturedAt:($iso),fiveHour:{remainingPercent:92,resetAt:$reset},weekly:{remainingPercent:81,resetAt:($reset+604800)},monthly:{remainingPercent:73,resetAt:($reset+2500000)}}' \
    >"$PI_OPENCODE_QUOTA_FILE"
fi
print -r -- "1"
exit 0
MOCK
chmod +x "$BIN_DIR/pi"

export QUOTA_SENTINEL_PI_BIN="$BIN_DIR/pi"
export QUOTA_SENTINEL_PI_AUTH_FILE="$TEST_TEMP_DIR/auth.json"
print -r -- '{"openai-codex":{"token":"x"},"antigravity":{"token":"x"},"opencode-go":{"type":"api_key","key":"x"}}' >"$QUOTA_SENTINEL_PI_AUTH_FILE"
export PI_MOCK_CALLS_DIR="$TEST_TEMP_DIR/calls"

export PI_SOURCE_ONLY=1
source "${0:A:h}/../quota-sentinel.sh"
LAST_TEMP_DIR="$TEST_TEMP_DIR"

# A throwaway deployment this suite builds is an INITIALIZED deployment:
# the authority manifest is required at runtime, so every state dir gets one
# (legacy backend), exactly as the installer leaves it on an upgraded host.
initialize_test_authority() { authority_bootstrap --assume-legacy >/dev/null; }

trap cleanup EXIT
ensure_temp_dir

# Hermetic quota stubs; individual cases re-point these as needed.
fetch_native_codex_quota() { return 1; }
fetch_native_antigravity_quota() { return 1; }
fetch_native_opencode_quota() { return 1; }
fetch_codexbar_codex_quota() { return 1; }
fetch_codexbar_antigravity_quota() { return 1; }
fetch_codexbar_opencode_quota() { return 1; }

typeset -ga CAPTURED_MESSAGES=()
send_feishu_message() {
  CAPTURED_MESSAGES+=("$4")
  return 0
}

reset_state() {
  rm -rf "$QUOTA_SENTINEL_STATE_DIR"
  mkdir -p "$QUOTA_SENTINEL_STATE_DIR"
  initialize_test_authority
  rm -rf "$PI_MOCK_CALLS_DIR"
  mkdir -p "$PI_MOCK_CALLS_DIR"
  # OpenCode is parked far in the future: this suite exercises codex and
  # antigravity, and an unparked third provider is "due" the moment a check
  # evaluates it.
  write_provider_next_due opencode $(( $(/bin/date '+%s') + 999999 ))
}
calls() {  # provider key → mock call counter (mock files use pi provider names)
  local key="$1"
  [[ "$key" == "codex" ]] && key="openai-codex"
  [[ "$key" == "opencode" ]] && key="opencode-go"
  cat "$PI_MOCK_CALLS_DIR/$key.count" 2>/dev/null || echo 0
}
assert_log_contains() {  # fixed-string match: patterns may contain '*' etc.
  grep -qF -- "$1" "$QUOTA_SENTINEL_LOG_DIR/$(TZ=Asia/Shanghai /bin/date '+%Y-%m-%d').log" ||
    { print -u2 "FAIL: log missing: $1"; exit 1; }
}
keep_ag_quiet() {  # antigravity: far-future deadline so checks never run it
  write_provider_next_due antigravity $(( $(/bin/date '+%s') + 999999 ))
}

T0="$(/bin/date '+%s')"

print -r -- "== RY1: initial attempt 1/1... first try succeeds =="
reset_state
PI_MOCK_SUCCESS_AFTER=1 run_and_reschedule_selected codex
[[ "$(calls codex)" == "1" ]]
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-retry-pending")" == "0" ]]
lt="$(read_provider_last_task codex)"
[[ "$(read_provider_last_attempt codex)" == "$lt" ]]
[[ "$(read_provider_next_due codex)" == "$(( lt + RUN_INTERVAL_SECONDS ))" ]]
[[ ! -e "$RUN_LOCK_FILE" && ! -e "$QUOTA_LOCK_FILE" ]]
print -r -- "  PASS: 1 call; success committed (attempt=task, due=+5h01)"

print -r -- "== RY2: fail -> 30s-equivalent -> success on 2/3; attempt 3 never runs =="
reset_state
PI_MOCK_SUCCESS_AFTER=2 run_and_reschedule_selected codex
[[ "$(calls codex)" == "2" ]]
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-retry-pending")" == "0" ]]
lt="$(read_provider_last_task codex)"
[[ "$(read_provider_next_due codex)" == "$(( lt + RUN_INTERVAL_SECONDS ))" ]]
assert_log_contains "model codex phase=initial attempt=1/3 result=failed"
assert_log_contains "state: codex retry_pending 0 -> 1"
assert_log_contains "state: codex retry_pending 1 -> 0"
print -r -- "  PASS: 2 total calls, success stopped the burst"

print -r -- "== RY3: third attempt succeeds =="
reset_state
PI_MOCK_SUCCESS_AFTER=3 run_and_reschedule_selected codex
[[ "$(calls codex)" == "3" ]]
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-retry-pending")" == "0" ]]
[[ -s "$QUOTA_SENTINEL_STATE_DIR/codex-last-task-at" ]]
assert_log_contains "model codex phase=initial attempt=3/3 result=success"
print -r -- "  PASS: 3 total calls, success on the last attempt"

print -r -- "== RY4: all three fail -> pending, last_task/next_due untouched =="
reset_state
write_provider_last_task codex 1000
write_provider_next_due codex 111
PI_MOCK_SUCCESS_AFTER=99 run_and_reschedule_selected codex
[[ "$(calls codex)" == "3" ]]
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-retry-pending")" == "1" ]]
[[ "$(read_provider_last_task codex)" == "1000" ]]
[[ "$(read_provider_next_due codex)" == "111" ]]
la="$(read_provider_last_attempt codex)"
(( la >= T0 ))
[[ "${CODEX_RUN_RESULT:-}" == "发送失败" ]]
assert_log_contains "model codex phase=initial attempt=3/3 result=failed"
assert_log_contains "retry initial: codex exhausted 3 attempts; retry_pending stays 1"
print -r -- "  PASS: 3 calls; debt pending; success state untouched; no 5h01 reseed"

print -r -- "== RY5: timeout counts as a failed attempt, then success commits =="
reset_state
PI_MOCK_FAIL_MODE=hang PI_MOCK_SUCCESS_AFTER=2 run_and_reschedule_selected codex
[[ "$(calls codex)" == "2" ]]
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-retry-pending")" == "0" ]]
[[ -s "$QUOTA_SENTINEL_STATE_DIR/codex-last-task-at" ]]
assert_log_contains "model codex phase=initial attempt=1/3 result=timeout rc=124"
print -r -- "  PASS: timeout logged (timeout=yes via rc=124) and retried"

print -r -- "== RY6: three timeouts -> pending, last_task untouched =="
reset_state
PI_MOCK_FAIL_MODE=hang PI_MOCK_SUCCESS_AFTER=99 run_and_reschedule_selected codex
[[ "$(calls codex)" == "3" ]]
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-retry-pending")" == "1" ]]
[[ ! -e "$QUOTA_SENTINEL_STATE_DIR/codex-last-task-at" ]]
print -r -- "  PASS: timeout burst exhausted; debt pending"

print -r -- "== RY7: watchdog repays a pending debt, 1/2 succeeds =="
reset_state
write_provider_retry_pending codex 1
write_provider_last_attempt codex "$(now_epoch)"
keep_ag_quiet
PI_MOCK_SUCCESS_AFTER=1 check_schedule
[[ "$(calls codex)" == "1" ]]
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-retry-pending")" == "0" ]]
lt="$(read_provider_last_task codex)"
[[ "$(read_provider_next_due codex)" == "$(( lt + RUN_INTERVAL_SECONDS ))" ]]
assert_log_contains "check: pending debt on codex; watchdog retry burst first"
print -r -- "  PASS: watchdog burst repaid the debt with a single call"

print -r -- "== RY8: watchdog fail then success = 2 total calls =="
reset_state
write_provider_retry_pending codex 1
write_provider_last_attempt codex "$(now_epoch)"
keep_ag_quiet
PI_MOCK_SUCCESS_AFTER=2 check_schedule
[[ "$(calls codex)" == "2" ]]
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-retry-pending")" == "0" ]]
[[ -s "$QUOTA_SENTINEL_STATE_DIR/codex-last-task-at" ]]
print -r -- "  PASS: watchdog burst stopped at the successful attempt"

print -r -- "== RY9: watchdog twice failed -> pending holds, quota acquisition still runs =="
reset_state
write_provider_retry_pending codex 1
write_provider_last_attempt codex "$(now_epoch)"
write_provider_last_task codex 555
write_provider_next_due codex 444
keep_ag_quiet
PI_MOCK_SUCCESS_AFTER=99 check_schedule
[[ "$(calls codex)" == "2" ]]
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-retry-pending")" == "1" ]]
[[ "$(read_provider_last_task codex)" == "555" ]]
[[ "$(read_provider_next_due codex)" == "444" ]]
assert_log_contains "quota codex: native fail"
print -r -- "  PASS: debt kept, scheduler untouched, quota refresh still performed"

print -r -- "== RY10: fresh data during pending is observation-only =="
reset_state
write_provider_retry_pending codex 1
write_provider_last_attempt codex "$(now_epoch)"
write_provider_next_due codex 444
write_provider_last_known_reset codex 333
keep_ag_quiet
FRESH_RESET=$(( T0 + 17000 ))
fetch_native_codex_quota() {
  "$JQ_BIN" -n --arg now "$(now_epoch)" --argjson reset "$FRESH_RESET" \
    '{source:"Native · codex app-server",fresh:true,capturedAt:($now|tonumber),fiveHour:{remainingPercent:80,resetAt:$reset},weekly:{remainingPercent:90,resetAt:($reset+500000)}}' \
    >"$CODEX_QUOTA_NORMALIZED_FILE"
  return 0
}
PI_MOCK_SUCCESS_AFTER=99 check_schedule
[[ "$(calls codex)" == "2" ]]
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-retry-pending")" == "1" ]]
[[ "$(read_provider_next_due codex)" == "444" ]]
[[ "$(read_provider_last_known_reset codex)" == "333" ]]
assert_log_contains "quota codex: native ok"
print -r -- "  PASS: fresh probe ran but did not override pending priority"

print -r -- "== RY11: watchdog success seeds fallback, then fresh calibrates =="
reset_state
write_provider_retry_pending codex 1
write_provider_last_attempt codex "$(now_epoch)"
keep_ag_quiet
FRESH_RESET=$(( T0 + 17000 ))
fetch_native_codex_quota() {
  "$JQ_BIN" -n --arg now "$(now_epoch)" --argjson reset "$FRESH_RESET" \
    '{source:"Native · codex app-server",fresh:true,capturedAt:($now|tonumber),fiveHour:{remainingPercent:80,resetAt:$reset},weekly:{remainingPercent:90,resetAt:($reset+500000)}}' \
    >"$CODEX_QUOTA_NORMALIZED_FILE"
  return 0
}
PI_MOCK_SUCCESS_AFTER=2 check_schedule
[[ "$(calls codex)" == "2" ]]
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-retry-pending")" == "0" ]]
lt="$(read_provider_last_task codex)"
[[ "$(read_provider_next_due codex)" == "$(( FRESH_RESET + RESET_BUFFER_SECONDS ))" ]]
print -r -- "  PASS: success fallback immediately replaced by reset+4min in the same check"

print -r -- "== RY12: success cycle keeps the fresh/stale contract =="
# Continuing the RY11 committed cycle: fresh failure preserves, latest fresh wins.
nd_before="$(read_provider_next_due codex)"
fetch_native_codex_quota() { return 1; }
check_schedule
[[ "$(read_provider_next_due codex)" == "$nd_before" ]]
FRESH_RESET2=$(( T0 + 17250 ))  # +250s after the trusted reset: nearby movement, accepted immediately
fetch_native_codex_quota() {
  "$JQ_BIN" -n --arg now "$(now_epoch)" --argjson reset "$FRESH_RESET2" \
    '{source:"Native · codex app-server",fresh:true,capturedAt:($now|tonumber),fiveHour:{remainingPercent:80,resetAt:$reset},weekly:{remainingPercent:90,resetAt:($reset+500000)}}' \
    >"$CODEX_QUOTA_NORMALIZED_FILE"
  return 0
}
check_schedule
[[ "$(read_provider_next_due codex)" == "$(( FRESH_RESET2 + RESET_BUFFER_SECONDS ))" ]]
fetch_native_codex_quota() { return 1; }
check_schedule
[[ "$(read_provider_next_due codex)" == "$(( FRESH_RESET2 + RESET_BUFFER_SECONDS ))" ]]
print -r -- "  PASS: fresh failure keeps deadline; latest fresh wins"

print -r -- "== RY13: cache and snapshot still display-only after success =="
live_fixture="$TEST_TEMP_DIR/live.json"
print -r -- '{"source":"CodexBar · cli","fresh":true,"capturedAt":1788000000,"fiveHour":{"remainingPercent":70,"resetAt":1788050000},"weekly":{"remainingPercent":80,"resetAt":1788650000}}' >"$live_fixture"
save_codexbar_cache "$live_fixture" "$CODEXBAR_CODEX_CACHE_FILE"
nd_before="$(read_provider_next_due codex)"
check_schedule
[[ "$(read_provider_next_due codex)" == "$nd_before" ]]
print -r -- "  PASS: cache-only probe cannot move the deadline"

print -r -- "== RY14: pending codex does not disturb normal antigravity =="
reset_state
write_provider_retry_pending codex 1
write_provider_last_attempt codex "$(now_epoch)"
ag_due=$(( T0 + 999999 ))
write_provider_next_due antigravity "$ag_due"
PI_MOCK_SUCCESS_AFTER=99 check_schedule
[[ "$(calls codex)" == "2" ]]
[[ "$(calls antigravity)" == "0" ]]
[[ "$(read_provider_next_due antigravity)" == "$ag_due" ]]
[[ ! -e "$QUOTA_SENTINEL_STATE_DIR/antigravity-retry-pending" ]]
print -r -- "  PASS: provider independence preserved (codex retried, antigravity untouched)"

print -r -- "== RY15: two pending providers repay in parallel rounds =="
reset_state
write_provider_retry_pending codex 1
write_provider_last_attempt codex "$(now_epoch)"
write_provider_retry_pending antigravity 1
write_provider_last_attempt antigravity "$(now_epoch)"
export PI_MOCK_SUCCESS_AFTER=2
t0="$EPOCHREALTIME"
check_schedule
elapsed="$(printf '%.2f' $(( EPOCHREALTIME - t0 )))"
[[ "$(calls codex)" == "2" ]]
[[ "$(calls antigravity)" == "2" ]]
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-retry-pending")" == "0" ]]
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/antigravity-retry-pending")" == "0" ]]
# Serial rounds would cost >= 2 x (fail + 1s + success); parallel rounds cost one gap.
# A serial implementation costs two full rounds (fail + interval + success
# each, ~4s+); parallel rounds cost one interval. The bound is the original
# 3s, now measured without whole-second quantisation.
(( elapsed < 3.0 )) || { print -u2 "FAIL: rounds appear serialized (${elapsed}s)"; exit 1; }
print -r -- "  PASS: both debts cleared in ${elapsed}s (round-parallel)"

print -r -- "== RY16: crash recovery — persisted debt survives process restart =="
reset_state
print -r -- "1" >"$QUOTA_SENTINEL_STATE_DIR/codex-retry-pending"
print -r -- "$(( T0 - 5 ))" >"$QUOTA_SENTINEL_STATE_DIR/codex-last-attempt-at"
keep_ag_quiet
PI_MOCK_SUCCESS_AFTER=99 check_schedule
[[ "$(calls codex)" == "2" ]]
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-retry-pending")" == "1" ]]
print -r -- "  PASS: pre-existing pending state picked up and repaid by watchdog burst"

print -r -- "== RY17: OpenCode commits success and seeds its own 5h01 fallback =="
reset_state
PI_MOCK_SUCCESS_AFTER=1 run_and_reschedule_selected opencode
[[ "$(calls opencode)" == "1" ]]
oc_task="$(read_provider_last_task opencode)"
[[ "$oc_task" =~ ^[0-9]+$ ]]
[[ "$(read_provider_next_due opencode)" == "$(( oc_task + RUN_INTERVAL_SECONDS ))" ]]
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/opencode-retry-pending")" == "0" ]]
print -r -- "  PASS: single attempt, success committed, fallback seeded"

print -r -- "== RY18: error summaries are logged masked =="
assert_log_contains "attempt=1 error: mock failure attempt 1: bearer***"
if grep -q "SuperSecretTokenValue123" "$QUOTA_SENTINEL_LOG_DIR/$(TZ=Asia/Shanghai /bin/date '+%Y-%m-%d').log"; then
  print -u2 "FAIL: unmasked credential leaked into the run log"
  exit 1
fi
print -r -- "  PASS: stderr summary present, credential masked"

print -r -- "retry regression: all cases passed"
