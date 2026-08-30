#!/bin/zsh
# P1-1 deadline regression: a reset-based deadline becomes committed when its
# reset occurs and stays blocked through its four-minute buffer, due execution,
# and any retries. Before that reset, Fresh keeps dynamic calibration inside
# the current task's bounded five-hour cycle horizon.
# Maps the live production
# starvation (2026-08-30 18:11:35 due, probe 18:11:39, fresh reset 23:11:38,
# deadline pushed to 23:15:38, burst never ran) onto deterministic cases.
# Fully isolated: temp state/logs, mock pi, stubbed probes — no real models.

set -euo pipefail

readonly TEST_TEMP_DIR="$(mktemp -d /private/tmp/quota-sentinel.XXXXXX)"
export QUOTA_SENTINEL_STATE_DIR="$TEST_TEMP_DIR/state"
export QUOTA_SENTINEL_LOG_DIR="$TEST_TEMP_DIR/logs"
export PI_SOURCE_ONLY=1
export FEISHU_APP_ID="test-app"
export FEISHU_APP_SECRET="test-secret"
export FEISHU_USER_ID="test-user"
export FEISHU_DISABLE_CHART=1

export QUOTA_SENTINEL_MODEL_TIMEOUT=2
export QUOTA_SENTINEL_MODEL_KILL_GRACE=1
export QUOTA_SENTINEL_RETRY_INTERVAL=1
export QUOTA_SENTINEL_INITIAL_ATTEMPTS=3
export QUOTA_SENTINEL_WATCHDOG_ATTEMPTS=2
export QUOTA_SENTINEL_WATCHDOG_RETRY_GAP=0

BIN_DIR="$TEST_TEMP_DIR/bin"
mkdir -p "$BIN_DIR" "$TEST_TEMP_DIR/calls"

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
  print -u2 "mock failure attempt $n"
  exit 1
fi
print -r -- "1"
exit 0
MOCK
chmod +x "$BIN_DIR/pi"

export QUOTA_SENTINEL_PI_BIN="$BIN_DIR/pi"
export QUOTA_SENTINEL_PI_AUTH_FILE="$TEST_TEMP_DIR/auth.json"
print -r -- '{"openai-codex":{},"antigravity":{}}' >"$QUOTA_SENTINEL_PI_AUTH_FILE"
export PI_MOCK_CALLS_DIR="$TEST_TEMP_DIR/calls"

export PI_SOURCE_ONLY=1
source "${0:A:h}/../quota-sentinel.sh"
LAST_TEMP_DIR="$TEST_TEMP_DIR"
trap cleanup EXIT
ensure_temp_dir

fetch_native_codex_quota() { return 1; }
fetch_native_antigravity_quota() { return 1; }
fetch_codexbar_codex_quota() { return 1; }
fetch_codexbar_antigravity_quota() { return 1; }

typeset -ga CAPTURED_MESSAGES=()
send_feishu_message() { CAPTURED_MESSAGES+=("$4"); return 0; }

reset_state() {
  rm -rf "$QUOTA_SENTINEL_STATE_DIR"; mkdir -p "$QUOTA_SENTINEL_STATE_DIR"
  rm -rf "$PI_MOCK_CALLS_DIR"; mkdir -p "$PI_MOCK_CALLS_DIR"
}
calls() {
  local key="$1"; [[ "$key" == "codex" ]] && key="openai-codex"
  cat "$PI_MOCK_CALLS_DIR/$key.count" 2>/dev/null || echo 0
}
assert_log_contains() {
  grep -qF -- "$1" "$QUOTA_SENTINEL_LOG_DIR/$(TZ=Asia/Shanghai /bin/date '+%Y-%m-%d').log" ||
    { print -u2 "FAIL: log missing: $1"; exit 1; }
}
# Write a fresh normalized quota file for the given provider (epoch reset).
set_fresh() {
  local provider="$1" reset="$2" file
  file=$(provider_normalized_quota_file "$provider")
  "$JQ_BIN" -n --arg now "$(/bin/date '+%s')" --argjson reset "$reset" \
    '{source:"Native",fresh:true,capturedAt:($now|tonumber),fiveHour:{remainingPercent:80,resetAt:$reset},weekly:{remainingPercent:90,resetAt:($reset+500000)}}' >"$file"
  return 0
}
keep_ag_quiet() { write_provider_next_due antigravity $(( $(/bin/date '+%s') + 999999 )); }

