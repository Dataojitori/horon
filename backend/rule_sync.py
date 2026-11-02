"""
远程模式下，把服务器记忆库里钩子要用的规则同步进本地的规则库。

远程模式的 CLI 命令都在服务器上执行，钩子在本机跑，只读节点的电路字段和规则表。
所以本机放一个和记忆库结构相同、只装规则的库（见 backend/remote_client.py），规则一变就用服务器的覆盖。

    rules = export_rules(db_path)        # 服务器：从记忆库导出，全程只读
    version = rules_version(rules)       # 两边算法相同：内容一样，版本就一样
    apply_rules(rules_db, rules)         # 本地：在一个事务里覆盖规则库

覆盖的是 concepts 的电路字段（CONCEPT_COLUMNS）和 RULE_TABLES 里的表。服务器上没有的节点从规则库删掉，
外键级联会带走它们的会话状态。会话表（sessions、session_active_sensors、active_chain_instances、
pending_notifications）只属于本机，其余不动。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from backend._db_common import _now

CONCEPT_COLUMNS = ("id", "name", "role", "is_active", "lifespan", "activation_type", "on_fire")

# 表名 → 列。排序键让两次导出的顺序一致。
RULE_TABLES: dict[str, tuple[str, ...]] = {
    "sensor_hooks": ("id", "sensor_concept_id", "event_type", "tool", "match_pattern", "created_at",
                     "adapter", "jev_question", "jev_threshold"),
    "tool_guards": ("id", "guard_concept_id", "tool", "args_pattern", "created_at", "adapter"),
    "compose_members": ("parent_concept_id", "member_concept_id", "order_index"),
    "inhibitions": ("target_concept_id", "inhibitor_concept_id", "created_at"),
    "aliases": ("alias", "concept_id"),  # 本地的 compile、reset 按名字找节点时用
}


def export_rules(db_path: str | Path) -> dict[str, list[list[Any]]]:
    """从 db_path 导出规则。只读打开，在同一个读事务里取完，各表彼此对齐。

    输出：{"concepts": [[...], ...], "sensor_hooks": [...], ...}，每行按上面列的顺序，可直接序列化成 JSON。
    库文件不存在时抛 sqlite3.OperationalError。
    """
    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as conn:
        conn.execute("BEGIN")
        rules = {"concepts": _rows(conn, "concepts", CONCEPT_COLUMNS)}
        for table, columns in RULE_TABLES.items():
            rules[table] = _rows(conn, table, columns)
        conn.execute("COMMIT")
    return rules


def _rows(conn: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> list[list[Any]]:
    names = ", ".join(columns)
    return [list(row) for row in conn.execute(f"SELECT {names} FROM {table} ORDER BY {names}")]


def rules_version(rules: dict[str, list[list[Any]]]) -> str:
    """规则内容的 SHA-256。不用计数器：无论走哪条路改了规则，内容变了版本就变，内容没变就不变。"""
    text = json.dumps(rules, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def apply_rules(db_path: str | Path, rules: dict[str, list[list[Any]]]) -> None:
    """用 rules 覆盖 db_path 的电路字段和规则表，一个事务里做完；中途失败本地库原样不动。

    节点按 id 对应：本地有的更新电路字段，没有的新建（只有名字和电路字段），
    rules 里没有的删除（连带它的会话状态）。RULE_TABLES 的表整张换成 rules 里的行。
    """
    concepts = rules["concepts"]
    with closing(sqlite3.connect(Path(db_path))) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        with conn:
            conn.execute("CREATE TEMP TABLE server_ids (id INTEGER PRIMARY KEY)")
            conn.executemany("INSERT INTO server_ids VALUES (?)", [(row[0],) for row in concepts])
            conn.execute("DELETE FROM concepts WHERE id NOT IN (SELECT id FROM server_ids)")
            for table in RULE_TABLES:
                conn.execute(f"DELETE FROM {table}")
            # 名字是 UNIQUE：服务器上两个节点互换了名字时，逐行改会撞上。先给改名的节点换成不会重复的临时名。
            conn.executemany("UPDATE concepts SET name = ? WHERE id = ? AND name != ?",
                             [(f"\0{row[0]}", row[0], row[1]) for row in concepts])
            columns = ", ".join(CONCEPT_COLUMNS)
            marks = ", ".join("?" * len(CONCEPT_COLUMNS))
            updates = ", ".join(f"{c} = excluded.{c}" for c in CONCEPT_COLUMNS[1:])
            now = _now()
            conn.executemany(
                f"INSERT INTO concepts ({columns}, created_at, updated_at) VALUES ({marks}, ?, ?) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                [[*row, now, now] for row in concepts],
            )
            for table, table_columns in RULE_TABLES.items():
                table_marks = ", ".join("?" * len(table_columns))
                conn.executemany(
                    f"INSERT INTO {table} ({', '.join(table_columns)}) VALUES ({table_marks})", rules[table])
