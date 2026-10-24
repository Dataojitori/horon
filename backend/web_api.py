"""
Horon 网页前端（web/）用的 REST 接口，做成一个 FastAPI router，谁挂载它谁决定每次请求打开哪个库。

    from backend.web_api import make_router
    app.include_router(make_router(open_db), prefix="/api")

open_db(request, session_id) 是挂载方提供的上下文管理器：按这次请求打开一个 HoronDB 交出来，
用完关闭。鉴权、选哪个库、加锁、计费都在它里面做；它可以抛 HTTPException 拒绝请求。
接口代码在同一个线程里进入它、使用连接、退出它，所以挂载方在里面开 CostMeter 能记到这次请求
里发生的所有付费调用（回滚时重算书腰向量、审查时问 Jev）。

session_id 是前端带来的会话（查询参数，默认 devonly），决定激活状态按哪个会话显示；
不看会话的接口也按它开连接，结果与会话无关。

本地挂载见 backend/server.py；远程挂载见 aisoulkeeper（按登录用户和 AI 打开那个 AI 的库）。
"""
from __future__ import annotations

from collections import Counter
from contextlib import AbstractContextManager
from typing import Callable

from fastapi import APIRouter, HTTPException, Query, Request

from ._db_common import OFFLINE_DEV_SESSION_ID
from .db import HoronDB
from .evaluator import load_active_states

OpenDB = Callable[[Request, str], AbstractContextManager[HoronDB]]


def make_router(open_db: OpenDB) -> APIRouter:
    """返回挂着全部前端接口的 router；open_db 见模块说明。"""
    router = APIRouter()

    @router.get("/sessions")
    def get_sessions(request: Request):
        """网页顶栏的会话下拉菜单：最近活跃的 5 个宿主会话，再加离线会话 devonly 放在最后。

        输出：[{session_id, adapter, last_active_at}]，按最后活跃时间从新到旧。
          adapter 是宿主名（claude-code / codex / antigravity），不知道时为 null。
        """
        with open_db(request, OFFLINE_DEV_SESSION_ID) as db:
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
    def get_graph(request: Request, session_id: str = OFFLINE_DEV_SESSION_ID):
        """Full graph for Galaxy View.

        Returns nodes (concepts) with degree centrality,
        and links (compose_member and inhibition relationships).
        节点和连线的激活状态按 session_id 这个会话显示。
        """
        with open_db(request, session_id) as db:
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
    def search_concepts(request: Request, q: str = Query(..., min_length=1)):
        with open_db(request, OFFLINE_DEV_SESSION_ID) as db:
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
    def get_concept(request: Request, concept_id: int, session_id: str = OFFLINE_DEV_SESSION_ID):
        """Full concept detail for Inspector / Dissection View（激活状态按 session_id 显示）。"""
        with open_db(request, session_id) as db:
            try:
                result = db.read_concept(concept_id)
            except ValueError as e:
                raise HTTPException(status_code=404, detail=str(e))
        return result.model_dump()

    @router.get("/neighborhood/{concept_id}")
    def get_neighborhood(request: Request, concept_id: int, session_id: str = OFFLINE_DEV_SESSION_ID):
        """解剖视图使用的邻域接口（激活状态按 session_id 显示）。"""
        with open_db(request, session_id) as db:
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
    def audit_database(request: Request):
        """触发全库状态与插件审查。"""
        with open_db(request, OFFLINE_DEV_SESSION_ID) as db:
            try:
                return {"report": db.audit_clusters_report()}
            except Exception as e:
                raise HTTPException(status_code=400, detail=str(e))

    @router.get("/reviews")
    def get_reviews(request: Request):
        """获取所有待人工审核的节点快照列表（按节点归总）。"""
        with open_db(request, OFFLINE_DEV_SESSION_ID) as db:
            try:
                return [item.model_dump() for item in db.list_snapshots()]
            except Exception as e:
                raise HTTPException(status_code=400, detail=str(e))

    @router.post("/reviews/{concept_id}/approve")
    def approve_review(request: Request, concept_id: int):
        """同意指定节点的所有修改，清除快照。"""
        with open_db(request, OFFLINE_DEV_SESSION_ID) as db:
            try:
                return db.approve_snapshots(concept_id).model_dump()
            except Exception as e:
                raise HTTPException(status_code=400, detail=str(e))

    @router.post("/reviews/{concept_id}/rollback")
    def rollback_review(request: Request, concept_id: int):
        """回滚指定节点至修改前快照状态（若节点被删除则恢复为 plain 砖块）。"""
        with open_db(request, OFFLINE_DEV_SESSION_ID) as db:
            try:
                return db.rollback_snapshots(concept_id).model_dump()
            except Exception as e:
                raise HTTPException(status_code=400, detail=str(e))

    return router
