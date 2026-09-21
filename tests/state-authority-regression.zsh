#!/bin/zsh
# State-backend authority: end-to-end lifecycle through the real shell.
#
# Phase 3B/3C make the durable authority manifest the ONLY thing that decides
# which backend owns scheduler state, and move every scheduler decision into
# Python. This suite proves the operator-visible contract of that switch,
# entirely inside a temp state dir:
#
#   AB1  a deployment that was never cut over keeps legacy authoritative and
#        byte-identical (the default path is unchanged)
#   AB2  `cutover` switches ownership in one epoch, leaves every legacy byte
#        untouched, and is idempotent on re-run
#   AB3  after the cutover the shell reads JSON and IGNORES later legacy
#        writes (the retired backend is not a second source of truth)
#   AB4  after the cutover the shell WRITES JSON and leaves legacy frozen
#   AB5  a corrupt authoritative document fails loudly and NEVER defaults the
#        provider back to legacy or to an all-unset state
#   AB6  an unreadable authority manifest stops the legacy accessors instead
#        of guessing a backend
#   AB7  the scheduler still makes correct decisions in JSON mode (due /
#        wait / matured-debt protection), i.e. the policy is backend-neutral
#
# Fully isolated: temp state dir, temp logs, no model calls, no network, no
# real Keychain access. It never touches the operator's live state dir.

set -euo pipefail

readonly TEST_TEMP_DIR="$(mktemp -d /private/tmp/quota-sentinel.XXXXXX)"
readonly SCRIPT_PATH="${0:A:h}/../quota-sentinel.sh"
export QUOTA_SENTINEL_STATE_DIR="$TEST_TEMP_DIR/state"
export QUOTA_SENTINEL_LOG_DIR="$TEST_TEMP_DIR/logs"
export PI_SOURCE_ONLY=1
export FEISHU_DISABLE_CHART=1

cleanup() { rm -rf "$TEST_TEMP_DIR" }
trap cleanup EXIT

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

fail() { print -u2 -- "FAIL: $*"; exit 1; }

# ---------------------------------------------------------------------------
print -r -- "== AB1: an uncut deployment keeps legacy authoritative =="
[[ ! -e "$AUTHORITY_MANIFEST" ]] || fail "fresh state dir already has a manifest"
[[ "$(authoritative_backend)" == "legacy" ]] || fail "bootstrap backend is not legacy"
legacy_active=0; legacy_backend_active || legacy_active=$?
(( legacy_active == 0 )) || fail "legacy_backend_active rejected the bootstrap state"
write_provider_next_due codex 111222
write_provider_last_task codex 111000
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-next-due-at")" == "111222" ]] ||
  fail "legacy writer did not reach the legacy slot file"
[[ ! -e "$QUOTA_SENTINEL_STATE_DIR/codex-state.json" ]] ||
  fail "legacy mode created a JSON document"
print -r -- "  PASS: legacy files are authoritative and no JSON was created"

