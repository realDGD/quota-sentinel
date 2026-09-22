"""Feishu card rendering from explicit quota readings and model results.

Rendering is pure: callers own time, credential lookup, locks and delivery.
The envelope's ``content`` is itself a JSON string, as required by Feishu.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from quota_sentinel.notifications.plan import Layout, plan_task


TITLE = "AI 模型运行与配额"
FOOTER = '<font color="grey">Pi 自动任务 · Fresh 重置后 4 分钟 · 无数据时 5 小时 01 分兜底</font>'
V1_FOOTER = "Pi 自动任务 · Fresh 重置后 4 分钟 · 无数据时 5 小时 01 分兜底"
PROVIDER_TITLES = {
    "codex": "GPT-5.6 Luna",
    "antigravity": "Gemini 3.7 Flash · Low",
    "opencode": "DeepSeek V4 Flash · Off",
}
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _envelope(user_id: str, request_uuid: str, content: Mapping[str, Any]) -> dict:
    return {
        "receive_id": user_id,
        "msg_type": "interactive",
        "content": _json(content),
        "uuid": request_uuid,
    }


def _markdown(content: str) -> dict:
    return {"tag": "markdown", "content": content}


def _column(elements: list, *, weight: int = 1, valign: str = "top") -> dict:
    return {
        "tag": "column", "width": "weighted", "weight": weight,
        "vertical_align": valign, "elements": elements,
    }


def _column_set(columns: list, *, flex: str, spacing: Optional[str] = None) -> dict:
    result = {"tag": "column_set", "flex_mode": flex}
    if spacing:
        result["horizontal_spacing"] = spacing
    result["columns"] = columns
    return result


def _document(reading: Any) -> Mapping[str, Any]:
    if reading is None:
        return {}
    document = getattr(reading, "document", None)
    if isinstance(document, Mapping):
        return document
    if hasattr(reading, "as_document"):
        return reading.as_document()
    if isinstance(reading, Mapping):
        return reading
    return {}


def _number(value: Any) -> Optional[int]:
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    return None


def format_reset_time(epoch: int) -> str:
    """The shell's `format_reset_time`: Shanghai wall clock."""
    return datetime.fromtimestamp(epoch, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S %Z")


def _timestamp(epoch: int) -> str:
    return format_reset_time(epoch)


def _duration(seconds: int) -> str:
    if seconds <= 0:
        return "即将重置"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return f"{days}天 {hours}小时 {minutes}分"
    if hours:
        return f"{hours}小时 {minutes}分"
    return f"{minutes}分"


def _reading_parts(reading: Any, now: int) -> tuple:
    doc = _document(reading)
    five = doc.get("fiveHour") if isinstance(doc.get("fiveHour"), Mapping) else {}
    week = doc.get("weekly") if isinstance(doc.get("weekly"), Mapping) else {}
    values = tuple(_number(v) for v in (
        five.get("remainingPercent"), five.get("resetAt"),
        week.get("remainingPercent"), week.get("resetAt"),
    ))
    if any(v is None for v in values):
        return "不可用", 0, "未知", "未知", 0, "未知", "未知"
    f_pct, f_reset, w_pct, w_reset = values
    source = doc.get("source", "未知")
    if not isinstance(source, str):
        source = "未知"
    if "cached" in source:
        source = "CodexBar · cached\n⚠️ 可能不是最新"
    elif "快照" in source:
        source = "Pi 快照\n⚠️ 可能不是最新"
    return (
        source,
        f_pct, _duration(f_reset - now), _timestamp(f_reset),
        w_pct, _duration(w_reset - now), _timestamp(w_reset),
    )


def progress_chart(percent: Any, color: str = "#57D0FB") -> dict:
    """The VChart linearProgress shape verified on the real Feishu client."""
    parsed = None
    if type(percent) is int or (isinstance(percent, str) and re.fullmatch(r"-?[0-9]+", percent)):
        parsed = int(percent)
    value = 1.0 if parsed is None else max(0, min(100, parsed)) / 100.0
    return {
        "tag": "chart", "aspect_ratio": "16:9", "height": "24px", "preview": False,
        "chart_spec": {
            "type": "linearProgress",
            "data": {"values": [{"type": "quota", "value": value}]},
            "direction": "horizontal", "xField": "value", "yField": "type",
            "seriesField": "type", "cornerRadius": 5, "color": [color],
            "progress": {"style": {"fill": color}}, "bandWidth": 10,
            "axes": [{"orient": "left", "visible": False}, {"orient": "bottom", "visible": False}],
            "legends": {"visible": False}, "tooltip": {"visible": False},
            "padding": {"top": 0, "bottom": 0, "left": 0, "right": 0},
        },
    }


def _result_or_success(result: Any) -> str:
    """The shell's `${VAR:-发送成功}`: absent AND empty both mean success."""
    return result or "发送成功"


def _provider_elements(provider: str, result: str, reading: Any, mode: str, now: int) -> list:
    if provider not in PROVIDER_TITLES:
        raise ValueError(f"unknown provider: {provider}")
    source, f_pct, f_dur, f_time, w_pct, w_dur, w_time = _reading_parts(reading, now)
    title = f"**{PROVIDER_TITLES[provider]}**"
    if result:
        status = "🟢 **成功**" if result == "发送成功" else "🔴 **失败**"
        heading = _column_set([
            _column([_markdown(title)], weight=3, valign="center"),
            _column([_markdown(status)], weight=1, valign="center"),
        ], flex="none")
    else:
        heading = _markdown(title)
    elements = [heading, _markdown(source)]
    five = [
        _markdown(f"**5 小时**　剩余 {f_pct}%"),
        progress_chart(f_pct, "#57D0FB"),
        _markdown(f"距离重置　{f_dur}\n重置时间　{f_time}"),
        {"tag": "hr"},
    ]
    weekly = [
        _markdown(f"**周额度**　剩余 {w_pct}%"),
        progress_chart(w_pct, "#54A6FD"),
        _markdown(f"距离重置　{w_dur}\n重置时间　{w_time}"),
        {"tag": "hr"},
    ]
    if mode == "single":
        elements.append(_column_set([_column(five), _column(weekly)], flex="stretch", spacing="medium"))
    else:
        elements.extend(five + weekly)
    if provider == "opencode":
        monthly = _document(reading).get("monthly")
        if isinstance(monthly, Mapping):
            pct = _number(monthly.get("remainingPercent"))
            reset = _number(monthly.get("resetAt"))
            if pct is not None and reset is not None:
                line = _markdown(f'<font color="grey">本月度　剩余 {pct}%　重置 {_timestamp(reset)}</font>')
                if elements[-1].get("tag") == "hr":
                    elements.insert(-1, line)
                else:
                    elements.append(line)
    return elements


def _v2_card(user_id: str, request_uuid: str, elements: list, red: bool = False) -> dict:
    return _envelope(user_id, request_uuid, {
        "schema": "2.0", "config": {"width_mode": "default"},
        "header": {"template": "red" if red else "green", "title": {"tag": "plain_text", "content": TITLE}},
        "body": {"direction": "vertical", "elements": elements},
    })


def _v1_card(user_id: str, request_uuid: str, message: str) -> dict:
    return _envelope(user_id, request_uuid, {
        "config": {"wide_screen_mode": True},
        "header": {"template": "red" if "发送失败" in message else "green", "title": {"tag": "plain_text", "content": TITLE}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": message}},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": V1_FOOTER}]},
        ],
    })


