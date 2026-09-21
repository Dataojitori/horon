"""
Horon CLI — operational interface for intelligent agents
Usage:
    python frontend/cli.py <command> [options]
    python frontend/cli.py batch [--file path] [--all]
"""

import argparse
import json
import logging
import os
import shlex
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from backend.db import HoronDB
from backend.models import CompileResult, MutationResult, ReadResult
from backend.text_patch import (
    normalize_literal_newlines,
    try_normalized_patch,
)

load_dotenv(Path(__file__).parent.parent / ".env")


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
        elif result.missing_prerequisites:
            missing_str = ", ".join(f"'{name}'" for name in result.missing_prerequisites)
            lines.append(
                f"[✗ 缺少前置] 概念 '{target}' 未就绪，缺少输入: {missing_str}"
            )
        else:
            lines.append(f"[✗ 未就绪] 概念 '{target}' 前置条件未满足。")
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


def _format_read_concept(result: ReadResult) -> str:
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

    if result.on_fire:
        lines.append(f"On-Fire: {result.on_fire}")

    alt_names = [a for a in result.aliases if a != result.name]
    if alt_names:
        lines.append(f"Also known as: {', '.join(alt_names)}")

    if result.disclosure:
        lines.append(f"Disclosure: {result.disclosure}")
    else:
        lines.append("Disclosure: (none)")

    if result.tags:
        lines.append(f"Tags: {', '.join(result.tags)}")
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
                for tg in result.tool_guards:
                    args_flag = f" --args-pattern '{tg.args_pattern}'" if tg.args_pattern else ""
                    lines.append(f"Tool Guard: {tg.tool}{args_flag}")
            else:
                lines.append("Tool Guard: (none)")

    elif result.role == "sensor":
        lines.append(f"Sensor: {result.lifespan or 'unknown'}")
        if result.sensor_hooks:
            for sh in result.sensor_hooks:
                tool_flag = f" --tool '{sh.tool}'" if sh.tool else ""
                lines.append(f"Hook: {sh.event_type}{tool_flag} --match-pattern '{sh.match_pattern}'")
        else:
            lines.append("Hook: (none)")

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
    lines.append(f"Content:\n{result.content}" if result.content else "Content: (empty)")

    # ── 页脚（仅包含运行时附着物：Reminders 与 联想推荐） ──
    footer_blocks: list[list[str]] = []

    if result.reminders:
        block = ["[ Reminders ]"]
        for rem in result.reminders:
            fired = rem.last_fired_at or "(never)"
            block.append(f"  #{rem.id}: {rem.message}")
            block.append(f"    when: {rem.condition} | last fired: {fired}")
        footer_blocks.append(block)

    if result.suggested_next:
        block = ["[ YOU MAY ALSO NEED ]"]
        for s in result.suggested_next:
            block.append(f"  - [{s.concept_id}] {s.concept_name}")
            if s.disclosure:
                block.append(f"    ↳ When: {s.disclosure}")
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
            elif m.field == "disclosure":
                lines.append(f'     ↳ Disclosure: "{m.snippet}"')
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


class RawOutput:
    def __init__(self, content: str):
        self.content = content


def _print(obj):
    if isinstance(obj, MutationResult):
        print(obj.message)
        if obj.fired_actions:
            print("\n[ FIRED ACTIONS ]")
            for fa in obj.fired_actions:
                act_str = json.dumps(fa.action, ensure_ascii=False) if isinstance(fa.action, (dict, list)) else str(fa.action)
                print(f"  * {fa.concept} (id={fa.concept_id}): {act_str}")
    elif isinstance(obj, ReadResult):
        print(_format_read_concept(obj))
    elif isinstance(obj, CompileResult):
        print(_format_compile(obj))
    elif isinstance(obj, RawOutput):
        sys.stdout.write(obj.content)
        if not obj.content.endswith("\n"):
            sys.stdout.write("\n")
    elif isinstance(obj, str):
        print(obj)
    else:
        print(str(obj))


def _read_file(path):
    """Read file content, strip trailing whitespace."""
    return Path(path).read_text(encoding="utf-8").rstrip()


