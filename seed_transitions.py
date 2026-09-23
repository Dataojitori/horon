#!/usr/bin/env python3
"""用 MCP 的结构关系给 Horon 的 concept_transitions 灌一批初始先验。

为什么需要它：
  Horon 的跳转推荐（read_concept 底部的 suggestion）完全靠 concept_transitions
  的历史 read 记录喂养。记忆刚从 MCP 迁进来，这张表几乎是空的 —— 601 个节点零连接，
  于是推荐永远推不出东西，而"推不出东西"本身又让人不去读，读记录更长不出来。
  这个脚本把 MCP 里已经存在的四种关系换算成**先验权重**灌进去，把冷启动顶过去。

灌的是先验，不是伪造的观测：
  真实一次共读产生的权重是 -log2(P')，实测落在 4.3 ~ 6.2。本脚本的种子权重上限
  1.6，**低于任何一次真实观测**，所以真读过一次的边永远排在种子前面。
  种子边一旦真的被走过，record_transition 会把它抬到 4+ 转正；没人走就随 0.95
  衰减慢慢被剪掉。两个方向都是对的。

四种关系与权重（同一对取最大值，不叠加）：
  1. 父 → 子   1.0 ~ 1.5，按 MCP 边上的 priority 给（priority 越小越靠前）
  2. 子 → 父   0.8，回溯比下钻少
  3. 正文里写了对方的 URI   1.6，这是显式的"去看 X"，最强
  4. 正文命中对方的 glossary 触发词   1.4 / (1 + log2(df))，df = 命中该词的节点数。
     "RAG" 这种 156 个节点都命中的词几乎不携带信息，按频次压到接近 0。

输入：
  - MCP 库（只读）：edges / paths / memories / glossary_keywords
  - memory_migration_ledger.json：node_uuid -> horon_id 的唯一映射，只认 status=migrated
  - Horon 库：读现有 concept_transitions，判断哪些对已经有真实记录
输出：
  - 默认 dry-run，只打印将要写入的统计与样例，一个字节都不落库
  - 带 --apply 才真正写入；已存在的 (from,to) 一律跳过，绝不覆盖真实读记录
  - 所有种子行的 last_accessed_at 统一写成哨兵值 SEED_STAMP，便于识别与整批撤销
  - --undo 删除所有仍带哨兵时间戳的行（被真实读取更新过的行时间戳已变，不会误删）

用法：
  python seed_transitions.py                 # dry-run，看要灌什么
  python seed_transitions.py --details       # additionally 打印每个来源的样例边
  python seed_transitions.py --apply         # 实写
  python seed_transitions.py --undo --apply  # 撤销未被真实读取碰过的种子
"""

from __future__ import annotations

import argparse
import collections
import math
import re
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

import memory_migration as M  # noqa: E402  复用它的只读连接、台账校验与 URI 解析

# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------

# 所有种子行共用的 last_accessed_at。真实读取会在 ON CONFLICT 时改写这个字段，
# 所以"仍带哨兵值"＝"这条边至今没被真正走过"，--undo 依此判断。
SEED_STAMP = "1970-01-01T00:00:00"

W_PARENT_TO_CHILD = {0: 1.5, 1: 1.5, 2: 1.2}   # 按 priority 取，缺省见下
W_PARENT_TO_CHILD_DEFAULT = 1.0
W_CHILD_TO_PARENT = 0.8
W_URI_REFERENCE = 1.6
W_GLOSSARY_BASE = 1.4

# 低于这个值不值得占推荐位；Horon 的剪枝线是 0.05，留一点余量
MIN_SEED_WEIGHT = 0.15
# 每个来源节点最多灌多少条出边。推荐列表只显示 10 条，灌更多只是把噪声塞进 w_total
MAX_OUT_DEGREE = 10

URI_RE = re.compile(r"\b([a-z]+)://([A-Za-z0-9_\-/]+)")


# ---------------------------------------------------------------------------
# 收集四种关系
# ---------------------------------------------------------------------------

def load_uuid_to_horon() -> dict[str, int]:
    """台账里 status=migrated 的 node_uuid -> horon_id 映射。

    输入：memory_migration_ledger.json（由 memory_migration.load_ledger 校验结构）。
    行为：只认 migrated；ignored 的节点在 Horon 里根本没有对应概念，必须排除，
          否则下面会拿 None 去建外键。
    输出：uuid -> horon concept id。
    """
    ledger = M.load_ledger()
    return {
        uuid: entry["horon_id"]
        for uuid, entry in ledger.items()
        if entry.get("status") == "migrated" and entry.get("horon_id") is not None
    }


