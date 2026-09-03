"""
Horon DB — shared constants, validators, path setup, and the transactional decorator.

Every sub-module (_db_plugins, _db_concepts, …) imports from here instead of
duplicating definitions.
"""
from __future__ import annotations

import ast
import json
import logging
import os
import re
import time
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from . import tag_sandbox
from .tag_sandbox import load_plugin

_logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

_CONTROL_CHAR_RE = re.compile(r'[\x00-\x1f\x7f]')
_MAX_NAME_LEN = 200
_FORBIDDEN_CHARS = {'→', '&', ':', '|'}

SYSTEM_TAGS = {"result", "exit", "state", "action"}
VALID_ON_FIRE_ACTION_TYPES = frozenset({"notify", "set_focus", "add_todo"})

_SANDBOX_ALLOWED_CALLS = frozenset({"exists", "status", "tags"})
_SANDBOX_ALLOWED_NODES = frozenset({
    ast.Expression, ast.BoolOp, ast.UnaryOp, ast.Not, ast.And, ast.Or,
    ast.Compare,
    ast.Eq, ast.NotEq, ast.Lt, ast.Gt, ast.LtE, ast.GtE,
    ast.In, ast.NotIn, ast.Is, ast.IsNot,
    ast.Call, ast.Constant, ast.Name, ast.Load,
    ast.Tuple, ast.List, ast.Set,
})

# ── Path / env ───────────────────────────────────────────────────────────────

_PROJECT_DIR = Path(__file__).parent.parent
_SCHEMA_PATH = _PROJECT_DIR / "backend" / "schema.sql"
_MIGRATIONS_DIR = _PROJECT_DIR / "backend" / "migrations"

load_dotenv(_PROJECT_DIR / ".env")

if "HORON_DB" not in os.environ:
    raise RuntimeError("HORON_DB not set. Check .env file.")
_DB_PATH = _PROJECT_DIR / os.environ["HORON_DB"]

# ── Helpers ──────────────────────────────────────────────────────────────────

def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ── Validators ───────────────────────────────────────────────────────────────

