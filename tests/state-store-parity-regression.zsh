#!/bin/zsh
# Python FileStateStore ↔ shell getters parity regression (SP-* cases).
#
# The strangler contract: for every value the project's writers can
# produce, quota_sentinel.state must read the shell's per-provider state
# files with the same semantics as the shell's own getters — same values,
# same unset rules, same transient reset_candidate compound — and must be
# strictly read-only. Known malformed-input divergences (zero-padded
# epochs, multi-colon candidates) are explicitly registered in SP4, so
# they remain decisions rather than discoveries. status() already routes
# its next-due display through the store (with the shell getter as
# fallback), so this suite is the compatibility proof for that seam and
# the guard for every future one.
# Fully isolated: temp state, stubbed probes, no network.

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
source "$SCRIPT_PATH"
LAST_TEMP_DIR="$TEST_TEMP_DIR"
trap 'rm -rf "$TEST_TEMP_DIR"' EXIT
ensure_temp_dir

fetch_native_codex_quota() { return 1; }
fetch_native_antigravity_quota() { return 1; }
fetch_native_opencode_quota() { return 1; }
fetch_codexbar_codex_quota() { return 1; }
fetch_codexbar_antigravity_quota() { return 1; }
fetch_codexbar_opencode_quota() { return 1; }

py() {
  PYTHONPATH="$SCRIPT_DIR" "$PYTHON3_BIN" -m quota_sentinel \
    --state-dir "$QUOTA_SENTINEL_STATE_DIR" "$@"
}

# python dump <provider> → value of one slot key
py_slot() {
  local provider="$1" key="$2"
  py dump "$provider" | awk -F= -v k="$key" '$1==k {print $2}'
}

# Drive a realistic full-slot state through the REAL shell transitions:
# legacy bootstrap → fresh-reset generation → in-flight candidate →
# successful run commit. Then compare every slot with the shell getters.
seed_realistic_state() {
  local provider="$1" base="$2"
  rm -rf "$QUOTA_SENTINEL_STATE_DIR"
  mkdir -p "$QUOTA_SENTINEL_STATE_DIR"
  migrate_legacy_state
  write_provider_last_attempt "$provider" "$(( base + 1 ))"
  write_provider_last_task "$provider" "$base"
  write_provider_next_due "$provider" "$(( base + 100 ))"
  write_provider_retry_pending "$provider" 1
  write_provider_last_known_reset "$provider" "$base"
  write_provider_last_window "$provider" "$base"
  write_provider_reset_anchor "$provider" "$base"
  write_provider_reset_candidate "$provider" "$(( base + 3600 ))" "$(( base + 10 ))"
}

# ---- SP1: every slot matches the shell getters, for every provider ---------
base_now=$(( $(/bin/date '+%s') ))
for p in "${PROVIDERS[@]}"; do
  seed_realistic_state "$p" "$base_now"
  [[ "$(py_slot "$p" last_attempt_at)" == "$(read_provider_last_attempt "$p")" ]]
  [[ "$(py_slot "$p" last_task_at)" == "$(read_provider_last_task "$p")" ]]
  [[ "$(py_slot "$p" next_due_at)" == "$(read_provider_next_due "$p")" ]]
  [[ "$(py_slot "$p" retry_pending)" == "1" ]]
  [[ "$(py_slot "$p" last_known_reset)" == "$(read_provider_last_known_reset "$p")" ]]
  [[ "$(py_slot "$p" last_triggered_window)" == "$(read_provider_last_window "$p")" ]]
  [[ "$(py_slot "$p" reset_anchor)" == "$(read_provider_reset_anchor "$p")" ]]
  [[ "$(py_slot "$p" reset_candidate)" == "$(read_provider_reset_candidate "$p")" ]]
done
# And the unset side: missing retry-pending reads 0 in python, unset in shell.
seed_realistic_state codex "$base_now"
rm -f "$QUOTA_SENTINEL_STATE_DIR/codex-retry-pending" \
      "$QUOTA_SENTINEL_STATE_DIR/codex-reset-candidate"
