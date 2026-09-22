#!/bin/zsh
# State-backend authority lifecycle: end-to-end through the real shell.
#
# The authority manifest is a REQUIRED durable fact, and its absence is
# UNKNOWN OWNERSHIP — not "legacy", and no longer a lifecycle boundary at
# which anything may invent an answer. A deployment that cut over and then
# lost the manifest looks exactly like one that never had it, so the only
# thing allowed to turn absence into a legacy epoch-0 fact is an operator
# asserting it out loud (`bootstrap-authority --assume-legacy`). This suite
# pins that contract, the operator commands that own the lifecycle, and the
# lock-safety of those commands.
#
#   AB1  virgin deployment: reads fail loud, an assertion WITHOUT the flag
#        creates nothing, and the explicit assertion materializes legacy
#        epoch 0 without touching scheduler state (M1)
#   AB2  initialized legacy deployment: deleting the manifest is CORRUPTION
#        and every automatic path refuses; only an explicit operator
#        assertion re-records it (M2)
#   AB3  JSON authoritative with advanced state: deleting the manifest must
#        not resurrect legacy, and must not modify the JSON (M3)
#   AB4  mutations under a missing manifest fail closed (M4)
#   AB5  cutover switches a whole roster in one epoch
#   AB6  after cutover legacy is retired: invisible to reads, refused to writers
#   AB7  rollback is a pure undo and refuses once JSON has advanced
#   AB8  the PUBLIC rollback cannot run while a writer holds run.lock (L1);
#        the deterministic reproducer for the interleaving that makes the
#        INTERNAL verb unsafe lives in tests/python-authority-regression.py
#   AB9  cutover cannot interleave with a writer holding run.lock (L2)
#   AB10 a failing command releases run.lock (no stale lock left behind)
#   AB11 a corrupt manifest is loud and never overwritten
#   AB12 the post-run window + sync path works under JSON authority
#
# Fully isolated: temp state dir, temp logs, no model calls, no network, no
# real launchctl, no Keychain access, and no operation against the
# operator's live state dir.

set -euo pipefail

readonly TEST_TEMP_DIR="$(mktemp -d /private/tmp/quota-sentinel.XXXXXX)"
readonly SCRIPT_PATH="${0:A:h}/../quota-sentinel.sh"
readonly REPO_DIR="${0:A:h:h}"
export QUOTA_SENTINEL_STATE_DIR="$TEST_TEMP_DIR/state"
export QUOTA_SENTINEL_LOG_DIR="$TEST_TEMP_DIR/logs"
export PI_SOURCE_ONLY=1
export FEISHU_DISABLE_CHART=1

fail() { print -u2 -- "FAIL: $*"; exit 1; }

mkdir -p "$QUOTA_SENTINEL_STATE_DIR"
mkdir -p "$TEST_TEMP_DIR/bin"
cat >"$TEST_TEMP_DIR/bin/pi" <<'MOCK'
#!/bin/zsh
exit 0
MOCK
chmod +x "$TEST_TEMP_DIR/bin/pi"
export QUOTA_SENTINEL_PI_BIN="$TEST_TEMP_DIR/bin/pi"

source "$SCRIPT_PATH"
LAST_TEMP_DIR="$TEST_TEMP_DIR"
ensure_temp_dir

readonly MANIFEST="$QUOTA_SENTINEL_STATE_DIR/backend-authority.json"

# Internal bridge verbs, invoked exactly the way the shell invokes them but
# WITHOUT a held lock — which is the whole point of them being internal.
internal() {
  PYTHONPATH="$REPO_DIR" /usr/bin/python3 -S -m quota_sentinel \
    --state-dir "$QUOTA_SENTINEL_STATE_DIR" "$@"
}

manifest_backend() {
  sed -n 's/.*"backend": "\([a-z]*\)".*/\1/p' "$MANIFEST" 2>/dev/null
}