T="$(/bin/date '+%s')"
ND_FILE="$(provider_next_due_file codex)"

print -r -- "== P1-1A1 (evaluate level): production repro — matured debt beats future fresh =="
reset_state
OLD_DUE=$(( T - 4 ))                 # 18:11:35 analog
FRESH_RESET=$(( T + 18043 ))         # 23:11:38 analog (probe-anchored window)
write_provider_next_due codex "$OLD_DUE"
set_fresh codex "$FRESH_RESET"
evaluate_provider codex "$T" && due=0 || due=1
[[ "$due" == 0 ]]
[[ "$(cat "$ND_FILE")" == "$OLD_DUE" ]]   # §47: deadline not rewritten to the future candidate
assert_log_contains "matured debt due=$OLD_DUE"
assert_log_contains "ignored this round"
print -r -- "  PASS: DUE=true with next_due untouched (old=$OLD_DUE, fresh candidate $FRESH_RESET ignored)"

print -r -- "== P1-1A2 (end to end): matured debt -> real initial burst =="
reset_state
write_provider_next_due codex "$(( T - 4 ))"
keep_ag_quiet
export PI_MOCK_SUCCESS_AFTER=1
check_schedule
[[ "$(calls codex)" == "1" ]]
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-retry-pending")" == "0" ]]
[[ -s "$QUOTA_SENTINEL_STATE_DIR/codex-last-task-at" ]]
assert_log_contains "matured debt due="
print -r -- "  PASS: check_schedule committed the debt and ran the burst"

print -r -- "== P1-1B / V1: before scheduled reset — nearby Fresh may push it later =="
reset_state
OLD_RESET=$(( T + 2760 ))
NEW_RESET=$(( OLD_RESET + 240 ))
write_provider_last_known_reset codex "$OLD_RESET"
write_provider_next_due codex $(( OLD_RESET + RESET_BUFFER_SECONDS ))
set_fresh codex "$NEW_RESET"
evaluate_provider codex "$T" && due=0 || due=1
[[ "$due" == 1 ]]
[[ "$(cat "$ND_FILE")" == "$(( NEW_RESET + RESET_BUFFER_SECONDS ))" ]]
print -r -- "  PASS: NOT_DUE with deadline recalibrated to reset+4min"

print -r -- "== P1-1C / V2: un-matured deadline — fresh may pull it earlier =="
reset_state
write_provider_next_due codex $(( T + 3000 ))
set_fresh codex $(( T + 540 ))
evaluate_provider codex "$T" && due=0 || due=1
[[ "$due" == 1 ]]   # reset>now validity means due lands at T+780: future at T
[[ "$(cat "$ND_FILE")" == "$(( T + 540 + RESET_BUFFER_SECONDS ))" ]]
# Same evaluation later (timer wake): the pulled-earlier deadline has matured.
evaluate_provider codex $(( T + 780 )) && due=0 || due=1
[[ "$due" == 0 ]]
print -r -- "  PASS: fresh-earlier written immediately; fires on the next evaluation"

print -r -- "== P1-1D: matured + fresh fail -> DUE, deadline untouched =="
reset_state
rm -f "$CODEX_QUOTA_NORMALIZED_FILE" "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE"  # no probe data at all
write_provider_next_due codex $(( T - 4 ))
evaluate_provider codex "$T" && due=0 || due=1
[[ "$due" == 0 ]]
[[ "$(cat "$ND_FILE")" == "$(( T - 4 ))" ]]
assert_log_contains "no valid fresh data"
print -r -- "  PASS: DUE=true without any fresh data"

