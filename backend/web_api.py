"""
Horon 网页前端（web/）用的 REST 接口，做成一个 FastAPI router。

    from backend.web_api import make_router
    app.include_router(make_router(db_path_for, remote=False), prefix="/api")

db_path_for(request) 由挂载方提供：这次请求要看哪个库，返回它的路径。鉴权也在它里面做，
不让看就抛 HTTPException。打开、关闭库由这里负责：
    remote=False  本地自己的库：照常打开（加载 Tag 插件）。
    remote=True   服务器上别人的库：打开时不加载插件，改正文前留快照。
库文件不存在时返回 404。

session_id 是前端带来的会话（查询参数，默认 devonly），决定激活状态按哪个会话显示；
不看会话的接口也按它开连接，结果与会话无关。

本地挂载见 backend/server.py；远程挂载见 aisoulkeeper（按登录用户和 AI 找到那个 AI 的库）。
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Callable, Iterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ._db_common import OFFLINE_DEV_SESSION_ID
from .db import HoronDB
from .evaluator import load_active_states


def get_db(
    request: Request,
    session_id: str = Query(OFFLINE_DEV_SESSION_ID),
) -> Iterator[HoronDB]:
    """每个请求的数据库连接依赖：自动根据环境打开库，并在请求结束后关闭。"""
    db_path_for: Callable[[Request], Path] | None = getattr(request.state, "horon_db_path_for", None)
    remote: bool = getattr(request.state, "horon_remote", False)
    if db_path_for is None:
        raise HTTPException(status_code=500, detail="数据库路径解析器未配置。")

    path = db_path_for(request)
    try:
        # FastAPI 把开库、跑接口、关库分三次丢进线程池，不保证同一条线程；
        # 这个连接只属于这一个请求、三步依次执行，不会被并发使用，所以关掉同线程检查。
        db = HoronDB(
            db_path=path,
            session_id=session_id,
            check_same_thread=False,
            snapshot_mode=remote,
            plugins_enabled=not remote,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="记忆库文件不存在。") from None
    try:
        yield db
    finally:
        db.close()


router = APIRouter()


@router.get("/sessions")
def get_sessions(db: HoronDB = Depends(get_db)):
    """网页顶栏的会话下拉菜单：最近活跃的 5 个宿主会话，再加离线会话 devonly 放在最后。

    输出：[{session_id, adapter, last_active_at}]，按最后活跃时间从新到旧。
      adapter 是宿主名（claude-code / codex / antigravity），不知道时为 null。
    """
    rows = db.conn.execute(
        "SELECT session_id, adapter, last_active_at FROM sessions "
        "WHERE session_id != ? ORDER BY last_active_at DESC LIMIT 5",
        (OFFLINE_DEV_SESSION_ID,),
    ).fetchall()
    offline = db.conn.execute(
        "SELECT session_id, adapter, last_active_at FROM sessions WHERE session_id = ?",
        (OFFLINE_DEV_SESSION_ID,),
    ).fetchall()
    return [dict(r) for r in [*rows, *offline]]


@router.get("/graph")
def get_graph(db: HoronDB = Depends(get_db)):
    """Full graph for Galaxy View.

    Returns nodes (concepts) with degree centrality,
    and links (compose_member and inhibition relationships).
    节点和连线的激活状态按 session_id 这个会话显示。
    """
    concepts = db.get_all_concepts()
    states = load_active_states(db.conn, db.session_id)

    degree: Counter[int] = Counter()
    links: list[dict] = []

    # 1. 组合与链条关系（CHAIN, AND, OR）
    rows = db.conn.execute(
        "SELECT c.id AS parent_id, c.name, c.activation_type, cm.member_concept_id, cm.order_index "
        "FROM compose_members cm "
        "JOIN concepts c ON cm.parent_concept_id = c.id "
        "ORDER BY cm.parent_concept_id, cm.order_index"
    ).fetchall()

    for r in rows:
        pid = r["parent_id"]
        mid = r["member_concept_id"]
        atype = r["activation_type"]
        status = "active" if states[pid] else "inactive"

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
            "is_active": states[cid],
            "lifespan": c.lifespan,
            "activation_type": c.activation_type,
            "when_to_read": c.when_to_read,
            "degree": degree.get(cid, 0),
            "tags": tags_by_cid.get(cid, []),
            "byte_size": c.byte_size,
        })

    return {"nodes": nodes, "links": links}


@router.get("/concepts/search")
def search_concepts(q: str = Query(..., min_length=1), db: HoronDB = Depends(get_db)):
    try:
        results = db.search_concepts(q)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
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


@router.get("/concepts/{concept_id}")
def get_concept(concept_id: int, db: HoronDB = Depends(get_db)):
    """Full concept detail for Inspector / Dissection View（激活状态按 session_id 显示）。"""
    try:
        result = db.read_concept(concept_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return result.model_dump()


@router.get("/neighborhood/{concept_id}")
def get_neighborhood(concept_id: int, db: HoronDB = Depends(get_db)):
    """解剖视图使用的邻域接口（激活状态按 session_id 显示）。"""
    try:
        focal = db.read_concept(concept_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    states = load_active_states(db.conn, db.session_id)
    parent_rows = db.conn.execute(
        "SELECT DISTINCT c.id, c.name, c.activation_type "
        "FROM compose_members cm "
        "JOIN concepts c ON c.id = cm.parent_concept_id "
        "WHERE cm.member_concept_id = ? "
        "ORDER BY c.id",
        (concept_id,),
    ).fetchall()

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
        rows = db.conn.execute(
            f"SELECT c.id, c.name, c.when_to_read, c.content, "
            f"       (SELECT COUNT(*) FROM compose_members cm "
            f"        WHERE cm.member_concept_id = c.id) AS degree "
            f"FROM concepts c WHERE c.id IN ({placeholders})",
            tuple(neighbor_ids),
        ).fetchall()
        for row in rows:
            c_str = row["content"]
            neighbors.append({
                "id": row["id"],
                "name": row["name"],
                "when_to_read": row["when_to_read"],
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
            "status": "active" if states[parent["id"]] else "inactive",
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


@router.post("/audit")
def audit_database(db: HoronDB = Depends(get_db)):
    """触发全库状态与插件审查。"""
    try:
        return {"report": db.audit_clusters_report()}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/reviews")
def get_reviews(db: HoronDB = Depends(get_db)):
    """获取所有待人工审核的节点快照列表（按节点归总）。"""
    try:
        return [item.model_dump() for item in db.list_snapshots()]
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/reviews/{concept_id}/approve")
def approve_review(concept_id: int, db: HoronDB = Depends(get_db)):
    """同意指定节点的所有修改，清除快照。"""
    try:
        return db.approve_snapshots(concept_id).model_dump()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/reviews/{concept_id}/rollback")
def rollback_review(concept_id: int, db: HoronDB = Depends(get_db)):
    """回滚指定节点至修改前快照状态（若节点被删除则恢复为 plain 砖块）。"""
    try:
        return db.rollback_snapshots(concept_id).model_dump()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


def make_router(db_path_for: Callable[[Request], Path], *, remote: bool = False) -> APIRouter:
    """返回挂载了前端接口的 router；每次请求会先将 db_path_for 和 remote 存入 request.state。"""

    async def set_db_context(request: Request):
        request.state.horon_db_path_for = db_path_for
        request.state.horon_remote = remote

    wrapper = APIRouter(dependencies=[Depends(set_db_context)])
    wrapper.include_router(router)
    return wrapper