def collect_edges(nm_conn, u2h: dict[str, int]) -> dict[tuple[int, int], list[tuple[float, str]]]:
    """把 MCP 的四种关系换算成 (from_horon_id, to_horon_id) -> [(权重, 来源标签)]。

    输入：MCP 只读连接、uuid->horon_id 映射。
    行为：四种关系各扫一遍，两端都必须在映射里，且不生成自环。同一对可能被多种
          关系命中，这里全都留着，由调用方取最大值 —— 取最大而不是求和，是因为
          "既是父子又互相引用"并不比"只是父子"多出一次真实的阅读。
    输出：pair -> 该对被哪些关系以多大权重支持的列表。
    """
    out: dict[tuple[int, int], list[tuple[float, str]]] = collections.defaultdict(list)

    def add(src_uuid: str, dst_uuid: str, weight: float, source: str) -> None:
        if src_uuid == dst_uuid:
            return
        a, b = u2h.get(src_uuid), u2h.get(dst_uuid)
        if a is None or b is None or a == b:
            return
        if weight < MIN_SEED_WEIGHT:
            return
        out[(a, b)].append((weight, source))

    # ── 1 & 2：父子边（双向，权重不同）
    rows = nm_conn.execute(
        "SELECT parent_uuid, child_uuid, priority FROM edges WHERE parent_uuid IS NOT NULL"
    ).fetchall()
    for r in rows:
        pri = r["priority"] if r["priority"] is not None else 99
        add(r["parent_uuid"], r["child_uuid"],
            W_PARENT_TO_CHILD.get(pri, W_PARENT_TO_CHILD_DEFAULT), "parent->child")
        add(r["child_uuid"], r["parent_uuid"], W_CHILD_TO_PARENT, "child->parent")

    # ── 正文只取当前生效版本
    memories = nm_conn.execute(
        "SELECT node_uuid, content FROM memories WHERE deprecated = 0 AND node_uuid IS NOT NULL"
    ).fetchall()
    contents = [(r["node_uuid"], r["content"] or "") for r in memories if r["node_uuid"] in u2h]

    # ── 3：正文里显式写出的 URI
    path2uuid: dict[str, str] = {}
    for r in nm_conn.execute(
        "SELECT domain, path, node_uuid FROM paths WHERE namespace = ?", (M.NAMESPACE,)
    ):
        path2uuid[f"{r['domain']}://{r['path']}"] = r["node_uuid"]

    for src, content in contents:
        for m in URI_RE.finditer(content):
            uri = f"{m.group(1)}://{m.group(2)}".rstrip("/")
            tgt = path2uuid.get(uri)
            if tgt:
                add(src, tgt, W_URI_REFERENCE, "uri-ref")

    # ── 4：glossary 触发词。df 先整体统计一遍，再按 1/(1+log2(df)) 压频繁词
    glossary = [
        (r["keyword"], r["node_uuid"])
        for r in nm_conn.execute("SELECT keyword, node_uuid FROM glossary_keywords")
        if r["node_uuid"] in u2h
    ]
    hits: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)  # keyword -> [(src, tgt)]
    for src, content in contents:
        for kw, tgt in glossary:
            if tgt != src and kw in content:
                hits[kw].append((src, tgt))
    for kw, pairs in hits.items():
        df = len({src for src, _ in pairs})
        weight = W_GLOSSARY_BASE / (1.0 + math.log2(df)) if df > 1 else W_GLOSSARY_BASE
        for src, tgt in pairs:
            add(src, tgt, weight, "glossary")

    return out


def resolve_and_cap(
    raw: dict[tuple[int, int], list[tuple[float, str]]]
) -> tuple[dict[tuple[int, int], tuple[float, str]], int]:
    """同一对取最高权重的那条关系，再按来源节点截断出边数。

    输入：collect_edges 的原始多重映射。
    行为：先取 max（理由见 collect_edges），再对每个 from 只留权重最高的
          MAX_OUT_DEGREE 条。不截断的话，被热门 glossary 词串起来的节点会挂上
          几十条弱边，既挤掉推荐位，又把 w_total 抬高、扭曲后续真实边的 surprise。
    输出：(pair -> (权重, 胜出的来源标签), 被截掉的条数)。
    """
    best = {pair: max(cands, key=lambda c: c[0]) for pair, cands in raw.items()}

    by_src: dict[int, list[tuple[int, float, str]]] = collections.defaultdict(list)
    for (a, b), (w, s) in best.items():
        by_src[a].append((b, w, s))

    capped: dict[tuple[int, int], tuple[float, str]] = {}
    dropped = 0
    for a, targets in by_src.items():
        targets.sort(key=lambda t: (-t[1], t[0]))
        for b, w, s in targets[:MAX_OUT_DEGREE]:
            capped[(a, b)] = (w, s)
        dropped += max(0, len(targets) - MAX_OUT_DEGREE)
    return capped, dropped


# ---------------------------------------------------------------------------
# 写入 / 撤销
# ---------------------------------------------------------------------------