print -r -- "== P1-1E: matured + stale cache future reset -> DUE =="
reset_state
write_provider_next_due codex $(( T - 4 ))
"$JQ_BIN" -n --argjson r "$(( T + 19000 ))" '{source:"CodexBar · cached",fresh:false,cached:true,capturedAt:1,fiveHour:{remainingPercent:50,resetAt:$r},weekly:{remainingPercent:50,resetAt:$r}}' >"$CODEX_QUOTA_NORMALIZED_FILE"
evaluate_provider codex "$T" && due=0 || due=1
[[ "$due" == 0 ]]
[[ "$(cat "$ND_FILE")" == "$(( T - 4 ))" ]]
print -r -- "  PASS: stale future reset cannot cancel the matured debt"

print -r -- "== P1-1F: matured -> burst success -> new cycle fresh calibration =="
reset_state
write_provider_next_due codex $(( T - 4 ))
keep_ag_quiet
R=$(( T + 17900 ))
fetch_native_codex_quota() {
  "$JQ_BIN" -n --arg now "$(date '+%s')" --argjson r "$R" \
    '{source:"Native",fresh:true,capturedAt:($now|tonumber),fiveHour:{remainingPercent:80,resetAt:$r},weekly:{remainingPercent:90,resetAt:($r+500000)}}' >"$CODEX_QUOTA_NORMALIZED_FILE"
  return 0
}
export PI_MOCK_SUCCESS_AFTER=1
check_schedule
[[ "$(calls codex)" == "1" ]]
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-retry-pending")" == "0" ]]
lt="$(read_provider_last_task codex)"
nd="$(read_provider_next_due codex)"
[[ "$nd" == "$(( R + RESET_BUFFER_SECONDS ))" ]]
print -r -- "  PASS: success committed (last_task=$lt) and fresh calibration took over (due=reset+4m)"

print -r -- "== P1-1G: matured -> burst all fail -> debt survives fresh =="
reset_state
write_provider_next_due codex $(( T - 4 ))
keep_ag_quiet
R=$(( T + 17900 ))
fetch_native_codex_quota() {
  "$JQ_BIN" -n --arg now "$(date '+%s')" --argjson r "$R" \
    '{source:"Native",fresh:true,capturedAt:($now|tonumber),fiveHour:{remainingPercent:80,resetAt:$r},weekly:{remainingPercent:90,resetAt:($r+500000)}}' >"$CODEX_QUOTA_NORMALIZED_FILE"
  return 0
}
export PI_MOCK_SUCCESS_AFTER=99
check_schedule            # initial burst: 3 attempts, all fail
[[ "$(calls codex)" == "3" ]]
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-retry-pending")" == "1" ]]
[[ "$(read_provider_next_due codex)" == "$(( T - 4 ))" ]]
check_schedule            # watchdog: 2 more attempts; fresh must not cancel the debt
[[ "$(calls codex)" == "5" ]]
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-retry-pending")" == "1" ]]
[[ "$(read_provider_next_due codex)" == "$(( T - 4 ))" ]]
print -r -- "  PASS: 3+2 attempts; pending survives; deadline never rewritten by fresh"

print -r -- "== P1-1H: pending + future fresh -> watchdog retry first =="
reset_state
write_provider_retry_pending codex 1
write_provider_last_attempt codex "$T"
write_provider_next_due codex $(( T - 4 ))
keep_ag_quiet
R=$(( T + 17900 ))
fetch_native_codex_quota() {
  "$JQ_BIN" -n --arg now "$(date '+%s')" --argjson r "$R" \
    '{source:"Native",fresh:true,capturedAt:($now|tonumber),fiveHour:{remainingPercent:80,resetAt:$r},weekly:{remainingPercent:90,resetAt:($r+500000)}}' >"$CODEX_QUOTA_NORMALIZED_FILE"
  return 0
}
export PI_MOCK_SUCCESS_AFTER=99
check_schedule
[[ "$(calls codex)" == "2" ]]
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-retry-pending")" == "1" ]]
[[ "$(read_provider_next_due codex)" == "$(( T - 4 ))" ]]
assert_log_contains "pending debt on codex; watchdog retry burst first"
print -r -- "  PASS: fresh future reset did not suppress the watchdog repayment"

