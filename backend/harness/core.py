"""
Horon Harness Functions — 神经效应器核心原子动作。

提供 4 个扁平直白的操作函数（直接操作 HoronDB，无任何 Class 伪封装）：
1. sense: 被动感觉传入（查 sensor_hooks 正则、按需问 Jev 复核、点火传感器、拓扑求值）
2. guard: 主动运动门禁（查 tool_guards 电位，未激活则逆推原因物理拦截）
3. drain: 效应排空封包（从 pending 队列提取通知，打上 【Horon Harness】 信头）
4. sync_session / end_turn: 时序生命周期复位
"""

from __future__ import annotations

import json
import re
from typing import Any

from backend.db import HoronDB
from backend.llm import DECISIONS_ENDPOINT, JEV_MODEL, openrouter_post
from backend._db_common import parse_jev_questions
from backend.evaluator import load_active_states
from backend.integrity import check_at_session_start

HARNESS_PREFIX = "【Horon Harness】"

# The two answers offered to Jev for every sensor question; a sensor fires on "yes".
# The labels follow the language the questions are written in; change them together.
JEV_CHOICES = {"yes": "是", "no": "不是"}
# How the event text is shown to Jev: who says it to whom, with the text quoted inside.
# A separate lead line ("below is a user message", then the text) left Jev unsure who was
# speaking: "He said 'go ahead'" was read as the user's own consent. {tool} is the tool name.
JEV_STATE = {
    "user_message": "用户对 AI 说：「{text}」",
    "model_message": "AI 对用户说：「{text}」",
    "tool_call": "AI 调用工具 {tool}，传的参数是：「{text}」",
    "tool_result": "AI 调用工具 {tool} 后，工具 {tool} 返回：「{text}」",
}
# Message events with the other side's previous message as context. Questions say "这段话",
# so the last line tells Jev which message is being judged.
JEV_STATE_WITH_PREV = {
    "user_message": "AI 先对用户说：「{prev}」\n用户接着对 AI 说：「{text}」\n"
                    "（要判断的是用户接着说的这一条，AI 那条只是上文。）",
    "model_message": "用户先对 AI 说：「{prev}」\nAI 接着对用户说：「{text}」\n"
                     "（要判断的是 AI 接着说的这一条，用户那条只是上文。）",
}
# Which sessions column stores each message event, and which column holds the other side.
_LAST_MESSAGE_COLUMNS = {
    "user_message": ("last_user_message", "last_model_message"),
    "model_message": ("last_model_message", "last_user_message"),
}
# Only the tail of a long previous message goes to Jev; the end is what the reply answers.
PREV_MESSAGE_MAX_CHARS = 1500


def _remember_message(db: HoronDB, event_type: str, text: str) -> str | None:
    """记下本会话这条消息，并取出对方的上一条。

    输入：事件类型（只处理 user_message / model_message，其他类型直接返回 None）、消息正文。
    行为：读 sessions 表里对方最近一条消息，再把这条写进本方那一列（会话还没登记就不写）。
    输出：对方上一条消息的末尾至多 PREV_MESSAGE_MAX_CHARS 个字；没有就返回 None。
    """
    cols = _LAST_MESSAGE_COLUMNS.get(event_type)
    if not cols or not text:
        return None
    own, other = cols
    row = db.conn.execute(
        f"SELECT {other} FROM sessions WHERE session_id = ?", (db.session_id,)).fetchone()
    db.conn.execute(
        f"UPDATE sessions SET {own} = ? WHERE session_id = ?", (text, db.session_id))
    db.conn.commit()
    prev = row[0] if row else None
    return prev[-PREV_MESSAGE_MAX_CHARS:] if prev else None


def _jev_state(event_type: str, text: str, tool_name: str | None, prev: str | None) -> str:
    """拼出送给 Jev 的情境：消息事件有对方上一条时一起送，否则只送这一条。"""
    if prev and event_type in JEV_STATE_WITH_PREV:
        return JEV_STATE_WITH_PREV[event_type].format(prev=prev, text=text)
    return JEV_STATE[event_type].format(tool=tool_name, text=text)


