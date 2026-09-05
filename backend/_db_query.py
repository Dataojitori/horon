"""Query / read mixin for HoronDB."""
from __future__ import annotations

import math
from typing import Any, Literal

import numpy as np

from ._db_common import _now, transactional, OFFLINE_DEV_SESSION_ID
from .embedding import get_embedding, EMBEDDING_DIMENSIONS
from .models import (
    Concept, ComposeMemberDetail, ReadResult,
    ReminderDetail, TransitionSuggestion,
    SearchMatch, ConceptSearchResult, SensorHookDetail, ToolGuardDetail, InhibitionDetail,
)

_DECAY_GAMMA = 0.95
_PRUNING_THRESHOLD = 0.05


class QueryMixin:
    """Search, read, expression lookup, directed-relation queries, and attention routing."""

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
                "Use one operator per --tag expression."
            )

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
    def _make_snippet(text: str, query: str, context_chars: int = 40) -> str:
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
            SELECT DISTINCT c.id, c.name, c.disclosure, c.content FROM concepts c
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

        SELECT tc.id AS concept_id, tc.name AS concept_name, 'disclosure' AS field, NULL AS target_id, tc.disclosure AS text
        FROM target_concepts tc
        WHERE tc.disclosure LIKE ? ESCAPE '\\' AND tc.disclosure IS NOT NULL

        UNION ALL

        SELECT tc.id AS concept_id, tc.name AS concept_name, 'content' AS field, NULL AS target_id, tc.content AS text
        FROM target_concepts tc
        WHERE tc.content LIKE ? ESCAPE '\\' AND tc.content IS NOT NULL

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

            where_clause = f"WHERE concept_id IN ({subq})"
            mem_where_clause = f"WHERE cm.parent_concept_id IN ({subq})"
        else:
            concepts = self.get_all_concepts()
            if not concepts:
                return []
            where_clause = ""
            mem_where_clause = ""
            child_params = []

        cids = [c.id for c in concepts]

        mem_rows = self.conn.execute(
            f"SELECT cm.parent_concept_id AS concept_id, cm.order_index, c.name "
            f"FROM compose_members cm "
            f"JOIN concepts c ON cm.member_concept_id = c.id "
            f"{mem_where_clause} ORDER BY cm.parent_concept_id, cm.order_index, cm.member_concept_id", child_params
        ).fetchall()
        mems_by_cid: dict[int, list[str]] = {}
        for r in mem_rows:
            mems_by_cid.setdefault(r["concept_id"], []).append(r["name"])

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
            rule = None
            if c.activation_type and c.id in mems_by_cid:
                rule = _OP.get(c.activation_type, " & ").join(mems_by_cid[c.id])

            c_dict = {
                "id": c.id,
                "name": c.name,
                "content": c.content,
                "disclosure": c.disclosure,
                "role": c.role,
                "is_active": c.is_active,
                "lifespan": c.lifespan,
                "activation_type": c.activation_type,
                "activation_rule": rule,
                "on_fire": c.on_fire,
                "tags": tags_by_cid.get(c.id, []),
            }
            result.append(c_dict)

        return result

    # ── Activation Rule & composition helpers ─────────────────────────────────

    def _get_activation_rule(self, concept_id: int) -> str | None:
        """取概念的激活规则字符串。若无激活规则返回 None。"""
        row = self.conn.execute(
            "SELECT activation_type FROM concepts WHERE id = ?", (concept_id,)
        ).fetchone()
        if not row or not row["activation_type"]:
            return None

        vtype = row["activation_type"]
        rows = self.conn.execute(
            "SELECT c.name "
            "FROM compose_members cm "
            "JOIN concepts c ON cm.member_concept_id = c.id "
            "WHERE cm.parent_concept_id = ? "
            "ORDER BY cm.order_index",
            (concept_id,),
        ).fetchall()
        if not rows:
            return None

        names = [r["name"] for r in rows]
        op = {"CHAIN": " → ", "AND": " & ", "OR": " | "}
        return op.get(vtype, " & ").join(names)

    def _find_composition_concept(
        self, vtype: str, member_ids: list[int], role: str | None = None
    ) -> int | None:
        """找到持有某个组合的概念 ID。

        CHAIN 按有序列表比较；AND/OR 按排序后的集合比较（忽略顺序），
        且 activation_type 必须相同。可选按 role 过滤。
        """
        if not member_ids:
            return None

        total_members = len(member_ids)
        if vtype == "CHAIN":
            target_sig: tuple[int, ...] = tuple(member_ids)
        else:
            target_sig = tuple(sorted(member_ids))

        placeholders = ",".join("?" * total_members)
        role_clause = "AND c.role = ? " if role else ""
        params: list[Any] = [*member_ids, vtype]
        if role:
            params.append(role)
        params.append(total_members)

        candidates = self.conn.execute(
            f"SELECT cm.parent_concept_id AS concept_id "
            f"FROM compose_members cm "
            f"JOIN concepts c ON cm.parent_concept_id = c.id "
            f"WHERE cm.member_concept_id IN ({placeholders}) "
            f"AND c.activation_type = ? "
            f"{role_clause}"
            f"GROUP BY cm.parent_concept_id "
            f"HAVING COUNT(*) = ?",
            params,
        ).fetchall()

        for cand in candidates:
            rows = self.conn.execute(
                "SELECT member_concept_id FROM compose_members "
                "WHERE parent_concept_id = ? "
                "ORDER BY order_index",
                (cand["concept_id"],),
            ).fetchall()
            if len(rows) != total_members:
                continue
            cand_ids = [r["member_concept_id"] for r in rows]
            if vtype == "CHAIN":
                cand_sig = tuple(cand_ids)
            else:
                cand_sig = tuple(sorted(cand_ids))
            if cand_sig == target_sig:
                return cand["concept_id"]
        return None

    # ── Compose members ──────────────────────────────────────────────────────

    def _get_compose_members(self, concept_id: int) -> list[ComposeMemberDetail]:
        rows = self.conn.execute(
            "SELECT c.id AS concept_id, c.name, c.disclosure, c.is_active, c.role, cm.order_index "
            "FROM compose_members cm "
            "JOIN concepts c ON cm.member_concept_id = c.id "
            "WHERE cm.parent_concept_id = ? "
            "ORDER BY cm.order_index, cm.member_concept_id",
            (concept_id,),
        ).fetchall()
        return [
            ComposeMemberDetail(
                concept_id=r["concept_id"],
                name=r["name"],
                order_index=r["order_index"],
                disclosure=r["disclosure"],
                is_active=r["is_active"],
                role=r["role"],
            )
            for r in rows
        ]

    def _get_sensor_hooks(self, concept_id: int) -> list[SensorHookDetail]:
        rows = self.conn.execute(
            "SELECT id, sensor_concept_id, event_type, tool, match_pattern, created_at "
            "FROM sensor_hooks WHERE sensor_concept_id = ? ORDER BY id",
            (concept_id,),
        ).fetchall()
        return [SensorHookDetail(**dict(r)) for r in rows]

    def _get_tool_guards(self, concept_id: int) -> list[ToolGuardDetail]:
        rows = self.conn.execute(
            "SELECT id, guard_concept_id, tool, args_pattern, created_at "
            "FROM tool_guards WHERE guard_concept_id = ? ORDER BY id",
            (concept_id,),
        ).fetchall()
        return [ToolGuardDetail(**dict(r)) for r in rows]

    def _get_inhibitions(
        self, concept_id: int, direction: Literal["incoming", "outgoing"] = "incoming"
    ) -> list[InhibitionDetail]:
        """查询概念的抑制关系。

        - direction="incoming": 谁抑制了当前概念 (inhibitor -> current)
        - direction="outgoing": 当前概念抑制了谁 (current -> target)
        """
        if direction == "incoming":
            filter_col = "i.target_concept_id"
            order_col = "i.inhibitor_concept_id"
        elif direction == "outgoing":
            filter_col = "i.inhibitor_concept_id"
            order_col = "i.target_concept_id"
        else:
            raise ValueError(f"Invalid direction '{direction}'. Use 'incoming' or 'outgoing'.")

        rows = self.conn.execute(
            f"SELECT i.target_concept_id, i.inhibitor_concept_id, "
            f"c_inh.name AS inhibitor_name, c_tgt.name AS target_name, "
            f"c_inh.is_active AS inhibitor_is_active, c_tgt.is_active AS target_is_active, "
            f"c_inh.role AS inhibitor_role, c_tgt.role AS target_role, "
            f"i.created_at "
            f"FROM inhibitions i "
            f"JOIN concepts c_inh ON i.inhibitor_concept_id = c_inh.id "
            f"JOIN concepts c_tgt ON i.target_concept_id = c_tgt.id "
            f"WHERE {filter_col} = ? ORDER BY {order_col}",
            (concept_id,),
        ).fetchall()
        return [InhibitionDetail(**dict(r)) for r in rows]

    # ── read_concept ─────────────────────────────────────────────────────────

    def read_concept(self, concept) -> ReadResult:
        """读取概念的完整视图。"""
        cid = self._resolve_id(concept)
        row = self.conn.execute(
            "SELECT * FROM concepts WHERE id = ?", (cid,)
        ).fetchone()
        if not row:
            raise ValueError(f"Concept not found: {concept}")

        activation_rule = self._get_activation_rule(cid)
        members = self._get_compose_members(cid)
        sensor_hooks = self._get_sensor_hooks(cid)
        tool_guards = self._get_tool_guards(cid)
        inhibitions = self._get_inhibitions(cid, direction="incoming")
        inhibiting = self._get_inhibitions(cid, direction="outgoing")

        active_chain_orders: list[int] = []
        if row["activation_type"] == "CHAIN":
            sess = self.get_current_session() or OFFLINE_DEV_SESSION_ID
            ac_rows = self.conn.execute(
                "SELECT current_order FROM active_chain_instances "
                "WHERE chain_concept_id = ? AND session_id = ? "
                "ORDER BY current_order",
                (cid, sess),
            ).fetchall()
            active_chain_orders = [r["current_order"] for r in ac_rows]

        aliases = [
            ar["alias"] for ar in self.conn.execute(
                "SELECT alias FROM aliases WHERE concept_id = ? ORDER BY alias", (cid,)
            ).fetchall()
        ]

        tags = [
            tr["tag"] for tr in self.conn.execute(
                "SELECT tag FROM concept_tags WHERE concept_id = ? ORDER BY tag", (cid,)
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
                f"Currently used by {ts_row['cnt']} other concept(s)."
            )

        reminders = [
            ReminderDetail(
                id=r["id"],
                condition=r["condition"],
                message=r["message"],
                created_at=r["created_at"],
                last_fired_at=r["last_fired_at"],
            )
            for r in self.conn.execute(
                "SELECT id, condition, message, created_at, last_fired_at "
                "FROM reminders WHERE concept_id = ? ORDER BY id",
                (cid,),
            ).fetchall()
        ]

        suggested_next = self._get_suggested_transitions(cid)

        return ReadResult(
            id=cid,
            name=row["name"],
            content=row["content"],
            disclosure=row["disclosure"],
            role=row["role"],
            is_active=row["is_active"],
            lifespan=row["lifespan"],
            activation_type=row["activation_type"],
            activation_rule=activation_rule,
            on_fire=row["on_fire"],
            aliases=aliases,
            tags=tags,
            tag_source_info=tag_source_info,
            reminders=reminders,
            members=members,
            sensor_hooks=sensor_hooks,
            tool_guards=tool_guards,
            inhibitions=inhibitions,
            inhibiting=inhibiting,
            active_chain_orders=active_chain_orders,
            suggested_next=suggested_next,
        )

    # ── Intent-based vector search ──────────────────────────────────────────

    def search_by_intent(
        self, intent_text: str, limit: int = 10,
    ) -> list[dict]:
        """Search concept disclosures by semantic similarity to an intent string.

        Computes the intent's embedding, then ranks all concepts that have
        stored disclosure embeddings by cosine similarity. Returns top-N results.
        """
        query_vec = get_embedding(intent_text)
        if query_vec is None:
            raise RuntimeError(
                "Failed to compute embedding for intent query. "
                "Check OPENROUTER_API_KEY and network connectivity."
            )

        rows = self.conn.execute(
            "SELECT c.id, c.name, c.disclosure, ce.embedding "
            "FROM concept_embeddings ce "
            "JOIN concepts c ON ce.concept_id = c.id "
            "WHERE c.disclosure IS NOT NULL AND c.disclosure != ''"
        ).fetchall()

        if not rows:
            return []

        expected_bytes = EMBEDDING_DIMENSIONS * 4
        valid_rows = []
        valid_blobs = []
        for r in rows:
            blob = r["embedding"]
            if isinstance(blob, (bytes, bytearray, memoryview)) and len(blob) == expected_bytes:
                valid_rows.append(r)
                valid_blobs.append(blob)

        if not valid_rows:
            return []

        matrix = np.frombuffer(b"".join(valid_blobs), dtype=np.float32).reshape(
            len(valid_rows), EMBEDDING_DIMENSIONS
        )
        q_vec = np.asarray(query_vec, dtype=np.float32)
        sims = matrix @ q_vec

        n_results = min(limit, len(valid_rows))
        if n_results <= 0:
            return []

        if len(sims) <= limit:
            top_indices = np.argsort(-sims)
        else:
            top_k_unsorted = np.argpartition(-sims, n_results)[:n_results]
            top_indices = top_k_unsorted[np.argsort(-sims[top_k_unsorted])]

        return [
            {
                "concept_id": valid_rows[i]["id"],
                "concept_name": valid_rows[i]["name"],
                "disclosure_text": valid_rows[i]["disclosure"],
                "similarity": round(float(sims[i]), 4),
            }
            for i in top_indices
        ]

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
        sess = self.get_current_session()
        if not sess:
            return

        recent_reads = self.conn.execute(
            "SELECT concept_id, timestamp FROM cli_audit_log "
            "WHERE command = 'read_concept' AND success = 1 AND session_id = ? "
            "ORDER BY id DESC LIMIT 10",
            (sess,),
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

            if cid == to_id:
                continue

            if cid not in distinct_from_ids:
                distinct_from_ids.append(cid)
            if len(distinct_from_ids) == 3:
                break

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
                "ON CONFLICT (from_concept_id, to_concept_id) "
                "DO UPDATE SET weight = ?, last_accessed_at = ?",
                (from_id, to_id, new_weight, _now(), new_weight, _now()),
            )