print -r -- "== P1-1I: provider isolation, both directions =="
reset_state
write_provider_next_due codex $(( T - 4 ))          # codex matured
write_provider_next_due antigravity $(( T + 8000 )) # antigravity future
set_fresh codex $(( T + 18000 ))
set_fresh antigravity $(( T + 7000 ))
evaluate_provider codex "$T" && due=0 || due=1
[[ "$due" == 0 ]]
[[ "$(read_provider_next_due codex)" == "$(( T - 4 ))" ]]
evaluate_provider antigravity "$T" && due=0 || due=1
[[ "$due" == 1 ]]
[[ "$(read_provider_next_due antigravity)" == "$(( T + 7000 + RESET_BUFFER_SECONDS ))" ]]
# reverse: antigravity matured, codex future
reset_state
write_provider_next_due codex $(( T + 8000 ))
write_provider_next_due antigravity $(( T - 4 ))
set_fresh codex $(( T + 18000 ))
set_fresh antigravity $(( T + 7000 ))
evaluate_provider codex "$T" && due=0 || due=1
[[ "$due" == 1 ]]
evaluate_provider antigravity "$T" && due=0 || due=1
[[ "$due" == 0 ]]
[[ "$(read_provider_next_due antigravity)" == "$(( T - 4 ))" ]]
print -r -- "  PASS: matured debt fires only for its own provider"

print -r -- "== P1-1J: un-matured deadline + cache-only probe stays untouched =="
reset_state
write_provider_next_due codex $(( T + 3000 ))
prepare_quota_probe   # mirrors production: resets IS_FRESH globals before reads
"$JQ_BIN" -n --argjson r "$(( T + 19000 ))" '{source:"CodexBar · cached",fresh:false,cached:true,capturedAt:1,fiveHour:{remainingPercent:50,resetAt:$r},weekly:{remainingPercent:50,resetAt:$r}}' >"$CODEX_QUOTA_NORMALIZED_FILE"
evaluate_provider codex "$T" && due=0 || due=1
[[ "$due" == 1 ]]
[[ "$(cat "$ND_FILE")" == "$(( T + 3000 ))" ]]
print -r -- "  PASS: stale data still cannot write deadlines in either direction"

print -r -- "== P1-1K: reset crossed but +4m pending — fresh cannot cancel armed deadline =="
reset_state
OLD_RESET=$(( T - 96 ))
OLD_DUE=$(( OLD_RESET + RESET_BUFFER_SECONDS ))
NEW_RESET=$(( T + 18000 ))
write_provider_last_known_reset codex "$OLD_RESET"
write_provider_next_due codex "$OLD_DUE"
set_fresh codex "$NEW_RESET"
evaluate_provider codex "$T" && due=0 || due=1
[[ "$due" == 1 ]]
[[ "$(read_provider_last_known_reset codex)" == "$OLD_RESET" ]]
[[ "$(read_provider_next_due codex)" == "$OLD_DUE" ]]
print -r -- "  PASS: armed reset+4m deadline stays frozen until it fires"

print -r -- "== P1-1L: direct sync (/usage path) also respects the armed deadline =="
reset_state
OLD_RESET=$(( T - 96 ))
OLD_DUE=$(( OLD_RESET + RESET_BUFFER_SECONDS ))
NEW_RESET=$(( T + 18000 ))
write_provider_last_known_reset codex "$OLD_RESET"
write_provider_next_due codex "$OLD_DUE"
set_fresh codex "$NEW_RESET"
sync_rc=0
sync_provider_deadline_from_quota codex "$T" || sync_rc=$?
(( sync_rc != 0 ))
[[ "$(read_provider_last_known_reset codex)" == "$OLD_RESET" ]]
[[ "$(read_provider_next_due codex)" == "$OLD_DUE" ]]
print -r -- "  PASS: /usage may display fresh data but cannot rewrite the armed schedule"

