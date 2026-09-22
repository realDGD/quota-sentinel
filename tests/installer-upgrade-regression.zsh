#!/bin/zsh
# Installer upgrade regression (I1-I9).
#
# `install-launchagents.sh --load` is the upgrade entry point, and three of
# its post-conditions are correctness properties rather than conveniences:
#
#   * the RETIRED scheduler agents (the legacy watchdog and precision timer)
#     must not be left loaded next to the listener/orchestrator — a second
#     scheduling entry point is exactly what the upgrade is supposed to end;
#   * the installer NEVER creates the authority manifest. A missing manifest
#     means the owner of the state is UNKNOWN, not that it is legacy, and a
#     deployment that cut over and then lost it looks identical from the
#     state directory alone. So the installer REQUIRES an existing,
#     parseable manifest and fails — with zero launchctl calls and zero
#     writes — when there is none;
#   * an existing manifest is READ, never rewritten: re-running the upgrade
#     cannot downgrade json to legacy or advance an epoch.
#
# Nothing here touches the real machine. The suite builds a throwaway REPO
# (so rendered plists never land in the checkout), a throwaway HOME, a fake
# `uv`, a fake `launchctl` that records its argv, and a temp state dir.
# It never calls the real launchctl and never runs a real cutover.

set -euo pipefail

readonly TEST_TEMP_DIR="$(mktemp -d /private/tmp/quota-sentinel.XXXXXX)"
cleanup() { rm -rf "$TEST_TEMP_DIR"; }
trap cleanup EXIT

readonly SRC_REPO="${0:A:h}/.."
readonly FAKE_REPO="$TEST_TEMP_DIR/repo"
readonly FAKE_HOME="$TEST_TEMP_DIR/home"
readonly FAKE_BIN="$TEST_TEMP_DIR/bin"
readonly LAUNCHCTL_LOG="$TEST_TEMP_DIR/launchctl.log"
readonly UV_LOG="$TEST_TEMP_DIR/uv.log"
readonly STATE_DIR="$TEST_TEMP_DIR/state"
readonly MANIFEST_NAME="backend-authority.json"

fail() { print -u2 -- "FAIL: $*"; exit 1; }

# ---- throwaway repo: installer + everything it needs ----------------------
mkdir -p "$FAKE_REPO" "$FAKE_HOME/Library/LaunchAgents" "$FAKE_BIN" "$STATE_DIR"
for f in install-launchagents.sh quota-sentinel.sh pyproject.toml uv.lock; do
  cp "$SRC_REPO/$f" "$FAKE_REPO/$f"
done
for f in "$SRC_REPO"/*.plist.template; do
  cp "$f" "$FAKE_REPO/${f:t}"
done
cp -R "$SRC_REPO/quota_sentinel" "$FAKE_REPO/quota_sentinel"
find "$FAKE_REPO/quota_sentinel" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
chmod +x "$FAKE_REPO/install-launchagents.sh"

# ---- fake uv --------------------------------------------------------------
cat >"$FAKE_BIN/uv" <<'MOCK'
#!/bin/zsh
print -r -- "sync $*" >>"$UV_LOG"
exit "${FAKE_UV_RC:-0}"
MOCK
chmod +x "$FAKE_BIN/uv"

# ---- fake launchctl -------------------------------------------------------
cat >"$FAKE_BIN/launchctl" <<'MOCK'
#!/bin/zsh
print -r -- "$*" >>"$LAUNCHCTL_LOG"
case "${1:-}" in
  bootout)
    # FAKE_LAUNCHCTL_ABSENT lists labels that were never loaded; booting
    # one out must look like a failure, exactly as real launchctl does.
    for lbl in ${=FAKE_LAUNCHCTL_ABSENT:-}; do
      [[ "${2:-}" == */$lbl ]] && exit 3
    done
    exit 0
    ;;
  bootstrap) exit 0 ;;
esac
exit 0
MOCK
chmod +x "$FAKE_BIN/launchctl"

export PATH="$FAKE_BIN:/usr/bin:/bin:/usr/sbin:/sbin"
export HOME="$FAKE_HOME"
export UV_LOG LAUNCHCTL_LOG
export QUOTA_SENTINEL_UV_BIN="$FAKE_BIN/uv"
export QUOTA_SENTINEL_LAUNCHCTL_BIN="$FAKE_BIN/launchctl"
export QUOTA_SENTINEL_STATE_DIR="$STATE_DIR"
export QUOTA_SENTINEL_PYTHON3_BIN="/usr/bin/python3"

