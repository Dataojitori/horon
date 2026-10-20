"""
opencode Hook Protocol Handler (Pure Function).

opencode 没有 hooks.json 式的命令钩子，只有 JS 插件。插件
（正本 backend/harness/opencode-plugin.js，全局装法见该文件开头）
把下面几个事件写成 JSON 从 stdin 交给 `run_gateway.py <事件> --adapter opencode`，
再按这里返回的 JSON 动手。payload 和返回值的形状是我们自己定的，不是 opencode 的格式：

  SessionStart     {session_id, source?, reset?}          → {context?}   新会话或上下文压缩（source="compact" / reset=true）时重置状态并返回提示
  UserPromptSubmit {session_id, prompt, reset?}           → {context?}   插件把 context 接在这条用户消息最后一个文本段的末尾
  PreToolUse       {session_id, tool_name, tool_input, mcp_servers}
                                                         → {decision:"deny", reason} 或 {}
                   工具名和参数先经 normalize_tool 翻成 Claude Code 的叫法再匹配。
                   放行时的通知不能当场送达（tool.execute.before 没有可写的上下文），先入队，
                   由紧接着的 PostToolUse 排空。
  PostToolUse      {session_id, tool_name, tool_output, mcp_servers} → {context?}   插件把 context 追加到工具输出末尾
  Stop             {session_id, last_assistant_message, stop_hook_active}
                                                         → {decision:"block", reason} 或 {}
                   block 时插件把 reason 当作一条新消息发回同一会话，让模型接着写；
                   接着写完的那次 Stop 带 stop_hook_active=true，不再 block，防止无限续写。
  Interrupt        {session_id}                          → {}           用户打断（MessageAbortedError）时清 turn 传感器
  Requeue          {session_id, context}                 → {}           PostToolUse 的通知没能贴进工具结果时送回队列
  tool_output：内置工具是 output 字符串；MCP 工具是插件从 content 里取出的 text 与 resource.text。
"""

from __future__ import annotations

import json
from typing import Any

from backend.db import HoronDB
from backend.harness.core import HARNESS_PREFIX, drain, end_turn, guard, sense, sync_session, wrap_harness_message

ADAPTER = "opencode"

# 与 codex / claude-code 一致：空工具结果也要能被正则传感器看到。
EMPTY_TOOL_RESULT = "<HORON_EMPTY_TOOL_RESULT>"


# 库里的传感器和守卫是照 Claude Code 的工具名和参数名写的（--tool Bash、"file_path": ...），
# 不少正则还依赖参数的先后顺序。opencode 的内置工具叫 bash/write/edit、参数叫 filePath/oldString，
# MCP 工具叫 <服务器>_<工具>。这里把它们翻成 Claude Code 的样子，现有规则就能原样在 opencode 里生效。
# 值：(Claude Code 工具名, [(opencode 参数名, Claude Code 参数名), ...] 按 Claude Code 的参数顺序)
_BUILTIN_TOOLS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "bash": ("Bash", [("command", "command"), ("description", "description"), ("timeout", "timeout"), ("workdir", "workdir")]),
    "write": ("Write", [("filePath", "file_path"), ("content", "content")]),
    "edit": ("Edit", [("filePath", "file_path"), ("oldString", "old_string"), ("newString", "new_string"), ("replaceAll", "replace_all")]),
    "read": ("Read", [("filePath", "file_path"), ("offset", "offset"), ("limit", "limit")]),
    "glob": ("Glob", [("pattern", "pattern"), ("path", "path")]),
    "grep": ("Grep", [("pattern", "pattern"), ("path", "path"), ("include", "glob")]),
    "webfetch": ("WebFetch", [("url", "url"), ("format", "format"), ("timeout", "timeout")]),
    "websearch": ("WebSearch", [("query", "query")]),
    "task": ("Agent", [("description", "description"), ("prompt", "prompt"), ("subagent_type", "subagent_type")]),
    "skill": ("Skill", [("name", "skill")]),
    "todowrite": ("TodoWrite", [("todos", "todos")]),
}


def _sanitize(name: str) -> str:
    """opencode 拼 MCP 工具名时对服务器名的处理：非 [A-Za-z0-9_-] 的字符换成下划线。"""
    return "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in name)