print -r -- "== P1-1M: success releases the block and post-run fresh takes over =="
commit_provider_success codex "$T"
[[ "$(read_provider_next_due codex)" == "$(( T + RUN_INTERVAL_SECONDS ))" ]]
sync_provider_deadline_from_quota codex "$T"
[[ "$(read_provider_last_known_reset codex)" == "$NEW_RESET" ]]
[[ "$(read_provider_next_due codex)" == "$(( NEW_RESET + RESET_BUFFER_SECONDS ))" ]]
print -r -- "  PASS: successful execution unlocks the next fresh cycle"

print -r -- "== P1-1N: direct sync cannot cancel an overdue deadline =="
reset_state
OLD_RESET=$(( T - 400 ))
OLD_DUE=$(( OLD_RESET + RESET_BUFFER_SECONDS ))
NEW_RESET=$(( T + 18000 ))
write_provider_last_known_reset codex "$OLD_RESET"
write_provider_next_due codex "$OLD_DUE"
set_fresh codex "$NEW_RESET"
sync_rc=0
sync_provider_deadline_from_quota codex "$T" || sync_rc=$?
(( sync_rc != 0 ))
[[ "$(read_provider_last_known_reset codex)" == "$OLD_RESET" ]]
[[ "$(read_provider_next_due codex)" == "$OLD_DUE" ]]
print -r -- "  PASS: overdue task debt also blocks /usage scheduler writes"

print -r -- "== P1-1O: rolling Fresh cannot starve either provider =="
for provider in codex antigravity; do
  reset_state
  CYCLE_START="$T"
  EXPECTED_DUE=$(( CYCLE_START + 18000 + RESET_BUFFER_SECONDS ))
  write_provider_last_task "$provider" "$CYCLE_START"
  write_provider_next_due "$provider" $(( CYCLE_START + RUN_INTERVAL_SECONDS ))
  matured=0
  for step in {0..31}; do
    PROBE_NOW=$(( CYCLE_START + step * 900 ))
    set_fresh "$provider" $(( PROBE_NOW + 18000 ))
    if evaluate_provider "$provider" "$PROBE_NOW"; then
      matured=1
      break
    fi
  done
  [[ "$matured" == "1" ]]
  [[ "$(read_provider_next_due "$provider")" == "$EXPECTED_DUE" ]]
  print -r -- "  PASS: $provider matured after bounded rolling probes; due stayed $EXPECTED_DUE"
done

print -r -- "== P1-1P: first fixed Fresh reset may be later than last_task + 5h =="
reset_state
CYCLE_START="$T"
FIXED_RESET=$(( CYCLE_START + 19800 ))
write_provider_last_task codex "$CYCLE_START"
write_provider_next_due codex $(( CYCLE_START + RUN_INTERVAL_SECONDS ))
set_fresh codex "$FIXED_RESET"
sync_provider_deadline_from_quota codex "$CYCLE_START"
[[ "$(read_provider_last_known_reset codex)" == "$FIXED_RESET" ]]
[[ "$(read_provider_next_due codex)" == "$(( FIXED_RESET + RESET_BUFFER_SECONDS ))" ]]
print -r -- "  PASS: fixed Native reset remained authoritative despite the older task anchor"