[[ "$(py_slot codex retry_pending)" == "0" ]]
[[ "$(py_slot codex reset_candidate)" == "unset" ]]
read_provider_retry_pending codex >/dev/null && { echo "SP1: shell saw pending" >&2; exit 1; }
# Malformed current file: shell treats as unset, python must agree.
print -r -- "garbage" >"$QUOTA_SENTINEL_STATE_DIR/codex-next-due-at"
[[ "$(py_slot codex next_due_at)" == "unset" ]]
read_provider_next_due codex >/dev/null && { echo "SP1: shell saw garbage" >&2; exit 1; }
print -r -- "SP1 (per-slot parity with shell getters): passed"

# ---- SP2: python reads are pure — no creation, no churn ---------------------
seed_realistic_state codex "$base_now"
sp2_sig() {
  local f
  find "$QUOTA_SENTINEL_STATE_DIR" -type f | LC_ALL=C sort | while IFS= read -r f; do
    print -r -- "$f|$(stat -f '%m|%z' "$f")|$(md5 -q "$f")"
  done
}
sp2_before="$(sp2_sig)"
py next-due codex >/dev/null
py dump codex >/dev/null
for p in "${PROVIDERS[@]}"; do py dump "$p" >/dev/null; done
[[ "$(sp2_sig)" == "$sp2_before" ]]
# A missing state dir must not be created either.
rm -rf "$TEST_TEMP_DIR/noexist"
PYTHONPATH="$SCRIPT_DIR" "$PYTHON3_BIN" -m quota_sentinel \
  --state-dir "$TEST_TEMP_DIR/noexist" next-due codex >/dev/null || true
[[ ! -e "$TEST_TEMP_DIR/noexist" ]]
print -r -- "SP2 (store load creates nothing, mutates nothing): passed"

# ---- SP3: CLI status routes through the store and reports the same ---------
seed_realistic_state codex "$base_now"
for p in antigravity opencode; do
  write_provider_next_due "$p" "$base_now"   # keep everything consistent
done
cli_home="$TEST_TEMP_DIR/fakehome"
mkdir -p "$cli_home/.pi/agent/npm/node_modules/pi-antigravity/src"
: >"$cli_home/.pi/agent/npm/node_modules/pi-antigravity/src/index.ts"
mkdir -p "$TEST_TEMP_DIR/bin"
print -r -- '#!/bin/zsh
exit 0' >"$TEST_TEMP_DIR/bin/pi"
chmod +x "$TEST_TEMP_DIR/bin/pi"
print -r -- '{"openai-codex":{"token":"x"},"antigravity":{"token":"x"},"opencode-go":{"type":"api_key","key":"x"}}' \
  >"$TEST_TEMP_DIR/cli-auth.json"
sp3_out="$(env HOME="$cli_home" PI_SOURCE_ONLY=0 \
  QUOTA_SENTINEL_STATE_DIR="$QUOTA_SENTINEL_STATE_DIR" \
  QUOTA_SENTINEL_LOG_DIR="$TEST_TEMP_DIR/logs2" \
  QUOTA_SENTINEL_PI_BIN="$TEST_TEMP_DIR/bin/pi" \
  QUOTA_SENTINEL_PI_AUTH_FILE="$TEST_TEMP_DIR/cli-auth.json" \
  FEISHU_APP_ID=test-app FEISHU_APP_SECRET=test-secret FEISHU_USER_ID=test-user \
  /bin/zsh "$SCRIPT_PATH" status)"
sp3_expect="$(print -r -- "next codex run: $(format_reset_time $(( base_now + 100 )))")"
print -r -- "$sp3_out" | grep -Fqx "$sp3_expect"
print -r -- "SP3 (status via store matches shell formatting exactly): passed"

