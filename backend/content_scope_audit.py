"""Jev-backed line-placement audit: does each line of a node's content belong under its name?

The only thing audited right now is single-node bloat: content that has grown past what
the name covers. Each non-empty line is asked "keep it in this node, or move it?", with
the names of the k most similar nodes (by name embedding) shown as context. A node needs
work when the lines Jev would move add up to at least MIN_BYTES bytes of UTF-8.
Sizes are measured in UTF-8 bytes rather than characters so the line means roughly the
same amount of content in any language (a CJK character is 3 bytes, and the same text in
English takes about three times as many characters). MIN_BYTES was set by hand on a dozen
Chinese nodes whose content was read and judged: nodes that really mixed in another topic
had >= 626 moved bytes, while clean nodes only showed single short lines drifting across
0.5 (<= 174 bytes). Jev leans towards "keep", which is expected; the result is a health
reading, not a list of edits.
"""

from __future__ import annotations

import zlib
from typing import Any

import numpy as np

from backend._db_common import _now
from backend.embedding import (
    EMBEDDING_MODEL, blob_to_embedding, embed_texts, embedding_to_blob, openrouter_post,
)


DEFAULT_MODEL = "typesafe/jev-1.13"
DECISIONS_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"

# A line counts as misplaced when Jev's "move" probability exceeds this.
MOVE_THRESHOLD = 0.5
# Misplaced UTF-8 bytes needed to flag the node. Nodes shorter than this cannot reach it,
# so they are passed without asking Jev.
MIN_BYTES = 500
# How many similar node names to show Jev as context.
NEIGHBOURS = 6

# Every sentence that depends on the language lives here; line labels are "L<n>" so
# the format itself carries no language. Swap this block to support another language.
PROMPT = {
    "intro": "下面是一个 AI 留给自己的记忆库里，名字为「{name}」的档案的内容，'我'指这个 AI 自己。它以后只能靠名字把档案找回来。",
    "others": "记忆库里还有这些名字相近的档案，另外也可以新建档案：",
    "body": "「{name}」的档案内容：",
    "question": "L{n} 放在「{name}」档案里合适，还是移走更好？",
    "keep": "这一行放在「{name}」档案里合适。",
    "move": "这一行更适合放进别的名字的档案，或者新建一个档案。",
}

# Shown under every report. Like PROMPT, swap it together with the language.
REPORT_FOOTER = """每行问 Jev 留在本节点还是移走；移走概率 > 0.5 的行加起来满 {min_bytes} B（UTF-8）判待检查。
这是节点健康度的读数，不是修改清单：不要照着标出的行挪，也不要读一遍就说“不用改”。
待检查说明名字和内容有一边出了问题。Jev 只看得到名字和正文，相当于一个没有上下文的陌生读者。读完整个节点，逐一排查：
1. 名字太窄、太空，装不下正文 → 改名字。
2. 缺一句交代，陌生读者接不上（别名、身份、前后关系）→ 补一句。
3. 装了两件事 → 拆开。去处先看上面列出的相近节点，再用 intent 搜，都没有才新建。
4. 死内容：已撤回的规则、指向不存在节点的引用、和别处矛盾的事实 → 删或改。
5. 相近节点里有名字、用途跟它重叠的 → 读一下，重复就合并。
改完重跑：改名后反而更差，问题在内容；改名后还要查别处对旧名字的引用。
认为某一行 Jev 判错了，要说得出它为什么读不懂这一行；同一种误判反复出现，就去改审核规则。"""


def load_name_embeddings(db) -> tuple[list[int], list[str], np.ndarray]:
    """Return every node's primary name and its unit-length embedding, from concept_name_embeddings.

    Input: an open HoronDB.
    Vectors are stored per name string (concept_name_embeddings.alias -> aliases.alias), and
    a node is looked up by its current primary name. Names with no stored vector, or one
    from another embedding model, are embedded now (in batches of 64) and written back.
    Stored and fetched vectors are already unit-length (embed_texts normalises them).
    Output: (ids, names, matrix) where names[i] is the name of node ids[i] and row i of
    matrix is its embedding.
    """
    rows = db.conn.execute(
        "SELECT c.id, c.name, e.embedding, e.embedding_model "
        "FROM concepts c LEFT JOIN concept_name_embeddings e ON e.alias = c.name ORDER BY c.id"
    ).fetchall()
    vectors: dict[int, list[float]] = {}
    todo: list[tuple[int, str]] = []
    for r in rows:
        if r["embedding_model"] == EMBEDDING_MODEL:
            vectors[r["id"]] = blob_to_embedding(r["embedding"])
        else:
            todo.append((r["id"], r["name"]))
    for start in range(0, len(todo), 64):
        batch = todo[start:start + 64]
        fetched = embed_texts([name for _, name in batch])
        with db.conn:
            db.conn.executemany(
                "INSERT INTO concept_name_embeddings (alias, embedding, embedding_model, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(alias) DO UPDATE SET "
                "embedding=excluded.embedding, "
                "embedding_model=excluded.embedding_model, updated_at=excluded.updated_at",
                [(name, embedding_to_blob(vector), EMBEDDING_MODEL, _now())
                 for (_, name), vector in zip(batch, fetched)],
            )
        vectors.update({cid: vector for (cid, _), vector in zip(batch, fetched)})
    ids = [r["id"] for r in rows]
    matrix = np.array([vectors[cid] for cid in ids], dtype=np.float32)
    return ids, [r["name"] for r in rows], matrix


