"""Horon compile engine.

This module contains graph traversal, ordered-step verification, route
reconstruction, and failure reporting. Database access and user-facing name
resolution stay in :mod:`backend.db`.
"""
from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass
from typing import Callable, NamedTuple


@dataclass(frozen=True)
class RelationGraph:
    """Minimal relation data shared by compilation and visualization.

    Produced by :mod:`backend.db` from the stored relations and consumed here
    as the input to compilation.
    """

    adjacency: dict[int, list[tuple[int, int, str, str]]]
    and_groups: list[tuple[frozenset[int], int, str, str]]


class _PathCost(NamedTuple):
    hypothesis_count: int | float
    total_jumps: int | float


class _RouteStep(NamedTuple):
    """One traversed relation and the concepts that caused its activation."""

    from_states: tuple[int, ...]
    destination_id: int
    edge_concept_id: int
    edge_short_code: str
    status: str


@dataclass(frozen=True)
class _CompileGraph:
    """Relation graph plus indexes used only by compilation."""

    adjacency: dict[int, list[tuple[int, int, str, str]]]
    incoming: dict[int, list[tuple[int, int, str, str]]]
    and_groups: list[tuple[frozenset[int], int, str, str]]
    and_index: dict[int, list[int]]
    and_variations: dict[int, list[frozenset[int]]]
    and_containers_by_member: dict[int, set[int]]

    @classmethod
    def from_relation_graph(cls, graph: RelationGraph) -> _CompileGraph:
        incoming: dict[int, list[tuple[int, int, str, str]]] = {}
        for from_cid, transitions in graph.adjacency.items():
            for to_cid, edge_cid, edge_sc, status in transitions:
                incoming.setdefault(to_cid, []).append(
                    (from_cid, edge_cid, edge_sc, status))

        and_index: dict[int, list[int]] = {}
        and_variations: dict[int, list[frozenset[int]]] = {}
        and_containers_by_member: dict[int, set[int]] = {}
        for index, (members, edge_cid, _, status) in enumerate(
                graph.and_groups):
            if status != "negated":
                and_variations.setdefault(edge_cid, []).append(members)
            for member in members:
                and_index.setdefault(member, []).append(index)
                if status != "negated":
                    and_containers_by_member.setdefault(member, set()).add(
                        edge_cid)

        return cls(
            adjacency=graph.adjacency,
            incoming=incoming,
            and_groups=graph.and_groups,
            and_index=and_index,
            and_variations=and_variations,
            and_containers_by_member=and_containers_by_member,
        )


