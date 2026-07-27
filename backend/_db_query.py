"""Query / read mixin for HoronDB."""
from __future__ import annotations

from .models import (
    Concept, VariationDetail, ComposeMemberDetail,
    DirectedRelation, RelationMember, ReadResult,
    ReminderDetail,
)


class QueryMixin:
    """Search, read, expression lookup, and directed-relation queries."""

    @staticmethod
    def _build_tag_filter_subquery(tag_expr: str) -> tuple[str, list]:
        """Parse a tag filter expression and return (subquery_sql, parameters).

        Syntax mirrors Horon's composition operators (& and | are
        forbidden in concept/tag names, so they are unambiguous):
          "鳥類 & 会飛ぶ"  → AND
          "鳥類 | 会飛ぶ"  → OR
          "plan"           → AND

        Mixing & and | in one expression is rejected.
        """
        has_and = "&" in tag_expr
        has_or = "|" in tag_expr
        if has_and and has_or:
            raise ValueError(
                "Tag filter cannot mix & and |. "
                "Use one operator per --tag expression.")

        if has_or:
            parts = [p.strip() for p in tag_expr.split("|")]
            mode = "OR"
        elif has_and:
            parts = [p.strip() for p in tag_expr.split("&")]
            mode = "AND"
        else:
            parts = [tag_expr.strip()]
            mode = "AND"

        parts = list(dict.fromkeys([p for p in parts if p]))
        if not parts:
            raise ValueError("Tag filter expression is empty.")

        placeholders = ",".join("?" for _ in parts)
        if mode == "AND":
            subq = (
                f"SELECT concept_id FROM concept_tags "
                f"WHERE tag IN ({placeholders}) "
                f"GROUP BY concept_id "
                f"HAVING COUNT(DISTINCT tag) = ?"
            )
            params = list(parts) + [len(parts)]
        else:
            subq = (
                f"SELECT DISTINCT concept_id FROM concept_tags "
                f"WHERE tag IN ({placeholders})"
            )
            params = list(parts)

        return subq, params

    def search_concepts(self, query=None,
                        tag_expr: str | None = None) -> list[Concept]:
        """按 alias、disclosure 或 content 模糊搜索 concept，可选按 tag 过滤。

        输入：query —— 文本子串（None = 不按文本过滤）；
              tag_expr —— tag 过滤表达式（"A & B" = AND, "A | B" = OR）。
              两者都给取交集；两者都不给报错。
        输出：命中的 Concept 列表。
        典型用法：goal 选单 = search_concepts(tag_expr='result')。
        """
        if query is None and not tag_expr:
            raise ValueError("Provide a search query, a tag, or both.")
        joins = []
        conditions = []
        parameters: list = []
        if tag_expr:
            subq, params = self._build_tag_filter_subquery(tag_expr)
            joins.append(f"JOIN ({subq}) ct_filter ON c.id = ct_filter.concept_id")
            parameters.extend(params)
        if query is not None:
            joins.extend([
                "LEFT JOIN aliases a ON c.id = a.concept_id",
                "LEFT JOIN variations v ON c.id = v.concept_id",
            ])
            escaped = (query.replace("\\", "\\\\")
                       .replace("%", "\\%").replace("_", "\\_"))
            like = f"%{escaped}%"
            conditions.append(
                "(a.alias LIKE ? ESCAPE '\\' "
                "OR c.disclosure LIKE ? ESCAPE '\\' "
                "OR v.content LIKE ? ESCAPE '\\')")
            parameters.extend([like, like, like])
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = self.conn.execute(
            "SELECT DISTINCT c.* FROM concepts c "
            + " ".join(joins) + where,
            parameters,
        ).fetchall()
        return [Concept(**dict(row)) for row in rows]

    def get_all_concepts(self) -> list[Concept]:
        """获取所有 concept。"""
        rows = self.conn.execute("SELECT * FROM concepts ORDER BY id").fetchall()
        return [Concept(**dict(row)) for row in rows]

    def get_all_concepts_overview(
        self, tag_expr: str | None = None,
    ) -> list[dict]:
        """获取所有概念及变体表达式的概览（供 CLI 和前端展示用，无 N+1 问题）。
        tag_expr: 可选 tag 过滤表达式（"A & B" = AND, "A | B" = OR）。"""
        if tag_expr:
            subq, child_params = self._build_tag_filter_subquery(tag_expr)
            join_clause = f"JOIN ({subq}) ct_filter ON c.id = ct_filter.concept_id"

            rows = self.conn.execute(
                f"SELECT DISTINCT c.* FROM concepts c {join_clause} ORDER BY c.id", child_params
            ).fetchall()
            concepts = [Concept(**dict(row)) for row in rows]

            if not concepts:
                return []

            cids = [c.id for c in concepts]
            placeholders = ",".join("?" * len(cids))

            where_clause = f"WHERE concept_id IN ({placeholders})"
            mem_where_clause = f"WHERE cm.concept_id IN ({placeholders})"
            child_params = cids
        else:
            concepts = self.get_all_concepts()
            if not concepts:
                return []
            where_clause = ""
            mem_where_clause = ""
            child_params = []

        var_rows = self.conn.execute(
            f"SELECT concept_id, short_code, type, status FROM variations "
            f"{where_clause} ORDER BY concept_id, short_code", child_params
        ).fetchall()
        vars_by_cid: dict[int, list] = {}
        for r in var_rows:
            vars_by_cid.setdefault(r["concept_id"], []).append(r)

        mem_rows = self.conn.execute(
            f"SELECT cm.concept_id, cm.short_code, cm.order_index, c.name "
            f"FROM compose_members cm "
            f"JOIN concepts c ON cm.member_concept_id = c.id "
            f"{mem_where_clause} ORDER BY cm.order_index, cm.member_concept_id", child_params
        ).fetchall()

        mems_by_var: dict[tuple, list[str]] = {}
        for r in mem_rows:
            key = (r["concept_id"], r["short_code"])
            mems_by_var.setdefault(key, []).append(r["name"])

        tag_rows = self.conn.execute(
            f"SELECT concept_id, tag FROM concept_tags "
            f"{where_clause} ORDER BY concept_id, tag", child_params
        ).fetchall()
        tags_by_cid: dict[int, list[str]] = {}
        for r in tag_rows:
            tags_by_cid.setdefault(r["concept_id"], []).append(r["tag"])

        _OP = {"CHAIN": " → ", "AND": " & ", "OR": " | "}

        result = []
        for c in concepts:
            c_dict = {
                "id": c.id,
                "name": c.name,
                "disclosure": c.disclosure,
                "tags": tags_by_cid.get(c.id, []),
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

    # ── Expression & composition helpers ─────────────────────────────────────

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

    # ── Directed relations ───────────────────────────────────────────────────

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

    # ── read_concept ─────────────────────────────────────────────────────────

    def read_concept(self, concept) -> ReadResult:
        """读取概念的完整视图。"""
        cid, _ = self._resolve_id(concept)
        row = self.conn.execute(
            "SELECT * FROM concepts WHERE id = ?", (cid,)
        ).fetchone()

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

        aliases = [
            ar["alias"] for ar in self.conn.execute(
                "SELECT alias FROM aliases WHERE concept_id = ?", (cid,)
            ).fetchall()
        ]

        tags = [
            tr["tag"] for tr in self.conn.execute(
                "SELECT tag FROM concept_tags WHERE concept_id = ? "
                "ORDER BY tag", (cid,)
            ).fetchall()
        ]

        tag_source_info = None
        ts_row = self.conn.execute(
            "SELECT t.name, COUNT(ct.concept_id) AS cnt "
            "FROM tags t "
            "LEFT JOIN concept_tags ct ON t.name = ct.tag AND ct.concept_id != ? "
            "WHERE t.source_concept_id = ? "
            "GROUP BY t.name",
            (cid, cid),
        ).fetchone()
        if ts_row:
            tag_source_info = (
                f"This concept's name is registered as tag "
                f"'{ts_row['name']}'. "
                f"Currently used by {ts_row['cnt']} other concept(s).")

        reminders = [
            ReminderDetail(
                id=r["id"],
                condition=r["condition"],
                message=r["message"],
                created_at=r["created_at"],
                last_fired_at=r["last_fired_at"],
            )
            for r in self.conn.execute(
                "SELECT * FROM reminders WHERE concept_id = ? ORDER BY id",
                (cid,),
            ).fetchall()
        ]

        inbound = self._query_inbound_relations(cid)
        outbound = self._query_outbound_relations(cid)

        return ReadResult(
            id=cid,
            name=row["name"],
            disclosure=row["disclosure"],
            aliases=aliases,
            tags=tags,
            tag_source_info=tag_source_info,
            reminders=reminders,
            variations=variations,
            inbound_confirmed=inbound["confirmed"],
            inbound_negated=inbound["negated"],
            inbound_hypotheses=inbound["hypothesis"],
            outbound_confirmed=outbound["confirmed"],
            outbound_negated=outbound["negated"],
            outbound_hypotheses=outbound["hypothesis"],
        )
