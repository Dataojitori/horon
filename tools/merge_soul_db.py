"""Merge a local Horon database with a copy of the same memory that has been used remotely
(for example a SOUL.db downloaded from a hosted Horon), into a new file, and write a report.

Both inputs are opened read-only; the output must be a path that does not exist yet.
The script never replaces either database.

Usage:
    python tools/merge_soul_db.py --local <local db or a copy> --remote <remote db> \
        --out <new file> --report <report.md> [--base <previous merge result>]

How it decides:
- The fork point is the end of the longest prefix that both cli_audit_log tables share.
- After the fork, the audit log says which side successfully wrote each node; the content itself
  is always taken from the real values on each side.
  Written on one side only: that side wins. Written on both sides and different, or different
  with no audit record on either side:
    without --base, the local version is kept and the node is listed under "Needs your decision";
    with --base, a three-way merge runs: if both sides only appended, or changed different
    paragraphs, they are merged automatically; only edits to the same paragraph are listed.
  --base must be the result file of the previous merge, and its audit log must be a prefix of
  the history both sides share.
  Deleted on one side: never deleted automatically, listed under "Needs your decision".
- Nodes created remotely whose ID is taken locally get a new ID.
- Recommendation weights: start from the local weights and replay the remote reads after the
  fork (decay and gain); the two sides' absolute weights are never added together.
- Both databases must have the same migrations applied, so all timestamps on both sides are
  UTC with an explicit offset and compare directly.
- Embeddings come with the side whose when_to_read wins, labelled with their model. If that model
  is not the current one, run `python -m backend.integrity --db <out>` to re-embed them.
- Sessions and pending notifications are not merged.
"""

import argparse
import datetime
import difflib
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HORON_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HORON_ROOT))
from backend._db_query import _AUTO_TRANSITION_STEP, _DECAY_GAMMA, _PRUNING_THRESHOLD  # noqa: E402

NODE_FIELDS = ("name", "content", "when_to_read", "role", "lifespan", "activation_type", "on_fire")
MUTATING_COMMANDS = ("create_concept", "update", "set", "add", "delete", "create_tag", "delete_tag")
AUDIT_COLUMNS = ("session_id", "timestamp", "command", "concept_id", "concept_name", "sub_action", "success")
# Differences in these tables are reported, never merged automatically
STRICT_TABLES = {
    "sensor_hooks": "SELECT sensor_concept_id, event_type, tool, match_pattern, adapter, jev_question, jev_threshold FROM sensor_hooks",
    "tool_guards": "SELECT guard_concept_id, tool, args_pattern, adapter FROM tool_guards",
    "inhibitions": "SELECT target_concept_id, inhibitor_concept_id FROM inhibitions",
    "compose_members": "SELECT parent_concept_id, member_concept_id, order_index FROM compose_members",
}
QUOTE = re.compile(r"「[^「」]*」|“[^“”]*”")
HISTORY_LIMIT = 30          # audit rows record_transition looks back over
CHAIN_BREAK_SECONDS = 1800  # reads more than 30 minutes apart do not chain
MAX_SOURCES = 3
MARKER_SIZE = 31            # length of git conflict markers, so a <<<<<<< inside content is not mistaken for one