def normalize_tool(tool: str, args: Any, mcp_servers: list[str] | None = None) -> tuple[str, Any]:
    """把 opencode 的工具名和参数翻成 Claude Code 的叫法。

    输入：opencode 工具名；参数（dict 或其他）；opencode 配置里的 MCP 服务器名列表。
    输出：(工具名, 参数)。内置工具改名并按 Claude Code 的顺序重排参数，没见过的参数原样接在后面；
      <服务器>_<工具> 改成 mcp__<服务器>__<工具>，参数不动；其余原样返回。
    """
    if tool in _BUILTIN_TOOLS:
        cc_name, fields = _BUILTIN_TOOLS[tool]
        if not isinstance(args, dict):
            return cc_name, args
        out: dict[str, Any] = {}
        used = set()
        for oc_key, cc_key in fields:
            if oc_key in args:
                out[cc_key] = args[oc_key]
                used.add(oc_key)
        for key, value in args.items():
            if key not in used:
                out[key] = value
        return cc_name, out
    # 服务器名长的先试，免得 a_b 服务器的工具被 a 服务器抢走。
    for server in sorted(mcp_servers or [], key=len, reverse=True):
        prefix = _sanitize(server) + "_"
        if tool.startswith(prefix) and len(tool) > len(prefix):
            return f"mcp__{server}__{tool[len(prefix):]}", args
    return tool, args


def _text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def handle_opencode(
    event_name: str,
    payload: dict[str, Any],
    db: HoronDB,
) -> tuple[int, dict[str, Any] | None, str | None]:
    """处理 opencode 插件转来的事件。返回 (exit_code, stdout_dict, stderr)。"""
    reset = payload.get("source") in ("compact", "clear") or bool(payload.get("reset"))

    if event_name == "SessionStart":
        init_msg = sync_session(db, ADAPTER, reset=reset)
        if init_msg:
            return 0, {"context": wrap_harness_message([init_msg])}, None
        return 0, {}, None

    if event_name == "UserPromptSubmit":
        notifications: list[str] = []
        if init_msg := sync_session(db, ADAPTER, reset=reset):
            notifications.append(init_msg)
        end_turn(db)

        user_text = _text(payload, "prompt")
        if user_text.startswith((HARNESS_PREFIX, "【Horon Harness")):
            user_text = ""
        if user_text:
            notifications.extend(sense(db, event_type="user_message", text=user_text))

        banner = drain(db, extra_messages=notifications)
        return 0, ({"context": banner} if banner else {}), None

    if event_name == "PreToolUse":
        init_msg = sync_session(db, ADAPTER, reset=reset)
        tool_name, tool_input = normalize_tool(
            _text(payload, "tool_name"), payload.get("tool_input") or {}, payload.get("mcp_servers"))
        allowed, deny_reason, fired = guard(db, tool_name, tool_input)
        if init_msg:
            fired = [init_msg, *fired]
        if not allowed:
            reason = deny_reason or "Blocked by Horon guard policy."
            return 0, {"decision": "deny", "reason": wrap_harness_message([*fired, reason])}, None
        db.push_pending_notifications(fired)
        return 0, {}, None

    if event_name == "PostToolUse":
        sync_session(db, ADAPTER)
        tool_name, _ = normalize_tool(_text(payload, "tool_name"), None, payload.get("mcp_servers"))
        combined = _text(payload, "tool_output") or EMPTY_TOOL_RESULT
        fired = sense(db, event_type="tool_result", text=combined, tool_name=tool_name)
        banner = drain(db, extra_messages=fired)
        return 0, ({"context": banner} if banner else {}), None

    if event_name == "Stop":
        sync_session(db, ADAPTER)
        last_msg = _text(payload, "last_assistant_message")
        fired_msgs = sense(db, event_type="model_message", text=last_msg) if last_msg.strip() else []
        all_warnings = [*fired_msgs, *db.pop_pending_notifications()]

        if all_warnings and not payload.get("stop_hook_active"):
            return 0, {"decision": "block", "reason": wrap_harness_message(all_warnings)}, None
        if all_warnings:
            # 已经续写过一次：不再 block，留到下一条用户消息排空。
            db.push_pending_notifications(all_warnings)
        end_turn(db)
        return 0, {}, None

    if event_name == "Requeue":
        # 插件没能把 PostToolUse 排出的通知贴进工具结果（结果形状不认识），送回队列等下次排空。
        # context 是 wrap_harness_message 打包过的整块：去掉信头，每条 "- " 开头的行拆回一条。
        sync_session(db, ADAPTER)
        items = [
            line[2:].strip() if line.startswith("- ") else line.strip()
            for line in _text(payload, "context").splitlines()
            if line.strip() and not line.strip().startswith(HARNESS_PREFIX)
        ]
        db.push_pending_notifications(items)
        return 0, {}, None

    if event_name == "Interrupt":
        sync_session(db, ADAPTER)
        end_turn(db)
        return 0, {}, None

    return 0, {}, None
