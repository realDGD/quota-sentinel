#!/bin/zsh
# Installer upgrade regression (I1-I5).
#
# `install-launchagents.sh --load` is the upgrade entry point, and two of its
# post-conditions are correctness properties rather than conveniences:
#
#   * the RETIRED scheduler agents (the legacy watchdog and precision timer)
#     must not be left loaded next to the listener/orchestrator — a second
#     scheduling entry point is exactly what the upgrade is supposed to end;
#   * the authority manifest must exist afterwards, because the runtime
#     refuses to guess a backend when it is missing, and an operator who
#     upgraded but never initialized would otherwise meet that refusal on
#     the very next command.
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

run_installer() {
  ( cd "$FAKE_REPO" && /bin/zsh ./install-launchagents.sh "$@" )
}

reset_logs() { : >"$LAUNCHCTL_LOG"; : >"$UV_LOG"; }
reset_state() {
  rm -rf "$STATE_DIR"
  mkdir -p "$STATE_DIR"
}
reset_agents() { rm -f "$FAKE_HOME/Library/LaunchAgents"/*.plist(N) 2>/dev/null || true; }

# ---------------------------------------------------------------------------
print -r -- "== I5: render-only mode never touches launchctl =="
reset_logs; reset_state
run_installer >/dev/null || fail "render-only install failed"
[[ ! -s "$LAUNCHCTL_LOG" ]] || fail "render-only mode called launchctl: $(cat "$LAUNCHCTL_LOG")"
[[ -e "$FAKE_HOME/Library/LaunchAgents/quota-sentinel.feishu-listener.plist" ]] ||
  fail "render-only mode did not install the listener plist"
# It DOES initialize authority: the runtime requires the manifest.
[[ -e "$STATE_DIR/backend-authority.json" ]] ||
  fail "installer did not initialize the authority manifest"
grep -q '"backend": "legacy"' "$STATE_DIR/backend-authority.json" ||
  fail "installer initialized an unexpected backend"
print -r -- "  PASS: render + authority init, zero launchctl calls"

# ---------------------------------------------------------------------------
print -r -- "== I1: --load retires the legacy schedulers, then starts the listener =="
reset_logs; reset_state; reset_agents
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
reset_logs; reset_state; reset_agents
FAKE_LAUNCHCTL_ABSENT="quota-sentinel quota-sentinel.timer" \
  run_installer --load >/dev/null || fail "install failed when retired labels were absent"
grep -q "bootstrap .*quota-sentinel.feishu-listener.plist" "$LAUNCHCTL_LOG" ||
  fail "listener was not bootstrapped when retired labels were absent"
print -r -- "  PASS: bootout is idempotent for never-loaded labels"

# ---------------------------------------------------------------------------
print -r -- "== I3: a failed uv sync installs nothing =="
reset_logs; reset_state; reset_agents
if FAKE_UV_RC=1 run_installer --load >/dev/null 2>&1; then
  fail "installer succeeded despite a failing uv sync"
fi
[[ ! -s "$LAUNCHCTL_LOG" ]] || fail "launchctl ran after a failed uv sync"
[[ ! -e "$STATE_DIR/backend-authority.json" ]] ||
  fail "authority was initialized after a failed uv sync"
print -r -- "  PASS: zero bootstrap on environment failure"

# ---------------------------------------------------------------------------
print -r -- "== I4: an unreadable authority manifest installs nothing =="
reset_logs; reset_state; reset_agents
print -r -- '{ not json' >"$STATE_DIR/backend-authority.json"
manifest_before="$(cat "$STATE_DIR/backend-authority.json")"
if run_installer --load >/dev/null 2>&1; then
  fail "installer succeeded over a corrupt authority manifest"
fi
[[ ! -s "$LAUNCHCTL_LOG" ]] || fail "launchctl ran over a corrupt manifest"
[[ "$(cat "$STATE_DIR/backend-authority.json")" == "$manifest_before" ]] ||
  fail "the corrupt manifest was overwritten"
print -r -- "  PASS: corrupt authority is loud, untouched, and blocks the install"

# ---------------------------------------------------------------------------
print -r -- "== I6: an EXISTING manifest is never rewritten =="
reset_logs; reset_state; reset_agents
run_installer >/dev/null || fail "first install failed"
python3 - <<'PY' || fail "could not prepare an initialized-json deployment"
import json, os, pathlib
p = pathlib.Path(os.environ["QUOTA_SENTINEL_STATE_DIR"]) / "backend-authority.json"
doc = json.loads(p.read_text())
doc["backend"] = "json"
doc["epoch"] = 7
p.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n")
PY
before="$(cat "$STATE_DIR/backend-authority.json")"
run_installer --load >/dev/null || fail "re-install over an initialized deployment failed"
[[ "$(cat "$STATE_DIR/backend-authority.json")" == "$before" ]] ||
  fail "the installer rewrote an existing authority manifest"
print -r -- "  PASS: initialization is idempotent and never downgrades JSON to legacy"

# ---------------------------------------------------------------------------
print -r -- "== I7: the installer never cuts over on its own =="
reset_logs; reset_state; reset_agents
run_installer --load >/dev/null || fail "--load install failed"
grep -q '"backend": "legacy"' "$STATE_DIR/backend-authority.json" ||
  fail "installer changed the backend without being asked"
[[ ! -e "$STATE_DIR/codex-state.json" ]] ||
  fail "installer performed a cutover (JSON documents were created)"
print -r -- "  PASS: upgrade and ownership switch stay separate actions"

print -r -- "installer upgrade regression: all cases passed"
