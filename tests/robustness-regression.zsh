#!/bin/zsh
# Shell-level robustness regression: process timeouts inside the real run/fetch
# paths, /usage busy reply, capturedAt schema unification, atomic state writes,
# and the run log. Fully isolated: temp state dir, temp log dir, mock binaries,
# zero model calls, zero quota consumption.

set -euo pipefail

readonly TEST_TEMP_DIR="$(mktemp -d /private/tmp/quota-sentinel.XXXXXX)"
export QUOTA_SENTINEL_STATE_DIR="$TEST_TEMP_DIR/state"
export QUOTA_SENTINEL_LOG_DIR="$TEST_TEMP_DIR/logs"
export PI_SOURCE_ONLY=1
export FEISHU_APP_ID="test-app"
export FEISHU_APP_SECRET="test-secret"
export FEISHU_USER_ID="test-user"
export FEISHU_DISABLE_CHART=1

# Aggressive timeouts keep the suite fast; env overrides exist for exactly this.
export QUOTA_SENTINEL_MODEL_TIMEOUT=2
export QUOTA_SENTINEL_MODEL_KILL_GRACE=1
export QUOTA_SENTINEL_CODEXBAR_TIMEOUT=1
export QUOTA_SENTINEL_CODEXBAR_KILL_GRACE=1

BIN_DIR="$TEST_TEMP_DIR/bin"
mkdir -p "$BIN_DIR"

# --- mock binaries -----------------------------------------------------------
# Fake CodexBar, mode via $CODEXBAR_MOCK_MODE: "ok" prints a valid cli payload,
# "fail" exits 1, "hang" spawns a recorded grandchild and hangs (timeout and
# orphan tests). Default "fail" keeps unexpected calls on the fallback path.
cat >"$BIN_DIR/codexbar" <<MOCK
#!/bin/zsh
mode="\${CODEXBAR_MOCK_MODE:-fail}"
if [[ "\$mode" == "hang" ]]; then
  /usr/bin/python3 -c 'import os,time,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)' "\$GRANDCHILD_PID_FILE" &
  sleep 30
  exit 0
fi
if [[ "\$mode" == "fail" ]]; then exit 1; fi
now=\$(/bin/date +%s)
print -r -- '[{"provider":"codex","source":"cli","usage":{"primary":{"windowMinutes":300,"usedPercent":30,"resetsAt":'\$((now+17000))'},"secondary":{"windowMinutes":10080,"usedPercent":20,"resetsAt":'\$((now+500000))'},"extraRateWindows":[]}}]'
MOCK
chmod +x "$BIN_DIR/codexbar"

