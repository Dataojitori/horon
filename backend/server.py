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
    and links (compose_member and inhibition relationships).
    """
    assert db is not None

    with _db_lock:
        concepts = db.get_all_concepts()

        degree: Counter[int] = Counter()
        links: list[dict] = []

        # 1. 组合与链条关系（CHAIN, AND, OR）
        rows = db.conn.execute(
            "SELECT c.id AS parent_id, c.name, c.activation_type, c.is_active, cm.member_concept_id, cm.order_index "
            "FROM compose_members cm "
            "JOIN concepts c ON cm.parent_concept_id = c.id "
            "ORDER BY cm.parent_concept_id, cm.order_index"
        ).fetchall()

        for r in rows:
            pid = r["parent_id"]
            mid = r["member_concept_id"]
            atype = r["activation_type"]
            status = "active" if r["is_active"] else "inactive"

            if atype == "CHAIN":
                kind = "directed"
            elif atype == "OR":
                kind = "or"
            else:
                kind = "undirected"

            links.append({
                "source": mid,
                "target": pid,
                "relation_id": pid,
                "status": status,
                "kind": kind,
            })
            degree[mid] += 1
            degree[pid] += 1

        # 2. 抑制边
        inh_rows = db.conn.execute(
            "SELECT target_concept_id, inhibitor_concept_id FROM inhibitions"
        ).fetchall()
        for r in inh_rows:
            src = r["inhibitor_concept_id"]
            tgt = r["target_concept_id"]
            links.append({
                "source": src,
                "target": tgt,
                "relation_id": tgt,
                "status": "inhibition",
                "kind": "inhibition",
            })
            degree[src] += 1
            degree[tgt] += 1

        # 3. 概念 tags
        tag_rows = db.conn.execute(
            "SELECT concept_id, tag FROM concept_tags ORDER BY concept_id, tag"
        ).fetchall()
        tags_by_cid: dict[int, list[str]] = {}
        for r in tag_rows:
            tags_by_cid.setdefault(r["concept_id"], []).append(r["tag"])

        nodes = []
        for c in concepts:
            cid = c.id
            nodes.append({
                "id": cid,
                "name": c.name,
                "role": c.role,
                "is_active": c.is_active,
                "lifespan": c.lifespan,
                "activation_type": c.activation_type,
                "disclosure": c.disclosure,
                "degree": degree.get(cid, 0),
                "tags": tags_by_cid.get(cid, []),
                "byte_size": c.byte_size,
            })

    return {"nodes": nodes, "links": links}


@app.get("/api/concepts/search")
def search_concepts(q: str = Query(..., min_length=1)):
    assert db is not None
    try:
        with _db_lock:
            results = db.search_concepts(q)
            return [
                {
                    "concept_id": r.concept_id,
                    "concept_name": r.concept_name,
                    "id": r.concept_id,
                    "name": r.concept_name,
                    "matches": [m.model_dump() for m in r.matches],
                }
                for r in results
            ]
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
    """解剖视图使用的邻域接口。"""
    assert db is not None
    try:
        with _db_lock:
            focal = db.read_concept(concept_id)
            parent_rows = db.conn.execute(
                "SELECT DISTINCT c.id, c.name, c.activation_type, c.is_active "
                "FROM compose_members cm "
                "JOIN concepts c ON c.id = cm.parent_concept_id "
                "WHERE cm.member_concept_id = ? "
                "ORDER BY c.id",
                (concept_id,),
            ).fetchall()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    neighbor_ids: set[int] = set()

    for m in focal.members:
        neighbor_ids.add(m.concept_id)
    for inh in focal.inhibitions:
        neighbor_ids.add(inh.inhibitor_concept_id)
    for inh in focal.inhibiting:
        neighbor_ids.add(inh.target_concept_id)
    for parent in parent_rows:
        neighbor_ids.add(parent["id"])

    neighbor_ids.discard(focal.id)

    neighbors = []
    if neighbor_ids:
        placeholders = ",".join("?" * len(neighbor_ids))
        with _db_lock:
            rows = db.conn.execute(
                f"SELECT c.id, c.name, c.disclosure, c.content, "
                f"       (SELECT COUNT(*) FROM compose_members cm "
                f"        WHERE cm.member_concept_id = c.id) AS degree "
                f"FROM concepts c WHERE c.id IN ({placeholders})",
                tuple(neighbor_ids),
            ).fetchall()
        for row in rows:
            nid = row["id"]
            c_str = row["content"]
            neighbors.append({
                "id": nid,
                "name": row["name"],
                "disclosure": row["disclosure"],
                "degree": row["degree"],
                "byte_size": len(c_str.encode("utf-8")) if c_str else 0,
            })

    internal_links = []
    # Note: focal.members are internal sub-elements rendered inside the focal container.
    # They should not be emitted as external links to focal.id to avoid redundant perimeter spokes.
    for parent in parent_rows:
        internal_links.append({
            "source": focal.id,
            "target": parent["id"],
            "variation_code": "",
            "status": "active" if parent["is_active"] else "inactive",
            "kind": "directed" if parent["activation_type"] == "CHAIN" else "undirected",
            "relation_id": parent["id"],
            "relation_name": parent["name"],
        })
    for inh in focal.inhibitions:
        internal_links.append({
            "source": inh.inhibitor_concept_id,
            "target": focal.id,
            "variation_code": "",
            "status": "active",
            "kind": "inhibition",
            "relation_id": focal.id,
            "relation_name": f"{inh.inhibitor_name} ─⊣ {focal.name}",
        })
    for inh in focal.inhibiting:
        internal_links.append({
            "source": focal.id,
            "target": inh.target_concept_id,
            "variation_code": "",
            "status": "active",
            "kind": "inhibition",
            "relation_id": inh.target_concept_id,
            "relation_name": f"{focal.name} ─⊣ {inh.target_name}",
        })

    return {
        "focal": focal.model_dump(),
        "neighbors": neighbors,
        "internal_links": internal_links,
    }

@app.post("/api/audit")
def audit_database():
    """触发全库状态与插件审查。"""
    assert db is not None
    try:
        with _db_lock:
            report = db.audit_clusters_report()
        return {"report": report}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/reviews")
def get_reviews():
    """获取所有待人工审核的节点快照列表（按节点归总）。"""
    assert db is not None
    try:
        with _db_lock:
            items = db.list_snapshots()
        return [item.model_dump() for item in items]
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/reviews/{concept_id}/approve")
def approve_review(concept_id: int):
    """同意指定节点的所有修改，清除快照。"""
    assert db is not None
    try:
        with _db_lock:
            res = db.approve_snapshots(concept_id)
        return res.model_dump()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/reviews/{concept_id}/rollback")
def rollback_review(concept_id: int):
    """回滚指定节点至修改前快照状态（若节点被删除则恢复为 plain 砖块）。"""
    assert db is not None
    try:
        with _db_lock:
            res = db.rollback_snapshots(concept_id)
        return res.model_dump()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