def _validate_condition_ast(expr: str) -> None:
    """Validate a reminder condition expression for sandbox safety.

    Whitelist approach: only comparison / boolean / whitelisted-call nodes
    are permitted.  Everything else (imports, attribute access, assignments,
    lambdas, comprehensions, subscript, …) is rejected outright.
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"Condition syntax error: {e}") from e

    for node in ast.walk(tree):
        ntype = type(node)
        if ntype not in _SANDBOX_ALLOWED_NODES:
            raise ValueError(
                f"Forbidden construct in condition: {ntype.__name__}. "
                f"Only comparisons, boolean logic (and/or/not), and calls "
                f"to exists/status/tags are allowed.")
        if ntype is ast.Call:
            if not isinstance(node.func, ast.Name):
                raise ValueError(
                    "Only direct function calls are allowed "
                    "(exists, status, tags).")
            if node.func.id not in _SANDBOX_ALLOWED_CALLS:
                raise ValueError(
                    f"Function '{node.func.id}' is not allowed. "
                    f"Allowed: {', '.join(sorted(_SANDBOX_ALLOWED_CALLS))}.")
            if node.keywords:
                raise ValueError(
                    f"Function '{node.func.id}' does not accept keyword arguments.")
            if len(node.args) != 1:
                raise ValueError(
                    f"Function '{node.func.id}' requires exactly 1 argument ({len(node.args)} given).")
            arg = node.args[0]
            if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
                raise ValueError(
                    f"Function '{node.func.id}' argument must be a string literal.")
        if ntype is ast.Name and isinstance(node.ctx, ast.Load):
            allowed_names = _SANDBOX_ALLOWED_CALLS | {"TODAY", "NOW"}
            if node.id not in allowed_names:
                raise ValueError(
                    f"Name '{node.id}' is not available in condition scope. "
                    f"Available: {', '.join(sorted(allowed_names))}.")


def _validate_name(name: str) -> str:
    """校验并清理名字（concept 名或 alias）。返回 strip 后的名字，不合法则 raise。"""
    name = name.strip()
    if not name:
        raise ValueError("Name cannot be empty.")
    if len(name) > _MAX_NAME_LEN:
        raise ValueError(f"Name too long ({len(name)} chars, max {_MAX_NAME_LEN}).")
    if _CONTROL_CHAR_RE.search(name):
        raise ValueError("Name cannot contain control characters.")
    bad = _FORBIDDEN_CHARS & set(name)
    if bad:
        raise ValueError(f"Name cannot contain operator characters: {bad}")
    try:
        int(name)
    except (ValueError, TypeError):
        pass
    else:
        raise ValueError("Name cannot be purely numeric (ambiguous with concept ID).")
    return name


def _validate_and_normalize_on_fire(raw: Any) -> str | None:
    """校验并归一化 on_fire 动作配置。

    支持格式：
      1. 多个动作（列表）：[{"notify": "消息内容"}, {"add_todo": "待办内容"}]
      2. 单个动作（字典）：{"notify": "消息内容"}
      3. 纯文本字符串："直接文本" -> 自动包装为 {"notify": "直接文本"}

    规则：
      - 任何动作的 key 必须在 VALID_ON_FIRE_ACTION_TYPES 白名单中。
      - 任何动作对应的 value 必须是非空字符串。
      - 包含未定义 key 或空值时直接抛出 ValueError。
    """
    if raw is None:
        return None

    # 1. 处理字符串输入
    if isinstance(raw, str):
        clean_str = raw.strip()
        if not clean_str:
            return None
        if (clean_str.startswith("{") and clean_str.endswith("}")) or (clean_str.startswith("[") and clean_str.endswith("]")):
            try:
                parsed = json.loads(clean_str)
            except Exception as e:
                raise ValueError(f"on_fire JSON 格式错误: {e}") from e
        else:
            # 纯文本字符串 -> 自动包装为 notify 动作字典
            return json.dumps({"notify": clean_str}, ensure_ascii=False)
    else:
        parsed = raw

    # 2. 校验单字典格式
    if isinstance(parsed, dict):
        if not parsed:
            raise ValueError("on_fire 动作对象不能为空。")
        for k, v in parsed.items():
            if k not in VALID_ON_FIRE_ACTION_TYPES:
                raise ValueError(
                    f"未定义的 on_fire 动作种类 '{k}'。支持的动作种类: {', '.join(sorted(VALID_ON_FIRE_ACTION_TYPES))}"
                )
            if not isinstance(v, str) or not v.strip():
                raise ValueError(f"动作 '{k}' 的内容必须是非空字符串，得到: {v!r}")
        return json.dumps(parsed, ensure_ascii=False)

    # 3. 校验列表格式
    elif isinstance(parsed, list):
        if not parsed:
            raise ValueError("on_fire 动作列表不能为空。")
        for item in parsed:
            if not isinstance(item, dict) or not item:
                raise ValueError(f"on_fire 列表中的每个动作必须是非空字典，得到: {item!r}")
            for k, v in item.items():
                if k not in VALID_ON_FIRE_ACTION_TYPES:
                    raise ValueError(
                        f"未定义的 on_fire 动作种类 '{k}'。支持的动作种类: {', '.join(sorted(VALID_ON_FIRE_ACTION_TYPES))}"
                    )
                if not isinstance(v, str) or not v.strip():
                    raise ValueError(f"动作 '{k}' 的内容必须是非空字符串，得到: {v!r}")
        return json.dumps(parsed, ensure_ascii=False)

    else:
        raise ValueError(f"无效的 on_fire 数据格式: {type(parsed).__name__}。必须是动作列表或字典。")


# ── Transactional decorator ──────────────────────────────────────────────────

def transactional(method):
    '''确保方法执行在显式事务边界内。失败自动回滚，成功自动提交。
    支持重入：已在事务内的嵌套调用直接穿透，不会提前 commit。'''
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        if getattr(self, '_in_transaction', False):
            return method(self, *args, **kwargs)

        self._in_transaction = True
        self._post_commit_hooks = []
        
        try:
            # Pre-check mtime for all cached plugins to ensure snapshot is up-to-date.
            # If a plugin file was modified, we omit it from the snapshot so that
            # the first access within the transaction will reload it.
            snapshot = {}
            for tag, (cached_plugin, cached_mtime) in getattr(self, '_plugin_cache', {}).items():
                filepath = tag_sandbox._PLUGINS_DIR / f"{tag}.py"
                current_mtime = filepath.stat().st_mtime if filepath.exists() else None
                if current_mtime == cached_mtime:
                    snapshot[tag] = cached_plugin
            self._txn_plugin_snapshot = snapshot
            
            with self.conn:
                res = method(self, *args, **kwargs)
                
            # 事务已成功提交，离开锁范围后执行后置钩子
            hooks = self._post_commit_hooks
            self._post_commit_hooks = []
            for hook in hooks:
                try:
                    hook()
                except Exception as e:
                    _logger.warning("Post-commit hook failed: %s", e)
            return res
        finally:
            self._in_transaction = False
            self._txn_plugin_snapshot.clear()
            self._post_commit_hooks = []
    return wrapper
