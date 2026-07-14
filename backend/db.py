"""
Horon — Inference Language for Intelligent Agents
DB operations (Concept → Variation 分層結構)

Concept:   概念的对外身份（名字 + disclosure），组合的参与单位。
Variation: 同一概念的不同解释（concept_id + short_code + type），
           type ∈ {CHAIN, AND, OR, NULL(原子)}，
           每个 variation 有独立的 status / content / unless / compose_members。
compose_members 的 member 引用 concept_id（hub），不是具体 variation。
"""
from __future__ import annotations

import os
import re
import secrets
import sqlite3
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from .compiler import Compiler, ExpressionRule, RelationGraph
from .models import (
    Concept, VariationDetail, ComposeMemberDetail,
    DirectedRelation, RelationMember, ReadResult,
    MutationResult,
)


_CONDITION_RE = re.compile(r'\$\{\s*(.+?)\s+(confirmed|negated)\s*\}')
_CONTROL_CHAR_RE = re.compile(r'[\x00-\x1f\x7f]')
_MAX_NAME_LEN = 200
_FORBIDDEN_CHARS = {'→', '&', ':', '|'}

# 被代码直接引用的 tag。词表本身在 tags 表里（数据不是 schema），
# 注册新 tag 走 create_tag；给概念盖未注册的 tag 会被外键拒绝。
_TAG_PLAN = "plan"
_TAG_RESULT = "result"


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
    '''确保方法执行在显式事务边界内。失败自动回滚，成功自动提交。
    支持重入：已在事务内的嵌套调用直接穿透，不会提前 commit。'''
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        if getattr(self, '_in_transaction', False):
            return method(self, *args, **kwargs)
        self._in_transaction = True
        try:
            with self.conn:
                return method(self, *args, **kwargs)
        finally:
            self._in_transaction = False
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

    # ── Concept lifecycle ────────────────────────────────────────────────────

    @transactional
    def create_concept(self, name: str,
                       disclosure: str | None = None) -> MutationResult:
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
        return MutationResult(
            message=f"Success. Created concept '{name}' ('{name}', id={concept_id}).",
            concept_id=concept_id,
            concept_name=name,
            short_code=sc,
        )

    @transactional
    def init_plan(self, name: str) -> MutationResult:
        """创建计划概念并原子化盖上 plan tag，返回引导性提示词。"""
        res = self.create_concept(name)
        self._add_tag(res.concept_id, _TAG_PLAN)
        res.message = (
            f"[OK] Concept '{name}' created with tag 'plan'.\n\n"
            f"[ACTION REQUIRED]\n"
            f"一个合法的计划必须包含具体的执行步骤。你现在必须补全其结构：\n"
            f"使用 `horon set \"{name}\" expression \"步骤A → 步骤B → ...\"` 为其设定一个由 CHAIN (→) 组成的表达式。"
        )
        return res

    @transactional
    def init_result(self, name: str) -> MutationResult:
        """创建结果概念并原子化盖上 result tag，纯快捷方式，无多余引导。"""
        res = self.create_concept(name)
        self._add_tag(res.concept_id, "result")
        res.message = f"[OK] Concept '{name}' created with tag 'result'."
        return res

    @transactional
    def suppose(self, expression: str) -> MutationResult:
        """从表达式直接原子化创建概念+组合变体（自动命名）。"""
        vtype, member_ids = self._parse_expression(expression, allow_single=False)

        existing = self._find_composition_variation(vtype, member_ids)
        if existing is not None:
            exist_cid, exist_sc, _ = existing
            exist_name = self._resolve_concept_name(exist_cid)
            raise ValueError(
                f"This relation already exists: '{exist_name}:{exist_sc}' "
                f"(id={exist_cid}). Do not create a duplicate. To record a "
                f"new observation of this relation, update the content of "
                f"that variation ('{exist_name}:{exist_sc}')."
            )

        member_names = [self._resolve_concept_name(mid) for mid in member_ids]
        
        if vtype == "CHAIN":
            joiner = "-then-"
        elif vtype == "AND":
            joiner = "-and-"
        elif vtype == "OR":
            joiner = "-or-"
        else:
            raise ValueError(f"Unknown variation type: {vtype}")
            
        base_name = joiner.join(member_names)
        
        try:
            _validate_name(base_name)
            self._check_name_available(base_name)
        except ValueError as e:
            raise ValueError(
                f"Auto-naming failed for '{expression}': {e}. "
                f"Please fall back to manual creation: "
                f"use `horon create_concept <custom_name>` then `horon add <custom_name> variation \"{expression}\"`."
            )
            
        final_name = base_name

        res = self.create_concept(final_name)
        set_res = self._set_expression(final_name, expression)

        msg = (
            f"Success. Created concept '{final_name}' (id={res.concept_id}) "
            f"to represent this relation.\n"
            f"Variation {set_res.short_code} expression: {expression}"
        )
        
        if vtype == "CHAIN" and len(member_ids) == 2:
            cursor = self.conn.execute("SELECT concept_id, tag FROM concept_tags WHERE concept_id IN (?, ?)", (member_ids[0], member_ids[1]))
            rows = cursor.fetchall()
            tags_a = {r["tag"] for r in rows if r["concept_id"] == member_ids[0]}
            tags_b = {r["tag"] for r in rows if r["concept_id"] == member_ids[1]}
            
            if _TAG_PLAN in tags_a and "result" in tags_b:
                msg += (
                    f"\n\n[ACTION REQUIRED]\n"
                    f"你刚刚建立了一个 Plan → Result 的预期链路。\n"
                    f"注意：此连接当前处于 hypothesis (假设) 状态。\n"
                    f"验证有了结果后，把验证过程和凭据 update 进该概念（id={res.concept_id}）的 content。"
                )
        
        set_res.message = msg
        return set_res

    def search_concepts(self, query=None, tag: str | None = None) -> list[Concept]:
        """按 alias、disclosure 或 content 模糊搜索 concept，可选按 tag 过滤。

        输入：query —— 文本子串（None = 不按文本过滤）；
              tag  —— 只返回带该 tag 的概念（None = 不按 tag 过滤）。
              两者都给取交集；两者都不给报错。
        输出：命中的 Concept 列表。
        典型用法：goal 选单 = search_concepts(tag='result')，
        返回所有曾以结果身份出现的概念，供 compile --goal 选靶。
        """
        if query is None and tag is None:
            raise ValueError("Provide a search query, a tag, or both.")
        joins = [
            "LEFT JOIN aliases a ON c.id = a.concept_id",
            "LEFT JOIN variations v ON c.id = v.concept_id",
        ]
        conditions = []
        parameters: list = []
        if tag is not None:
            joins.append("JOIN concept_tags ct ON c.id = ct.concept_id")
            conditions.append("ct.tag = ?")
            parameters.append(tag)
        if query is not None:
            escaped = (query.replace("\\", "\\\\")
                       .replace("%", "\\%").replace("_", "\\_"))
            like = f"%{escaped}%"
            conditions.append(
                "(a.alias LIKE ? ESCAPE '\\' "
                "OR c.disclosure LIKE ? ESCAPE '\\' "
                "OR v.content LIKE ? ESCAPE '\\')")
            parameters.extend([like, like, like])
        rows = self.conn.execute(
            "SELECT DISTINCT c.* FROM concepts c "
            + " ".join(joins)
            + " WHERE " + " AND ".join(conditions),
            parameters,
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
            "SELECT concept_id, short_code, type, status FROM variations "
            "ORDER BY concept_id, short_code"
        ).fetchall()
        vars_by_cid: dict[int, list] = {}
        for r in var_rows:
            vars_by_cid.setdefault(r["concept_id"], []).append(r)

        mem_rows = self.conn.execute(
            "SELECT cm.concept_id, cm.short_code, cm.order_index, c.name "
            "FROM compose_members cm "
            "JOIN concepts c ON cm.member_concept_id = c.id "
            "ORDER BY cm.order_index, cm.member_concept_id"
        ).fetchall()

        mems_by_var: dict[tuple, list[str]] = {}
        for r in mem_rows:
            key = (r["concept_id"], r["short_code"])
            mems_by_var.setdefault(key, []).append(r["name"])

        _OP = {"CHAIN": " → ", "AND": " & ", "OR": " | "}

        result = []
        for c in concepts:
            c_dict = {
                "id": c.id,
                "name": c.name,
                "disclosure": c.disclosure,
                "variations": [],
            }
            for v in vars_by_cid.get(c.id, []):
                key = (c.id, v["short_code"])
                expr = None
                vtype = v["type"]
                names = mems_by_var.get(key)
                if names and vtype:
                    expr = _OP.get(vtype, " & ").join(names)

                c_dict["variations"].append({
                    "short_code": v["short_code"],
                    "type": vtype,
                    "status": v["status"] or "hypothesis",
                    "expression": expr,
                })
            result.append(c_dict)

        return result

    # ── Helpers ───────────────────────────────────────────────────────────────

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
            # CHAIN 允许同一概念在不同位置重复出现（如 A → B → A：链条绕回
            # 起点），每个 order_index 都是独立 occurrence，编译器不去重。
            # 但相邻两段相同（A → A）是零跨度自环，无语义，拒绝。
            if any(a == b for a, b in zip(ids, ids[1:])):
                raise ValueError(
                    "A concept cannot immediately follow itself in a chain "
                    "(e.g. 'A → A' is invalid).")
        elif vtype != "SINGLE":
            # AND / OR 是集合语义，成员必须互不相同（A & A / A | A 无意义）。
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

    def _check_plan_chain(self, cid: int, vtype: str) -> tuple[bool, bool]:
        """plan tag 的表达式约束：只能是 CHAIN。返回 (is_plan, is_first_chain)。"""
        cursor = self.conn.execute(
            "SELECT tag FROM concept_tags WHERE concept_id = ?", (cid,))
        tags = {row["tag"] for row in cursor.fetchall()}
        is_plan = _TAG_PLAN in tags

        if is_plan and vtype != "CHAIN":
            raise ValueError(
                f"Concept has '{_TAG_PLAN}' tag and can only accept "
                f"CHAIN expressions (got {vtype})."
            )

        cursor = self.conn.execute(
            "SELECT COUNT(*) as c FROM variations "
            "WHERE concept_id = ? AND type = 'CHAIN'", (cid,))
        is_first_chain = cursor.fetchone()["c"] == 0
        return is_plan, is_first_chain

    def _plan_first_chain_hint(self, cname: str) -> str:
        """计划首次建立 CHAIN 后的下一步引导文案。"""
        return (
            f"\n\n[ACTION REQUIRED]\n"
            f"你已经为计划确立了执行步骤。下一步是将它连接到预期结果：\n"
            f"`horon suppose \"{cname} → 预期结果概念\"`\n"
            f"（预期结果概念应带有 result 标签。如果还没有，先用 `horon init_result` 创建。）"
        )

    # ── Add ──────────────────────────────────────────────────────────────────

    def add(self, concept, kind: str, value: str) -> MutationResult:
        """给概念添加 name（别名）、variation（变种）或 tag（分类标签）。"""
        if kind == "name":
            return self._add_name(concept, value)
        elif kind == "variation":
            return self._add_variation(concept, value)
        elif kind == "tag":
            return self._add_tag(concept, value)
        raise ValueError(
            f"Unknown type: '{kind}'. Use 'name', 'variation' or 'tag'.")

    @transactional
    def _add_name(self, concept, name: str) -> MutationResult:
        """给 concept 加一个 alias。"""
        name = _validate_name(name)
        cid, _ = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"
        existing = self.conn.execute(
            "SELECT concept_id FROM aliases WHERE alias=?", (name,)
        ).fetchone()
        if existing:
            if existing["concept_id"] == cid:
                return MutationResult(
                    message=f"Success. '{name}' is already an alias for {label}.",
                    concept_id=cid, concept_name=cname,
                )
            raise ValueError(
                f"Name '{name}' already resolves to concept "
                f"{existing['concept_id']}.")
        self.conn.execute(
            "INSERT INTO aliases (alias, concept_id) VALUES (?,?)",
            (name, cid),
        )
        return MutationResult(
            message=f"Success. Added alias '{name}' to {label}.",
            concept_id=cid, concept_name=cname,
        )

    @transactional
    def _add_variation(self, concept, expression: str) -> MutationResult:
        """给 concept 新增变种，必须带 expression。"""
        if not expression or not expression.strip():
            raise ValueError("Expression cannot be empty.")
        cid, _ = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"

        vtype, member_ids = self._parse_expression(expression)

        is_plan, is_first_chain = self._check_plan_chain(cid, vtype)

        if cid in member_ids:
            raise ValueError(
                "A concept cannot appear in its own expression.")

        existing = self._find_composition_variation(vtype, member_ids)
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
            "(concept_id, short_code, type, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (cid, sc, vtype, now, now),
        )
        for idx, member_cid in enumerate(member_ids, start=1):
            self.conn.execute(
                "INSERT INTO compose_members "
                "(concept_id, short_code, member_concept_id, order_index) "
                "VALUES (?,?,?,?)",
                (cid, sc, member_cid, idx),
            )
        msg = f"Success. Added variation {sc} to {label}."
        if is_plan and is_first_chain:
            msg += self._plan_first_chain_hint(cname)

        return MutationResult(
            message=msg,
            concept_id=cid, concept_name=cname, short_code=sc,
        )

    @transactional
    def _add_tag(self, concept, tag: str) -> MutationResult:
        """给概念盖一个 tag。

        输入：concept —— 概念名/别名/ID；tag —— 必须已在 tags 词表注册。
        行为：未注册的 tag 直接报错并列出已知词表（词表变更走数据库迁移，
              无运行时注册入口）；概念已有该 tag 时幂等成功。
        输出：MutationResult。
        """
        tag = tag.strip()
        if not tag:
            raise ValueError("Tag cannot be empty.")
        cid, _ = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"
        if tag == _TAG_PLAN:
            invalid = self.conn.execute(
                "SELECT type FROM variations "
                "WHERE concept_id = ? AND type IN ('AND', 'OR') LIMIT 1",
                (cid,),
            ).fetchone()
            if invalid:
                raise ValueError(
                    f"Cannot tag {label} as '{_TAG_PLAN}': "
                    f"plans can only contain CHAIN expressions "
                    f"(found {invalid['type']})."
                )
        registered = self.conn.execute(
            "SELECT 1 FROM tags WHERE name = ?", (tag,)
        ).fetchone()
        if not registered:
            known = [row["name"] for row in self.conn.execute(
                "SELECT name FROM tags ORDER BY name")]
            raise ValueError(
                f"Tag '{tag}' is not registered. "
                f"Known tags: {', '.join(known)}. "
                f"New tags ship with their consumer code via DB migration.")
        already = self.conn.execute(
            "SELECT 1 FROM concept_tags WHERE concept_id = ? AND tag = ?",
            (cid, tag),
        ).fetchone()
        if already:
            return MutationResult(
                message=f"Success. {label} already has tag '{tag}'.",
                concept_id=cid, concept_name=cname,
            )
        self.conn.execute(
            "INSERT INTO concept_tags (concept_id, tag) VALUES (?,?)",
            (cid, tag),
        )
        return MutationResult(
            message=f"Success. Tagged {label} as '{tag}'.",
            concept_id=cid, concept_name=cname,
        )

    # ── Delete ───────────────────────────────────────────────────────────────

    def delete(self, target, kind: str | None = None,
               value: str | None = None) -> MutationResult:
        """删除操作。

        kind 省略:   删除 variation（target 为概念名或 概念:sc）。
        name:       删除别名（value=要删的别名，必填）。
        expression: 清除组合回原子态（target 为概念名或 概念:sc）。
        tag:        揭掉概念上的 tag（value=要揭的 tag，必填）。
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
        elif kind == "tag":
            if value is None:
                raise ValueError("Specify which tag to delete.")
            return self._delete_tag(target, value)
        raise ValueError(
            f"Unknown type: '{kind}'. "
            f"Use 'name', 'expression' or 'tag'.")

    @transactional
    def _delete_name(self, concept, name: str) -> MutationResult:
        """删一个 alias。拒删当前显示名。"""
        cid, _ = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"
        row = self.conn.execute(
            "SELECT concept_id FROM aliases WHERE alias=?", (name,)
        ).fetchone()
        if not row:
            raise ValueError(f"No such alias: '{name}'.")
        if row["concept_id"] != cid:
            raise ValueError(
                f"Alias '{name}' belongs to concept "
                f"{row['concept_id']}, not {label}.")
        if cname == name:
            raise ValueError(
                f"'{name}' is the display name. "
                f"Use 'set name' to change it first.")
        self.conn.execute(
            "DELETE FROM aliases WHERE alias=?", (name,))
        return MutationResult(
            message=f"Success. Removed alias '{name}' from {label}.",
            concept_id=cid, concept_name=cname,
        )

    @transactional
    def _delete_tag(self, concept, tag: str) -> MutationResult:
        """揭掉概念上的一个 tag。

        输入：concept —— 概念名/别名/ID；tag —— 该概念身上现有的 tag。
        行为：概念没有这个 tag 时报错（与 _delete_name 同待遇，删不存在
              的东西是调用方认知错误，不静默吞掉）。只揭概念与 tag 的
              关联，不动 tags 词表本身。
        输出：MutationResult。
        """
        tag = tag.strip()
        cid, _ = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"
        existing = self.conn.execute(
            "SELECT 1 FROM concept_tags WHERE concept_id = ? AND tag = ?",
            (cid, tag),
        ).fetchone()
        if not existing:
            raise ValueError(f"{label} does not have tag '{tag}'.")
        self.conn.execute(
            "DELETE FROM concept_tags WHERE concept_id = ? AND tag = ?",
            (cid, tag),
        )
        return MutationResult(
            message=f"Success. Removed tag '{tag}' from {label}.",
            concept_id=cid, concept_name=cname,
        )

    @transactional
    def _delete_variation(self, node) -> MutationResult:
        """删除 variation。最后一个 → concept 也删。

        删除最后一个 variation 会连带删除 concept 本体。
        删除前检查是否有其他 concept 的 variation 引用该 concept
        作为 compose_member；有则拒绝，防止产生残缺的死组合。
        """
        cid, sc = self._resolve_single_variation(node)
        cname = self._resolve_concept_name(cid)
        label = f"'{node}' ('{cname}', id={cid})"

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

        downgraded = self.audit_status_integrity()

        remaining = self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM variations WHERE concept_id=?",
            (cid,),
        ).fetchone()

        if remaining["cnt"] == 0:
            self.conn.execute(
                "DELETE FROM concepts WHERE id=?", (cid,))
            msg = f"Success. Deleted concept {label} (last variation {sc} removed)."
        else:
            msg = (f"Success. Deleted variation {sc} from {label}. "
                   f"{remaining['cnt']} variation(s) remaining.")

        if downgraded:
            msg += "\nCascaded downgrades:\n" + "\n".join(f"  - {log}" for log in downgraded)

        return MutationResult(
            message=msg,
            concept_id=cid, concept_name=cname, short_code=sc,
        )

    @transactional
    def _delete_expression(self, node) -> MutationResult:
        """清除 variation 的组合，回到原子态。status 一并清除。"""
        cid, sc = self._resolve_single_variation(node)
        cname = self._resolve_concept_name(cid)
        label = f"'{node}' ('{cname}', id={cid})"

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
            "UPDATE variations SET type=NULL, status=NULL, updated_at=? "
            "WHERE concept_id=? AND short_code=?",
            (_now(), cid, sc),
        )

        downgraded = self.audit_status_integrity()
        msg = (f"Success. Cleared expression and status for "
               f"variation {sc} of {label}.")
        if downgraded:
            msg += "\nCascaded downgrades:\n" + "\n".join(f"  - {log}" for log in downgraded)

        return MutationResult(
            message=msg,
            concept_id=cid, concept_name=cname, short_code=sc,
        )

    # ── Set ──────────────────────────────────────────────────────────────────

    def set(self, target, prop: str, value: str) -> MutationResult:
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
    def _set_disclosure(self, concept, text: str) -> MutationResult:
        cid, _ = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"
        text = text.strip()
        if not text:
            raise ValueError("Disclosure cannot be empty.")
        self.conn.execute(
            "UPDATE concepts SET disclosure=?, updated_at=? WHERE id=?",
            (text, _now(), cid),
        )
        return MutationResult(
            message=f"Success. Disclosure for {label} set to: {text}",
            concept_id=cid, concept_name=cname,
        )

    @transactional
    def _set_status(self, node, value: str) -> MutationResult:
        valid = ("hypothesis", "confirmed", "negated")
        if value not in valid:
            raise ValueError(
                f"Invalid status: '{value}'. "
                f"Must be one of: {', '.join(valid)}.")
        cid, sc = self._resolve_single_variation(node)
        cname = self._resolve_concept_name(cid)
        label = f"'{node}' ('{cname}', id={cid})"

        if value == "confirmed":
            members = self.conn.execute(
                "SELECT member_concept_id FROM compose_members "
                "WHERE concept_id=? AND short_code=?",
                (cid, sc)
            ).fetchall()
            if members:
                vtype_row = self.conn.execute(
                    "SELECT type FROM variations "
                    "WHERE concept_id=? AND short_code=?",
                    (cid, sc)
                ).fetchone()
                vtype = vtype_row["type"] if vtype_row else None
                member_ids = [row["member_concept_id"] for row in members]

                def _member_confirmed(mid: int) -> bool:
                    return self.conn.execute(
                        "SELECT 1 FROM variations "
                        "WHERE concept_id=? AND status='confirmed'",
                        (mid,)
                    ).fetchone() is not None

                if vtype == "OR":
                    # OR 是「择一」：只要任一成员有 confirmed 变体即可确认。
                    if not any(_member_confirmed(mid) for mid in member_ids):
                        names = ", ".join(
                            f"'{self._resolve_concept_name(mid)}'"
                            for mid in member_ids)
                        raise ValueError(
                            f"Cannot confirm OR variation. None of its members "
                            f"({names}) has a confirmed variation.")
                else:
                    # CHAIN / AND（及兼容的 NULL 类型）：成员缺一不可。
                    for mid in member_ids:
                        if not _member_confirmed(mid):
                            member_name = self._resolve_concept_name(mid)
                            raise ValueError(
                                f"Cannot confirm variation. Member concept "
                                f"'{member_name}' (id={mid}) has no confirmed "
                                f"variations.")

        self.conn.execute(
            "UPDATE variations SET status=?, updated_at=? "
            "WHERE concept_id=? AND short_code=?",
            (value, _now(), cid, sc),
        )

        downgraded = []
        if value != "confirmed":
            downgraded = self.audit_status_integrity()
            
        msg = f"Success. Status of {label} variation {sc} set to: {value}"
        if downgraded:
            msg += "\nCascaded downgrades:\n" + "\n".join(f"  - {log}" for log in downgraded)

        return MutationResult(
            message=msg,
            concept_id=cid, concept_name=cname, short_code=sc,
        )

    @transactional
    def audit_status_integrity(self) -> list[str]:
        """
        审计数据库，降级违规的 confirmed 组合变种（即当其子元素没有任何 confirmed 变种时）。
        由于子元素的降级可能导致父元素违规，本方法会循环执行级联降级，直到全库合规。
        返回所有被降级的变种记录信息。
        """
        downgraded_logs = []
        max_iterations = 100
        for _ in range(max_iterations):
            violating_variations = self.conn.execute("""
                -- CHAIN / AND（含兼容的 NULL 类型）：成员缺一不可，
                -- 任一成员没有 confirmed 变体即违规。
                SELECT DISTINCT v.concept_id, v.short_code, c.name
                FROM variations v
                JOIN concepts c ON v.concept_id = c.id
                JOIN compose_members cm ON v.concept_id = cm.concept_id AND v.short_code = cm.short_code
                WHERE v.status = 'confirmed'
                  AND IFNULL(v.type, 'AND') <> 'OR'
                  AND NOT EXISTS (
                      SELECT 1 FROM variations child_v
                      WHERE child_v.concept_id = cm.member_concept_id
                        AND child_v.status = 'confirmed'
                  )
                UNION
                -- OR 是「择一」：只有当所有成员都没有 confirmed 变体时才违规。
                SELECT v.concept_id, v.short_code, c.name
                FROM variations v
                JOIN concepts c ON v.concept_id = c.id
                WHERE v.status = 'confirmed'
                  AND v.type = 'OR'
                  AND NOT EXISTS (
                      SELECT 1 FROM compose_members cm
                      JOIN variations child_v
                        ON child_v.concept_id = cm.member_concept_id
                      WHERE cm.concept_id = v.concept_id
                        AND cm.short_code = v.short_code
                        AND child_v.status = 'confirmed'
                  )
            """).fetchall()

            if not violating_variations:
                break

            for row in violating_variations:
                cid = row["concept_id"]
                sc = row["short_code"]
                cname = row["name"]
                
                self.conn.execute(
                    "UPDATE variations SET status = 'hypothesis', updated_at = ? "
                    "WHERE concept_id = ? AND short_code = ?",
                    (_now(), cid, sc)
                )
                downgraded_logs.append(
                    f"Downgraded '{cname}' (id={cid}, sc={sc}) to 'hypothesis' "
                    f"due to unconfirmed child members."
                )
        else:
            raise RuntimeError(
                f"audit_status_integrity exceeded max_iterations ({max_iterations}). "
                "Possible circular dependency or loop bug detected."
            )

        return downgraded_logs

    def audit_plans(self) -> list[str]:
        """计划卫生检查：'plan' tag 的两条 lint。

        输入：无（扫描全库带 plan tag 的概念）。
        输出：问题行列表，全部合规时为空。两类问题：
          [malformed] 计划没有任何 CHAIN 出边 —— 违反"建计划必须绑定
                      期待结果"（计划之后必须 → 到你猜会发生的事）。
          [open]      计划的某条出边 status 仍是假设 —— 期待还没跟现实
                      碰撞结账，欠一次 confirm/negate。这是跨会话
                      "接着上次的活继续干"的入口清单。
        只读，不修改任何数据。
        """
        findings: list[str] = []
        plan_rows = self.conn.execute(
            "SELECT ct.concept_id, c.name FROM concept_tags ct "
            "JOIN concepts c ON c.id = ct.concept_id "
            "WHERE ct.tag = ? ORDER BY c.name",
            (_TAG_PLAN,),
        ).fetchall()
        for plan in plan_rows:
            outbound = self._query_outbound_relations(plan["concept_id"])
            outbound_total = sum(len(group) for group in outbound.values())
            if outbound_total == 0:
                findings.append(
                    f"[malformed] plan '{plan['name']}' "
                    f"(id={plan['concept_id']}) has no outbound expectation. "
                    f"A plan must chain into the result you expect from it.")
                continue
            for relation in outbound["hypothesis"]:
                findings.append(
                    f"[open] plan '{plan['name']}' (id={plan['concept_id']}): "
                    f"expectation '{relation.expression}' "
                    f"(see \"{relation.concept_name}\") is still a hypothesis. "
                    f"Settle it against reality: confirm or negate.")
        return findings

    @transactional
    def _set_name(self, concept, new_name: str) -> MutationResult:
        """改显示名。旧显示名降级为 alias，保留在名字集合里。"""
        new_name = _validate_name(new_name)
        cid, _ = self._resolve_id(concept)
        old_name = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{old_name}', id={cid})"
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
        return MutationResult(
            message=f"Success. Renamed {label} to '{new_name}'.",
            concept_id=cid, concept_name=new_name,
        )

    @transactional
    def _set_expression(self, node, expression: str) -> MutationResult:
        """覆盖 variation 的组合。"""
        if not expression or not expression.strip():
            raise ValueError("Expression cannot be empty.")
        cid, sc = self._resolve_single_variation(node)
        cname = self._resolve_concept_name(cid)
        label = f"'{node}' ('{cname}', id={cid})"

        vtype, member_ids = self._parse_expression(expression)

        is_plan, is_first_chain = self._check_plan_chain(cid, vtype)

        if cid in member_ids:
            raise ValueError(
                "A concept cannot appear in its own expression.")

        existing = self._find_composition_variation(vtype, member_ids)
        if existing is not None:
            exist_cid, exist_sc, _ = existing
            if exist_cid != cid or exist_sc != sc:
                exist_name = self._resolve_concept_name(exist_cid)
                raise ValueError(
                    f"Variation '{exist_name}:{exist_sc}' already "
                    f"has the same composition: {expression}")

        self.conn.execute(
            "DELETE FROM compose_members "
            "WHERE concept_id=? AND short_code=?",
            (cid, sc),
        )
        for idx, member_cid in enumerate(member_ids, start=1):
            self.conn.execute(
                "INSERT INTO compose_members "
                "(concept_id, short_code, member_concept_id, order_index) "
                "VALUES (?,?,?,?)",
                (cid, sc, member_cid, idx),
            )
        self.conn.execute(
            "UPDATE variations SET type=?, status=NULL, updated_at=? "
            "WHERE concept_id=? AND short_code=?",
            (vtype, _now(), cid, sc),
        )

        downgraded = self.audit_status_integrity()
        msg = (f"Success. Expression of {label} variation {sc} "
               f"set to: {expression}. "
               f"Status was also reset to null.")
        if downgraded:
            msg += "\nCascaded downgrades:\n" + "\n".join(f"  - {log}" for log in downgraded)
        if is_plan and is_first_chain:
            msg += self._plan_first_chain_hint(cname)

        return MutationResult(
            message=msg,
            concept_id=cid, concept_name=cname, short_code=sc,
        )

    # ── Update ───────────────────────────────────────────────────────────────

    def get_variation_field(self, node, field: str) -> tuple[int, str, str | None]:
        """获取 variation 指定字段的当前值，供 CLI 层 patch mode 使用。避免 CLI 重复解析。"""
        valid_fields = ("content", "unless")
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
    def update(self, node, field: str, value: str) -> MutationResult:
        """给 variation 写 content 或 unless（patch/append 由 CLI 层处理）。

        node: concept 名/ID，或 "concept:short_code"。
        sole variation 时自动定位。
        """
        valid_fields = ("content", "unless")
        if field not in valid_fields:
            raise ValueError(
                f"Unknown field: '{field}'. "
                f"Use one of: {', '.join(valid_fields)}.")
        concept_id, sc = self._resolve_single_variation(node)
        cname = self._resolve_concept_name(concept_id)
        label = f"'{node}' ('{cname}', id={concept_id})"
        self.conn.execute(
            f"UPDATE variations SET {field}=?, updated_at=? "
            f"WHERE concept_id=? AND short_code=?",
            (value, _now(), concept_id, sc),
        )
        return MutationResult(
            message=f"Success. Updated {field} of variation {sc} of {label}.",
            concept_id=concept_id, concept_name=cname, short_code=sc,
        )

    # ── Query ────────────────────────────────────────────────────────────────

    def _get_expression(self, concept_id: int,
                        short_code: str) -> str | None:
        """取 variation 的组合表达式字符串。无组合返回 None。"""
        vtype_row = self.conn.execute(
            "SELECT type FROM variations "
            "WHERE concept_id = ? AND short_code = ?",
            (concept_id, short_code),
        ).fetchone()
        if not vtype_row or not vtype_row["type"]:
            return None

        rows = self.conn.execute(
            "SELECT c.name "
            "FROM compose_members cm "
            "JOIN concepts c ON cm.member_concept_id = c.id "
            "WHERE cm.concept_id = ? AND cm.short_code = ? "
            "ORDER BY cm.order_index",
            (concept_id, short_code),
        ).fetchall()
        if not rows:
            return None

        names = [r["name"] for r in rows]
        op = {"CHAIN": " → ", "AND": " & ", "OR": " | "}
        return op.get(vtype_row["type"], " & ").join(names)

    def _find_composition_variation(
        self, vtype: str, member_ids: list[int]
    ) -> tuple[int, str, str | None] | None:
        """找到持有某个组合的 variation → (concept_id, short_code, status)。

        通过构建规范签名在候选 variation 中精确匹配。
        CHAIN 按有序列表比较；AND/OR 按排序后的集合比较（忽略顺序），
        且 type 必须相同。
        """
        total_members = len(member_ids)
        if vtype == "CHAIN":
            target_sig: tuple[int, ...] = tuple(member_ids)
        else:
            target_sig = tuple(sorted(member_ids))

        placeholders = ",".join("?" * total_members)
        candidates = self.conn.execute(
            f"SELECT cm.concept_id, cm.short_code "
            f"FROM compose_members cm "
            f"JOIN variations v USING (concept_id, short_code) "
            f"WHERE cm.member_concept_id IN ({placeholders}) "
            f"AND v.type = ? "
            f"GROUP BY cm.concept_id, cm.short_code "
            f"HAVING COUNT(*) = ?",
            (*member_ids, vtype, total_members),
        ).fetchall()

        for cand in candidates:
            rows = self.conn.execute(
                "SELECT member_concept_id FROM compose_members "
                "WHERE concept_id = ? AND short_code = ? "
                "ORDER BY order_index",
                (cand["concept_id"], cand["short_code"]),
            ).fetchall()
            if len(rows) != total_members:
                continue
            cand_ids = [r["member_concept_id"] for r in rows]
            if vtype == "CHAIN":
                cand_sig = tuple(cand_ids)
            else:
                cand_sig = tuple(sorted(cand_ids))
            if cand_sig == target_sig:
                status = self.conn.execute(
                    "SELECT status FROM variations "
                    "WHERE concept_id = ? AND short_code = ?",
                    (cand["concept_id"], cand["short_code"]),
                ).fetchone()
                return (
                    cand["concept_id"],
                    cand["short_code"],
                    status["status"],
                )
        return None

    def _query_directed_relations(
        self, concept_id: int, *, inbound: bool,
    ) -> dict[str, list[DirectedRelation]]:
        """查 concept_id 的有向关系，按 status 分组。

        只有 CHAIN 类型的变体有方向性。AND/OR 的成员之间无有向关系。

        inbound=True：concept_id 作为被指向方（出现在 order_index >= 2），
            关系另一端是相邻上一个 order_index (idx - 1) 的成员。
        inbound=False：concept_id 作为指向方（其 order_index + 1 存在），
            关系另一端是相邻下一个 order_index (idx + 1) 的成员。

        单条 JOIN 一次取出 (variation, status, 另一端成员名/disclosure)，
        避免逐行回查；表达式字符串按 variation 缓存，不重复构建。
        """
        offset = -1 if inbound else 1
        rows = self.conn.execute(
            "SELECT "
            "  self_cm.concept_id AS v_cid, "
            "  self_cm.short_code AS v_sc, "
            "  self_cm.order_index AS self_pos, "
            "  v.status           AS status, "
            "  rel.name           AS concept_name, "
            "  other_cm.member_concept_id AS member_id, "
            "  m.name             AS member_name, "
            "  m.disclosure       AS member_disclosure "
            "FROM compose_members self_cm "
            "JOIN variations v "
            "  ON v.concept_id = self_cm.concept_id "
            "  AND v.short_code = self_cm.short_code "
            "  AND v.type = 'CHAIN' "
            "JOIN concepts rel ON rel.id = self_cm.concept_id "
            "JOIN compose_members other_cm "
            "  ON other_cm.concept_id = self_cm.concept_id "
            "  AND other_cm.short_code = self_cm.short_code "
            "  AND other_cm.order_index = self_cm.order_index + ? "
            "JOIN concepts m ON m.id = other_cm.member_concept_id "
            "WHERE self_cm.member_concept_id = ? "
            "ORDER BY self_cm.concept_id, self_cm.short_code, "
            "self_cm.order_index, other_cm.member_concept_id",
            (offset, concept_id),
        ).fetchall()

        grouped: dict[str, list[DirectedRelation]] = {
            "confirmed": [], "negated": [], "hypothesis": [],
        }
        # 同一 (variation, order_index) 的关系聚合成一条。
        relations: dict[tuple[int, str, int], DirectedRelation] = {}
        expr_cache: dict[tuple[int, str], str | None] = {}
        for row in rows:
            v_cid, v_sc, self_pos = row["v_cid"], row["v_sc"], row["self_pos"]
            s = row["status"] or "hypothesis"
            if s not in grouped:
                continue
            key = (v_cid, v_sc, self_pos)
            relation = relations.get(key)
            if relation is None:
                var_key = (v_cid, v_sc)
                if var_key not in expr_cache:
                    expr_cache[var_key] = self._get_expression(v_cid, v_sc)
                relation = DirectedRelation(
                    expression=expr_cache[var_key] or "",
                    concept_id=v_cid,
                    concept_name=row["concept_name"],
                    members=[],
                )
                relations[key] = relation
                grouped[s].append(relation)
            relation.members.append(RelationMember(
                concept_id=row["member_id"],
                concept_name=row["member_name"],
                disclosure=row["member_disclosure"],
            ))
        return grouped

    def _query_inbound_relations(
        self, concept_id: int,
    ) -> dict[str, list[DirectedRelation]]:
        return self._query_directed_relations(concept_id, inbound=True)

    def _query_outbound_relations(
        self, concept_id: int,
    ) -> dict[str, list[DirectedRelation]]:
        return self._query_directed_relations(concept_id, inbound=False)

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

                cache_key = (expr, expected)
                if cache_key not in expr_cache:
                    try:
                        vtype, member_ids = self._parse_expression(expr, allow_single=True)
                        if vtype in ("SINGLE", "OR"):
                            # 动态单节点 / OR 追踪
                            if expected == "negated":
                                expr_cache[cache_key] = {"mode": "unsupported_negated", "target": None}
                            else:
                                expr_cache[cache_key] = {"mode": "any_confirmed", "member_ids": member_ids}
                        elif vtype == "AND":
                            expr_cache[cache_key] = {"mode": "unsupported_and", "target": None}
                        else:
                            # CHAIN 实体追踪
                            target = self._find_composition_variation(vtype, member_ids)
                            if target:
                                expr_cache[cache_key] = {"mode": "exact", "target": target}
                            else:
                                expr_cache[cache_key] = {"mode": "not_met", "target": None}
                    except ValueError:
                        expr_cache[cache_key] = {"mode": "broken", "target": None}

                cache_val = expr_cache[cache_key]
                mode = cache_val["mode"]

                if mode == "not_met":
                    continue
                elif mode == "broken":
                    alerts.append(
                        f"Broken reference in '{source_name}': "
                        f"its unless condition watches '{expr}', "
                        f"but some concepts in it could not be resolved. "
                        f"read_concept '{source_name}' and decide whether "
                        f"to update or remove the unless condition.")
                    continue
                elif mode == "unsupported_negated":
                    alerts.append(
                        f"Invalid condition in '{source_name}': "
                        f"'{expr} negated' is not supported. "
                        f"Single-concept and OR conditions do not support 'negated'.")
                    continue
                elif mode == "unsupported_and":
                    alerts.append(
                        f"Invalid condition in '{source_name}': "
                        f"AND conditions such as '{expr} {expected}' are not supported.")
                    continue
                elif mode == "exact":
                    target_cid, _, actual = cache_val["target"]
                    if actual != expected:
                        continue
                    target_name = self._resolve_concept_name(target_cid)
                    alerts.append(
                        f"Unless triggered on '{source_name}': "
                        f"'{target_name}' is now {actual} "
                        f"(the condition ${{{expr} {expected}}} has been met). "
                        f"The ground has shifted — review '{source_name}' "
                        f"and any related concepts.")
                elif mode == "any_confirmed":
                    member_ids = cache_val["member_ids"]
                    placeholders = ",".join("?" * len(member_ids))
                    row = self.conn.execute(
                        f"SELECT v.concept_id FROM variations v "
                        f"WHERE v.concept_id IN ({placeholders}) AND v.status = 'confirmed' LIMIT 1",
                        tuple(member_ids)
                    ).fetchone()
                    if row:
                        target_cid = row["concept_id"]
                        target_name = self._resolve_concept_name(target_cid)
                        alerts.append(
                            f"Unless triggered on '{source_name}': "
                            f"'{target_name}' has a confirmed variation "
                            f"(the condition ${{{expr} confirmed}} has been met). "
                            f"The ground has shifted — review '{source_name}' "
                            f"and any related concepts.")
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
        member_rows = self.conn.execute(
            "SELECT cm.short_code, cm.member_concept_id AS concept_id, c.name, "
            "       cm.order_index, c.disclosure "
            "FROM compose_members cm "
            "JOIN concepts c ON cm.member_concept_id = c.id "
            "WHERE cm.concept_id = ? "
            "ORDER BY cm.order_index, cm.member_concept_id",
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

        # tags
        tags = [
            tr["tag"] for tr in self.conn.execute(
                "SELECT tag FROM concept_tags WHERE concept_id = ? "
                "ORDER BY tag", (cid,)
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
            tags=tags,
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

    def _load_relation_graph(self) -> RelationGraph:
        """Load each variation as one indivisible expression rule."""
        rows = self.conn.execute(
            "SELECT cm.concept_id, cm.short_code, cm.order_index, "
            "cm.member_concept_id, v.status, v.type "
            "FROM compose_members cm "
            "JOIN variations v USING (concept_id, short_code) "
            "ORDER BY cm.concept_id, cm.short_code, cm.order_index, "
            "cm.member_concept_id"
        ).fetchall()
        grouped: dict[tuple[int, str], tuple[list[int], str, str]] = {}
        for row in rows:
            key = (row["concept_id"], row["short_code"])
            if key not in grouped:
                grouped[key] = (
                    [],
                    row["status"] or "hypothesis",
                    row["type"] or "AND",
                )
            grouped[key][0].append(row["member_concept_id"])

        expressions = []
        for (concept_id, short_code), (members, status, vtype) in grouped.items():
            if vtype == "CHAIN":
                positions = tuple(frozenset({m}) for m in members)
            else:
                positions = (frozenset(members),)
            expressions.append(ExpressionRule(
                concept_id=concept_id,
                short_code=short_code,
                status=status,
                type=vtype,
                positions=positions,
            ))
        return RelationGraph(tuple(expressions))



    def compile(
        self,
        assume: list[str | int],
        block: list[str | int],
        constraints: list[str | int],
        goal: str | int,
    ) -> dict:
        """解析用户输入并委托给独立的编译引擎。"""
        result: dict = {
            "passed": False,
            "compiled_route": [],
            "concept_order": [],
            "break": None,
            "detour": None,
            "blocked": [],
            "errors": [],
            "goal_inbound_count": 0,
        }

        try:
            assume_ids = {self._resolve_id(s)[0] for s in assume}
            block_ids = frozenset(self._resolve_id(s)[0] for s in block)
            constraint_ids = {self._resolve_id(s)[0] for s in constraints}
            goal_id = self._resolve_id(goal)[0]
        except ValueError as e:
            result["errors"].append(str(e))
            return result
            
        inbound = self._query_inbound_relations(goal_id)
        # goal_inbound_count 数的是"被审视过的边界总量"，negated 也计入：
        # 一条被验证后否定的入边，同样证明注意力到过这一带。
        # 负结果可以清偿"补路"义务，避免在世界本来就窄的目标上永远催建假边。
        result["goal_inbound_count"] = (
            len(inbound.get("confirmed", []))
            + len(inbound.get("hypothesis", []))
            + len(inbound.get("negated", []))
        )

        conflicts = []
        if goal_id in block_ids:
            conflicts.append(f"goal ({self._resolve_concept_name(goal_id)}) is blocked")
        if assume_ids & block_ids:
            names = [self._resolve_concept_name(cid) for cid in assume_ids & block_ids]
            conflicts.append(f"assume and block intersect: {', '.join(names)}")
        if constraint_ids & block_ids:
            names = [self._resolve_concept_name(cid) for cid in constraint_ids & block_ids]
            conflicts.append(f"constraints and block intersect: {', '.join(names)}")
        
        if conflicts:
            result["errors"].append("Contradictory inputs: " + "; ".join(conflicts))
            return result

        input_names: dict[int, str] = {}
        for raw in list(assume) + list(block) + list(constraints) + [goal]:
            try:
                cid = self._resolve_id(raw)[0]
                input_names.setdefault(cid, str(raw))
            except ValueError:
                pass

        graph = self._load_relation_graph()
        compiler_result = Compiler(
            graph, self._resolve_concept_name, block=block_ids,
        ).compile(assume_ids, constraint_ids, goal_id, input_names)
        
        result.update(compiler_result)
        return result
