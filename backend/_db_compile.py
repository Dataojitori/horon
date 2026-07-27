"""Compile (path verification) mixin for HoronDB."""
from __future__ import annotations

from .compiler import Compiler, ExpressionRule, RelationGraph


class CompileMixin:
    """Load relation graph and run the compiler."""

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