print -r -- "== P1-1Q: a far-later reset needs stability across probes =="
reset_state
CYCLE_START="$T"
TRUSTED_RESET=$(( CYCLE_START + 3600 ))
FIXED_RESET=$(( CYCLE_START + 19800 ))
write_provider_last_task codex "$CYCLE_START"
write_provider_last_known_reset codex "$TRUSTED_RESET"
write_provider_next_due codex $(( TRUSTED_RESET + RESET_BUFFER_SECONDS ))
set_fresh codex "$FIXED_RESET"
sync_rc=0
sync_provider_deadline_from_quota codex "$CYCLE_START" || sync_rc=$?
(( sync_rc != 0 ))
[[ "$(read_provider_last_known_reset codex)" == "$TRUSTED_RESET" ]]
[[ "$(read_provider_next_due codex)" == "$(( TRUSTED_RESET + RESET_BUFFER_SECONDS ))" ]]
set_fresh codex "$FIXED_RESET"
sync_rc=0
sync_provider_deadline_from_quota codex $(( CYCLE_START + 30 )) || sync_rc=$?
(( sync_rc != 0 ))
[[ "$(read_provider_last_known_reset codex)" == "$TRUSTED_RESET" ]]
set_fresh codex "$FIXED_RESET"
sync_provider_deadline_from_quota codex $(( CYCLE_START + 900 ))
[[ "$(read_provider_last_known_reset codex)" == "$FIXED_RESET" ]]
[[ "$(read_provider_next_due codex)" == "$(( FIXED_RESET + RESET_BUFFER_SECONDS ))" ]]
print -r -- "  PASS: first far movement deferred; unchanged second observation promoted"

print -r -- "== P1-1R: near and earlier reset movement remains immediate =="
reset_state
CYCLE_START="$T"
TRUSTED_RESET=$(( CYCLE_START + 18000 ))
NEAR_RESET=$(( TRUSTED_RESET + 240 ))
EARLY_RESET=$(( CYCLE_START + 15000 ))
write_provider_last_task codex "$CYCLE_START"
write_provider_last_known_reset codex "$TRUSTED_RESET"
write_provider_next_due codex $(( TRUSTED_RESET + RESET_BUFFER_SECONDS ))
set_fresh codex "$NEAR_RESET"
sync_provider_deadline_from_quota codex "$CYCLE_START"
NEAR_DUE=$(( NEAR_RESET + RESET_BUFFER_SECONDS ))
[[ "$(read_provider_last_known_reset codex)" == "$NEAR_RESET" ]]
[[ "$(read_provider_next_due codex)" == "$NEAR_DUE" ]]
set_fresh codex "$EARLY_RESET"
sync_provider_deadline_from_quota codex "$CYCLE_START"
[[ "$(read_provider_last_known_reset codex)" == "$EARLY_RESET" ]]
[[ "$(read_provider_next_due codex)" == "$(( EARLY_RESET + RESET_BUFFER_SECONDS ))" ]]
print -r -- "  PASS: nearby later movement and earlier refresh both stayed dynamic"

print -r -- "== P1-1S: cumulative near movement cannot chase either provider =="
for provider in codex antigravity; do
  reset_state
  CYCLE_START="$T"
  ANCHOR_RESET=$(( CYCLE_START + 18000 ))
  FIRST_NEAR_RESET=$(( ANCHOR_RESET + 240 ))
  write_provider_last_task "$provider" "$CYCLE_START"
  write_provider_last_known_reset "$provider" "$ANCHOR_RESET"
  write_provider_next_due "$provider" $(( ANCHOR_RESET + RESET_BUFFER_SECONDS ))

  # Each individual observation is only +240s from the previously accepted
  # reset, but the sequence drifts far beyond the original window. Comparing
  # only with the last accepted value lets this chase forever at short probe
  # intervals even though no fixed far reset was independently confirmed.
  for step in {1..8}; do
    PROBE_NOW=$(( CYCLE_START + step * 60 ))
    set_fresh "$provider" $(( ANCHOR_RESET + step * 240 ))
    sync_provider_deadline_from_quota "$provider" "$PROBE_NOW" || true
  done

  [[ "$(read_provider_last_known_reset "$provider")" == "$FIRST_NEAR_RESET" ]]
  [[ "$(read_provider_next_due "$provider")" == "$(( FIRST_NEAR_RESET + RESET_BUFFER_SECONDS ))" ]]
  print -r -- "  PASS: $provider kept the first-window bound under cumulative +240s drift"
done

cleanup
print -r -- "p1 deadline regression: all cases passed"