def render_task_card(providers: Sequence[str], results: Mapping[str, str], readings: Mapping[str, Any], user_id: str, request_uuid: str, *, now: Optional[int] = None) -> dict:
    """Render one scheduled run or recovery card for exactly its providers."""
    members = tuple(providers)
    if not members:
        raise ValueError("a task card needs at least one provider")
    current = int(time.time() if now is None else now)
    plan = plan_task(members)
    red = any(_result_or_success(results.get(p)) != "发送成功" for p in members)
    # The legacy two-column builder only handles the original pair. A lone
    # OpenCode provider must use the stacked renderer used by its preview.
    if plan.layout is Layout.TWO_COLUMN and set(members) == {"codex", "antigravity"}:
        elements = [_column_set([
            _column(_provider_elements("codex", _result_or_success(results.get("codex")), readings.get("codex"), "dual", current)),
            _column(_provider_elements("antigravity", _result_or_success(results.get("antigravity")), readings.get("antigravity"), "dual", current)),
        ], flex="stretch", spacing="medium"), _markdown(FOOTER)]
    else:
        elements = []
        for provider in members:
            elements.extend(_provider_elements(provider, _result_or_success(results.get(provider)), readings.get(provider), "single", current))
        elements.append(_markdown(FOOTER))
    return _v2_card(user_id, request_uuid, elements, red=red)


