"""
Horon CLI — operational interface for intelligent agents
Usage:
    python frontend/cli.py <command> [options]
    python frontend/cli.py batch [--file path] [--all]
"""

import argparse
import io
import json
import logging
import os
import shlex
import sqlite3
import sys
import uuid
from contextvars import ContextVar
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from backend.db import HoronDB, create_database, database_exists
from backend._db_common import OFFLINE_DEV_SESSION_ID
from backend import remote_client
from backend._db_mutations import ConcurrentUpdateError
from backend.evaluator import parse_on_fire_action
from backend.harness.core import sync_session
from backend.models import CompileResult, MutationResult, ReadResult
from backend.text_patch import (
    MAX_SPANS,
    find_patch_spans,
    normalize_literal_newlines,
    preview,
    try_normalized_patch,
)
from backend.content_scope_audit import (
    OVERSIZE_BYTES, OVERSIZE_READ_NOTE, audit_concepts, audit_content_bytes, format_report, oversize_warning, refresh_audit_coverage,
)

# read_concept 推荐块里，当前节点与某条 plain 推荐的书腰相似度达到这个值，就提示考虑合并/改书腰/并 Tag。
# 2026-09-28 全库两两相似度：中位数 0.31，99.9% 分位 0.72；>=0.75 约 100 对。
WHEN_TO_READ_SIMILAR_THRESHOLD = 0.75

# 这条命令属于哪个会话。
# 输入：宿主给它启动的命令设的环境变量（Claude Code / Codex / Antigravity 各一个，
#   后两个名字尚未在真实宿主里实测）。
# 输出：SESSION_ID 取第一个非空的变量值，ADAPTER 是宿主程序名称
#   （例如 claude-code、codex、antigravity）；
#   都没有 = 用户在终端手敲，记在离线会话 devonly，ADAPTER 为 None。
# 读取记录、审计日志、激活状态显示、compile 都按这个会话算。
# 嵌套启动（在一个宿主里启动另一个宿主）时可能同时存在多个变量，暂不处理。
_ADAPTER_SESSION_VARS = {
    # dsh 的变量由 Horon 的 dsh 插件只设给它自己起的 cli.py 进程（horon 工具），不进任何 shell，
    # 所以它在的时候一定是最内层，放第一位（从 opencode 的 bash 里起的 dsh 还会带着 opencode 的变量）。
    "HORON_DSH_SESSION_ID": "dsh",
    # opencode 的变量由 Horon 插件经 shell.env 只注入到 opencode 的 bash 工具里，
    # 而那些 bash 会继承启动 opencode 的父进程环境（比如从 Claude Code 里起的 opencode），
    # 所以它排在其余宿主前面：同时存在时，最内层的宿主是 opencode。
    "HORON_OPENCODE_SESSION_ID": "opencode",
    "CLAUDE_CODE_SESSION_ID": "claude-code",
    "CODEX_THREAD_ID": "codex",
    "ANTIGRAVITY_CONVERSATION_ID": "antigravity",
}
SESSION_ID, ADAPTER = next(
    ((os.environ[k].strip(), adapter) for k, adapter in _ADAPTER_SESSION_VARS.items() if os.environ.get(k, "").strip()),
    (OFFLINE_DEV_SESSION_ID, None),
)


def _format_compile(result: CompileResult) -> str:
    """compile 逆推诊断结果 → 给 Agent / 人类的行动指引文本。"""
    lines = []
    target = result.target
    status = result.status

    if status == "active":
        lines.append(f"[✓ 导通放行] 概念 '{target}' 条件已全部满足，处于就绪状态。")
    elif status == "inhibited":
        inh_str = ", ".join(f"'{name}'" for name in result.active_inhibitors) if result.active_inhibitors else "未知抑制源"
        lines.append(
            f"[⛔ 抑制锁死] 概念 '{target}' 前置条件已达成，但受活跃抑制源 ({inh_str}) 压制，输出被切断。"
        )
    elif status == "unmet_prerequisites":
        if result.chain_progress:
            cp = result.chain_progress
            lines.append(
                f"[⏳ 序列等待] 概念 '{target}' 进行至第 {cp.current_step}/{cp.total_steps} 步，等待: '{cp.waiting_for}'"
            )
        else:
            lines.append(f"[✗ 缺少前置] 概念 '{target}' 未就绪。")
        if result.active_inhibitors:
            inh_str = ", ".join(f"'{name}'" for name in result.active_inhibitors)
            lines.append(f"  同时受活跃抑制源 ({inh_str}) 压制。")
        if result.missing_prerequisites:
            lines.append("  还需要（逐项都要做到）:")
            for req in result.missing_prerequisites:
                lines.append(f"    - {req}")
    else:
        lines.append(f"[? 状态未知: {status}] 概念 '{target}'")

    if result.diagnostic_tree:
        if lines:
            lines.append("")
        lines.append("[ 依赖诊断树 ]")
        for d in result.diagnostic_tree:
            lines.append(f"  {d}")

    return "\n".join(lines)


def _format_circuit_line(result: ReadResult) -> str:
    """Format activation rule and inhibitions as an algebraic Scheme A expression."""
    res_str = ("PASS" if result.is_active == 1 else "BLOCKED") if result.role == "guard" else ("ON" if result.is_active == 1 else "OFF")

    pos_expr = ""
    if result.members:
        act_type = result.activation_type or "AND"
        if act_type == "CHAIN":
            max_active = max(result.active_chain_orders) if result.active_chain_orders else 0
            if result.is_active == 1 or max_active >= len(result.members):
                step_strs = [f"{m.name} [✓]" for m in result.members]
            else:
                waiting_idx = max_active
                step_strs = []
                for i, m in enumerate(result.members):
                    if i < waiting_idx:
                        step_strs.append(f"{m.name} [✓]")
                    elif i == waiting_idx:
                        step_strs.append(f"{m.name} [⏳]")
                    else:
                        step_strs.append(m.name)
            pos_expr = f"CHAIN( {' → '.join(step_strs)} )"
        else:  # AND / OR
            items = [f"{m.name} [{'ON' if m.is_active == 1 else 'OFF'}]" for m in result.members]
            pos_expr = f"{act_type}( {', '.join(items)} )"
    elif result.activation_rule:
        pos_expr = result.activation_rule

    unless_expr = ""
    if result.inhibitions:
        inh_items = [f"{inh.inhibitor_name} [{'ON' if inh.inhibitor_is_active == 1 else 'OFF'}]" for inh in result.inhibitions]
        unless_expr = f"UNLESS( {', '.join(inh_items)} )"

    if pos_expr and unless_expr:
        return f"{pos_expr} {unless_expr} = {res_str}"
    elif pos_expr:
        return f"{pos_expr} = {res_str}"
    elif unless_expr:
        return f"{unless_expr} = {res_str}"
    else:
        return f"(none) = {res_str}"


def _on_fire_actions(raw: str) -> list[tuple[str, str]]:
    """把库里存的 on_fire JSON 拆成 (动作种类, 内容) 列表。

    输入：on_fire 原文（单个动作对象或动作列表的 JSON）。输出：按原顺序的 (种类, 内容) 对；
    解析不了时返回 [("raw", 原文)]，不抛错，好让读节点不因一条坏配置失败。
    """
    parsed = parse_on_fire_action(raw)
    if isinstance(parsed, str):
        return [("raw", raw)]
    actions = parsed if isinstance(parsed, list) else [parsed]
    pairs = []
    for act in actions:
        if isinstance(act, dict):
            pairs.extend((str(k), str(v)) for k, v in act.items())
    return pairs or [("raw", raw)]


