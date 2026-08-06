"""
Horon — Inference Language for Intelligent Agents
DB operations (Concept → Variation 分層結構)

Concept:   概念的对外身份（名字 + disclosure），组合的参与单位。
Variation: 同一概念的不同解释（concept_id + short_code + type），
           type ∈ {CHAIN, AND, OR, NULL(原子)}，
           每个 variation 有独立的 status / content / compose_members。
compose_members 的 member 引用 concept_id（hub），不是具体 variation。

Module layout:
  _db_common.py    — constants, validators, path setup, transactional decorator
  _db_plugins.py   — PluginMixin  (proxy factories, mutation hooks, cluster audit)
  _db_concepts.py  — ConceptMixin (create/delete concepts & tags, suppose)
  _db_mutations.py — MutationMixin (add / delete / set / update)
  _db_query.py     — QueryMixin   (search, read, expressions, relations)
  _db_reminders.py — ReminderMixin (reminder CRUD, sandbox eval, inbox)
  _db_compile.py   — CompileMixin (relation graph, compile)
  db.py  (this file) — HoronDB assembly + core infrastructure
"""
from __future__ import annotations

import secrets
import sqlite3
import sys
from pathlib import Path

from ._db_common import (
    _now, _validate_condition_ast, SYSTEM_TAGS, transactional,  # noqa: F401  re-exported
    _SCHEMA_PATH, _MIGRATIONS_DIR, _DB_PATH,
)
from ._db_plugins import PluginMixin
from ._db_concepts import ConceptMixin
from ._db_mutations import MutationMixin
from ._db_query import QueryMixin
from ._db_reminders import ReminderMixin
from ._db_compile import CompileMixin


