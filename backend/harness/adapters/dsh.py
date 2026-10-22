"""
DeepSeek Harness (dsh) Hook Protocol Handler (Pure Function).

dsh 没有命令钩子，只有 JS 插件。插件（正本 backend/harness/dsh-plugin/index.js，挂法见该文件开头）
把 dsh 的事件翻成和 opencode 插件相同的事件和 payload，从 stdin 交给
`run_gateway.py <事件> --adapter dsh`。事件、payload 和返回值见 adapters/opencode.py 开头，
处理逻辑也共用（handle_plugin_event）。工具名和参数按 dsh 原样匹配
（pwsh、read、edit、horon 的参数是 {"args": [...]}）。
"""

from __future__ import annotations

from typing import Any

from backend.db import HoronDB
from backend.harness.adapters.opencode import handle_plugin_event

ADAPTER = "dsh"


def handle_dsh(
    event_name: str,
    payload: dict[str, Any],
    db: HoronDB,
) -> tuple[int, dict[str, Any] | None, str | None]:
    """处理 dsh 插件转来的事件。返回 (exit_code, stdout_dict, stderr)。"""
    return handle_plugin_event(event_name, payload, db, adapter=ADAPTER)