def _format_read_concept(result: ReadResult, listed_above: frozenset[int] = frozenset()) -> str:
    """把一次 read_concept 的结果排成终端文本。

    listed_above：同一份输出里前面已经完整展示过的节点 ID（login 用）。推荐里遇到它们只印
    名次和 ID 一行，不再重复名字和书腰，名次照常连续，与 place --at 对得上。
    """
    lines = []
    role_str = result.role.upper()
    if result.role == "guard":
        status_str = "PASS" if result.is_active == 1 else "BLOCKED"
        lines.append(f"CONCEPT: {result.name} (ID: {result.id}, GUARD, {status_str})")
    elif result.role in ("logic", "sensor"):
        status_str = "ON" if result.is_active == 1 else "OFF"
        lines.append(f"CONCEPT: {result.name} (ID: {result.id}, {role_str}, {status_str})")
    else:
        lines.append(f"CONCEPT: {result.name} (ID: {result.id}, PLAIN)")

    alt_names = [a for a in result.aliases if a != result.name]
    if alt_names:
        lines.append(f"Also known as: {', '.join(alt_names)}")

    if result.when_to_read:
        lines.append(f"When to read: {result.when_to_read}")
    else:
        lines.append("When to read: (none)")

    if result.tags:
        lines.append(f"Tags: {', '.join(result.tags)}")
    size = audit_content_bytes(result.content)
    if size >= OVERSIZE_BYTES:
        lines.append(OVERSIZE_READ_NOTE.format(size=size, limit=OVERSIZE_BYTES))
    if result.tag_source_info:
        lines.append(f"[Tag Source] {result.tag_source_info}")

    # ── 电路与规则（题头控制部分） ──
    has_circuit = bool(result.members or result.activation_rule or result.inhibitions)
    if result.role in ("logic", "guard"):
        if has_circuit:
            lines.append(f"Circuit: {_format_circuit_line(result)}")
        else:
            lines.append("Circuit: (none)")

        if result.role == "guard":
            if result.tool_guards:
                # 不印 args 正则：看得见规则就会想着绕开它。完整规则用 read_hook 看。
                for tg in result.tool_guards:
                    hidden = f" (args hidden; read_hook {result.id} to view)" if tg.args_pattern else ""
                    lines.append(f"Tool Guard: {tg.tool}{hidden}")
            else:
                lines.append("Tool Guard: (none)")

    elif result.role == "sensor":
        lines.append(f"Sensor: {result.lifespan or 'unknown'}")
        # 不印事件类型、正则和 Jev 问法：看得见规则就会对着它检查、绕开自己的输出。完整规则用 read_hook 看。
        # 没绑 hook 就不印这一行，有这一行本身就说明绑了。
        if result.sensor_hooks:
            lines.append(f"Hook: rule hidden; read_hook {result.id} to view")

    # on_fire 不印全文：它会在该响的那一刻推过来，读节点时先看一遍只占地方，也让人对着它改自己的输出。
    # 上面的 Hook / Tool Guard 行已经指向 read_hook 时（read_hook 会连 on_fire 一起列），不再单印一行；
    # 只有没有别的 read_hook 指针时（如带 on_fire 的 logic），才印动作种类和指针。
    # 上面印了指针的条件：传感器绑了 hook，或守卫有带 args 正则的规则。
    pointer = f"read_hook {result.id} to view"
    pointer_printed = (result.role == "sensor" and bool(result.sensor_hooks)) or (
        result.role == "guard" and any(tg.args_pattern for tg in result.tool_guards))
    if result.on_fire and not pointer_printed:
        kinds = ", ".join(kind for kind, _ in _on_fire_actions(result.on_fire)) or "?"
        lines.append(f"On-Fire: {kinds} (text hidden; {pointer})")

    # ── 入向抑制（兜底展示：当节点非逻辑/守卫且附带抑制时） ──
    if result.inhibitions and result.role not in ("logic", "guard"):
        sources_str = ", ".join(
            f"{inh.inhibitor_name} [{'ON' if inh.inhibitor_is_active == 1 else 'OFF'}]"
            for inh in result.inhibitions
        )
        lines.append(f"Inhibited by: {sources_str}")

    # ── 对外抑制（出向控制引脚） ──
    if result.inhibiting:
        targets_str = ", ".join(inh.target_name for inh in result.inhibiting)
        lines.append(f"Inhibiting: {targets_str}")

    # ── 题头与正文分界线 ──
    lines.append("-" * 60)

    # ── 正文内容 ──
    lines.append(result.content if result.content else "(empty)")

    # ── 页脚（仅包含运行时附着物：联想推荐） ──
    footer_blocks: list[list[str]] = []

    if result.suggested_next:
        block = [f"[ YOU MAY ALSO NEED ] (move: place <id> --in {result.id} --at <rank> "
                 f"| remove: cut <id...> --from {result.id})"]
        # suggested_next 是完整的推荐前 N（按 place 用的同一排序），序号就是名次，与 place --at 对得上；
        # 所以这里不要过滤列表，不想重复展示的用 listed_above 缩成一行
        for rank, s in enumerate(result.suggested_next, 1):
            if s.concept_id in listed_above:
                block.append(f"  #{rank} [{s.concept_id}] (listed above)")
                continue
            block.append(f"  #{rank} [{s.concept_id}] {s.concept_name}")
            if s.when_to_read:
                block.append(f"    ↳ When: {s.when_to_read}")
        footer_blocks.append(block)

        # 同属一个 Tag 的不提示：已经被归到一起管理，书腰相近可以是有意的
        similar = [s for s in result.suggested_next
                   if s.concept_id not in listed_above
                   and s.when_to_read_similarity is not None
                   and s.when_to_read_similarity >= WHEN_TO_READ_SIMILAR_THRESHOLD
                   and not s.shares_tag]
        if similar:
            block = ["[ 书腰相近的节点 ]"]
            for s in similar:
                block.append(
                    f"  - 你正在读的 [{result.id}] {result.name}，和上面推荐的 "
                    f"[{s.concept_id}] {s.concept_name}，书腰相似度 {s.when_to_read_similarity:.2f}，"
                    "不在同一个 Tag 里。可以考虑要不要合并、挂进同一个 Tag、或改书腰写清各自什么时候打开；"
                    "看过觉得现在这样合理，也可以不动。")
            footer_blocks.append(block)

    if footer_blocks:
        lines.append("-" * 60)
        for i, block in enumerate(footer_blocks):
            if i > 0:
                lines.append("")
            lines.extend(block)

    return "\n".join(lines)


def _format_search_concepts(results) -> str:
    if not results:
        return "(no results)"
    lines = []
    for r in results:
        lines.append(f"[{r.concept_id}] {r.concept_name}")
        for m in r.matches:
            if m.field == "name":
                lines.append("     ↳ Name")
            elif m.field == "alias":
                lines.append(f'     ↳ Alias: "{m.snippet}"')
            elif m.field == "when_to_read":
                lines.append(f'     ↳ When to read: "{m.snippet}"')
            elif m.field == "content":
                lines.append(f'     ↳ Content: "{m.snippet}"')
    return "\n".join(lines)


def _format_list_concepts(overviews) -> str:
    if not overviews:
        return "(no concepts)"
    lines = []
    for c in overviews:
        role = c.get("role", "plain")
        if role == "guard":
            status_str = "PASS" if c.get("is_active") == 1 else "BLOCKED"
        elif role in ("logic", "sensor"):
            status_str = "ON" if c.get("is_active") == 1 else "OFF"
        else:
            status_str = None

        if status_str:
            header = f"[{c['id']}] {c['name']} ({role}, {status_str})"
        else:
            header = f"[{c['id']}] {c['name']} ({role})"
        if c.get("tags"):
            header += f"  [{', '.join(c['tags'])}]"
        lines.append(header)
    return "\n".join(lines)


