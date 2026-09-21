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
set -euo pipefail

readonly REPO_DIR="${0:A:h}"
readonly LOG_DIR="$HOME/Library/Logs/Quota-Sentinel"
readonly AGENT_DIR="$HOME/Library/LaunchAgents"
readonly DOMAIN="gui/$(id -u)"

# Only the listener is meant to be loaded during normal operation. The
# watchdog and precision timer are kept as disabled rollback artifacts.
readonly ALL_LABELS=(quota-sentinel.feishu-listener quota-sentinel quota-sentinel.timer)
readonly ACTIVE_LABELS=(quota-sentinel.feishu-listener)

readonly MODE="${1:-}"

if [[ -n "$MODE" && "$MODE" != "--load" ]]; then
  print -ru2 -- "usage: ${0:t} [--load]"
  exit 2
fi

command -v plutil >/dev/null || { print -ru2 -- "plutil not found"; exit 1; }

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
  for label in "${ACTIVE_LABELS[@]}"; do
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$AGENT_DIR/$label.plist"
    print -r -- "bootstrapped  $label"
  done
fi
