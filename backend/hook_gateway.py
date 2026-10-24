"""
Horon Hook Gateway — 多环境生命周期 Hooks 统一网关。

支持的环境适配器 (Adapters):
  - antigravity (默认): Antigravity / Cursor IDE 生命周期 (.agents/hooks.json)
  - claude-code:        Anthropic Claude Code CLI 生命周期 (settings.json)
  - codex:              OpenAI Codex CLI 生命周期 (hooks.json / config.toml)
  - opencode:           opencode 插件转发 (~/.config/opencode/plugin/horon-harness.js)
  - dsh:                DeepSeek Harness 插件转发 (backend/harness/dsh-plugin/)

调用方式 (CLI Entry Point):
  python -m backend.hook_gateway <EventName> [--adapter antigravity|claude-code|codex|opencode|dsh]
  (通过 stdin 接收 Hook JSON Payload，通过 stdout / stderr 和 Exit Code 输出决策)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv

from backend.db import HoronDB, create_database, database_exists
from backend.harness.adapters.antigravity import handle_antigravity
from backend.harness.adapters.claude_code import handle_claude_code
from backend.harness.adapters.codex import handle_codex
from backend.harness.adapters.opencode import handle_opencode
from backend.harness.adapters.dsh import handle_dsh
from backend.harness.core import (
    HARNESS_PREFIX,
    drain,
    end_turn,
    guard,
    sense,
    sync_session,
    wrap_harness_message,
)

logger = logging.getLogger("horon.hook_gateway")


def _read_stdin_payload() -> dict[str, Any]:
    """从标准输入 (stdin) 读取并解析 Hook JSON 数据载荷。"""
    try:
        if not sys.stdin.isatty():
            content = sys.stdin.read().strip()
            if content:
                content = content.lstrip("\ufeff")
                return json.loads(content)
    except Exception as e:
        logger.debug(f"Failed to parse stdin payload: {e}")
    return {}


def detect_adapter_type(payload: dict[str, Any], explicit_adapter: str | None = None) -> str:
    """根据命令行参数或 payload 特征自动检测适配器类型。"""
    if explicit_adapter:
        clean = explicit_adapter.strip().lower()
        if clean in ("claude", "claude-code", "claudecode"):
            return "claude-code"
        if clean in ("codex", "openai-codex"):
            return "codex"
        if clean in ("antigravity", "cursor"):
            return "antigravity"
        if clean == "opencode":
            return "opencode"
        if clean == "dsh":
            return "dsh"

    # 基于 Payload 结构特征启发式自动探测
    if "hook_event_name" in payload or "tool_response" in payload:
        if "turn_id" in payload:
            return "codex"
        return "claude-code"

    if "conversationId" in payload or "transcriptPath" in payload or "toolCall" in payload:
        return "antigravity"

    return "antigravity"


# 各宿主 payload 里放会话 ID 的字段，按优先顺序。
_SESSION_KEYS = {
    "claude-code": ("session_id", "conversation_id", "conversationId"),
    "codex": ("session_id", "conversation_id", "conversationId", "thread_id"),
    "antigravity": ("conversationId", "conversation_id"),
    "opencode": ("session_id",),
    "dsh": ("session_id",),
}


def payload_session_id(payload: dict[str, Any], adapter_type: str) -> str:
    """从钩子 payload 取出会话 ID。

    输入：宿主送来的 payload；宿主名（决定按哪些字段找）。
    输出：去掉首尾空白的会话 ID。
    异常：所有候选字段都不是非空字符串时抛 ValueError。
    """
    keys = _SESSION_KEYS.get(adapter_type, _SESSION_KEYS["antigravity"])
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError(f"{adapter_type} hook payload 缺少有效会话 ID（查找字段：{', '.join(keys)}）。")


def dispatch_hook(
    event_name: str,
    payload: dict[str, Any],
    db: HoronDB,
    adapter_type: str = "antigravity",
) -> tuple[int, Any, str | None]:
    """通用生命周期事件分发入口。

    输入：事件名；payload；db 是已打开的连接，会话须与 payload 一致，由调用方负责关闭。
    Returns:
        tuple[exit_code, stdout_data, stderr_data]
    """
    if adapter_type == "claude-code":
        return handle_claude_code(event_name, payload, db)
    elif adapter_type == "codex":
        return handle_codex(event_name, payload, db)
    elif adapter_type == "opencode":
        return handle_opencode(event_name, payload, db)
    elif adapter_type == "dsh":
        return handle_dsh(event_name, payload, db)
    else:
        res = handle_antigravity(event_name, payload, db)
        return 0, res, None


def main():
    """CLI 入口点。"""
    parser = argparse.ArgumentParser(description="Horon Multi-Environment Hook Gateway")
    parser.add_argument("event", nargs="?", default="", help="Hook Event Name (e.g. PreToolUse, PreInvocation)")
    parser.add_argument(
        "--adapter",
        dest="adapter",
        choices=["antigravity", "claude-code", "codex", "opencode", "dsh"],
        default=None,
        help="Target IDE/CLI adapter (default: auto-detect)",
    )
    args, unknown = parser.parse_known_args()

    payload = _read_stdin_payload()
    event_name = args.event
    if not event_name and "event" in payload:
        event_name = payload["event"]
    elif not event_name and "hook_event_name" in payload:
        event_name = payload["hook_event_name"]

    if not event_name:
        print(json.dumps({}))
        return

    adapter_type = detect_adapter_type(payload, args.adapter)
    load_dotenv(_PROJECT_ROOT / ".env")
    db_path = _PROJECT_ROOT / os.environ["HORON_DB"]
    if not database_exists(db_path):  # 刚装好 Horon、还没跑过 CLI 时，第一次 hook 建库
        create_database(db_path)
    db = HoronDB(db_path=db_path, session_id=payload_session_id(payload, adapter_type))
    try:
        exit_code, stdout_data, stderr_data = dispatch_hook(event_name, payload, db, adapter_type)
    finally:
        db.close()

    if stderr_data:
        sys.stderr.write(stderr_data)

    if stdout_data is not None:
        if isinstance(stdout_data, str):
            print(stdout_data)
        else:
            print(json.dumps(stdout_data, ensure_ascii=False))

    if exit_code != 0:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
