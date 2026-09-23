"""Compile (backward solver diagnostics) mixin for HoronDB.

`compile(target, assume)` 回答一个问题：目标节点现在为什么通电 / 为什么没通电，
以及"如果我再点亮这几个传感器"之后还差什么。

实现要点：
- 不另写一套求值逻辑。先在 SQLite SAVEPOINT 里把 assume 的传感器喂给真正的
  GraphEvaluator 跑一趟，读出假设下的全图电位与 CHAIN 进度，然后 ROLLBACK。
  诊断结论因此与运行时引擎逐位一致，且 compile 对数据库零副作用。
- 然后从目标沿 compose_members 向下递归，直到熄灭的叶子传感器，
  沿途检查 inhibitions，产出差集（还差什么）与一棵人类可读的依赖诊断树。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .models import ChainProgress, CompileResult

_SAVEPOINT = "horon_compile_probe"
_MAX_DEPTH = 32  # DAG 已保证无环；这里只是防御性上限


@dataclass
class _Snapshot:
    """假设条件下求值后的全图只读快照。"""
    concepts: dict[int, Any]                        # id -> sqlite Row (name, role, lifespan, activation_type)
    active: dict[int, int]                          # id -> 假设下的电位
    members: dict[int, list[tuple[int, int]]]       # parent -> [(order_index, member_id)]，按序
    inhibitors: dict[int, list[int]]                # target -> [inhibitor_id]
    chain_steps: dict[int, set[int]]                # chain_id -> 假设下的活跃步骤集合
    hooks: dict[int, Any]                           # sensor_id -> sensor_hooks Row
    assumed: set[int] = field(default_factory=set)  # 本次 --assume 的传感器（已亮的按"再发生一次"处理）
    req_cache: dict[int, list[str]] = field(default_factory=dict)  # cid -> 差集（记忆化）


class CompileMixin:
    """Backward solver: diagnose why a logic/guard/sensor node is (not) active."""

    # ── Public API ──────────────────────────────────────────────────────────

    def compile(
        self,
        target: str | int,
        assume: list[str | int] | None = None,
    ) -> CompileResult:
        """逆推诊断目标节点的电位。

        输入：
          target: 目标概念（名字 / 别名 / ID）。必须是 sensor、logic 或 guard。
          assume: 额外假设点亮的传感器（名字 / 别名 / ID）。只接受 role='sensor'；
                  logic/guard 的电位由规则推导，想假设它们亮请改为假设其上游传感器。
        行为：
          在 savepoint 中以 assume 为输入跑一趟真实拓扑求值，读取结果后回滚，
          再从目标向下递归诊断。不写入任何表。
        输出：
          CompileResult:
            status = "active"              目标通电（guard 即放行）
                   | "inhibited"           自身规则已满足，但被活跃抑制源压为 0
                   | "unmet_prerequisites" 规则未满足
            missing_prerequisites  还需要做到的叶子条件；AND 逐项列出，
                                   OR 合并为一项 "A 或 B"，中间层被抑制写为 "解除…"
            active_inhibitors      直接压在目标上的活跃抑制源
            chain_progress         目标为 CHAIN 时的步进进度
            diagnostic_tree        逐层缩进的依赖诊断文本
        异常：
          ValueError — 概念不存在、目标是 plain、或 assume 了非 sensor。
        """
        target_id = self._resolve_id(target)
        target_row = self.conn.execute(
            "SELECT name, role FROM concepts WHERE id = ?", (target_id,)
        ).fetchone()
        if target_row["role"] == "plain":
            raise ValueError(
                f"'{target_row['name']}' 是 plain 砖块，不参与电路，没有可诊断的电位。"
            )
        assume_ids = self._resolve_assumptions(assume or [])
        snap = self._probe_snapshot(assume_ids)

        row = snap.concepts[target_id]
        name = row["name"]
        role = row["role"]

        tree: list[str] = []
        self._render_tree(target_id, snap, tree, depth=0, seen=set(), root=True)

        if snap.active[target_id] == 1:
            return CompileResult(target=name, status="active", diagnostic_tree=tree)

        if role == "sensor":
            return CompileResult(
                target=name,
                status="unmet_prerequisites",
                missing_prerequisites=[name],
                diagnostic_tree=tree,
            )

        active_inhs = [
            snap.concepts[i]["name"]
            for i in snap.inhibitors.get(target_id, [])
            if snap.active[i] == 1
        ]
        rule_met = self._rule_met(target_id, snap)
        if rule_met and active_inhs:
            return CompileResult(
                target=name,
                status="inhibited",
                active_inhibitors=active_inhs,
                diagnostic_tree=tree,
            )

        # 复制一份：_requirements_of_rule 可能直接返回 req_cache 里的列表，下面的 append 不能改到缓存
        missing = list(self._requirements_of_rule(target_id, snap, depth=0))
        if active_inhs:
            missing.append(f"解除「{name}」的抑制（抑制源: {'、'.join(active_inhs)}）")
        return CompileResult(
            target=name,
            status="unmet_prerequisites",
            active_inhibitors=active_inhs,
            missing_prerequisites=missing,
            chain_progress=self._chain_progress(target_id, snap),
            diagnostic_tree=tree,
        )

    # ── Snapshot (savepoint probe) ──────────────────────────────────────────

    def _resolve_assumptions(self, assume: list[str | int]) -> list[int]:
        """解析 assume 列表；拒绝非 sensor 节点。返回去重后的 concept id 列表。"""
        ids: list[int] = []
        for q in assume:
            cid = self._resolve_id(q)
            row = self.conn.execute(
                "SELECT name, role FROM concepts WHERE id = ?", (cid,)
            ).fetchone()
            if row["role"] != "sensor":
                raise ValueError(
                    f"--assume 只接受 sensor：'{row['name']}' 是 {row['role']}。"
                    f"logic/guard 的电位由规则推导，请改为假设它上游的传感器。"
                )
            if cid not in ids:
                ids.append(cid)
        return ids

    def _probe_snapshot(self, assume_ids: list[int]) -> _Snapshot:
        """在 savepoint 内用真实求值器跑一趟假设求值，读出快照后整体回滚。"""
        conn: sqlite3.Connection = self.conn
        baseline = {
            r["id"]: r["is_active"]
            for r in conn.execute("SELECT id, is_active FROM concepts").fetchall()
        }
        conn.execute(f"SAVEPOINT {_SAVEPOINT}")
        try:
            evaluator = self._evaluator()  # 可能写 current_session（devonly 兜底），同样会被回滚
            # 按用户给出的顺序逐个点亮：假设的语义是"依次发生"，CHAIN 的推进因此与 --assume 顺序一致，
            # 而不是取决于同一趟求值里 Kahn 队列的出队顺序（那实际上跟 concept id 走）。
            # 已亮的传感器先熄灭再点亮：假设它"再发生一次"，产生真正的 0→1 上升沿
            # （compile 会建议"「S」再产生一次上升沿"，照做时必须真的推进 CHAIN，而不是被静默忽略）。
            evaluator.evaluate()
            for sid in assume_ids:
                if baseline.get(sid) == 1:
                    evaluator.evaluate(deactivated_sensors=[sid])
                evaluator.evaluate(activated_sensors=[sid])
            session_id = evaluator.session_id

            concepts = {
                r["id"]: r
                for r in conn.execute(
                    "SELECT id, name, role, is_active, lifespan, activation_type FROM concepts"
                ).fetchall()
            }
            members: dict[int, list[tuple[int, int]]] = {}
            for r in conn.execute(
                "SELECT parent_concept_id, member_concept_id, order_index "
                "FROM compose_members ORDER BY parent_concept_id, order_index"
            ).fetchall():
                members.setdefault(r["parent_concept_id"], []).append(
                    (r["order_index"], r["member_concept_id"])
                )
            inhibitors: dict[int, list[int]] = {}
            for r in conn.execute(
                "SELECT target_concept_id, inhibitor_concept_id FROM inhibitions "
                "ORDER BY inhibitor_concept_id"
            ).fetchall():
                inhibitors.setdefault(r["target_concept_id"], []).append(
                    r["inhibitor_concept_id"]
                )
            chain_steps: dict[int, set[int]] = {}
            for r in conn.execute(
                "SELECT chain_concept_id, current_order FROM active_chain_instances "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchall():
                chain_steps.setdefault(r["chain_concept_id"], set()).add(r["current_order"])
            hooks = {
                r["sensor_concept_id"]: r
                for r in conn.execute(
                    "SELECT sensor_concept_id, event_type, tool, match_pattern FROM sensor_hooks"
                ).fetchall()
            }
        finally:
            conn.execute(f"ROLLBACK TO {_SAVEPOINT}")
            conn.execute(f"RELEASE {_SAVEPOINT}")

        return _Snapshot(
            concepts=concepts,
            active={cid: r["is_active"] for cid, r in concepts.items()},
            members=members,
            inhibitors=inhibitors,
            chain_steps=chain_steps,
            hooks=hooks,
            assumed=set(assume_ids),
        )

    # ── Rule evaluation on snapshot ─────────────────────────────────────────

    @staticmethod
    def _rule_met(cid: int, snap: _Snapshot) -> bool:
        """只看激活规则本身（不看抑制）是否满足。与 GraphEvaluator 的判定一致。"""
        row = snap.concepts[cid]
        mems = snap.members.get(cid, [])
        if not mems:
            return False
        act = row["activation_type"]
        if act == "AND":
            return all(snap.active[m] == 1 for _, m in mems)
        if act == "OR":
            return any(snap.active[m] == 1 for _, m in mems)
        if act == "CHAIN":
            return max(i for i, _ in mems) in snap.chain_steps.get(cid, set())
        return False

    @staticmethod
    def _chain_position(cid: int, snap: _Snapshot) -> tuple[int, int, int | None]:
        """CHAIN 进度：(已完成步数 k, 总步数 N, 下一步等待的 member id 或 None)。"""
        mems = snap.members.get(cid, [])
        total = max((i for i, _ in mems), default=0)
        steps = snap.chain_steps.get(cid, set())
        k = max(steps) if steps else 0
        waiting = next((m for i, m in mems if i == k + 1), None) if k < total else None
        return k, total, waiting

    def _chain_progress(self, cid: int, snap: _Snapshot) -> ChainProgress | None:
        if snap.concepts[cid]["activation_type"] != "CHAIN":
            return None
        k, total, waiting = self._chain_position(cid, snap)
        if waiting is None:
            return None
        return ChainProgress(
            current_step=k,
            total_steps=total,
            waiting_for=snap.concepts[waiting]["name"],
        )

    # ── Requirement (差集) extraction ───────────────────────────────────────

    def _requirements_of_node(self, cid: int, snap: _Snapshot, depth: int) -> list[str]:
        """一个熄灭节点要亮起来，还需要做到什么（扁平列表，元素之间为"且"）。

        结果按 cid 缓存在 snap.req_cache：DAG 共享节点只算一次，避免指数级重复递归。
        """
        if snap.active[cid] == 1 or depth > _MAX_DEPTH:
            return []
        if cid in snap.req_cache:
            return snap.req_cache[cid]
        row = snap.concepts[cid]
        name = row["name"]
        role = row["role"]
        if role == "sensor":
            reqs = [name]
        elif role == "plain":
            reqs = [f"[配置错误] plain「{name}」被当作前置，但它电位恒为 0"]
        else:
            reqs = [] if self._rule_met(cid, snap) else self._requirements_of_rule(cid, snap, depth)
            # 抑制与规则是"且"关系：规则满足了也要解除抑制，规则没满足时抑制同样要解除
            inhs = [
                snap.concepts[i]["name"]
                for i in snap.inhibitors.get(cid, [])
                if snap.active[i] == 1
            ]
            if inhs:
                reqs = reqs + [f"解除「{name}」的抑制（抑制源: {'、'.join(inhs)}）"]
        snap.req_cache[cid] = reqs
        return reqs

    @staticmethod
    def _member_ref(cid: int, snap: _Snapshot) -> str:
        """在 OR / 嵌套 CHAIN 的文本里引用一个成员：传感器写裸名，其余写「名」（细节见诊断树）。"""
        row = snap.concepts[cid]
        if row["role"] == "sensor":
            return row["name"]
        if row["role"] == "plain":
            return f"「{row['name']}」[配置错误: plain 恒为 0]"
        return f"「{row['name']}」"

    def _requirements_of_rule(self, cid: int, snap: _Snapshot, depth: int) -> list[str]:
        """logic/guard 节点的激活规则未满足时，差集是什么。"""
        row = snap.concepts[cid]
        name = row["name"]
        mems = snap.members.get(cid, [])
        if not mems:
            return [f"[配置错误]「{name}」未配置任何前置（孤岛死锁）"]

        act = row["activation_type"]
        if act == "AND":
            out: list[str] = []
            for _, m in mems:
                for req in self._requirements_of_node(m, snap, depth + 1):
                    if req not in out:
                        out.append(req)
            return out

        if act == "OR":
            # 选项只写成员名，不内联其子树：内联会让共享子图的文本按指数膨胀，
            # 且嵌套"或/且"的优先级无法读清。每个 logic 选项的细节由诊断树展开一次。
            alts: list[str] = []
            for _, m in mems:
                if snap.active[m] == 1:
                    continue
                alt = self._member_ref(m, snap)
                if alt not in alts:
                    alts.append(alt)
            if not alts:
                return []
            return [alts[0]] if len(alts) == 1 else [" 或 ".join(alts)]

        if act == "CHAIN":
            k, total, waiting = self._chain_position(cid, snap)
            if waiting is None:
                if k >= total:
                    return []
                return [f"[配置错误] CHAIN「{name}」步骤编号不连续，找不到第 {k + 1} 步"]
            wname = snap.concepts[waiting]["name"]
            rest = total - k - 1
            tail = f"（之后还有 {rest} 步）" if rest > 0 else ""
            if snap.active[waiting] == 1:
                return [
                    f"「{wname}」再产生一次上升沿（0→1），推进 CHAIN「{name}」第 {k + 1}/{total} 步{tail}"
                ]
            if depth == 0:
                # 目标自身的进度由 chain_progress 表达，这里把等待步骤展开到叶子；剩余步骤由诊断树列出
                return self._requirements_of_node(waiting, snap, depth + 1)
            # 嵌套 CHAIN 只写等待成员名，理由同 OR 分支
            return [f"CHAIN「{name}」第 {k + 1}/{total} 步需要 {self._member_ref(waiting, snap)}{tail}"]

        return [f"[配置错误]「{name}」的 activation_type 无效: {act}"]

    # ── Diagnostic tree ─────────────────────────────────────────────────────

    def _node_label(self, cid: int, snap: _Snapshot) -> str:
        row = snap.concepts[cid]
        role = row["role"]
        on = snap.active[cid] == 1
        inhibited = (
            not on
            and role in ("logic", "guard")
            and self._rule_met(cid, snap)
            and any(snap.active[i] == 1 for i in snap.inhibitors.get(cid, []))
        )
        mark = "✓" if on else ("⛔" if inhibited else "✗")
        if role == "sensor":
            kind = f"sensor/{row['lifespan']}"
        elif role in ("logic", "guard"):
            kind = f"{role}/{row['activation_type']}"
        else:
            kind = role
        suffix = " (假设)" if cid in snap.assumed else ""
        return f"{mark} {row['name']} [{kind}]{suffix}"

    def _sensor_hint(self, cid: int, snap: _Snapshot) -> str:
        """熄灭的传感器要怎么才能点亮。"""
        row = snap.concepts[cid]
        if row["lifespan"] == "permanent":
            return "需人工 `set active`（permanent 开关）"
        hook = snap.hooks.get(cid)
        if hook is None:
            return f"[配置错误] {row['lifespan']} 传感器没有 sensor_hook，永远无法点亮"
        tool = f"[{hook['tool']}]" if hook["tool"] else ""
        return f"等待 {hook['event_type']}{tool} 匹配 /{hook['match_pattern']}/"

    def _render_tree(
        self,
        cid: int,
        snap: _Snapshot,
        out: list[str],
        depth: int,
        seen: set[int],
        root: bool = False,
        prefix: str = "",
    ) -> None:
        """深度优先写出依赖诊断树。

        熄灭节点向下展开；通电节点只在根部展开一层（说明它为何通电）。
        DAG 中被多个父节点共享的节点只展开一次，之后标注"(见上)"。
        """
        indent = "  " * depth
        label = self._node_label(cid, snap)
        if cid in seen:
            out.append(f"{indent}{prefix}{label} (见上)")
            return
        seen.add(cid)

        row = snap.concepts[cid]
        role = row["role"]
        on = snap.active[cid] == 1

        if role == "sensor":
            hint = "" if on else f" — {self._sensor_hint(cid, snap)}"
            out.append(f"{indent}{prefix}{label}{hint}")
            return
        out.append(f"{indent}{prefix}{label}")

        if role not in ("logic", "guard") or depth > _MAX_DEPTH:
            return

        child_indent = "  " * (depth + 1)
        for i in snap.inhibitors.get(cid, []):
            if snap.active[i] == 1:
                out.append(f"{child_indent}⛔ 活跃抑制源: {snap.concepts[i]['name']}")

        if on and not root:
            return

        mems = snap.members.get(cid, [])
        if not mems:
            out.append(f"{child_indent}[配置错误] 未配置任何前置（孤岛死锁）")
            return

        act = row["activation_type"]
        if act == "CHAIN":
            k, total, _ = self._chain_position(cid, snap)
            out.append(f"{child_indent}CHAIN 进度 {k}/{total}")
            for idx, m in mems:
                pos = "✓ 已过" if idx <= k else ("→ 等待" if idx == k + 1 else "  未到")
                if idx == k + 1 and snap.active[m] == 0:
                    self._render_tree(m, snap, out, depth + 1, seen, prefix=f"{idx}. {pos} ")
                else:
                    out.append(f"{child_indent}{idx}. {pos} {self._node_label(m, snap)}")
            return

        if act == "OR" and len(mems) > 1:
            out.append(f"{child_indent}(任一满足即可)")
        for _, m in mems:
            self._render_tree(m, snap, out, depth + 1, seen)
