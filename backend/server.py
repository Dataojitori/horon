"""
Horon API server — thin REST layer on top of HoronDB.
Serves graph data for the visualization frontend.
"""
from __future__ import annotations

import threading
from collections import Counter
from contextlib import asynccontextmanager, contextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from ._db_common import (
    DEFAULT_DB_ALIAS,
    OFFLINE_DEV_SESSION_ID,
    DB_REGISTRY,
    resolve_db_path,
)
from .db import HoronDB
from .evaluator import load_active_states

# alias → HoronDB handle (lazy). Each file gets its own connection, its own
# plugin cache (per HoronDB instance) and its own lock: requests against
# different databases never block each other, same-database requests stay
# serialized on the shared sqlite connection like before.
_db_handles: dict[str, HoronDB] = {}
_db_locks: dict[str, threading.Lock] = {}
_registry_lock = threading.Lock()


def _lock_for(alias: str) -> threading.Lock:
    with _registry_lock:
        lock = _db_locks.get(alias)
        if lock is None:
            lock = threading.Lock()
            _db_locks[alias] = lock
        return lock


def get_db(alias: str | None = None) -> HoronDB:
    """Resolve alias (validated against the allowlist) to its open handle."""
    path = resolve_db_path(alias)  # raises ValueError on unknown alias
    key = (alias or "").strip() or DEFAULT_DB_ALIAS
    with _registry_lock:
        handle = _db_handles.get(key)
        if handle is None:
            handle = HoronDB(
                check_same_thread=False,
                session_id=OFFLINE_DEV_SESSION_ID,
                db_path=path,
            )
            _db_handles[key] = handle
            _db_locks.setdefault(key, threading.Lock())
        return handle


db: HoronDB | None = None
# Back-compat for anything importing server.db: points at the default alias.
# 同步 endpoint 跑在 FastAPI 线程池里，共用同一个 sqlite 连接（check_same_thread=False）。
# 用每库一把锁把每个 endpoint 的 DB 访问串行化，避免并发请求交错使用游标。


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    # 默认库句柄预热（建表/打迁移和以前一样发生在启动时）；
    # 其他库惰性打开，第一次被 ?db= 点名时才建文件。
    # 全局连接只服务不看会话的接口（图结构、搜索、审计、快照），会话定为 devonly；
    # 要按会话显示激活状态的接口用 _session_db 另开连接。
    db = get_db()
    yield
    for handle in list(_db_handles.values()):
        handle.close()
    _db_handles.clear()


