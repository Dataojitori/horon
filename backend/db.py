"""
Horon — Inference Language for Intelligent Agents
DB operations (Harness v3 Executable Cognitive System)

Module layout:
  _db_common.py    — constants, validators, path setup, transactional decorator
  _db_plugins.py   — PluginMixin  (proxy factories, mutation hooks, cluster audit)
  _db_concepts.py  — ConceptMixin (create/delete concepts & tags)
  _db_mutations.py — MutationMixin (add / delete / set / update, role/lifespan/hooks/guards)
  _db_query.py     — QueryMixin   (search, read, expressions, relations)
  _db_reminders.py — ReminderMixin (reminder CRUD, sandbox eval, inbox)
  _db_compile.py   — CompileMixin (backward solver, compile)
  db.py  (this file) — HoronDB assembly + core infrastructure
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
import uuid
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
            self.conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
            self.conn.executemany(
                "INSERT INTO schema_migrations (version, applied_at) "
                "VALUES (?, ?)",
                [(path.stem, _now()) for path in self._migration_files()],
            )
        else:
            self._apply_migrations()
        for tag in SYSTEM_TAGS:
            self.conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,))
        self.conn.commit()

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

        # 禁用外键约束并开启 legacy_alter_table 以便安全执行表重建与重命名置换
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("PRAGMA legacy_alter_table = ON")
        try:
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
        finally:
            self.conn.execute("PRAGMA legacy_alter_table = OFF")
            self.conn.execute("PRAGMA foreign_keys = ON")

    # ── Session Lifecycle ────────────────────────────────────────────────────

    def init_session(self) -> str:
        """初始化/开启新会话：自动生成 ID、写入 current_session、熄灭 session/turn 传感器、清空时序链。"""
        new_id = f"sess_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        now = _now()

        # 1. 覆盖写入当前活跃会话
        self.conn.execute("DELETE FROM current_session")
        self.conn.execute(
            "INSERT INTO current_session (session_id, created_at) VALUES (?, ?)",
            (new_id, now),
        )

        # 2. 熄灭会话级与回合级临时传感器
        self.conn.execute(
            "UPDATE concepts SET is_active = 0, updated_at = ? "
            "WHERE lifespan IN ('session', 'turn')",
            (now,),
        )

        # 3. 清除时序链状态机
        self.conn.execute("DELETE FROM active_chain_instances")
        self.conn.commit()

        return new_id

    def _resolve_session_id(self) -> str:
        """获取当前有效会话 ID。直接读 current_session 表；若未初始化则自动生成。"""
        try:
            row = self.conn.execute(
                "SELECT session_id FROM current_session LIMIT 1"
            ).fetchone()
            if row and row["session_id"]:
                return row["session_id"]
        except sqlite3.OperationalError:
            pass
        return self.init_session()

    # ── Concept Resolution ───────────────────────────────────────────────────

    def _resolve_id(self, query) -> int:
        """将输入解析为 concept_id (int)。

        接受格式：
          "爱"   → cid (按 name 或 alias 查)
          42     → cid (按 concept ID 查)
          "42"   → cid (纯数字字符串优先按 ID 查，查不到再按 name/alias 查)
        """
        if query is None:
            raise ValueError("Concept query cannot be None.")

        # 1. 尝试直接以整型 ID 解析
        raw_int: int | None = None
        if isinstance(query, int):
            raw_int = query
        elif isinstance(query, str) and query.strip().isdigit():
            try:
                raw_int = int(query.strip())
            except (ValueError, TypeError):
                pass

        if raw_int is not None:
            row = self.conn.execute(
                "SELECT id FROM concepts WHERE id = ?", (raw_int,)
            ).fetchone()
            if row:
                return row["id"]

        # 2. 尝试以名字或别名解析（主名在创建与修改时已严格同步至 aliases 表）
        if isinstance(query, str):
            query_str = query.strip()
            row = self.conn.execute(
                "SELECT concept_id FROM aliases WHERE alias = ?", (query_str,)
            ).fetchone()
            if row:
                return row["concept_id"]

        raise ValueError(f"Concept not found: {query}")

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
                   sub_action: str | None = None,
                   success: bool = True,
                   **kwargs) -> None:
        """INSERT into cli_audit_log. 审计写失败不打断主操作。"""
        try:
            sess = self._resolve_session_id()
            self.conn.execute(
                "INSERT INTO cli_audit_log"
                " (session_id, timestamp, command, concept_id, concept_name,"
                "  sub_action, success)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sess, _now(), command, concept_id, concept_name,
                 sub_action, int(success)),
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

    def _parse_activation_rule(self, activation_rule: str, allow_single: bool = False) -> tuple[str, list[int]]:
        """拆分激活规则，解析为 (activation_type, member_concept_ids)。

        严格不混用：一个激活规则只能包含一种运算符。
        返回 (activation_type, ordered_member_ids)。

        CHAIN (A → B → C):  ('CHAIN', [id_A, id_B, id_C])
        AND   (A & B & C):  ('AND',   [id_A, id_B, id_C])
        OR    (A | B | C):  ('OR',    [id_A, id_B, id_C])
        Single (A):         ('SINGLE',[id_A])
        """
        has_arrow = "→" in activation_rule
        has_amp = "&" in activation_rule
        has_pipe = "|" in activation_rule

        op_count = sum([has_arrow, has_amp, has_pipe])
        if op_count > 1:
            raise ValueError(
                "Mixed operators in one activation rule are not allowed. "
                "Use only one of: '→' (CHAIN), '&' (AND), '|' (OR). "
                "Decompose into sub-concepts if needed.")

        if has_arrow:
            vtype = "CHAIN"
            parts = [s.strip() for s in activation_rule.split("→")]
        elif has_pipe:
            vtype = "OR"
            parts = [s.strip() for s in activation_rule.split("|")]
        elif has_amp:
            vtype = "AND"
            parts = [s.strip() for s in activation_rule.split("&")]
        else:
            if not allow_single:
                raise ValueError(
                    "Activation rule must contain at least one operator: "
                    "'→' (CHAIN), '&' (AND), or '|' (OR).")
            vtype = "SINGLE"
            parts = [activation_rule.strip()]

        if any(not p for p in parts):
            raise ValueError(
                "Invalid syntax: empty operand in activation rule. "
                "Each operator must separate two concepts.")
        if vtype != "SINGLE" and len(parts) < 2:
            raise ValueError(
                f"{vtype} activation rule requires at least 2 concepts.")

        ids = [self._resolve_id(n) for n in parts]
        if vtype == "CHAIN":
            if any(a == b for a, b in zip(ids, ids[1:])):
                raise ValueError(
                    "A concept cannot immediately follow itself in a chain "
                    "(e.g. 'A → A' is invalid).")
        elif vtype != "SINGLE":
            if len(set(ids)) != len(ids):
                raise ValueError(
                    "A concept cannot appear more than once in an AND/OR "
                    "activation rule (duplicates are not allowed).")
        return vtype, ids

    def audit_db_integrity(self) -> str:
        """Audit system-level database integrity (e.g., missing embeddings) and auto-patch them."""
        lines = ["## Database Integrity Audit"]
        
        # Check concepts with disclosure for missing embeddings
        rows = self.conn.execute(
            """
            SELECT c.id, c.disclosure
            FROM concepts c
            LEFT JOIN concept_embeddings ce ON c.id = ce.concept_id
            WHERE c.disclosure IS NOT NULL AND c.disclosure != '' AND ce.embedding IS NULL
            """
        ).fetchall()
        
        if not rows:
            lines.append("  - Embeddings: All concept disclosures have embeddings ✓")
        else:
            lines.append(f"  - Embeddings: Found {len(rows)} concepts missing embeddings. Syncing...")
            from .embedding import sync_single_embedding
            success_count = 0
            failed_count = 0
            for r in rows:
                try:
                    ok = sync_single_embedding(self, r["id"], r["disclosure"])
                    if ok:
                        success_count += 1
                    else:
                        failed_count += 1
                        lines.append(f"    - Failed to sync concept {r['id']}: embedding generation or write failed")
                except Exception as e:
                    failed_count += 1
                    lines.append(f"    - Failed to sync concept {r['id']}: {e}")
            if failed_count > 0:
                lines.append(f"    - Synced {success_count}/{len(rows)} embeddings (failed: {failed_count}).")
            else:
                lines.append(f"    - Synced {success_count}/{len(rows)} embeddings.")
            
        return "\n".join(lines)
