"""
Runtime Evaluation Engine for Horon Harness v3

Implements:
1. DAG topological sorting & cycle detection (Kahn's Algorithm).
2. Combinational logic gating (AND / OR) and state-machine sequencing (CHAIN NFA).
3. Declarative inhibition gating (Inhibition Gate with zero-cost recovery).
4. Edge-triggered on_fire action pulses (0 -> 1 rising edge & (N-1) -> N terminal transitions).
5. Only sensor states and CHAIN progress are stored; logic/guard states are derived on every read.
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


def load_active_states(conn: sqlite3.Connection, session_id: str) -> dict[int, int]:
    """读取某个会话下，库里每个节点当前是否激活。

    任何代码想知道「某个节点在某个会话里是否激活」，都调用本函数，不要自己拼 SQL。
    只有传感器（输入）的状态是存下来的，逻辑节点和守卫每次按规则现算：
      - plain 节点：不参与电路，一律视为未激活。
      - 永久传感器：所有会话共用一份，存在 concepts.is_active。
      - turn/session 传感器：每个会话各自记录，session_active_sensors 里有这一行就是激活。
      - 逻辑节点、守卫：由上面这些输入加上该会话的 CHAIN 进度，按拓扑顺序算出来。

    输入：数据库连接；会话 ID（宿主传来的会话，或离线的 devonly）。
    输出：{节点 id: 1 已激活 / 0 未激活}，覆盖库里全部节点。
    """
    ev = GraphEvaluator(conn, session_id)
    graph = ev._load_graph()
    return ev._derive(graph, ev._load_inputs(graph["concept_map"]), ev._load_chain_steps())


class GraphEvaluator:
    """Kahn DAG topological evaluation engine for Horon graph state."""

    def __init__(self, conn: sqlite3.Connection, session_id: str):
        if not session_id or not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("GraphEvaluator 要求非空的 session_id。")
        self.conn = conn
        self.session_id = session_id.strip()

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

    def _load_inputs(self, concept_map: dict[int, Any]) -> dict[int, int]:
        """读出传感器的存储状态；逻辑节点、守卫、plain 先填 0，由 _derive 算。"""
        lit = {
            r["concept_id"]
            for r in self.conn.execute(
                "SELECT concept_id FROM session_active_sensors WHERE session_id = ?",
                (self.session_id,),
            )
        }
        states: dict[int, int] = {}
        for cid, row in concept_map.items():
            if row["role"] != "sensor":
                states[cid] = 0
            elif row["lifespan"] == "permanent":
                states[cid] = row["is_active"]
            else:
                states[cid] = 1 if cid in lit else 0
        return states

    def _load_chain_steps(self) -> dict[int, set[int]]:
        """读出本会话每个 CHAIN 当前处在哪些步骤。"""
        steps: dict[int, set[int]] = collections.defaultdict(set)
        for row in self.conn.execute(
            "SELECT chain_concept_id, current_order FROM active_chain_instances WHERE session_id = ?",
            (self.session_id,),
        ):
            steps[row["chain_concept_id"]].add(row["current_order"])
        return steps

    def _derive(
        self,
        graph: dict[str, Any],
        inputs: dict[int, int],
        chain_steps: dict[int, set[int]],
    ) -> dict[int, int]:
        """按拓扑顺序，从传感器状态和 CHAIN 进度算出逻辑节点、守卫是否激活。不推进 CHAIN、不写库。

        输入：_load_graph 的结果；传感器状态（_load_inputs 的格式）；CHAIN 进度。
        输出：全部节点的激活状态（传感器原样照抄）。
        """
        concept_map = graph["concept_map"]
        states = dict(inputs)
        in_degree = dict(graph["in_degree"])
        queue = collections.deque([cid for cid in graph["all_ids"] if in_degree[cid] == 0])
        while queue:
            u = queue.popleft()
            for v in graph["adj"][u]:
                in_degree[v] -= 1
                if in_degree[v] == 0:
                    if concept_map[v]["role"] in ("logic", "guard"):
                        states[v] = self._eval_node_potential(
                            v, concept_map[v], states,
                            graph["parent_members"], graph["target_inhibitors"], chain_steps,
                        )
                    queue.append(v)
        return states

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
        5. Persist changed sensor states and CHAIN progress (logic/guard states are not stored).
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

        # 2. 事件发生前的状态：按存储的输入和 CHAIN 进度现算
        initial_chain_steps = self._load_chain_steps()
        old_active = self._derive(graph, self._load_inputs(concept_map), initial_chain_steps)

        curr_active: dict[int, int] = dict(old_active)

        for sid in activated_sensors:
            if sid in concept_map and concept_map[sid]["role"] == "sensor":
                curr_active[sid] = 1
        for sid in deactivated_sensors:
            if sid in concept_map and concept_map[sid]["role"] == "sensor":
                curr_active[sid] = 0

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
                row = concept_map[cid]
                cname = row["name"]
                active_changed[cname] = new_val

                # 只存传感器；逻辑节点和守卫下次读取时现算
                if row["role"] != "sensor":
                    continue
                if row["lifespan"] == "permanent":
                    self.conn.execute(
                        "UPDATE concepts SET is_active = ?, updated_at = ? WHERE id = ?",
                        (new_val, now, cid),
                    )
                elif new_val == 1:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO session_active_sensors (session_id, concept_id, activated_at) "
                        "VALUES (?, ?, ?)",
                        (self.session_id, cid, now),
                    )
                else:
                    self.conn.execute(
                        "DELETE FROM session_active_sensors WHERE session_id = ? AND concept_id = ?",
                        (self.session_id, cid),
                    )

        return EvaluationResult(active_changed=active_changed, fired_actions=fired_actions)

    def reset_session(self) -> EvaluationResult:
        """重置会话：清空本会话的 CHAIN 进度、待发通知和已激活的 turn/session 传感器。

        逻辑节点和守卫不存状态，输入清空后读取时自然算成未激活，所以不需要再求值。
        """
        for table in ("active_chain_instances", "pending_notifications", "session_active_sensors"):
            self.conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (self.session_id,))
        return EvaluationResult()

    def end_turn(self) -> EvaluationResult:
        """回合结束：熄灭本会话里 lifespan='turn' 的传感器（不需要再求值，理由同 reset_session）。"""
        self.conn.execute(
            "DELETE FROM session_active_sensors WHERE session_id = ? AND concept_id IN "
            "(SELECT id FROM concepts WHERE role = 'sensor' AND lifespan = 'turn')",
            (self.session_id,),
        )
        return EvaluationResult()