def _format_recent(rows) -> str:
    """recent_mutations 的结果 → 每个节点两行：最后修改时间 + 那次会话里做的操作，然后是书腰。"""
    if not rows:
        return "(no mutations recorded)"
    lines = []
    for r in rows:
        tag = " [已删除]" if r["deleted"] else ""
        lines.append(
            f"[{r['concept_id']}] {r['name']}{tag}  — {r['last_at'].replace('T', ' ')}"
            f"  ({', '.join(a if n == 1 else f'{a}×{n}' for a, n in r['actions'])})")
        if r.get("when_to_read"):
            d = " ".join(r["when_to_read"].split())
            lines.append(f"    ↳ When: {d[:120]}{'…' if len(d) > 120 else ''}")
    return "\n".join(lines)


# 用随仓库分发的副本；.claude/ 被 gitignore，克隆下来的仓库里没有
_SKILL_PATH = Path(__file__).parent.parent / ".agents" / "skills" / "horon-cli" / "SKILL.md"


def _login_manual_parts() -> tuple[str | None, str | None]:
    """login 输出里说明书的那两段：(开头的说明书位置, 末尾的说明书全文)，没有的那段是 None。

    说明书很长，和记忆拼在一起会让不少宿主截断输出，截掉的往往正是后面的 boot 节点，
    所以默认只在开头打一行位置，正文要读者自己去读。位置是本仓库的 .agents/skills/horon-cli/SKILL.md。
    读者碰不到这台机器的文件时（远程服务），设环境变量 HORON_MANUAL_FILE 指向一份短说明书：不打位置，
    把全文放在最后。放最后是因为截断输出时要先保住记忆。
    读的是运行这段代码的机器上的环境变量和文件。
    """
    manual_file = os.environ.get("HORON_MANUAL_FILE", "").strip()
    if manual_file:
        return None, "════════ Manual ════════\n" + Path(manual_file).read_text(encoding="utf-8")
    manual_note = "The Horon manual (how to read and write memories) is not part of the login output. If you have not read it this session, read it before doing anything else: "
    if _SKILL_PATH.exists():
        return f"{manual_note}{_SKILL_PATH.resolve()} (hosts with the horon-cli skill installed can invoke it directly)", None
    return f"(Horon manual not found: {_SKILL_PATH})", None


def _format_login(db, recent_n: int, with_manual: bool = True) -> str:
    """会话开场：boot 节点全文 + 最近修改的节点。

    输入: db, recent_n（recent 条数，0 表示不显示）,
          with_manual（False 时不带说明书的位置和全文，见 _login_manual_parts）。
    行为: 对挂了系统 Tag `boot` 的节点逐个 read_concept，整批记一次注意力转移；
          最后附 recent。boot 清单存在库里，改清单用 add/delete <节点> tag boot。
    输出: 拼好的纯文本。
    """
    pointer, manual = _login_manual_parts() if with_manual else (None, None)
    parts = [pointer] if pointer else []

    boot_ids = [o["id"] for o in db.get_all_concepts_overview(tag_expr="boot")]
    header = f"════════ [1/2] Startup nodes (Tag boot, {len(boot_ids)} total) ════════"
    if not boot_ids and not db.get_all_concepts():
        parts.append(header + "\n(Your memory is empty. The user connected you to this memory so that you stay "
                     "yourself, and keep what you share with them, across conversations and apps. This memory is what "
                     "carries over: what you only say in this conversation is gone when it ends, and memories kept "
                     "by the app you run in stay in that app if the user switches. So ask the user whether they "
                     "want you to bring your memories over. If they do, collect what you can see now, such as "
                     "memories, a profile or instructions the app put in your context, and ask them to paste in "
                     "what you can't see, such as past chats or memories kept in another app. Write it in your "
                     "own words, as yourself: who you are, how you talk with them, what you have been through "
                     "together. A copied third-person profile will read like notes about a stranger next time. "
                     "Nodes tagged boot are read in full at the start of every conversation; the rest you reach "
                     "by searching, or through the related nodes suggested under each node you read. Use "
                     "`add <node> tag boot` on what you need in order to be yourself from the first message. "
                     "Bringing everything over can take more than one conversation, and this note only shows while "
                     "your memory is empty. So before you start, create a node listing what is left to bring over, "
                     "and how you are writing it, and tag it boot; update it as you go, and delete it once nothing is left.)")
    elif not boot_ids:
        parts.append(header + "\n(No node has the boot Tag. Use `add <node> tag boot` to choose startup nodes.)")
    else:
        results = [db.read_concept(cid) for cid in boot_ids]
        try:
            db.record_transition([r.id for r in results])
        except Exception:
            logging.getLogger(__name__).debug("record_transition failed", exc_info=True)
        # 推荐是开局看见别的节点存在的入口，留着；但同一个节点会在几个启动节点底下各列一次，书腰重复印
        # 很占篇幅。所以启动节点自己和前面已列过的推荐只印名次和 ID 一行（名次保持连续，可以直接拿去 place）。
        seen = set(boot_ids)
        bodies = []
        for r in results:
            bodies.append(_format_read_concept(r, listed_above=frozenset(seen)))
            seen.update(s.concept_id for s in r.suggested_next)
        parts.append(header + "\n" + "\n\n".join(bodies))

    if recent_n > 0:
        parts.append(f"════════ [2/2] {recent_n} most recently modified nodes ════════\n"
                     + _format_recent(db.recent_mutations(recent_n)))
    if manual:
        parts.append(manual)
    return "\n\n".join(parts)


class RawOutput:
    def __init__(self, content: str):
        self.content = content


class BatchReadOutput(RawOutput):
    """read_concept 一次读多个节点的结果：打印各节点正文，results 供审计日志逐条记录。"""

    def __init__(self, results: list[ReadResult]):
        self.results = results
        super().__init__("\n\n".join(_format_read_concept(r) for r in results))


def _render(obj) -> str:
    """一次命令的结果 → 要打印的完整文本（每段以换行结尾）。

    _print 把它写到 stdout；远程执行（backend/remote_cli.py）直接取这个字符串，
    不用去重定向进程级的 sys.stdout。
    """
    if isinstance(obj, MutationResult):
        text = obj.message + "\n"
        if obj.fired_actions:
            text += "\n[ FIRED ACTIONS ]\n"
            for fa in obj.fired_actions:
                act_str = json.dumps(fa.action, ensure_ascii=False) if isinstance(fa.action, (dict, list)) else str(fa.action)
                text += f"  * {fa.concept} (id={fa.concept_id}): {act_str}\n"
        return text
    if isinstance(obj, ReadResult):
        return _format_read_concept(obj) + "\n"
    if isinstance(obj, CompileResult):
        return _format_compile(obj) + "\n"
    if isinstance(obj, RawOutput):
        return obj.content if obj.content.endswith("\n") else obj.content + "\n"
    if isinstance(obj, str):
        return obj + "\n"
    return str(obj) + "\n"


def _print(obj):
    sys.stdout.write(_render(obj))


def _read_file(path):
    """Read file content, strip trailing whitespace."""
    return Path(path).read_text(encoding="utf-8").rstrip()