class HoronDB(
    PluginMixin,
    ConceptMixin,
    MutationMixin,
    QueryMixin,
    ReminderMixin,
    CompileMixin,
):
    def __init__(self, *, check_same_thread: bool = True):
        is_new = not _DB_PATH.exists() or _DB_PATH.stat().st_size == 0
        self._plugin_cache: dict[str, tuple[dict | None, float | None]] = {}
        self._txn_plugin_snapshot: dict[str, dict | None] = {}
        self.conn = sqlite3.connect(
            str(_DB_PATH), check_same_thread=check_same_thread)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if is_new:
            self.conn.executescript(_SCHEMA_PATH.read_text())
            self.conn.executemany(
                "INSERT INTO schema_migrations (version, applied_at) "
                "VALUES (?, ?)",
                [(path.stem, _now()) for path in self._migration_files()],
            )
            self.conn.commit()
        else:
            self._apply_migrations()

    def close(self):
        self.conn.close()

    # ── Schema migrations ────────────────────────────────────────────────────

    @staticmethod
    def _migration_files() -> list[Path]:
        if not _MIGRATIONS_DIR.exists():
            return []
        return sorted(_MIGRATIONS_DIR.glob("*.sql"))

    def _apply_migrations(self):
        """Apply SQL migrations that this existing database has not recorded."""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )""")
        self.conn.commit()
        applied = {
            row["version"]
            for row in self.conn.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        }

        for path in self._migration_files():
            version = path.stem
            if version in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            escaped_version = version.replace("'", "''")
            full_script = (
                "BEGIN;\n"
                f"{sql}\n"
                "INSERT INTO schema_migrations (version, applied_at) "
                f"VALUES ('{escaped_version}', '{_now()}');\n"
                "COMMIT;"
            )
            try:
                self.conn.executescript(full_script)
            except sqlite3.OperationalError as e:
                err_msg = str(e).lower()
                sql_lower = sql.lower()
                is_drop_col_idempotent = (
                    "drop column" in sql_lower and "no such column" in err_msg
                )
                is_add_col_idempotent = (
                    "add column" in sql_lower and "duplicate column name" in err_msg
                )
                if is_drop_col_idempotent or is_add_col_idempotent:
                    self.conn.rollback()
                    self.conn.execute(
                        "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                        (version, _now()),
                    )
                    self.conn.commit()
                else:
                    self.conn.rollback()
                    raise
            except Exception:
                self.conn.rollback()
                raise

    # ── Resolution ───────────────────────────────────────────────────────────

    def _resolve_id(self, query) -> tuple[int, list[str]]:
        """将输入解析为 (concept_id, [short_codes])。

        接受格式：
          "爱"      → (cid, ["a3f1"])          — sole variation
          "爱"      → (cid, ["a3f1","b7e2"])   — 多 variation
          "爱:a3f1" → (cid, ["a3f1"])          — 显式指定，验证存在
          42        → 同 "爱"，按 concept ID 查

        调用者按 len 判断：
          len == 1 → 唯一 variation，直接用 [0]
          len > 1  → 多 variation，按业务决定报错还是新建
        """
        sc: str | None = None

        if isinstance(query, str) and ":" in query:
            concept_part, sc = query.split(":", 1)
            query = concept_part

        cid: int | None = None

        raw_int: int | None = None
        if isinstance(query, int):
            raw_int = query
        elif isinstance(query, str):
            try:
                raw_int = int(query)
            except (ValueError, TypeError):
                pass

        if raw_int is not None:
            row = self.conn.execute(
                "SELECT id FROM concepts WHERE id = ?", (raw_int,)
            ).fetchone()
            if row:
                cid = row["id"]

        if cid is None and isinstance(query, str):
            row = self.conn.execute(
                "SELECT concept_id FROM aliases WHERE alias = ?", (query,)
            ).fetchone()
            if row:
                cid = row["concept_id"]

        if cid is None:
            raise ValueError(f"Concept not found: {query}")

        if sc is not None:
            row = self.conn.execute(
                "SELECT 1 FROM variations "
                "WHERE concept_id=? AND short_code=?",
                (cid, sc),
            ).fetchone()
            if not row:
                concept_name = self._resolve_concept_name(cid)
                raise ValueError(
                    f"No variation '{sc}' in concept '{concept_name}' (ID: {cid}). "
                    f"Use '{query}:short_code' format.")
            return (cid, [sc])

        var_rows = self.conn.execute(
            "SELECT short_code FROM variations WHERE concept_id=? "
            "ORDER BY short_code",
            (cid,),
        ).fetchall()
        return (cid, [r["short_code"] for r in var_rows])

    def _resolve_single_variation(self, node) -> tuple[int, str]:
        """解析单点定位，确保精确命中一个 variation。"""
        cid, scs = self._resolve_id(node)
        if len(scs) == 0:
            concept_name = self._resolve_concept_name(cid)
            raise ValueError(
                f"Concept '{concept_name}' (ID: {cid}) has no variations.")
        if len(scs) != 1:
            concept_name = self._resolve_concept_name(cid)
            raise ValueError(
                f"Concept '{concept_name}' (ID: {cid}) has multiple variations; "
                f"use '{node}:short_code' to specify.")
        return cid, scs[0]

    def _resolve_concept_name(self, concept_id: int) -> str:
        """concept ID → 显示名。"""
        row = self.conn.execute(
            "SELECT name FROM concepts WHERE id=?", (concept_id,)
        ).fetchone()
        if row:
            return row["name"]
        raise ValueError(f"No concept with id {concept_id}")

    def log_action(self, *, command: str,
                   concept_id: int | None = None,
                   concept_name: str | None = None,
                   short_code: str | None = None,
                   sub_action: str | None = None,
                   success: bool = True) -> None:
        """INSERT into cli_audit_log. 审计写失败不打断真正的操作，
        但会在 stderr 报一行，避免静默吞掉表缺失/SQL 错等真正的 bug。"""
        try:
            self.conn.execute(
                "INSERT INTO cli_audit_log"
                " (timestamp, command, concept_id, concept_name,"
                "  short_code, sub_action, success)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_now(), command, concept_id, concept_name,
                 short_code, sub_action, int(success)),
            )
            self.conn.commit()
        except Exception as e:
            print(f"[audit] log_action failed: {e}", file=sys.stderr)

    def _check_name_available(self, name: str,
                              exclude_concept_id: int | None = None) -> None:
        """跨 concept 名字唯一性校验。不同 concept 之间不允许重名（主名或别名）。
        aliases 表是名字的唯一权威来源（主名在建 concept 时同步注册）。"""
        row = self.conn.execute(
            "SELECT concept_id FROM aliases WHERE alias = ?", (name,)
        ).fetchone()
        if row and (exclude_concept_id is None
                    or row["concept_id"] != exclude_concept_id):
            raise ValueError(
                f"Name '{name}' already belongs to concept "
                f"{row['concept_id']}.")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _parse_expression(self, expression: str, allow_single: bool = False) -> tuple[str, list[int]]:
        """拆分表达式，解析为 (type, member_concept_ids)。

        严格不混用：一个表达式只能包含一种运算符。
        返回 (variation_type, ordered_member_ids)。

        CHAIN (A → B → C):  ('CHAIN', [id_A, id_B, id_C])
        AND   (A & B & C):  ('AND',   [id_A, id_B, id_C])
        OR    (A | B | C):  ('OR',    [id_A, id_B, id_C])
        Single (A):         ('SINGLE',[id_A])
        """
        has_arrow = "→" in expression
        has_amp = "&" in expression
        has_pipe = "|" in expression

        op_count = sum([has_arrow, has_amp, has_pipe])
        if op_count > 1:
            raise ValueError(
                "Mixed operators in one expression are not allowed. "
                "Use only one of: '→' (CHAIN), '&' (AND), '|' (OR). "
                "Decompose into sub-concepts if needed.")

        if has_arrow:
            vtype = "CHAIN"
            parts = [s.strip() for s in expression.split("→")]
        elif has_pipe:
            vtype = "OR"
            parts = [s.strip() for s in expression.split("|")]
        elif has_amp:
            vtype = "AND"
            parts = [s.strip() for s in expression.split("&")]
        else:
            if not allow_single:
                raise ValueError(
                    "Expression must contain at least one operator: "
                    "'→' (CHAIN), '&' (AND), or '|' (OR).")
            vtype = "SINGLE"
            parts = [expression.strip()]

        if any(not p for p in parts):
            raise ValueError(
                "Invalid syntax: empty operand in expression. "
                "Each operator must separate two concepts.")
        if vtype != "SINGLE" and len(parts) < 2:
            raise ValueError(
                f"{vtype} expression requires at least 2 concepts.")

        ids = [self._resolve_id(n)[0] for n in parts]
        if vtype == "CHAIN":
            if any(a == b for a, b in zip(ids, ids[1:])):
                raise ValueError(
                    "A concept cannot immediately follow itself in a chain "
                    "(e.g. 'A → A' is invalid).")
        elif vtype != "SINGLE":
            if len(set(ids)) != len(ids):
                raise ValueError(
                    "A concept cannot appear more than once in an AND/OR "
                    "expression (duplicates are not allowed).")
        return vtype, ids

    def _next_short_code(self, concept_id: int) -> str:
        """为 concept 生成随机 short_code（4 位 hex，不复用已删除的码）。"""
        existing = {
            r["short_code"] for r in self.conn.execute(
                "SELECT short_code FROM variations WHERE concept_id=?",
                (concept_id,),
            ).fetchall()
        }
        for _ in range(100):
            sc = secrets.token_hex(2)
            if sc not in existing:
                return sc
        raise RuntimeError("short_code collision limit reached")
