#!/bin/zsh

set -euo pipefail

readonly TEST_DIR="${0:A:h}"
PI_SOURCE_ONLY=1 source "$TEST_DIR/../quota-sentinel.sh"

LAST_TEMP_DIR="$(mktemp -d /private/tmp/quota-sentinel.XXXXXX)"
trap cleanup EXIT
ensure_temp_dir

cp "$TEST_DIR/quota-fixture.json" "$LAST_TEMP_DIR/quota.json"
cp "$TEST_DIR/antigravity-quota-fixture.json" "$LAST_TEMP_DIR/antigravity-quota.json"
CODEX_QUOTA_FILE="$LAST_TEMP_DIR/quota.json"
ANTIGRAVITY_QUOTA_FILE="$LAST_TEMP_DIR/antigravity-quota.json"
use_pi_or_saved_codex_fallback
use_pi_or_saved_antigravity_fallback
export CURRENT_FORMAT_TIME=1788007800

codex_output="$(codex_quota_message)"
codex_expected=$'5 小时：■■■■■□□□□□ 剩余 48%\n重置：2026-08-29 22:04:34 CST（剩余 1小时 14分）\n周额度：■■■■■■■□□□ 剩余 65%\n重置：2026-09-04 08:01:57 CST（剩余 5天 11小时 11分）\n来源：Pi 快照（可能不是最新）'
[[ "$codex_output" == "$codex_expected" ]]

antigravity_output="$(antigravity_quota_message)"
antigravity_expected=$'5 小时：■■■■■■■■■■ 剩余 100%\n重置：2026-08-30 00:21:14 CST（剩余 3小时 31分）\n周额度：■■■■■■■■■□ 剩余 91%\n重置：2026-09-03 16:41:58 CST（剩余 4天 19小时 51分）\n来源：Pi 快照（可能不是最新）'
[[ "$antigravity_output" == "$antigravity_expected" ]]

cleanup
[[ ! -e "$LAST_TEMP_DIR" ]]
LAST_TEMP_DIR=""
print -r -- "quota regression: ok"

