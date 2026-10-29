"""
Horon DB — shared constants, validators, path setup, and the transactional decorator.

Every sub-module (_db_plugins, _db_concepts, …) imports from here instead of
duplicating definitions.
"""
from __future__ import annotations

import json
import logging
import re
import time
from functools import wraps
from pathlib import Path

from . import tag_sandbox
from .tag_sandbox import load_plugin

_logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

_CONTROL_CHAR_RE = re.compile(r'[\x00-\x1f\x7f]')
_MAX_NAME_LEN = 200
_FORBIDDEN_CHARS = {'→', '&', ':', '|'}

SYSTEM_TAGS = {"exit", "state", "boot"}
VALID_ON_FIRE_ACTION_TYPES = frozenset({"notify", "set_focus", "add_todo"})
# on_fire 所有动作文字加起来的大小上限，按 UTF-8 字节算（中日文一字 3 字节、英文字母 1 字节，
# 同样的信息量大致同价；正文的 8192 B 线也按字节）。900 B 约中文 300 字、英文 150 词。
# 提醒在那一刻只该推一两问；规则清单写进做法节点正文，能查的交给代码、正则或 Jev。超了写入直接拒绝。
ON_FIRE_MAX_BYTES = 900

OFFLINE_DEV_SESSION_ID = "devonly"

# ── Path / env ───────────────────────────────────────────────────────────────

_PROJECT_DIR = Path(__file__).parent.parent
_SCHEMA_PATH = _PROJECT_DIR / "backend" / "schema.sql"
_MIGRATIONS_DIR = _PROJECT_DIR / "backend" / "migrations"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ── Validators ───────────────────────────────────────────────────────────────

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


def parse_jev_questions(raw: str | None) -> list[str]:
    """读 sensor_hooks.jev_question 列。输入：列值。输出：题目列表，空列返回 []。
    列里是纯文本就当一道题；是 JSON 字符串列表就是多道题。"""
    if not raw or not raw.strip():
        return []
    text = raw.strip()
    if text.startswith("["):
        try:
            items = json.loads(text)
        except ValueError:
            return [text]
        if isinstance(items, list):
            return [it.strip() for it in items if isinstance(it, str) and it.strip()]
    return [text]


def dump_jev_questions(questions: list[str]) -> str | None:
    """写 sensor_hooks.jev_question 列。输入：题目列表。输出：列值；
    没有题返回 None，一道题存纯文本，多道存 JSON 字符串列表。"""
    if not questions:
        return None
    if len(questions) == 1:
        return questions[0]
    return json.dumps(questions, ensure_ascii=False)


def _check_on_fire_length(texts) -> None:
    """输入：on_fire 各动作的文字。行为：UTF-8 总字节数超过 ON_FIRE_MAX_BYTES 就抛 ValueError，
    报错里说超了多少、该怎么拆。输出：无。"""
    total = sum(len(t.encode("utf-8")) for t in texts)
    if total > ON_FIRE_MAX_BYTES:
        raise ValueError(
            f"on_fire text is {total} B, over the {ON_FIRE_MAX_BYTES} B limit (UTF-8; about 300 Chinese characters "
            f"or 150 English words). Keep only the one or two questions left for your judgment at that moment; put the "
            f"rules and their reasons in the body of a practice node, and leave whatever can be checked to code, regex "
            f"or Jev (a sensor that fires only when it finds a violation in the written output).")


def _validate_and_normalize_on_fire(raw: Any) -> str | None:
    """校验并归一化 on_fire 动作配置。

    支持格式：
      1. 单个动作（字典或 JSON 字符串）：{"notify": "消息内容"}
      2. 多个动作（列表或 JSON 字符串）：[{"notify": "消息内容"}, {"add_todo": "待办内容"}]

    规则：
      - 必须显式指定动作类型，且 key 必须在 VALID_ON_FIRE_ACTION_TYPES 白名单中。
      - 任何动作对应的 value 必须是非空字符串。
      - 纯文本或未指定动作类型的非法格式直接抛出 ValueError。
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
                raise ValueError(f"on_fire JSON 格式错误: {e}。请确保使用合法的标准 JSON 格式（如 '{{\"notify\": \"...\"}}'）。") from e
        else:
            raise ValueError(
                f"on_fire 配置必须是合法的 JSON 动作对象（如 '{{\"notify\": \"...\"}}'）或动作列表。不支持未指定动作类型的裸文本。"
            )
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
        _check_on_fire_length(parsed.values())
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
        _check_on_fire_length(v for item in parsed for v in item.values())
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