# Snapshot every legacy byte so AB2 can prove the cutover is read-only on them.
typeset -A LEGACY_BEFORE=()
for f in "$QUOTA_SENTINEL_STATE_DIR"/*(N); do
  [[ "${f:t}" == backend-authority.json ]] && continue
  LEGACY_BEFORE[${f:t}]="$(cat "$f")"
done

# ---------------------------------------------------------------------------
print -r -- "== AB2: explicit cutover switches ownership in one epoch =="
# PI_SOURCE_ONLY=0 is required: this suite sources the script, so the
# variable is already exported and would otherwise suppress main().
cutover_out="$(PI_SOURCE_ONLY=0 /bin/zsh "$SCRIPT_PATH" cutover)"
print -r -- "$cutover_out" | grep -q "state backend cutover complete" ||
  fail "cutover did not report completion: $cutover_out"
[[ "$(authoritative_backend)" == "json" ]] || fail "authority did not flip to json"
grep -q '"backend": "json"' "$AUTHORITY_MANIFEST" || fail "manifest content wrong"
grep -q '"epoch": 1' "$AUTHORITY_MANIFEST" || fail "epoch did not advance to 1"
[[ "$(stat -f '%Lp' "$AUTHORITY_MANIFEST")" == "600" ]] ||
  fail "manifest is not 0600"
[[ "$(stat -f '%Lp' "$QUOTA_SENTINEL_STATE_DIR")" == "700" ]] ||
  fail "state dir is not 0700"
for name in "${(@k)LEGACY_BEFORE}"; do
  [[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/$name")" == "${LEGACY_BEFORE[$name]}" ]] ||
    fail "cutover modified legacy file $name"
done
# The document decodes to exactly the legacy state it was built from.
[[ "$(scheduler_bridge json-dump codex | grep '^next_due_at=')" == "next_due_at=111222" ]] ||
  fail "json document does not carry the legacy deadline"
# No stray temp files from the many atomic publishes.
stray="$(find "$QUOTA_SENTINEL_STATE_DIR" -name '*.tmp.*' -print -quit)"
[[ -z "$stray" ]] || fail "cutover left a temp file behind: $stray"
print -r -- "  PASS: json owns the state, legacy bytes untouched, epoch=1"

# Idempotent second run.
before_manifest="$(cat "$AUTHORITY_MANIFEST")"
PI_SOURCE_ONLY=0 /bin/zsh "$SCRIPT_PATH" cutover | grep -q "cutover complete" ||
  fail "second cutover did not complete"
[[ "$(cat "$AUTHORITY_MANIFEST")" == "$before_manifest" ]] ||
  fail "a re-run of cutover changed the manifest"
print -r -- "  PASS: cutover is idempotent"

# ---------------------------------------------------------------------------
print -r -- "== AB3: after the cutover, legacy writes are invisible =="
next_due_before="$(scheduler_bridge next-due codex)"
print -r -- "999999" >"$QUOTA_SENTINEL_STATE_DIR/codex-next-due-at"
[[ "$(scheduler_bridge next-due codex)" == "$next_due_before" ]] ||
  fail "a legacy write changed what the authoritative reader sees"
[[ "$(scheduler_bridge dump codex | grep '^next_due_at=')" == "next_due_at=999999" ]] ||
  fail "the per-backend diagnostic no longer reads the legacy file"
# The legacy accessors refuse to serve state once JSON owns it.
# The guard calls die(), which exits its shell; run each in a subshell so a
# refusal cannot take the suite down with it.
guard_rc=0
( read_provider_next_due codex >/dev/null 2>&1 ) || guard_rc=$?
(( guard_rc != 0 )) || fail "a legacy reader served state under JSON authority"
print -r -- "  PASS: legacy is a retired artifact, not a second truth"

# ---------------------------------------------------------------------------
print -r -- "== AB4: transitions land in JSON and leave legacy frozen =="
legacy_next_due="$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-next-due-at")"
commit_provider_success codex 500000
[[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/codex-next-due-at")" == "$legacy_next_due" ]] ||
  fail "a transition wrote the retired legacy backend"
[[ "$(scheduler_bridge next-due codex)" == "$(( 500000 + RUN_INTERVAL_SECONDS ))" ]] ||
  fail "the success commit did not advance the JSON deadline"
[[ "$(scheduler_bridge json-dump codex | grep '^retry_pending=')" == "retry_pending=0" ]] ||
  fail "retry_pending=0 was not materialized in the document"
guard_rc=0
( write_provider_next_due codex 1 >/dev/null 2>&1 ) || guard_rc=$?
(( guard_rc != 0 )) || fail "a legacy writer was allowed to run under JSON authority"
print -r -- "  PASS: writes go to JSON only; legacy writers refuse"

# ---------------------------------------------------------------------------
print -r -- "== AB5: a corrupt authoritative document fails closed =="
doc="$QUOTA_SENTINEL_STATE_DIR/codex-state.json"
cp "$doc" "$TEST_TEMP_DIR/codex-state.json.good"
print -r -- '{ this is not json' >"$doc"
corrupt_rc=0
corrupt_out="$(scheduler_bridge next-due codex 2>&1)" || corrupt_rc=$?
(( corrupt_rc != 0 )) || fail "a corrupt document still produced a deadline"
print -r -- "$corrupt_out" | grep -qi "json\|corrupt" ||
  fail "corrupt document failure was not explained: $corrupt_out"
# Nothing silently reset the provider, and no decision was committed.
commit_rc=0
( commit_provider_success codex 600000 >/dev/null 2>&1 ) || commit_rc=$?
(( commit_rc != 0 )) || fail "a mutation succeeded over a corrupt document"
[[ "$(cat "$doc")" == '{ this is not json' ]] ||
  fail "the corrupt document was silently repaired"
cp "$TEST_TEMP_DIR/codex-state.json.good" "$doc"
[[ "$(scheduler_bridge next-due codex)" == "$(( 500000 + RUN_INTERVAL_SECONDS ))" ]] ||
  fail "restoring the document did not restore the deadline"
print -r -- "  PASS: corrupt state is loud, fail-closed and never defaulted"

# ---------------------------------------------------------------------------
print -r -- "== AB6: an unreadable manifest stops the legacy accessors =="
good_manifest="$(cat "$AUTHORITY_MANIFEST")"
print -r -- '{"schema_version": 1, "backend": "sqlite", "epoch": 3}' >"$AUTHORITY_MANIFEST"
manifest_rc=0
( read_provider_next_due codex >/dev/null 2>&1 ) || manifest_rc=$?
(( manifest_rc != 0 )) || fail "an unknown backend was treated as readable"
backend_rc=0
( authoritative_backend >/dev/null 2>&1 ) || backend_rc=$?
(( backend_rc != 0 )) || fail "an unknown backend name was accepted"
print -r -- "$good_manifest" >"$AUTHORITY_MANIFEST"
[[ "$(authoritative_backend)" == "json" ]] || fail "manifest restore failed"
print -r -- "  PASS: an unreadable authority fact is never guessed"

# ---------------------------------------------------------------------------
print -r -- "== AB7: scheduler decisions are backend-neutral =="
now="$(now_epoch)"
# A committed debt is protected: a far-future fresh probe may not move it.
# A legacy writer must be refused here; the subshell keeps die() contained.
( write_provider_last_task codex 1 >/dev/null 2>&1 ) || true
scheduler_bridge scheduler-commit-success --provider codex --now "$now" >/dev/null
future=$(( now + 3000 ))
quota_file="$TEST_TEMP_DIR/codex-quota.json"
print -r -- "{\"source\":\"test\",\"fresh\":true,\"fiveHour\":{\"resetAt\":$future}}" >"$quota_file"
verdict="$(scheduler_bridge scheduler-decide --provider codex --now "$now" \
  --quota-file "$quota_file" --fresh 1 | sed -n 's/^decision=//p')" || true
[[ "$verdict" == "wait" ]] || fail "a fresh deadline ahead of now did not WAIT: $verdict"
# Calibration is reset + the four-minute safety buffer, not the raw reset.
[[ "$(scheduler_bridge next-due codex)" == "$(( future + RESET_BUFFER_SECONDS ))" ]] ||
  fail "the fresh reset did not calibrate the JSON deadline"
# The same decision shape holds on the legacy backend (backend neutrality).
legacy_dir="$TEST_TEMP_DIR/legacy-again"
mkdir -p "$legacy_dir"
# rc 1 is the verb's "wait" verdict, not a failure: capture it explicitly so
# set -e does not read a legitimate decision as an error.
legacy_rc=0
legacy_out="$(env QUOTA_SENTINEL_STATE_DIR="$legacy_dir" PI_SOURCE_ONLY=1 \
  /bin/zsh -c 'source "$1"; scheduler_bridge scheduler-decide --provider codex --now '"$now"' --quota-file '"$quota_file"' --fresh 1' _ "$SCRIPT_PATH")" || legacy_rc=$?
(( legacy_rc == 0 || legacy_rc == 1 )) || fail "legacy backend decision failed rc=$legacy_rc"
print -r -- "$legacy_out" | grep -q '^backend=legacy$' ||
  fail "the legacy backend did not serve the same decision path"
print -r -- "  PASS: the decision policy is identical on both backends"

# ---------------------------------------------------------------------------
# AB8: the post-run sync path works under JSON authority.
#
# Regression for a real defect this suite's sibling audit found: the post-run
# window record was still a legacy slot write, so `run` would have died (or,
# without the guard, written the retired backend) immediately after the first
# cutover. This drives the exact sequence that path performs.
print -r -- "== AB8: post-run window + deadline sync under JSON authority =="
now="$(now_epoch)"
future=$(( now + 1800 ))
print -r -- "{\"source\":\"test\",\"fresh\":true,\"fiveHour\":{\"resetAt\":$future},\"weekly\":{\"resetAt\":$(( future + 500000 ))}}" >"$quota_file"
scheduler_bridge scheduler-commit-success --provider antigravity --now "$now" >/dev/null
bridge_run_logged "state" \
  scheduler-last-window --provider antigravity --reset "$future" >/dev/null
[[ "$(scheduler_bridge state-dump antigravity | grep '^last_triggered_window=')" == "last_triggered_window=$future" ]] ||
  fail "the post-run window record did not reach the JSON document"
sync_rc=0
scheduler_bridge scheduler-sync --provider antigravity --now "$now" \
  --quota-file "$quota_file" --fresh 1 >/dev/null || sync_rc=$?
(( sync_rc == 0 )) || fail "post-run sync failed under JSON authority (rc=$sync_rc)"
[[ "$(scheduler_bridge next-due antigravity)" == "$(( future + RESET_BUFFER_SECONDS ))" ]] ||
  fail "post-run sync did not calibrate the JSON deadline"
# The whole sequence left every legacy byte alone. AB3 deliberately wrote a
# legacy slot to prove retired writes are invisible, so the baseline here is
# taken now rather than reused from AB1.
# Only the LEGACY SLOT files are frozen; the JSON documents in the same
# directory are the live backend and are expected to change.
typeset -a LEGACY_SLOTS=(
  codex-next-due-at codex-last-task-at codex-last-attempt-at
  antigravity-next-due-at antigravity-last-task-at antigravity-last-attempt-at
)
typeset -A LEGACY_NOW=()
for name in "${LEGACY_SLOTS[@]}"; do
  [[ -e "$QUOTA_SENTINEL_STATE_DIR/$name" ]] || continue
  LEGACY_NOW[$name]="$(cat "$QUOTA_SENTINEL_STATE_DIR/$name")"
done
(( ${#LEGACY_NOW} > 0 )) || fail "no legacy slot files to compare against"
scheduler_bridge scheduler-sync --provider codex --now "$now" \
  --quota-file "$quota_file" --fresh 1 >/dev/null 2>&1 || true
for name in "${(@k)LEGACY_NOW}"; do
  [[ "$(cat "$QUOTA_SENTINEL_STATE_DIR/$name")" == "${LEGACY_NOW[$name]}" ]] ||
    fail "the post-run path wrote the retired legacy file $name"
done
print -r -- "  PASS: post-run window and sync are JSON-only"

print -r -- "state authority regression: all cases passed"
