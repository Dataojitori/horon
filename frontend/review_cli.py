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
    python frontend/review_cli.py search_concepts <query> [--tag <tag>] [--limit <n>]
    python frontend/review_cli.py list_concepts [--tag <tag>]
    python frontend/review_cli.py compile --target <name> [--assume ...]
    python frontend/review_cli.py read_memory <uri>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.db import HoronDB
from frontend.cli import (
    RawOutput,
    _format_list_concepts,
    _format_search_concepts,
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

    # read_concept
    p = sub.add_parser("read_concept", allow_abbrev=False)
    p.add_argument("concept")

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

    # compile
    p = sub.add_parser("compile", allow_abbrev=False,
                       help="Backward solver diagnostics for target concept.")
    p.add_argument("--target", required=True,
                   help="Target concept to diagnose (why it is active / inactive)")
    p.add_argument("--assume", nargs="*", default=[],
                   help="Sensors to hypothetically light, in the given order (sensor only; logic/guard potentials are derived)")

    # read_memory
    p = sub.add_parser("read_memory", allow_abbrev=False)
    p.add_argument("uri")
    # No --out here (unlike cli.py): a file-write path would let the reviewer
    # overwrite horon.db / opencode.json and break the read-only hard lock.

    return parser


def _dispatch(args: argparse.Namespace, db: HoronDB):
    """Execute one read-only command and return its result object.

    Args:
        args: Parsed CLI arguments.
        db: An open HoronDB handle.

    Returns:
        A ReadResult / CompileResult / RawOutput suitable for `_print`.

    Raises:
        ValueError: on an unknown command.
    """
    if args.command == "read_concept":
        return db.read_concept(args.concept)

    if args.command == "search_concepts":
        results = db.search_concepts(args.query, tag_expr=args.tag, limit=args.limit)
        return RawOutput(_format_search_concepts(results))

    if args.command == "list_concepts":
        overviews = db.get_all_concepts_overview(tag_expr=args.tag)
        return RawOutput(_format_list_concepts(overviews))

    if args.command == "compile":
        return db.compile(
            target=args.target,
            assume=args.assume,
        )

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
