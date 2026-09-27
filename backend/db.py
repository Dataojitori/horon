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

import sqlite3
import sys
import time
from pathlib import Path

from ._db_common import (
    _now, _validate_condition_ast, SYSTEM_TAGS, transactional,  # noqa: F401  re-exported
    _SCHEMA_PATH, _MIGRATIONS_DIR, _DB_PATH, OFFLINE_DEV_SESSION_ID,
)
from ._db_plugins import PluginMixin
from ._db_concepts import ConceptMixin
from ._db_mutations import MutationMixin
from ._db_query import QueryMixin
from ._db_reminders import ReminderMixin
from ._db_compile import CompileMixin
from ._db_snapshots import SnapshotMixin
from .evaluator import GraphEvaluator
from .models import EvaluationResult


class HoronDB(
    PluginMixin,
    ConceptMixin,
    MutationMixin,
    QueryMixin,
    ReminderMixin,
    CompileMixin,
    SnapshotMixin,
):
    def __init__(self, *, session_id: str, db_path: str | Path | None = None, check_same_thread: bool = True,
                 snapshot_mode: bool = False):
        """session_id：这个连接代表哪个会话，创建时定下，之后不变。
        读激活状态、求值、通知队列、审计日志、插件看到的 is_active 全部按它算。
        会话在进程启动时就已确定：CLI 取宿主环境变量（没有则 devonly），钩子取 payload，
        网页后台按每个请求带的会话各开一个连接。"""
        if not session_id or not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("HoronDB 要求非空的 session_id。")
        self.snapshot_mode = snapshot_mode
        self.session_id = session_id.strip()
        target_path = Path(db_path) if db_path else _DB_PATH
        is_new = not target_path.exists() or target_path.stat().st_size == 0
        self._plugin_cache: dict[str, tuple[dict | None, float | None]] = {}
        self._txn_plugin_snapshot: dict[str, dict | None] = {}
        self.conn = sqlite3.connect(
            str(target_path), check_same_thread=check_same_thread)
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
        self.cleanup_expired_sessions(days=7)
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

    # ── Session & Evaluation Lifecycle ───────────────────────────────────────

    def cleanup_expired_sessions(self, days: int = 7) -> int:
        """删除超过指定天数未活跃的会话及其关联状态。

        排除 devonly 离线开发会话。
        """
        # 和 _now() 同一种时间（本地时间、同一格式），字符串比较才成立
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - days * 86400))
        rows = self.conn.execute(
            "SELECT session_id FROM sessions "
            "WHERE session_id != ? AND last_active_at < ?",
            (OFFLINE_DEV_SESSION_ID, cutoff),
        ).fetchall()
        if not rows:
            return 0
        expired_ids = [r["session_id"] for r in rows]
        placeholders = ",".join("?" for _ in expired_ids)

        self.conn.execute(f"DELETE FROM session_active_sensors WHERE session_id IN ({placeholders})", expired_ids)
        self.conn.execute(f"DELETE FROM active_chain_instances WHERE session_id IN ({placeholders})", expired_ids)
        self.conn.execute(f"DELETE FROM pending_notifications WHERE session_id IN ({placeholders})", expired_ids)
        self.conn.execute(f"DELETE FROM sessions WHERE session_id IN ({placeholders})", expired_ids)
        self.conn.commit()
        return len(expired_ids)

    def init_session(self, adapter: str | None = None) -> None:
        """登记本连接的会话，清空它的临时状态（turn/session 传感器、CHAIN 进度、待发通知），再求值一遍。
        adapter：来自哪个宿主，只供网页显示。"""
        self.touch_session(adapter)
        self.session_reset()

    def touch_session(self, adapter: str | None = None) -> None:
        """登记本连接的会话；已登记的只更新最后活跃时间（过期清理按这个时间算）。
        adapter：来自哪个宿主，只供网页显示；传了就记下，没传不覆盖已有值。"""
        now = _now()
        self.conn.execute(
            "INSERT INTO sessions (session_id, created_at, last_active_at, adapter) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET last_active_at = excluded.last_active_at, "
            "adapter = COALESCE(excluded.adapter, adapter)",
            (self.session_id, now, now, adapter),
        )
        self.conn.commit()

    def _evaluator(self) -> GraphEvaluator:
        """求值器工厂：按本连接的会话读写激活状态。其他会话在自己下次 sync_session 时重算跟上。"""
        return GraphEvaluator(self.conn, session_id=self.session_id)

    def session_reset(self) -> EvaluationResult:
        """重置本连接的会话：清空它的 CHAIN 进度、待发通知与已激活的 turn/session 传感器。"""
        res = self._evaluator().reset_session()
        self.conn.commit()
        return res

    def turn_end(self) -> EvaluationResult:
        """单回合结束：熄灭本连接会话里的 turn 传感器。"""
        res = self._evaluator().end_turn()
        self.conn.commit()
        return res

    def evaluate(
        self,
        activated_sensors: list[int] | None = None,
        deactivated_sensors: list[int] | None = None,
    ) -> EvaluationResult:
        """在本连接的会话下执行单趟 Kahn 拓扑排序求值与时序/抑制计算。"""
        res = self._evaluator().evaluate(
            activated_sensors=activated_sensors,
            deactivated_sensors=deactivated_sensors,
        )
        self.conn.commit()
        return res

    def push_pending_notifications(self, messages: list[str]) -> None:
        """向本连接会话的待消费通知队列追加一条或多条消息。"""
        if not messages:
            return
        now = _now()
        rows = [(self.session_id, msg.strip(), now) for msg in messages if msg and msg.strip()]
        if rows:
            self.conn.executemany(
                "INSERT INTO pending_notifications (session_id, message, created_at) VALUES (?, ?, ?)",
                rows,
            )
            self.conn.commit()

    def pop_pending_notifications(self) -> list[str]:
        """提取并清空本连接会话在队列中积压的所有未读通知（FIFO 按入队顺序）。"""
        rows = self.conn.execute(
            "SELECT id, message FROM pending_notifications WHERE session_id = ? ORDER BY id ASC",
            (self.session_id,),
        ).fetchall()
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" for _ in ids)
        self.conn.execute(
            f"DELETE FROM pending_notifications WHERE id IN ({placeholders})",
            ids,
        )
        self.conn.commit()
        return [r["message"] for r in rows]


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
            self.conn.execute(
                "INSERT INTO cli_audit_log"
                " (session_id, timestamp, command, concept_id, concept_name,"
                "  sub_action, success)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self.session_id, _now(), command, concept_id, concept_name,
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

    _CIRCUIT_MEMBER_ROLES = ("sensor", "logic")

    def _check_circuit_members(self, member_ids: list[int]) -> None:
        """校验激活规则的成员角色。

        输入：已解析的成员概念 ID 列表。
        行为：逐个查角色；只有 sensor / logic 有电位可向下游传递
        （求值器把 plain 电位恒置 0，guard 是工具放行出口），
        其余角色作为成员会让规则永远无法满足。
        输出：无；遇到第一个不合规成员即抛 ValueError，说明其名称、ID 与角色。
        """
        for mid in member_ids:
            m_row = self.conn.execute(
                "SELECT name, role FROM concepts WHERE id = ?", (mid,)
            ).fetchone()
            if m_row and m_row["role"] not in self._CIRCUIT_MEMBER_ROLES:
                raise ValueError(
                    f"激活规则成员只能是 sensor 或 logic 角色（具备电位传递能力）。"
                    f"概念 '{m_row['name']}' (id={mid}) 的角色为 '{m_row['role']}'，无法参与电路激活。"
                )

    def _check_role_change_keeps_members_valid(self, cid: int, new_role: str) -> None:
        """角色切换前，校验该概念若仍被上游规则引用，新角色是否还能当成员。

        输入：待切换概念 ID、目标角色。
        行为：新角色属于 sensor / logic 时直接通过；否则查 compose_members
        中以它为成员的上游节点。
        输出：无；若存在上游引用则抛 ValueError，列出上游节点，
        提示先从这些规则中移除它再切换角色。
        """
        if new_role in self._CIRCUIT_MEMBER_ROLES:
            return
        parents = self.conn.execute(
            "SELECT DISTINCT p.id, p.name FROM compose_members cm "
            "JOIN concepts p ON cm.parent_concept_id = p.id "
            "WHERE cm.member_concept_id = ? ORDER BY p.id",
            (cid,),
        ).fetchall()
        if parents:
            listing = ", ".join(f"'{p['name']}' (id={p['id']})" for p in parents)
            raise ValueError(
                f"该概念仍是以下节点激活规则的成员：{listing}。"
                f"切换为 '{new_role}' 后它将无法传递电位，请先从这些规则中移除它，再切换角色。"
            )

    def _parse_activation_rule(self, activation_rule: str) -> tuple[str, list[int]]:
        """拆分激活规则，解析为 (activation_type, member_concept_ids)。

        严格不混用：一个激活规则只能包含一种运算符。
        返回 (activation_type, ordered_member_ids)。

        CHAIN (A → B → C):  ('CHAIN', [id_A, id_B, id_C])
        AND   (A & B & C):  ('AND',   [id_A, id_B, id_C])
        OR    (A | B | C):  ('OR',    [id_A, id_B, id_C])
        Single (A):         ('AND',   [id_A])  # 单输入归一化为单成员 AND

        单成员规则对所有角色一律放行。原先这里有个 allow_single 开关，仅 guard 传 True，
        意在阻止 `logic = A` 这种无意义的转发中继。但它只扫规则字符串里有没有运算符，
        看不见抑制边——而 `logic = A UNLESS B` 等价于 A AND NOT B，是真正的二元逻辑，
        却被一并误判成套娃。结果是想表达「A 发生了而 B 没发生」的人被逼去借 guard 的壳，
        造出不管任何工具的空壳门禁，污染比套娃本身更隐蔽。
        真套娃（单成员且无 incoming inhibition）改由结构审计捕捉，不在写时拦。
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
            vtype = "AND"
            parts = [activation_rule.strip()]

        if any(not p for p in parts):
            raise ValueError(
                "Invalid syntax: empty operand in activation rule. "
                "Each operator must separate two concepts.")
        if op_count > 0 and len(parts) < 2:
            raise ValueError(
                f"{vtype} activation rule requires at least 2 concepts.")

        ids = [self._resolve_id(n) for n in parts]
        if vtype in ("AND", "OR") and len(set(ids)) != len(ids):
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

        # Check circuit composition contract (members must be sensor or logic)
        invalid_cm = self.conn.execute(
            """
            SELECT cm.parent_concept_id, p.name AS parent_name, p.role AS parent_role,
                   cm.member_concept_id, m.name AS member_name, m.role AS member_role
            FROM compose_members cm
            JOIN concepts p ON cm.parent_concept_id = p.id
            JOIN concepts m ON cm.member_concept_id = m.id
            WHERE m.role NOT IN ('sensor', 'logic')
            ORDER BY cm.parent_concept_id
            """
        ).fetchall()
        if not invalid_cm:
            lines.append("  - Circuit Contracts: All composition members are circuit nodes (sensor/logic) ✓")
        else:
            lines.append(f"  - Circuit Contracts: Found {len(invalid_cm)} composition violations (members with role not in sensor/logic):")
            for r in invalid_cm:
                lines.append(f"    - [{r['parent_concept_id']}] '{r['parent_name']}' ({r['parent_role']}) -> Member [{r['member_concept_id']}] '{r['member_name']}' ({r['member_role']})")

            
        return "\n".join(lines)
