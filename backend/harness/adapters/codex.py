"""
OpenAI Codex CLI Hook Protocol Handler (Pure Function).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from backend.db import HoronDB
from backend.harness.core import HARNESS_PREFIX, drain, end_turn, guard, sense, sync_session, wrap_harness_message

logger = logging.getLogger("horon.harness.adapters.codex")

# 空工具结果本身也需要能被正则传感器观察到，例如命令被 timeout 杀掉、
# 搜索零命中或接口返回空集。不同 IDE 保持各自的 payload 解析，但这里沿用
# Horon 对“空结果可感知”的语义。
EMPTY_TOOL_RESULT = "<HORON_EMPTY_TOOL_RESULT>"

_TOOL_RESULT_KEYS = ("tool_response", "tool_result", "result", "output")
_ASSISTANT_MSG_KEYS = ("last_assistant_message", "assistant_message", "message", "response")


def _first_str(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    """按候选键顺序取得第一个非空字符串。"""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _stringify_tool_result(payload: dict[str, Any]) -> str:
    """从 Codex PostToolUse payload 提取适合传感器匹配的正文。"""
    raw: Any = None
    for key in _TOOL_RESULT_KEYS:
        if payload.get(key) is not None:
            raw = payload[key]
            break

    if isinstance(raw, dict) and ("stdout" in raw or "stderr" in raw):
        parts = [str(raw.get(key) or "") for key in ("stdout", "stderr")]
        body = "\n".join(part for part in parts if part)
    elif isinstance(raw, dict) and isinstance(raw.get("text"), str):
        body = raw["text"]
    elif isinstance(raw, (dict, list)):
        body = json.dumps(raw, ensure_ascii=False) if raw else ""
    elif raw is not None:
        body = str(raw)
    else:
        body = ""

    error = payload.get("error")
    if error:
        return f"{error}\n{body}" if body else str(error)
    return body


def handle_codex(
    event_name: str,
    payload: dict[str, Any],
    db: HoronDB,
) -> tuple[int, dict[str, Any] | None, str | None]:
    """处理 OpenAI Codex 的 Hook 事件。

    Returns:
        tuple[exit_code, stdout_dict, stderr_content]
    """
    session_id = payload.get("session_id") or payload.get("turn_id") or payload.get("conversationId") or "default"
    clean_id = session_id.strip() if isinstance(session_id, str) else "default"

    if event_name == "SessionStart":
        init_msg = sync_session(db, clean_id)
        if init_msg:
            return 0, {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": wrap_harness_message([init_msg]),
                }
            }, None
        return 0, {}, None

    elif event_name == "UserPromptSubmit":
        notifications: list[str] = []
        if init_msg := sync_session(db, clean_id):
            notifications.append(init_msg)

        user_text = _first_str(payload, ("prompt", "message", "user_prompt", "user_input"))
        if user_text.startswith(HARNESS_PREFIX) or user_text.startswith("【Horon Harness"):
            user_text = ""

        if user_text:
            fired = sense(db, event_type="user_message", text=user_text)
            notifications.extend(fired)

        banner = drain(db, session_id=clean_id, extra_messages=notifications)
        if banner:
            return 0, {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": banner,
                }
            }, None
        return 0, {}, None

    elif event_name == "PreToolUse":
        sync_session(db, clean_id)
        tool_name = payload.get("tool_name") or payload.get("tool", "")
        tool_input = payload.get("tool_input")
        if tool_input is None:
            tool_input = payload.get("args") or payload.get("parameters") or {}

        # Codex can carry hook context directly into the next model request.
        # Do not defer these notifications to Stop: unlike Antigravity, Codex
        # has no PreInvocation hook that drains the queue before every model
        # continuation.
        allowed, deny_reason, fired = guard(db, tool_name, tool_input, session_id="")
        if not allowed:
            reason = deny_reason or "Blocked by Horon guard policy."
            deny_items = [*fired, reason] if fired else [reason]
            return 0, {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": wrap_harness_message(deny_items),
                }
            }, None

        if fired:
            return 0, {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": wrap_harness_message(fired),
                }
            }, None

        # Codex treats permissionDecision="allow" without updatedInput as an
        # invalid PreToolUse response.  A successful no-op is the allow path.
        return 0, {}, None

    elif event_name == "PostToolUse":
        sync_session(db, clean_id)
        tool_name = payload.get("tool_name") or payload.get("tool", "")
        combined = _stringify_tool_result(payload) or EMPTY_TOOL_RESULT

        fired = sense(db, event_type="tool_result", text=combined, tool_name=tool_name, session_id="")
        if fired:
            return 0, {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": wrap_harness_message(fired),
                }
            }, None
        return 0, {}, None

    elif event_name == "Stop":
        sync_session(db, clean_id)
        last_msg = _first_str(payload, _ASSISTANT_MSG_KEYS)
        fired_msgs: list[str] = []
        if last_msg.strip():
            fired_msgs = sense(db, event_type="model_message", text=last_msg)

        pending = db.pop_pending_notifications(clean_id)
        all_warnings = [*fired_msgs, *pending]
        # Codex 在 Stop 续写产生的下一次 Stop 中会设置 stop_hook_active。
        # 若不判这个字段，同一 model_message 传感器可能反复点火造成无限续写。
        already_continued = bool(payload.get("stop_hook_active"))

        if all_warnings and not already_continued:
            return 0, {
                "decision": "block",
                "reason": wrap_harness_message(all_warnings),
            }, None

        if all_warnings:
            # 本轮已经续写过，不再 block；通知放回队列，由下一次用户输入排空，
            # 避免防循环的同时把仍有价值的广播吞掉。
            db.push_pending_notifications(clean_id, all_warnings)

        end_turn(db)
        return 0, {}, None

    elif event_name == "Interrupt":
        # Interrupt is distinct from Stop in Codex.  Without this reset,
        # lifespan='turn' sensors from a cancelled turn leak into the next
        # prompt in the same session.
        sync_session(db, clean_id)
        end_turn(db)
        return 0, {}, None

    return 0, {}, None
