"""
Horon — Inference Language for Intelligent Agents
DB operations (Concept → Variation 分層結構)

Concept:   概念的对外身份（名字 + disclosure），组合的参与单位。
Variation: 同一概念的不同解释（concept_id + short_code），
           每个 variation 有独立的 status / evidence / unless / compose_members。
compose_members 的 member 引用 concept_id（hub），不是具体 variation。
"""
from __future__ import annotations

import heapq
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import NamedTuple

from dotenv import load_dotenv
from .models import (
    Concept, VariationDetail, ComposeMemberDetail,
    RelationRow, OutboundRelation, ReadResult,
)


class _PathCost(NamedTuple):
    hypothesis_count: int | float
    total_jumps: int | float


class _RouteStep(NamedTuple):
    """
    记录图遍历中“这一步是怎么走的”完整上下文，用于最终的路径重建。
    注意：在影分身机制中，作为字典键的当前到达节点（如被激活的关系本身），
    和这一步在图物理结构上的落脚点（destination_id）可能并不相同。
    """
    from_states: tuple[tuple[int, bool], ...] # 这一步是从哪些 (concept_id, used_req) 状态出发的
    destination_id: int               # 这一步在图结构上实际走到的目标节点
    edge_concept_id: int              # 促成这一步跳转的关系（边）的 concept_id
    edge_short_code: str              # 该关系对应的 variation short_code
    status: str                       # 关系的确认状态 (confirmed / hypothesis)


_CONDITION_RE = re.compile(r'\$\{\s*(.+?→.+?)\s+(confirmed|negated)\s*\}')
_CONTROL_CHAR_RE = re.compile(r'[\x00-\x1f\x7f]')
_MAX_NAME_LEN = 200
_FORBIDDEN_CHARS = {'→', '&', ':'}


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


_PROJECT_DIR = Path(__file__).parent.parent
_SCHEMA_PATH = _PROJECT_DIR / "backend" / "schema.sql"

load_dotenv(_PROJECT_DIR / ".env")

if "HORON_DB" not in os.environ:
    raise RuntimeError("HORON_DB not set. Check .env file.")
_DB_PATH = _PROJECT_DIR / os.environ["HORON_DB"]


def init_db():
    """建表。只需跑一次。"""
    conn = sqlite3.connect(str(_DB_PATH))
    conn.executescript(_SCHEMA_PATH.read_text())
    conn.close()


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")



from functools import wraps

def transactional(method):
    '''确保方法执行在显式事务边界内。失败自动回滚，成功自动提交。'''
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.conn:
            return method(self, *args, **kwargs)
    return wrapper

