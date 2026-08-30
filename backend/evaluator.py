"""
Runtime Evaluation Engine for Horon Harness v3

Implements:
1. DAG topological sorting & cycle detection (Kahn's Algorithm).
2. Combinational logic gating (AND / OR) and state-machine sequencing (CHAIN NFA).
3. Declarative inhibition gating (Inhibition Gate with zero-cost recovery).
4. Edge-triggered on_fire action pulses (0 -> 1 rising edge & (N-1) -> N terminal transitions).
5. Continuous potential (is_active) materialization & state transitions.
6. Session/turn lifecycle resets (session_reset, turn_end).
"""

from __future__ import annotations

import collections
import json
import sqlite3
from typing import Any

from ._db_common import _now
from .models import EvaluationResult, FiredAction


def parse_on_fire_action(action_str: str | None) -> Any:
    """Parse on_fire string to JSON object or return raw string if not JSON."""
    if not action_str:
        return None
    try:
        return json.loads(action_str)
    except (json.JSONDecodeError, TypeError):
        return action_str


class GraphEvaluator:
    """Kahn DAG topological evaluation engine for Horon graph state."""

    def __init__(self, conn: sqlite3.Connection, session_id: str = "default"):
        self.conn = conn
        self.session_id = session_id

    def _load_graph(self) -> dict[str, Any]:
        """Fetch concepts, members, and inhibitions; construct DAG adjacency and in-degrees."""
        # 1. Fetch all concepts metadata
        concept_rows = self.conn.execute(
            "SELECT id, name, role, is_active, lifespan, activation_type, on_fire FROM concepts"
        ).fetchall()
        concept_map = {row["id"]: row for row in concept_rows}
        all_ids = set(concept_map.keys())

        # 2. Compose members: parent -> list of (order_index, member_id), member -> list of (parent_id, order_index)
        parent_members: dict[int, list[tuple[int, int]]] = collections.defaultdict(list)
        member_parents: dict[int, list[tuple[int, int]]] = collections.defaultdict(list)
        for row in self.conn.execute(
            "SELECT parent_concept_id, member_concept_id, order_index "
            "FROM compose_members "
            "ORDER BY parent_concept_id, order_index"
        ).fetchall():
            p_id, m_id, idx = row["parent_concept_id"], row["member_concept_id"], row["order_index"]
            parent_members[p_id].append((idx, m_id))
            member_parents[m_id].append((p_id, idx))

        # 3. Inhibitions: target -> list of inhibitor_ids, inhibitor -> list of target_ids
        target_inhibitors: dict[int, list[int]] = collections.defaultdict(list)
        inhibitor_targets: dict[int, list[int]] = collections.defaultdict(list)
        for row in self.conn.execute(
            "SELECT target_concept_id, inhibitor_concept_id FROM inhibitions"
        ).fetchall():
            t_id, i_id = row["target_concept_id"], row["inhibitor_concept_id"]
            target_inhibitors[t_id].append(i_id)
            inhibitor_targets[i_id].append(t_id)

        # 4. Adjacency & In-degrees (compose member -> parent, inhibitor -> target)
        adj: dict[int, list[int]] = {cid: [] for cid in all_ids}
        in_degree: dict[int, int] = {cid: 0 for cid in all_ids}

        for m_id, parents in member_parents.items():
            for p_id, _ in parents:
                if m_id in adj and p_id in in_degree:
                    adj[m_id].append(p_id)
                    in_degree[p_id] += 1

        for i_id, targets in inhibitor_targets.items():
            for t_id in targets:
                if i_id in adj and t_id in in_degree:
                    adj[i_id].append(t_id)
                    in_degree[t_id] += 1

        return {
            "concept_map": concept_map,
            "all_ids": all_ids,
            "parent_members": parent_members,
            "member_parents": member_parents,
            "target_inhibitors": target_inhibitors,
            "adj": adj,
            "in_degree": in_degree,
        }

    def check_cycles(self) -> None:
        """Verify the graph has no cycles across compose_members and inhibitions.

        Raises ValueError if a cycle is detected.
        """
        graph = self._load_graph()
        all_ids = graph["all_ids"]
        adj = graph["adj"]
        in_degree = dict(graph["in_degree"])

        queue = collections.deque([cid for cid in all_ids if in_degree[cid] == 0])
        visited_count = 0
        while queue:
            u = queue.popleft()
            visited_count += 1
            for v in adj[u]:
                in_degree[v] -= 1
                if in_degree[v] == 0:
                    queue.append(v)

        if visited_count < len(all_ids):
            concept_map = graph["concept_map"]
            unresolved = [concept_map[cid]["name"] for cid, deg in in_degree.items() if deg > 0]
            raise ValueError(
                f"Cycle detected in graph topology involving concepts: {', '.join(unresolved)}"
            )

    def _eval_node_potential(
        self,
        node_id: int,
        v_row: Any,
        curr_active: dict[int, int],
        parent_members: dict[int, list[tuple[int, int]]],
        target_inhibitors: dict[int, list[int]],
        active_chain_steps: dict[int, set[int]],
    ) -> int:
        """Evaluate the potential of a logic or guard node with inhibition override."""
        # Check inhibition gate
        if any(curr_active[inh_id] == 1 for inh_id in target_inhibitors.get(node_id, [])):
            return 0

        v_act_type = v_row["activation_type"]
        members = parent_members.get(node_id, [])

        if v_act_type == "AND":
            return 1 if members and all(curr_active[m_id] == 1 for _, m_id in members) else 0
        elif v_act_type == "OR":
            return 1 if members and any(curr_active[m_id] == 1 for _, m_id in members) else 0
        elif v_act_type == "CHAIN":
            max_step = max(s[0] for s in members) if members else 1
            return 1 if max_step in active_chain_steps.get(node_id, set()) else 0
        return 0

    def evaluate(
        self,
        activated_sensors: list[int] | None = None,
        deactivated_sensors: list[int] | None = None,
    ) -> EvaluationResult:
        """Perform a single-pass topological evaluation across the entire graph.

        1. Load graph snapshot & apply input sensor delta.
        2. Propagate potentials in Kahn topological order.
        3. Advance CHAIN NFA instances and evaluate logic/guard nodes.
        4. Detect cycles and collect fired on_fire actions.
        5. Atomically persist updated potentials and NFA instances to DB.
        """
        now = _now()
        activated_sensors = activated_sensors or []
        deactivated_sensors = deactivated_sensors or []

        # 1. Load graph metadata & baseline potentials
        graph = self._load_graph()
        concept_map = graph["concept_map"]
        all_ids = graph["all_ids"]
        parent_members = graph["parent_members"]
        member_parents = graph["member_parents"]
        target_inhibitors = graph["target_inhibitors"]
        adj = graph["adj"]
        in_degree = dict(graph["in_degree"])

        old_active: dict[int, int] = {cid: concept_map[cid]["is_active"] for cid in all_ids}
        curr_active: dict[int, int] = dict(old_active)

        for sid in activated_sensors:
            if sid in concept_map and concept_map[sid]["role"] == "sensor":
                curr_active[sid] = 1
        for sid in deactivated_sensors:
            if sid in concept_map and concept_map[sid]["role"] == "sensor":
                curr_active[sid] = 0

        # Plain concepts always have 0 potential
        for cid, row in concept_map.items():
            if row["role"] == "plain":
                curr_active[cid] = 0

        # 2. Load active CHAIN steps for current session
        initial_chain_steps: dict[int, set[int]] = collections.defaultdict(set)
        for row in self.conn.execute(
            "SELECT chain_concept_id, current_order FROM active_chain_instances WHERE session_id = ?",
            (self.session_id,),
        ).fetchall():
            initial_chain_steps[row["chain_concept_id"]].add(row["current_order"])

        active_chain_steps: dict[int, set[int]] = {
            cid: set(steps) for cid, steps in initial_chain_steps.items()
        }

        fired_actions: list[FiredAction] = []

        terminal_transition_chains: set[int] = set()

        def _advance_chain_nfa(node_id: int):
            chains_to_process: dict[int, list[int]] = collections.defaultdict(list)
            for p_id, step_idx in member_parents.get(node_id, []):
                p_row = concept_map.get(p_id)
                if p_row and p_row["activation_type"] == "CHAIN":
                    chains_to_process[p_id].append(step_idx)

            for p_id, step_indices in chains_to_process.items():
                p_row = concept_map[p_id]
                steps = parent_members.get(p_id, [])
                if not steps:
                    continue
                max_step = max(s[0] for s in steps)
                snapshot = set(active_chain_steps.get(p_id, set()))
                new_steps = set(snapshot)

                for step_idx in step_indices:
                    if step_idx > 1:
                        prev_step = step_idx - 1
                        if prev_step in snapshot:
                            new_steps.discard(prev_step)
                            new_steps.add(step_idx)
                            if step_idx == max_step:
                                terminal_transition_chains.add(p_id)

                if 1 in step_indices:
                    new_steps.add(1)
                    if max_step == 1:
                        terminal_transition_chains.add(p_id)

                active_chain_steps[p_id] = new_steps

        # 3. Process nodes in Kahn topological order
        queue = collections.deque([cid for cid in all_ids if in_degree[cid] == 0])
        visited_count = 0

        while queue:
            u = queue.popleft()
            visited_count += 1
            u_row = concept_map[u]

            # If node u underwent a 0 -> 1 rising edge, advance CHAIN NFA and check sensor on_fire
            if old_active[u] == 0 and curr_active[u] == 1:
                _advance_chain_nfa(u)
                if u_row["role"] == "sensor" and u_row["on_fire"]:
                    fired_actions.append(FiredAction(
                        concept=u_row["name"],
                        concept_id=u,
                        action=parse_on_fire_action(u_row["on_fire"]),
                    ))

            for v in adj[u]:
                in_degree[v] -= 1
                if in_degree[v] == 0:
                    v_row = concept_map[v]
                    v_role = v_row["role"]

                    if v_role in ("logic", "guard"):
                        new_val = self._eval_node_potential(
                            v, v_row, curr_active, parent_members, target_inhibitors, active_chain_steps
                        )
                        curr_active[v] = new_val

                        # Check 0 -> 1 rising edge for AND/OR logic and guards
                        if v_row["activation_type"] in ("AND", "OR") and old_active[v] == 0 and new_val == 1 and v_row["on_fire"]:
                            fired_actions.append(FiredAction(
                                concept=v_row["name"],
                                concept_id=v,
                                action=parse_on_fire_action(v_row["on_fire"]),
                            ))
                        # Check terminal transition for CHAIN logic/guards (ensuring not inhibited and on_fire configured)
                        elif v_row["activation_type"] == "CHAIN" and v in terminal_transition_chains and new_val == 1 and v_row["on_fire"]:
                            fired_actions.append(FiredAction(
                                concept=v_row["name"],
                                concept_id=v,
                                action=parse_on_fire_action(v_row["on_fire"]),
                            ))
                    elif v_role == "plain":
                        curr_active[v] = 0

                    queue.append(v)

        if visited_count < len(all_ids):
            unresolved = [concept_map[cid]["name"] for cid, deg in in_degree.items() if deg > 0]
            raise ValueError(
                f"Cycle detected in graph topology involving concepts: {', '.join(unresolved)}"
            )

        # 4. Atomic batch persistence
        all_chain_ids = set(initial_chain_steps.keys()) | set(active_chain_steps.keys())
        for chain_id in all_chain_ids:
            old_steps = initial_chain_steps.get(chain_id, set())
            new_steps = active_chain_steps.get(chain_id, set())
            for step in old_steps - new_steps:
                self.conn.execute(
                    "DELETE FROM active_chain_instances WHERE chain_concept_id = ? AND current_order = ? AND session_id = ?",
                    (chain_id, step, self.session_id),
                )
            for step in new_steps - old_steps:
                self.conn.execute(
                    "INSERT OR IGNORE INTO active_chain_instances (chain_concept_id, current_order, session_id) VALUES (?, ?, ?)",
                    (chain_id, step, self.session_id),
                )

        active_changed: dict[str, int] = {}
        for cid, new_val in curr_active.items():
            if new_val != old_active[cid]:
                cname = concept_map[cid]["name"]
                active_changed[cname] = new_val
                self.conn.execute(
                    "UPDATE concepts SET is_active = ?, updated_at = ? WHERE id = ?",
                    (new_val, now, cid),
                )

        return EvaluationResult(active_changed=active_changed, fired_actions=fired_actions)

    def reset_session(self) -> EvaluationResult:
        """Reset session: clear active CHAIN instances and turn/session sensors, then re-evaluate graph."""
        now = _now()
        # 1. Clear CHAIN instances
        self.conn.execute(
            "DELETE FROM active_chain_instances WHERE session_id = ?",
            (self.session_id,),
        )
        # 2. Reset ephemeral sensors
        self.conn.execute(
            "UPDATE concepts SET is_active = 0, updated_at = ? WHERE lifespan IN ('session', 'turn')",
            (now,),
        )
        # 3. Re-evaluate graph to cascade deactivation
        return self.evaluate()

    def end_turn(self) -> EvaluationResult:
        """End turn: reset turn sensors to 0, then re-evaluate graph."""
        now = _now()
        self.conn.execute(
            "UPDATE concepts SET is_active = 0, updated_at = ? WHERE lifespan = 'turn'",
            (now,),
        )
        return self.evaluate()

