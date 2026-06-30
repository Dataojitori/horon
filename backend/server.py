"""
Horon API server — thin REST layer on top of HoronDB.
Serves graph data for the visualization frontend.
"""
from __future__ import annotations

import threading
from collections import Counter
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .db import HoronDB
from .models import ReadResult

db: HoronDB | None = None
# 同步 endpoint 跑在 FastAPI 线程池里，共用同一个 sqlite 连接（check_same_thread=False）。
# 用一把锁把每个 endpoint 的 DB 访问串行化，避免并发请求交错使用游标。
_db_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = HoronDB(check_same_thread=False)
    yield
    if db:
        db.close()


app = FastAPI(title="Horon API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/graph")
def get_graph():
    """Full graph for Galaxy View.

    Returns nodes (concepts) with degree centrality,
    and links (compose_member relationships).
    """
    assert db is not None

    with _db_lock:
        concepts = db.get_all_concepts()

        graph = db._load_relation_graph()

        degree: Counter[int] = Counter()
        links: list[dict] = []

        for src, edges in graph.adjacency.items():
            for tgt, edge_cid, edge_sc, status in edges:
                links.append({
                    "source": src,
                    "target": tgt,
                    "relation_id": edge_cid,
                    "status": status,
                    "kind": "directed",
                })
                degree[src] += 1
                degree[tgt] += 1
                degree[edge_cid] += 1

        for members, edge_cid, edge_sc, status in graph.and_groups:
            mems = list(members)
            for i, a in enumerate(mems):
                for b in mems[i + 1:]:
                    links.append({
                        "source": a,
                        "target": b,
                        "relation_id": edge_cid,
                        "status": status,
                        "kind": "undirected",
                    })
                    degree[a] += 1
                    degree[b] += 1
            degree[edge_cid] += len(mems)

        nodes = []
        for c in concepts:
            cid = c.id
            nodes.append({
                "id": cid,
                "name": c.name,
                "disclosure": c.disclosure,
                "degree": degree.get(cid, 0),
            })

    return {"nodes": nodes, "links": links}


@app.get("/api/concepts/search")
def search_concepts(q: str = Query(..., min_length=1)):
    assert db is not None
    try:
        with _db_lock:
            results = db.search_concepts(q)
        return [r.model_dump() for r in results]
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/concepts/{concept_id}")
def get_concept(concept_id: int):
    """Full concept detail for Inspector / Dissection View."""
    assert db is not None
    try:
        with _db_lock:
            result = db.read_concept(concept_id)
        return result.model_dump()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/neighborhood/{concept_id}")
def get_neighborhood(concept_id: int):
    """解剖视图使用的有向邻域。

    圈内展示焦点概念自身 variation 的成员，圈外只展示焦点概念的
    inbound / outbound。`A & B` 是组合概念自身的内部结构，不是
    A 或 B 的入边/出边；要查看它，应打开承载该 variation 的组合概念。
    """
    assert db is not None
    try:
        with _db_lock:
            focal = db.read_concept(concept_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    # 这里只收集有向 inbound / outbound。无序组合 A & B 有意不在
    # 成员 A、B 的页面反向展开，而是在打开组合概念时作为圈内成员展示。
    neighbor_ids: set[int] = set()

    for rel in (focal.inbound_confirmed + focal.inbound_negated +
                focal.inbound_hypotheses):
        neighbor_ids.add(rel.from_concept_id)

    for rel in (focal.outbound_confirmed + focal.outbound_negated +
                focal.outbound_hypotheses):
        neighbor_ids.add(rel.target_concept_id)

    neighbor_ids.discard(focal.id)

    neighbors = []
    if neighbor_ids:
        # 一次性查出所有邻居（含 degree），避免逐个邻居 2 次查询的 N+1。
        placeholders = ",".join("?" * len(neighbor_ids))
        with _db_lock:
            rows = db.conn.execute(
                f"SELECT c.id, c.name, c.disclosure, "
                f"       (SELECT COUNT(*) FROM compose_members cm "
                f"        WHERE cm.member_concept_id = c.id) AS degree "
                f"FROM concepts c WHERE c.id IN ({placeholders})",
                tuple(neighbor_ids),
            ).fetchall()
        for row in rows:
            neighbors.append({
                "id": row["id"],
                "name": row["name"],
                "disclosure": row["disclosure"],
                "degree": row["degree"],
            })

    internal_links = []
    # 按 (方向, 邻居, 关系) 去重：入边与出边各自独立，双向关系 A↔focal 两条
    # 都会保留；同一对概念之间的多条不同关系（如 水→盐 既"溶解"又"导电"）也
    # 各画一条，只折叠掉同一关系的重复行。
    seen_inbound: set[tuple[int, int]] = set()
    seen_outbound: set[tuple[int, int]] = set()

    for rel in (focal.inbound_confirmed + focal.inbound_negated +
                focal.inbound_hypotheses):
        key = (rel.from_concept_id, rel.concept_id)
        if key not in seen_inbound:
            seen_inbound.add(key)
            status = ("confirmed" if rel in focal.inbound_confirmed
                      else "negated" if rel in focal.inbound_negated
                      else "hypothesis")
            internal_links.append({
                "source": rel.from_concept_id,
                "target": focal.id,
                "variation_code": "",
                "status": status,
                "kind": "directed",
                "relation_id": rel.concept_id,
                "relation_name": rel.concept_name,
            })

    for rel in (focal.outbound_confirmed + focal.outbound_negated +
                focal.outbound_hypotheses):
        key = (rel.target_concept_id, rel.concept_id)
        if key not in seen_outbound:
            seen_outbound.add(key)
            status = ("confirmed" if rel in focal.outbound_confirmed
                      else "negated" if rel in focal.outbound_negated
                      else "hypothesis")
            internal_links.append({
                "source": focal.id,
                "target": rel.target_concept_id,
                "variation_code": "",
                "status": status,
                "kind": "directed",
                "relation_id": rel.concept_id,
                "relation_name": rel.concept_name,
            })

    return {
        "focal": focal.model_dump(),
        "neighbors": neighbors,
        "internal_links": internal_links,
    }

@app.post("/api/audit")
def audit_database():
    """触发全库状态审查。"""
    assert db is not None
    try:
        with _db_lock:
            logs = db.audit_status_integrity()
        return {"downgraded_logs": logs}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
