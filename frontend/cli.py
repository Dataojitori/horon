"""
Horon CLI — operational interface for intelligent agents
Usage:
    python frontend/cli.py <command> [options]
    python frontend/cli.py batch [--file path] [--all]
"""

import argparse
import json
import os
import shlex
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from backend.db import HoronDB
from backend.models import MutationResult, ReadResult
from backend.text_patch import (
    normalize_literal_newlines,
    try_normalized_patch,
)

load_dotenv(Path(__file__).parent.parent / ".env")


def _format_route(edges: list[dict], label: str) -> list[str]:
    """将由多条边拼接而成的路径，格式化为易读的文本列表。

    Args:
        edges: compile 返回的边链。每条边形如
               {"from": {"name": ...}, "to": {"name": ...},
                "concept_id": int, "name": str, "status": str,
                "segments": [{"from": ..., "to": ...}, ...]}
        label: 输出首行的前缀标签（如 "route", "detour"）。

    Returns:
        多行字符串列表。格式示例（label="route"）：

        连续路径：
        [
            "route: A → B(confirmed) → C(confirmed)",
            "  A → B : see \"AtoB\"",
            "  B → C : see \"BtoC\""
        ]

        含影分身跳跃（edge[i].to ≠ edge[i+1].from）：
        [
            "route: A → B(confirmed), AtoB → C(confirmed)",
            "  A → B : see \"AtoB\"",
            "  AtoB → C : see \"AtoBtoC\""
        ]

        第一行（摘要）：沿途节点用 → 连接。相邻边不连续时用逗号
        分段重起，提示读者去看明细行了解跳跃原因。
        后续行（明细）：每条边的物理遍历 from → to + 关系概念名，
        供 read_concept 查阅。
    """
    if not edges:
        return [f"{label}: (empty)"]

    # ── 摘要：检测断裂，分段拼接 ──
    route_segments: list[list[str]] = []
    seg: list[str] = [edges[0]["from"]["name"]]
    for i, edge in enumerate(edges):
        is_break = False
        if i > 0:
            if len(edge["from"]["concept_ids"]) > 1:
                # & 组具有多源汇聚语义，在单行文本中必须强制断开重起
                is_break = True
            elif edge["from"]["concept_ids"][0] != edges[i - 1]["to"]["concept_id"]:
                # 断裂：上一条边的 to 和这条边的 from 不是同一个概念
                is_break = True

        if is_break:
            route_segments.append(seg)
            seg = [edge["from"]["name"]]
            
        if edge.get("segments"):
            for s in edge["segments"]:
                seg.append(f'{s["to"]["name"]}({edge["status"]})')
        else:
            seg.append(f'{edge["to"]["name"]}({edge["status"]})')
            
    route_segments.append(seg)
    summary = ", ".join(" → ".join(s) for s in route_segments)
    lines = [f"{label}: {summary}"]

    # ── 明细 ──
    for edge in edges:
        if edge.get("segments"):
            for s in edge["segments"]:
                lines.append(
                    f'  {s["from"]["name"]} → {s["to"]["name"]}'
                    f' : see "{edge["name"]}"'
                )
        else:
            lines.append(
                f'  {edge["from"]["name"]} → {edge["to"]["name"]}'
                f' : see "{edge["name"]}"'
            )
    return lines


def _format_compile(result: dict) -> str:
    """compile 结果 → 行動指引文本（给 agent 的提示词）。

    输出结构（固定三段）：
      1. 判定 —— passed / BLOCKED / failed
      2. 路线展示 —— route / verified / detour
      3. 行动指令 —— 假设阻断清单 或 确认后的执行提醒（二选一，不重叠）
    """
    lines = []

    # ── 硬错误（概念解析失败等）──
    if result["errors"]:
        for e in result["errors"]:
            lines.append(f"error: {e}")
        return "\n".join(lines)

    compiled = result["compiled_route"]
    brk = result["break"]
    detour = result.get("detour")

    # 收集所有会展示给 agent 的边，统一做假设检查
    all_edges = list(compiled) + (detour or [])
    hyp_edges = [e for e in all_edges if e["status"] != "confirmed"]

    # ── 第一段：判定 ──
    if result["passed"]:
        if hyp_edges:
            lines.append(
                f"BLOCKED. route exists but contains "
                f"{len(hyp_edges)} unverified hypothesis(es). "
                f"do NOT execute until every hypothesis is resolved.")
        else:
            lines.append(
                "compilation passed. this route is ready to execute:")
    else:
        lines.append(
            f'compilation failed. '
            f'no known path from {brk["from"]["name"]} '
            f'to {brk["to"]["name"]}.')

    # ── 第二段：路线展示 ──
    if result["passed"]:
        lines.extend(_format_route(compiled, "route"))
    else:
        if compiled:
            lines.append(
                "verified path from start to break point:")
            lines.extend(_format_route(compiled, "verified"))
        if detour:
            lines.append(
                "an alternative route from one of your established waypoints to the goal exists:")
            lines.extend(_format_route(detour, "detour"))
        if not compiled and not detour:
            lines.append(
                "no route from start to goal exists. "
                "bridge the gap with hypotheses.")

    # ── 第三段：行动指令（假设阻断 与 执行提醒 互斥）──
    if hyp_edges:
        lines.append(
            "unverified steps in this route "
            "(to investigate these assumptions, use read_concept):"
        )
        for e in hyp_edges:
            if e.get("segments"):
                for s in e["segments"]:
                    lines.append(
                        f'  ✗ {s["from"]["name"]} → {s["to"]["name"]} '
                        f'({e["status"]}) : see "{e["name"]}"')
            else:
                lines.append(
                    f'  ✗ {e["from"]["name"]} → {e["to"]["name"]} '
                    f'({e["status"]}) : see "{e["name"]}"')
    elif all_edges:
        lines.append(
            'all steps confirmed. if you intend to execute any part of this route, '
            'use read_concept to inspect the nodes first — do not assume based on names alone.')

    return "\n".join(lines)


