#!/bin/zsh

set -euo pipefail

readonly TEST_DIR="${0:A:h}"
PI_SOURCE_ONLY=1 source "$TEST_DIR/../quota-sentinel.sh"

message=$'时间：2026-08-29 20:30:00 CST\n【GPT-5.6 Luna】\n5 小时：■■■■■□□□□□ 剩余 48%\n带 "引号" 和 \\ 反斜杠'
payload="$(feishu_message_payload "test-user" "$message" "quota-sentinel-regression")"

print -r -- "$payload" |
  "$JQ_BIN" -e \
    --arg receive_id "test-user" \
    --arg text "$message" \
    --arg uuid "quota-sentinel-regression" \
    '.receive_id == $receive_id and
     .msg_type == "interactive" and
     .uuid == $uuid and
     (.content | type) == "string" and
     ((.content | fromjson) as $card |
       $card.header.template == "green" and
       $card.header.title.content == "AI 模型运行与配额" and
       $card.elements[0].tag == "div" and
       $card.elements[0].text.tag == "lark_md" and
       $card.elements[0].text.content == $text and
       $card.elements[1].tag == "hr" and
       $card.elements[2].tag == "note")' >/dev/null

failed_payload="$(feishu_message_payload "test-user" "发送失败" "quota-sentinel-failed")"
print -r -- "$failed_payload" |
  "$JQ_BIN" -e '(.content | fromjson | .header.template) == "red"' >/dev/null

card_message="$(notification_message \
  "发送成功" \
  $'来源：Native · codex app-server\n5 小时：■■■■■□□□□□ 剩余 48%\n距离重置：1小时 14分\n重置时间：2026-08-29 22:04:34 CST\n\n周额度：■■■■■■■□□□ 剩余 65%\n距离重置：5天 11小时 11分\n重置时间：2026-09-04 08:01:57 CST' \
  "发送失败" \
  $'来源：不可用\n5 小时：□□□□□□□□□□ 获取失败\n距离重置：未知\n重置时间：未知\n\n周额度：□□□□□□□□□□ 获取失败\n距离重置：未知\n重置时间：未知')"
[[ "$card_message" == *'🟢 **成功**'* ]]
[[ "$card_message" == *'🔴 **失败**'* ]]
[[ "$card_message" == *'**5 小时**'* ]]
[[ "$card_message" == *'**周额度**'* ]]
[[ "$card_message" == *'距离重置　'* ]]
[[ "$card_message" == *'重置时间　'* ]]
[[ "$card_message" != *'↳'* ]]
[[ "$card_message" != *'（剩余'* ]]
[[ "$card_message" != *'来源　'* ]]

# -------------------------------------------------------------
# Case 1: Codex Auto Task Order
# -------------------------------------------------------------
c_task="$(format_provider_card_section "**GPT-5.6 Luna**" "发送成功" $'来源：Native · codex app-server\n5 小时：■■■■■■■■■■ 剩余 100%\n距离重置：4小时 58分\n重置时间：2026-08-30 17:32:52 CST\n\n周额度：■■■■■■■■□□ 剩余 84%\n距离重置：6天 21小时 30分\n重置时间：2026-09-06 07:32:52 CST')"
c_expected=$'**GPT-5.6 Luna**    🟢 **成功**\nNative · codex app-server\n\n**5 小时**　■■■■■■■■■■ 剩余 100%\n距离重置　4小时 58分\n重置时间　2026-08-30 17:32:52 CST\n\n**周额度**　■■■■■■■■□□ 剩余 84%\n距离重置　6天 21小时 30分\n重置时间　2026-09-06 07:32:52 CST'
[[ "$c_task" == "$c_expected" ]]

# -------------------------------------------------------------
# Case 2: Antigravity Auto Task Order
# -------------------------------------------------------------
a_task="$(format_provider_card_section "**Gemini 3.7 Flash · Low**" "发送成功" $'来源：Native · agy local service\n5 小时：■■■■■■■■□□ 剩余 77%\n距离重置：4小时 55分\n重置时间：2026-08-30 18:56:55 CST\n\n周额度：■■■■■■■■■□ 剩余 86%\n距离重置：4天 06小时 41分\n重置时间：2026-09-03 16:41:58 CST')"
a_expected=$'**Gemini 3.7 Flash · Low**    🟢 **成功**\nNative · agy local service\n\n**5 小时**　■■■■■■■■□□ 剩余 77%\n距离重置　4小时 55分\n重置时间　2026-08-30 18:56:55 CST\n\n**周额度**　■■■■■■■■■□ 剩余 86%\n距离重置　4天 06小时 41分\n重置时间　2026-09-03 16:41:58 CST'
[[ "$a_task" == "$a_expected" ]]

# -------------------------------------------------------------
# Case 3: /usage Order (No status line)
# -------------------------------------------------------------
u_task="$(format_provider_card_section "**GPT-5.6 Luna**" "" $'来源：Native · codex app-server\n5 小时：■■■■■■■■■■ 剩余 100%\n距离重置：4小时 58分\n重置时间：2026-08-30 17:32:52 CST\n\n周额度：■■■■■■■■□□ 剩余 84%\n距离重置：6天 21小时 30分\n重置时间：2026-09-06 07:32:52 CST')"
u_expected=$'**GPT-5.6 Luna**\nNative · codex app-server\n\n**5 小时**　■■■■■■■■■■ 剩余 100%\n距离重置　4小时 58分\n重置时间　2026-08-30 17:32:52 CST\n\n**周额度**　■■■■■■■■□□ 剩余 84%\n距离重置　6天 21小时 30分\n重置时间　2026-09-06 07:32:52 CST'
[[ "$u_task" == "$u_expected" ]]
[[ "$u_task" != *"成功"* ]]
[[ "$u_task" != *"失败"* ]]

