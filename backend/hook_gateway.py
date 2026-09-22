"""
Horon Hook Gateway — 多环境生命周期 Hooks 统一网关。

支持的环境适配器 (Adapters):
  - antigravity (默认): Antigravity / Cursor IDE 生命周期 (.agents/hooks.json)
  - claude-code:        Anthropic Claude Code CLI 生命周期 (settings.json)
  - codex:              OpenAI Codex CLI 生命周期 (hooks.json / config.toml)

调用方式 (CLI Entry Point):
  python -m backend.hook_gateway <EventName> [--adapter antigravity|claude-code|codex]
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

from backend.db import HoronDB
from backend.harness.adapters.antigravity import handle_antigravity
from backend.harness.adapters.claude_code import handle_claude_code
from backend.harness.adapters.codex import handle_codex
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
    """根据命令行参数、环境变量或 payload 特征自动检测适配器类型。"""
    if explicit_adapter:
        clean = explicit_adapter.strip().lower()
        if clean in ("claude", "claude-code", "claudecode"):
            return "claude-code"
        if clean in ("codex", "openai-codex"):
            return "codex"
        if clean in ("antigravity", "cursor"):
            return "antigravity"

    env_adapter = os.environ.get("HORON_HOOK_ENV", "").strip().lower()
    if env_adapter in ("claude-code", "claude"):
        return "claude-code"
    if env_adapter in ("codex", "openai"):
        return "codex"
    if env_adapter in ("antigravity", "cursor"):
        return "antigravity"

    # 基于 Payload 结构特征启发式自动探测
    if "hook_event_name" in payload or "tool_response" in payload:
        if "turn_id" in payload:
            return "codex"
        return "claude-code"

    if "conversationId" in payload or "transcriptPath" in payload or "toolCall" in payload:
        return "antigravity"

    return "antigravity"


def dispatch_hook(
    event_name: str,
    payload: dict[str, Any],
    db: HoronDB | None = None,
    adapter_type: str = "antigravity",
) -> tuple[int, Any, str | None]:
    """通用生命周期事件分发入口。
    
    Returns:
        tuple[exit_code, stdout_data, stderr_data]
    """
    should_close = False
    if db is None:
        db = HoronDB()
        should_close = True

    try:
        if adapter_type == "claude-code":
            return handle_claude_code(event_name, payload, db)
        elif adapter_type == "codex":
            return handle_codex(event_name, payload, db)
        else:
            res = handle_antigravity(event_name, payload, db)
            return 0, res, None
    finally:
        if should_close:
            db.close()


def main():
    """CLI 入口点。"""
    parser = argparse.ArgumentParser(description="Horon Multi-Environment Hook Gateway")
    parser.add_argument("event", nargs="?", default="", help="Hook Event Name (e.g. PreToolUse, PreInvocation)")
    parser.add_argument(
        "--adapter",
        dest="adapter",
        choices=["antigravity", "claude-code", "codex"],
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
    exit_code, stdout_data, stderr_data = dispatch_hook(event_name, payload, adapter_type=adapter_type)

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
