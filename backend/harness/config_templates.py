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
    """生成 OpenAI Codex 的 hooks.json 格式配置。"""
    return {
        "description": "Horon Harness Hooks for OpenAI Codex",
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
                            "statusMessage": "Horon guard checking tool permissions",
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
