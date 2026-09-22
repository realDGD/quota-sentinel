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
# This script does NOT switch the state backend, and it does NOT create the
# authority manifest. Upgrading the code, asserting ownership and moving
# ownership are three separate actions on purpose: an operator can install,
# watch the existing backend behave, and only then run
# `./quota-sentinel.sh cutover`. The installer REQUIRES an existing,
# parseable authority manifest and refuses to guess one.
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
# Authority manifest: VALIDATED, never created.
#
# The manifest is the single durable ownership fact and this installer is not
# allowed to guess it. "No manifest" and "pre-protocol legacy deployment" are
# indistinguishable from the state directory alone, so an installer that
# wrote `legacy` here would silently re-legitimize a deployment that had
# already cut over and lost its manifest — resurrecting the deadlines and
# retry debt the authoritative JSON documents have moved past.
#
# This step is therefore strictly READ-ONLY: it requires an existing,
# parseable manifest and fails — before any launchctl call and before any
# label is rendered — when there is none. Creating one is an explicit
# operator decision, never an installer side effect:
#
#     ./quota-sentinel.sh bootstrap-authority --assume-legacy
#
# ORDERING: this runs BEFORE the agents are (re)started, so a deployment
# whose owner is unknown never gets a scheduler started over it. The retired
# schedulers are then stopped before the new listener starts, so no old
# writer is ever alive alongside the new one.
# ---------------------------------------------------------------------------
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
authority_out="$(
  PYTHONPATH="$REPO_DIR" "$PYTHON3_BIN" -S -m quota_sentinel \
    --state-dir "$STATE_DIR" authority 2>&1
)" || {
  print -ru2 -- "authority manifest missing or unreadable:"
  print -ru2 -- "$authority_out"
  print -ru2 -- ""
  print -ru2 -- "Refusing to install agents over a state directory whose owner is unknown."
  print -ru2 -- "This installer never creates the authority manifest: a missing manifest"
  print -ru2 -- "means the owner is UNKNOWN, not that it is legacy, and a deployment that"
  print -ru2 -- "cut over and then lost it looks exactly the same from here."
  print -ru2 -- ""
  print -ru2 -- "  * manifest lost -> restore backend-authority.json from backup; do NOT recreate it."
  print -ru2 -- "  * confirmed pre-protocol deployment -> assert it once, explicitly:"
  print -ru2 -- "      ./quota-sentinel.sh bootstrap-authority --assume-legacy"
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