def _format_read_concept(result: ReadResult) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append(f"CONCEPT: {result.name} (ID: {result.id})")

    alt_names = [a for a in result.aliases if a != result.name]
    if alt_names:
        lines.append(f"Also known as: {', '.join(alt_names)}")

    disc = result.disclosure if result.disclosure else "(not set)"
    lines.append(f"Disclosure: {disc}")
    lines.append("=" * 60)

    if result.alerts:
        lines.append("[!] ALERTS (Requires Attention)")
        for alert in result.alerts:
            lines.append(f"- {alert}")
        lines.append("=" * 60)

    lines.append(f"VARIATIONS ({len(result.variations)})")
    multi = len(result.variations) > 1
    for v in result.variations:
        lines.append("")
        if multi:
            lines.append(f"--- [{v.short_code}] ---")
        if v.expression:
            type_tag = f" [{v.type}]" if v.type else ""
            lines.append(f"Expression: {v.expression}{type_tag} (status: {v.status or 'not set'})")
        else:
            lines.append(f"Expression: (Atomic / Not yet decomposed) (status: {v.status or 'not set'})")
        lines.append(f"Evidence:\n{v.evidence}" if v.evidence else "Evidence: (empty)")
        lines.append(f"Unless:\n{v.unless}" if v.unless else "Unless: (empty)")
    lines.append("")
    lines.append("=" * 60)

    def format_rel_group(group_name, items):
        if not items:
            return []
        out = [f"  * {group_name}:"]
        for item in items:
            out.append(f"    - {item.expression} (Concept ID: {item.concept_id}, Name: '{item.concept_name}')")
            # 关系另一端的成员列表，逐个展示有 disclosure 的。
            for member in item.members:
                if member.disclosure:
                    out.append(
                        f"      Disclosure ({member.concept_name}): "
                        f"{member.disclosure}")
        return out

    inbound_lines = []
    inbound_lines.extend(format_rel_group("Confirmed", result.inbound_confirmed))
    inbound_lines.extend(format_rel_group("Hypotheses", result.inbound_hypotheses))
    inbound_lines.extend(format_rel_group("Negated", result.inbound_negated))

    outbound_lines = []
    outbound_lines.extend(format_rel_group("Confirmed", result.outbound_confirmed))
    outbound_lines.extend(format_rel_group("Hypotheses", result.outbound_hypotheses))
    outbound_lines.extend(format_rel_group("Negated", result.outbound_negated))

    lines.append("RELATIONS (Connections to this Concept)")

    lines.append("")
    lines.append(f"[ INBOUND ] (Paths leading TO '{result.name}')")
    if inbound_lines:
        lines.extend(inbound_lines)
    else:
        lines.append("  (empty)")

    lines.append("")
    lines.append(f"[ OUTBOUND ] (Paths leading FROM '{result.name}')")
    if outbound_lines:
        lines.extend(outbound_lines)
    else:
        lines.append("  (empty)")

    lines.append("")
    lines.append("=" * 60)

    return "\n".join(lines)


class RawOutput:
    def __init__(self, content: str):
        self.content = content


def _print(obj):
    if isinstance(obj, MutationResult):
        print(obj.message)
    elif isinstance(obj, ReadResult):
        print(_format_read_concept(obj))
    elif isinstance(obj, dict) and "compiled_route" in obj:
        print(_format_compile(obj))
    elif isinstance(obj, dict):
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    elif isinstance(obj, list):
        for item in obj:
            print(item.model_dump_json(indent=2))
    elif obj is None:
        print("done")
    elif isinstance(obj, RawOutput):
        sys.stdout.write(obj.content)
    elif isinstance(obj, str):
        print(obj)
    else:
        print(obj.model_dump_json(indent=2))


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
        if current_value is None:
            return append
        return current_value + "\n" + append

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