# Every durable byte of the deployment, so a refusal can be proven to have
# changed NOTHING (not merely "the manifest still looks right").
snapshot_state_bytes() {
  local f
  for f in "$QUOTA_SENTINEL_STATE_DIR"/*(N.); do
    print -r -- "${f:t} $(shasum -a 256 <"$f" | cut -d' ' -f1)"
  done
}

reset_deployment() {
  rm -rf "$QUOTA_SENTINEL_STATE_DIR"
  mkdir -p "$QUOTA_SENTINEL_STATE_DIR"
}

# Run the script as a COMMAND. This suite sources it, so PI_SOURCE_ONLY is
# exported; without clearing it the child would source and exit without
# running main().
run_cli() { PI_SOURCE_ONLY=0 /bin/zsh "$SCRIPT_PATH" "$@"; }

# ---------------------------------------------------------------------------
print -r -- "== AB1: a virgin deployment must be initialized explicitly (M1) =="
reset_deployment
[[ ! -e "$MANIFEST" ]] || fail "a fresh state dir already has a manifest"
if internal authority >/dev/null 2>&1; then
  fail "an uninitialized deployment answered an authority read"
fi
legacy_rc=0
legacy_backend_active || legacy_rc=$?
(( legacy_rc == 2 )) || fail "legacy_backend_active did not report unreadable (rc=$legacy_rc)"
check_out="$(run_cli check 2>&1 || true)"
print -r -- "$check_out" | grep -q "bootstrap-authority --assume-legacy" ||
  fail "the failure did not name the explicit remedy: $check_out"
printf '111\n' >"$QUOTA_SENTINEL_STATE_DIR/codex-next-due-at"
before="$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-next-due-at")"
# The operator verb must refuse WITHOUT the assertion, and must create
# nothing while refusing: that is what stops an accidental "repair" from
# re-legitimizing a deployment whose manifest was lost.
refuse_rc=0
refuse_out="$(run_cli bootstrap-authority 2>&1)" || refuse_rc=$?
(( refuse_rc == 3 )) || fail "bootstrap without --assume-legacy exited $refuse_rc"
print -r -- "$refuse_out" | grep -q "UNKNOWN" ||
  fail "the refusal did not explain itself: $refuse_out"
[[ ! -e "$MANIFEST" ]] || fail "a refused bootstrap created a manifest"
# ... and the lower-level helper does not SYNTHESIZE the assertion either:
# a caller that cannot show the operator's flag cannot create the fact.
bare_rc=0
authority_bootstrap >/dev/null 2>&1 || bare_rc=$?
(( bare_rc != 0 )) || fail "authority_bootstrap ran without an assertion"
[[ ! -e "$MANIFEST" ]] || fail "a bare authority_bootstrap created a manifest"
bare_rc=0
authority_bootstrap --force >/dev/null 2>&1 || bare_rc=$?
(( bare_rc != 0 )) || fail "authority_bootstrap accepted an unknown argument"
[[ ! -e "$MANIFEST" ]] || fail "an unknown argument still created a manifest"
init_out="$(run_cli bootstrap-authority --assume-legacy)"
print -r -- "$init_out" | grep -q "bootstrapped as legacy" || fail "bootstrap did not report: $init_out"
[[ "$(manifest_backend)" == "legacy" ]] || fail "bootstrap chose the wrong backend"
grep -q '"epoch": 0' "$MANIFEST" || fail "bootstrap did not start at epoch 0"
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-next-due-at")" == "$before" ]] ||
  fail "bootstrapping modified scheduler state"
[[ "$(stat -f '%Lp' "$MANIFEST")" == "600" ]] || fail "manifest is not 0600"
epoch_before="$(cat "$MANIFEST")"
run_cli bootstrap-authority --assume-legacy | grep -q "already present" ||
  fail "a second bootstrap did not report idempotence"
[[ "$(cat "$MANIFEST")" == "$epoch_before" ]] || fail "a second bootstrap rewrote the manifest"
print -r -- "  PASS: only an explicit assertion creates authority; reads never guess"

# ---------------------------------------------------------------------------
print -r -- "== AB2: deleting the manifest later is corruption, not bootstrap (M2) =="
rm -f "$MANIFEST"
if internal authority >/dev/null 2>&1; then
  fail "a deleted manifest was silently re-interpreted"
fi
guard_rc=0
( read_provider_next_due codex >/dev/null 2>&1 ) || guard_rc=$?
(( guard_rc != 0 )) || fail "a legacy reader served state with no manifest"
run_cli check >/dev/null 2>&1 || true
[[ ! -e "$MANIFEST" ]] || fail "the runtime re-created the manifest (self-healing)"
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-next-due-at")" == "$before" ]] ||
  fail "a refused command still modified state"
# Automatic paths are exhausted; the only thing left is the operator
# asserting the deployment predates the protocol.
run_cli bootstrap-authority --assume-legacy >/dev/null
[[ "$(manifest_backend)" == "legacy" ]] || fail "the assertion did not record legacy"
print -r -- "  PASS: absence is loud; only the operator re-asserts ownership"

# ---------------------------------------------------------------------------
print -r -- "== AB3: JSON + advanced state + deleted manifest never resurrects legacy (M3) =="
reset_deployment
run_cli bootstrap-authority --assume-legacy >/dev/null
# A legacy deadline that the cutover copies and the JSON backend then moves
# far past — so "did this read fall back to legacy?" has an answer that is
# visibly different from the authoritative one.
write_provider_next_due codex 111111
write_provider_last_task codex 100000
run_cli cutover >/dev/null || fail "cutover failed"
commit_provider_success codex 500000
json_next_due="$(internal next-due codex)"
[[ "$json_next_due" == "$(( 500000 + RUN_INTERVAL_SECONDS ))" ]] ||
  fail "the success commit did not land in JSON"
legacy_next_due="$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-next-due-at")"
json_bytes_before="$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-state.json")"

rm -f "$MANIFEST"
read_rc=0
read_out="$(internal next-due codex 2>&1)" || read_rc=$?
(( read_rc != 0 )) || fail "a read with no manifest still answered: $read_out"
if [[ "$legacy_next_due" != "$json_next_due" ]]; then
  print -r -- "$read_out" | grep -q "$legacy_next_due" &&
    fail "the read fell back to the stale legacy value"
fi
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-state.json")" == "$json_bytes_before" ]] ||
  fail "a failed authority read modified the JSON document"
( read_provider_next_due codex >/dev/null 2>&1 ) && fail "legacy became readable again"
print -r -- "  PASS: missing manifest never returns the retired backend"

# ---------------------------------------------------------------------------
print -r -- "== AB4: mutations under a missing manifest fail closed (M4) =="
for verb in "scheduler-commit-success --provider codex --now 600000" \
            "scheduler-begin-attempt --provider codex --now 600000" \
            "scheduler-sync --provider codex --now 600000 --fresh 0"; do
  rc=0
  internal ${=verb} >/dev/null 2>&1 || rc=$?
  (( rc != 0 )) || fail "mutation '$verb' succeeded with no manifest"
done
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-state.json")" == "$json_bytes_before" ]] ||
  fail "a refused mutation modified the authoritative document"
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-next-due-at")" == "$legacy_next_due" ]] ||
  fail "a refused mutation wrote the retired legacy backend"
print -r -- "  PASS: no default state, no legacy write, no document change"

# ---------------------------------------------------------------------------
print -r -- "== AB5: cutover switches a whole roster in one epoch =="
reset_deployment
run_cli bootstrap-authority --assume-legacy >/dev/null
write_provider_next_due codex 111222
write_provider_last_task codex 111000
typeset -A LEGACY_BEFORE=()
for f in "$QUOTA_SENTINEL_STATE_DIR"/*(N.); do
  [[ "${f:t}" == "backend-authority.json" ]] && continue
  LEGACY_BEFORE[${f:t}]="$(cat "$f")"
done
cutover_out="$(run_cli cutover)"
print -r -- "$cutover_out" | grep -q "cutover complete" ||
  fail "cutover did not complete: $cutover_out"
[[ "$(manifest_backend)" == "json" ]] || fail "authority did not flip to json"
grep -q '"epoch": 1' "$MANIFEST" || fail "epoch did not advance to 1"
for name in "${(@k)LEGACY_BEFORE}"; do
  [[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/$name")" == "${LEGACY_BEFORE[$name]}" ]] ||
    fail "cutover modified legacy file $name"
done
[[ "$(internal next-due codex)" == "111222" ]] || fail "JSON did not carry the deadline"
[[ -z "$(find "$QUOTA_SENTINEL_STATE_DIR" -name '*.tmp.*' -print -quit)" ]] ||
  fail "cutover left a temp file behind"
before_manifest="$(cat "$MANIFEST")"
run_cli cutover | grep -q "cutover complete" || fail "second cutover failed"
[[ "$(cat "$MANIFEST")" == "$before_manifest" ]] || fail "a re-run changed the manifest"
print -r -- "  PASS: one epoch, legacy bytes untouched, idempotent"

# ---------------------------------------------------------------------------
print -r -- "== AB6: after the cutover the legacy backend is retired =="
next_due_before="$(internal next-due codex)"
print -r -- "999999" >"$QUOTA_SENTINEL_STATE_DIR/codex-next-due-at"
[[ "$(internal next-due codex)" == "$next_due_before" ]] ||
  fail "a legacy write changed the authoritative read"
guard_rc=0
( write_provider_next_due codex 1 >/dev/null 2>&1 ) || guard_rc=$?
(( guard_rc != 0 )) || fail "a legacy writer ran under JSON authority"
print -r -- "  PASS: legacy is an artifact, not a second truth"

# ---------------------------------------------------------------------------
print -r -- "== AB7: rollback is a pure undo and then refuses =="
# AB6 deliberately wrote a legacy slot to prove such writes are invisible,
# which leaves the two backends diverged — and a diverged rollback is
# exactly what the next assertion expects to be REFUSED. So start from a
# clean cutover to test the pure-undo path itself.
reset_deployment
run_cli bootstrap-authority --assume-legacy >/dev/null
write_provider_next_due codex 333000
write_provider_last_task codex 300000
run_cli cutover >/dev/null || fail "cutover failed"
legacy_snapshot="$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-next-due-at")"
run_cli rollback >/dev/null || fail "rollback failed"
[[ "$(manifest_backend)" == "legacy" ]] || fail "rollback did not return ownership"
grep -q '"epoch": 2' "$MANIFEST" || fail "rollback did not advance the epoch"
[[ "$(read_provider_next_due codex)" == "$legacy_snapshot" ]] ||
  fail "legacy state was not restored"
run_cli cutover >/dev/null || fail "second cutover failed"
commit_provider_success codex 700000
rollback_rc=0
run_cli rollback >/dev/null 2>&1 || rollback_rc=$?
(( rollback_rc != 0 )) || fail "rollback discarded advanced JSON state"
[[ "$(manifest_backend)" == "json" ]] || fail "a refused rollback changed ownership"
print -r -- "  PASS: pure undo only; divergence refuses"

# ---------------------------------------------------------------------------
print -r -- "== AB8: the public rollback cannot bypass run.lock (L1) =="
reset_deployment
run_cli bootstrap-authority --assume-legacy >/dev/null
write_provider_last_task codex 1000
write_provider_next_due codex 2000
run_cli cutover >/dev/null || fail "cutover failed"
zsh -c '
  source "$1"
  acquire_run_lock || exit 3
  commit_provider_success codex 900000
  /bin/date +%s >"$2/writer-ready"
  "$SLEEP_BIN" 3
  release_run_lock
' _ "$SCRIPT_PATH" "$TEST_TEMP_DIR" &
writer_pid=$!
for _ in {1..80}; do
  [[ -e "$TEST_TEMP_DIR/writer-ready" ]] && break
  "$SLEEP_BIN" 0.1
done
[[ -e "$TEST_TEMP_DIR/writer-ready" ]] || fail "the writer never took run.lock"
advanced_due="$(internal next-due codex)"
[[ "$advanced_due" == "$(( 900000 + RUN_INTERVAL_SECONDS ))" ]] || fail "writer did not advance JSON"

# The public verb must not be able to run at all while a writer holds the
# lock. (The INTERNAL verb is deliberately lock-blind — that is why it is
# not an operator command. The deterministic reproducer for the update it
# could lose lives in tests/python-authority-regression.py, where the
# cutover/rollback code exposes a pause hook and the interleaving can be
# driven precisely instead of raced.)
#
# WHOLE-ROSTER + run.lock, together: the refusal must leave EVERY byte of
# the deployment alone — not just the manifest. A "verify the roster, then
# flip" rollback that ran unsynchronized would show up here as a changed
# document even when the manifest looked right.
locked_snapshot="$(snapshot_state_bytes)"
env QUOTA_SENTINEL_RUN_LOCK_WAIT=1 PI_SOURCE_ONLY=0 /bin/zsh "$SCRIPT_PATH" rollback >/dev/null 2>&1 &&
  fail "the public rollback ran while a writer held run.lock"
[[ "$(manifest_backend)" == "json" ]] ||
  fail "the public rollback flipped ownership without the lock"
[[ "$(snapshot_state_bytes)" == "$locked_snapshot" ]] ||
  fail "a refused rollback still modified the deployment"
[[ "$(internal next-due codex)" == "$advanced_due" ]] ||
  fail "the advanced JSON state was lost"
wait "$writer_pid" || fail "the writer failed"
print -r -- "  PASS: the public lifecycle verb waits for run.lock"

# ---------------------------------------------------------------------------
print -r -- "== AB9: cutover cannot interleave with a lock-holding writer (L2) =="
rm -f "$TEST_TEMP_DIR/cutover-ready"
zsh -c '
  source "$1"
  acquire_run_lock || exit 3
  /bin/date +%s >"$2/cutover-ready"
  "$SLEEP_BIN" 3
  release_run_lock
' _ "$SCRIPT_PATH" "$TEST_TEMP_DIR" &
cut_pid=$!
for _ in {1..80}; do
  [[ -e "$TEST_TEMP_DIR/cutover-ready" ]] && break
  "$SLEEP_BIN" 0.1
done
[[ -e "$TEST_TEMP_DIR/cutover-ready" ]] || fail "the second writer never took run.lock"
locked_snapshot="$(snapshot_state_bytes)"
env QUOTA_SENTINEL_RUN_LOCK_WAIT=1 PI_SOURCE_ONLY=0 /bin/zsh "$SCRIPT_PATH" cutover >/dev/null 2>&1 &&
  fail "cutover ran while another process held run.lock"
[[ "$(snapshot_state_bytes)" == "$locked_snapshot" ]] ||
  fail "a refused cutover modified the deployment"
wait "$cut_pid" || fail "the lock holder failed"
print -r -- "  PASS: cutover serializes on the same run.lock, whole roster untouched"

# ---------------------------------------------------------------------------
print -r -- "== AB10: a failing command releases run.lock =="
reset_deployment
run_cli bootstrap-authority --assume-legacy >/dev/null
# Force a failure INSIDE a shell function while the lock is held: the
# scheduler bridge cannot read a corrupt manifest, and the error path must
# still release the lock (zsh skips the EXIT trap for errexit exits that
# happen inside a function).
print -r -- '{ corrupt' >"$MANIFEST"
run_cli check >/dev/null 2>&1 || true
[[ ! -e "$QUOTA_SENTINEL_STATE_DIR/run.lock" ]] ||
  fail "a failed command left run.lock behind"
print -r -- "  PASS: no stale lock after a loud failure"

# ---------------------------------------------------------------------------
print -r -- "== AB11: a corrupt manifest is loud and never overwritten =="
reset_deployment
print -r -- '{"schema_version": 1, "backend": "sqlite", "epoch": 3}' >"$MANIFEST"
corrupt_before="$(cat "$MANIFEST")"
if internal authority >/dev/null 2>&1; then fail "an unknown backend was accepted"; fi
init_rc=0
run_cli bootstrap-authority --assume-legacy >/dev/null 2>&1 || init_rc=$?
(( init_rc != 0 )) || fail "an explicit bootstrap overwrote a corrupt manifest"
[[ "$(cat "$MANIFEST")" == "$corrupt_before" ]] || fail "the corrupt manifest was replaced"
print -r -- "  PASS: corruption blocks the lifecycle and is preserved for diagnosis"

# ---------------------------------------------------------------------------
print -r -- "== AB12: the post-run window + sync path works under JSON =="
reset_deployment
run_cli bootstrap-authority --assume-legacy >/dev/null
run_cli cutover >/dev/null || fail "cutover failed"
now="$(now_epoch)"
future=$(( now + 1800 ))
quota_file="$TEST_TEMP_DIR/codex-quota.json"
print -r -- "{\"source\":\"test\",\"fresh\":true,\"fiveHour\":{\"resetAt\":$future},\"weekly\":{\"resetAt\":$(( future + 500000 ))}}" >"$quota_file"
internal scheduler-commit-success --provider antigravity --now "$now" >/dev/null
bridge_run_logged "state" scheduler-last-window --provider antigravity --reset "$future" >/dev/null
[[ "$(internal state-dump antigravity | grep '^last_triggered_window=')" == "last_triggered_window=$future" ]] ||
  fail "the post-run window record did not reach the JSON document"
internal scheduler-sync --provider antigravity --now "$now" \
  --quota-file "$quota_file" --fresh 1 >/dev/null ||
  fail "post-run sync failed under JSON authority"
[[ "$(internal next-due antigravity)" == "$(( future + RESET_BUFFER_SECONDS ))" ]] ||
  fail "post-run sync did not calibrate the JSON deadline"
print -r -- "  PASS: post-run window and sync are JSON-only"

print -r -- "state authority regression: all cases passed"
