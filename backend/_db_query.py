"""Query / read mixin for HoronDB."""
from __future__ import annotations

import sqlite3
import struct
import math

from ._db_common import _now, transactional
from .embedding import get_embedding, EMBEDDING_DIMENSIONS
from .models import (
    Concept, VariationDetail, ComposeMemberDetail,
    DirectedRelation, RelationMember, ReadResult,
    ReminderDetail, DisclosureDetail, TransitionSuggestion,
    SearchMatch, ConceptSearchResult,
)

_DECAY_GAMMA = 0.95
_PRUNING_THRESHOLD = 0.05


class QueryMixin:
    """Search, read, expression lookup, directed-relation queries, and attention routing."""

    # ── Disclosure helpers ────────────────────────────────────────────────────

    def _get_disclosures(self, concept_id: int) -> list[DisclosureDetail]:
        rows = self.conn.execute(
            "SELECT id, text, created_at FROM disclosures "
            "WHERE concept_id = ? ORDER BY id",
            (concept_id,),
        ).fetchall()
        return [DisclosureDetail(id=r["id"], text=r["text"],
                                 created_at=r["created_at"]) for r in rows]

    def _get_disclosures_batch(
        self, concept_ids: list[int] | set[int],
    ) -> dict[int, list[DisclosureDetail]]:
        if not concept_ids:
            return {}
        ids = list(concept_ids)
        result: dict[int, list[DisclosureDetail]] = {cid: [] for cid in ids}
        
        # SQLite has a hard limit on the number of host parameters (often 999).
        # We chunk the ids to avoid "sqlite3.OperationalError: too many SQL variables".
        chunk_size = 900
        for i in range(0, len(ids), chunk_size):
            chunk = ids[i:i + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT id, concept_id, text, created_at FROM disclosures "
                f"WHERE concept_id IN ({placeholders}) ORDER BY concept_id, id",
                tuple(chunk),
            ).fetchall()
            for r in rows:
                result[r["concept_id"]].append(
                    DisclosureDetail(id=r["id"], text=r["text"],
                                     created_at=r["created_at"]))
        return result

    # ── Tag filter ────────────────────────────────────────────────────────────

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

    @staticmethod
    def _make_snippet(text: str, query: str,
                      context_chars: int = 40) -> str:
        """Extract a text snippet around the first occurrence of *query*."""
        pos = text.lower().find(query.lower())
        if pos == -1:
            return text[:80] + ("..." if len(text) > 80 else "")
        start = max(0, pos - context_chars)
        end = min(len(text), pos + len(query) + context_chars)
        snippet = text[start:end]
        if start > 0:
            snippet = "..." + snippet
        if end < len(text):
            snippet = snippet + "..."
        return snippet

    def search_concepts(
        self, query: str, tag_expr: str | None = None, limit: int | None = 50,
    ) -> list[ConceptSearchResult]:
        """按 alias、disclosure 或 content 模糊搜索 concept，可選按 tag 過濾。

        返回 ConceptSearchResult（concept 身份 + 命中字段明細）。
        query 為必需的非空字符串；可選 limit 限制返回数量（默认 50）。
        """
        if not query or not query.strip():
            raise ValueError(
                "search_concepts requires a non-empty query string. "
                "Use list_concepts [--tag ...] to browse concepts."
            )
        query = query.strip()

        tag_join = ""
        tag_params: list = []
        if tag_expr:
            subq, params = self._build_tag_filter_subquery(tag_expr)
            tag_join = f"JOIN ({subq}) ct_filter ON c.id = ct_filter.concept_id"
            tag_params = params

        # ── Single-pass SQL UNION ALL search for match attribution ──
        escaped = (query.replace("\\", "\\\\")
                   .replace("%", "\\%").replace("_", "\\_"))
        like = f"%{escaped}%"

        sql = f"""
        WITH target_concepts AS (
            SELECT DISTINCT c.id, c.name FROM concepts c
            {tag_join}
        )
        SELECT tc.id AS concept_id, tc.name AS concept_name, 'name' AS field, NULL AS target_id, tc.name AS text
        FROM target_concepts tc
        WHERE tc.name LIKE ? ESCAPE '\\'

        UNION ALL

        SELECT tc.id AS concept_id, tc.name AS concept_name, 'alias' AS field, NULL AS target_id, a.alias AS text
        FROM target_concepts tc
        JOIN aliases a ON tc.id = a.concept_id
        WHERE a.alias LIKE ? ESCAPE '\\' AND a.alias != tc.name

        UNION ALL

        SELECT tc.id AS concept_id, tc.name AS concept_name, 'disclosure' AS field, CAST(d.id AS TEXT) AS target_id, d.text AS text
        FROM target_concepts tc
        JOIN disclosures d ON tc.id = d.concept_id
        WHERE d.text LIKE ? ESCAPE '\\'

        UNION ALL

        SELECT tc.id AS concept_id, tc.name AS concept_name, 'variation' AS field, v.short_code AS target_id, v.content AS text
        FROM target_concepts tc
        JOIN variations v ON tc.id = v.concept_id
        WHERE v.content LIKE ? ESCAPE '\\' AND v.content IS NOT NULL

        ORDER BY concept_id
        """

        if limit is not None and limit > 0:
            sql = f"""
            WITH raw AS ({sql.strip()}),
            top_concepts AS (
                SELECT DISTINCT concept_id FROM raw ORDER BY concept_id LIMIT ?
            )
            SELECT raw.* FROM raw
            JOIN top_concepts tc ON raw.concept_id = tc.concept_id
            ORDER BY raw.concept_id
            """
            sql_params = tag_params + [like, like, like, like, limit]
        else:
            sql_params = tag_params + [like, like, like, like]

        rows = self.conn.execute(sql, sql_params).fetchall()

        results_by_cid: dict[int, ConceptSearchResult] = {}
        for r in rows:
            cid = r["concept_id"]
            cname = r["concept_name"]
            field = r["field"]
            target_id = r["target_id"]
            text = r["text"]

            if cid not in results_by_cid:
                if limit is not None and limit > 0 and len(results_by_cid) >= limit:
                    continue
                results_by_cid[cid] = ConceptSearchResult(
                    concept_id=cid, concept_name=cname, matches=[]
                )

            snippet = text if field in ("name", "alias") else self._make_snippet(text, query)
            results_by_cid[cid].matches.append(
                SearchMatch(
                    field=field,
                    target_id=str(target_id) if target_id is not None else None,
                    snippet=snippet,
                )
            )

        return list(results_by_cid.values())



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
            # Use the subquery directly to avoid SQLite's parameter limits (too many SQL variables)
            where_clause = f"WHERE concept_id IN ({subq})"
            mem_where_clause = f"WHERE cm.concept_id IN ({subq})"
        else:
            concepts = self.get_all_concepts()
            if not concepts:
                return []
            cids = [c.id for c in concepts]
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

        disc_map = self._get_disclosures_batch(cids)

        _OP = {"CHAIN": " → ", "AND": " & ", "OR": " | "}

        result = []
        for c in concepts:
            c_dict = {
                "id": c.id,
                "name": c.name,
                "disclosures": [d.model_dump() for d in disc_map.get(c.id, [])],
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

        单条 JOIN 一次取出 (variation, status, 另一端成员名)，随后批量加载
        成员的 disclosures，避免逐行回查；表达式字符串按 variation 缓存，
        不重复构建。
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
            "  m.name             AS member_name "
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
        member_cids: set[int] = set()

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
            member_cids.add(row["member_id"])
            relation.members.append(RelationMember(
                concept_id=row["member_id"],
                concept_name=row["member_name"],
                disclosures=[],
            ))

        if member_cids:
            disc_map = self._get_disclosures_batch(member_cids)
            for relation in relations.values():
                for member in relation.members:
                    member.disclosures = disc_map.get(member.concept_id, [])

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

        disclosures = self._get_disclosures(cid)

        var_rows = self.conn.execute(
            "SELECT * FROM variations WHERE concept_id = ? "
            "ORDER BY short_code",
            (cid,),
        ).fetchall()
        member_rows = self.conn.execute(
            "SELECT cm.short_code, cm.member_concept_id AS concept_id, c.name, "
            "       cm.order_index "
            "FROM compose_members cm "
            "JOIN concepts c ON cm.member_concept_id = c.id "
            "WHERE cm.concept_id = ? "
            "ORDER BY cm.order_index, cm.member_concept_id",
            (cid,),
        ).fetchall()

        member_cids: set[int] = set()
        members_raw_by_sc: dict[str, list[dict]] = {}
        for mr in member_rows:
            d = dict(mr)
            sc = d.pop("short_code")
            member_cids.add(d["concept_id"])
            members_raw_by_sc.setdefault(sc, []).append(d)

        member_disc_map = self._get_disclosures_batch(member_cids)

        variations: list[VariationDetail] = []
        for vr in var_rows:
            expr = self._get_expression(cid, vr["short_code"])
            members = [
                ComposeMemberDetail(
                    concept_id=m["concept_id"],
                    name=m["name"],
                    order_index=m["order_index"],
                    disclosures=member_disc_map.get(m["concept_id"], []),
                )
                for m in members_raw_by_sc.get(vr["short_code"], [])
            ]
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
        suggested_next = self._get_suggested_transitions(cid)

        return ReadResult(
            id=cid,
            name=row["name"],
            disclosures=disclosures,
            aliases=aliases,
            tags=tags,
            tag_source_info=tag_source_info,
            reminders=reminders,
            variations=variations,
            suggested_next=suggested_next,
            inbound_confirmed=inbound["confirmed"],
            inbound_negated=inbound["negated"],
            inbound_hypotheses=inbound["hypothesis"],
            outbound_confirmed=outbound["confirmed"],
            outbound_negated=outbound["negated"],
            outbound_hypotheses=outbound["hypothesis"],
        )

    # ── Intent-based vector search ──────────────────────────────────────────



    def search_by_intent(
        self, intent_text: str, limit: int = 10,
    ) -> list[dict]:
        """Search disclosures by semantic similarity to an intent string.

        Computes the intent's embedding, then ranks all disclosures that have
        stored embeddings by cosine similarity. Returns top-N results with
        concept info.
        """
        query_vec = get_embedding(intent_text)
        if query_vec is None:
            raise RuntimeError(
                "Failed to compute embedding for intent query. "
                "Check OPENROUTER_API_KEY and network connectivity.")

        rows = self.conn.execute(
            "SELECT d.id, d.concept_id, d.text, d.embedding, c.name "
            "FROM disclosures d "
            "JOIN concepts c ON d.concept_id = c.id "
            "WHERE d.embedding IS NOT NULL"
        ).fetchall()

        scored: list[tuple[float, dict]] = []
        for r in rows:
            try:
                blob = r["embedding"]
                stored_vec = struct.unpack(f'<{len(blob)//4}f', blob)
                if len(stored_vec) != EMBEDDING_DIMENSIONS:
                    continue
                sim = sum(x * y for x, y in zip(query_vec, stored_vec))
            except (TypeError, ValueError, struct.error):
                continue
            scored.append((sim, {
                "disclosure_id": r["id"],
                "concept_id": r["concept_id"],
                "concept_name": r["name"],
                "disclosure_text": r["text"],
                "similarity": round(sim, 4),
            }))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored[:limit]]

    # ── Attention routing ────────────────────────────────────────────────────

    def _get_suggested_transitions(
        self, concept_id: int, limit: int = 10,
    ) -> list[TransitionSuggestion]:
        rows = self.conn.execute(
            "SELECT ct.to_concept_id, c.name, ct.weight "
            "FROM concept_transitions ct "
            "JOIN concepts c ON ct.to_concept_id = c.id "
            "WHERE ct.from_concept_id = ? "
            "ORDER BY ct.weight DESC LIMIT ?",
            (concept_id, limit),
        ).fetchall()
        return [
            TransitionSuggestion(
                concept_id=r["to_concept_id"],
                concept_name=r["name"],
                weight=round(r["weight"], 4),
            )
            for r in rows
        ]

    @transactional
    def record_transition(self, to_id: int) -> None:
        """Record an attention transition A → B with surprise-weighted update.

        Traces back up to 3 recent distinct successful reads. The most recent
        read has relevance 1.0, the second 0.5, and the third 0.25.
        
        Algorithm for each traced 'from_id' (order matters):
        1. Decay all outgoing weights from A by γ (0.95)
        2. Prune dead edges (weight < 0.05)
        3. P' = max(w₀, 0.05) / (W_total + 1.0)   — smoothed prior
        4. I  = -log₂(P')                         — surprise
        5. new_weight = w₀ + (I * relevance)
        """
        recent_reads = self.conn.execute(
            "SELECT concept_id, timestamp FROM cli_audit_log "
            "WHERE command = 'read_concept' AND success = 1 "
            "ORDER BY id DESC LIMIT 10"
        ).fetchall()
        
        if not recent_reads:
            return
            
        import datetime
        def _parse_ts(ts_str: str) -> datetime.datetime:
            clean = str(ts_str).replace("T", " ").split(".")[0]
            return datetime.datetime.strptime(clean, "%Y-%m-%d %H:%M:%S")

        now_time = _parse_ts(_now())
        last_time = now_time
        
        distinct_from_ids = []
        for row in recent_reads:
            cid = row["concept_id"]
            row_time = _parse_ts(row["timestamp"])
            
            # If the gap between this read and the next (or current) read is > 30 mins, break the chain
            if (last_time - row_time).total_seconds() > 30 * 60:
                break
                
            last_time = row_time
            
            if cid not in distinct_from_ids:
                distinct_from_ids.append(cid)
            if len(distinct_from_ids) == 3:
                break
                
        now = _now()
        for i, from_id in enumerate(distinct_from_ids):
            if from_id == to_id:
                continue
                
            exists = self.conn.execute(
                "SELECT 1 FROM concepts WHERE id = ?", (from_id,)
            ).fetchone()
            if not exists:
                continue
                
            relevance = 0.5 ** i
            
            self.conn.execute(
                "UPDATE concept_transitions SET weight = weight * ? "
                "WHERE from_concept_id = ?",
                (_DECAY_GAMMA, from_id),
            )
            
            self.conn.execute(
                "DELETE FROM concept_transitions "
                "WHERE from_concept_id = ? AND weight < ?",
                (from_id, _PRUNING_THRESHOLD),
            )
            
            row = self.conn.execute(
                "SELECT weight FROM concept_transitions "
                "WHERE from_concept_id = ? AND to_concept_id = ?",
                (from_id, to_id),
            ).fetchone()
            w0 = row["weight"] if row else 0.0

            total_row = self.conn.execute(
                "SELECT COALESCE(SUM(weight), 0.0) AS total "
                "FROM concept_transitions WHERE from_concept_id = ?",
                (from_id,),
            ).fetchone()
            w_total = total_row["total"]

            p_prime = max(w0, _PRUNING_THRESHOLD) / (w_total + 1.0)
            surprise = -math.log2(p_prime)
            new_weight = w0 + (surprise * relevance)

            self.conn.execute(
                "INSERT INTO concept_transitions "
                "(from_concept_id, to_concept_id, weight, last_accessed_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(from_concept_id, to_concept_id) "
                "DO UPDATE SET weight = ?, last_accessed_at = ?",
                (from_id, to_id, new_weight, now, new_weight, now),
            )
