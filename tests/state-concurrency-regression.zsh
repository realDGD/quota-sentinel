#!/bin/zsh
# State-concurrency and read-purity regression (SU-* cases).
#
# Covers the scheduler-state transaction boundary introduced with the
# /usage concurrency fix and the bootstrap-only legacy migration:
#   SU-U1..U4  /usage opportunistic deadline sync is serialized by run.lock
#              (idle sync, busy skip, lock-order trace, concurrent-commit
#              integrity)
#   SU-C1/C2   reset-candidate lifecycle survives a process restart in both
#              directions (promotes after confirmation age, holds before it)
#   SU-M1..M3  migration is idempotent, getters are pure, and the CLI
#              command-entry bootstrap still migrates a legacy deployment
# Fully isolated: temp state, temp logs, stubbed probes and Feishu transport.
# Zero real model calls, zero quota consumption, zero network.

set -euo pipefail

readonly TEST_TEMP_DIR="$(mktemp -d /private/tmp/quota-sentinel.XXXXXX)"
readonly SCRIPT_PATH="${0:A:h}/../quota-sentinel.sh"
export QUOTA_SENTINEL_STATE_DIR="$TEST_TEMP_DIR/state"
export QUOTA_SENTINEL_LOG_DIR="$TEST_TEMP_DIR/logs"
export PI_SOURCE_ONLY=1
export FEISHU_APP_ID="test-app"
export FEISHU_APP_SECRET="test-secret"
export FEISHU_USER_ID="test-user"
export FEISHU_DISABLE_CHART=1

typeset -ga SU_CHILD_PIDS=()
cleanup() {
  local pid
  for pid in "${SU_CHILD_PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done
  rm -rf "$TEST_TEMP_DIR"
}
trap cleanup EXIT

source "$SCRIPT_PATH"
LAST_TEMP_DIR="$TEST_TEMP_DIR"
trap cleanup EXIT
ensure_temp_dir

# ---- hermetic stubs ---------------------------------------------------------
fetch_native_codex_quota() { return 1; }
fetch_native_antigravity_quota() { return 1; }
fetch_native_opencode_quota() { return 1; }
fetch_codexbar_codex_quota() { return 1; }
fetch_codexbar_antigravity_quota() { return 1; }
fetch_codexbar_opencode_quota() { return 1; }
save_pi_quota_snapshots() { return 0; }

typeset -g MOCK_CODEX_RESET=0
collect_effective_quotas() {
  ensure_temp_dir
  local now
  now="$(/bin/date '+%s')"
  if (( MOCK_CODEX_RESET > 0 )); then
    print -r -- '{"source":"Native · codex app-server","fresh":true,"capturedAt":'$now',"fiveHour":{"remainingPercent":80,"resetAt":'$MOCK_CODEX_RESET'},"weekly":{"remainingPercent":90,"resetAt":'$(( MOCK_CODEX_RESET + 500000 ))'}}' >"$CODEX_QUOTA_NORMALIZED_FILE"
    CODEX_QUOTA_IS_FRESH=1
  else
    rm -f "$CODEX_QUOTA_NORMALIZED_FILE"
  fi
  rm -f "$ANTIGRAVITY_QUOTA_NORMALIZED_FILE" "$OPENCODE_QUOTA_NORMALIZED_FILE"
}

typeset -ga CAPTURED_MESSAGES=()
send_feishu_message() {
  CAPTURED_MESSAGES+=("$4")
  return 0
}

# ---- helpers ----------------------------------------------------------------
reset_state() {
  rm -rf "$QUOTA_SENTINEL_STATE_DIR"
  mkdir -p "$QUOTA_SENTINEL_STATE_DIR"
}

# Path|mtime|size|md5 per state file; tmp-leak detection folded in.
# Lock files are coordination, not scheduler state — excluded on purpose.
state_signature() {
  local f
  {
    find "$QUOTA_SENTINEL_STATE_DIR" -type f -name '*.tmp.*' -print
    find "$QUOTA_SENTINEL_STATE_DIR" -type f ! -name '*.lock' | LC_ALL=C sort | while IFS= read -r f; do
      print -r -- "$f|$(stat -f '%m|%z' "$f")|$(md5 -q "$f")"
    done
  }
}

# Hold run.lock in a live child process (shlock refuses while the recorded
# pid is alive — exactly the busy-scheduler simulation).
spawn_run_lock_holder() {
  local ttl="${1:-30}"
  zsh -c '/usr/bin/shlock -p "$$" -f "$1" && exec /bin/sleep "$2"' _ \
    "$QUOTA_SENTINEL_STATE_DIR/run.lock" "$ttl" &
  SU_CHILD_PIDS+=($!)
  local i
  for i in {1..100}; do
    [[ -e "$QUOTA_SENTINEL_STATE_DIR/run.lock" ]] && return 0
    "$SLEEP_BIN" 0.05
  done
  return 1
}