def _false_alarm_hint(db: HoronDB, concept_id: int) -> str:
    """接在提醒末尾的一句：误报时去哪看、改哪条 hook。

    输入：发出提醒的节点 ID。输出：传感器指向它自己的 read_hook；logic/guard 列出它的直接输入
    （名字和 ID），因为 hook 挂在输入传感器上。
    """
    row = db.conn.execute("SELECT role FROM concepts WHERE id = ?", (concept_id,)).fetchone()
    if row and row["role"] == "sensor":
        return f" (If you think this is a false alarm, run read_hook {concept_id} to see its rule and consider fixing its hook.)"
    members = db.conn.execute(
        "SELECT c.id, c.name FROM compose_members cm JOIN concepts c ON cm.member_concept_id = c.id "
        "WHERE cm.parent_concept_id = ? ORDER BY cm.order_index, c.id", (concept_id,)).fetchall()
    inputs = ", ".join(f"{m['name']} ({m['id']})" for m in members) or "(no inputs)"
    return f" (If you think this is a false alarm, consider fixing the hooks of its inputs: {inputs})"


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


def sync_session(db: HoronDB, adapter: str | None = None, *, reset: bool = False) -> str | None:
    """会话对齐（对齐的是 db 这个连接的会话）。每个钩子事件和每次 CLI 运行开头调用。

    输入：adapter 是宿主程序名称（例如 claude-code、codex、antigravity、opencode），
    供网页显示，也供 Tag 插件限制哪些宿主可以修改节点；
    reset=True 表示宿主发生了上下文压缩或清空（session_id 不变，但模型上下文已丢失），
    需要清空本会话的临时状态并重新返回提示。
    行为：首次见到的会话 → 登记并清空它的临时状态，跑一次数据库完整性检查
      （backend.integrity.check_at_session_start：缺向量后台补，电路组合违规附在提示后），返回就绪提示；
      reset=True 的会话 → 清空它的临时状态，返回上下文压缩重置提示；
      其余已登记的会话 → 只更新最后活跃时间。
      （不需要重算：逻辑节点和守卫不存状态，每次读取都按当前输入现算。）
    输出：首次登记或重置时的提示文字，否则 None。
    """
    row = db.conn.execute("SELECT session_id FROM sessions WHERE session_id = ?", (db.session_id,)).fetchone()
    if reset:
        db.init_session(adapter)
        return (
            f"This session was just compacted (Session: `{db.session_id}`), and Horon's temporary session state "
            "has been reset. The earlier `login` output and the `horon-cli` manual are no longer in context: "
            "run `python frontend/cli.py login` again and read the whole `horon-cli` manual (SKILL.md) first."
        )
    if not row:
        db.init_session(adapter)
        ready = f"Horon is ready (Session: `{db.session_id}`)."
        problems = check_at_session_start(db)
        return f"{ready}\n- {problems}" if problems else ready
    db.touch_session(adapter)
    return None


def end_turn(db: HoronDB) -> None:
    """回合结束：熄灭 db 这个连接的会话中所有 lifespan='turn' 的传感器并重新拓扑求值。"""
    db.turn_end()


