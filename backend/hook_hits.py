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
QUIET_DAYS = 30            # 点过火的传感器这么多天没再点，audit 列出来
NEVER_FIRED_DAYS = 7       # 传感器设下这么多天一次没点过火，audit 列出来


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


def quiet_sensor_report(conn: sqlite3.Connection) -> str | None:
    """列出没在点火的传感器，分两组：以前点过火、最近 QUIET_DAYS 天没点的；设下满 NEVER_FIRED_DAYS 天一次都没点过的。
    没有就返回 None。

    只看传感器的点火（sensor_hit），不看守卫：守卫管的多是少见的危险操作，长时间不命中是常态。
    规则重新绑定后只算之后的点火。“一次都没点过”只列满 NEVER_FIRED_DAYS 天的：从规则设下和这个库开始记命中
    （最早一行 sensor_match / sensor_hit / guard_hit）两者中较晚的那天算起，刚升级还没开始记的时候，
    老传感器不会全被列出来。
    """
    # 一次扫描：每个节点最早的命中（取全库最小值当开始记录的时间）和最后一次点火
    per_concept = conn.execute(
        "SELECT concept_id, MIN(timestamp), MAX(CASE WHEN command = ? THEN timestamp END) FROM cli_audit_log "
        "WHERE command IN (?, ?, ?) GROUP BY concept_id",
        (SENSOR_HIT, SENSOR_MATCH, SENSOR_HIT, GUARD_HIT)).fetchall()
    if not per_concept:
        return None
    logging_start = min(first for _, first, _ in per_concept)
    last_fired = {cid: last for cid, _, last in per_concept if last}
    quiet_cutoff, never_cutoff = _days_ago(QUIET_DAYS), _days_ago(NEVER_FIRED_DAYS)
    sensors = conn.execute(
        "SELECT c.id, c.name, sh.created_at FROM sensor_hooks sh JOIN concepts c ON c.id = sh.sensor_concept_id "
        "ORDER BY sh.created_at").fetchall()
    quiet, never = [], []
    for cid, name, created in sensors:
        last = last_fired.get(cid)
        if last and last >= created:
            if last < quiet_cutoff:
                quiet.append(f"  [{cid}] {name} (last fired {last[:10]})")
        elif max(created, logging_start) < never_cutoff:
            never.append(f"  [{cid}] {name} (set {created[:10]})")
    parts = []
    if quiet:
        parts.append("\n".join([
            f"[Sensors quiet for {QUIET_DAYS}+ days] {len(quiet)} sensors fired before but not in the last "
            f"{QUIET_DAYS} days. The situation may have stopped coming up; if it has not, check the rule "
            "with read_hook.", *quiet]))
    if never:
        parts.append("\n".join([
            f"[Sensors that have never fired] {len(never)} sensors have never fired. "
            "Either the situation has not come up, or the rule is written wrong.", *never]))
    return "\n\n".join(parts) or None