stop_run_lock_holder() {
  local pid
  for pid in "${SU_CHILD_PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done
  SU_CHILD_PIDS=()
  rm -f "$QUOTA_SENTINEL_STATE_DIR/run.lock"
}

write_mock_codex_quota() {
  # Direct normalized-file seeding for cases that drive sync_provider_*
  # without going through the usage/collect entry points.
  local reset="$1" now
  now="$(/bin/date '+%s')"
  print -r -- '{"source":"Native · codex app-server","fresh":true,"capturedAt":'$now',"fiveHour":{"remainingPercent":80,"resetAt":'$reset'},"weekly":{"remainingPercent":90,"resetAt":'$(( reset + 500000 ))'}}' >"$CODEX_QUOTA_NORMALIZED_FILE"
  CODEX_QUOTA_IS_FRESH=1
}

# ---- SU-M1: bootstrap migration is idempotent --------------------------------
reset_state
print -r -- "987654" >"$QUOTA_SENTINEL_STATE_DIR/last-task-at"
print -r -- "876543" >"$QUOTA_SENTINEL_STATE_DIR/next-due-at"
print -r -- "100000:200000" >"$QUOTA_SENTINEL_STATE_DIR/last-triggered-window"
migrate_legacy_state
m1_sig1="$(state_signature)"
migrate_legacy_state
m1_sig2="$(state_signature)"
[[ "$m1_sig1" == "$m1_sig2" ]]
[[ -r "$QUOTA_SENTINEL_STATE_DIR/last-task-at" ]]   # legacy sources never consumed
[[ "$(read_provider_last_task codex)" == "987654" ]]
print -r -- "SU-M1 (migration idempotent, legacy sources kept): passed"

# ---- SU-M2: every getter is pure (no writes, no mtime churn) -----------------
m2_before="$(state_signature)"
for p in codex antigravity opencode; do
  read_provider_next_due "$p" >/dev/null || true
  read_provider_last_task "$p" >/dev/null || true
  read_provider_last_attempt "$p" >/dev/null || true
  read_provider_last_known_reset "$p" >/dev/null || true
  read_provider_last_window "$p" >/dev/null || true
  read_provider_retry_pending "$p" >/dev/null || true
  read_provider_reset_candidate "$p" >/dev/null || true
  read_provider_reset_anchor "$p" >/dev/null || true
done
read_next_due >/dev/null || true
[[ "$(state_signature)" == "$m2_before" ]]
print -r -- "SU-M2 (getters pure: state bytes and mtimes identical): passed"