# ---- SP4: malformed-value registry (Phase 1.1) --------------------------------
# Every pathological file content gets BOTH sides' verdict asserted
# explicitly. "SAME" cases prove agreement; DIVERGENCE cases are deliberate
# strictness decisions for content no project writer can produce — they are
# pinned here so the difference is known forever, never discovered.
epoch_verdicts=()
for v in "" "garbage" "42" "42
43" " 42" "42 " "42"$'\r' "+5" "-5" "5_0" "٤٢" "007"; do
  rm -f "$QUOTA_SENTINEL_STATE_DIR/codex-next-due-at"
  printf '%s\n' "$v" >"$QUOTA_SENTINEL_STATE_DIR/codex-next-due-at"
  s="$(read_provider_next_due codex 2>/dev/null || echo unset)"
  p="$(py_slot codex next_due_at)"
  epoch_verdicts+=("[$v] shell=$s python=$p")
done
# Agreements on all reject-cases, canonical agreement on "42":
printf '%s\n' "${epoch_verdicts[@]}" | grep -Fq '[42] shell=42 python=42'
printf '%s\n' "${epoch_verdicts[@]}" | grep -Fq '[garbage] shell=unset python=unset'
printf '%s\n' "${epoch_verdicts[@]}" | grep -Fq '[ 42] shell=unset python=unset'
printf '%s\n' "${epoch_verdicts[@]}" | grep -Fq '[+5] shell=unset python=unset'
printf '%s\n' "${epoch_verdicts[@]}" | grep -Fq '[-5] shell=unset python=unset'
printf '%s\n' "${epoch_verdicts[@]}" | grep -Fq '[5_0] shell=unset python=unset'
printf '%s\n' "${epoch_verdicts[@]}" | grep -Fq $'[٤٢] shell=unset python=unset'
# CRLF: shell keeps the CR and rejects; python must NOT normalize it away:
printf '%s\n' "${epoch_verdicts[@]}" | grep -Fq $'[42\r] shell=unset python=unset'
# DIVERGENCE #1 (registered): zero-padded epoch — shell echoes raw bytes,
# store canonicalizes to the integer value. Both are valid epochs to the
# scheduler (arithmetic equality); display divergence is accepted.
printf '%s\n' "${epoch_verdicts[@]}" | grep -Fq '[007] shell=007 python=7'
# Multi-line garbage (the "[42 / 43]" pair of verdict lines): both reject.
printf '%s\n' "${epoch_verdicts[@]}" | grep -Fq '43] shell=unset python=unset'

# Compound slot:
rm -f "$QUOTA_SENTINEL_STATE_DIR/codex-reset-candidate"
printf '%s\n' "5:6" >"$QUOTA_SENTINEL_STATE_DIR/codex-reset-candidate"
[[ "$(read_provider_reset_candidate codex)" == "5:6" && "$(py_slot codex reset_candidate)" == "5:6" ]]
# DIVERGENCE #2 (registered): multi-colon garbage — the shell slices
# first:last ("5:6:7" → "5:7" = a valid-looking candidate!); the store
# rejects to None. Writers only emit one colon; strictness here prevents a
# corrupt file from being silently interpreted as a candidate.
printf '%s\n' "5:6:7" >"$QUOTA_SENTINEL_STATE_DIR/codex-reset-candidate"
[[ "$(read_provider_reset_candidate codex 2>/dev/null || echo unset)" == "5:7" ]]
[[ "$(py_slot codex reset_candidate)" == "unset" ]]
# Padded compound: both reject.
printf '%s\n' " 5:6 " >"$QUOTA_SENTINEL_STATE_DIR/codex-reset-candidate"
[[ "$(read_provider_reset_candidate codex 2>/dev/null || echo unset)" == "unset" ]]
[[ "$(py_slot codex reset_candidate)" == "unset" ]]
# Corrupt encoding bytes: shell treats as unparsable value; store unsets
# the slot ONLY (other slots still readable — asserted in the python suite).
printf '\377\376\n' >"$QUOTA_SENTINEL_STATE_DIR/codex-reset-candidate"
[[ "$(read_provider_reset_candidate codex 2>/dev/null || echo unset)" == "unset" ]]
[[ "$(py_slot codex reset_candidate)" == "unset" ]]
rm -f "$QUOTA_SENTINEL_STATE_DIR/codex-reset-candidate"
print -r -- "SP4 (malformed-value registry: agreements + 2 registered divergences): passed"

print -r -- "state-store parity regression: all cases passed"