# -------------------------------------------------------------
# Case 4: Cached Warning Preserved
# -------------------------------------------------------------
cached_sec="$(format_provider_card_section "**Gemini 3.7 Flash · Low**" "" $'来源：CodexBar · cached（可能不是最新）\n5 小时：■■■■■■■■□□ 剩余 77%\n距离重置：4小时 55分\n重置时间：2026-08-30 18:56:55 CST\n\n周额度：■■■■■■■■■□ 剩余 86%\n距离重置：4天 06小时 41分\n重置时间：2026-09-03 16:41:58 CST')"
[[ "$cached_sec" == *'CodexBar · cached'* ]]
[[ "$cached_sec" == *'⚠️ 可能不是最新'* ]]
[[ "$cached_sec" != *'来源'* ]]

# -------------------------------------------------------------
# Case 5: Pi Snapshot Warning Preserved
# -------------------------------------------------------------
pi_sec="$(format_provider_card_section "**GPT-5.6 Luna**" "发送成功" $'来源：Pi 快照（可能不是最新）\n5 小时：■■■■■■■■■■ 剩余 100%\n距离重置：4小时 58分\n重置时间：2026-08-30 17:32:52 CST\n\n周额度：■■■■■■■■□□ 剩余 84%\n距离重置：6天 21小时 30分\n重置时间：2026-09-06 07:32:52 CST')"
[[ "$pi_sec" == *'Pi 快照'* ]]
[[ "$pi_sec" == *'⚠️ 可能不是最新'* ]]
[[ "$pi_sec" != *'来源'* ]]

# -------------------------------------------------------------
# Case 6: Absolute absence of old bracketed format & arrows
# -------------------------------------------------------------
[[ "$c_task" != *'（剩余'* ]]
[[ "$a_task" != *'（剩余'* ]]
[[ "$u_task" != *'（剩余'* ]]
[[ "$c_task" != *'↳'* ]]
[[ "$a_task" != *'↳'* ]]
[[ "$u_task" != *'↳'* ]]

# -------------------------------------------------------------
# Case 7: Card JSON 2.0 & Linear Progress Chart Builders
# -------------------------------------------------------------
chart_5h="$(build_linear_progress_chart 77 "#57D0FB")"
print -r -- "$chart_5h" | "$JQ_BIN" -e '
  .tag == "chart" and
  .height == "24px" and
  .chart_spec.type == "linearProgress" and
  .chart_spec.color == ["#57D0FB"] and
  .chart_spec.cornerRadius == 5 and
  (.chart_spec.roundCap == null) and
  (.chart_spec.track == null) and
  (.chart_spec.progress.style.cornerRadius == null) and
  .chart_spec.data.values[0].value == 0.77
' >/dev/null

chart_w="$(build_linear_progress_chart 86 "#54A6FD")"
print -r -- "$chart_w" | "$JQ_BIN" -e '
  .chart_spec.type == "linearProgress" and
  .chart_spec.color == ["#54A6FD"] and
  .chart_spec.cornerRadius == 5 and
  .chart_spec.data.values[0].value == 0.86
' >/dev/null

chart_clamp="$(build_linear_progress_chart 120 "#57D0FB")"
print -r -- "$chart_clamp" | "$JQ_BIN" -e '.chart_spec.data.values[0].value == 1' >/dev/null

preview_both="$(card_preview both)"
print -r -- "$preview_both" | "$JQ_BIN" -e '
  .msg_type == "interactive" and
  ((.content | fromjson) as $card |
    $card.schema == "2.0" and
    $card.config.width_mode == "default" and
    $card.body.elements[0].tag == "column_set" and
    $card.body.elements[0].flex_mode == "stretch")
' >/dev/null

preview_single="$(card_preview single)"
print -r -- "$preview_single" | "$JQ_BIN" -e '
  .msg_type == "interactive" and
  ((.content | fromjson) as $card |
    $card.schema == "2.0" and
    $card.config.width_mode == "default" and
    $card.body.elements[0].tag == "column_set")
' >/dev/null

preview_usage="$(card_preview usage)"
print -r -- "$preview_usage" | "$JQ_BIN" -e '
  .msg_type == "interactive" and
  ((.content | fromjson) as $card |
    $card.schema == "2.0" and
    $card.config.width_mode == "default" and
    ($card.body.elements[0].content | contains("即时配额查询")))
' >/dev/null

[[ "$(feishu_lookup_payload "user@example.com")" == '{"emails":["user@example.com"]}' ]]
[[ "$(feishu_lookup_payload "13011111111")" == '{"mobiles":["13011111111"]}' ]]
[[ "$(feishu_lookup_payload "+85212345678")" == '{"mobiles":["+85212345678"]}' ]]
[[ "$(feishu_lookup_payload "+8613011111111")" == '{"mobiles":["13011111111"]}' ]]

if feishu_lookup_payload "not-an-identifier"; then
  print -u2 -r -- "feishu regression: invalid identifier was accepted"
  exit 1
fi

(
  export FEISHU_APP_ID="app-id" FEISHU_APP_SECRET="app-secret" FEISHU_USER_ID="test-user"
  feishu_ready
)

print -r -- "feishu regression: ok"