def render_usage_card(roster: Sequence[str], readings: Mapping[str, Any], user_id: str, request_uuid: str, *, now: Optional[int] = None) -> dict:
    """Render a full-roster /usage card with no model-run status markers."""
    current = int(time.time() if now is None else now)
    elements = [_markdown("**即时配额查询**　未执行模型任务")]
    for provider in roster:
        elements.extend(_provider_elements(provider, "", readings.get(provider), "single", current))
    elements.append(_markdown(FOOTER))
    return _v2_card(user_id, request_uuid, elements)


def render_busy_card(user_id: str, request_uuid: str) -> dict:
    return _v1_card(user_id, request_uuid, "⏳ 配额正在刷新，请稍后再试")


def render_progress_test_card(user_id: str, request_uuid: str) -> dict:
    elements = [
        _markdown("每列从上到下依次为 0% / 1% / 50% / 77% / 99% / 100%。"),
        _markdown("验收目标：所有非 0% 进度条两端圆角一致，track 同样两端圆角。"),
    ]
    for percent in (0, 1, 50, 77, 99, 100):
        elements.extend((_markdown(f"{percent}%"), progress_chart(percent)))
    payload = _v2_card(user_id, request_uuid, elements)
    body = json.loads(payload["content"])
    body["header"] = {"template": "blue", "title": {"tag": "plain_text", "content": "Linear Progress 圆角验收"}}
    payload["content"] = _json(body)
    return payload


def _quota_bar(percent: int) -> str:
    filled = max(0, min(10, (percent + 5) // 10))
    return "■" * filled + "□" * (10 - filled)


def _text_section(provider: str, result: str, reading: Any, now: int) -> str:
    source, f_pct, f_dur, f_time, w_pct, w_dur, w_time = _reading_parts(reading, now)
    status = ""
    if result:
        status = "    " + ("🟢 **成功**" if result == "发送成功" else "🔴 **失败**")
    if source == "不可用":
        five = "**5 小时**　□□□□□□□□□□ 获取失败\n距离重置　未知\n重置时间　未知"
        week = "**周额度**　□□□□□□□□□□ 获取失败\n距离重置　未知\n重置时间　未知"
    else:
        five = f"**5 小时**　{_quota_bar(f_pct)} 剩余 {f_pct}%\n距离重置　{f_dur}\n重置时间　{f_time}"
        week = f"**周额度**　{_quota_bar(w_pct)} 剩余 {w_pct}%\n距离重置　{w_dur}\n重置时间　{w_time}"
    return f"**{PROVIDER_TITLES[provider]}**{status}\n{source}\n\n{five}\n\n{week}"


def render_task_text_card(providers: Sequence[str], results: Mapping[str, str], readings: Mapping[str, Any], user_id: str, request_uuid: str, *, now: Optional[int] = None) -> dict:
    current = int(time.time() if now is None else now)
    sections = [_text_section(p, _result_or_success(results.get(p)), readings.get(p), current) for p in providers]
    message = "\n\n────────────\n\n".join(sections)
    message += f"\n\n**图例**　■ 剩余　□ 已用\n🕒 {_timestamp(current)}"
    return _v1_card(user_id, request_uuid, message)


def render_usage_text_card(roster: Sequence[str], readings: Mapping[str, Any], user_id: str, request_uuid: str, *, now: Optional[int] = None) -> dict:
    current = int(time.time() if now is None else now)
    sections = [_text_section(p, "", readings.get(p), current) for p in roster]
    message = "**即时配额查询**　未执行模型任务\n\n" + "\n\n────────────\n\n".join(sections)
    message += f"\n\n**图例**　■ 剩余　□ 已用\n🕒 {_timestamp(current)}"
    return _v1_card(user_id, request_uuid, message)


__all__ = [
    "format_reset_time", "progress_chart", "render_task_card", "render_usage_card", "render_busy_card",
    "render_progress_test_card", "render_task_text_card", "render_usage_text_card",
]