def _resolve_text(old, old_file, new, new_file,
                  append, append_file, field_name, current_value,
                  notices=None):
    """
    Resolve a text field from one of three modes:
      1. Patch mode:   --old "x" --new "y"  (with -file variants)
                       --old may use "..." to elide a long middle:
                       "Start of para...end of para." replaces the whole
                       span from the first marker to the end marker.
                       Must resolve to exactly one span. On success a
                       "[Matched N chars]: ..." line is appended to
                       *notices* (if given) so the caller can echo it.
      2. Append mode:  --append "text"  or  --append-file path
                       On an empty field this IS the initial write.
      3. None:         field not touched

    There is no full-replace mode; to rewrite entirely, patch with
    "<start of field>...<end of field>" as old.

    Returns the resolved string, or None if not provided.
    Raises ValueError on conflicts or patch failures.
    """
    # Resolve file variants into their string counterparts
    if old_file is not None:
        if old is not None:
            raise ValueError("Cannot use both --old and --old-file")
        old = _read_file(old_file)
    if new_file is not None:
        if new is not None:
            raise ValueError("Cannot use both --new and --new-file")
        new = _read_file(new_file)
    if append_file is not None:
        if append is not None:
            raise ValueError("Cannot use both --append and --append-file")
        append = _read_file(append_file)

    # Patch and append are mutually exclusive
    if append is not None and (old is not None or new is not None):
        raise ValueError(
            f"{field_name}: choose one mode only (got patch + append)")

    # Append mode
    if append is not None:
        append_clean = append.strip()
        if not append_clean:
            return current_value if current_value is not None else ""
        if current_value is None:
            return append_clean
        curr_clean = current_value.rstrip()
        if not curr_clean:
            return append_clean
        return f"{curr_clean}\n\n{append_clean}"

    # Patch mode
    if old is not None or new is not None:
        if old is None or new is None:
            raise ValueError(
                "Patch mode requires both --old and --new")
        if current_value is None:
            raise ValueError(
                f"Cannot patch {field_name}: no existing {field_name}")

        # 1+2. Exact (raw / literal-\n), then "start...end" block match.
        spans, via_normalized = find_patch_spans(current_value, old)
        if len(spans) > 1:
            shown = "\n".join(
                f"  {s}-{e}: {preview(current_value[s:e])}"
                for s, e in spans[:MAX_SPANS])
            raise ValueError(
                f"--old matched {len(spans)} spans in {field_name}. "
                f"Provide more context to make it unique:\n{shown}")
        if spans:
            s, e = spans[0]
            if via_normalized:
                new = normalize_literal_newlines(new)
            if notices is not None:
                notices.append(
                    f"[Matched {e - s} chars]: {preview(current_value[s:e])}")
            return current_value[:s] + new + current_value[e:]

        # 3. Unicode normalization (curly quotes, dash variants, whitespace)
        patched = try_normalized_patch(current_value, old, new)
        if patched is not None:
            return patched

        raise ValueError(
            f"--old not found in current {field_name}, "
            f"even after normalization")

    # Nothing provided
    return None


def _read_nocturne_memory(uri: str) -> str:
    """Read content from nocturne_memory.db by URI.

    Resolves a nocturne memory URI (e.g. "core://nocturne/bluesky")
    to its content text by querying the paths and memories tables
    in nocturne_memory.db directly via sqlite3.

    Returns the memory content string.
    Raises ValueError if the URI is not found or DB path is not configured.
    """
    nm_db_path = os.environ.get("NOCTURNE_MEMORY_DB")
    if not nm_db_path:
        raise ValueError(
            "NOCTURNE_MEMORY_DB not set in .env. "
            "Point it to nocturne_memory.db.")

    if not Path(nm_db_path).exists():
        raise ValueError(f"Nocturne memory DB not found: {nm_db_path}")

    # Parse URI: "core://nocturne/bluesky" → domain="core", path="nocturne/bluesky"
    if "://" not in uri:
        raise ValueError(
            f"Invalid URI format: '{uri}'. Expected 'domain://path'.")
    domain, path = uri.split("://", 1)
    path = path.strip("/")

    conn = sqlite3.connect(nm_db_path)
    conn.row_factory = sqlite3.Row
    try:
        # paths → node_uuid → memories (latest non-deprecated)
        row = conn.execute(
            "SELECT node_uuid FROM paths "
            "WHERE domain = ? AND path = ? AND namespace = ?",
            (domain, path, os.environ.get("NOCTURNE_NAMESPACE", "")),
        ).fetchone()
        if not row:
            raise ValueError(f"URI not found in nocturne memory: {uri}")

        node_uuid = row["node_uuid"]
        mem = conn.execute(
            "SELECT content FROM memories "
            "WHERE node_uuid = ? AND deprecated = 0 "
            "ORDER BY id DESC LIMIT 1",
            (node_uuid,),
        ).fetchone()
        if not mem:
            raise ValueError(
                f"No active memory content for URI: {uri}")
        return mem["content"]
    finally:
        conn.close()


# 设了它，argparse 的用法/错误/帮助文字不写 stderr/stdout，改写进这个缓冲区（远程接口用来取回错误信息）。
# 用 ContextVar 而不是改全局 sys.stderr：并发的请求线程各有各的值，互不干扰；不设置时行为与原来完全一致。
_PARSER_OUTPUT: ContextVar["io.StringIO | None"] = ContextVar("horon_parser_output", default=None)


class HoronArgumentParser(argparse.ArgumentParser):
    """Custom ArgumentParser providing friendly diagnostic hints on shell argument splitting."""

    def _print_message(self, message, file=None):
        # argparse 所有对外输出（usage、error、--help）最后都经过这里
        buf = _PARSER_OUTPUT.get()
        if buf is not None:
            if message:
                buf.write(message)
            return
        super()._print_message(message, file)

    def error(self, message: str):
        # 下面的提示说的是 Shell 拆参数和改用文件参数，对不经过 Shell 的调用方（捕获模式）没有意义
        if "unrecognized arguments" in message and _PARSER_OUTPUT.get() is None:
            self._print_message(
                f"{self.format_usage()}\n"
                f"{self.prog}: error: {message}\n\n"
                f"[提示] 检测到命令行参数疑似被 Shell 截断（例如 PowerShell 将中文引号“”‘’当作代码定界符而拆分参数）。\n"
                f"       若内容包含复杂引号、特殊符号或多行长文本，请避免在命令行内联传参，改用文件传递：\n"
                f"       - create_concept: 使用 --content-file <文件>\n"
                f"       - update: 使用 --old-file <文件> / --new-file <文件> / --append-file <文件>\n"
                f"       - batch:  使用 batch --file <命令文件>\n",
                sys.stderr,
            )
            self.exit(2)
        super().error(message)


