"""
Antigravity / Cursor IDE Hook Protocol Handler (Pure Function).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from backend.db import HoronDB
from backend.harness.core import HARNESS_PREFIX, drain, end_turn, guard, sense, sync_session, wrap_harness_message

logger = logging.getLogger("horon.harness.adapters.antigravity")


def _extract_text(content: Any) -> str:
    """安全提取步骤内容中的纯文本（兼容纯字符串、字典或结构化 block 列表）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict):
                if "text" in item and isinstance(item["text"], str):
                    chunks.append(item["text"])
                elif "content" in item and isinstance(item["content"], str):
                    chunks.append(item["content"])
        return "\n".join(chunks).strip()
    if isinstance(content, dict):
        if "text" in content and isinstance(content["text"], str):
            return content["text"]
        if "content" in content and isinstance(content["content"], str):
            return content["content"]
    return ""


def get_latest_transcript_entries(transcript_path: str | None, max_entries: int = 10) -> list[dict[str, Any]]:
    """从 transcript.jsonl 日志文件中倒序读取最新的步骤记录。"""
    if not transcript_path:
        return []
    p = Path(transcript_path)
    if not p.exists() or not p.is_file():
        return []

    entries: list[dict[str, Any]] = []
    chunk_size = 64 * 1024

    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            if file_size == 0:
                return []

            cursor = file_size
            buffer = b""

            while cursor > 0 and len(entries) < max_entries:
                read_size = min(chunk_size, cursor)
                cursor -= read_size
                f.seek(cursor)
                chunk = f.read(read_size)
                buffer = chunk + buffer

                lines = buffer.split(b"\n")
                if cursor > 0:
                    buffer = lines[0]
                    complete_lines = lines[1:]
                else:
                    buffer = b""
                    complete_lines = lines

                for raw_line in reversed(complete_lines):
                    line_str = raw_line.decode("utf-8", errors="replace").strip()
                    if not line_str:
                        continue
                    try:
                        data = json.loads(line_str)
                        if isinstance(data, dict):
                            entries.append(data)
                            if len(entries) >= max_entries:
                                break
                    except Exception:
                        continue

            if buffer and len(entries) < max_entries:
                line_str = buffer.decode("utf-8", errors="replace").strip()
                if line_str:
                    try:
                        data = json.loads(line_str)
                        if isinstance(data, dict):
                            entries.append(data)
                    except Exception:
                        pass

        return entries
    except Exception as e:
        logger.debug(f"Failed to read transcript: {e}")
        return []


def handle_antigravity(event_name: str, payload: dict[str, Any], db: HoronDB) -> dict[str, Any]:
    """处理 Antigravity / Cursor 的 Hook 事件并返回响应字典。"""
    conv_id = payload.get("conversationId")

    if event_name == "PreInvocation":
        notifications: list[str] = []
        if init_msg := sync_session(db, conv_id):
            notifications.append(init_msg)

        clean_id = conv_id.strip()

        # 从 transcript 提取用户最新真实输入
        transcript_path = payload.get("transcriptPath")
        entries = get_latest_transcript_entries(transcript_path, max_entries=5)
        user_text = ""
        for entry in entries:
            if entry.get("source") in ("USER_EXPLICIT", "USER") or entry.get("type") == "USER_INPUT":
                content = _extract_text(entry.get("content", ""))
                if content and (content.startswith(HARNESS_PREFIX) or content.startswith("【Horon Harness")):
                    continue
                user_text = content
                break

        if user_text:
            fired = sense(db, event_type="user_message", text=user_text)
            notifications.extend(fired)

        if broadcast := drain(db, session_id=clean_id, extra_messages=notifications):
            return {"injectSteps": [{"userMessage": broadcast}]}
        return {}

    elif event_name == "PreToolUse":
        sync_session(db, conv_id)
        clean_id = conv_id.strip()

        tool_call = payload.get("toolCall", {})
        tool_name = tool_call.get("name", "")
        tool_args = tool_call.get("args", {})

        allowed, deny_reason, fired = guard(db, tool_name, tool_args, session_id=clean_id)
        if not allowed:
            deny_items = [*fired, deny_reason] if deny_reason else fired
            return {
                "decision": "deny",
                "reason": wrap_harness_message(deny_items),
            }
        return {"decision": "allow"}

    elif event_name == "PostToolUse":
        sync_session(db, conv_id)
        clean_id = conv_id.strip()

        tool_call = payload.get("toolCall", {})
        tool_name = tool_call.get("name") or payload.get("toolName") or payload.get("tool", "")
        error = payload.get("error")
        result = payload.get("result") if payload.get("result") is not None else payload.get("output")
        if result is None:
            result = payload.get("response")

        result_str = json.dumps(result, ensure_ascii=False) if isinstance(result, (dict, list)) else (str(result) if result is not None else "")
        combined = f"{error}\n{result_str}" if error else result_str

        sense(db, event_type="tool_result", text=combined, tool_name=tool_name, session_id=clean_id)
        return {}

    elif event_name == "PostInvocation":
        sync_session(db, conv_id)

        transcript_path = payload.get("transcriptPath")
        entries = get_latest_transcript_entries(transcript_path, max_entries=5)
        model_text = ""
        for entry in entries:
            if entry.get("type") == "PLANNER_RESPONSE" and entry.get("source") == "MODEL":
                content = _extract_text(entry.get("content", ""))
                if content.strip():
                    model_text = content
                    break

        fired_msgs: list[str] = []
        if model_text:
            fired_msgs = sense(db, event_type="model_message", text=model_text)

        if fired_msgs and (broadcast := wrap_harness_message(fired_msgs)):
            return {
                "injectSteps": [{"userMessage": broadcast}],
                "terminationBehavior": "force_continue",
            }
        return {}

    elif event_name == "Stop":
        sync_session(db, conv_id)
        clean_id = conv_id.strip()

        pending = db.pop_pending_notifications(clean_id)
        if pending:
            return {
                "decision": "continue",
                "reason": wrap_harness_message(pending),
            }

        end_turn(db)
        return {"decision": "allow"}

    return {}