def similar_names(own_id: int, ids: list[int], names: list[str], matrix: np.ndarray,
                  k: int = NEIGHBOURS) -> list[tuple[int, str]]:
    """(id, name) of the k other nodes whose name embedding is closest to this node's name.

    Input: the node's id and the (ids, names, matrix) returned by load_name_embeddings.
    """
    sims = matrix @ matrix[ids.index(own_id)]
    return [(ids[i], names[i]) for i in np.argsort(-sims) if ids[i] != own_id][:k]


def audit_content_bytes(content: str | None) -> int:
    """Count exactly the non-empty line bytes used by the audit threshold."""
    return sum(len(t.encode("utf-8")) for t in (content or "").splitlines() if t.strip())


def audit_fingerprint(name: str, content: str | None) -> str:
    """Identify the name and content that were audited, as 8 hex digits.

    Every audit recomputes this for every node, so speed matters more than collision
    resistance: CRC32 over name + NUL + content takes about a quarter of the time of
    JSON + SHA-256, and an edit going unnoticed needs a 1-in-2^32 coincidence.
    """
    return f"{zlib.crc32((name + chr(0) + (content or '')).encode('utf-8')):08x}"


def jev_line_verdicts(name: str, content: str, other_names: list[str],
                      model: str = DEFAULT_MODEL) -> list[dict[str, Any]]:
    """Ask Jev, line by line, whether each line belongs under this node's name.

    Input: the node's name, its full content, and the similar node names shown as context.
    The request shows Jev the name, the other names and the content with each non-empty
    line labelled "L<n>" (1-based line number), and asks one keep/move question per line.
    The request goes to OpenRouter's decisions endpoint once; an HTTP or network error is
    raised (see openrouter_post), and the audit can simply be run again.
    Output: {"n", "text", "move"} for every non-empty line, where "move" is Jev's
    probability that the line should be moved elsewhere.
    """
    lines = [(i + 1, t) for i, t in enumerate(content.splitlines()) if t.strip()]
    state = "\n".join([
        PROMPT["intro"].format(name=name), "",
        PROMPT["others"], *(f"- {other}" for other in other_names), "",
        PROMPT["body"].format(name=name),
        *(f"L{n} {text}" for n, text in lines),
    ])
    choices = {"keep": PROMPT["keep"].format(name=name), "move": PROMPT["move"]}
    questions = {
        f"L{n}": {
            "type": "choice",
            "instructions": PROMPT["question"].format(n=n, name=name),
            "criteria": choices,
        }
        for n, _ in lines
    }
    answers = openrouter_post(DECISIONS_ENDPOINT, {"model": model, "state": state, "questions": questions},
                              timeout=180)["answers"]
    return [{"n": n, "text": t, "move": answers[f"L{n}"]["probabilities"]["move"]}
            for n, t in lines]


def format_report(results: list[dict[str, Any]], preview: int = 40) -> str:
    """Render audit_concepts results as the text printed by audit --scope.

    Input: the results, and how many characters of each flagged line to show.
    Output: one block per node (verdict; for audited nodes the moved UTF-8 bytes and their
    share of the content, each line Jev would move, and the similar nodes when flagged),
    followed by REPORT_FOOTER.
    """
    out: list[str] = []
    for index, result in enumerate(results):
        if index:
            out.append("")
        status = "待检查" if result["needs_review"] else "通过"
        out.append(f"[{status}] [{result['id']}] {result['name']}")
        if result.get("unchanged"):
            out.append("  名字和正文自上次通过后没改过，不送 Jev")
        elif result["skipped"]:
            out.append(f"  正文 {result['total_bytes']} B，不到 {MIN_BYTES} B，不送 Jev，判通过")
        else:
            share = result["moved_bytes"] / result["total_bytes"]
            out.append(f"  该移走的行共 {result['moved_bytes']} B，占正文 {share:.0%}"
                       f"（{result['total_bytes']} B；满 {MIN_BYTES} B 判待检查）")
            for line in result["lines"]:
                if line["move"] > MOVE_THRESHOLD:
                    out.append(f"  → L{line['n']} 移走={line['move']:.2f} | {line['text'][:preview]}")
            if result["needs_review"] and result["others"]:
                out.append("  给 Jev 对照的相近节点：" + "；".join(f"[{i}] {n}" for i, n in result["others"]))
    out.append("")
    out.append(REPORT_FOOTER.format(min_bytes=MIN_BYTES))
    return "\n".join(out)


