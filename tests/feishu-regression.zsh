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
  $'5 小时：■■■■■□□□□□ 剩余 48%\n重置：2026-08-29 22:04:34 CST\n周额度：■■■■■■■□□□ 剩余 65%\n重置：2026-09-04 08:01:57 CST' \
  "发送失败" \
  $'5 小时：□□□□□□□□□□ 获取失败\n重置：未知\n周额度：□□□□□□□□□□ 获取失败\n重置：未知')"
[[ "$card_message" == *'🟢 **发送成功**'* ]]
[[ "$card_message" == *'🔴 **发送失败**'* ]]
[[ "$card_message" == *'**5 小时**'* ]]
[[ "$card_message" == *'**周额度**'* ]]

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