def open_horon_db():
    """打开 Horon 库。

    行为：走 backend.HoronDB 而不是裸 sqlite3.connect —— 它负责 PRAGMA foreign_keys、
          schema 迁移与系统 Tag 初始化。concept_transitions 没有 FTS／向量／插件挂钩，
          但绕过这层拿到的连接不保证外键打开，种子写歪了会留下悬空引用。
    输出：HoronDB 实例（调用方负责 .conn.commit() 与 .conn.close()）。
    """
    from backend.db import HoronDB
    return HoronDB()


def cmd_seed(args: argparse.Namespace) -> None:
    u2h = load_uuid_to_horon()
    with M.get_nm_conn() as nm:
        raw = collect_edges(nm, u2h)
    seeds, dropped = resolve_and_cap(raw)

    db = open_horon_db()
    try:
        existing = {
            (r["from_concept_id"], r["to_concept_id"])
            for r in db.conn.execute(
                "SELECT from_concept_id, to_concept_id FROM concept_transitions"
            )
        }
        live_ids = {r["id"] for r in db.conn.execute("SELECT id FROM concepts")}

        to_write: list[tuple[int, int, float, str]] = []
        skipped_existing = 0
        skipped_missing = 0
        for (a, b), (w, s) in sorted(seeds.items()):
            if (a, b) in existing:
                skipped_existing += 1
                continue
            if a not in live_ids or b not in live_ids:
                # 台账指向的概念被删掉了；这属于台账与 Horon 不同步，交给 memory_migration scan 报
                skipped_missing += 1
                continue
            to_write.append((a, b, w, s))

        by_source = collections.Counter(s for *_, s in to_write)
        print(f"台账映射: {len(u2h)} 个 MCP 实体 -> Horon 概念")
        print(f"候选边: {len(raw)} 对 -> 去重取最大后 {len(seeds)} 对（出度截断丢弃 {dropped} 条）")
        print(f"跳过: 已有真实/既存记录 {skipped_existing} 条 | 概念已不存在 {skipped_missing} 条")
        print(f"将写入: {M.bold(str(len(to_write)))} 条，按来源: {dict(by_source)}")
        if to_write:
            ws = [w for *_, w, _ in to_write]
            print(f"权重范围: {min(ws):.3f} ~ {max(ws):.3f}（一次真实共读约 4.3+，种子永远排在其后）")

        if args.details:
            print("\n样例（每种来源各 5 条）:")
            names = {r["id"]: r["name"] for r in db.conn.execute("SELECT id, name FROM concepts")}
            shown: collections.Counter = collections.Counter()
            for a, b, w, s in sorted(to_write, key=lambda t: -t[2]):
                if shown[s] >= 5:
                    continue
                shown[s] += 1
                print(f"  [{s:13s} w={w:.3f}] {names.get(a, a)} -> {names.get(b, b)}")

        if not args.apply:
            print(M.yellow("\ndry-run：没有写入任何东西。确认无误后加 --apply。"))
            return

        db.conn.executemany(
            "INSERT INTO concept_transitions "
            "(from_concept_id, to_concept_id, weight, last_accessed_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (from_concept_id, to_concept_id) DO NOTHING",
            [(a, b, w, SEED_STAMP) for a, b, w, _ in to_write],
        )
        db.conn.commit()
        print(M.green(f"\n已写入 {len(to_write)} 条种子边，哨兵时间戳 {SEED_STAMP}。"))
        print(M.dim("撤销（只删至今没被真正走过的）: python seed_transitions.py --undo --apply"))
    finally:
        db.conn.close()


def cmd_undo(args: argparse.Namespace) -> None:
    db = open_horon_db()
    try:
        row = db.conn.execute(
            "SELECT COUNT(*) AS n FROM concept_transitions WHERE last_accessed_at = ?",
            (SEED_STAMP,),
        ).fetchone()
        total = db.conn.execute("SELECT COUNT(*) AS n FROM concept_transitions").fetchone()["n"]
        print(f"带哨兵时间戳（未被真实读取碰过）的种子边: {row['n']} / 全表 {total}")
        if not args.apply:
            print(M.yellow("dry-run：没有删除任何东西。确认后加 --apply。"))
            return
        db.conn.execute(
            "DELETE FROM concept_transitions WHERE last_accessed_at = ?", (SEED_STAMP,)
        )
        db.conn.commit()
        print(M.green(f"已删除 {row['n']} 条种子边。被真实读取更新过的边保持不动。"))
    finally:
        db.conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="seed_transitions.py",
        description="用 MCP 的结构关系给 Horon 的跳转推荐灌初始先验（默认 dry-run）",
        allow_abbrev=False,
    )
    parser.add_argument("--apply", action="store_true", help="真正写入/删除；不加则只做 dry-run")
    parser.add_argument("--details", action="store_true", help="打印每种来源的样例边")
    parser.add_argument("--undo", action="store_true", help="删除所有仍带哨兵时间戳的种子边")
    args = parser.parse_args()

    try:
        if args.undo:
            cmd_undo(args)
        else:
            cmd_seed(args)
    except Exception as e:
        print(M.red(f"执行出错: {e}"), file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