# ---- SU-U1: idle scheduler → /usage syncs the deadline and sends the card ----
reset_state
MOCK_CODEX_RESET=$(( $(/bin/date '+%s') + 18000 ))
send_usage_notification
[[ "$(read_provider_next_due codex)" == "$(( MOCK_CODEX_RESET + RESET_BUFFER_SECONDS ))" ]]
[[ "$(read_provider_reset_anchor codex)" == "$MOCK_CODEX_RESET" ]]
[[ "$(read_provider_last_known_reset codex)" == "$MOCK_CODEX_RESET" ]]
(( ${#CAPTURED_MESSAGES[@]} == 1 ))
[[ ! -e "$QUOTA_SENTINEL_STATE_DIR/run.lock" ]]
[[ ! -e "$QUOTA_SENTINEL_STATE_DIR/quota.lock" ]]
print -r -- "SU-U1 (idle /usage: fresh deadline synced + card sent + locks free): passed"

# ---- SU-U2: run.lock held externally → /usage skips sync, still succeeds -----
u2_before="$(state_signature)"
u2_cards="${#CAPTURED_MESSAGES[@]}"
spawn_run_lock_holder
u2_start="$(/bin/date '+%s')"
send_usage_notification
(( $(/bin/date '+%s') - u2_start <= 15 ))           # never queues on run.lock
[[ "$(state_signature)" == "$u2_before" ]]          # zero scheduler-state writes
(( ${#CAPTURED_MESSAGES[@]} == u2_cards + 1 ))
[[ ! -e "$QUOTA_SENTINEL_STATE_DIR/quota.lock" ]]
[[ -e "$QUOTA_SENTINEL_STATE_DIR/run.lock" ]]        # the holder's lock survives
stop_run_lock_holder
print -r -- "SU-U2 (busy scheduler: sync skipped, no block, card delivered): passed"

# ---- SU-U3: lock order — run.lock only ever attempted after quota release ----
typeset -g QS_TRACE="$TEST_TEMP_DIR/lock-trace"
: >"$QS_TRACE"
orig_acquire_run_lock="${functions[acquire_run_lock]}"
orig_release_quota_lock="${functions[release_quota_lock]}"
u3_pre_run='print -r -- run-attempt >>"$QS_TRACE"
'
u3_pre_rel='print -r -- quota-released >>"$QS_TRACE"
'
functions[acquire_run_lock]="$u3_pre_run$orig_acquire_run_lock"
functions[release_quota_lock]="$u3_pre_rel$orig_release_quota_lock"
send_usage_notification
functions[acquire_run_lock]="$orig_acquire_run_lock"
functions[release_quota_lock]="$orig_release_quota_lock"
# Exactly one non-blocking attempt, strictly after quota.lock was released:
# no quota→run nesting exists → the ABBA window against check_schedule is
# structurally gone.
(( $(grep -c 'run-attempt' "$QS_TRACE") == 1 ))
u3_first_run="$(grep -n 'run-attempt' "$QS_TRACE" | head -1 | cut -d: -f1)"
u3_last_release="$(grep -n 'quota-released' "$QS_TRACE" | tail -1 | cut -d: -f1)"
(( u3_first_run > u3_last_release ))
print -r -- "SU-U3 (trace: quota released before single non-blocking run attempt): passed"

# ---- SU-U4: /usage concurrent with run.lock-holding state commits ------------
# A detached writer replicates the check/run retry phase (acquire run.lock,
# commit repeatedly, release) while the foreground fires /usage requests.
# Every commit — from both sides — serializes on run.lock, so the final state
# must be well-formed with no torn or leftover files.
reset_state
zsh -c '
  source "$1"
  acquire_run_lock || exit 3
  i=0
  while (( i < 15 )); do
    (( i += 1 ))
    commit_provider_success codex $(( 1000000 + i ))
    "$SLEEP_BIN" 0.1
  done
  release_run_lock
' _ "$SCRIPT_PATH" &
u4_writer=$!
MOCK_CODEX_RESET=$(( $(/bin/date '+%s') + 18000 ))
for _ in {1..6}; do
  send_usage_notification
done
u4_rc=0
wait "$u4_writer" || u4_rc=$?
(( u4_rc == 0 )) || { print -r -- "SU-U4 writer child failed rc=$u4_rc" >&2; exit 1; }
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-retry-pending")" == "0" ]]
[[ "$(read_provider_last_task codex)" =~ ^[0-9]+$ ]]
[[ "$(read_provider_last_attempt codex)" =~ ^[0-9]+$ ]]
[[ "$(read_provider_next_due codex)" =~ ^[0-9]+$ ]]
if [[ -e "$QUOTA_SENTINEL_STATE_DIR/codex-last-known-reset-at" ]]; then
  [[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-last-known-reset-at")" =~ ^[0-9]+$ ]]
fi
[[ -z "$(find "$QUOTA_SENTINEL_STATE_DIR" -name '*.tmp.*' -print -quit)" ]]
# Cards delivered for every /usage regardless of who held the state lock.
# (U1:1 + U2:1 + U3:1 + U4:6)
(( ${#CAPTURED_MESSAGES[@]} == 9 ))
print -r -- "SU-U4 (interleaved commit×usage: serialized, state well-formed): passed"

# ---- SU-C2 / SU-C1: candidate lifecycle across a real process restart ---------
reset_state
c_base=$(( $(/bin/date '+%s') ))
c_r0=$(( c_base + 18000 ))
c_r1=$(( c_r0 + 3600 ))

# Generation established from the first Fresh reset.
write_mock_codex_quota "$c_r0"
rc=0; sync_provider_deadline_from_quota codex "$c_base" || rc=$?
(( rc == 0 ))
c_due0=$(( c_r0 + RESET_BUFFER_SECONDS ))
[[ "$(read_provider_reset_anchor codex)" == "$c_r0" ]]

# Far-later fresh reset → parked as candidate, deadline preserved.
write_mock_codex_quota "$c_r1"
rc=0; sync_provider_deadline_from_quota codex "$(( c_base + 5 ))" || rc=$?
(( rc == 3 ))
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-reset-candidate")" == "$c_r1:$(( c_base + 5 ))" ]]
[[ "$(read_provider_next_due codex)" == "$c_due0" ]]

# SU-C2: independent observation too young (age < RESET_CONFIRM_MIN_AGE)
# → still awaiting; candidate record untouched.
rc=0; sync_provider_deadline_from_quota codex "$(( c_base + 30 ))" || rc=$?
(( rc == 3 ))
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-reset-candidate")" == "$c_r1:$(( c_base + 5 ))" ]]
[[ "$(read_provider_next_due codex)" == "$c_due0" ]]
print -r -- "SU-C2 (young second observation: awaiting preserved, no promotion): passed"

# SU-C1: promotion must happen in a FRESH PROCESS reading only on-disk state.
zsh -c '
  source "$1"
  ensure_temp_dir
  print -r -- "{\"source\":\"Native · codex app-server\",\"fresh\":true,\"capturedAt\":1,\"fiveHour\":{\"remainingPercent\":80,\"resetAt\":$2},\"weekly\":{\"remainingPercent\":90,\"resetAt\":$(( $2 + 500000 ))}}" >"$CODEX_QUOTA_NORMALIZED_FILE"
  CODEX_QUOTA_IS_FRESH=1
  sync_provider_deadline_from_quota codex "$3"
' _ "$SCRIPT_PATH" "$c_r1" "$(( c_base + 125 ))"
[[ ! -e "$QUOTA_SENTINEL_STATE_DIR/codex-reset-candidate" ]]
[[ "$(read_provider_last_known_reset codex)" == "$c_r1" ]]
[[ "$(read_provider_reset_anchor codex)" == "$c_r1" ]]
[[ "$(read_provider_next_due codex)" == "$(( c_r1 + RESET_BUFFER_SECONDS ))" ]]
# Proof the CHILD did the promotion: back at age 45 the parent would still be
# "awaiting" (rc 3) if the candidate were on disk — a near-movement rc 0 here
# is only reachable against the already-promoted anchor.
rc=0; sync_provider_deadline_from_quota codex "$(( c_base + 50 ))" || rc=$?
(( rc == 0 ))
print -r -- "SU-C1 (restart after candidate write: promotion driven by disk state): passed"

# ---- SU-M3: CLI command-entry bootstrap migrates an old deployment -----------
cli_home="$TEST_TEMP_DIR/fakehome"
mkdir -p "$cli_home/.pi/agent/npm/node_modules/pi-antigravity/src"
: >"$cli_home/.pi/agent/npm/node_modules/pi-antigravity/src/index.ts"
mkdir -p "$TEST_TEMP_DIR/bin"
print -r -- '#!/bin/zsh
exit 0' >"$TEST_TEMP_DIR/bin/pi"
chmod +x "$TEST_TEMP_DIR/bin/pi"
print -r -- '{"openai-codex":{"token":"x"},"antigravity":{"token":"x"},"opencode-go":{"type":"api_key","key":"x"}}' \
  >"$TEST_TEMP_DIR/cli-auth.json"
cli_state="$TEST_TEMP_DIR/cli-state"
mkdir -p "$cli_state"
print -r -- "987654" >"$cli_state/last-task-at"
print -r -- "876543" >"$cli_state/next-due-at"
print -r -- "100000:200000" >"$cli_state/last-triggered-window"
cli_out="$(env HOME="$cli_home" PI_SOURCE_ONLY=0 \
  QUOTA_SENTINEL_STATE_DIR="$cli_state" \
  QUOTA_SENTINEL_LOG_DIR="$TEST_TEMP_DIR/cli-logs" \
  QUOTA_SENTINEL_PI_BIN="$TEST_TEMP_DIR/bin/pi" \
  QUOTA_SENTINEL_PI_AUTH_FILE="$TEST_TEMP_DIR/cli-auth.json" \
  FEISHU_APP_ID=test-app FEISHU_APP_SECRET=test-secret FEISHU_USER_ID=test-user \
  /bin/zsh "$SCRIPT_PATH" status)"
print -r -- "$cli_out" | grep -q 'next codex run:'
[[ "$(cat "$cli_state/codex-last-task-at")" == "987654" ]]
[[ "$(cat "$cli_state/codex-next-due-at")" == "876543" ]]
[[ "$(cat "$cli_state/antigravity-last-known-reset-at")" == "200000" ]]
[[ "$(cat "$cli_state/codex-last-attempt-at")" == "987654" ]]
print -r -- "SU-M3 (CLI entry bootstrap migrates legacy deployment): passed"

# ---- SU-B1: non-state commands keep the side-effect-free boundary ------------
rm -rf "$TEST_TEMP_DIR/nostate"
env PI_SOURCE_ONLY=0 \
  QUOTA_SENTINEL_STATE_DIR="$TEST_TEMP_DIR/nostate" \
  QUOTA_SENTINEL_LOG_DIR="$TEST_TEMP_DIR/cli-logs" \
  FEISHU_APP_ID=x FEISHU_APP_SECRET=x FEISHU_USER_ID=x \
  /bin/zsh "$SCRIPT_PATH" help >/dev/null
[[ ! -e "$TEST_TEMP_DIR/nostate" ]]
print -r -- "SU-B1 (help creates no state dir: bootstrap scope is exact): passed"

print -r -- "state-concurrency regression: all cases passed"
