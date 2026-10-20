"""Horon 只读 MCP 适配器。

给 run_command 会从头部截断长输出的宿主（Antigravity）用：
login / read_concept / search_keyword / search_semantic 原样转调 frontend/cli.py，
返回 CLI 的完整 stdout，不做任何截断、分页或改写。
写操作仍走 CLI。

会话归属：CLI 按 ANTIGRAVITY_CONVERSATION_ID 等环境变量判断会话。
本进程常驻，环境变量里没有每个对话的 ID；Antigravity 把它放在每次请求的
_meta["antigravity.google/conversation_id"]，这里取出来再设给 CLI 子进程。
"""
import os
import subprocess
import sys
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "frontend" / "cli.py"

mcp = FastMCP("horon")


def _conversation_id(ctx: Context | None) -> str | None:
    """从请求的 _meta 取 Antigravity 会话 ID（2026-10-04 实测键名）；取不到返回 None。"""
    try:
        meta = ctx.request_context.meta.model_dump() if ctx else {}
    except Exception:  # 没有请求上下文或没有 _meta
        return None
    cid = meta.get("antigravity.google/conversation_id")
    return str(cid) if cid else None


def _run(*args: str, ctx: Context | None = None) -> str:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    cid = _conversation_id(ctx)
    if cid:
        env["ANTIGRAVITY_CONVERSATION_ID"] = cid
    p = subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=REPO, env=env, capture_output=True, timeout=300,
        stdin=subprocess.DEVNULL,  # 不能继承 MCP 的 stdio 管道，否则子进程挂住
    )
    out = p.stdout.decode("utf-8", errors="replace")
    err = p.stderr.decode("utf-8", errors="replace").strip()
    if p.returncode != 0 or err:
        out += f"\n[exit {p.returncode}]\n{err}" if err else f"\n[exit {p.returncode}]"
    return out


@mcp.tool()
def login(ctx: Context, recent: int = 10) -> str:
    """会话开场：输出所有 boot 节点全文和最近修改的节点（等同 cli.py login）。"""
    return _run("login", "--recent", str(recent), ctx=ctx)


@mcp.tool()
def read_concept(concepts: list[str], ctx: Context) -> str:
    """读取一个或多个节点（名称、别名或 ID）的全文。要一起看的节点一次传完，整批算一次读取。"""
    return _run("read_concept", *concepts, ctx=ctx)


@mcp.tool()
def search_keyword(query: str, ctx: Context, tag: str | None = None, limit: int = 50) -> str:
    """关键词搜索节点名称、别名、书腰与正文。tag 为可选的 Tag 表达式（A & B / A | B）。"""
    args = ["search_keyword", query, "--limit", str(limit)]
    if tag:
        args += ["--tag", tag]
    return _run(*args, ctx=ctx)


@mcp.tool()
def search_semantic(query: str, ctx: Context, limit: int = 10) -> str:
    """按书腰语义检索：用大白话描述当前情境或疑问。"""
    return _run("search_semantic", query, "--limit", str(limit), ctx=ctx)


if __name__ == "__main__":
    mcp.run()