def _resolve_text(old, old_file, new, new_file,
                  append, append_file, field_name, current_value):
    """
    Resolve a text field from one of three modes:
      1. Patch mode:   --old "x" --new "y"  (with -file variants)
      2. Append mode:  --append "text"  or  --append-file path
                       On an empty field this IS the initial write.
      3. None:         field not touched

    There is no full-replace mode. To rewrite entirely, patch with the
    full current text as old — which forces you to have actually read it.

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

        # 1. Exact match
        count = current_value.count(old)
        if count == 1:
            return current_value.replace(old, new, 1)
        if count > 1:
            raise ValueError(
                f"--old matched {count} times in {field_name}. "
                f"Provide more context to make it unique")

        # 2. Literal \n normalization (AI sends "\\n" instead of real newlines)
        if "\\n" in old:
            norm_old = normalize_literal_newlines(old)
            if norm_old != old:
                norm_count = current_value.count(norm_old)
                if norm_count == 1:
                    norm_new = normalize_literal_newlines(new) if "\\n" in new else new
                    return current_value.replace(norm_old, norm_new, 1)
                if norm_count > 1:
                    raise ValueError(
                        f"--old matched {norm_count} times in {field_name} "
                        f"(after newline normalization). "
                        f"Provide more context to make it unique")

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
            "WHERE domain = ? AND path = ? AND namespace = ''",
            (domain, path),
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


class HoronArgumentParser(argparse.ArgumentParser):
    """Custom ArgumentParser providing friendly diagnostic hints on shell argument splitting."""

    def error(self, message: str):
        if "unrecognized arguments" in message:
            sys.stderr.write(
                f"{self.format_usage()}\n"
                f"{self.prog}: error: {message}\n\n"
                f"[提示] 检测到命令行参数疑似被 Shell 截断（例如 PowerShell 将中文引号“”‘’当作代码定界符而拆分参数）。\n"
                f"       若内容包含复杂引号、特殊符号或多行长文本，请避免在命令行内联传参，改用文件传递：\n"
                f"       - create_concept: 使用 --content-file <文件>\n"
                f"       - update: 使用 --old-file <文件> / --new-file <文件> / --append-file <文件>\n"
                f"       - batch:  使用 batch --file <命令文件>\n"
            )
            self.exit(2)
        super().error(message)


def _build_parser():
    parser = HoronArgumentParser(prog="horon", description="Horon CLI",
                                 allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)

    # reset
    sub.add_parser("reset", allow_abbrev=False,
                   help="Reset ephemeral sensors (session/turn) and chains for the current session.")

    # create_concept
    p = sub.add_parser("create_concept", allow_abbrev=False)
    p.add_argument("name")
    p.add_argument("--disclosure", default=None)
    p.add_argument("--content", default=None,
                   help="Why this concept exists and what observation prompted it.")
    p.add_argument("--content-file", "--content_file", dest="content_file", default=None,
                   help="Read concept content from file.")
    p.add_argument("--role", default="plain", choices=["plain", "sensor", "logic", "guard"])
    p.add_argument("--lifespan", default=None, choices=["turn", "session", "permanent"])
    p.add_argument("--activation-rule", "--activation_rule", dest="activation_rule", default=None,
                   help="Activation rule for logic/guard nodes (e.g. 'A & B', 'A → B', 'A | B')")
    p.add_argument("--on-fire", "--on_fire", dest="on_fire", default=None,
                   help="On-fire action JSON config")

    # create_tag
    p = sub.add_parser("create_tag", allow_abbrev=False)
    p.add_argument("name")

    # delete_tag
    p = sub.add_parser("delete_tag", allow_abbrev=False)
    p.add_argument("name")

    # list_tags
    sub.add_parser("list_tags", allow_abbrev=False)

    # audit (sweep tag cluster lints or system database integrity)
    p = sub.add_parser("audit", allow_abbrev=False,
                       help="Run diagnostic audits on tag clusters or database system integrity.")
    p.add_argument("tag", nargs="?", default=None, help="Tag name for tag cluster audit")
    p.add_argument("--all", action="store_true", help="Run all audits (all tag clusters + database integrity)")
    p.add_argument("--db", action="store_true", help="Run database system integrity audit (e.g. missing embeddings)")

    # search_concepts
    p = sub.add_parser("search_concepts", allow_abbrev=False)
    p.add_argument("query", help="Keyword query to search in concepts, aliases, disclosures, and content")
    p.add_argument("--tag", default=None,
                   help='Tag filter expression: "A & B" (AND), "A | B" (OR)')
    p.add_argument("--limit", type=int, default=50,
                   help="Maximum number of concepts to return (default: 50)")


    # list_concepts
    p = sub.add_parser("list_concepts", allow_abbrev=False)
    p.add_argument("--tag", default=None,
                   help='Tag filter expression: "A & B" (AND), "A | B" (OR)')

    # add (name / tag / inhibition)
    p = sub.add_parser("add", allow_abbrev=False)
    p.add_argument("target")
    p.add_argument("kind", choices=["name", "tag", "inhibition"])
    p.add_argument("value")

    # delete (default=concept, or name/activation-rule/tag/disclosure/sensor_hook/tool_guard/inhibition)
    p = sub.add_parser("delete", allow_abbrev=False)
    p.add_argument("target")
    p.add_argument("kind", nargs="?", default=None,
                   choices=["name", "activation-rule", "activation_rule", "tag", "disclosure", "sensor_hook", "sensor-hook", "tool_guard", "tool-guard", "inhibition"])
    p.add_argument("value", nargs="?", default=None)

    # set (name, disclosure, activation-rule, role, lifespan, active, on_fire, sensor_hook, tool_guard)
    p = sub.add_parser("set", allow_abbrev=False)
    p.add_argument("target")
    p.add_argument("prop", choices=["name", "disclosure", "activation-rule", "activation_rule", "role", "lifespan", "active", "on_fire", "on-fire", "sensor_hook", "sensor-hook", "tool_guard", "tool-guard"])
    p.add_argument("value")
    p.add_argument("--lifespan", default=None, choices=["turn", "session", "permanent"],
                   help="Lifespan when setting role to sensor")
    p.add_argument("--activation-rule", "--activation_rule", dest="activation_rule", default=None,
                   help="Activation rule when setting role to logic/guard")
    p.add_argument("--on-fire", "--on_fire", dest="on_fire", default=None,
                   help="On-fire action config")
    p.add_argument("--match-pattern", "--match_pattern", dest="match_pattern", default="",
                   help="Regex or substring match pattern for sensor_hook")
    p.add_argument("--tool", default=None,
                   help="Tool name for sensor_hook (tool_call/tool_result)")
    p.add_argument("--args-pattern", "--args_pattern", dest="args_pattern", default=None,
                   help="Args regex pattern for tool_guard")

    # update (content — patch or append)
    p = sub.add_parser("update", allow_abbrev=False)
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
    p = sub.add_parser("read_concept", allow_abbrev=False)
    p.add_argument("concept")

    # compile — backward solver diagnostics for target concept
    p = sub.add_parser("compile", allow_abbrev=False,
                       help="Backward solver diagnostics for target concept.")
    p.add_argument("--target", required=True,
                   help="Target concept to diagnose (why it is active / inactive)")
    p.add_argument("--assume", nargs="*", default=[],
                   help="Additional hypothetical active concepts")

    # read_memory — read from nocturne_memory.db
    p = sub.add_parser("read_memory", allow_abbrev=False,
        help="Read content from nocturne memory by URI. "
             "Prints to stdout or writes to --out file.")
    p.add_argument("uri",
        help="Nocturne memory URI (e.g. core://nocturne/bluesky)")
    p.add_argument("--out", default=None,
        help="Write content to file instead of stdout")

    # remind (create / list / delete)
    p = sub.add_parser("remind", allow_abbrev=False,
        help="Manage reminder rules on concepts.")
    p.add_argument("concept", nargs="?", default=None,
        help="Concept to attach reminder to (for create mode)")
    p.add_argument("--when", default=None,
        help="Sandbox Python expression as trigger condition")
    p.add_argument("--msg", default=None,
        help="Message shown when condition fires")
    p.add_argument("--list", action="store_true",
        help="List all reminder rules")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--del", type=int, default=None, dest="del_id",
        help="Delete a reminder by ID")

    # inbox
    sub.add_parser("inbox", allow_abbrev=False,
        help="Evaluate all reminder conditions and show triggered ones.")

    # intent (semantic search by disclosure embedding)
    p = sub.add_parser("intent", allow_abbrev=False,
        help="Search concepts by semantic intent (vector similarity on disclosures).")
    p.add_argument("query", help="Natural language intent to search for")
    p.add_argument("--limit", type=int, default=10,
        help="Maximum number of results to return (default: 10)")

    # batch
    p = sub.add_parser("batch", allow_abbrev=False,
        help="Run multiple commands. Reads from stdin or --file. "
             "Only prints last result unless --all is set.")
    p.add_argument("--file", default=None,
        help="Read commands from file (default: stdin)")
    p.add_argument("--all", action="store_true",
        help="Print all results, not just the last")

    return parser


def _dispatch(args, db):
    """Execute a single command, return result object."""
    if args.command == "reset":
        sess = db.get_current_session()
        if not sess:
            sess = db.init_session("devonly")
        else:
            db.session_reset()
        return RawOutput(f"Success. Session '{sess}' reset (ephemeral sensors & chains cleared).")

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
            args.disclosure,
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

    elif args.command == "audit":
        if args.tag and (args.all or args.db):
            raise ValueError(
                "Cannot combine a specific tag name with --all or --db. "
                "Use 'audit <tag>' for a tag cluster audit, 'audit --db' for database integrity, "
                "or 'audit --all' to run all checks."
            )
        if args.all:
            tag_report = db.audit_clusters_report()
            db_report = db.audit_db_integrity()
            return RawOutput(f"{tag_report}\n\n{db_report}")
        elif args.db:
            return RawOutput(db.audit_db_integrity())
        elif args.tag:
            return RawOutput(db.audit_clusters_report(args.tag))
        else:
            raise ValueError(
                "Specify an audit target: a tag name (e.g., 'audit plan'), "
                "'--db' (database integrity), or '--all' (all audits)."
            )

    elif args.command == "search_concepts":
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
            on_fire=getattr(args, "on_fire", None),
            match_pattern=getattr(args, "match_pattern", ""),
            tool=getattr(args, "tool", None),
            args_pattern=getattr(args, "args_pattern", None),
        )

    elif args.command == "update":
        # 使用 DB 提供的 helper 获取当前值，避免在 CLI 层重复查询
        _, _, current_value = db.get_concept_field(args.node, args.field)
        resolved = _resolve_text(
            old=args.old,
            old_file=args.old_file,
            new=args.new,
            new_file=args.new_file,
            append=args.append,
            append_file=args.append_file,
            field_name=args.field,
            current_value=current_value,
        )
        if resolved is None:
            raise ValueError(
                "Provide --old/--new (patch) or --append.")
        return db.update(args.node, args.field, resolved)

    elif args.command == "read_concept":
        result = db.read_concept(args.concept)
        try:
            db.record_transition(result.id)
        except Exception:
            logging.getLogger(__name__).debug(
                "record_transition failed", exc_info=True)
        return result

    elif args.command == "read_memory":
        content = _read_nocturne_memory(args.uri)
        if args.out:
            Path(args.out).write_text(content, encoding="utf-8")
            return f"Success. Wrote {len(content)} chars to {args.out}"
        return RawOutput(content)

    elif args.command == "remind":
        if args.list:
            reminders = db.list_reminders(args.limit, args.offset)
            if not reminders:
                return RawOutput("(no reminders)")
            lines = []
            for r in reminders:
                fired = r.get("last_fired_at") or "(never)"
                lines.append(
                    f"#{r['id']} [{r['concept_name']} "
                    f"(id={r['concept_id']})] {r['message']}")
                lines.append(f"   when: {r['condition']}")
                lines.append(
                    f"   created: {r['created_at']} "
                    f"| last fired: {fired}")
            total = db.conn.execute(
                "SELECT COUNT(*) AS cnt FROM reminders"
            ).fetchone()["cnt"]
            shown = len(reminders)
            if total > args.offset + shown:
                lines.append(
                    f"\n(showing {args.offset+1}-{args.offset+shown} "
                    f"of {total} — use --offset {args.offset+shown} "
                    f"to see more)")
            return RawOutput("\n".join(lines))

        elif args.del_id is not None:
            return db.delete_reminder(args.del_id)

        elif args.concept is not None and args.when is not None and args.msg is not None:
            return db.add_reminder(args.concept, args.when, args.msg)

        else:
            raise ValueError(
                "Usage:\n"
                '  remind <concept> --when <condition> --msg <message>\n'
                "  remind --list [--limit N] [--offset M]\n"
                "  remind --del <id>")

    elif args.command == "inbox":
        result = db.evaluate_inbox()
        triggered = result["triggered"]
        errors = result["errors"]
        quiet = result["quiet_count"]

        if not triggered and not errors:
            return RawOutput(
                f"(empty inbox — {quiet} reminder(s) quiet)")

        lines = []
        if triggered:
            for r in triggered:
                lines.append(
                    f"#{r['id']} [{r['concept_name']} "
                    f"(id={r['concept_id']})] {r['message']}")
                lines.append(f"   condition: {r['condition']}")
        if errors:
            lines.append("")
            lines.append("[Errors]")
            for r in errors:
                lines.append(
                    f"#{r['id']} [{r['concept_name']} "
                    f"(id={r['concept_id']})] {r['message']}")
                lines.append(f"   condition: {r['condition']}")
                lines.append(f"   error: {r['error']}")
        if quiet > 0:
            lines.append(f"\n--- {quiet} reminder(s) quiet ---")
        return RawOutput("\n".join(lines))

    elif args.command == "compile":
        try:
            return db.compile(
                target=args.target,
                assume=args.assume,
            )
        except NotImplementedError:
            return RawOutput(
                f"[Step 3 Pending] Backward solver for 'compile --target' will be implemented in Step 3.\n"
                f"Target: '{args.target}'\n"
                f"Assumed: {', '.join(args.assume) if args.assume else '(none)'}"
            )

    elif args.command == "intent":
        results = db.search_by_intent(args.query, limit=args.limit)
        if not results:
            return RawOutput("(no results — disclosures may lack embeddings)")
        lines = []
        for i, r in enumerate(results, 1):
            lines.append(
                f"{i}. [{r['concept_id']}] {r['concept_name']} "
                f"(sim={r['similarity']:.4f})")
            lines.append(f"   disclosure: {r['disclosure_text']}")
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

    try:
        result = _dispatch(args, db)
    except Exception:
        db.log_action(command=cmd, sub_action=sub_action, success=False)
        raise

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


def main():
    parser = _build_parser()
    args = parser.parse_args()
    db = HoronDB(snapshot_mode=True)

    try:
        if args.command == "batch":
            if args.file:
                raw_text = Path(args.file).read_text(encoding="utf-8")
            else:
                raw_text = sys.stdin.read()

            try:
                commands = _parse_batch_commands(raw_text)
            except Exception as e:
                print(f"Fail. Syntax error in batch script: {e}")
                sys.exit(1)

            last_result = None
            for start_line, tokens, raw_cmd in commands:
                try:
                    cmd_args = parser.parse_args(tokens)
                    if cmd_args.command == "batch":
                        print(f"Line {start_line}: batch inside batch is not allowed")
                        sys.exit(1)
                    result = _audited_dispatch(cmd_args, db)
                    if args.all:
                        _print(result)
                    last_result = result
                except SystemExit:
                    first_line = raw_cmd.splitlines()[0] if raw_cmd else ""
                    print(f"Fail. Line {start_line}: invalid command: {first_line}")
                    sys.exit(1)
                except Exception as e:
                    print(f"Fail. Line {start_line}: {e}")
                    sys.exit(1)

            if not args.all and last_result is not None:
                _print(last_result)

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
