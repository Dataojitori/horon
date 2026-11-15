"""
传感器和守卫的命中记录：钩子每命中一次在 cli_audit_log 记一行，read_hook 和 audit 拿来看规则管不管用。

    jev_check     挂了 Jev 的传感器问 Jev 的每道题一行。sub_action 形如 q0 jev=0.08（第几题、答“是”的概率）。
    sensor_match  挂了 Jev 的 sensor_hook 正则匹配上一次事件（还没问 Jev）。没挂 Jev 的不记，它匹配上就是 sensor_hit。
                  sub_action 是事件类型。
    sensor_hit    传感器点火：没挂 Jev 的正则匹配上就算，挂了 Jev 的要 Jev 也答“是”。sub_action 是事件类型。
    guard_hit     守卫的 tool_guard 规则匹配上一次工具调用。sub_action 是 pass 或 blocked。

只记命中，不记事件原文。远程模式下这些行先落在本机规则库，跟着下一条 CLI 命令送到服务器
（backend/remote_client.py 的 _pending_audit_rows、backend/remote_cli.py 的 import_audit_rows）。

规则重新绑定会刷新它的 created_at，统计只数 created_at 之后的命中，旧规则的命中不算。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from backend._db_common import TIME_FORMAT

JEV_CHECK = "jev_check"
SENSOR_MATCH = "sensor_match"
SENSOR_HIT = "sensor_hit"
GUARD_HIT = "guard_hit"
HIT_WINDOW_DAYS = 7        # read_hook 只数最近这么多天的命中
NEVER_HIT_AFTER_DAYS = 7   # 规则建了这么久还一次没命中，audit 列出来


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(TIME_FORMAT)


def _short(ts: str) -> str:
    return f"{ts[:16].replace('T', ' ')} UTC"


def hit_summary(conn: sqlite3.Connection, concept_id: int, since: str, kind: str) -> str:
    """这个节点的规则最近 HIT_WINDOW_DAYS 天里命中了几次（规则在这之内设的，从 since 起数）。

    kind 是 "tool_guard"、"sensor"（没挂 Jev）或 "sensor_jev"（挂了 Jev）：
      tool_guard  匹配了几次工具调用，其中放行、拦下各几次。
      sensor      正则匹配了几次（匹配上就点火）。
      sensor_jev  正则匹配了几次、问了 Jev 几次（本会话已亮着的不再问）、Jev 答“是”几次。
                  Jev 调用失败时按正则点火，也算在答“是”里。
    """
    start = max(since, _days_ago(HIT_WINDOW_DAYS))
    rows = conn.execute(
        "SELECT command, sub_action, COUNT(*), MAX(timestamp) FROM cli_audit_log "
        "WHERE concept_id = ? AND timestamp >= ? GROUP BY command, sub_action",
        (concept_id, start)).fetchall()

    def count(command: str) -> int:
        return sum(n for cmd, _, n, _ in rows if cmd == command)

    def last(command: str) -> str:
        ts = max((t for cmd, _, _, t in rows if cmd == command), default=None)
        return f", last at {_short(ts)}" if ts else ""

    head = f"Last {HIT_WINDOW_DAYS} days (since {start[:10]}):"
    if kind == "tool_guard":
        n = {sub: c for cmd, sub, c, _ in rows if cmd == GUARD_HIT}
        return (f"{head} regex matched {count(GUARD_HIT)} tool calls "
                f"(allowed {n.get('pass', 0)}, blocked {n.get('blocked', 0)}){last(GUARD_HIT)}")
    if kind == "sensor":
        return f"{head} regex matched {count(SENSOR_HIT)} times{last(SENSOR_HIT)}"
    # 一次提问每道题记一行 jev_check（sub_action 形如 "q0 jev=0.08"），数第 0 题就是提问次数
    asked = sum(n for cmd, sub, n, _ in rows if cmd == JEV_CHECK and (sub or "").startswith("q0 "))
    return (f"{head} regex matched {count(SENSOR_MATCH)} times, asked Jev {asked}, "
            f"Jev said yes {count(SENSOR_HIT)}{last(SENSOR_HIT)}")


def never_hit_report(conn: sqlite3.Connection) -> str | None:
    """列出一次都没命中过的 sensor_hook / tool_guard。没有就返回 None。
    从规则设下、和这个库开始记命中（最早一行命中记录）两者中较晚的那天算起，满 NEVER_HIT_AFTER_DAYS 天才列：
    刚升级、还没开始记的时候，老规则不会全被当成没命中。
    传感器只要正则匹配上过就不算没命中：Jev 一直答“不是”是题的问题，不是规则写错。"""
    hit_commands = (SENSOR_MATCH, SENSOR_HIT, GUARD_HIT)
    last_hit = dict(conn.execute(
        "SELECT concept_id, MAX(timestamp) FROM cli_audit_log WHERE command IN (?, ?, ?) GROUP BY concept_id",
        hit_commands).fetchall())
    (logging_start,) = conn.execute(
        "SELECT MIN(timestamp) FROM cli_audit_log WHERE command IN (?, ?, ?)", hit_commands).fetchone()
    if logging_start is None:
        return None
    cutoff = _days_ago(NEVER_HIT_AFTER_DAYS)
    rules = conn.execute(
        "SELECT c.id, c.name, r.kind, r.created_at FROM ("
        "  SELECT sensor_concept_id AS cid, 'sensor_hook' AS kind, created_at FROM sensor_hooks"
        "  UNION ALL SELECT guard_concept_id, 'tool_guard', created_at FROM tool_guards"
        ") r JOIN concepts c ON c.id = r.cid ORDER BY r.created_at").fetchall()
    rows = [(cid, name, kind, created) for cid, name, kind, created in rules
            if max(created, logging_start) < cutoff and (last_hit.get(cid) or "") < created]
    if not rows:
        return None
    lines = [f"[Rules that have never hit] {len(rows)} sensor_hook / tool_guard rules have not matched once in "
             f"the {NEVER_HIT_AFTER_DAYS}+ days since they were set. Either the event has not happened, "
             "or the rule is written wrong (a tool name in the wrong case, an args_pattern key the host "
             "does not send, a ^ that anchors at the opening { of the argument JSON). Check each with read_hook."]
    lines += [f"  [{cid}] {name} ({kind}, set {created[:10]})" for cid, name, kind, created in rows]
    return "\n".join(lines)