def _build_parser():
    parser = argparse.ArgumentParser(prog="horon", description="Horon CLI",
                                     allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)

    # create_concept
    p = sub.add_parser("create_concept", allow_abbrev=False)
    p.add_argument("name")
    p.add_argument("--disclosure", default=None)

    # search_concepts
    p = sub.add_parser("search_concepts", allow_abbrev=False)
    p.add_argument("query")

    # list_concepts
    p = sub.add_parser("list_concepts", allow_abbrev=False)
    p.add_argument("--with-disclosure", action="store_true", default=False)

    # add (name or variation)
    p = sub.add_parser("add", allow_abbrev=False)
    p.add_argument("target")
    p.add_argument("kind", choices=["name", "variation"])
    p.add_argument("value")

    # delete (default=variation, or name/expression)
    p = sub.add_parser("delete", allow_abbrev=False)
    p.add_argument("target")
    p.add_argument("kind", nargs="?", default=None,
                   choices=["name", "expression"])
    p.add_argument("value", nargs="?", default=None)

    # set (disclosure, status, name)
    p = sub.add_parser("set", allow_abbrev=False)
    p.add_argument("target")
    p.add_argument("prop", choices=["disclosure", "status", "name",
                                     "expression"])
    p.add_argument("value")

    # update (evidence / unless — patch or append)
    p = sub.add_parser("update", allow_abbrev=False)
    p.add_argument("node")
    p.add_argument("field", choices=["evidence", "unless"])
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

    # compile — travel order: start, waypoints..., goal.
    # Waypoints are constraints on the path search: the more you give,
    # the tighter the route is pinned down. Two concepts = unconstrained
    # search (subsumes the old find_path).
    p = sub.add_parser("compile", allow_abbrev=False)
    p.add_argument("steps", nargs="+")
    p.add_argument("goal")

    # read_memory — read from nocturne_memory.db
    p = sub.add_parser("read_memory", allow_abbrev=False,
        help="Read content from nocturne memory by URI. "
             "Prints to stdout or writes to --out file.")
    p.add_argument("uri",
        help="Nocturne memory URI (e.g. core://nocturne/bluesky)")
    p.add_argument("--out", default=None,
        help="Write content to file instead of stdout")

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
    if args.command == "create_concept":
        return db.create_concept(args.name, args.disclosure)

    elif args.command == "search_concepts":
        return db.search_concepts(args.query)

    elif args.command == "list_concepts":
        overviews = db.get_all_concepts_overview()
        lines = []
        for c in overviews:
            lines.append(f"[{c['id']}] {c['name']}")
            
            variations = c["variations"]
            for v in variations:
                expr = v["expression"]
                status = v["status"]
                prefix = f"[{v['short_code']}] " if len(variations) > 1 else ""
                
                if expr:
                    lines.append(f"      = {prefix}{expr} ({status})")
                else:
                    lines.append(f"      = {prefix}[Atomic] ({status})")
                    
            if args.with_disclosure and c["disclosure"]:
                lines.append(f"      Disclosure: {c['disclosure']}")
                
        return RawOutput("\n".join(lines))

    elif args.command == "add":
        return db.add(args.target, args.kind, args.value)

    elif args.command == "delete":
        return db.delete(args.target, args.kind,
                         getattr(args, "value", None))

    elif args.command == "set":
        return db.set(args.target, args.prop, args.value)

    elif args.command == "update":
        # 使用 DB 提供的 helper 获取当前值，避免在 CLI 层重复解析 variation
        _, _, current_value = db.get_variation_field(args.node, args.field)
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
        return db.read_concept(args.concept)

    elif args.command == "read_memory":
        content = _read_nocturne_memory(args.uri)
        if args.out:
            Path(args.out).write_text(content, encoding="utf-8")
            return f"Success. Wrote {len(content)} chars to {args.out}"
        return RawOutput(content)

    elif args.command == "compile":
        return db.compile(args.steps, args.goal)


def _audited_dispatch(args, db):
    """Wrap _dispatch with audit logging.

    Audit info comes from two sources:
      - sub_action: CLI routing (which sub-command was used)
      - concept_id/name/short_code: DB return value (MutationResult or ReadResult)
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
    short_code = None

    if isinstance(result, MutationResult):
        concept_id = result.concept_id
        concept_name = result.concept_name
        short_code = result.short_code
    elif isinstance(result, ReadResult):
        concept_id = result.id
        concept_name = result.name

    db.log_action(
        command=cmd,
        concept_id=concept_id,
        concept_name=concept_name,
        short_code=short_code,
        sub_action=sub_action,
        success=True,
    )
    return result


def main():
    parser = _build_parser()
    args = parser.parse_args()
    db = HoronDB()

    try:
        if args.command == "batch":
            if args.file:
                lines = Path(args.file).read_text(encoding="utf-8").splitlines()
            else:
                lines = sys.stdin.read().splitlines()

            last_result = None
            for i, line in enumerate(lines, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    lexer = shlex.shlex(line, posix=True)
                    lexer.whitespace_split = True
                    lexer.escape = ''
                    tokens = list(lexer)
                    cmd_args = parser.parse_args(tokens)
                    if cmd_args.command == "batch":
                        print(f"Line {i}: batch inside batch is not allowed")
                        sys.exit(1)
                    result = _audited_dispatch(cmd_args, db)
                    if args.all:
                        _print(result)
                    last_result = result
                except SystemExit:
                    print(f"Fail. Line {i}: invalid command: {line}")
                    sys.exit(1)
                except Exception as e:
                    print(f"Fail. Line {i}: {e}")
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
