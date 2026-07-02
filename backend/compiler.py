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
    consumed_steps: int


class Compiler:
    def __init__(
        self,
        graph: RelationGraph,
        resolve_concept_name: Callable[[int], str],
    ) -> None:
        self.graph = graph
        self._resolve_concept_name = resolve_concept_name
        self._rules = {rule.key: rule for rule in graph.expressions}
        pure_groups: dict[int, list[tuple[int, str]]] = {}
        for rule in graph.expressions:
            if rule.type == "AND" and rule.status != "negated":
                pure_groups.setdefault(rule.concept_id, []).append(rule.key)
        self._pure_groups = {
            concept_id: tuple(keys)
            for concept_id, keys in pure_groups.items()
        }

    @staticmethod
    def _consume(events: tuple[int, ...], steps: tuple[int, ...]) -> int:
        """Return the length of the ordered step prefix found in events.

        A future step appearing early is harmless: a later occurrence can
        still consume it.  Repeated concept IDs therefore work naturally.
        """
        index = 0
        for concept_id in events:
            if index < len(steps) and concept_id == steps[index]:
                index += 1
        return index

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
        start_id: int,
        proofs: tuple[_Proof, ...],
        steps: tuple[int, ...],
    ) -> tuple[tuple[tuple[int, str], ...], ...]:
        """Return the meaningfully distinct orders of prerequisite proofs.

        A position's members are unordered, but enumerating every parent
        permutation costs ``n!``.  Build the orders incrementally instead and
        merge states that have selected the same parents, contain the same
        actions, and have consumed the same ordered-step prefix.

        Such states are interchangeable for the remaining parents: merging a
        later proof appends the same not-yet-seen actions, and step matching
        only depends on the already-consumed prefix length.  Keeping one
        representative therefore removes duplicate work without choosing a
        greedy parent order or discarding a distinct waypoint outcome.
        """
        if len(proofs) < 2:
            return (self._merge_actions(proofs),)

        # (selected parent indexes, action set, consumed step count) -> order
        states: dict[
            tuple[int, frozenset[tuple[int, str]], int],
            tuple[tuple[int, str], ...],
        ] = {(0, frozenset(), self._consume((start_id,), steps)): ()}
        full_mask = (1 << len(proofs)) - 1

        for _ in proofs:
            next_states: dict[
                tuple[int, frozenset[tuple[int, str]], int],
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
                        start_id, merged_actions, steps)
                    consumed = self._consume(events, steps)
                    key = (mask | bit, merged_set, consumed)
                    next_states.setdefault(key, merged_actions)
            states = next_states

        return tuple(
            actions
            for (mask, _, _), actions in states.items()
            if mask == full_mask
        )

    def _events_for_actions(
        self,
        start_id: int,
        actions: tuple[tuple[int, str], ...],
        steps: tuple[int, ...],
    ) -> tuple[tuple[int, ...], tuple[tuple[int, str], ...]]:
        events: list[int] = [start_id]
        supports: list[tuple[int, str]] = []
        seen_supports: set[tuple[int, str]] = set()
        for key in actions:
            rule = self._rules[key]
            for position in rule.positions[1:]:
                for concept_id in self._order_position(position, events, steps):
                    support = self._append_arrival(events, concept_id, steps)
                    if (support is not None
                            and support not in seen_supports
                            and support not in actions):
                        seen_supports.add(support)
                        supports.append(support)
            self._append_arrival(events, rule.concept_id, steps,
                                 expand_container=False)
        return tuple(events), tuple(supports)

    def _append_arrival(
        self,
        events: list[int],
        concept_id: int,
        steps: tuple[int, ...],
        *,
        expand_container: bool = True,
    ) -> tuple[int, str] | None:
        """Expand requested members when an arrow arrives at an & container.

        This preserves Horon's existing rule that ``Start → N`` can
        concretely use adjacent steps ``A, B`` when ``N = A & B``.  Members
        already established earlier need not be emitted again.
        """
        selected_key: tuple[int, str] | None = None
        if expand_container and concept_id in self._pure_groups:
            existing = set(events)
            best_match: tuple[int, ...] = ()
            best_rank: tuple[int, int, tuple[int, str]] | None = None
            for key in self._pure_groups[concept_id]:
                members = self._rules[key].positions[0]
                index = self._consume(tuple(events), steps)
                matched: list[int] = []
                while index < len(steps) and steps[index] in members:
                    member = steps[index]
                    if member in matched:
                        break
                    matched.append(member)
                    index += 1
                candidate = tuple(matched)
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
        steps: tuple[int, ...],
    ) -> tuple[int, ...]:
        """Choose a deterministic member order that consumes most next steps."""
        consumed = Compiler._consume(tuple(prior_events), steps)
        remaining = list(members)
        ordered: list[int] = []
        while consumed < len(steps) and steps[consumed] in remaining:
            member = steps[consumed]
            ordered.append(member)
            remaining.remove(member)
            consumed += 1
        ordered.extend(sorted(remaining))
        return tuple(ordered)

    def _search(
        self,
        start_id: int,
        steps: tuple[int, ...],
    ) -> tuple[dict[int, dict[frozenset[tuple[int, str]], _Proof]], _Proof | None]:
        """Compute non-duplicate proofs until the finite expression closure."""
        empty = _Proof((), (), (start_id,), self._consume((start_id,), steps))
        labels: dict[int, dict[frozenset[tuple[int, str]], _Proof]] = {
            start_id: {frozenset(): empty},
        }

        best_complete: _Proof | None = None
        changed = True
        while changed:
            changed = False
            for rule in self.graph.expressions:
                if rule.status == "negated":
                    continue
                prerequisites = tuple(sorted(rule.positions[0]))

                if rule.type == "OR":
                    # OR: any single prerequisite being reached suffices.
                    reachable = [m for m in prerequisites if m in labels]
                    if not reachable:
                        continue
                    # Try each reachable member independently.
                    candidate_selections: list[tuple[_Proof, ...]] = []
                    for member in reachable:
                        for proof in labels[member].values():
                            candidate_selections.append((proof,))
                else:
                    # CHAIN / AND: all prerequisites must be reached.
                    if not all(member in labels for member in prerequisites):
                        continue
                    choices = [tuple(labels[member].values())
                               for member in prerequisites]
                    candidate_selections = list(itertools.product(*choices))

                for selected in candidate_selections:
                    if any(rule.key in proof.actions for proof in selected):
                        continue
                    orders = self._merge_parent_orders(
                        start_id, tuple(selected), steps)
                    for actions in orders:
                        if rule.key in actions:
                            continue
                        actions = (*actions, rule.key)
                        action_set = frozenset(actions)
                        events, supports = self._events_for_actions(
                            start_id, actions, steps)
                        proof = _Proof(
                            actions,
                            supports,
                            events,
                            self._consume(events, steps),
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
                            previous = node_labels.get(action_set)
                            if previous is None or proof.consumed_steps > previous.consumed_steps:
                                node_labels[action_set] = proof
                                changed = True

                        if proof.consumed_steps == len(steps):
                            if (best_complete is None
                                    or self._cost(proof) < self._cost(best_complete)):
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
                            self._resolve_concept_name(cid) for cid in from_ids),
                    },
                    "to": {
                        "concept_id": rule.concept_id if rule.type != "CHAIN" else final_ids[0],
                        "name": self._resolve_concept_name(rule.concept_id) if rule.type != "CHAIN" else self._resolve_concept_name(final_ids[0]),
                    },
                    "segments": None,
                })
        return route

    def compile(self, waypoints: list[int], input_names: dict[int, str]) -> dict:
        result: dict = {
            "passed": False,
            "compiled_route": [],
            "concept_order": [],
            "break": None,
            "detour": None,
            "errors": [],
        }
        steps = tuple(waypoints)
        labels, complete = self._search(waypoints[0], steps)

        if complete is not None:
            # ``passed`` means the requested route exists.  Hypothesis edges
            # remain visible in compiled_route for the CLI to report BLOCKED.
            result["passed"] = True
            result["compiled_route"] = self._route_for(complete)
            result["concept_order"] = [
                {
                    "concept_id": cid,
                    "name": input_names.get(cid, self._resolve_concept_name(cid)),
                }
                for cid in complete.events
            ]
            return result

        # Pick the proof that consumed the longest prefix, then the cheapest.
        all_proofs = {
            proof
            for node_labels in labels.values()
            for proof in node_labels.values()
        }
        best = min(
            all_proofs,
            key=lambda proof: (-proof.consumed_steps, self._cost(proof)),
        )
        result["compiled_route"] = self._route_for(best)
        result["concept_order"] = [
            {
                "concept_id": cid,
                "name": input_names.get(cid, self._resolve_concept_name(cid)),
            }
            for cid in best.events
        ]
        index = best.consumed_steps
        from_id = waypoints[index - 1] if index else waypoints[0]
        to_id = waypoints[index] if index < len(waypoints) else waypoints[-1]
        result["break"] = {
            "from": {"concept_id": from_id,
                     "name": input_names.get(from_id, self._resolve_concept_name(from_id))},
            "to": {"concept_id": to_id,
                   "name": input_names.get(to_id, self._resolve_concept_name(to_id))},
        }

        # A detour ignores intermediate steps but still ends at the requested goal.
        detour_start = from_id
        _, detour = self._search(
            detour_start, (detour_start, waypoints[-1]))
        if detour is None and detour_start != waypoints[0]:
            _, detour = self._search(
                waypoints[0], (waypoints[0], waypoints[-1]))
        if detour is not None:
            result["detour"] = self._route_for(detour)
        return result
