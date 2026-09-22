"""
Anthropic Claude Code CLI Hook Protocol Handler (Pure Function).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from backend.db import HoronDB
from backend.harness.core import HARNESS_PREFIX, drain, end_turn, guard, sense, sync_session, wrap_harness_message

logger = logging.getLogger("horon.harness.adapters.claude_code")

# 字段名以探针实测为准，不以文档为准：同一页官方文档两次抓取，对
# UserPromptSubmit 分别说 user_input 和 user_prompt，而 2026-09-23 实测是
# `prompt` —— 两个都错。保留多键兼容而不收窄成实测的单键，是因为按单一字段名
# 读的代价是静默失效：取不到文本时 sense() 返回空列表，不报错、不警告，看起来
# 一切正常，只是传感器永远不点火。实测清单见 Horon #1380。
# 实测命中项排在最前。
# 工具跑完却什么都没输出，本身就是一条需要被感知的信号（命令被 timeout 杀掉、
# 搜索 0 命中、接口返回空集，长得都一样）。但传感器是正则匹配文本的，空串匹配
# 不了任何 pattern —— 不给它一个可见的替身，"什么都没返回"就是全图唯一感知不到
# 的事件，而这恰好是我把"没查到"讲成"不存在"的起点。
EMPTY_TOOL_RESULT = "<HORON_EMPTY_TOOL_RESULT>"

_PROMPT_KEYS = ("prompt", "user_prompt", "user_input", "user_message")
_TOOL_RESULT_KEYS = ("tool_response", "tool_result", "result", "output")
_ASSISTANT_MSG_KEYS = ("last_assistant_message", "assistant_message", "message")


def _first_str(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    """按候选键顺序取第一个非空字符串值。

    输入：payload = Hook 送来的 JSON；keys = 候选字段名，按可能性排序。
    行为：只认非空字符串；遇到 dict/list 等结构化值直接跳过交给别的函数处理。
    输出：命中返回该字符串；全部落空返回空字符串。
    """
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _text_blocks(raw: Any) -> str | None:
    """MCP content 数组 `[{"type":"text","text":...}, ...]` → 拼接各块 text。

    只有**每一块**都是这个形状才认；混了别的东西就返回 None，不挑着抽。
    """
    if not isinstance(raw, list) or not raw:
        return None
    if all(isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str) for b in raw):
        return "\n".join(b["text"] for b in raw)
    return None


def _unwrap_mcp_string(raw: str, depth: int = 0) -> str:
    """剥开 MCP 裸字符串外面的信封，剥不动就原样返回。

    实测（2026-09-23，bluesky）：tool_response 是字符串，内容是
    `{"result":"<真正的正文>"}` —— 正文又是一段 JSON 文本。只剥两种信封：
    键集合恰好是 {"result"} 的对象、以及 content 块数组。剥开后如果里面
    是业务 JSON（帖子、资料之类），**原样保留那段字符串**，不去挑 text/content
    字段 —— uri、did、handle 都可能是传感器要匹配的东西。
    这里从不判空：MCP 返回 "[]" 还是 "{}" 由传感器自己看，不替它下结论。
    """
    stripped = raw.strip()
    if depth > 3 or stripped[:1] not in ("{", "["):
        return raw
    try:
        parsed = json.loads(stripped, strict=False)
    except ValueError:
        return raw
    if isinstance(parsed, dict) and set(parsed) == {"result"} and isinstance(parsed["result"], str):
        return _unwrap_mcp_string(parsed["result"], depth + 1)
    if (joined := _text_blocks(parsed)) is not None:
        return joined
    return raw


def _extract_body(raw: Any) -> str | None:
    """从一个认得出的工具结果形状里抽出正文（未转义的原文）。

    输入：raw = tool_response 的值。
    行为：只认完整形状，不按单个字段名往下挖（上一版那样挖，Write 结果、
          MCP 业务 JSON 会被截成单个字段或误判成空）：
          - 字符串（MCP，实测）→ _unwrap_mcp_string
          - Bash（实测）：有 stdout/stderr → stdout + stderr
          - Read（实测）：`{"type":"text","file":{"content": str}}` → file.content
          - Grep/Glob（未实测，Glob/Grep 在她的设定里是 deny 的，平时不会出现）：
            有 `numFiles` 或 `mode` 且带 `filenames` 列表 → content 非空优先
            （Grep content 模式下 filenames 是 [] 而正文在 content），否则
            filenames 逐行。两者都空 = 真的零命中。
          - MCP content 块数组 → 拼接 text
          - `{"type":"text","text": str}`（文档形状，键不多于这两个）→ text
    输出：认得出返回正文（空串 = 真的什么都没有）；认不出返回 None，
          调用方退回 json.dumps，至少不丢字段。
    """
    if isinstance(raw, str):
        return _unwrap_mcp_string(raw)
    if (joined := _text_blocks(raw)) is not None:
        return joined
    if not isinstance(raw, dict):
        return None
    if "stdout" in raw or "stderr" in raw:
        parts = [str(raw.get(k) or "") for k in ("stdout", "stderr")]
        return "\n".join(p for p in parts if p)
    file_obj = raw.get("file")
    if raw.get("type") == "text" and isinstance(file_obj, dict) and isinstance(file_obj.get("content"), str):
        return file_obj["content"]
    if ("numFiles" in raw or "mode" in raw) and isinstance(raw.get("filenames"), list):
        content = raw.get("content")
        if isinstance(content, str) and content:
            return content
        return "\n".join(str(f) for f in raw["filenames"])
    if set(raw) <= {"type", "text"} and isinstance(raw.get("text"), str):
        return raw["text"]
    return None


def _stringify_tool_result(payload: dict[str, Any]) -> str:
    """把工具结果压成一段可供正则匹配的文本。

    输入：payload = PostToolUse / PostToolUseFailure 的 JSON。
    行为：整个 json.dumps 的话正文会被转义成 `\\r\\n` 和 `\\"`，传感器正则
          要匹配输出内容就得穿过这层转义，很容易静默失手 —— 所以先交给
          _extract_body 按实测形状抽正文，认不出才退回 dumps。
          认得出但正文为空（Grep 0 命中、空文件、命令无输出）时返回空串，
          调用方会换成 EMPTY_TOOL_RESULT。
          error（失败事件才有）拼在最前面，让针对报错的传感器也能命中。
    输出：合并后的文本；什么都没有时返回空串。
    """
    raw: Any = None
    for key in _TOOL_RESULT_KEYS:
        if payload.get(key) is not None:
            raw = payload[key]
            break

    body = _extract_body(raw) if raw is not None else ""
    if body is None:
        body = json.dumps(raw, ensure_ascii=False) if isinstance(raw, (dict, list)) else str(raw)

    error = payload.get("error")
    if error:
        return f"{error}\n{body}" if body else str(error)
    return body


def handle_claude_code(
    event_name: str,
    payload: dict[str, Any],
    db: HoronDB,
) -> tuple[int, str | dict[str, Any] | None, str | None]:
    """处理 Claude Code 的 Hook 事件。

    Returns:
        tuple[exit_code, stdout_content, stderr_content]
    """
    session_id = payload.get("session_id") or payload.get("conversationId") or "default"
    clean_id = session_id.strip() if isinstance(session_id, str) else "default"

    if event_name == "SessionStart":
        init_msg = sync_session(db, clean_id)
        if init_msg:
            return 0, wrap_harness_message([init_msg]), None
        return 0, None, None

    elif event_name == "UserPromptSubmit":
        notifications: list[str] = []
        if init_msg := sync_session(db, clean_id):
            notifications.append(init_msg)

        # 新的用户输入 = 新回合，不管上一回合有没有正常收尾。Esc 打断、API 报错时
        # Stop 不会触发，turn 传感器会亮着拖进这一轮，和新点亮的凑出假 AND。
        # 这里先补一次 end_turn（幂等：只是把 turn 传感器置 0 再求值）。
        end_turn(db)

        user_text = _first_str(payload, _PROMPT_KEYS)
        if user_text.startswith(HARNESS_PREFIX) or user_text.startswith("【Horon Harness"):
            user_text = ""

        if user_text:
            fired = sense(db, event_type="user_message", text=user_text)
            notifications.extend(fired)

        banner = drain(db, session_id=clean_id, extra_messages=notifications)
        return 0, banner or None, None

    elif event_name == "PreToolUse":
        sync_session(db, clean_id)
        tool_name = payload.get("tool_name") or payload.get("tool", "")
        tool_input = payload.get("tool_input")
        if tool_input is None:
            tool_input = payload.get("args") or payload.get("parameters") or {}

        allowed, deny_reason, fired = guard(db, tool_name, tool_input, session_id=clean_id)
        if not allowed:
            reason = deny_reason or "Blocked by Horon guard policy."
            deny_items = [*fired, reason] if fired else [reason]
            # 按官方文档，exit 2 时 Claude Code 忽略 stdout 的 JSON，只把 stderr
            # 交给模型 —— 所以**stderr 才是实际载体**，必须带着完整的拦截理由和
            # 同时点火的传感器通知，不能精简。JSON 保留是因为这份文档在字段名上
            # 已经错过两次（见 Horon #1380），万一它其实会读 JSON，reason 也是全的。
            # 两条路径内容一致，哪条生效都不丢信息。（未实测，有 guard 拦截时抓一次。）
            return (
                2,
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": wrap_harness_message(deny_items) or reason,
                    }
                },
                f"{wrap_harness_message(deny_items)}\n",
            )

        # No Horon guard blocked the call.  Stay silent so Claude Code still
        # applies its normal permission flow instead of auto-approving it.
        return 0, None, None

    elif event_name in ("PostToolUse", "PostToolUseFailure"):
        sync_session(db, clean_id)
        tool_name = payload.get("tool_name") or payload.get("tool", "")
        combined = _stringify_tool_result(payload) or EMPTY_TOOL_RESULT

        sense(db, event_type="tool_result", text=combined, tool_name=tool_name, session_id=clean_id)

        # 反重力在每次模型调用前（PreInvocation）排空 pending，所以工具结果点亮的
        # 通知、以及 PreToolUse 放行时暂存的 tool_call 通知，下一步就能看到。
        # Claude Code 没有逐步的 PreInvocation；这里不排空的话，它们要一直憋到
        # Stop 才出来，那时模型已经带着错误前提说完了整段话。
        # PostToolUse / PostToolUseFailure 都接受 hookSpecificOutput.additionalContext，
        # 会以 system reminder 的形式贴在工具结果旁边。
        if banner := drain(db, session_id=clean_id):
            return 0, {
                "hookSpecificOutput": {
                    "hookEventName": event_name,
                    "additionalContext": banner,
                }
            }, None
        return 0, None, None

    elif event_name == "Stop":
        sync_session(db, clean_id)

        last_msg = _first_str(payload, _ASSISTANT_MSG_KEYS)
        fired_msgs: list[str] = []
        if last_msg.strip():
            fired_msgs = sense(db, event_type="model_message", text=last_msg)

        pending = db.pop_pending_notifications(clean_id)
        all_warnings = [*fired_msgs, *pending]

        # 死循环闸门：block 会把话语权交还给模型，它的下一段回复又会经过同一个
        # model_message 传感器。而现有的 pattern 恰好匹配「这就记／马上记」这类
        # 措辞 —— 被拦下后解释自己要去补记，正好再次点火。stop_hook_active 表示
        # 这一轮已经由 Stop hook 续过一次，此时不再拦，也不把警告带进下一轮。
        # （2026-09-23 实测确认 Claude Code 确实送这个字段，文档说没有是错的。）
        # 已知形状，尚未处理：block 时不走 end_turn，所以 turn 传感器会跨着这次
        # 续写继续亮 —— 上一段回复点亮的 model_message 传感器，会和新一段回合里
        # 新亮的传感器凑成一次并不成立的 AND。闸门挡住了它变成死循环，但多报一次
        # 仍会发生。真要修得在 core 侧让 model_message 传感器每次 Stop 重新评估。
        already_continued = bool(payload.get("stop_hook_active"))

        if all_warnings and not already_continued:
            broadcast = wrap_harness_message(all_warnings)
            # 只用顶层 decision/reason。hookSpecificOutput 下的 Stop 只认
            # additionalContext；往里塞 decision/reason 有过不了 schema 校验的风险，
            # 而校验失败按文档是「非阻断错误，动作照常进行」—— 也就是整个 block
            # 静默失效，模型照样停下。
            return 0, {"decision": "block", "reason": broadcast}, None

        # 已续写过一次就放行；这轮的警告不再推迟到下一条用户输入，
        # 否则旧回复的告警会误导下一轮任务。

        end_turn(db)
        return 0, None, None

    return 0, None, None
