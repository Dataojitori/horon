"""
Horon Harness Configuration Generators for Various Environments.

提供 Antigravity、Claude Code、OpenAI Codex 等环境的 Hook 配置文件生成模板。
"""

from __future__ import annotations

import json
from typing import Any


def get_antigravity_hooks_config(command_prefix: str = "python run_gateway.py") -> dict[str, Any]:
    """生成 Antigravity / Cursor 的 .agents/hooks.json 格式配置。"""
    return {
        "horon-harness": {
            "PreInvocation": [
                {
                    "type": "command",
                    "command": f"{command_prefix} PreInvocation",
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{command_prefix} PreToolUse",
                        }
                    ],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{command_prefix} PostToolUse",
                        }
                    ],
                }
            ],
            "PostInvocation": [
                {
                    "type": "command",
                    "command": f"{command_prefix} PostInvocation",
                }
            ],
            "Stop": [
                {
                    "type": "command",
                    "command": f"{command_prefix} Stop",
                }
            ],
        }
    }


def get_claude_code_hooks_config(command_prefix: str = "python run_gateway.py --adapter claude-code") -> dict[str, Any]:
    """生成 Anthropic Claude Code 的 settings.json hooks 配置。"""
    return {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{command_prefix} SessionStart",
                        }
                    ]
                }
            ],
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{command_prefix} UserPromptSubmit",
                        }
                    ]
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{command_prefix} PreToolUse",
                        }
                    ]
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{command_prefix} PostToolUse",
                        }
                    ]
                }
            ],
            "PostToolUseFailure": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{command_prefix} PostToolUseFailure",
                        }
                    ]
                }
            ],
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{command_prefix} Stop",
                        }
                    ]
                }
            ],
        }
    }


def get_codex_hooks_config(command_prefix: str = "python run_gateway.py --adapter codex") -> dict[str, Any]:
    """生成 OpenAI Codex 的 hooks.json 格式配置。

    输入: command_prefix，钩子命令的前缀（解释器 + run_gateway.py + --adapter codex），事件名会接在它后面。
          Codex 在用户项目的目录里执行钩子，所以真正安装时必须传入绝对路径；默认值只在仓库根目录下有效。
    输出: 可直接写入 hooks.json 的字典。事件、matcher、timeout、additionalContextLimit
          照搬作者本机实测跑通的配置；其中 Interrupt 不可省略——Codex 被用户打断时不会触发 Stop，
          要靠它清掉本回合的 turn 传感器，否则它们会一直亮着。
    """

    def entry(event: str, status: str, matcher: str | None = None, **extra: Any) -> list[dict[str, Any]]:
        command = f"{command_prefix} {event}"
        # Windows 上 Codex 用 PowerShell 执行 commandWindows；以引号开头的一行会被当成字符串表达式而报错，
        # 要加调用运算符 `& `。不以引号开头的命令（如裸 `python ...`）两种 shell 都能直接跑，保持原样。
        command_windows = f"& {command}" if command.startswith('"') else command
        hook: dict[str, Any] = {"type": "command", "command": command, "commandWindows": command_windows, "statusMessage": status}
        hook.update(extra)
        group: dict[str, Any] = {"hooks": [hook]}
        if matcher is not None:
            group = {"matcher": matcher, **group}
        return [group]

    return {
        "description": "Horon Harness Hooks for OpenAI Codex",
        "hooks": {
            "SessionStart": entry("SessionStart", "Initializing Horon Harness",
                                  matcher="startup|resume|clear|compact", additionalContextLimit=5000),
            "UserPromptSubmit": entry("UserPromptSubmit", "Horon sensing user input", additionalContextLimit=5000),
            "PreToolUse": entry("PreToolUse", "Horon checking tool guards", matcher="*",
                                timeout=30, additionalContextLimit=5000),
            "PostToolUse": entry("PostToolUse", "Horon sensing tool result", matcher="*", timeout=30),
            "Stop": entry("Stop", "Horon evaluating turn completion", timeout=30),
            "Interrupt": entry("Interrupt", "Horon resetting interrupted turn", timeout=3),
        },
    }