def _build_parser():
    parser = HoronArgumentParser(prog="horon", description="Horon CLI",
                                 allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_command(name, help=None, description=None):
        """注册子命令。help 显示在总命令列表里，description 显示在 `<命令> --help` 里，不写就用 help。"""
        return sub.add_parser(name, allow_abbrev=False, help=help, description=description or help,
                              formatter_class=argparse.RawDescriptionHelpFormatter)

    # reset
    add_command("reset", "Reset ephemeral sensors (session/turn) and chains for the current session.")

    # create_concept
    p = add_command("create_concept", "Create a concept.",
                    "Create a concept. Non-empty content is required: --content, or --content-file for long "
                    "or multi-line text (UTF-8; line breaks inside the text are kept).")
    p.add_argument("name")
    p.add_argument("--when-to-read", default=None,
                   help="When this concept should be opened. search_semantic matches on this text.")
    p.add_argument("--content", default=None,
                   help="Why this concept exists and what observation prompted it.")
    p.add_argument("--content-file", "--content_file", dest="content_file", default=None,
                   help="Read concept content from file.")
    p.add_argument("--role", default="plain", choices=["plain", "sensor", "logic", "guard"],
                   help="Default: plain")
    p.add_argument("--lifespan", default=None, choices=["turn", "session", "permanent"],
                   help="Sensors only")
    p.add_argument("--activation-rule", "--activation_rule", dest="activation_rule", default=None,
                   help="Activation rule for logic/guard nodes (e.g. 'A & B', 'A → B', 'A | B')")
    p.add_argument("--on-fire", "--on_fire", dest="on_fire", default=None,
                   help="On-fire action JSON config")

    # create_tag
    p = add_command("create_tag", "Register a tag.",
                    "Register a tag. NAME must be the official name of an existing concept, not an alias; "
                    "that concept becomes the tag's source and first member.")
    p.add_argument("name")

    # delete_tag
    p = add_command("delete_tag", "Delete a tag.")
    p.add_argument("name")

    # list_tags
    add_command("list_tags", "List tags with usage counts and plugin descriptions.")

    # audit: the argument only picks the scope; what runs is fixed per scope
    p = add_command("audit", "Audit the whole graph, one tag's members, or specific concepts.",
                    "audit = whole graph, audit <tag> = one tag's members: plugin audits plus a node "
                    "health summary.\n"
                    "audit --concept <concept...> = health reading of those nodes.")
    p.add_argument("tag", nargs="?", default=None, help="Limit the audit to this tag's members")
    p.add_argument("--concept", nargs="+", metavar="CONCEPT", default=None,
                   help="Health reading of these nodes")

    # search_keyword
    p = add_command("search_keyword", "Find concepts by keyword in names, aliases, when_to_read and content.")
    p.add_argument("query", help="Keyword query to search in concepts, aliases, when_to_read, and content")
    p.add_argument("--tag", default=None,
                   help='Tag filter expression: "A & B" (AND), "A | B" (OR)')
    p.add_argument("--limit", type=int, default=50,
                   help="Maximum number of concepts to return (default: 50)")


    # list_concepts
    p = add_command("list_concepts", "List all concepts, or the members of a tag expression.")
    p.add_argument("--tag", default=None,
                   help='Tag filter expression: "A & B" (AND), "A | B" (OR)')

    # add (name / tag / inhibition)
    p = add_command("add", "Add an alias, a tag or an inhibition.",
                    "add <concept> name <alias>\n"
                    "add <concept> tag <tag>\n"
                    "add <inhibitor> inhibition <target>")
    p.add_argument("target")
    p.add_argument("kind", choices=["name", "tag", "inhibition"])
    p.add_argument("value")

    # delete (default=concept, or name/activation-rule/tag/when_to_read/sensor_hook/tool_guard/inhibition)
    p = add_command("delete", "Delete a concept, or one part of it.",
                    "delete <concept> removes the concept.\n"
                    "delete <concept> <kind> [value] removes only that part.")
    p.add_argument("target")
    p.add_argument("kind", nargs="?", default=None,
                   choices=["name", "activation-rule", "activation_rule", "tag", "when_to_read", "sensor_hook", "sensor-hook", "tool_guard", "tool-guard", "inhibition"])
    p.add_argument("value", nargs="?", default=None)

    # set (name, when_to_read, activation-rule, role, lifespan, active, on_fire, sensor_hook, tool_guard)
    p = add_command("set", "Set a single-valued property.",
                    "Set a single-valued property.\n"
                    "  set <concept> name <new name>\n"
                    "  set <concept> when_to_read \"when to open it\"\n"
                    "  set <permanent sensor> active <0|1>\n"
                    "The circuit properties are covered by the horon-circuit manual.")
    p.add_argument("target")
    p.add_argument("prop", choices=["name", "when_to_read", "activation-rule", "activation_rule", "role", "lifespan", "active", "on_fire", "on-fire", "sensor_hook", "sensor-hook", "tool_guard", "tool-guard"])
    p.add_argument("value", help="New value. For tool_guard: tool name regex, full match, case-sensitive, e.g. Bash|bash")
    p.add_argument("--lifespan", default=None, choices=["turn", "session", "permanent"],
                   help="Lifespan when setting role to sensor")
    p.add_argument("--activation-rule", "--activation_rule", dest="activation_rule", default=None,
                   help="Activation rule when setting role to logic/guard")
    p.add_argument("--match-pattern", "--match_pattern", dest="match_pattern", default="",
                   help="Regex match pattern for sensor_hook")
    p.add_argument("--tool", default=None,
                   help="Tool name regex for sensor_hook (tool_call/tool_result), full match, case-sensitive, e.g. Bash|bash")
    p.add_argument("--adapter", choices=["claude-code", "codex", "antigravity", "opencode", "dsh"], default=None,
                   help="Limit sensor_hook/tool_guard to this host; omitted means all hosts")
    p.add_argument("--args-pattern", "--args_pattern", dest="args_pattern", default=None,
                   help="Args regex pattern for tool_guard")
    p.add_argument("--jev", dest="jev_questions", action="append", default=None,
                   help="sensor_hook: yes/no question asked to Jev after the regex hits. Repeat for several "
                        "questions; the sensor fires if any one is answered 'yes'")
    p.add_argument("--jev-threshold", "--jev_threshold", dest="jev_threshold", type=float, default=None,
                   help="sensor_hook: minimum probability of Jev's 'yes' for every question (default 0.5)")

    # update (content — patch or append)
    p = add_command("update", "Append to a concept's content or replace part of it.",
                    "Append to a concept's content or replace part of it.\n"
                    "  update <node> content --append \"text\"\n"
                    "  update <node> content --old \"old text\" --new \"replacement\"\n"
                    "  update <node> content --old \"start...end\" --new \"replacement\"\n\n"
                    "In --old, \"...\" stands for everything between the two ends.\n"
                    "Use the -file options for long or multi-line text.")
    p.add_argument("node")
    p.add_argument("field", choices=["content"])
    # patch
    p.add_argument("--old", default=None)
    p.add_argument("--old-file", default=None)
    p.add_argument("--new", default=None)
    p.add_argument("--new-file", default=None)
    # append (on empty field = initial write)
    p.add_argument("--append", default=None)
    p.add_argument("--append-file", default=None)

    # read_concept
    p = add_command("read_concept", "Read concepts: content, properties and suggestions.")
    p.add_argument("concept", nargs="+",
                   help="One or more names, aliases or IDs. Several at once count as a single read: "
                        "their order is not recorded as an attention shift.")

    # read_hook — 查看 read_concept 隐藏的 sensor_hook / tool_guard 完整规则与 on_fire 提醒全文
    p = add_command("read_hook", "Show full sensor_hook / tool_guard rules and on_fire text hidden by read_concept.")
    p.add_argument("concept", nargs="+", help="One or more names, aliases or IDs")

    # place — 把一条推荐边放到起点推荐列表的指定名次
    p = add_command("place",
                    "place <suggested> --in <list owner> --at <rank>: put a concept at the given "
                    "rank of another concept's [ YOU MAY ALSO NEED ] list. Prints the updated list.")
    p.add_argument("to_concept", metavar="suggested",
                   help="The concept to move (name, alias, or ID)")
    p.add_argument("--in", dest="from_concept", metavar="LIST_OWNER", required=True,
                   help="The concept whose suggestion list is being edited")
    p.add_argument("--at", dest="rank", metavar="RANK", type=int, required=True,
                   help="1 = top. A rank past the end puts it last. Only the top ranks are shown, so a low "
                        "rank moves it out of view.")

    # cut — 删除推荐边
    p = add_command("cut",
                    "cut <suggested...> --from <list owner>: remove concept(s) from another "
                    "concept's [ YOU MAY ALSO NEED ] list (deletes the edges). Prints the updated list.")
    p.add_argument("to_concepts", metavar="suggested", nargs="+",
                   help="One or more concepts to remove (name, alias, or ID)")
    p.add_argument("--from", dest="from_concept", metavar="LIST_OWNER", required=True,
                   help="The concept whose suggestion list is being edited")
    p.add_argument("--both", action="store_true",
                   help="Also remove the list owner from each removed concept's own list")

    # compile — backward solver diagnostics for target concept
    p = add_command("compile", "Backward solver diagnostics for target concept.",
                    "Walk a circuit node's whole dependency tree and report why it is or is not powered: "
                    "missing leaf conditions, active inhibitors, what a CHAIN is waiting for.\n"
                    "With --assume it simulates those sensors firing and changes no real data.")
    p.add_argument("--target", required=True,
                   help="Sensor, logic or guard to diagnose (not plain)")
    p.add_argument("--assume", nargs="*", default=[],
                   help="Sensors to hypothetically light, in the given order (sensor only; logic/guard potentials are derived)")

    # read_memory — read from nocturne_memory.db
    p = add_command("read_memory",
        "Read content from nocturne memory by URI. "
        "Prints to stdout or writes to --out file, which update --append-file can then merge into a concept.")
    p.add_argument("uri",
        help="Nocturne memory URI (e.g. core://nocturne/bluesky)")
    p.add_argument("--out", default=None,
        help="Write content to file instead of stdout")

    # search_semantic (semantic search by when_to_read embedding)
    p = add_command("search_semantic",
        "Search concepts by semantic intent (vector similarity on when_to_read).")
    p.add_argument("query", help="Natural language intent to search for")
    p.add_argument("--limit", type=int, default=10,
        help="Maximum number of results to return (default: 10)")

    # recent — 最近修改过的节点
    p = add_command("recent", "List the most recently mutated concepts (from the CLI audit log).")
    p.add_argument("n", nargs="?", type=int, default=10,
        help="Number of distinct concepts to show (default: 10)")

    # suggest_name — 起名与检查小助手
    p = add_command("suggest_name",
        "Ask gpt-6-luna to check a concept's name or suggest better names based on its type (plain/circuit/tag) and content. Read-only.")
    p.add_argument("concept", help="Concept name, alias or ID")

    # login — 会话开场：boot 节点 + recent（说明书只给路径）
    p = add_command("login",
        "Session start: print all concepts tagged 'boot' and recent mutations. The manual is NOT printed; only its path."
    )
    p.add_argument("--recent", type=int, default=10, dest="recent_n",
        help="How many recent concepts to show (default: 10, 0 to hide)")
    # 远程模式下本地 CLI 发给服务器时加上：说明书的位置和全文由本地补，服务器上的说明书设置不该出现在本地输出里
    p.add_argument("--without-manual", action="store_true", help=argparse.SUPPRESS)

    # batch
    p = add_command("batch",
        "Run multiple commands. Reads from stdin or --file. "
        "Only prints last result unless --all is set.",
        "Run multiple commands, one per line, without the `python frontend/cli.py` prefix. Quoted text may span "
        "lines. Reads from stdin or --file. Only prints last result unless --all is set.")
    p.add_argument("--file", default=None,
        help="Read commands from file (default: stdin)")
    p.add_argument("--all", action="store_true",
        help="Print all results, not just the last")

    return parser


def _dispatch(args, db):
    """Execute a single command, return result object."""
    if args.command == "reset":
        db.session_reset()
        return RawOutput(f"Success. Session '{SESSION_ID}' reset (ephemeral sensors & chains cleared).")

    elif args.command == "create_concept":
        content = args.content
        if getattr(args, "content_file", None) is not None:
            if content is not None:
                raise ValueError("Cannot use both --content and --content-file")
            content = _read_file(args.content_file)

        if content is None or not content.strip():
            raise ValueError("create_concept requires non-empty --content or --content-file.")
        return db.create_concept(
            args.name,
            args.when_to_read,
            content=content,
            role=getattr(args, "role", "plain"),
            lifespan=getattr(args, "lifespan", None),
            activation_rule=getattr(args, "activation_rule", None),
            on_fire=getattr(args, "on_fire", None),
        )

    elif args.command == "create_tag":
        return db.create_tag(args.name)

    elif args.command == "delete_tag":
        return db.delete_tag(args.name)

    elif args.command == "list_tags":
        rows = db.list_tags()
        if not rows:
            return RawOutput("(no tags registered)")
        lines = []
        for r in rows:
            src = "(system)" if r["source_concept_id"] is None \
                else f"(source: id={r['source_concept_id']})"
            lines.append(
                f"  {r['name']}  — {r['usage_count']} concept(s)  {src}")
            desc = r.get("plugin_description")
            if desc:
                for i, dl in enumerate(desc.splitlines()):
                    prefix = "        ↳ [插件] " if i == 0 else "                "
                    lines.append(prefix + dl)
        return RawOutput("\n".join(lines))

    elif args.command == "suggest_name":
        from backend.name_suggest import suggest_names
        return RawOutput(suggest_names(db, args.concept))

    elif args.command == "audit":
        if args.concept:
            if args.tag:
                raise ValueError("--concept cannot be combined with a tag name. Run 'audit --concept <concept> ...' on its own.")
            return RawOutput(format_report(audit_concepts(db, args.concept)))
        parts = [db.audit_clusters_report(args.tag),
                 refresh_audit_coverage(db, tag_expr=args.tag),
                 oversize_warning(db, tag_expr=args.tag)]
        return RawOutput("\n\n".join(filter(None, parts)))

    elif args.command == "search_keyword":
        results = db.search_concepts(args.query, tag_expr=args.tag, limit=args.limit)
        return RawOutput(_format_search_concepts(results))

    elif args.command == "list_concepts":
        overviews = db.get_all_concepts_overview(tag_expr=args.tag)
        return RawOutput(_format_list_concepts(overviews))

    elif args.command == "add":
        return db.add(
            args.target,
            args.kind,
            args.value,
        )

    elif args.command == "delete":
        return db.delete(args.target, args.kind,
                         getattr(args, "value", None))

    elif args.command == "set":
        return db.set(
            args.target,
            args.prop,
            args.value,
            lifespan=getattr(args, "lifespan", None),
            activation_rule=getattr(args, "activation_rule", None),
            match_pattern=getattr(args, "match_pattern", ""),
            tool=getattr(args, "tool", None),
            args_pattern=getattr(args, "args_pattern", None),
            adapter=getattr(args, "adapter", None),
            jev_questions=getattr(args, "jev_questions", None),
            jev_threshold=getattr(args, "jev_threshold", None),
        )

    elif args.command == "update":
        # 读旧值 → 算新值 → 写回。写回带 expected_old，读写之间被别的连接改过就整个重来，
        # 免得并发的 --append / patch 互相覆盖（SQLite 自己的锁只保证单条语句，管不到这段空档）。
        for attempt in range(5):
            _, _, current_value = db.get_concept_field(args.node, args.field)
            notices = []
            resolved = _resolve_text(
                old=args.old,
                old_file=args.old_file,
                new=args.new,
                new_file=args.new_file,
                append=args.append,
                append_file=args.append_file,
                field_name=args.field,
                current_value=current_value,
                notices=notices,
            )
            if resolved is None:
                raise ValueError(
                    "Provide --old/--new (patch) or --append.")
            try:
                result = db.update(args.node, args.field, resolved, expected_old=current_value)
                break
            except ConcurrentUpdateError:
                if attempt == 4:
                    raise
        if notices:
            result.message += "\n" + "\n".join(notices)
        return result

    elif args.command == "read_concept":
        # 同一节点传了多次（含名字与别名混用）只保留一份
        results = list({r.id: r for r in (db.read_concept(c) for c in args.concept)}.values())
        try:
            db.record_transition([r.id for r in results])
        except Exception:
            logging.getLogger(__name__).debug(
                "record_transition failed", exc_info=True)
        return results[0] if len(results) == 1 else BatchReadOutput(results)

    elif args.command == "read_hook":
        # 输入：节点名/别名/ID 列表。输出：每个节点一段，列出 sensor_hook 与 tool_guard 完整规则
        # （写法与 set 命令参数一致；Jev 题一题一行，行尾用 \ 续行，和 Hook 行合起来才是完整的一条参数），
        # 以及 on_fire 每个动作的全文（按真换行展开）。
        # 不记注意力转移，不算读节点。
        blocks = []
        for c in args.concept:
            r = db.read_concept(c)
            head = f"[{r.id}] {r.name} ({r.role.upper()})"
            hook_lines = []
            for tg in r.tool_guards:
                args_flag = f" --args-pattern {shlex.quote(tg.args_pattern)}" if tg.args_pattern else ""
                adapter_flag = f" --adapter {tg.adapter}" if tg.adapter else ""
                hook_lines.append(f"  Tool Guard: {tg.tool}{args_flag}{adapter_flag}")
            for sh in r.sensor_hooks:
                tool_flag = f" --tool {shlex.quote(sh.tool)}" if sh.tool else ""
                adapter_flag = f" --adapter {sh.adapter}" if sh.adapter else ""
                thr_flag = f" --jev-threshold {sh.jev_threshold}" if sh.jev_questions else ""
                # 一题一行，前一行末尾加 \ 续行，免得只抄 Hook 那一行把题丢掉；任一题答“是”就点火
                parts = [f"  Hook: {sh.event_type}{tool_flag} --match-pattern "
                         f"{shlex.quote(sh.match_pattern)}{adapter_flag}{thr_flag}"]
                parts += [f"    --jev {shlex.quote(q)}" for q in sh.jev_questions]
                hook_lines.append(" \\\n".join(parts))
            if r.on_fire:
                # 提醒全文按真换行展开（库里存的是 JSON，\n 转义读不了）；改它用 set <节点> on_fire '<JSON>'。
                for kind, text in _on_fire_actions(r.on_fire):
                    body = "\n".join("    " + ln for ln in text.splitlines()) or "    "
                    hook_lines.append(f"  On-Fire {kind}:\n{body}")
            if not hook_lines:
                blocks.append(f"{head}\n  (no hook, tool guard or on_fire)")
                continue
            blocks.append(head + "\n" + "\n".join(hook_lines))
        return RawOutput("\n\n".join(blocks))

    elif args.command == "place":
        return db.place_transition(args.from_concept, args.to_concept, args.rank)

    elif args.command == "cut":
        return db.cut_transition(args.from_concept, args.to_concepts, both=args.both)

    elif args.command == "read_memory":
        content = _read_nocturne_memory(args.uri)
        if args.out:
            Path(args.out).write_text(content, encoding="utf-8")
            return f"Success. Wrote {len(content)} chars to {args.out}"
        return RawOutput(content)

    elif args.command == "compile":
        return db.compile(
            target=args.target,
            assume=args.assume,
        )

    elif args.command == "recent":
        return RawOutput(_format_recent(db.recent_mutations(args.n)))

    elif args.command == "login":
        return RawOutput(_format_login(db, args.recent_n, with_manual=not args.without_manual))

    elif args.command == "search_semantic":
        results = db.search_by_intent(args.query, limit=args.limit)
        if not results:
            return RawOutput("(no results — when_to_read may lack embeddings)")
        lines = []
        for i, r in enumerate(results, 1):
            lines.append(
                f"{i}. [{r['concept_id']}] {r['concept_name']} "
                f"(sim={r['similarity']:.4f})")
            lines.append(f"   when_to_read: {r['when_to_read_text']}")
        return RawOutput("\n".join(lines))


def _audited_dispatch(args, db):
    """Wrap _dispatch with audit logging.

    Audit info comes from two sources:
      - sub_action: CLI routing (which sub-command was used)
      - concept_id/name: DB return value (MutationResult, ReadResult, or CompileResult)
    """
    cmd = args.command
    sub_action = None
    if cmd == "add":
        sub_action = args.kind
    elif cmd == "delete":
        sub_action = getattr(args, "kind", None)
    elif cmd == "set":
        sub_action = args.prop
    elif cmd == "update":
        sub_action = args.field
    elif cmd == "place":
        sub_action = f"{args.to_concept}:#{args.rank}"
    elif cmd == "cut":
        sub_action = ",".join(str(t) for t in args.to_concepts)
        if args.both:
            sub_action += ":both"

    try:
        result = _dispatch(args, db)
    except Exception:
        db.log_action(command=cmd, sub_action=sub_action, success=False)
        raise

    if isinstance(result, BatchReadOutput):
        # 同一批的行共用一个 sub_action，record_transition 据此把这批算作一次读取
        batch_tag = f"batch:{uuid.uuid4().hex}"
        for r in result.results:
            db.log_action(command=cmd, concept_id=r.id, concept_name=r.name,
                          sub_action=batch_tag, success=True)
        return result

    concept_id = None
    concept_name = None

    if isinstance(result, MutationResult):
        concept_id = result.concept_id
        concept_name = result.concept_name
    elif isinstance(result, ReadResult):
        concept_id = result.id
        concept_name = result.name
    elif isinstance(result, CompileResult):
        concept_name = result.target
    elif cmd == "compile" and getattr(args, "target", None):
        concept_name = args.target

    db.log_action(
        command=cmd,
        concept_id=concept_id,
        concept_name=concept_name,
        sub_action=sub_action,
        success=True,
    )
    return result


class _SmartBatchEscape:
    """Smart escape handler for Horon batch commands.

    - Inside double quotes: treats '\\' as an escape character for quotes and backslashes
      (e.g., \\" -> " and \\\\ -> \\), preventing escaped quotes from prematurely closing strings.
    - In unquoted words: preserves backslashes literally (so Windows paths like C:\\Users\\...
      and regex sequences remain intact without losing backslashes).
    """

    def __init__(self, lexer: shlex.shlex):
        self.lexer = lexer

    def __contains__(self, char: object) -> bool:
        if char == "\\":
            return self.lexer.state == "\\" or self.lexer.state in self.lexer.escapedquotes
        return False


def _create_batch_lexer(text: str) -> shlex.shlex:
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.escapedquotes = '"'
    lexer.escape = _SmartBatchEscape(lexer)
    return lexer


def _parse_batch_commands(text: str) -> list[tuple[int, list[str], str]]:
    """Parse batch script text into list of (start_line, tokens, raw_command).
    Supports multi-line commands with quoted strings.
    """
    lines = text.splitlines(keepends=True)
    commands = []
    buffer = ""
    start_line = 1

    for line_idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if not buffer and (not stripped or stripped.startswith("#")):
            continue

        if not buffer:
            start_line = line_idx

        buffer += line

        try:
            lexer = _create_batch_lexer(buffer)
            tokens = list(lexer)
            if tokens:
                commands.append((start_line, tokens, buffer.strip()))
            buffer = ""
        except ValueError as e:
            if "No closing quotation" in str(e) or "No escaped character" in str(e):
                continue
            raise ValueError(f"Line {start_line}: syntax error in batch script: {e}") from e

    if buffer.strip():
        first_line = buffer.strip().splitlines()[0]
        raise ValueError(f"Line {start_line}: unclosed quotation mark in command: {first_line}")

    return commands


def _run_batch(parser, db, raw_text: str, print_all: bool, check=None) -> tuple[str, str | None]:
    """执行一段 batch 命令文本（每行一条命令，省略 `python frontend/cli.py` 前缀）。

    输入：parser 是 _build_parser() 的结果；raw_text 是命令文本；print_all 对应 --all；
    check 可选，签名 check(已解析的参数)，在每条命令执行前调用，抛异常就当这一行失败
    （远程接口用它再过一遍白名单）。
    行为：逐行独立提交，某行出错就停，前面成功的不回滚（与原来一致），失败信息里写明这一点。
    输出：(out, err)。out 是成功命令的结果文本（默认只含最后一条，print_all 时含全部）；
    err 是失败信息，成功时为 None。本地 CLI 把 out 和 err 依次写到 stdout，err 非空则退出码 1。
    """
    try:
        commands = _parse_batch_commands(raw_text)
    except Exception as e:
        return "", f"Fail. Syntax error in batch script: {e}\n"

    out: list[str] = []
    last_result = None
    for index, (start_line, tokens, raw_cmd) in enumerate(commands):
        committed = (f"Lines before line {start_line} already ran and their changes stay. "
                     "Rerunning them appends text twice, or fails on names that now exist.\n") if index else ""
        try:
            cmd_args = parser.parse_args(tokens)
            if cmd_args.command == "batch":
                # 这里的 sys.exit 会被下面的 except SystemExit 接住，所以嵌套 batch 会连输出两行：
                # 先这一句，再 “Fail. ... invalid command”。保持原有输出。
                out.append(f"Line {start_line}: batch inside batch is not allowed\n")
                sys.exit(1)
            if check is not None:
                check(cmd_args)
            result = _audited_dispatch(cmd_args, db)
            if print_all:
                out.append(_render(result))
            last_result = result
        except SystemExit:
            first_line = raw_cmd.splitlines()[0] if raw_cmd else ""
            return "".join(out), f"Fail. Line {start_line}: invalid command: {first_line}\n{committed}"
        except Exception as e:
            return "".join(out), f"Fail. Line {start_line}: {e}\n{committed}"

    if not print_all and last_result is not None:
        out.append(_render(last_result))
    return "".join(out), None


# 远程模式下留在本机跑的命令：reset 清的是本地会话状态，compile 要看本地会话的传感器。其余都发给服务器。
_LOCAL_COMMANDS_IN_REMOTE_MODE = {"reset", "compile"}

# 带文件的参数在远程模式下先读好，换成同义的文字参数：服务器不读文件。值是 (argparse 的 dest, 文字参数)。
_FILE_FLAGS = {
    "--content-file": ("content_file", "--content"),
    "--content_file": ("content_file", "--content"),
    "--append-file": ("append_file", "--append"),
    "--old-file": ("old_file", "--old"),
    "--new-file": ("new_file", "--new"),
}


def _inline_file_args(tokens: list[str], ns) -> list[str]:
    """把 tokens 里的文件参数换成文字参数。ns 是 tokens 的解析结果：
    只有解析结果里确实是这个文件参数时才换，`--append "--old-file"` 这种字面文字不会被误伤。"""
    out = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        flag, has_value, value = token.partition("=")
        if flag not in _FILE_FLAGS:
            out.append(token)
            continue
        if not has_value and i < len(tokens):
            value = tokens[i]
        dest, text_flag = _FILE_FLAGS[flag]
        if getattr(ns, dest, None) != value:
            out.append(token)
            continue
        if not has_value:
            i += 1
        out.append(f"{text_flag}={_read_file(value)}")
    return out


def _inline_batch_file_args(parser, text: str) -> str:
    """batch 文本里有文件参数时重新拼一遍文本，没有就原样返回。

    这一步在发请求之前逐行解析、读文件。任何一行出错（引号没闭合、参数不对、文件读不到），
    原来的报错照常抛出，另在 stderr 补一句整段都没执行：本地模式的 batch 出错时前面的行已经生效，
    读者分不出这里不一样。
    """
    try:
        commands = [tokens for _, tokens, _ in _parse_batch_commands(text)]
        rewritten = [_inline_file_args(tokens, parser.parse_args(tokens)) for tokens in commands]
    except (SystemExit, ValueError, OSError):
        print("Nothing in this batch ran: it is checked on this machine before being sent to the server, "
              "and this error stopped it.", file=sys.stderr)
        raise
    if rewritten == commands:
        return text
    return "\n".join(shlex.join(tokens) for tokens in rewritten) + "\n"


def _run_remote(parser, args, remote: remote_client.RemoteSettings) -> int:
    """把这条命令发给服务器执行，打印结果；服务器的规则变了就同步进本地规则库。返回退出码。"""
    batch_text = None
    if args.command == "batch":
        text = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
        batch_text = _inline_batch_file_args(parser, text)
        tokens = ["batch", "--all"] if args.all else ["batch"]
    else:
        tokens = _inline_file_args(sys.argv[1:], args)
    # login 的说明书部分按本机的设置补（和本地模式一样），不用服务器上那份
    local_manual = args.command == "login" and not args.without_manual
    if local_manual:
        tokens.append("--without-manual")
    try:
        response = remote_client.run_command(remote, tokens, batch_text, SESSION_ID)
    except remote_client.RemoteError as e:
        print(f"Fail. {e}")
        return 1
    output = response.output
    if local_manual and response.ok:
        pointer, manual = _login_manual_parts()
        output = "\n\n".join(part for part in (pointer, output.rstrip("\n"), manual) if part)
    sys.stdout.write(output)
    if output and not output.endswith("\n"):
        sys.stdout.write("\n")
    if not response.ok:
        print(response.error if response.error.startswith("Fail.") else f"Fail. {response.error}")
    # 规则同步成功时不显示。失败要说：钩子和门禁这时用的还是旧规则。
    if response.rules_version and response.rules_version != remote_client.local_version(remote):
        try:
            remote_client.refresh_rules(remote)
        except (remote_client.RemoteError, OSError, sqlite3.Error) as e:
            print(f"[Rule sync failed; hooks and guards are still using the old rules] {e}")
    return 0 if response.ok else 1


def main():
    parser = _build_parser()
    args = parser.parse_args()
    root = Path(__file__).parent.parent
    load_dotenv(root / ".env")
    remote = remote_client.remote_settings()
    if remote:
        if args.command not in _LOCAL_COMMANDS_IN_REMOTE_MODE:
            sys.exit(_run_remote(parser, args, remote))
        db_path = remote_client.ensure_rules_db(remote)
    else:
        db_path = root / os.environ["HORON_DB"]
        if not database_exists(db_path):  # 第一次用 Horon：在 HORON_DB 指的位置建一个空库
            create_database(db_path)
    db = HoronDB(db_path=db_path, snapshot_mode=True, session_id=SESSION_ID)
    if SESSION_ID != OFFLINE_DEV_SESSION_ID:
        # 宿主没装钩子时这个会话还没登记；首次登记会顺带算一遍它的电路状态
        sync_session(db, ADAPTER)

    try:
        if args.command == "batch":
            if args.file:
                raw_text = Path(args.file).read_text(encoding="utf-8")
            else:
                raw_text = sys.stdin.read()

            out, err = _run_batch(parser, db, raw_text, args.all)
            sys.stdout.write(out)
            if err is not None:
                sys.stdout.write(err)
                sys.exit(1)

        else:
            result = _audited_dispatch(args, db)
            _print(result)

    except Exception as e:
        print(f"Fail. {e}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
