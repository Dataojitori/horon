"""Read-only Horon CLI for the external reviewer (外审).

Exposes ONLY non-mutating commands: read_concept, search_concepts,
list_concepts, compile, read_memory. It wires up NO mutation path at all — so
even if the reviewer's prompt or its opencode bash permissions were somehow
bypassed, this entrypoint CANNOT add / set / delete / update / create / batch
the graph.

This is the HARD lock. The opencode agent's bash allow-list (which permits only
`python frontend/review_cli.py *`) is the soft second layer. Two locks, because
the global config's `python frontend/cli.py *: allow` would otherwise let a
reviewer mutate.

Usage (same shape as cli.py, minus every write command):
    python frontend/review_cli.py read_concept <name>
    python frontend/review_cli.py search_concepts <query>
    python frontend/review_cli.py list_concepts
    python frontend/review_cli.py compile --assume A --goal G [--block ...] [--constraints ...]
    python frontend/review_cli.py read_memory <uri>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.db import HoronDB
from frontend.cli import (  # reuse the exact read-side formatters
    RawOutput,
    _print,
    _read_nocturne_memory,
)


def _build_parser() -> argparse.ArgumentParser:
    """Build the read-only argument parser.

    Returns:
        A parser exposing only the five non-mutating subcommands.
    """
    parser = argparse.ArgumentParser(
        prog="horon-review",
        description="Horon read-only CLI (external reviewer)",
        allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("read_concept", allow_abbrev=False)
    p.add_argument("concept")

    p = sub.add_parser("search_concepts", allow_abbrev=False)
    p.add_argument("query")

    sub.add_parser("list_concepts", allow_abbrev=False)

    p = sub.add_parser("compile", allow_abbrev=False)
    p.add_argument("--assume", nargs="+", required=True)
    p.add_argument("--block", nargs="*", default=[])
    p.add_argument("--constraints", nargs="*", default=[])
    p.add_argument("--goal", required=True)

    p = sub.add_parser("read_memory", allow_abbrev=False)
    p.add_argument("uri")

    return parser


def _dispatch(args: argparse.Namespace, db: HoronDB):
    """Execute one read-only command and return its result object.

    Args:
        args: Parsed CLI arguments.
        db: An open HoronDB handle.

    Returns:
        A ReadResult / list / dict / RawOutput suitable for `_print`.

    Raises:
        ValueError: on a compile input error or an unknown command.
    """
    if args.command == "read_concept":
        return db.read_concept(args.concept)

    if args.command == "search_concepts":
        return db.search_concepts(args.query)

    if args.command == "list_concepts":
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
        return RawOutput("\n".join(lines))

    if args.command == "compile":
        result = db.compile(
            args.assume, args.block, args.constraints, args.goal)
        if result["errors"]:
            raise ValueError("; ".join(result["errors"]))
        return result

    if args.command == "read_memory":
        return RawOutput(_read_nocturne_memory(args.uri))

    raise ValueError(f"unknown command: {args.command}")


def main() -> None:
    """Parse args, run one read-only command, print the result."""
    parser = _build_parser()
    args = parser.parse_args()
    db = HoronDB()
    try:
        _print(_dispatch(args, db))
    except Exception as e:
        print(f"Fail. {e}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