def record_pass(db, concept_id: int, fingerprint: str) -> None:
    """Store the fingerprint of the name and content that just passed on the concept row.

    Input: an open HoronDB, the node's id, and audit_fingerprint() of what passed.
    Output: none; concepts.scope_audit_fingerprint is set and committed (updated_at is
    left alone, since recording an audit is not an edit of the node).
    """
    with db.conn:
        db.conn.execute("UPDATE concepts SET scope_audit_fingerprint = ? WHERE id = ?",
                        (fingerprint, concept_id))


def audit_concepts(db, concepts: list[str]) -> list[dict[str, Any]]:
    """Run the content-scope audit on the given nodes and record the ones that pass.

    Input: an open HoronDB and node names, aliases or ids (duplicates are audited once).
    For each node, in the order given:
    - name and content unchanged since its last pass (stored fingerprint matches):
      not sent to Jev; result {"id", "name", "unchanged": True, "needs_review": False};
    - non-empty lines under MIN_BYTES bytes: passed without Jev ("skipped": True);
    - otherwise Jev judges every line, shown the NEIGHBOURS most similar node names (name
      embeddings are loaded on the first such node); the node needs review when the lines
      above MOVE_THRESHOLD add up to MIN_BYTES bytes.
    Audited results hold {"id", "name", "total_bytes", "skipped", "others", "lines",
    "moved_bytes", "needs_review"}; "others" is (id, name) of the similar nodes, so the
    report can show where misplaced lines might belong; "lines" is jev_line_verdicts().
    Side effect: every node that passes has its fingerprint stored.
    Output: one result per node, ready for format_report.
    """
    ids = list(dict.fromkeys(db._resolve_id(c) for c in concepts))
    rows = {r["id"]: r for r in db.conn.execute(
        f"SELECT id, name, content, scope_audit_fingerprint FROM concepts "
        f"WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()}
    embeddings = None
    results = []
    for n in (rows[i] for i in ids):
        content = n["content"] or ""
        fingerprint = audit_fingerprint(n["name"], content)
        if n["scope_audit_fingerprint"] == fingerprint:
            results.append({"id": n["id"], "name": n["name"], "unchanged": True, "needs_review": False})
            continue

        total = audit_content_bytes(content)
        result = {"id": n["id"], "name": n["name"], "total_bytes": total, "skipped": total < MIN_BYTES,
                  "others": [], "lines": [], "moved_bytes": 0, "needs_review": False}
        if not result["skipped"]:
            if embeddings is None:
                embeddings = load_name_embeddings(db)
            others = similar_names(n["id"], *embeddings)
            lines = jev_line_verdicts(n["name"], content, [name for _, name in others])
            moved = sum(len(line["text"].encode("utf-8")) for line in lines if line["move"] > MOVE_THRESHOLD)
            result.update(others=others, lines=lines, moved_bytes=moved, needs_review=moved >= MIN_BYTES)

        if not result["needs_review"]:
            record_pass(db, n["id"], fingerprint)
        results.append(result)
    return results


def refresh_audit_coverage(db, tag_expr: str | None = None, preview: int = 10) -> str | None:
    """Pass the short pending nodes, then summarise which nodes still need a content-scope audit, most-read first.

    Input: an open HoronDB; tag_expr limits the check to that tag's members (None = all
    nodes). Node list, stored fingerprints and read counts are queried from db here.
    A node needs review if the fingerprint of its current name and content, computed now,
    differs from concepts.scope_audit_fingerprint (NULL = never passed).
    Side effect: pending nodes under MIN_BYTES are recorded as passed right here, since
    audit_concepts would pass them without asking Jev anyway.
    Output: one line naming the `preview` most-read pending nodes with their read counts,
    plus the total, or None when every node's pass is still current.
    """
    read_counts = db.get_read_counts()
    stored = dict(db.conn.execute("SELECT id, scope_audit_fingerprint FROM concepts").fetchall())
    pending = []
    for node in db.get_all_concepts_overview(tag_expr=tag_expr):
        fingerprint = audit_fingerprint(node["name"], node["content"])
        if stored.get(node["id"]) == fingerprint:
            continue
        if audit_content_bytes(node["content"]) < MIN_BYTES:
            record_pass(db, node["id"], fingerprint)
            continue
        pending.append(node)
    if not pending:
        return None
    pending.sort(key=lambda n: read_counts.get(n["id"], 0), reverse=True)
    shown = "、".join(f"[{n['id']}] {n['name']}（读 {read_counts.get(n['id'], 0)} 次）"
                     for n in pending[:preview])
    return (f"[正文范围审查] 这是节点的健康度检查：名字是找回节点的唯一入口，正文长出了名字覆盖不到的内容，"
            f"那部分就按名字找不回来，读者看到名字产生的预期也会落空。audit --scope 让 Jev 逐行判断正文的每一行"
            f"是否属于节点的名字，给出健康读数。目前有 {len(pending)} 个节点没有当前有效的健康读数"
            f"（从没查过、查出不健康，或查过后正文又改了），其中被 read_concept 读得最多的 "
            f"{min(preview, len(pending))} 个：{shown}")