app = FastAPI(title="Horon API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@contextmanager
def _session_db(session_id: str, alias: str | None = None):
    """按请求带的会话开一个只用于本次请求的连接，用完关闭。
    HoronDB 的会话在创建时定下、之后不变，所以不能拿全局连接去切会话。
    alias 决定连哪个库文件（默认库），和读接口的 ?db= 保持一致。"""
    sdb = HoronDB(session_id=session_id, db_path=resolve_db_path(alias))
    try:
        yield sdb
    finally:
        sdb.close()


def _alias_param(db_alias: str | None = None) -> str:
    """校验 ?db= 别名：未知别名直接 400，绝不把原始路径拼进文件访问。"""
    try:
        resolve_db_path(db_alias)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return (db_alias or "").strip() or DEFAULT_DB_ALIAS


@app.get("/api/databases")
def list_databases():
    """前端顶栏的库选择器：[{alias, default}]。"""
    return [
        {"alias": alias, "default": alias == DEFAULT_DB_ALIAS}
        for alias in sorted(DB_REGISTRY)
    ]


@app.get("/api/sessions")
def get_sessions(db_alias: str | None = Query(default=None, alias="db")):
    """网页顶栏的会话下拉菜单：最近活跃的 5 个宿主会话，再加离线会话 devonly 放在最后。

    输出：[{session_id, adapter, last_active_at}]，按最后活跃时间从新到旧。
      adapter 是宿主名（claude-code / codex / antigravity），不知道时为 null。
    """
    alias = _alias_param(db_alias)
    handle = get_db(alias)
    with _lock_for(alias):
        rows = handle.conn.execute(
            "SELECT session_id, adapter, last_active_at FROM sessions "
            "WHERE session_id != ? ORDER BY last_active_at DESC LIMIT 5",
            (OFFLINE_DEV_SESSION_ID,),
        ).fetchall()
        offline = handle.conn.execute(
            "SELECT session_id, adapter, last_active_at FROM sessions WHERE session_id = ?",
            (OFFLINE_DEV_SESSION_ID,),
        ).fetchall()
    return [dict(r) for r in [*rows, *offline]]


@app.get("/api/graph")
def get_graph(
    session_id: str = OFFLINE_DEV_SESSION_ID,
    db_alias: str | None = Query(default=None, alias="db"),
):
    """Full graph for Galaxy View.

    Returns nodes (concepts) with degree centrality,
    and links (compose_member and inhibition relationships).
    节点和连线的激活状态按 session_id 这个会话显示。
    """
    alias = _alias_param(db_alias)
    handle = get_db(alias)

    with _lock_for(alias):
        concepts = handle.get_all_concepts()
        states = load_active_states(handle.conn, session_id)

        degree: Counter[int] = Counter()
        links: list[dict] = []

        # 1. 组合与链条关系（CHAIN, AND, OR）
        rows = handle.conn.execute(
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
        inh_rows = handle.conn.execute(
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
        tag_rows = handle.conn.execute(
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
                "disclosure": c.disclosure,
                "degree": degree.get(cid, 0),
                "tags": tags_by_cid.get(cid, []),
                "byte_size": c.byte_size,
            })

    return {"nodes": nodes, "links": links}


@app.get("/api/concepts/search")
def search_concepts(
    q: str = Query(..., min_length=1),
    db_alias: str | None = Query(default=None, alias="db"),
):
    alias = _alias_param(db_alias)
    handle = get_db(alias)
    try:
        with _lock_for(alias):
            results = handle.search_concepts(q)
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
def get_concept(
    concept_id: int,
    session_id: str = OFFLINE_DEV_SESSION_ID,
    db_alias: str | None = Query(default=None, alias="db"),
):
    """Full concept detail for Inspector / Dissection View（激活状态按 session_id 显示）。"""
    alias = _alias_param(db_alias)
    try:
        with _lock_for(alias), _session_db(session_id, alias) as sdb:
            result = sdb.read_concept(concept_id)
        return result.model_dump()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/neighborhood/{concept_id}")
def get_neighborhood(
    concept_id: int,
    session_id: str = OFFLINE_DEV_SESSION_ID,
    db_alias: str | None = Query(default=None, alias="db"),
):
    """解剖视图使用的邻域接口（激活状态按 session_id 显示）。"""
    alias = _alias_param(db_alias)
    handle = get_db(alias)
    try:
        with _lock_for(alias), _session_db(session_id, alias) as sdb:
            focal = sdb.read_concept(concept_id)
            states = load_active_states(sdb.conn, sdb.session_id)
            parent_rows = sdb.conn.execute(
                "SELECT DISTINCT c.id, c.name, c.activation_type "
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
        with _lock_for(alias):
            rows = handle.conn.execute(
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

@app.post("/api/audit")
def audit_database(db_alias: str | None = Query(default=None, alias="db")):
    """触发全库状态与插件审查。"""
    alias = _alias_param(db_alias)
    handle = get_db(alias)
    try:
        with _lock_for(alias):
            report = handle.audit_clusters_report()
        return {"report": report}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/reviews")
def get_reviews(db_alias: str | None = Query(default=None, alias="db")):
    """获取所有待人工审核的节点快照列表（按节点归总）。"""
    alias = _alias_param(db_alias)
    handle = get_db(alias)
    try:
        with _lock_for(alias):
            items = handle.list_snapshots()
        return [item.model_dump() for item in items]
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/reviews/{concept_id}/approve")
def approve_review(concept_id: int, db_alias: str | None = Query(default=None, alias="db")):
    """同意指定节点的所有修改，清除快照。"""
    alias = _alias_param(db_alias)
    handle = get_db(alias)
    try:
        with _lock_for(alias):
            res = handle.approve_snapshots(concept_id)
        return res.model_dump()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/reviews/{concept_id}/rollback")
def rollback_review(concept_id: int, db_alias: str | None = Query(default=None, alias="db")):
    """回滚指定节点至修改前快照状态（若节点被删除则恢复为 plain 砖块）。"""
    alias = _alias_param(db_alias)
    handle = get_db(alias)
    try:
        with _lock_for(alias):
            res = handle.rollback_snapshots(concept_id)
        return res.model_dump()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