def sense(
    db: HoronDB,
    event_type: str,
    text: str,
    tool_name: str | None = None,
) -> list[str]:
    """被动感知传入：匹配 sensor_hooks，点火传感器并沿拓扑图求值。

    输入：事件类型、事件文本（消息正文 / 工具结果 / 序列化的调用参数）、工具名。
    行为：
      1. 正则初筛：钩子的 tool 规则对工具名做 re.fullmatch（整串匹配、区分大小写，跨宿主写成 Bash|bash），
         match_pattern 对 text 做 re.search（忽略大小写）。写坏的正则跳过这条钩子，由开局完整性检查报告。
      2. 命中且没挂 Jev 题的钩子，直接点火。
      3. 命中且挂了 Jev 题（jev_question 列，可多道，见 parse_jev_questions）、本会话里还没亮的钩子，
         把所有钩子的所有题合成一次请求问 Jev（state 按 JEV_STATE 写成“谁对谁说：「text」”；用户消息和
         AI 回复会带上对方的上一条当上文，见 _remember_message）。一道题“是”的概率 >= 钩子的 jev_threshold 算命中；
         一个钩子任一道命中，它的传感器就点火。Jev 调用失败时这些钩子按正则结果点火，
         并多返回一条说明失败原因的消息。
      4. 每道题在 cli_audit_log 记一行 jev_check，sub_action 形如 q0 jev=0.08（第几题、“是”的概率）。
    输出：本次点火产生的 notify 消息列表，每条开头写着来源节点“notify from <id> <名字>: ”，
      经 Jev 点火的传感器在提醒后逐行接上命中的每道题：Jev 这次答“是”的概率、阈值、题目原文，
      末尾跟一句误报时该去改哪条 hook
      （不往通知队列里放东西）。
    """
    if not text and not tool_name:
        return []

    prev = _remember_message(db, event_type, text)

    query = (
        "SELECT sh.id AS hook_id, sh.sensor_concept_id, sh.event_type, sh.tool, "
        "sh.match_pattern, sh.jev_question, sh.jev_threshold, c.name "
        "FROM sensor_hooks sh "
        "JOIN concepts c ON sh.sensor_concept_id = c.id "
        "WHERE sh.event_type = ? "
        "AND (sh.adapter IS NULL OR sh.adapter = "
        "(SELECT adapter FROM sessions WHERE session_id = ?))"
    )
    rows = db.conn.execute(query, (event_type, db.session_id)).fetchall()
    activated: list[int] = []
    to_ask: list[Any] = []
    for r in rows:
        try:
            if r["tool"] and not re.fullmatch(r["tool"], tool_name or ""):
                continue
            hit = re.search(r["match_pattern"], text, re.IGNORECASE)
        except Exception:
            # 这里不发消息：model_message 的消息会让宿主强制续写，每次事件都发就停不下来。
            # 写坏的规则由开局完整性检查（backend.integrity.broken_regexes）报给 AI。
            continue
        if not hit:
            continue
        qs = [{"position": i, "question": q,
               "threshold": r["jev_threshold"] if r["jev_threshold"] is not None else 0.5}
              for i, q in enumerate(parse_jev_questions(r["jev_question"]))]
        if qs:
            to_ask.append((r, qs))
        else:
            activated.append(r["sensor_concept_id"])

    if to_ask:
        # A sensor already on in this session gets no new rising edge, so asking Jev
        # again could not change anything; skip it.
        states = load_active_states(db.conn, db.session_id)
        to_ask = [(r, qs) for r, qs in to_ask if not states.get(r["sensor_concept_id"])]

    messages: list[str] = []
    jev_hits: dict[int, list[str]] = {}   # sensor_id -> 命中那几道题的说明行（概率、阈值、题目）
    if to_ask:
        def key(r, q):
            return f"{r['hook_id']}.{q['position']}"
        try:
            answers = openrouter_post(DECISIONS_ENDPOINT, {
                "model": JEV_MODEL,
                "state": _jev_state(event_type, text, tool_name, prev),
                "questions": {
                    key(r, q): {
                        "type": "choice",
                        "instructions": q["question"],
                        "criteria": JEV_CHOICES,
                    }
                    for r, qs in to_ask for q in qs
                },
            }, timeout=20)["answers"]
            yes = {key(r, q): answers[key(r, q)]["probabilities"]["yes"] for r, qs in to_ask for q in qs}
        except Exception as e:
            activated += [r["sensor_concept_id"] for r, _ in to_ask]
            messages.append(
                f"Jev call failed: {e}\n"
                "Offline or timed out: check the network. No key, or HTTP 401/402: check OPENROUTER_API_KEY in .env "
                "and the OpenRouter balance. HTTP 404 No endpoints found usually means this version was superseded: "
                "look for a newer decider at openrouter.ai/perplexity; if there is one, update JEV_MODEL in "
                "backend/llm.py and run audit --concept to verify (the model page saying it is online is not reliable).\n"
                f"These sensors fired on their regex alone, without Jev: {', '.join(r['name'] for r, _ in to_ask)}")
        else:
            for r, qs in to_ask:
                hits = []
                for q in qs:
                    p_yes = yes[key(r, q)]
                    # One jev_check row per question; sub_action reads e.g. "q0 jev=0.08".
                    db.log_action(command="jev_check", concept_id=r["sensor_concept_id"],
                                  concept_name=r["name"], sub_action=f"q{q['position']} jev={p_yes:.2f}")
                    if p_yes >= q["threshold"]:
                        hits.append(f"Jev yes {p_yes:.2f} ≥ threshold {q['threshold']}: {q['question']}")
                if hits:
                    activated.append(r["sensor_concept_id"])
                    jev_hits[r["sensor_concept_id"]] = hits

    if activated:
        eval_res = db.evaluate(activated_sensors=activated)
        for fa in eval_res.fired_actions:
            actions = fa.action if isinstance(fa.action, list) else [fa.action] if isinstance(fa.action, dict) else []
            for act in actions:
                if isinstance(act, dict) and "notify" in act and act["notify"]:
                    # Name the node so a wrong alarm can be written back to it.
                    hits = jev_hits.get(fa.concept_id)
                    # 误报提示另起一行，免得粘在最后一道题后面像是题目的一部分。
                    detail = ("\n" + "\n".join(f"- {h}" for h in hits) + "\n") if hits else ""
                    messages.append(f"notify from {fa.concept_id} {fa.concept}: {act['notify']}{detail}"
                                    f"{_false_alarm_hint(db, fa.concept_id)}")

    return messages