readonly MANIFEST="$STATE_DIR/$MANIFEST_NAME"

run_installer() {
  ( cd "$FAKE_REPO" && /bin/zsh ./install-launchagents.sh "$@" )
}

reset_logs() { : >"$LAUNCHCTL_LOG"; : >"$UV_LOG"; }
reset_state() {
  rm -rf "$STATE_DIR"
  mkdir -p "$STATE_DIR"
}
reset_agents() { rm -f "$FAKE_HOME/Library/LaunchAgents"/*.plist(N) 2>/dev/null || true; }

# Seed the ownership fact the way an OPERATOR does — never the way the
# installer used to. The installer is not allowed to do this at all.
seed_legacy_authority() {
  reset_state
  (
    cd "$FAKE_REPO" &&
      PI_SOURCE_ONLY=0 /bin/zsh ./quota-sentinel.sh bootstrap-authority --assume-legacy
  ) >/dev/null 2>&1 || fail "could not seed a legacy authority manifest"
  [[ -e "$MANIFEST" ]] || fail "the seeding assertion created no manifest"
}

# ---------------------------------------------------------------------------
print -r -- "== I8: no manifest means NO install (never an inferred legacy) =="
reset_logs; reset_state; reset_agents
if run_installer >/dev/null 2>&1; then
  fail "the installer succeeded on a state dir with no authority manifest"
fi
[[ ! -e "$MANIFEST" ]] || fail "the installer created the authority manifest"
[[ ! -s "$LAUNCHCTL_LOG" ]] || fail "launchctl ran with no authority manifest"
[[ -z "$(print -l "$FAKE_HOME/Library/LaunchAgents"/*.plist(N) 2>/dev/null)" ]] ||
  fail "the installer rendered plists with no authority manifest"
failure_out="$(run_installer 2>&1 || true)"
print -r -- "$failure_out" | grep -q "bootstrap-authority --assume-legacy" ||
  fail "the refusal did not name the explicit operator remedy: $failure_out"
print -r -- "  PASS: unknown ownership blocks the upgrade and creates nothing"

# ---------------------------------------------------------------------------
print -r -- "== I5: render-only mode never touches launchctl =="
seed_legacy_authority; reset_logs; reset_agents
manifest_before="$(cat "$MANIFEST")"
run_installer >/dev/null || fail "render-only install failed"
[[ ! -s "$LAUNCHCTL_LOG" ]] || fail "render-only mode called launchctl: $(cat "$LAUNCHCTL_LOG")"
[[ -e "$FAKE_HOME/Library/LaunchAgents/quota-sentinel.feishu-listener.plist" ]] ||
  fail "render-only mode did not install the listener plist"
[[ "$(cat "$MANIFEST")" == "$manifest_before" ]] ||
  fail "render-only mode rewrote the authority manifest"
print -r -- "  PASS: render only, authority read and left byte-identical"

# ---------------------------------------------------------------------------
print -r -- "== I1: --load retires the legacy schedulers, then starts the listener =="
seed_legacy_authority; reset_logs; reset_agents
run_installer --load >/dev/null || fail "--load install failed"
expected="bootout gui/$(id -u)/quota-sentinel
bootout gui/$(id -u)/quota-sentinel.timer
bootout gui/$(id -u)/quota-sentinel.feishu-listener
bootstrap gui/$(id -u) $FAKE_HOME/Library/LaunchAgents/quota-sentinel.feishu-listener.plist"
actual="$(cat "$LAUNCHCTL_LOG")"
[[ "$actual" == "$expected" ]] || {
  print -u2 -- "unexpected launchctl sequence:"
  print -u2 -- "$actual"
  exit 1
}
print -r -- "  PASS: retired watchdog + timer, then listener restart (in order)"

# ---------------------------------------------------------------------------
print -r -- "== I2: retired labels that were never loaded are not an error =="
seed_legacy_authority; reset_logs; reset_agents
FAKE_LAUNCHCTL_ABSENT="quota-sentinel quota-sentinel.timer" \
  run_installer --load >/dev/null || fail "install failed when retired labels were absent"
grep -q "bootstrap .*quota-sentinel.feishu-listener.plist" "$LAUNCHCTL_LOG" ||
  fail "listener was not bootstrapped when retired labels were absent"
print -r -- "  PASS: bootout is idempotent for never-loaded labels"

# ---------------------------------------------------------------------------
print -r -- "== I3: a failed uv sync installs nothing =="
seed_legacy_authority; reset_logs; reset_agents
manifest_before="$(cat "$MANIFEST")"
if FAKE_UV_RC=1 run_installer --load >/dev/null 2>&1; then
  fail "installer succeeded despite a failing uv sync"
fi
[[ ! -s "$LAUNCHCTL_LOG" ]] || fail "launchctl ran after a failed uv sync"
[[ "$(cat "$MANIFEST")" == "$manifest_before" ]] ||
  fail "the authority manifest changed after a failed uv sync"
print -r -- "  PASS: zero bootstrap on environment failure"

# ---------------------------------------------------------------------------
print -r -- "== I4: an unreadable authority manifest installs nothing =="
reset_logs; reset_state; reset_agents
print -r -- '{ not json' >"$MANIFEST"
manifest_before="$(cat "$MANIFEST")"
if run_installer --load >/dev/null 2>&1; then
  fail "installer succeeded over a corrupt authority manifest"
fi
[[ ! -s "$LAUNCHCTL_LOG" ]] || fail "launchctl ran over a corrupt manifest"
[[ "$(cat "$MANIFEST")" == "$manifest_before" ]] ||
  fail "the corrupt manifest was overwritten"
print -r -- "  PASS: corrupt authority is loud, untouched, and blocks the install"

# ---------------------------------------------------------------------------
print -r -- "== I6: an EXISTING manifest is never rewritten =="
reset_logs; reset_state; reset_agents
print -r -- '{"schema_version": 1, "backend": "json", "epoch": 7}' >"$MANIFEST"
before="$(cat "$MANIFEST")"
run_installer --load >/dev/null || fail "install over an initialized deployment failed"
[[ "$(cat "$MANIFEST")" == "$before" ]] ||
  fail "the installer rewrote an existing authority manifest"
print -r -- "  PASS: upgrade reads ownership and never downgrades JSON to legacy"

# ---------------------------------------------------------------------------
print -r -- "== I7: the installer never cuts over on its own =="
seed_legacy_authority; reset_logs; reset_agents
run_installer --load >/dev/null || fail "--load install failed"
grep -q '"backend": "legacy"' "$MANIFEST" ||
  fail "installer changed the backend without being asked"
[[ ! -e "$STATE_DIR/codex-state.json" ]] ||
  fail "installer performed a cutover (JSON documents were created)"
print -r -- "  PASS: upgrade and ownership switch stay separate actions"

# ---------------------------------------------------------------------------
print -r -- "== I9: deleting a manifest after cutover is never self-healed =="
reset_logs; reset_state; reset_agents
# A cut-over deployment: JSON owns the state and has moved past legacy.
print -r -- '{"schema_version": 1, "backend": "json", "epoch": 4}' >"$MANIFEST"
print -r -- '{"schema_version": 1, "provider": "codex"}' >"$STATE_DIR/codex-state.json"
json_before="$(cat "$STATE_DIR/codex-state.json")"
rm -f "$MANIFEST"
if run_installer --load >/dev/null 2>&1; then
  fail "the installer succeeded after the manifest was deleted"
fi
[[ ! -e "$MANIFEST" ]] ||
  fail "the installer recreated a deleted manifest as legacy epoch 0"
[[ "$(cat "$STATE_DIR/codex-state.json")" == "$json_before" ]] ||
  fail "a refused install modified the authoritative JSON document"
[[ ! -s "$LAUNCHCTL_LOG" ]] || fail "launchctl ran with unknown ownership"
# ...and the explicit operator assertion is the ONLY thing that may decide.
( cd "$FAKE_REPO" && PI_SOURCE_ONLY=0 /bin/zsh ./quota-sentinel.sh bootstrap-authority ) \
  >/dev/null 2>&1 && fail "the operator verb bootstrapped without --assume-legacy"
[[ ! -e "$MANIFEST" ]] || fail "a refused operator bootstrap created a manifest"
print -r -- "  PASS: lost ownership stays lost until an operator says otherwise"

print -r -- "installer upgrade regression: all cases passed"