def open_read_only(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def parse_time(text: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(text)


def audit_rows(db: sqlite3.Connection) -> list[tuple]:
    columns = ", ".join(("id",) + AUDIT_COLUMNS)
    return [tuple(r) for r in db.execute(f"SELECT {columns} FROM cli_audit_log ORDER BY id")]


def find_fork(local_rows: list[tuple], remote_rows: list[tuple]) -> int:
    """Return the id of the last audit row both sides share; exit if even the first row differs."""
    fork = None
    for a, b in zip(local_rows, remote_rows):
        if a != b:
            break
        fork = a[0]
    if fork is None:
        raise SystemExit("The two audit logs differ from the first row; there is no fork point.")
    return fork


def writes_after(rows: list[tuple], fork: int) -> tuple[set[int], set[int]]:
    """Return (IDs of nodes successfully written after the fork, IDs of nodes created after the fork)."""
    touched, created = set(), set()
    for _id, _session, _time, command, concept_id, _name, _sub, success in rows:
        if _id > fork and success and concept_id is not None and command in MUTATING_COMMANDS:
            touched.add(concept_id)
            if command == "create_concept":
                created.add(concept_id)
    return touched, created


def load_nodes(db: sqlite3.Connection) -> dict[int, dict]:
    return {r["id"]: dict(r) for r in db.execute("SELECT * FROM concepts")}


def node_differs(a: dict, b: dict) -> list[str]:
    return [f for f in NODE_FIELDS if a[f] != b[f]]


def resolve_appends(merged: str) -> str | None:
    """Resolve git's diff3 output. Where both sides only added text at the same spot (empty base
    section), keep both, local first. Return the resolved text, or None if a conflict has a
    non-empty base section (both sides changed the same paragraph)."""
    opening, base_mark, middle, closing = (c * MARKER_SIZE for c in "<|=>")
    result, ours, base, theirs, section = [], [], [], [], None
    for line in merged.split("\n"):
        if line.startswith(opening + " "):
            section, ours, base, theirs = "ours", [], [], []
        elif section and line.startswith(base_mark + " "):
            section = "base"
        elif section and line == middle:
            section = "theirs"
        elif section and line.startswith(closing + " "):
            if base:
                return None
            result += ours + theirs
            section = None
        elif section:
            {"ours": ours, "base": base, "theirs": theirs}[section].append(line)
        else:
            result.append(line)
    return "\n".join(result)


def merge_text(local: str | None, base: str | None, remote: str | None) -> str | None:
    """Three-way merge by line with git merge-file. Return the merged text, or None if both sides changed the same paragraph."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        for name, text in (("local", local), ("base", base), ("remote", remote)):
            # add a trailing newline, or "no newline at end of file" counts as a change to the last line
            (folder / name).write_bytes(((text or "") + "\n").encode("utf-8"))
        done = subprocess.run(
            ["git", "merge-file", "-p", "--diff3", f"--marker-size={MARKER_SIZE}", "local", "base", "remote"],
            cwd=tmp, capture_output=True)
    if not 0 <= done.returncode < 128:
        raise SystemExit(f"git merge-file failed: {done.stderr.decode('utf-8', 'replace')}")
    merged = resolve_appends(done.stdout.decode("utf-8"))
    return None if merged is None else merged[:-1]


def merge_node(local: dict, remote: dict, base: dict) -> tuple[dict, list[str]]:
    """Three-way merge field by field. Return (merged fields, names of fields both sides changed in the same place). Content merges by line; other fields compare as a whole."""
    merged, clashes = {}, []
    for field in NODE_FIELDS:
        if local[field] == remote[field] or remote[field] == base[field]:
            merged[field] = local[field]
        elif local[field] == base[field]:
            merged[field] = remote[field]
        elif field == "content" and (text := merge_text(local[field], base[field], remote[field])) is not None:
            merged[field] = text
        else:
            clashes.append(field)
    return merged, clashes


def classify(local: dict, remote: dict, base: dict, local_touched: set, local_created: set,
             remote_touched: set, remote_created: set) -> dict:
    """Sort every node into one kind of handling. Lists hold node IDs; decisions holds (ID, reason); merged holds {ID: merged fields}."""
    plan = {"take_remote": [], "keep_local": [], "new_remote": [], "decisions": [], "merged": {}}
    for cid in sorted(set(local) | set(remote)):
        # A node created and then deleted remotely is not in remote; an ID it shares with a local node belongs to the local one
        if cid in remote_created and cid in remote:
            plan["new_remote"].append(cid)
        elif cid not in remote:
            if cid not in local_created:
                plan["decisions"].append((cid, "Exists locally but not remotely; not deleted automatically"))
        elif cid not in local:
            plan["decisions"].append((cid, "Exists remotely but not locally; neither deleted nor added automatically"))
        elif node_differs(local[cid], remote[cid]):
            in_local, in_remote = cid in local_touched, cid in remote_touched
            if in_remote and not in_local:
                plan["take_remote"].append(cid)
            elif in_local and not in_remote:
                plan["keep_local"].append(cid)
            elif cid in base:
                merged, clashes = merge_node(local[cid], remote[cid], base[cid])
                if clashes:
                    plan["decisions"].append((cid, f"Both sides changed the same place ({', '.join(clashes)}); local version kept for now"))
                else:
                    plan["merged"][cid] = merged
            elif in_local and in_remote:
                plan["decisions"].append((cid, "Both sides changed it and they differ; local version kept for now"))
            else:
                plan["decisions"].append((cid, "Contents differ but neither audit log records a change; local version kept for now"))
    return plan


def check_base(base_db: sqlite3.Connection, local_rows: list[tuple], fork: int) -> None:
    """The base's audit log must be a prefix of the history both sides share before the fork; otherwise it is not their common ancestor."""
    last = audit_rows(base_db)[-1]
    if last[0] > fork or last not in local_rows[last[0] - 1:last[0] + 1]:
        raise SystemExit(f"The last audit row of --base ({last[0]}) is not in the shared history: it is not the result of the previous merge.")


def assign_ids(new_remote: list[int], local: dict, remote: dict) -> dict[int, int]:
    """Return {old remote ID: new ID after merging}, only for nodes whose ID is taken locally."""
    next_id = max(set(local) | set(remote)) + 1
    remap = {}
    for cid in new_remote:
        if cid in local:
            remap[cid] = next_id
            next_id += 1
    return remap


def check_names(plan: dict, local: dict, remote: dict) -> None:
    names = {n["name"]: cid for cid, n in local.items()}
    for cid in plan["new_remote"] + plan["take_remote"]:
        owner = names.get(remote[cid]["name"])
        if owner is not None and (cid in plan["new_remote"] or owner != cid):
            raise SystemExit(f"Remote node {cid} has the same name as local node {owner}: {remote[cid]['name']}")
    for cid, merged in plan["merged"].items():
        owner = names.get(merged["name"])
        if owner is not None and owner != cid:
            raise SystemExit(f"After merging, node {cid} has the same name as local node {owner}: {merged['name']}")


def build_read_events(rows: list[tuple], fork: int, remap: dict, boot_ids: list[int]) -> list[dict]:
    """Turn the remote read_concept / login calls after the fork back into the sequence of record_transition calls."""
    events, seen_rows = [], []
    for row in rows:
        _id, session, ts, command, concept_id, _name, sub_action, success = row
        if _id <= fork or not success or command not in ("read_concept", "login"):
            continue
        target = remap.get(concept_id, concept_id)
        if command == "login":
            events.append({"session": session, "time": ts, "to": [remap.get(i, i) for i in boot_ids],
                           "before": len(seen_rows)})
            continue
        last = events[-1] if events else None
        if sub_action and last and last.get("batch") == sub_action:
            last["to"].append(target)
        else:
            events.append({"session": session, "time": ts, "to": [target], "before": len(seen_rows),
                           "batch": sub_action})
        seen_rows.append((session, ts, target, sub_action or _id))
    for event in events:
        event["history"] = [r for r in seen_rows[:event["before"]] if r[0] == event["session"]]
    return events


def transition_sources(event: dict) -> list[tuple[int, float]]:
    """Pick sources the same way record_transition does. Return [(source node, relevance)]."""
    last_time = parse_time(event["time"])
    groups: list[tuple[list, object]] = []
    for _session, ts, concept_id, key in reversed(event["history"][-HISTORY_LIMIT:]):
        row_time = parse_time(ts)
        if (last_time - row_time).total_seconds() > CHAIN_BREAK_SECONDS:
            break
        last_time = row_time
        if groups and groups[-1][1] == key:
            groups[-1][0].append(concept_id)
        else:
            groups.append(([concept_id], key))
    sources, used, depth = [], set(event["to"]), 0
    for members, _key in groups:
        fresh = [m for m in dict.fromkeys(members) if m not in used]
        if not fresh:
            continue
        used.update(fresh)
        sources.extend((m, 0.5 ** depth) for m in fresh)
        depth += 1
        if depth == MAX_SOURCES:
            break
    return sources


def replay_reads(weights: dict, events: list[dict], existing: set[int]) -> None:
    """Update weights in place: {(from, to): (weight, last access time)}."""
    for event in events:
        # Nodes deleted locally after the fork are not in the result; transitions into them would break the foreign keys
        # (decay and step still follow the remote read as it happened)
        targets = [t for t in event["to"] if t in existing]
        step = _AUTO_TRANSITION_STEP / len(event["to"])
        for source, relevance in transition_sources(event):
            if source not in existing:
                continue
            factor = _DECAY_GAMMA ** relevance
            for key in [k for k in weights if k[0] == source]:
                weight, ts = weights[key]
                if weight * factor < _PRUNING_THRESHOLD:
                    del weights[key]
                else:
                    weights[key] = (weight * factor, ts)
            for target in targets:
                old = weights.get((source, target), (0.0, ""))[0]
                weights[(source, target)] = (old + step * relevance, event["time"])


def load_weights(db: sqlite3.Connection, remap: dict | None = None) -> dict:
    remap = remap or {}
    return {(remap.get(r[0], r[0]), remap.get(r[1], r[1])): (r[2], r[3])
            for r in db.execute("SELECT from_concept_id, to_concept_id, weight, last_accessed_at FROM concept_transitions")}


def verify_replay(replayed: dict, remote_weights: dict, local_read_ids: set[int]) -> dict:
    """For source nodes the local side never read, the replayed weights must equal the real remote weights. This checks the replay logic."""
    by_source: dict[int, list] = {}
    for key in set(replayed) | set(remote_weights):
        by_source.setdefault(key[0], []).append(key)
    matched, expected_gap, unexplained = 0, 0, []
    for source, keys in by_source.items():
        same = all(k in replayed and k in remote_weights
                   and abs(replayed[k][0] - remote_weights[k][0]) < 1e-9 for k in keys)
        if same:
            matched += 1
        elif source in local_read_ids:
            expected_gap += 1
        else:
            unexplained.append(source)
    return {"matched": matched, "local_also_read": expected_gap, "unexplained": sorted(unexplained)}


def upsert_node(out: sqlite3.Connection, row: dict, keep_created: bool) -> None:
    columns = list(row)
    update_columns = [c for c in columns if c != "id" and not (keep_created and c in ("created_at", "is_active"))]
    assignments = ", ".join(f"{c} = excluded.{c}" for c in update_columns)
    out.execute(
        f"INSERT INTO concepts ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
        f"ON CONFLICT(id) DO UPDATE SET {assignments}",
        [row[c] for c in columns],
    )


def add_snapshot(out: sqlite3.Connection, concept_id: int, name: str, field: str, original, creation: int, ts: str) -> None:
    """Put changes brought in from the remote side into the review list too. Fields that already have a snapshot keep the earliest original value."""
    out.execute(
        "INSERT OR IGNORE INTO snapshots (concept_id, concept_name, field, original_value, is_creation, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (concept_id, name, field, original, creation, ts),
    )


def apply_nodes(out, plan, local, remote, remap) -> None:
    for cid in plan["take_remote"]:
        row = dict(remote[cid])
        row["updated_at"] = row["updated_at"]
        upsert_node(out, row, keep_created=True)
        for field in ("content", "when_to_read"):
            if local[cid][field] != remote[cid][field]:
                add_snapshot(out, cid, local[cid]["name"], field, local[cid][field], 0, row["updated_at"])
    for cid, merged in plan["merged"].items():
        row = {**local[cid], **merged}
        row["updated_at"] = max(local[cid]["updated_at"], remote[cid]["updated_at"])
        upsert_node(out, row, keep_created=True)
        for field in ("content", "when_to_read"):
            if local[cid][field] != merged[field]:
                add_snapshot(out, cid, local[cid]["name"], field, local[cid][field], 0, row["updated_at"])
    for cid in plan["new_remote"]:
        row = dict(remote[cid])
        row["id"] = remap.get(cid, cid)
        row["created_at"] = row["created_at"]
        row["updated_at"] = row["updated_at"]
        upsert_node(out, row, keep_created=False)
        for field in ("content", "when_to_read"):
            add_snapshot(out, row["id"], row["name"], field, None, 1, row["created_at"])


def when_to_read_of(db: sqlite3.Connection, concept_id: int) -> str | None:
    row = db.execute("SELECT when_to_read FROM concepts WHERE id = ?", (concept_id,)).fetchone()
    return row[0] if row else None


def apply_sets(out, local_db, remote_db, plan, remap, decisions, existing: set[int]) -> None:
    """Merge aliases and tags as sets; bring embeddings along for new remote nodes and nodes whose when_to_read changed.
    Remote rows about nodes that are not in the result (e.g. deleted locally after the fork) are skipped."""
    local_aliases = dict(local_db.execute("SELECT alias, concept_id FROM aliases").fetchall())
    copied_aliases = []
    for alias, cid in remote_db.execute("SELECT alias, concept_id FROM aliases").fetchall():
        target = remap.get(cid, cid)
        if target not in existing:
            continue
        if alias not in local_aliases:
            out.execute("INSERT INTO aliases (alias, concept_id) VALUES (?, ?)", (alias, target))
            copied_aliases.append(alias)
        elif local_aliases[alias] != target and cid in plan["new_remote"]:
            decisions.append((cid, f"Alias {alias} points to {local_aliases[alias]} locally"))

    for name, source in remote_db.execute("SELECT name, source_concept_id FROM tags").fetchall():
        out.execute("INSERT OR IGNORE INTO tags (name, source_concept_id) VALUES (?, ?)",
                    (name, remap.get(source, source)))
    for cid, tag in remote_db.execute("SELECT concept_id, tag FROM concept_tags").fetchall():
        if remap.get(cid, cid) not in existing:
            continue
        out.execute("INSERT OR IGNORE INTO concept_tags (concept_id, tag) VALUES (?, ?)", (remap.get(cid, cid), tag))

    # Vectors are copied with their model name even if the two sides use different models. Horon re-embeds
    # any vector whose model is not the current EMBEDDING_MODEL (backend/integrity.py, content_scope_audit.py).
    for row in remote_db.execute("SELECT * FROM concept_embeddings").fetchall():
        cid = row["concept_id"]
        merged = plan["merged"].get(cid)
        reindexed = merged and merged["when_to_read"] == when_to_read_of(remote_db, cid) != when_to_read_of(local_db, cid)
        if cid in plan["new_remote"] or cid in plan["take_remote"] or reindexed:
            out.execute(
                "INSERT INTO concept_embeddings (concept_id, embedding, embedding_model, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(concept_id) DO UPDATE SET embedding = excluded.embedding, "
                "embedding_model = excluded.embedding_model, updated_at = excluded.updated_at",
                (remap.get(cid, cid), row["embedding"], row["embedding_model"], row["updated_at"]),
            )
    for alias in copied_aliases:
        row = remote_db.execute("SELECT * FROM concept_name_embeddings WHERE alias = ?", (alias,)).fetchone()
        if row:
            out.execute(
                "INSERT INTO concept_name_embeddings (alias, embedding, embedding_model, updated_at) VALUES (?, ?, ?, ?)",
                (alias, row["embedding"], row["embedding_model"], row["updated_at"]),
            )


def apply_audit(out, local_rows, remote_rows, fork, remap) -> int:
    """Interleave both audit logs after the fork by time and renumber from the fork point. Return the number of merged rows."""
    merged = [(r[2], 0, r[0], r[1:]) for r in local_rows if r[0] > fork]
    for row in remote_rows:
        if row[0] > fork:
            _id, session, ts, command, concept_id, name, sub_action, success = row
            merged.append((ts, 1, _id,
                           (session, ts, command, remap.get(concept_id, concept_id), name, sub_action, success)))
    merged.sort(key=lambda r: r[:3])
    out.execute("DELETE FROM cli_audit_log WHERE id > ?", (fork,))
    placeholders = ", ".join("?" for _ in range(len(AUDIT_COLUMNS) + 1))
    out.executemany(
        f"INSERT INTO cli_audit_log (id, {', '.join(AUDIT_COLUMNS)}) VALUES ({placeholders})",
        [(fork + i, *m[3]) for i, m in enumerate(merged, 1)],
    )
    return len(merged)


def apply_weights(out, weights: dict) -> None:
    out.execute("DELETE FROM concept_transitions")
    out.executemany(
        "INSERT INTO concept_transitions (from_concept_id, to_concept_id, weight, last_accessed_at) VALUES (?, ?, ?, ?)",
        [(a, b, w, ts) for (a, b), (w, ts) in weights.items()],
    )


def strict_table_differences(local_db, remote_db, remap) -> list[str]:
    notes = []
    for table, query in STRICT_TABLES.items():
        left = {tuple(r) for r in local_db.execute(query)}
        right = {tuple(remap.get(v, v) if isinstance(v, int) else v for v in r) for r in remote_db.execute(query)}
        if left != right:
            notes.append(f"{table}: {len(left - right)} rows only local, {len(right - left)} rows only remote; not merged")
    return notes


def verify_output(out_path, plan, local, remote, remap) -> dict:
    """Reopen the result file and check it node by node. Exit on any mismatch."""
    db = open_read_only(out_path)
    result = load_nodes(db)
    expected_count = len(local) + sum(1 for cid in plan["new_remote"] if cid not in local or cid in remap)
    if len(result) != expected_count:
        raise SystemExit(f"Wrong node count: expected {expected_count}, got {len(result)}")
    for cid, node in local.items():
        wanted = remote[cid] if cid in plan["take_remote"] else plan["merged"].get(cid, node)
        if node_differs(result[cid], wanted):
            raise SystemExit(f"Node {cid} differs from the version it should have")
    for cid in plan["new_remote"]:
        if node_differs(result[remap.get(cid, cid)], remote[cid]):
            raise SystemExit(f"New remote node {cid} differs after writing")
    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    broken = db.execute("PRAGMA foreign_key_check").fetchall()
    if integrity != "ok" or broken:
        raise SystemExit(f"Database check failed: {integrity}, {len(broken)} foreign key problems")
    lost = []
    for cid, node in {**remote, **local}.items():
        if cid in plan["new_remote"]:
            continue
        final = result.get(cid, {}).get("content") or ""
        for version, label in ((local.get(cid), "local"), (remote.get(cid), "remote")):
            if version:
                for quote in QUOTE.findall(version["content"] or ""):
                    if quote not in final:
                        lost.append((cid, label, quote))
    counts = {"concepts": len(result),
              "aliases": db.execute("SELECT count(*) FROM aliases").fetchone()[0],
              "concept_tags": db.execute("SELECT count(*) FROM concept_tags").fetchone()[0],
              "embeddings": db.execute("SELECT count(*) FROM concept_embeddings").fetchone()[0],
              "transitions": db.execute("SELECT count(*) FROM concept_transitions").fetchone()[0],
              "audit": db.execute("SELECT count(*) FROM cli_audit_log").fetchone()[0],
              "snapshots": db.execute("SELECT count(*) FROM snapshots").fetchone()[0]}
    db.close()
    return {"lost_quotes": lost, "counts": counts}


def diff_text(left: str | None, right: str | None, right_label: str = "remote") -> str:
    lines = difflib.unified_diff((left or "").splitlines(), (right or "").splitlines(), "local", right_label, lineterm="", n=0)
    return "\n".join(lines)


def write_report(path, args, fork, fork_time, plan, local, remote, remap, replay_check, notes, checks,
                 remote_writes, local_writes, reference_hits) -> None:
    out = ["# Merge report: local and remote database", "",
           f"- Local: `{args.local}`", f"- Remote: `{args.remote}`", f"- Result: `{args.out}`",
           f"- Fork point: audit row {fork} ({fork_time})",
           f"- Nodes written after the fork: {len(local_writes)} local, {len(remote_writes)} remote", ""]
    out += ["## Needs your decision", ""]
    if plan["decisions"] or notes or reference_hits:
        out += [f"- [{cid}] {reason}" for cid, reason in plan["decisions"]]
        out += [f"- {note}" for note in notes]
        out += [f"- [{cid}] content mentions renumbered node {old}; the script did not change it" for cid, old in reference_hits]
    else:
        out.append("None.")
    out += ["", "## Nodes taken from the remote side", ""]
    for cid in plan["take_remote"]:
        out += [f"### [{cid}] {remote[cid]['name']}", "", "```diff", diff_text(local[cid]["content"], remote[cid]["content"]), "```", ""]
    out += ["## Nodes merged three-way automatically", ""]
    if not plan["merged"]:
        out += ["None", ""]
    for cid, merged in plan["merged"].items():
        out += [f"### [{cid}] {merged['name']}", "", "Difference between local and merged:", "", "```diff",
                diff_text(local[cid]["content"], merged["content"], "merged"), "```", ""]
    out += ["## Nodes kept at the local version", ""]
    out += [f"- [{cid}] {local[cid]['name']}" for cid in plan["keep_local"]] or ["None"]
    out += ["", "## Nodes created remotely", ""]
    for cid in plan["new_remote"]:
        target = remap.get(cid, cid)
        note = f" (renumbered: {cid} → {target})" if cid in remap else ""
        out.append(f"- [{target}] {remote[cid]['name']}{note}")
    out += ["", "## Check of the replayed recommendation weights", "",
            f"- Source nodes matching the real remote result: {replay_check['matched']}",
            f"- Also read locally, so expected to differ: {replay_check['local_also_read']}",
            f"- Unexplained: {replay_check['unexplained'] or 'none'}", "",
            "## Checks", "", f"- Row counts in the result: {checks['counts']}", "- Integrity and foreign keys: passed", "- Every node equals the version it should have: passed", ""]
    out += ["## Quoted text found only in an old version on one side", "",
            "These quotes are missing from the result. When only one side rewrote the node, the rewrite itself usually removed them; take a look.", ""]
    out += [f"- [{cid}] ({label}) {quote}" for cid, label, quote in checks["lost_quotes"]] or ["None"]
    Path(path).write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--local", type=Path, required=True)
    parser.add_argument("--remote", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--base", type=Path, help="result file of the previous merge; enables the three-way merge")
    args = parser.parse_args()

    if args.out.exists():
        raise SystemExit(f"Output file already exists: {args.out}")
    if args.out.resolve() in (args.local.resolve(), args.remote.resolve()):
        raise SystemExit("The output cannot be one of the inputs.")

    local_db, remote_db = open_read_only(args.local), open_read_only(args.remote)
    if dict(local_db.execute("SELECT name, sql FROM sqlite_master").fetchall()) != \
            dict(remote_db.execute("SELECT name, sql FROM sqlite_master").fetchall()):
        raise SystemExit("The two databases have different schemas; bring them to the same Horon version first.")
    if {r[0] for r in local_db.execute("SELECT version FROM schema_migrations")} !=             {r[0] for r in remote_db.execute("SELECT version FROM schema_migrations")}:
        raise SystemExit("The two databases have different migrations applied; open both with the same Horon version first.")

    local_rows, remote_rows = audit_rows(local_db), audit_rows(remote_db)
    fork = find_fork(local_rows, remote_rows)
    fork_time = next(r[2] for r in local_rows if r[0] == fork)
    local_touched, local_created = writes_after(local_rows, fork)
    remote_touched, remote_created = writes_after(remote_rows, fork)

    local, remote = load_nodes(local_db), load_nodes(remote_db)
    base = {}
    if args.base:
        base_db = open_read_only(args.base)
        check_base(base_db, local_rows, fork)
        base = load_nodes(base_db)
        base_db.close()
    plan = classify(local, remote, base, local_touched, local_created, remote_touched, remote_created)
    check_names(plan, local, remote)
    remap = assign_ids(plan["new_remote"], local, remote)

    # Replay the remote reads after the fork
    boot_ids = [r[0] for r in remote_db.execute(
        "SELECT concept_id FROM concept_tags WHERE tag = 'boot' ORDER BY concept_id")]
    events = build_read_events(remote_rows, fork, remap, boot_ids)
    weights = load_weights(local_db)
    existing = set(local) | {remap.get(c, c) for c in plan["new_remote"]}
    replay_reads(weights, events, existing)
    local_read_ids = {r[4] for r in local_rows if r[0] > fork and r[3] == "read_concept"} | \
                     ({i for i in boot_ids} if any(r[0] > fork and r[3] == "login" for r in local_rows) else set())
    # Check: starting from local weights only, source nodes the local side never touched should come out equal to the remote result
    replay_check = verify_replay(weights, load_weights(remote_db, remap), local_read_ids)
    if replay_check["unexplained"]:
        print("Replayed recommendation weights have unexplained source nodes:", replay_check["unexplained"], file=sys.stderr)

    reference_hits = []
    if remap:
        pattern = re.compile(r"(?<!\d)(" + "|".join(str(i) for i in remap) + r")(?!\d)")
        for cid in plan["new_remote"] + plan["take_remote"]:
            for match in set(pattern.findall(remote[cid]["content"] or "")):
                reference_hits.append((cid, match))

    notes = strict_table_differences(local_db, remote_db, remap)

    target = sqlite3.connect(args.out)
    local_db.backup(target)
    target.execute("BEGIN")
    apply_nodes(target, plan, local, remote, remap)
    apply_sets(target, local_db, remote_db, plan, remap, plan["decisions"], existing)
    apply_weights(target, weights)
    apply_audit(target, local_rows, remote_rows, fork, remap)
    target.commit()
    target.close()

    checks = verify_output(args.out, plan, local, remote, remap)
    write_report(args.report, args, fork, fork_time, plan, local, remote, remap, replay_check, notes, checks,
                 remote_touched, local_touched, reference_hits)
    print(f"Done. Fork point {fork}; taken from remote {len(plan['take_remote'])}, kept local {len(plan['keep_local'])}, "
          f"merged three-way {len(plan['merged'])}, created remotely {len(plan['new_remote'])}, "
          f"needs your decision {len(plan['decisions'])}. Report: {args.report}")


if __name__ == "__main__":
    main()
