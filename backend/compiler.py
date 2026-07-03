"""Proof search for Horon expressions (CHAIN / AND / OR).

Each variation type defines a distinct traversal rule:
  CHAIN: Ordered sequence.  positions[0] is the prerequisite; later
         positions are emitted in order; the owning concept is emitted last.
  AND:   All members in positions[0] must be reached (conjunction).
  OR:    Any member in positions[0] suffices (disjunction).
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Callable, NamedTuple


@dataclass(frozen=True)
class ExpressionRule:
    concept_id: int
    short_code: str
    status: str
    type: str                              # 'CHAIN', 'AND', 'OR'
    positions: tuple[frozenset[int], ...]

    @property
    def key(self) -> tuple[int, str]:
        return (self.concept_id, self.short_code)


@dataclass(frozen=True)
class RelationGraph:
    """The complete stored expressions, without compiler-only indexes."""

    expressions: tuple[ExpressionRule, ...]


class _PathCost(NamedTuple):
    hypothesis_count: int | float
    total_jumps: int | float


@dataclass(frozen=True)
class _Proof:
    """One non-duplicated causal action sequence and its occurrence stream."""

    actions: tuple[tuple[int, str], ...]
    supports: tuple[tuple[int, str], ...]
    events: tuple[int, ...]
    met_constraints: frozenset[int]


class Compiler:
    def __init__(
        self,
        graph: RelationGraph,
        resolve_concept_name: Callable[[int], str],
        block: frozenset[int] = frozenset(),
    ) -> None:
        self.graph = graph
        self._resolve_concept_name = resolve_concept_name
        self._block = block
        self._rules: dict[tuple[int, str], ExpressionRule] = {}
        for rule in graph.expressions:
            if rule.concept_id in block:
                continue
            if any(m in block for pos in rule.positions for m in pos):
                continue
            self._rules[rule.key] = rule
        pure_groups: dict[int, list[tuple[int, str]]] = {}
        for rule in self._rules.values():
            if rule.type == "AND" and rule.status != "negated":
                pure_groups.setdefault(rule.concept_id, []).append(rule.key)
        self._pure_groups = {
            concept_id: tuple(keys)
            for concept_id, keys in pure_groups.items()
        }

    def _cost(self, proof: _Proof) -> _PathCost:
        keys = tuple(dict.fromkeys((*proof.actions, *proof.supports)))
        return _PathCost(
            sum(self._rules[key].status != "confirmed" for key in keys),
            len(proof.actions),
        )

    @staticmethod
    def _merge_actions(proofs: tuple[_Proof, ...]) -> tuple[tuple[int, str], ...]:
        """Merge parent DAGs in the requested order, deduplicating prefixes."""
        seen: set[tuple[int, str]] = set()
        merged: list[tuple[int, str]] = []
        for proof in proofs:
            for key in proof.actions:
                if key not in seen:
                    seen.add(key)
                    merged.append(key)
        return tuple(merged)

    def _merge_parent_orders(
        self,
        assume_events: tuple[int, ...],
        proofs: tuple[_Proof, ...],
        constraints: frozenset[int],
    ) -> tuple[tuple[tuple[int, str], ...], ...]:
        """Return the meaningfully distinct orders of prerequisite proofs.

        States that have selected the same parents, contain the same
        actions, and have met the same constraints are interchangeable.
        """
        if len(proofs) < 2:
            return (self._merge_actions(proofs),)

        initial_met = frozenset(assume_events) & constraints
        states: dict[
            tuple[int, frozenset[tuple[int, str]], frozenset[int]],
            tuple[tuple[int, str], ...],
        ] = {(0, frozenset(), initial_met): ()}
        full_mask = (1 << len(proofs)) - 1

        for _ in proofs:
            next_states: dict[
                tuple[int, frozenset[tuple[int, str]], frozenset[int]],
                tuple[tuple[int, str], ...],
            ] = {}
            for (mask, action_set, _), actions in states.items():
                for index, proof in enumerate(proofs):
                    bit = 1 << index
                    if mask & bit:
                        continue
                    merged = list(actions)
                    merged.extend(
                        action for action in proof.actions
                        if action not in action_set
                    )
                    merged_actions = tuple(merged)
                    merged_set = frozenset((*action_set, *proof.actions))
                    events, _ = self._events_for_actions(
                        assume_events, merged_actions, constraints)
                    met = frozenset(events) & constraints
                    key = (mask | bit, merged_set, met)
                    next_states.setdefault(key, merged_actions)
            states = next_states

        return tuple(
            actions
            for (mask, _, _), actions in states.items()
            if mask == full_mask
        )

    def _events_for_actions(
        self,
        assume_events: tuple[int, ...],
        actions: tuple[tuple[int, str], ...],
        constraints: frozenset[int],
    ) -> tuple[tuple[int, ...], tuple[tuple[int, str], ...]]:
        events: list[int] = list(assume_events)
        supports: list[tuple[int, str]] = []
        seen_supports: set[tuple[int, str]] = set()
        for key in actions:
            rule = self._rules[key]
            for position in rule.positions[1:]:
                for concept_id in self._order_position(
                        position, events, constraints):
                    support = self._append_arrival(
                        events, concept_id, constraints)
                    if (support is not None
                            and support not in seen_supports
                            and support not in actions):
                        seen_supports.add(support)
                        supports.append(support)
            self._append_arrival(events, rule.concept_id, constraints,
                                 expand_container=False)
        return tuple(events), tuple(supports)

    def _append_arrival(
        self,
        events: list[int],
        concept_id: int,
        constraints: frozenset[int],
        *,
        expand_container: bool = True,
    ) -> tuple[int, str] | None:
        """Expand AND containers when arriving, emitting unmet constraint members."""
        selected_key: tuple[int, str] | None = None
        if expand_container and concept_id in self._pure_groups:
            existing = set(events)
            met = frozenset(events) & constraints
            unmet = constraints - met
            best_match: tuple[int, ...] = ()
            best_rank: tuple[int, int, tuple[int, str]] | None = None
            for key in self._pure_groups[concept_id]:
                members = self._rules[key].positions[0]
                candidate_members = [
                    m for m in members if m in unmet and m not in existing
                ]
                candidate = tuple(candidate_members)
                if not candidate or not members <= existing | set(candidate):
                    continue
                rank = (
                    -len(candidate),
                    0 if self._rules[key].status == "confirmed" else 1,
                    key,
                )
                if best_rank is None or rank < best_rank:
                    best_rank = rank
                    best_match = candidate
                    selected_key = key
            events.extend(best_match)
        events.append(concept_id)
        return selected_key

    @staticmethod
    def _order_position(
        members: frozenset[int],
        prior_events: list[int],
        constraints: frozenset[int],
    ) -> tuple[int, ...]:
        """Choose a deterministic member order that prioritises unmet constraints."""
        met = frozenset(prior_events) & constraints
        unmet = constraints - met
        priority = sorted(m for m in members if m in unmet)
        rest = sorted(m for m in members if m not in unmet)
        return tuple(priority + rest)

    def _search(
        self,
        assume: frozenset[int],
        constraints: frozenset[int],
        goal: int,
    ) -> tuple[
        dict[int, dict[tuple[frozenset[tuple[int, str]], frozenset[int]], _Proof]],
        _Proof | None,
    ]:
        """Compute proofs until fixed point, searching for goal with constraints."""
        assume_events = tuple(sorted(assume))
        initial_met = frozenset(assume_events) & constraints

        labels: dict[
            int,
            dict[tuple[frozenset[tuple[int, str]], frozenset[int]], _Proof],
        ] = {}
        for aid in assume:
            proof = _Proof((), (), assume_events, initial_met)
            labels.setdefault(aid, {})[(frozenset(), initial_met)] = proof

        best_complete: _Proof | None = None
        if goal in assume and constraints <= initial_met:
            best_complete = _Proof((), (), assume_events, initial_met)
            return labels, best_complete

        changed = True
        while changed:
            changed = False
            for rule in self._rules.values():
                if rule.status == "negated":
                    continue
                prerequisites = tuple(sorted(rule.positions[0]))

                if rule.type == "OR":
                    reachable = [m for m in prerequisites if m in labels]
                    if not reachable:
                        continue
                    candidate_selections: list[tuple[_Proof, ...]] = []
                    for member in reachable:
                        for proof in labels[member].values():
                            candidate_selections.append((proof,))
                else:
                    if not all(member in labels for member in prerequisites):
                        continue
                    choices = [tuple(labels[member].values())
                               for member in prerequisites]
                    candidate_selections = list(itertools.product(*choices))

                for selected in candidate_selections:
                    if any(rule.key in proof.actions for proof in selected):
                        continue
                    orders = self._merge_parent_orders(
                        assume_events, tuple(selected), constraints)
                    for actions in orders:
                        if rule.key in actions:
                            continue
                        actions = (*actions, rule.key)
                        action_set = frozenset(actions)
                        events, supports = self._events_for_actions(
                            assume_events, actions, constraints)
                        met = frozenset(events) & constraints
                        proof = _Proof(
                            actions,
                            supports,
                            events,
                            met,
                        )

                        produced = {rule.concept_id}
                        for position in rule.positions[1:]:
                            produced.update(position)
                        for concept_id in tuple(produced):
                            for group_key in self._pure_groups.get(
                                    concept_id, ()):
                                group = self._rules[group_key].positions[0]
                                if group <= set(events):
                                    produced.update(group)
                        for concept_id in produced:
                            node_labels = labels.setdefault(concept_id, {})
                            label_key = (action_set, met)
                            previous = node_labels.get(label_key)
                            if previous is None:
                                node_labels[label_key] = proof
                                changed = True

                        if goal in produced and constraints <= met:
                            if (best_complete is None
                                    or self._cost(proof)
                                    < self._cost(best_complete)):
                                best_complete = proof
        return labels, best_complete

    def _route_for(self, proof: _Proof) -> list[dict]:
        route: list[dict] = []
        report_keys = list(proof.actions)
        report_keys.extend(
            key for key in proof.supports
            if (key not in proof.actions
                and self._rules[key].status != "confirmed")
        )
        for key in report_keys:
            rule = self._rules[key]
            from_ids = tuple(sorted(rule.positions[0]))
            if rule.type == "CHAIN":
                segments = []
                for i in range(len(rule.positions) - 1):
                    seg_from = tuple(sorted(rule.positions[i]))
                    seg_to = tuple(sorted(rule.positions[i + 1]))
                    segments.append({
                        "from": {
                            "concept_id": seg_from[0],
                            "name": self._resolve_concept_name(seg_from[0]),
                        },
                        "to": {
                            "concept_id": seg_to[0],
                            "name": self._resolve_concept_name(seg_to[0]),
                        },
                    })
                from_ids = tuple(sorted(rule.positions[0]))
                final_ids = tuple(sorted(rule.positions[-1]))
                route.append({
                    "concept_id": rule.concept_id,
                    "short_code": rule.short_code,
                    "name": self._resolve_concept_name(rule.concept_id),
                    "type": rule.type,
                    "status": rule.status,
                    "from": {
                        "concept_ids": list(from_ids),
                        "name": self._resolve_concept_name(from_ids[0]),
                    },
                    "to": {
                        "concept_id": final_ids[0],
                        "name": self._resolve_concept_name(final_ids[0]),
                    },
                    "segments": segments,
                })
            else:
                from_ids = tuple(sorted(rule.positions[0]))
                final_ids = tuple(sorted(rule.positions[-1]))
                from_sep = " | " if rule.type == "OR" else " & "
                route.append({
                    "concept_id": rule.concept_id,
                    "short_code": rule.short_code,
                    "name": self._resolve_concept_name(rule.concept_id),
                    "type": rule.type,
                    "status": rule.status,
                    "from": {
                        "concept_ids": list(from_ids),
                        "name": from_sep.join(
                            self._resolve_concept_name(cid)
                            for cid in from_ids),
                    },
                    "to": {
                        "concept_id": (rule.concept_id if rule.type != "CHAIN"
                                       else final_ids[0]),
                        "name": (self._resolve_concept_name(rule.concept_id)
                                 if rule.type != "CHAIN"
                                 else self._resolve_concept_name(final_ids[0])),
                    },
                    "segments": None,
                })
        return route

    def compile(
        self,
        assume: set[int],
        constraints: set[int],
        goal: int,
        input_names: dict[int, str],
    ) -> dict:
        result: dict = {
            "passed": False,
            "compiled_route": [],
            "concept_order": [],
            "break": None,
            "detour": None,
            # 回带调用方传入的 --block 排除项（原样上交，供文案点名用）。
            "blocked": [
                {
                    "concept_id": cid,
                    "name": input_names.get(
                        cid, self._resolve_concept_name(cid)),
                }
                for cid in sorted(self._block)
            ],
            "errors": [],
        }

        labels, complete = self._search(
            frozenset(assume), frozenset(constraints), goal)

        if complete is not None:
            result["passed"] = True
            result["compiled_route"] = self._route_for(complete)
            result["concept_order"] = [
                {
                    "concept_id": cid,
                    "name": input_names.get(
                        cid, self._resolve_concept_name(cid)),
                }
                for cid in complete.events
            ]
            return result

        # compiled_route = 遵守约束下走得最远的「最佳部分路线」：满足约束最多，
        # 其次代价最小。可能没到 goal。
        all_proofs = {
            proof
            for node_labels in labels.values()
            for proof in node_labels.values()
        }
        best = (
            min(
                all_proofs,
                key=lambda p: (-len(p.met_constraints), self._cost(p)),
            )
            if all_proofs
            else None
        )
        # goal 是否「撇开约束就能到」。约束在搜索中只影响打分、不拦路，
        # 所以 goal 出现在 labels 里 == goal 本身可达。
        goal_reached = bool(labels.get(goal))
        
        goal_reachable_without_block = goal_reached
        if not goal_reached and self._block:
            unblocked_compiler = Compiler(self.graph, self._resolve_concept_name)
            unblocked_labels, _ = unblocked_compiler._search(
                frozenset(assume), frozenset(), goal)
            goal_reachable_without_block = bool(unblocked_labels.get(goal))

        if best is not None:
            result["compiled_route"] = self._route_for(best)
            result["concept_order"] = [
                {
                    "concept_id": cid,
                    "name": input_names.get(
                        cid, self._resolve_concept_name(cid)),
                }
                for cid in best.events
            ]

        unmet = frozenset(constraints) - (
            best.met_constraints if best else frozenset())
        result["break"] = {
            "goal_reached": goal_reached,
            "goal_reachable_without_block": goal_reachable_without_block,
            "unmet_constraints": [
                {
                    "concept_id": cid,
                    "name": input_names.get(
                        cid, self._resolve_concept_name(cid)),
                }
                for cid in sorted(unmet)
            ],
        }

        # goal 可达、只是被约束卡住 → 撤掉约束再搜一条到 goal 的兜底路线。
        # goal 本身就不可达时，撤约束也白搭，不必再搜。
        if goal_reached:
            _, detour = self._search(
                frozenset(assume), frozenset(), goal)
            if detour is not None:
                result["detour"] = self._route_for(detour)

        return result
