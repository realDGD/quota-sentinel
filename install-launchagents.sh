#!/bin/zsh
#
# Render this checkout's LaunchAgent templates and install them into
# ~/Library/LaunchAgents.
#
# launchd does not expand "~" or "$HOME" in ProgramArguments, so the plists
# need literal absolute paths. The committed *.plist.template files carry
# __REPO_DIR__ / __LOG_DIR__ placeholders instead, and this script substitutes
# the paths for wherever the checkout actually lives. The rendered *.plist
# files are gitignored so no local path ever enters the repository.
#
# Usage:
#   ./install-launchagents.sh          render + install into ~/Library/LaunchAgents
#   ./install-launchagents.sh --load   ... and bootstrap the active listener
#
# This script does NOT switch the state backend. Upgrading the code and
# moving ownership are separate actions on purpose, so an operator can
# install, watch the existing (legacy) backend behave, and only then run
# `./quota-sentinel.sh cutover`.
#
set -euo pipefail

readonly REPO_DIR="${0:A:h}"
readonly LOG_DIR="$HOME/Library/Logs/Quota-Sentinel"
readonly AGENT_DIR="$HOME/Library/LaunchAgents"
readonly DOMAIN="gui/$(id -u)"

# The scheduler's state dir, matching the shell's default and override.
readonly STATE_DIR="${QUOTA_SENTINEL_STATE_DIR:-$HOME/Library/Application Support/Quota-Sentinel}"
readonly PYTHON3_BIN="${QUOTA_SENTINEL_PYTHON3_BIN:-/usr/bin/python3}"

# Only the listener is meant to be loaded during normal operation. The
# watchdog and precision timer are kept as disabled rollback artifacts.
readonly ALL_LABELS=(quota-sentinel.feishu-listener quota-sentinel quota-sentinel.timer)
readonly ACTIVE_LABELS=(quota-sentinel.feishu-listener)
# Loaded by an older installation, and retired by this one. Leaving them
# loaded would keep a second scheduling entry point alive next to the
# listener/orchestrator, which is exactly the "two schedulers" state the
# upgrade must not leave behind.
readonly RETIRED_LABELS=(quota-sentinel quota-sentinel.timer)

readonly MODE="${1:-}"

if [[ -n "$MODE" && "$MODE" != "--load" ]]; then
  print -ru2 -- "usage: ${0:t} [--load]"
  exit 2
fi

command -v plutil >/dev/null || { print -ru2 -- "plutil not found"; exit 1; }

readonly LAUNCHCTL_BIN="${QUOTA_SENTINEL_LAUNCHCTL_BIN:-/bin/launchctl}"
[[ -x "$LAUNCHCTL_BIN" ]] || {
  print -ru2 -- "launchctl not found at $LAUNCHCTL_BIN"
  exit 1
}

# The listener LaunchAgent runs inside the project's uv environment with
# --frozen --no-sync, i.e. it NEVER resolves or syncs at start (no network,
# no lock drift, no self-mutating daemon). The environment is therefore a
# SETUP obligation of this installer: verify uv, then sync strictly from
# the committed lockfile before any agent is rendered or loaded.
readonly UV_BIN="${QUOTA_SENTINEL_UV_BIN:-/opt/homebrew/bin/uv}"
[[ -x "$UV_BIN" ]] || {
  print -ru2 -- "uv not found at $UV_BIN — install it first (brew install uv)"
  exit 1
}
"$UV_BIN" sync --locked --project "$REPO_DIR" >/dev/null || {
  print -ru2 -- "uv sync --locked failed: environment missing, incomplete, or pyproject/uv.lock drifted; fix before installing agents"
  exit 1
}
print -r -- "synced     $REPO_DIR/.venv (uv.lock verified)"

# ---------------------------------------------------------------------------
# Authority manifest.
#
# Every initialized deployment has one, and the runtime REQUIRES it: "no
# manifest" is only allowed to mean something at a lifecycle boundary, and
# this is that boundary. `authority-initialize` is idempotent (an existing
# manifest wins and is never rewritten) and refuses to touch a corrupt one,
# so re-running the installer can never destroy the ownership fact.
#
# It takes the scheduler's real run.lock itself, so it cannot interleave
# with an in-flight check or model run.
#
# ORDERING: this runs BEFORE the agents are (re)started. That is safe
# because initialization only records the authority that is already in
# effect (legacy for a deployment that predates the protocol); it reads no
# scheduler state and starts no process. The retired schedulers are then
# stopped before the new listener starts, so no old writer is ever alive
# alongside the new one.
# ---------------------------------------------------------------------------
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
authority_out="$(
  PYTHONPATH="$REPO_DIR" "$PYTHON3_BIN" -S -m quota_sentinel \
    --state-dir "$STATE_DIR" authority-initialize 2>&1
)" || {
  print -ru2 -- "authority initialization failed:"
  print -ru2 -- "$authority_out"
  print -ru2 -- "refusing to install agents over an uninitialized or unreadable authority manifest"
  exit 1
}
print -r -- "authority  $(print -r -- "$authority_out" | head -1)"

mkdir -p "$LOG_DIR" "$AGENT_DIR"
chmod 700 "$LOG_DIR"

for label in "${ALL_LABELS[@]}"; do
  tpl="$REPO_DIR/$label.plist.template"
  out="$REPO_DIR/$label.plist"

  [[ -r "$tpl" ]] || { print -ru2 -- "missing template: $tpl"; exit 1; }

  sed -e "s|__REPO_DIR__|$REPO_DIR|g" \
      -e "s|__LOG_DIR__|$LOG_DIR|g" \
      "$tpl" >"$out"

  if grep -q '__REPO_DIR__\|__LOG_DIR__' "$out"; then
    print -ru2 -- "unrendered placeholder remains in $out"
    exit 1
  fi

  plutil -lint "$out" >/dev/null || { print -ru2 -- "invalid plist: $out"; exit 1; }

  cp -f "$out" "$AGENT_DIR/$label.plist"
  print -r -- "rendered  $AGENT_DIR/$label.plist"
done

if [[ "$MODE" == "--load" ]]; then
  # Retire the legacy schedulers FIRST. `bootout` is idempotent here: a
  # label that was never loaded exits non-zero, which is not an error —
  # the goal is the post-condition "not loaded", not the transition.
  for label in "${RETIRED_LABELS[@]}"; do
    if "$LAUNCHCTL_BIN" bootout "$DOMAIN/$label" 2>/dev/null; then
      print -r -- "retired      $label (was loaded)"
    else
      print -r -- "retired      $label (already not loaded)"
    fi
  done
  for label in "${ACTIVE_LABELS[@]}"; do
    "$LAUNCHCTL_BIN" bootout "$DOMAIN/$label" 2>/dev/null || true
    "$LAUNCHCTL_BIN" bootstrap "$DOMAIN" "$AGENT_DIR/$label.plist"
    print -r -- "bootstrapped  $label"
  done
fi