# Fake pi: `auth` subcommand exits quietly; an antigravity model call replies
# "1" and writes a quota capture (with an ISO capturedAt, exercising the epoch
# conversion on the run path); a codex model call hangs to hit the timeout.
cat >"$BIN_DIR/pi" <<MOCK
#!/bin/zsh
if [[ "\${1:-}" == "auth" ]]; then exit 0; fi
provider=""
args=("\$@")
for ((i = 1; i <= \$#args; i++)); do
  [[ "\${args[\$i]}" == "--provider" ]] && provider="\${args[\$((i+1))]}"
done
if [[ "\$provider" == "openai-codex" ]]; then
  sleep 30
  exit 0
fi
if [[ "\$provider" == "antigravity" ]]; then
  now=\$(/bin/date +%s)
  print -r -- '{"capturedAt":"'"$(/bin/date -u '+%Y-%m-%dT%H:%M:%S.000Z')"'","fiveHour":{"remainingPercent":90,"resetAt":'\$((now+17000))'},"weekly":{"remainingPercent":80,"resetAt":'\$((now+500000))'}}' >"\$PI_ANTIGRAVITY_QUOTA_FILE"
  print -r -- "1"
  exit 0
fi
exit 1
MOCK
chmod +x "$BIN_DIR/pi"

export QUOTA_SENTINEL_PI_BIN="$BIN_DIR/pi"
export QUOTA_SENTINEL_PI_AUTH_FILE="$TEST_TEMP_DIR/auth.json"
print -r -- '{"openai-codex":{"token":"x"},"antigravity":{"token":"x"}}' >"$QUOTA_SENTINEL_PI_AUTH_FILE"
export QUOTA_SENTINEL_CODEXBAR_BIN="$BIN_DIR/codexbar"

export PI_SOURCE_ONLY=1
source "${0:A:h}/../quota-sentinel.sh"
LAST_TEMP_DIR="$TEST_TEMP_DIR"
trap cleanup EXIT
ensure_temp_dir

# Native probes are stubbed out: they are covered by their own timeout logic
# and must stay hermetic here.
fetch_native_codex_quota() { return 1; }
fetch_native_antigravity_quota() { return 1; }

typeset -ga CAPTURED_MESSAGES=()
send_feishu_message() {
  CAPTURED_MESSAGES+=("$4")
  return 0
}

assert_log_contains() {
  local pattern="$1" log_file
  log_file="$QUOTA_SENTINEL_LOG_DIR/$(TZ=Asia/Shanghai /bin/date '+%Y-%m-%d').log"
  [[ -r "$log_file" ]] || { print -u2 "FAIL: run log missing: $log_file"; exit 1; }
  grep -q "$pattern" "$log_file" || { print -u2 "FAIL: log missing pattern: $pattern"; exit 1; }
}

print -r -- "== R1: CodexBar live timeout -> cache fallback; scheduler untouched (C2+C4) =="
reset_state() { rm -rf "$QUOTA_SENTINEL_STATE_DIR"; mkdir -p "$QUOTA_SENTINEL_STATE_DIR"; }
reset_state
# Seed a valid cache via the real cache writer.
live_fixture="$TEST_TEMP_DIR/live.json"
print -r -- '{"source":"CodexBar · cli","fresh":true,"capturedAt":1788000000,"fiveHour":{"remainingPercent":70,"resetAt":1788050000},"weekly":{"remainingPercent":80,"resetAt":1788650000}}' >"$live_fixture"
save_codexbar_cache "$live_fixture" "$CODEXBAR_CODEX_CACHE_FILE"
# Deadline change-log fires through the central writer.
write_provider_next_due codex 1111111
write_provider_next_due codex 5555555
assert_log_contains "state: codex next_due_at 1111111 -> 5555555"
write_provider_last_known_reset codex 4444444
CODEXBAR_MOCK_MODE=hang collect_effective_quotas
[[ "$(jq -r '.fresh' "$CODEX_QUOTA_NORMALIZED_FILE")" == "false" ]]
[[ "$(jq -r '.source' "$CODEX_QUOTA_NORMALIZED_FILE")" == *"cached"* ]]
[[ "$(jq -r '.capturedAt' "$CODEX_QUOTA_NORMALIZED_FILE")" == "1788000000" ]]
[[ "$(read_provider_next_due codex)" == "5555555" ]]
[[ "$(read_provider_last_known_reset codex)" == "4444444" ]]
assert_log_contains "quota codex: codexbar-live TIMEOUT"
print -r -- "  PASS: live timeout fell through to cache; next_due/last_known_reset unchanged"

print -r -- "== R2: timed-out codexbar leaves no orphan grandchildren (C5) =="
reset_state
GRANDCHILD_PID_FILE="$TEST_TEMP_DIR/grandchild.pid"
export GRANDCHILD_PID_FILE
CODEXBAR_MOCK_MODE=hang fetch_codexbar_codex_quota "$CODEX_QUOTA_NORMALIZED_FILE" && rc=0 || rc=$?
(( rc != 0 ))
gc_pid="$(cat "$GRANDCHILD_PID_FILE")"
sleep 0.3
if kill -0 "$gc_pid" 2>/dev/null; then
  print -u2 "FAIL: grandchild $gc_pid survived the group kill"
  exit 1
fi
print -r -- "  PASS: grandchild $gc_pid gone after process-group kill"

print -r -- "== R3: codex task timeout + antigravity success (M6, whole run path) =="
reset_state
t0="$(now_epoch)"
run_and_reschedule_selected codex antigravity
elapsed=$(( $(now_epoch) - t0 ))
(( elapsed < 15 )) || { print -u2 "FAIL: run took ${elapsed}s, timeout did not bound it"; exit 1; }
[[ "${CODEX_RUN_RESULT:-}" == "发送失败" ]]
[[ "${ANTIGRAVITY_RUN_RESULT:-}" == "发送成功" ]]
[[ -s "$QUOTA_SENTINEL_STATE_DIR/codex-last-task-at" ]]
[[ -s "$QUOTA_SENTINEL_STATE_DIR/antigravity-last-task-at" ]]
[[ ! -e "$RUN_LOCK_FILE" ]]
[[ ! -e "$QUOTA_LOCK_FILE" ]]
last_msg="${CAPTURED_MESSAGES[-1]}"
[[ "$last_msg" == *"GPT-5.6 Luna"* && "$last_msg" == *"🔴"* ]]
[[ "$last_msg" == *"Gemini 3.7 Flash"* && "$last_msg" == *"🟢"* ]]
assert_log_contains "run codex: TIMEOUT after 2s"
assert_log_contains "run antigravity: success"
print -r -- "  PASS: run finished in ${elapsed}s; codex=失败 antigravity=成功; locks released"

print -r -- "== R4: /usage busy reply, then recovery =="
reset_state
write_provider_next_due codex 123456
write_provider_last_task codex 654321
before_state="$(cat "$QUOTA_SENTINEL_STATE_DIR"/codex-next-due-at "$QUOTA_SENTINEL_STATE_DIR"/codex-last-task-at)"
acquire_quota_lock  # simulate a watchdog/other holder
busy_out="$(FEISHU_DRY_RUN=1 PI_SOURCE_ONLY=0 QUOTA_SENTINEL_STATE_DIR="$QUOTA_SENTINEL_STATE_DIR" QUOTA_SENTINEL_LOG_DIR="$QUOTA_SENTINEL_LOG_DIR" \
  FEISHU_APP_ID=test-app FEISHU_APP_SECRET=test-secret FEISHU_USER_ID=test-user \
  FEISHU_DISABLE_CHART=1 "${0:A:h}/../quota-sentinel.sh" usage 2>/dev/null)"
release_quota_lock
[[ "$busy_out" == *"配额正在刷新，请稍后再试"* ]]
[[ "$busy_out" != *"quota.lock"* && "$busy_out" != *"shlock"* ]]
after_state="$(cat "$QUOTA_SENTINEL_STATE_DIR"/codex-next-due-at "$QUOTA_SENTINEL_STATE_DIR"/codex-last-task-at)"
[[ "$before_state" == "$after_state" ]]
assert_log_contains "usage: quota busy after 20s"
print -r -- "  PASS: busy payload shown, no internals leaked, scheduler state unchanged"

# Recovery: lock released -> normal usage path runs (collect stubbed hermetic).
reset_state
CAPTURED_MESSAGES=()
send_usage_notification
[[ "${CAPTURED_MESSAGES[-1]}" == *"GPT-5.6 Luna"* ]]
[[ "${CAPTURED_MESSAGES[-1]}" == *"即时配额查询"* ]]
assert_log_contains "usage: completed"
print -r -- "  PASS: /usage recovered normally after the lock freed"

print -r -- "== R5: capturedAt unified to epoch-integer/null at every boundary =="
reset_state
ensure_temp_dir
ts_expected="$(/bin/date -u -j -f '%Y-%m-%dT%H:%M:%S' '2026-08-30T07:15:57' '+%s')"
mk_headers_fixture() {
  print -r -- '{"capturedAt":'"$1"',"status":"ok","headers":{"x-codex-primary-used-percent":"20","x-codex-primary-window-minutes":"300","x-codex-primary-reset-at":"1790000000","x-codex-secondary-used-percent":"10","x-codex-secondary-window-minutes":"10080","x-codex-secondary-reset-at":"1791000000"}}'
}
fx="$TEST_TEMP_DIR/cap.json"
out="$TEST_TEMP_DIR/cap-out.json"

mk_headers_fixture '"2026-08-30T07:15:57.123Z"' >"$fx"
normalize_pi_codex_quota "$fx" "$out"
[[ "$(jq -r '.capturedAt' "$out")" == "$ts_expected" ]]

mk_headers_fixture '"not-a-date"' >"$fx"
normalize_pi_codex_quota "$fx" "$out"
[[ "$(jq -r '.capturedAt' "$out")" == "null" ]]

mk_headers_fixture 'null' >"$fx"
normalize_pi_codex_quota "$fx" "$out"
[[ "$(jq -r '.capturedAt' "$out")" == "null" ]]

mk_headers_fixture '1788000000' >"$fx"
normalize_pi_codex_quota "$fx" "$out"
[[ "$(jq -r '.capturedAt' "$out")" == "1788000000" ]]
[[ "$(jq -r '.fresh' "$out")" == "false" ]]

# Disk snapshot with a legacy ISO capturedAt must be renormalised on read.
print -r -- '{"source":"Pi 快照（可能不是最新）","fresh":false,"cached":true,"capturedAt":"2026-08-30T07:15:57.123Z","fiveHour":{"remainingPercent":50,"resetAt":1790000000},"weekly":{"remainingPercent":60,"resetAt":1791000000}}' >"$PI_CODEX_SNAPSHOT_FILE"
use_pi_snapshot_codex "$out"
[[ "$(jq -r '.capturedAt' "$out")" == "$ts_expected" ]]
[[ "$(jq -r '.fresh' "$out")" == "false" ]]
print -r -- "  PASS: ISO+ms -> epoch; invalid/missing -> null (never now); snapshot read path normalised"

# Cache capturedAt must not drift across repeated reads.
print -r -- "== R6: cache capturedAt stable; atomic writer leaves no debris =="
save_codexbar_cache "$live_fixture" "$CODEXBAR_CODEX_CACHE_FILE"
use_codexbar_cached_codex "$out"
c1="$(jq -r '.capturedAt' "$out")"
use_codexbar_cached_codex "$out"
c2="$(jq -r '.capturedAt' "$out")"
[[ "$c1" == "1788000000" && "$c2" == "1788000000" ]]

atomic_write_state_file "$QUOTA_SENTINEL_STATE_DIR/writer-test" "42"
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/writer-test")" == "42" ]]
[[ "$(stat -f '%Lp' "$QUOTA_SENTINEL_STATE_DIR/writer-test")" == "600" ]]
print -r -- "== R7: legacy migration (atomic) produces the same provider state =="
reset_state
print -r -- "987654" >"$QUOTA_SENTINEL_STATE_DIR/last-task-at"
print -r -- "876543" >"$QUOTA_SENTINEL_STATE_DIR/next-due-at"
print -r -- "100000:200000" >"$QUOTA_SENTINEL_STATE_DIR/last-triggered-window"
[[ "$(read_provider_last_task codex)" == "987654" ]]
[[ "$(read_provider_last_task antigravity)" == "987654" ]]
[[ "$(read_provider_next_due codex)" == "876543" ]]
[[ "$(read_provider_last_known_reset codex)" == "100000" ]]
[[ "$(read_provider_last_known_reset antigravity)" == "200000" ]]
for f in "$QUOTA_SENTINEL_STATE_DIR"/codex-*; do
  [[ "$(stat -f '%Lp' "$f")" == "600" ]]
done
print -r -- "  PASS: migration via atomic writer, values and modes intact"

# Temp dir permission invariant (P2-4).
[[ "$(stat -f '%Lp' "$LAST_TEMP_DIR")" == "700" ]]

print -r -- "robustness regression: all cases passed"