class Compiler:
    """Compile routes over an already-loaded Horon graph."""

    def __init__(
        self,
        graph: RelationGraph,
        resolve_concept_name: Callable[[int], str],
    ) -> None:
        self.graph = _CompileGraph.from_relation_graph(graph)
        self._resolve_concept_name = resolve_concept_name

    @staticmethod
    def _activate_route(
        active: set[int],
        provenance: dict[int, frozenset[int]],
        edges: list[dict],
        matched_ids: list[int],
    ) -> tuple[set[int], dict[int, frozenset[int]]]:
        """Apply one route and propagate which ordered steps it depends on."""
        active = set(active)
        provenance = dict(provenance)
        final_dependencies = frozenset()
        for index, edge in enumerate(edges):
            dependencies: set[int] = set()
            for from_id in edge["from"]["concept_ids"]:
                dependencies.update(provenance.get(from_id, ()))
            if index == len(edges) - 1:
                dependencies.update(matched_ids)
            final_dependencies = frozenset(dependencies)
            provenance[edge["concept_id"]] = final_dependencies
            provenance[edge["to"]["concept_id"]] = final_dependencies
            active.add(edge["concept_id"])
            active.add(edge["to"]["concept_id"])
        for matched_id in matched_ids:
            provenance[matched_id] = final_dependencies
            active.add(matched_id)
        return active, provenance

    def _cheapest_route(
        self,
        active_concept_ids: set[int],
        goal_concept_id: int,
    ) -> list[dict] | None:
        """Return the lowest-cost causal route from active concepts to goal.

        Multiple labels are retained for the same concept because locally
        longer routes can share a hypothesis and become cheaper after an
        unordered composition is activated.
        """
        if goal_concept_id in active_concept_ids:
            return []

        EdgeKey = tuple[int, str, str]
        EdgeSet = frozenset[EdgeKey]

        label_nodes: dict[int, int] = {}
        label_edges: dict[int, EdgeSet] = {}
        label_steps: dict[int, tuple[_RouteStep, tuple[int, ...]]] = {}
        labels_by_node: dict[int, dict[EdgeSet, int]] = {}
        frontier: list[tuple[_PathCost, int]] = []
        next_label_id = 0

        def edge_set_cost(edges: EdgeSet) -> _PathCost:
            return _PathCost(
                sum(status != "confirmed" for _, _, status in edges),
                len(edges),
            )

        def register_label(
            node_id: int,
            edges: EdgeSet,
            step: _RouteStep | None,
            parent_label_ids: tuple[int, ...] = (),
        ) -> int | None:
            nonlocal next_label_id
            node_labels = labels_by_node.setdefault(node_id, {})
            if edges in node_labels:
                return None

            label_id = next_label_id
            next_label_id += 1
            node_labels[edges] = label_id
            label_nodes[label_id] = node_id
            label_edges[label_id] = edges
            if step is not None:
                label_steps[label_id] = (step, parent_label_ids)
            heapq.heappush(frontier, (edge_set_cost(edges), label_id))
            return label_id

        for start_id in active_concept_ids:
            register_label(start_id, frozenset(), None)

        goal_label_id: int | None = None
        while frontier:
            _, current_label_id = heapq.heappop(frontier)
            current_cid = label_nodes[current_label_id]
            if current_cid == goal_concept_id:
                goal_label_id = current_label_id
                break

            for to_cid, edge_cid, edge_sc, status in self.graph.adjacency.get(
                    current_cid, []):
                if status == "negated":
                    continue
                next_edges = label_edges[current_label_id] | {
                    (edge_cid, edge_sc, status),
                }
                step = _RouteStep(
                    (current_cid,), to_cid, edge_cid, edge_sc, status)
                for next_cid in (to_cid, edge_cid):
                    if next_cid == current_cid:
                        continue
                    register_label(
                        next_cid,
                        frozenset(next_edges),
                        step,
                        (current_label_id,),
                    )

            for group_index in self.graph.and_index.get(current_cid, []):
                members, edge_cid, edge_sc, status = self.graph.and_groups[
                    group_index]
                if (status == "negated" or edge_cid in members
                        or not members <= labels_by_node.keys()):
                    continue
                ordered_members = tuple(sorted(members))
                member_label_choices = [
                    tuple(labels_by_node[member].values())
                    for member in ordered_members
                ]
                for parent_label_ids in itertools.product(
                        *member_label_choices):
                    merged_edges = frozenset().union(
                        *(label_edges[label_id]
                          for label_id in parent_label_ids),
                        {(edge_cid, edge_sc, status)},
                    )
                    register_label(
                        edge_cid,
                        merged_edges,
                        _RouteStep(
                            ordered_members,
                            edge_cid,
                            edge_cid,
                            edge_sc,
                            status,
                        ),
                        tuple(parent_label_ids),
                    )

        if goal_label_id is None:
            return None

        edges: list[dict] = []
        emitted_steps: set[tuple] = set()
        visited_states: set[int] = set()

        def trace(label_id: int):
            if label_id in visited_states:
                return
            visited_states.add(label_id)
            step_record = label_steps.get(label_id)
            if step_record is None:
                return
            step, parent_label_ids = step_record
            for parent_label_id in parent_label_ids:
                trace(parent_label_id)

            signature = tuple(step)
            if signature in emitted_steps:
                return
            emitted_steps.add(signature)
            from_cids = step.from_states
            edges.append({
                "concept_id": step.edge_concept_id,
                "short_code": step.edge_short_code,
                "name": self._resolve_concept_name(step.edge_concept_id),
                "status": step.status,
                "from": {
                    "concept_ids": sorted(from_cids),
                    "name": " & ".join(
                        self._resolve_concept_name(cid)
                        for cid in sorted(from_cids)
                    ),
                },
                "to": {
                    "concept_id": step.destination_id,
                    "name": self._resolve_concept_name(step.destination_id),
                },
            })

        trace(goal_label_id)
        return edges

    def _find_step_arrivals(
        self,
        active_concept_ids: set[int],
        remaining_step_ids: list[int],
        *,
        provenance: dict[int, frozenset[int]],
        prefer_provenance: set[int] | None = None,
        require_provenance: set[int] | None = None,
        allow_expression_match: bool = True,
    ) -> list[tuple[list[dict], int]]:
        """Return all routes that concretely use the current ordered step."""
        current_step_id = remaining_step_ids[0]
        destinations = {current_step_id}
        if allow_expression_match:
            destinations.update(
                self.graph.and_containers_by_member.get(current_step_id, ()))

        prefer_provenance = prefer_provenance or set()
        require_provenance = require_provenance or set()
        candidates: list[
            tuple[tuple[int, _PathCost, int], list[dict], int]
        ] = []
        for destination_id in destinations:
            for from_cid, edge_cid, edge_sc, status in self.graph.incoming.get(
                    destination_id, []):
                if status == "negated":
                    continue
                prefix = self._cheapest_route(
                    active_concept_ids, from_cid)
                if prefix is None:
                    continue

                activated_before_arrow = set(active_concept_ids)
                for edge in prefix:
                    activated_before_arrow.add(edge["concept_id"])
                    activated_before_arrow.add(edge["to"]["concept_id"])

                matched_count = 1 if destination_id == current_step_id else 0
                if matched_count == 0 and allow_expression_match:
                    for members in self.graph.and_variations.get(
                            destination_id, []):
                        matched: set[int] = set()
                        for count, step_id in enumerate(
                                remaining_step_ids, start=1):
                            if step_id not in members:
                                break
                            matched.add(step_id)
                            if members <= activated_before_arrow | matched:
                                matched_count = max(matched_count, count)
                if matched_count == 0:
                    continue

                witness = {
                    "concept_id": edge_cid,
                    "short_code": edge_sc,
                    "name": self._resolve_concept_name(edge_cid),
                    "status": status,
                    "from": {
                        "concept_ids": [from_cid],
                        "name": self._resolve_concept_name(from_cid),
                    },
                    "to": {
                        "concept_id": destination_id,
                        "name": self._resolve_concept_name(destination_id),
                    },
                }
                route = [*prefix, witness]

                _, route_provenance = self._activate_route(
                    active_concept_ids,
                    provenance,
                    route,
                    remaining_step_ids[:matched_count],
                )

                final_dependencies = route_provenance[destination_id]
                if not require_provenance <= final_dependencies:
                    continue
                cost = _PathCost(
                    sum(edge["status"] != "confirmed" for edge in route),
                    len(route),
                )
                missing_preferred = len(
                    prefer_provenance - final_dependencies)
                key = (missing_preferred, cost, -matched_count)
                candidates.append((key, route, matched_count))

        candidates.sort(key=lambda candidate: candidate[0])
        return [
            (route, matched_count)
            for _, route, matched_count in candidates
        ]

    def compile(
        self,
        waypoints: list[int],
        input_names: dict[int, str],
    ) -> dict:
        """Compile already-resolved waypoints without changing API shape."""
        result: dict = {
            "passed": False,
            "compiled_route": [],
            "break": None,
            "detour": None,
            "errors": [],
        }
        required_steps = waypoints[1:-1]
        best_failure: tuple[int, list[dict]] = (0, [])

        def search(
            step_index: int,
            active: set[int],
            provenance: dict[int, frozenset[int]],
            compiled: list[dict],
        ) -> list[dict] | None:
            nonlocal best_failure
            if step_index > best_failure[0]:
                best_failure = (step_index, compiled)

            if step_index == len(required_steps):
                goal_arrivals = self._find_step_arrivals(
                    active,
                    [waypoints[-1]],
                    provenance=provenance,
                    require_provenance=set(required_steps),
                    allow_expression_match=False,
                )
                if not goal_arrivals:
                    return None
                goal_edges, _ = goal_arrivals[0]
                return [*compiled, *goal_edges]

            arrivals = self._find_step_arrivals(
                active,
                required_steps[step_index:],
                provenance=provenance,
                prefer_provenance=set(required_steps[:step_index]),
            )
            for edges, matched_count in arrivals:
                matched_ids = required_steps[
                    step_index:step_index + matched_count]
                next_active, next_provenance = self._activate_route(
                    active, provenance, edges, matched_ids)
                found = search(
                    step_index + matched_count,
                    next_active,
                    next_provenance,
                    [*compiled, *edges],
                )
                if found is not None:
                    return found
            return None

        compiled_edges = search(
            0,
            {waypoints[0]},
            {waypoints[0]: frozenset()},
            [],
        )

        if compiled_edges is not None:
            result["passed"] = True
            result["compiled_route"] = compiled_edges
        else:
            satisfied_count, compiled_prefix = best_failure
            result["compiled_route"] = compiled_prefix
            last_satisfied_id = (
                required_steps[satisfied_count - 1]
                if satisfied_count else waypoints[0]
            )
            failed_to_id = (
                required_steps[satisfied_count]
                if satisfied_count < len(required_steps)
                else waypoints[-1]
            )
            result["break"] = {
                "from": {
                    "concept_id": last_satisfied_id,
                    "name": input_names[last_satisfied_id],
                },
                "to": {
                    "concept_id": failed_to_id,
                    "name": input_names[failed_to_id],
                },
            }
            detour_starts = {
                waypoints[0], *required_steps[:satisfied_count]
            }
            detour_edges = self._cheapest_route(
                detour_starts, waypoints[-1])
            if detour_edges is not None:
                result["detour"] = detour_edges

        def apply_names(edge_list: list[dict]):
            for edge in edge_list:
                member_names = [
                    input_names.get(cid, self._resolve_concept_name(cid))
                    for cid in edge["from"]["concept_ids"]
                ]
                edge["from"]["name"] = " & ".join(member_names)

                cid_to = edge["to"]["concept_id"]
                if cid_to in input_names:
                    edge["to"]["name"] = input_names[cid_to]

        apply_names(result["compiled_route"])
        if result.get("detour"):
            apply_names(result["detour"])
        return result