_INHIBITOR_LIFESPAN_TEXT = {
    "turn": "turn sensor, stays on until this turn ends",
    "session": "session sensor, stays on until the session resets",
    "permanent": "permanent sensor, does not clear on its own",
}


def _describe_inhibitor(inhibition) -> str:
    """抑制源的名字；是传感器就附上它什么时候熄灭。logic 跟随上游，没有自己的寿命。"""
    lifespan = _INHIBITOR_LIFESPAN_TEXT.get(inhibition.inhibitor_lifespan)
    return f"{inhibition.inhibitor_name} ({lifespan})" if lifespan else inhibition.inhibitor_name


def _deny_reason(db: HoronDB, cid: int, guard_name: str, states: dict[int, int]) -> str:
    """守卫未通电时的拒绝理由（只看守卫的直接成员与抑制源，不下钻）。

    输入：守卫概念 id 与名字；states 是该会话的激活状态（load_active_states 的结果）；
      CHAIN 进度按 db 这个连接的会话读。
    行为：按守卫自身的 activation_type 选说法——
      AND   列出未激活的传感器 / 未激活的前置节点（全部都要满足）；
      OR    列出未激活成员，注明任一满足即可；
      CHAIN 报告序列进度与正在等待的成员（该成员已激活时，提示需要它重新发生一次）。
      未激活的 logic 成员可能缺前置也可能被抑制，这里不替它下结论，指路给 compile。
    输出：一句英文理由（不含 "Tool ... blocked" 前缀）。
    """
    inhs = db._get_inhibitions(cid, states, direction="incoming")
    target = guard_name.replace('"', '\\"')
    hint = f" (run `python frontend/cli.py compile --target \"{target}\"` to see why)"
    active = [i for i in inhs if i.inhibitor_is_active == 1]
    if active:
        names = ", ".join(_describe_inhibitor(i) for i in active)
        return f"locked by active inhibitor(s): {names}" + (hint if any(i.inhibitor_role != "sensor" for i in active) else "")

    members = db._get_compose_members(cid, states)
    if not members:
        return "[config error] this guard has no preconditions, so it can never pass"

    row = db.conn.execute("SELECT activation_type FROM concepts WHERE id = ?", (cid,)).fetchone()
    act = row["activation_type"] if row else None

    if act == "CHAIN":
        steps = {
            r["current_order"]
            for r in db.conn.execute(
                "SELECT current_order FROM active_chain_instances "
                "WHERE chain_concept_id = ? AND session_id = ?",
                (cid, db.session_id),
            ).fetchall()
        }
        total = max(m.order_index for m in members)
        k = max(steps) if steps else 0
        waiting = next((m for m in members if m.order_index == k + 1), None)
        if waiting is None:
            if k >= total:
                return "[internal bug] the sequence is complete but the guard state was not updated (inconsistent evaluation)"
            return f"[config error] sequence step numbers have a gap: step {k + 1}/{total} not found"
        if waiting.is_active == 1:
            return (
                f"sequence at step {k}/{total}, waiting for '{waiting.name}' to happen again"
                f" (it is already on; only a 0→1 change advances the sequence)"
            )
        reason = f"sequence at step {k}/{total}, waiting for: {waiting.name}"
        return reason + (hint if waiting.role != "sensor" else "")

    unlit_sensors = [m.name for m in members if m.is_active == 0 and m.role == "sensor"]
    unlit_logic = [m.name for m in members if m.is_active == 0 and m.role != "sensor"]
    if not unlit_sensors and not unlit_logic:
        return "[internal bug] all preconditions are met but the guard state was not updated (inconsistent evaluation)"

    if act == "OR":
        names = unlit_sensors + unlit_logic
        return f"any one of these preconditions is enough, none is met yet: {', '.join(names)}" + (hint if unlit_logic else "")

    parts = []
    if unlit_sensors:
        parts.append(f"sensors not on: {', '.join(unlit_sensors)}")
    if unlit_logic:
        parts.append(f"preconditions not powered: {', '.join(unlit_logic)}{hint}")
    return "; ".join(parts)