class HoronDB:
    def __init__(self, *, check_same_thread: bool = True):
        is_new = not _DB_PATH.exists() or _DB_PATH.stat().st_size == 0
        self.conn = sqlite3.connect(
            str(_DB_PATH), check_same_thread=check_same_thread)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if is_new:
            self.conn.executescript(_SCHEMA_PATH.read_text())

    def close(self):
        self.conn.close()

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

        # ── resolve concept_id ──
        # aliases 表是名字解析的唯一权威来源（主名在创建时同步注册）。
        # int 输入或纯数字字符串按 concept ID 直查 concepts 表。
        cid: int | None = None

        # 尝试当 concept ID（int 或纯数字字符串）
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

        # 非数字，或数字没命中 → 查 aliases
        if cid is None and isinstance(query, str):
            row = self.conn.execute(
                "SELECT concept_id FROM aliases WHERE alias = ?", (query,)
            ).fetchone()
            if row:
                cid = row["concept_id"]

        if cid is None:
            raise ValueError(f"Concept not found: {query}")

        # ── resolve short_codes ──
        if sc is not None:
            row = self.conn.execute(
                "SELECT 1 FROM variations "
                "WHERE concept_id=? AND short_code=?",
                (cid, sc),
            ).fetchone()
            if not row:
                raise ValueError(
                    f"No variation '{sc}' in concept {cid}. "
                    f"Use 'name:short_code' format.")
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
            raise ValueError(
                "Concept has no variations.")
        if len(scs) != 1:
            raise ValueError(
                "Multiple variations; use 'concept:short_code' "
                "to specify.")
        return cid, scs[0]

    def _resolve_concept_name(self, concept_id: int) -> str:
        """concept ID → 显示名。"""
        row = self.conn.execute(
            "SELECT name FROM concepts WHERE id=?", (concept_id,)
        ).fetchone()
        if row:
            return row["name"]
        raise ValueError(f"No concept with id {concept_id}")

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

    # ── Concept lifecycle ────────────────────────────────────────────────────

    @transactional
    def create_concept(self, name: str,
                       disclosure: str | None = None) -> str:
        """创建概念 concept + 默认 variation + 同名 alias。"""
        name = _validate_name(name)
        self._check_name_available(name)
        now = _now()
        cursor = self.conn.execute(
            "INSERT INTO concepts (name, disclosure, created_at, updated_at) "
            "VALUES (?,?,?,?)",
            (name, disclosure, now, now),
        )
        concept_id = cursor.lastrowid
        sc = secrets.token_hex(2)
        self.conn.execute(
            "INSERT INTO variations "
            "(concept_id, short_code, created_at, updated_at) "
            "VALUES (?,?,?,?)",
            (concept_id, sc, now, now),
        )
        self.conn.execute(
            "INSERT INTO aliases (alias, concept_id) VALUES (?,?)",
            (name, concept_id),
        )
        return f"Success. Created concept '{name}' ('{name}', id={concept_id})."

    def search_concepts(self, query) -> list[Concept]:
        """按 alias、disclosure 或 evidence 模糊搜索 concept。"""
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        rows = self.conn.execute(
            "SELECT DISTINCT c.* FROM concepts c "
            "LEFT JOIN aliases a ON c.id = a.concept_id "
            "LEFT JOIN variations v ON c.id = v.concept_id "
            "WHERE a.alias LIKE ? ESCAPE '\\' "
            "OR c.disclosure LIKE ? ESCAPE '\\' "
            "OR v.evidence LIKE ? ESCAPE '\\'",
            (like, like, like),
        ).fetchall()
        return [Concept(**dict(row)) for row in rows]

    def get_all_concepts(self) -> list[Concept]:
        """获取所有 concept。"""
        rows = self.conn.execute("SELECT * FROM concepts ORDER BY id").fetchall()
        return [Concept(**dict(row)) for row in rows]

    def get_all_concepts_overview(self) -> list[dict]:
        """获取所有概念及变体表达式的概览（供 CLI 和前端展示用，无 N+1 问题）。"""
        concepts = self.get_all_concepts()
        
        var_rows = self.conn.execute(
            "SELECT concept_id, short_code, status FROM variations "
            "ORDER BY concept_id, short_code"
        ).fetchall()
        vars_by_cid = {}
        for r in var_rows:
            vars_by_cid.setdefault(r["concept_id"], []).append(r)
            
        mem_rows = self.conn.execute(
            "SELECT cm.concept_id, cm.short_code, cm.position, c.name "
            "FROM compose_members cm "
            "JOIN concepts c ON cm.member_concept_id = c.id "
            "ORDER BY cm.position, cm.member_concept_id"
        ).fetchall()
        
        mems_by_var = {}
        for r in mem_rows:
            key = (r["concept_id"], r["short_code"])
            if key not in mems_by_var:
                mems_by_var[key] = {1: [], 2: []}
            mems_by_var[key][r["position"]].append(r["name"])
            
        result = []
        for c in concepts:
            c_dict = {
                "id": c.id,
                "name": c.name,
                "disclosure": c.disclosure,
                "variations": []
            }
            for v in vars_by_cid.get(c.id, []):
                key = (c.id, v["short_code"])
                expr = None
                if key in mems_by_var:
                    m = mems_by_var[key]
                    if m[2]:
                        expr = f"{m[1][0]} → {m[2][0]}"
                    else:
                        expr = " & ".join(m[1])
                        
                c_dict["variations"].append({
                    "short_code": v["short_code"],
                    "status": v["status"] or "hypothesis",
                    "expression": expr
                })
            result.append(c_dict)
            
        return result

    def _concept_label(self, input_query, cid: int) -> str:
        """统一的概念标识格式：'input' ('display_name', id=N)。"""
        name = self._resolve_concept_name(cid)
        return f"'{input_query}' ('{name}', id={cid})"

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _parse_expression(self, expression: str) -> tuple[list[int], list[int]]:
        """拆分表达式，解析为 concept_id，并校验合法性。

        有序 (A → B): 返回 ([id_A], [id_B])
        无序 (A & B & C): 返回 ([id_A, id_B, id_C], [])
        """
        arrow_count = expression.count("→")
        amp_count = expression.count("&")

        if arrow_count > 1:
            raise ValueError("Expression can contain at most one →.")
        if arrow_count and amp_count:
            raise ValueError(
                "Cannot mix → and & in one expression. "
                "Create intermediate concepts for complex compositions."
            )

        if arrow_count:
            left, right = expression.split("→", 1)
            left, right = left.strip(), right.strip()
            if not left or not right:
                raise ValueError("→ requires a concept on each side.")
            names_at_1 = [left]
            names_at_2 = [right]
        else:
            parts = [s.strip() for s in expression.split("&")]
            if any(not p for p in parts):
                raise ValueError(
                    "Invalid syntax: found consecutive '&' (like '&&') "
                    "or a leading/trailing '&'. Each '&' must separate two concepts."
                )
            if len(parts) < 2:
                raise ValueError("& requires at least 2 concepts.")
            names_at_1 = parts
            names_at_2 = []

        ids_at_1 = [self._resolve_id(n)[0] for n in names_at_1]
        ids_at_2 = [self._resolve_id(n)[0] for n in names_at_2]

        members = ids_at_1 + ids_at_2

        # 成员去重：同一概念在一个表达式里出现多次没有语义，且
        # compose_members 主键 (concept_id, short_code, member_concept_id)
        # 不含 position，重复（含 A → A 这类自环）会直接撞主键。
        if len(set(members)) != len(members):
            raise ValueError("A concept cannot appear more than once in an expression.")

        return ids_at_1, ids_at_2

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

    # ── Add ──────────────────────────────────────────────────────────────────

    def add(self, concept, kind: str, value: str) -> str:
        """给概念添加 name（别名）或 variation（变种）。"""
        if kind == "name":
            return self._add_name(concept, value)
        elif kind == "variation":
            return self._add_variation(concept, value)
        raise ValueError(
            f"Unknown type: '{kind}'. Use 'name' or 'variation'.")

    @transactional
    def _add_name(self, concept, name: str) -> str:
        """给 concept 加一个 alias。"""
        name = _validate_name(name)
        cid, _ = self._resolve_id(concept)
        label = self._concept_label(concept, cid)
        existing = self.conn.execute(
            "SELECT concept_id FROM aliases WHERE alias=?", (name,)
        ).fetchone()
        if existing:
            if existing["concept_id"] == cid:
                return f"Success. '{name}' is already an alias for {label}."
            raise ValueError(
                f"Name '{name}' already resolves to concept "
                f"{existing['concept_id']}.")
        self.conn.execute(
            "INSERT INTO aliases (alias, concept_id) VALUES (?,?)",
            (name, cid),
        )
        return f"Success. Added alias '{name}' to {label}."

    @transactional
    def _add_variation(self, concept, expression: str) -> str:
        """给 concept 新增变种，必须带 expression。"""
        if not expression or not expression.strip():
            raise ValueError("Expression cannot be empty.")
        cid, _ = self._resolve_id(concept)
        label = self._concept_label(concept, cid)

        ids_at_1, ids_at_2 = self._parse_expression(expression)

        members = ids_at_1 + ids_at_2
        # 自引用 → 不允许（成员不能包含自己）
        if cid in members:
            raise ValueError(
                "A concept cannot appear in its own expression.")

        # 重复组合检测
        existing = self._find_composition_variation(expression)
        if existing is not None:
            exist_cid, exist_sc, _ = existing
            exist_name = self._resolve_concept_name(exist_cid)
            raise ValueError(
                f"Variation '{exist_name}:{exist_sc}' already has "
                f"the same composition: {expression}")

        sc = self._next_short_code(cid)
        now = _now()
        self.conn.execute(
            "INSERT INTO variations "
            "(concept_id, short_code, created_at, updated_at) "
            "VALUES (?,?,?,?)",
            (cid, sc, now, now),
        )
        for member_cid in ids_at_1:
            self.conn.execute(
                "INSERT INTO compose_members "
                "(concept_id, short_code, member_concept_id, position) "
                "VALUES (?,?,?,1)",
                (cid, sc, member_cid),
            )
        for member_cid in ids_at_2:
            self.conn.execute(
                "INSERT INTO compose_members "
                "(concept_id, short_code, member_concept_id, position) "
                "VALUES (?,?,?,2)",
                (cid, sc, member_cid),
            )
        return f"Success. Added variation {sc} to {label}."

    # ── Delete ───────────────────────────────────────────────────────────────

    def delete(self, target, kind: str | None = None,
               value: str | None = None) -> str:
        """删除操作。

        kind 省略:   删除 variation（target 为概念名或 概念:sc）。
        name:       删除别名（value=要删的别名，必填）。
        expression: 清除组合回原子态（target 为概念名或 概念:sc）。
        """
        if kind is None:
            return self._delete_variation(target)
        elif kind == "name":
            if value is None:
                raise ValueError("Specify which name to delete.")
            return self._delete_name(target, value)
        elif kind == "expression":
            if value is not None:
                raise ValueError(
                    f"delete expression takes no extra argument, "
                    f"but got: '{value}'.")
            return self._delete_expression(target)
        raise ValueError(
            f"Unknown type: '{kind}'. "
            f"Use 'name' or 'expression'.")

    @transactional
    def _delete_name(self, concept, name: str) -> str:
        """删一个 alias。拒删当前显示名。"""
        cid, _ = self._resolve_id(concept)
        label = self._concept_label(concept, cid)
        concept_name = self._resolve_concept_name(cid)
        row = self.conn.execute(
            "SELECT concept_id FROM aliases WHERE alias=?", (name,)
        ).fetchone()
        if not row:
            raise ValueError(f"No such alias: '{name}'.")
        if row["concept_id"] != cid:
            raise ValueError(
                f"Alias '{name}' belongs to concept "
                f"{row['concept_id']}, not {label}.")
        if concept_name == name:
            raise ValueError(
                f"'{name}' is the display name. "
                f"Use 'set name' to change it first.")
        self.conn.execute(
            "DELETE FROM aliases WHERE alias=?", (name,))
        return f"Success. Removed alias '{name}' from {label}."

    @transactional
    def _delete_variation(self, node) -> str:
        """删除 variation。最后一个 → concept 也删。

        删除最后一个 variation 会连带删除 concept 本体。
        删除前检查是否有其他 concept 的 variation 引用该 concept
        作为 compose_member；有则拒绝，防止产生残缺的死组合。
        """
        cid, sc = self._resolve_single_variation(node)
        label = self._concept_label(node, cid)

        # 如果这是最后一个 variation，删它 = 删 concept 本体。
        # 先检查外部引用：别的 concept 的 variation 是否把该 concept
        # 当作 compose_member。有则拒绝，因为 CASCADE 会静默删掉
        # 那些 compose_members 行，把别人的 variation 变成死代码。
        var_count = self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM variations "
            "WHERE concept_id=?",
            (cid,),
        ).fetchone()["cnt"]

        if var_count == 1:
            refs = self.conn.execute(
                "SELECT DISTINCT cm.concept_id, cm.short_code, "
                "  c.name "
                "FROM compose_members cm "
                "JOIN concepts c ON cm.concept_id = c.id "
                "WHERE cm.member_concept_id = ? "
                "AND cm.concept_id != ?",
                (cid, cid),
            ).fetchall()
            if refs:
                ref_parts = []
                for r in refs:
                    expr = self._get_expression(
                        r["concept_id"], r["short_code"])
                    tag = f"  - '{r['name']}:{r['short_code']}'" \
                          f" (id={r['concept_id']})"
                    if expr:
                        tag += f"  [{expr}]"
                    ref_parts.append(tag)
                detail = "\n".join(ref_parts)
                raise ValueError(
                    f"Cannot delete {label}: it is still referenced "
                    f"as a compose member by:\n{detail}\n"
                    f"Use read_concept to review them before deciding "
                    f"how to proceed.")

        self.conn.execute(
            "DELETE FROM variations "
            "WHERE concept_id=? AND short_code=?",
            (cid, sc),
        )
        remaining = self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM variations WHERE concept_id=?",
            (cid,),
        ).fetchone()

        if remaining["cnt"] == 0:
            self.conn.execute(
                "DELETE FROM concepts WHERE id=?", (cid,))
            return (f"Success. Deleted variation {sc} from {label}. "
                    f"No variations remaining; concept deleted.")

        return (f"Success. Deleted variation {sc} from {label}. "
                f"{remaining['cnt']} variation(s) remaining.")

    @transactional
    def _delete_expression(self, node) -> str:
        """清除 variation 的组合，回到原子态。status 一并清除。"""
        cid, sc = self._resolve_single_variation(node)
        label = self._concept_label(node, cid)

        if not self._get_expression(cid, sc):
            raise ValueError(
                f"Variation {sc} of {label} "
                f"has no expression to delete.")

        # 约束：每个 concept 最多一个原子变种
        existing_atomic = self.conn.execute(
            "SELECT v.short_code FROM variations v "
            "WHERE v.concept_id = ? AND v.short_code != ? "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM compose_members cm "
            "  WHERE cm.concept_id = v.concept_id "
            "  AND cm.short_code = v.short_code"
            ") LIMIT 1",
            (cid, sc),
        ).fetchone()
        if existing_atomic:
            raise ValueError(
                f"Cannot clear expression: variation "
                f"{existing_atomic['short_code']} already has no "
                f"expression. Only one atomic variation allowed "
                f"per concept, because two would be logically "
                f"identical.")

        self.conn.execute(
            "DELETE FROM compose_members "
            "WHERE concept_id=? AND short_code=?",
            (cid, sc),
        )
        self.conn.execute(
            "UPDATE variations SET status=NULL, updated_at=? "
            "WHERE concept_id=? AND short_code=?",
            (_now(), cid, sc),
        )
        return (f"Success. Cleared expression and status for "
                f"variation {sc} of {label}.")

    # ── Set ──────────────────────────────────────────────────────────────────

    def set(self, target, prop: str, value: str) -> str:
        """设置属性：disclosure、status、name（rename）、expression。"""
        if prop == "disclosure":
            return self._set_disclosure(target, value)
        elif prop == "status":
            return self._set_status(target, value)
        elif prop == "name":
            return self._set_name(target, value)
        elif prop == "expression":
            return self._set_expression(target, value)
        raise ValueError(
            f"Unknown property: '{prop}'. "
            f"Use 'disclosure', 'status', 'name', or 'expression'.")

    @transactional
    def _set_disclosure(self, concept, text: str) -> str:
        cid, _ = self._resolve_id(concept)
        label = self._concept_label(concept, cid)
        text = text.strip()
        if not text:
            raise ValueError("Disclosure cannot be empty.")
        self.conn.execute(
            "UPDATE concepts SET disclosure=?, updated_at=? WHERE id=?",
            (text, _now(), cid),
        )
        return f"Success. Disclosure for {label} set to: {text}"

    @transactional
    def _set_status(self, node, value: str) -> str:
        valid = ("hypothesis", "confirmed", "negated")
        if value not in valid:
            raise ValueError(
                f"Invalid status: '{value}'. "
                f"Must be one of: {', '.join(valid)}.")
        cid, sc = self._resolve_single_variation(node)
        label = self._concept_label(node, cid)
        if not self._get_expression(cid, sc):
            raise ValueError(
                "Cannot set status: variation has no expression.")
        self.conn.execute(
            "UPDATE variations SET status=?, updated_at=? "
            "WHERE concept_id=? AND short_code=?",
            (value, _now(), cid, sc),
        )
        return f"Success. Status of {label} variation {sc} set to: {value}"

    @transactional
    def _set_name(self, concept, new_name: str) -> str:
        """改显示名。旧显示名降级为 alias，保留在名字集合里。"""
        new_name = _validate_name(new_name)
        cid, _ = self._resolve_id(concept)
        label = self._concept_label(concept, cid)
        self._check_name_available(new_name, exclude_concept_id=cid)
        self.conn.execute(
            "UPDATE concepts SET name=?, updated_at=? WHERE id=?",
            (new_name, _now(), cid),
        )
        existing = self.conn.execute(
            "SELECT concept_id FROM aliases WHERE alias=?", (new_name,)
        ).fetchone()
        if not existing:
            self.conn.execute(
                "INSERT INTO aliases (alias, concept_id) VALUES (?,?)",
                (new_name, cid),
            )
        return f"Success. Renamed {label} to '{new_name}'."

    @transactional
    def _set_expression(self, node, expression: str) -> str:
        """覆盖 variation 的组合。"""
        if not expression or not expression.strip():
            raise ValueError("Expression cannot be empty.")
        cid, sc = self._resolve_single_variation(node)
        label = self._concept_label(node, cid)

        ids_at_1, ids_at_2 = self._parse_expression(expression)

        members = ids_at_1 + ids_at_2
        # 自引用 → 不允许
        if cid in members:
            raise ValueError(
                "A concept cannot appear in its own expression.")

        # 重复组合检测（排除自身）
        existing = self._find_composition_variation(expression)
        if existing is not None:
            exist_cid, exist_sc, _ = existing
            if exist_cid != cid or exist_sc != sc:
                exist_name = self._resolve_concept_name(exist_cid)
                raise ValueError(
                    f"Variation '{exist_name}:{exist_sc}' already "
                    f"has the same composition: {expression}")

        # 清除旧组合，写入新组合
        self.conn.execute(
            "DELETE FROM compose_members "
            "WHERE concept_id=? AND short_code=?",
            (cid, sc),
        )
        for member_cid in ids_at_1:
            self.conn.execute(
                "INSERT INTO compose_members "
                "(concept_id, short_code, member_concept_id, position) "
                "VALUES (?,?,?,1)",
                (cid, sc, member_cid),
            )
        for member_cid in ids_at_2:
            self.conn.execute(
                "INSERT INTO compose_members "
                "(concept_id, short_code, member_concept_id, position) "
                "VALUES (?,?,?,2)",
                (cid, sc, member_cid),
            )
        self.conn.execute(
            "UPDATE variations SET status=NULL, updated_at=? "
            "WHERE concept_id=? AND short_code=?",
            (_now(), cid, sc),
        )
        return (f"Success. Expression of {label} variation {sc} "
                f"set to: {expression}. "
                f"Status was also reset to null.")

    # ── Update ───────────────────────────────────────────────────────────────

    def get_variation_field(self, node, field: str) -> tuple[int, str, str | None]:
        """获取 variation 指定字段的当前值，供 CLI 层 patch mode 使用。避免 CLI 重复解析。"""
        valid_fields = ("evidence", "unless")
        if field not in valid_fields:
            raise ValueError(
                f"Unknown field: '{field}'. "
                f"Use one of: {', '.join(valid_fields)}.")
        concept_id, sc = self._resolve_single_variation(node)
        row = self.conn.execute(
            f"SELECT {field} FROM variations WHERE concept_id=? AND short_code=?",
            (concept_id, sc),
        ).fetchone()
        return concept_id, sc, row[field]

    @transactional
    def update(self, node, field: str, value: str) -> str:
        """给 variation 写 evidence 或 unless（patch/append 由 CLI 层处理）。

        node: concept 名/ID，或 "concept:short_code"。
        sole variation 时自动定位。
        """
        valid_fields = ("evidence", "unless")
        if field not in valid_fields:
            raise ValueError(
                f"Unknown field: '{field}'. "
                f"Use one of: {', '.join(valid_fields)}.")
        concept_id, sc = self._resolve_single_variation(node)
        label = self._concept_label(node, concept_id)
        self.conn.execute(
            f"UPDATE variations SET {field}=?, updated_at=? "
            f"WHERE concept_id=? AND short_code=?",
            (value, _now(), concept_id, sc),
        )
        return f"Success. Updated {field} of variation {sc} of {label}."

    # ── Query ────────────────────────────────────────────────────────────────

    def _get_expression(self, concept_id: int,
                        short_code: str) -> str | None:
        """取 variation 的组合表达式字符串。无组合返回 None。"""
        rows = self.conn.execute(
            "SELECT cm.position, c.name "
            "FROM compose_members cm "
            "JOIN concepts c ON cm.member_concept_id = c.id "
            "WHERE cm.concept_id = ? AND cm.short_code = ? "
            "ORDER BY cm.position, cm.member_concept_id",
            (concept_id, short_code),
        ).fetchall()
        if not rows:
            return None
        pos1 = [r["name"] for r in rows if r["position"] == 1]
        pos2 = [r["name"] for r in rows if r["position"] == 2]
        if pos2:
            return f"{pos1[0]} → {pos2[0]}"
        return " & ".join(pos1)

    def _find_composition_variation(
        self, expression: str,
    ) -> tuple[int, str, str | None] | None:
        """找到持有某个组合的 variation → (concept_id, short_code, status)。"""
        try:
            ids_at_1, ids_at_2 = self._parse_expression(expression)
        except ValueError:
            return None

        if ids_at_2:
            # →: position 1 和 position 2 各一个
            row = self.conn.execute(
                "SELECT cm1.concept_id, cm1.short_code, v.status "
                "FROM compose_members cm1 "
                "JOIN compose_members cm2 "
                "  ON cm1.concept_id = cm2.concept_id "
                "  AND cm1.short_code = cm2.short_code "
                "JOIN variations v "
                "  ON v.concept_id = cm1.concept_id "
                "  AND v.short_code = cm1.short_code "
                "WHERE cm1.member_concept_id=? AND cm1.position=1 "
                "AND cm2.member_concept_id=? AND cm2.position=2",
                (ids_at_1[0], ids_at_2[0]),
            ).fetchone()
        else:
            # &: 全在 position 1，精确匹配成员集合
            # 排除带 position 2 的有序关系
            n = len(ids_at_1)
            placeholders = ",".join("?" * n)
            row = self.conn.execute(
                f"SELECT cm.concept_id, cm.short_code, v.status "
                f"FROM compose_members cm "
                f"JOIN variations v "
                f"  ON v.concept_id = cm.concept_id "
                f"  AND v.short_code = cm.short_code "
                f"WHERE cm.position=1 "
                f"AND cm.member_concept_id IN ({placeholders}) "
                f"AND NOT EXISTS ("
                f"  SELECT 1 FROM compose_members cm_chk "
                f"  WHERE cm_chk.concept_id = cm.concept_id "
                f"  AND cm_chk.short_code = cm.short_code "
                f"  AND cm_chk.position = 2"
                f") "
                f"GROUP BY cm.concept_id, cm.short_code "
                f"HAVING COUNT(DISTINCT cm.member_concept_id) = ? "
                f"AND ? = ("
                f"  SELECT COUNT(*) FROM compose_members cm2 "
                f"  WHERE cm2.concept_id = cm.concept_id "
                f"  AND cm2.short_code = cm.short_code "
                f"  AND cm2.position = 1"
                f")",
                (*ids_at_1, n, n),
            ).fetchone()

        if row:
            return (row["concept_id"], row["short_code"], row["status"])
        return None

    def _query_inbound_relations(
        self, concept_id: int,
    ) -> dict[str, list[RelationRow]]:
        """查所有以 concept_id 为 target（position 2）的有向关系，按 status 分组。"""
        rows = self.conn.execute(
            "SELECT DISTINCT "
            "  c_rel.id AS concept_id, c_rel.name AS concept_name, v.status, "
            "  c_src.id AS from_concept_id, "
            "  c_src.name AS from_concept_name, "
            "  c_src.disclosure AS from_concept_disclosure, "
            "  c_tgt.name AS to_concept_name "
            "FROM compose_members cm2 "
            "JOIN compose_members cm1 "
            "  ON cm1.concept_id = cm2.concept_id "
            "  AND cm1.short_code = cm2.short_code "
            "  AND cm1.position = 1 "
            "JOIN variations v "
            "  ON v.concept_id = cm2.concept_id "
            "  AND v.short_code = cm2.short_code "
            "JOIN concepts c_rel ON v.concept_id = c_rel.id "
            "JOIN concepts c_src ON cm1.member_concept_id = c_src.id "
            "JOIN concepts c_tgt ON cm2.member_concept_id = c_tgt.id "
            "WHERE cm2.member_concept_id = ? AND cm2.position = 2",
            (concept_id,),
        ).fetchall()
        grouped: dict[str, list[RelationRow]] = {
            "confirmed": [], "negated": [], "hypothesis": [],
        }
        for row in rows:
            # 未设状态(NULL)等同于 hypothesis，与 _load_graph 的图谱语义保持一致。
            s = row["status"] or "hypothesis"
            if s in grouped:
                grouped[s].append(RelationRow(
                    expression=f"{row['from_concept_name']} → {row['to_concept_name']}",
                    concept_id=row["concept_id"],
                    concept_name=row["concept_name"],
                    from_concept_id=row["from_concept_id"],
                    from_concept_disclosure=row["from_concept_disclosure"],
                ))
        return grouped

    def _query_outbound_relations(
        self, concept_id: int,
    ) -> dict[str, list[OutboundRelation]]:
        """查所有以 concept_id 为 source（position 1）的有向关系，按 status 分组。"""
        rows = self.conn.execute(
            "SELECT DISTINCT "
            "  c_src.name AS from_concept_name, "
            "  c_tgt.id AS target_concept_id, "
            "  c_tgt.name AS target_concept_name, "
            "  c_tgt.disclosure AS target_concept_disclosure, "
            "  c_rel.id AS concept_id, c_rel.name AS concept_name, "
            "  v.status "
            "FROM compose_members cm1 "
            "JOIN compose_members cm2 "
            "  ON cm1.concept_id = cm2.concept_id "
            "  AND cm1.short_code = cm2.short_code "
            "  AND cm2.position = 2 "
            "JOIN variations v "
            "  ON v.concept_id = cm1.concept_id "
            "  AND v.short_code = cm1.short_code "
            "JOIN concepts c_rel ON v.concept_id = c_rel.id "
            "JOIN concepts c_src ON cm1.member_concept_id = c_src.id "
            "JOIN concepts c_tgt ON cm2.member_concept_id = c_tgt.id "
            "WHERE cm1.member_concept_id = ? AND cm1.position = 1",
            (concept_id,),
        ).fetchall()
        grouped: dict[str, list[OutboundRelation]] = {
            "confirmed": [], "negated": [], "hypothesis": [],
        }
        for row in rows:
            # 未设状态(NULL)等同于 hypothesis，与 _load_graph 的图谱语义保持一致。
            s = row["status"] or "hypothesis"
            if s in grouped:
                grouped[s].append(OutboundRelation(
                    expression=f"{row['from_concept_name']} → {row['target_concept_name']}",
                    concept_id=row["concept_id"],
                    concept_name=row["concept_name"],
                    target_concept_id=row["target_concept_id"],
                    target_concept_disclosure=row["target_concept_disclosure"],
                ))
        return grouped

    def _scan_alerts(self, concept_ids: set[int]) -> list[str]:
        """检查给定 concept 集的 unless 条件，返回已触发的警报。"""
        alerts: list[str] = []
        if not concept_ids:
            return alerts

        placeholders = ",".join("?" * len(concept_ids))
        var_rows = self.conn.execute(
            f"SELECT v.concept_id, v.short_code, v.unless, c.name "
            f"FROM variations v JOIN concepts c ON v.concept_id = c.id "
            f"WHERE v.concept_id IN ({placeholders}) AND v.unless IS NOT NULL",
            tuple(concept_ids),
        ).fetchall()

        expr_cache = {}

        for vrow in var_rows:
            source_name = vrow["name"]
            for match in _CONDITION_RE.finditer(vrow["unless"]):
                expr = match.group(1).strip()
                expected = match.group(2)

                if expr not in expr_cache:
                    expr_cache[expr] = self._find_composition_variation(expr)
                target = expr_cache[expr]

                if target is None:
                    alerts.append(
                        f"Broken reference in '{source_name}': "
                        f"its unless condition watches '{expr}', "
                        f"but that composition no longer exists in "
                        f"the graph. read_concept '{source_name}' "
                        f"and decide whether to update or remove "
                        f"the unless condition.")
                    continue
                target_cid, _, actual = target

                if actual != expected:
                    continue

                target_name = self._resolve_concept_name(target_cid)

                alerts.append(
                    f"Unless triggered on '{source_name}': "
                    f"'{target_name}' is now {actual} "
                    f"(the condition ${{{expr} {expected}}} "
                    f"has been met). The ground has shifted — "
                    f"review '{source_name}' and any related "
                    f"concepts to decide whether its evidence, "
                    f"status, and unless still hold.")
        return alerts

    def read_concept(self, concept) -> ReadResult:
        """读取概念的完整视图：concept 本体 + 入边 + 出边 + alerts。"""
        cid, _ = self._resolve_id(concept)
        row = self.conn.execute(
            "SELECT * FROM concepts WHERE id = ?", (cid,)
        ).fetchone()

        # variations
        var_rows = self.conn.execute(
            "SELECT * FROM variations WHERE concept_id = ? "
            "ORDER BY short_code",
            (cid,),
        ).fetchall()
        # 一次性查出该 concept 所有变体的成员，按 short_code 分组（避免逐变体 N+1 查询）。
        member_rows = self.conn.execute(
            "SELECT cm.short_code, cm.member_concept_id AS concept_id, c.name, "
            "       cm.position, c.disclosure "
            "FROM compose_members cm "
            "JOIN concepts c ON cm.member_concept_id = c.id "
            "WHERE cm.concept_id = ? "
            "ORDER BY cm.position, cm.member_concept_id",
            (cid,),
        ).fetchall()
        members_by_sc: dict[str, list[ComposeMemberDetail]] = {}
        for mr in member_rows:
            d = dict(mr)
            sc = d.pop("short_code")
            members_by_sc.setdefault(sc, []).append(ComposeMemberDetail(**d))

        variations: list[VariationDetail] = []
        for vr in var_rows:
            expr = self._get_expression(cid, vr["short_code"])
            members = members_by_sc.get(vr["short_code"], [])
            variations.append(VariationDetail(**dict(vr), expression=expr, members=members))

        # aliases
        aliases = [
            ar["alias"] for ar in self.conn.execute(
                "SELECT alias FROM aliases WHERE concept_id = ?", (cid,)
            ).fetchall()
        ]

        # 入边 / 出边
        inbound = self._query_inbound_relations(cid)
        outbound = self._query_outbound_relations(cid)

        # alerts 范围：自身 + 以自身为成员的组合概念
        related_rows = self.conn.execute(
            "SELECT DISTINCT concept_id FROM compose_members "
            "WHERE member_concept_id = ?",
            (cid,),
        ).fetchall()
        visible_ids: set[int] = {cid}
        visible_ids |= {r["concept_id"] for r in related_rows}
        alerts = self._scan_alerts(visible_ids)

        return ReadResult(
            id=cid,
            name=row["name"],
            disclosure=row["disclosure"],
            aliases=aliases,
            variations=variations,
            inbound_confirmed=inbound["confirmed"],
            inbound_negated=inbound["negated"],
            inbound_hypotheses=inbound["hypothesis"],
            outbound_confirmed=outbound["confirmed"],
            outbound_negated=outbound["negated"],
            outbound_hypotheses=outbound["hypothesis"],
            alerts=alerts,
        )

    # ── Compile (path verification) ─────────────────────────────────────────

    def _load_graph(self) -> None:
        """从 DB 加载遍历图到内存，供 _cheapest_route 使用。

        → 和 & 分开存储（语义不同）：
        - → 是点对点的有向边，直接用于 Dijkstra 寻路 + 影分身。
        - & 是多成员共现组。成员之间不互通（& 不提供推导关系）。
          全部成员到齐时，组合概念本身被激活。

        写入三个实例属性：
          _adjacency:  → 边的邻接表。
            from_cid -> [(to_cid, edge_cid, edge_sc, status)]
          _and_groups: & 组的列表。
            [(member_set, edge_cid, edge_sc, status)]
          _and_index:  从成员 cid 快速查到它所属的 & 组。
            member_cid -> [_and_groups 里的下标]

        包含 negated 状态的边（供外部获取全量图谱使用），算法逻辑在遍历时自行跳过。名字不存——下游按需查 concepts 表。
        """
        # ── → 边 ──
        # compose_members 自连接：pos-1 是起点，pos-2 是终点，
        # 同 (concept_id, short_code) 的一对即一条有向边。
        adjacency: dict[int, list[tuple[int, int, str, str]]] = {}
        rows = self.conn.execute(
            "SELECT "
            "  cm1.member_concept_id AS from_cid, "
            "  cm2.member_concept_id AS to_cid, "
            "  cm1.concept_id AS edge_cid, "
            "  cm1.short_code AS edge_sc, "
            "  v.status "
            "FROM compose_members cm1 "
            "JOIN compose_members cm2 USING (concept_id, short_code) "
            "JOIN variations v USING (concept_id, short_code) "
            "WHERE cm1.position = 1 AND cm2.position = 2"
        ).fetchall()
        for r in rows:
            status = r["status"] or "hypothesis"
            adjacency.setdefault(r["from_cid"], []).append(
                (r["to_cid"], r["edge_cid"], r["edge_sc"], status))
        self._adjacency = adjacency

        # ── & 组 ──
        # 全部成员都在 pos-1、没有 pos-2 的 variation 就是 & 关系。
        # 逐行取出后按 (concept_id, short_code) 分组，拼成成员集合。
        rows = self.conn.execute(
            "SELECT cm.concept_id AS edge_cid, cm.short_code AS edge_sc, "
            "  cm.member_concept_id AS member_cid, v.status "
            "FROM compose_members cm "
            "JOIN variations v USING (concept_id, short_code) "
            "WHERE cm.position = 1 "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM compose_members cm2 "
            "  WHERE cm2.concept_id = cm.concept_id "
            "  AND cm2.short_code = cm.short_code "
            "  AND cm2.position = 2)"
        ).fetchall()
        # 按 variation 分组，收集成员集合
        raw: dict[tuple[int, str], tuple[set[int], str]] = {}
        for r in rows:
            key = (r["edge_cid"], r["edge_sc"])
            if key not in raw:
                raw[key] = (set(), r["status"] or "hypothesis")
            raw[key][0].add(r["member_cid"])

        # 构建 & 组列表和成员→组的反向索引
        and_groups: list[tuple[frozenset[int], int, str, str]] = []
        and_index: dict[int, list[int]] = {}
        for (edge_cid, edge_sc), (members, status) in raw.items():
            idx = len(and_groups)
            and_groups.append(
                (frozenset(members), edge_cid, edge_sc, status))
            for m in members:
                and_index.setdefault(m, []).append(idx)

        self._and_groups = and_groups
        self._and_index = and_index



    def _cheapest_route(
        self,
        active_concept_ids: set[int],
        required_concept_ids: set[int],
        goal_concept_id: int,
    ) -> list[dict] | None:
        """从多个起点到单一终点的最短路搜索（Dijkstra），并强制依赖 required_concept_ids。
        代价结构: _PathCost(hypothesis_count, total_jumps) 按字典序比较。
        """
        if goal_concept_id in active_concept_ids:
            if not required_concept_ids or goal_concept_id in required_concept_ids:
                return []

        inf_cost = _PathCost(float("inf"), float("inf"))
        costs: dict[tuple[int, bool], _PathCost] = {}
        step_to_reach: dict[tuple[int, bool], _RouteStep] = {}
        frontier: list[tuple[_PathCost, int, bool]] = []

        for start_id in active_concept_ids:
            used_req = True if (not required_concept_ids or start_id in required_concept_ids) else False
            costs[(start_id, used_req)] = _PathCost(0, 0)
            heapq.heappush(frontier, (_PathCost(0, 0), start_id, used_req))

        def get_neighbors(current_cid: int, current_cost: _PathCost, current_used_req: bool):
            for to_cid, edge_cid, edge_sc, status in self._adjacency.get(current_cid, []):
                if status == "negated":
                    continue
                next_cost = _PathCost(
                    current_cost.hypothesis_count + (0 if status == "confirmed" else 1),
                    current_cost.total_jumps + 1
                )
                yield to_cid, next_cost, current_used_req, _RouteStep(((current_cid, current_used_req),), to_cid, edge_cid, edge_sc, status)

                if edge_cid != current_cid and edge_cid != to_cid:
                    yield edge_cid, next_cost, current_used_req, _RouteStep(((current_cid, current_used_req),), to_cid, edge_cid, edge_sc, status)

            for group_index in self._and_index.get(current_cid, []):
                members, edge_cid, edge_sc, status = self._and_groups[group_index]
                if status == "negated":
                    continue
                if edge_cid in members:
                    continue

                if not all((m, False) in costs or (m, True) in costs for m in members):
                    continue

                edge_cost_add = (0 if status == "confirmed" else 1)

                if all((m, False) in costs for m in members):
                    bottleneck_false = max(costs[(m, False)] for m in members)
                    next_cost = _PathCost(
                        bottleneck_false.hypothesis_count + edge_cost_add,
                        bottleneck_false.total_jumps + 1
                    )
                    combo = tuple((m, False) for m in members)
                    yield edge_cid, next_cost, False, _RouteStep(combo, edge_cid, edge_cid, edge_sc, status)

                best_true_cost = None
                best_true_combination = None
                for force_true_m in members:
                    if (force_true_m, True) not in costs:
                        continue
                    cur_bottleneck = costs[(force_true_m, True)]
                    combo = [(force_true_m, True)]
                    for other_m in members:
                        if other_m == force_true_m: continue
                        opts = [(costs[(other_m, u)], u) for u in (True, False) if (other_m, u) in costs]
                        best_other_cost, best_other_u = min(opts)
                        cur_bottleneck = max(cur_bottleneck, best_other_cost)
                        combo.append((other_m, best_other_u))
                    
                    if best_true_cost is None or cur_bottleneck < best_true_cost:
                        best_true_cost = cur_bottleneck
                        best_true_combination = tuple(combo)
                        
                if best_true_cost is not None:
                    next_cost = _PathCost(
                        best_true_cost.hypothesis_count + edge_cost_add,
                        best_true_cost.total_jumps + 1
                    )
                    yield edge_cid, next_cost, True, _RouteStep(best_true_combination, edge_cid, edge_cid, edge_sc, status)

        while frontier:
            cost_here, current_cid, current_used_req = heapq.heappop(frontier)
            if cost_here > costs.get((current_cid, current_used_req), inf_cost):
                continue
            if current_cid == goal_concept_id and current_used_req == True:
                break

            for next_cid, next_cost, next_used_req, step_info in get_neighbors(current_cid, cost_here, current_used_req):
                if next_cost < costs.get((next_cid, next_used_req), inf_cost):
                    costs[(next_cid, next_used_req)] = next_cost
                    step_to_reach[(next_cid, next_used_req)] = step_info
                    heapq.heappush(frontier, (next_cost, next_cid, next_used_req))

        if (goal_concept_id, True) not in costs:
            return None

        edges: list[dict] = []
        edges_set = set()
        visited_states = set()

        def trace(state: tuple[int, bool]):
            if state in visited_states:
                return
            visited_states.add(state)

            step = step_to_reach.get(state)
            if not step:
                return

            for from_state in step.from_states:
                trace(from_state)

            from_cids = tuple(s[0] for s in step.from_states)
            edge_tuple = (
                step.edge_concept_id,
                step.edge_short_code,
                from_cids,
                step.destination_id,
                step.status
            )

            if edge_tuple in edges_set:
                return

            edges_set.add(edge_tuple)

            from_data = {
                "concept_ids": sorted(from_cids),
                "name": " & ".join(self._resolve_concept_name(c) for c in sorted(from_cids))
            }

            edges.append({
                "concept_id": step.edge_concept_id,
                "short_code": step.edge_short_code,
                "name": self._resolve_concept_name(step.edge_concept_id),
                "status": step.status,
                "from": from_data,
                "to": {
                    "concept_id": step.destination_id,
                    "name": self._resolve_concept_name(step.destination_id)
                },
            })

        trace((goal_concept_id, True))
        return edges




    def compile(self, steps: list[str | int],
                goal: str | int) -> dict:
        """验证 steps[0] → ... → goal 的路径，从左往右逐段检查，累积影分身。

        Args:
            steps: 按序途经的概念（steps[0] 为起点）。
            goal:  终点。

        Returns dict:
            passed:         bool — 全通 or 断路。
            compiled_route: 验证过的边链
                            （全通=整条路线，断路=起点到断口左端的已验证链）。
                            边形状: {"concept_id": int, "short_code": str,
                                     "name": str, "status": str,
                                     "from": {"concept_id": int, "name": str},
                                     "to": {"concept_id": int, "name": str}}
            break:          断口信息（仅断路时）。
            detour:         从任一已确立的途经点（含起点）到终点的无约束自由通路（仅断路时，无则 None）。
            errors:         文本诊断（解析失败等）。
        """
        result: dict = {
            "passed": False,
            "compiled_route": [],
            "break": None,
            "detour": None,
            "errors": [],
        }

        waypoint_inputs = list(steps) + [goal]
        try:
            waypoints = [self._resolve_id(s)[0] for s in waypoint_inputs]
        except ValueError as e:
            result["errors"].append(str(e))
            return result

        input_names = {cid: str(inp) for cid, inp in zip(waypoints, waypoint_inputs)}

        self._load_graph()

        # 从左往右逐段验证，累积影分身 concept 作为下一段的起点集
        compiled_edges: list[dict] = []
        activated: set[int] = {waypoints[0]}
        newly_activated: set[int] = {waypoints[0]}

        for i in range(len(waypoints) - 1):
            edges = self._cheapest_route(activated, newly_activated, waypoints[i + 1])
            if edges is None:
                result["break"] = {
                    "from": {"concept_id": waypoints[i],
                             "name": input_names[waypoints[i]]},
                    "to": {"concept_id": waypoints[i + 1],
                           "name": input_names[waypoints[i + 1]]},
                }
                break
            compiled_edges.extend(edges)
            
            newly_activated = set()
            for edge in edges:
                newly_activated.add(edge["concept_id"])
            newly_activated.add(waypoints[i + 1])
            
            activated.update(newly_activated)

        result["compiled_route"] = compiled_edges

        if result["break"] is None:
            # ── 全通 ──
            result["passed"] = True
        else:
            # ── 断路 ──
            detour_edges = self._cheapest_route(
                activated, set(), waypoints[-1])
            if detour_edges is not None:
                result["detour"] = detour_edges

        def _apply_names(edge_list: list[dict]):
            for edge in edge_list:
                member_names = [
                    input_names.get(cid, self._resolve_concept_name(cid))
                    for cid in edge["from"]["concept_ids"]
                ]
                edge["from"]["name"] = " & ".join(member_names)

                cid_to = edge["to"]["concept_id"]
                if cid_to in input_names:
                    edge["to"]["name"] = input_names[cid_to]

        _apply_names(result["compiled_route"])
        if result.get("detour"):
            _apply_names(result["detour"])

        del self._adjacency, self._and_groups, self._and_index
        return result
