"""
Horon Harness Functions — 神经效应器核心原子动作。

提供 4 个扁平直白的操作函数（直接操作 HoronDB，无任何 Class 伪封装）：
1. sense: 被动感觉传入（查 sensor_hooks 正则、点火传感器、拓扑求值）
2. guard: 主动运动门禁（查 tool_guards 电位，未激活则逆推原因物理拦截）
3. drain: 效应排空封包（从 pending 队列提取通知，打上 【Horon Harness】 信头）
4. sync_session / end_turn: 时序生命周期复位
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from backend._db_common import OFFLINE_DEV_SESSION_ID
from backend.db import HoronDB

logger = logging.getLogger("horon.harness")

HARNESS_PREFIX = "【Horon Harness】"


def wrap_harness_message(items: list[str]) -> str:
    """将一条或多条系统通知统一包装为带 【Horon Harness】 信头的广播块。"""
    if not items:
        return ""
    lines: list[str] = []
    for item in items:
        for line in str(item).splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            if line_str.startswith("- "):
                lines.append(line_str)
            else:
                lines.append(f"- {line_str}")
    if not lines:
        return ""
    return f"{HARNESS_PREFIX}\n" + "\n".join(lines)


def sync_session(db: HoronDB, session_id: str) -> str | None:
    """会话对齐：若检测到新会话，重置 session/turn 传感器并重新求值全图。"""
    if not session_id or not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("Session ID 无效，拒绝执行状态同步。")

    clean_id = session_id.strip()
    curr = db.conn.execute("SELECT session_id FROM current_session LIMIT 1").fetchone()
    if not curr or curr["session_id"] != clean_id:
        db.init_session(session_id=clean_id)
        return f"Horon 系统已初始化就绪（Session: `{clean_id}`）。"
    return None


def end_turn(db: HoronDB) -> None:
    """回合结束：熄灭所有 lifespan='turn' 的传感器并重新拓扑求值。"""
    db.turn_end()


def sense(
    db: HoronDB,
    event_type: str,
    text: str,
    tool_name: str | None = None,
    session_id: str = "",
) -> list[str]:
    """被动感知传入：匹配 sensor_hooks，点火传感器并沿拓扑图求值。

    若提供了 session_id，点火产生的 notify 广播会自动暂存入 pending 队列。
    """
    if not text and not tool_name:
        return []

    query = (
        "SELECT sh.id AS hook_id, sh.sensor_concept_id, sh.event_type, sh.tool, "
        "sh.match_pattern, c.name, c.is_active "
        "FROM sensor_hooks sh "
        "JOIN concepts c ON sh.sensor_concept_id = c.id "
        "WHERE sh.event_type = ?"
    )
    rows = db.conn.execute(query, (event_type,)).fetchall()
    activated: list[int] = []

    for r in rows:
        if r["tool"] and tool_name and r["tool"] != tool_name:
            continue
        if r["tool"] and not tool_name:
            continue

        pat = r["match_pattern"]
        if pat:
            try:
                if re.search(pat, text, re.IGNORECASE):
                    activated.append(r["sensor_concept_id"])
            except Exception as e:
                logger.debug(f"Regex error on hook #{r['hook_id']} pattern {pat!r}: {e}")
        else:
            if r["tool"] and tool_name and r["tool"] == tool_name:
                activated.append(r["sensor_concept_id"])

    messages: list[str] = []
    if activated:
        eval_res = db.evaluate(activated_sensors=activated)
        for fa in eval_res.fired_actions:
            actions = fa.action if isinstance(fa.action, list) else [fa.action] if isinstance(fa.action, dict) else []
            for act in actions:
                if isinstance(act, dict) and "notify" in act and act["notify"]:
                    messages.append(str(act["notify"]))

    clean_id = session_id.strip() if session_id else ""
    if messages and clean_id:
        db.push_pending_notifications(clean_id, messages)

    return messages


def _deny_reason(db: HoronDB, cid: int, guard_name: str) -> str:
    """守卫未通电时的拒绝理由（只看守卫的直接成员与抑制源，不下钻）。

    输入：守卫概念 id 与名字。
    行为：按守卫自身的 activation_type 选说法——
      AND   列出熄灭的传感器 / 未通电的前置节点（全部都要满足）；
      OR    列出熄灭成员，注明任一满足即可；
      CHAIN 报告序列进度与正在等待的成员（该成员已亮时，提示需要它重新发生一次）。
      未通电的 logic 成员可能缺前置也可能被抑制，这里不替它下结论，指路给 compile。
    输出：一句中文理由（不含"工具被拦截"前缀）。
    """
    inhs = db._get_inhibitions(cid, direction="incoming")
    active_inhs = [i.inhibitor_name for i in inhs if i.inhibitor_is_active == 1]
    if active_inhs:
        return f"当前被活跃抑制源 ({', '.join(active_inhs)}) 强制锁死"

    members = db._get_compose_members(cid)
    if not members:
        return "[系统配置错误] 该守卫未配置任何前置条件（孤岛死锁）"

    target = guard_name.replace('"', '\\"')
    hint = f"（原因请运行 `python frontend/cli.py compile --target \"{target}\"` 查看）"
    row = db.conn.execute("SELECT activation_type FROM concepts WHERE id = ?", (cid,)).fetchone()
    act = row["activation_type"] if row else None

    if act == "CHAIN":
        session = db.get_current_session() or OFFLINE_DEV_SESSION_ID
        steps = {
            r["current_order"]
            for r in db.conn.execute(
                "SELECT current_order FROM active_chain_instances "
                "WHERE chain_concept_id = ? AND session_id = ?",
                (cid, session),
            ).fetchall()
        }
        total = max(m.order_index for m in members)
        k = max(steps) if steps else 0
        waiting = next((m for m in members if m.order_index == k + 1), None)
        if waiting is None:
            if k >= total:
                return "[系统内部Bug] 序列已走完但守卫电位未同步（求值状态不一致）"
            return f"[系统配置错误] 序列步骤编号不连续，找不到第 {k + 1}/{total} 步"
        if waiting.is_active == 1:
            return (
                f"序列进行至第 {k}/{total} 步，等待 '{waiting.name}' 重新发生一次"
                f"（它当前已处于点亮状态，只有 0→1 的变化才能推进序列）"
            )
        reason = f"序列进行至第 {k}/{total} 步，等待: {waiting.name}"
        return reason + (hint if waiting.role != "sensor" else "")

    unlit_sensors = [m.name for m in members if m.is_active == 0 and m.role == "sensor"]
    unlit_logic = [m.name for m in members if m.is_active == 0 and m.role != "sensor"]
    if not unlit_sensors and not unlit_logic:
        return "[系统内部Bug] 前置已全部满足但守卫电位未同步（求值状态不一致）"

    if act == "OR":
        names = unlit_sensors + unlit_logic
        return f"以下前置任一满足即可，目前都未满足: {', '.join(names)}" + (hint if unlit_logic else "")

    parts = []
    if unlit_sensors:
        parts.append(f"传感器未点亮: {', '.join(unlit_sensors)}")
    if unlit_logic:
        parts.append(f"前置节点未通电: {', '.join(unlit_logic)}{hint}")
    return "；".join(parts)


def guard(
    db: HoronDB,
    tool_name: str,
    tool_args: dict[str, Any] | str | None,
    session_id: str = "",
) -> tuple[bool, str | None, list[str]]:
    """主动运动门禁：检查工具调用的守卫权限。

    工作逻辑：
    1. 顺便匹配 tool_call 类型的传感器；
    2. 校验 tool_guards 表中的守卫规则：未激活则深入逆推缺失前置/活跃抑制源；
    3. 若放行且产生了传感器通知，暂存入 pending 队列；若拦截则随 deny 决策返回；
    4. 返回 (allowed, deny_reason, fired_notifications)。
    """
    if not tool_name:
        return True, None, []

    if isinstance(tool_args, str):
        args_json = tool_args
    elif isinstance(tool_args, (dict, list)):
        args_json = json.dumps(tool_args, ensure_ascii=False)
    else:
        args_json = "{}"

    # 1. 匹配 tool_call 传感器并点火（暂不入库 pending 队列，避免拦截时重复广播）
    fired_msgs = sense(db, event_type="tool_call", text=args_json, tool_name=tool_name, session_id="")

    # 2. 检查 tool_guards 规则
    query = (
        "SELECT tg.id AS guard_rule_id, tg.guard_concept_id, tg.tool, tg.args_pattern, "
        "c.name, c.is_active "
        "FROM tool_guards tg "
        "JOIN concepts c ON tg.guard_concept_id = c.id "
        "WHERE tg.tool = ?"
    )
    guards = db.conn.execute(query, (tool_name,)).fetchall()

    for g in guards:
        pat = g["args_pattern"]
        if pat:
            try:
                if not re.search(pat, args_json, re.IGNORECASE):
                    continue
            except Exception:
                continue

        if g["is_active"] == 0:
            try:
                reason = _deny_reason(db, g["guard_concept_id"], g["name"])
            except Exception as e:
                reason = f"[系统内部异常] 守卫状态校验失败: {e}"

            return False, f"工具 '{tool_name}' 调用已被拦截：受守卫 '{g['name']}' 管辖，{reason}。", fired_msgs

    # 3. 校验通过放行后，若点火产生了通知且存在有效会话，暂存入队列供后续阶段消费
    clean_id = session_id.strip() if session_id else ""
    if fired_msgs and clean_id:
        db.push_pending_notifications(clean_id, fired_msgs)

    return True, None, fired_msgs


def drain(
    db: HoronDB,
    session_id: str,
    extra_messages: list[str] | None = None,
) -> str:
    """排空指定会话的 pending 通知，并合并额外消息，打包带 【Horon Harness】 信头的广播。"""
    all_msgs: list[str] = []
    if extra_messages:
        all_msgs.extend(extra_messages)

    clean_id = session_id.strip() if session_id else ""
    if clean_id:
        pending = db.pop_pending_notifications(clean_id)
        if pending:
            all_msgs.extend(pending)

    return wrap_harness_message(all_msgs)