def guard(
    db: HoronDB,
    tool_name: str,
    tool_args: dict[str, Any] | str | None,
) -> tuple[bool, str | None, list[str]]:
    """主动运动门禁：检查工具调用的守卫权限。

    工作逻辑：
    1. 顺便匹配 tool_call 类型的传感器；
    2. 校验 tool_guards 表中的守卫规则：tool 规则对工具名做 re.fullmatch，args_pattern 对序列化参数做
       re.search（忽略大小写）；未激活则深入逆推缺失前置/活跃抑制源。规则的正则写坏时看不出它管不管这次调用，
       一律拦下并说明怎么修，不看守卫是否通电；工具名规则写坏时所有工具都被拦。
       参数里带 tool_guard / tool-guard 的调用不拦，否则修规则的命令也过不去；
    3. 返回 (allowed, deny_reason, fired_notifications)。通知不在这里入队，
       放行时由各 adapter 决定是入队还是直接送出，拦截时随拒绝理由一起返回。
    """
    if not tool_name:
        return True, None, []

    if isinstance(tool_args, str):
        args_json = tool_args
    elif isinstance(tool_args, (dict, list)):
        args_json = json.dumps(tool_args, ensure_ascii=False)
    else:
        args_json = "{}"

    # 1. 匹配 tool_call 传感器并点火
    fired_msgs = sense(db, event_type="tool_call", text=args_json, tool_name=tool_name)

    # 2. 检查 tool_guards 规则
    query = (
        "SELECT tg.id AS guard_rule_id, tg.guard_concept_id, tg.tool, tg.args_pattern, tg.adapter, c.name "
        "FROM tool_guards tg "
        "JOIN concepts c ON tg.guard_concept_id = c.id "
        "WHERE (tg.adapter IS NULL OR tg.adapter = "
        "(SELECT adapter FROM sessions WHERE session_id = ?))"
    )
    guards = db.conn.execute(query, (db.session_id,)).fetchall()
    states = load_active_states(db.conn, db.session_id)

    for g in guards:
        try:
            if not re.fullmatch(g["tool"], tool_name):
                continue
            pat = g["args_pattern"]
            if pat and not re.search(pat, args_json, re.IGNORECASE):
                continue
        except Exception as e:
            # 重新绑定会整条替换规则，所以命令要保持原规则的形状，否则规则的范围会变。
            args_arg = " --args-pattern '<regex>'" if g["args_pattern"] else ""
            adapter_arg = f" --adapter {g['adapter']}" if g["adapter"] else ""
            broken = (f"Guard {g['guard_concept_id']} '{g['name']}' has a broken regex ({e}): "
                      f"tool {g['tool']!r}, args_pattern {g['args_pattern']!r}. "
                      f"Calls are blocked until you bind it again: "
                      f"set {g['guard_concept_id']} tool_guard '<tool>'{args_arg}{adapter_arg}")
            # 修这条规则的调用本身不拦：Bash 守卫写坏后，修它的命令也要走 Bash。
            if re.search(r"tool[_-]guard", args_json):
                fired_msgs.append(broken)
                continue
            return False, broken, fired_msgs

        if states[g["guard_concept_id"]] == 0:
            try:
                reason = _deny_reason(db, g["guard_concept_id"], g["name"], states)
            except Exception as e:
                reason = f"[internal error] guard state check failed: {e}"

            return False, f"Tool '{tool_name}' call blocked by guard '{g['name']}': {reason}.", fired_msgs

    return True, None, fired_msgs


def drain(
    db: HoronDB,
    extra_messages: list[str] | None = None,
) -> str:
    """排空 db 这个连接的会话的 pending 通知，并合并额外消息，打包带 【Horon Harness】 信头的广播。"""
    all_msgs: list[str] = []
    if extra_messages:
        all_msgs.extend(extra_messages)

    pending = db.pop_pending_notifications()
    if pending:
        all_msgs.extend(pending)

    return wrap_harness_message(all_msgs)
