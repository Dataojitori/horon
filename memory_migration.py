#!/usr/bin/env python3
"""Horon 记忆平稳迁移与审计台账工具 (Nocturne Memory -> Horon)

定位与纪律：
  1. 实体锚定：以 MCP 的 node_uuid 作为跨系统实体的不可变主键。
  2. 动态查径：台账中不冗余抄录 path，扫描时当场查表获取该实体的所有对等接入路径（无主副之分）。
  3. 单向演进：Horon 是前向主干，MCP 是正在退役的存量源；绝不反向回写 MCP。
  4. 版本比对：以 memories.id（整数版本号）的精确匹配判定版本变动（捕捉新版更新或异常回滚），免除时间戳漂移误判。
  5. 迁移方针：通用 `note` 字段完整记录迁移删减、拆分合并逻辑或忽略理由，为后续同步提供依据。
  6. 明确指认：所有关联与版本确认均显式指认 Horon 概念，杜绝含混的隐式快捷开关。
  7. 不猜：所有命令只接受完整 URI 或完整 node_uuid，不做模糊匹配。使用者是能一次贴全
     地址的程序，省几个字母换来的是"返回值到底是查到的还是猜到的"无法分辨。

用法：
  python memory_migration.py scan                                   # 全盘审计：进度统计、MCP变动警报、未迁移清单
  python memory_migration.py scan --prefix core://salem             # 限定前缀范围扫描
  python memory_migration.py scan --stat-only                       # 仅输出概括统计
  python memory_migration.py show <UUID/URI/Horon名/ID>             # 查看单节点详情与历史迁移方针
  python memory_migration.py diff <UUID/URI/Horon名/ID>             # 查看 MCP 与 Horon 当前正文差异
  python memory_migration.py update <UUID/URI> --horon <名/ID> [--note "迁移方针"] # 首次迁入、改绑概念、或确认新版后追平版本号
  python memory_migration.py update <UUID/URI> --status ignored --note "忽略理由"   # 标记为忽略
  python memory_migration.py update <UUID/URI> --note "更新方针"    # 仅修改备注/方针
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import difflib
import json
import os
import re
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 环境与路径
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent.resolve()

def _load_env_fallback():
    """读取本仓库 .env 中的配置（如果存在）"""
    env_file = SCRIPT_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            if k not in os.environ:
                os.environ[k] = v

_load_env_fallback()

NM_DB_PATH = Path(os.environ.get(
    "NOCTURNE_MEMORY_DB",
    os.environ.get("NOCTURNE_DB", r"C:\Users\niwatori\OneDrive\code\nocturne_memory\nocturne_memory.db")
))

_raw_horon_db = Path(os.environ.get(
    "HORON_DB",
    str(SCRIPT_DIR / "horon.db")
))
HORON_DB_PATH = _raw_horon_db if _raw_horon_db.is_absolute() else (SCRIPT_DIR / _raw_horon_db)

LEDGER_PATH = SCRIPT_DIR / "memory_migration_ledger.json"
NAMESPACE = os.environ.get("NOCTURNE_NAMESPACE", "")

# ---------------------------------------------------------------------------
# 终端格式化与色彩
# ---------------------------------------------------------------------------
USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ

def _c(text: str, color_code: str) -> str:
    return f"\033[{color_code}m{text}\033[0m" if USE_COLOR else text

def red(t: str) -> str: return _c(t, "31")
def green(t: str) -> str: return _c(t, "32")
def yellow(t: str) -> str: return _c(t, "33")
def blue(t: str) -> str: return _c(t, "34")
def magenta(t: str) -> str: return _c(t, "35")
def cyan(t: str) -> str: return _c(t, "36")
def bold(t: str) -> str: return _c(t, "1")
def dim(t: str) -> str: return _c(t, "2")

# ---------------------------------------------------------------------------
# 数据库连接 (只读模式，严禁直写 SQLite)
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def get_nm_conn() -> Iterator[sqlite3.Connection]:
    if not NM_DB_PATH.exists():
        raise FileNotFoundError(f"Nocturne Memory 数据库未找到: {NM_DB_PATH}")
    conn = sqlite3.connect(f"{NM_DB_PATH.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

@contextlib.contextmanager
def get_horon_conn() -> Iterator[sqlite3.Connection]:
    if not HORON_DB_PATH.exists():
        raise FileNotFoundError(f"Horon 数据库未找到: {HORON_DB_PATH}")
    conn = sqlite3.connect(f"{HORON_DB_PATH.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# 台账读写 (以 node_uuid 为根键的扁平 JSON)
# ---------------------------------------------------------------------------
ALLOWED_STATUS = ("migrated", "ignored")

def load_ledger() -> dict[str, dict[str, Any]]:
    """读台账，并逐条校验结构。

    输入：LEDGER_PATH 指向的 JSON 文件，形如 {node_uuid: {status, horon_id, ...}}。
    行为：台账是我判断"哪条还没迁"的唯一依据。一条结构坏掉的记录不会报错，
          它会让对应实体从所有清单里**静默消失**——既不计入已迁移／已忽略，
          也不进待迁移清单（因为 ledger 里有键），我扫一眼会以为它处理过了。
          所以宁可在入口处硬失败，也不让带病的条目往下走。校验三件事：
          每条是对象、status 取值合法、migrated 必须带 horon_id。
    输出：uuid -> 条目 的字典。文件不存在或为空返回 {}；结构不合法抛 RuntimeError，
          错误信息里带上是哪个 uuid、坏在哪。
    """
    if not LEDGER_PATH.exists():
        return {}
    content = LEDGER_PATH.read_text(encoding="utf-8-sig").strip()
    if not content:
        return {}
    try:
        data = json.loads(content)
    except Exception as e:
        raise RuntimeError(f"台账 JSON 解析失败 ({LEDGER_PATH}): {e}") from e

    if not isinstance(data, dict):
        raise RuntimeError(f"台账根节点必须是对象，实际是 {type(data).__name__} ({LEDGER_PATH})")

    for uuid, entry in data.items():
        where = f"台账条目 {uuid} ({LEDGER_PATH})"
        if not isinstance(entry, dict):
            raise RuntimeError(f"{where}: 必须是对象，实际是 {type(entry).__name__}")
        status = entry.get("status")
        if status not in ALLOWED_STATUS:
            raise RuntimeError(f"{where}: status={status!r} 不是合法取值，只能是 {ALLOWED_STATUS} 之一")
        if status == "migrated" and entry.get("horon_id") is None:
            raise RuntimeError(f"{where}: status=migrated 却没有 horon_id，无法确定它迁到了哪个 Horon 概念")
    return data

def save_ledger(ledger: dict[str, dict[str, Any]]) -> None:
    # 严格以 UUID 排序，保证 git diff 稳定干净
    sorted_ledger = {k: ledger[k] for k in sorted(ledger.keys())}
    temp_file = LEDGER_PATH.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(sorted_ledger, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )
    temp_file.replace(LEDGER_PATH)

# ---------------------------------------------------------------------------
# 实体与 URI 解析及文本规范化辅助函数
# ---------------------------------------------------------------------------
UUID_REGEX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

def normalize_text(text: str | None) -> str:
    """规范化换行符并去除首尾空白，消除跨平台 \r\n / \r 差异与末尾空行差异"""
    if not text:
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()

def parse_uri(uri: str) -> tuple[str, str]:
    """解析 domain://path 格式"""
    if "://" not in uri:
        return "core", uri.strip("/")
    domain, path = uri.split("://", 1)
    return domain.strip(), path.strip("/")

def resolve_node_uuid(nm_conn: sqlite3.Connection, query: str) -> str | None:
    """把一串输入解析成 MCP 的 node_uuid。

    输入：完整的 node_uuid，或完整的 URI（`domain://path`；不写 `://` 时按 core 域处理，
          `writer://` 这种只有域名的写法对应根节点，其 path 存为空串）。
    行为：只做精确匹配。**不做模糊猜测**——猜中的和查中的会走同一个 return，调用方
          拿到 uuid 后无从分辨，于是 update 之类的写入命令没有任何办法拒绝一个猜出来的
          目标。使用者是能一次贴全地址的程序，省几个字母不值得让返回值变得无法预判。
    输出：命中返回 node_uuid 字符串；没命中返回 None。任何情况下都不抛异常。
    """
    query = query.strip()
    if not query:
        return None

    if UUID_REGEX.match(query):
        query_lower = query.lower()
        # 校验该 UUID 在 MCP 存量中是否真实存在 (检查 paths 或 memories)
        row = nm_conn.execute(
            "SELECT node_uuid FROM paths WHERE LOWER(node_uuid) = ? AND namespace = ? "
            "UNION SELECT node_uuid FROM memories WHERE LOWER(node_uuid) = ? LIMIT 1",
            (query_lower, NAMESPACE, query_lower)
        ).fetchone()
        return row["node_uuid"] if row else None

    domain, path = parse_uri(query)
    row = nm_conn.execute(
        "SELECT node_uuid FROM paths WHERE domain = ? AND path = ? AND namespace = ?",
        (domain, path, NAMESPACE)
    ).fetchone()
    return row["node_uuid"] if row else None

def get_node_paths(nm_conn: sqlite3.Connection, node_uuid: str) -> list[str]:
    """获取某个 UUID 关联的所有等价路径"""
    rows = nm_conn.execute(
        "SELECT domain || '://' || COALESCE(path, '') AS uri FROM paths "
        "WHERE node_uuid = ? AND namespace = ? "
        "ORDER BY LENGTH(COALESCE(path, '')) ASC, path ASC",
        (node_uuid, NAMESPACE)
    ).fetchall()
    return [r["uri"] for r in rows]

def get_mcp_active_memory(nm_conn: sqlite3.Connection, node_uuid: str) -> sqlite3.Row | None:
    """取这个节点当前生效的那一版正文。

    输入：node_uuid。
    行为：`deprecated = 0` 的那一行就是当前版。唯一性由库自己保证——
          `CREATE UNIQUE INDEX idx_unique_active_memory ON memories(node_uuid)
           WHERE deprecated = 0 AND node_uuid IS NOT NULL`
          所以这里不需要排序、不需要防重，fetchone 拿到的就是唯一那行。
          **不拿 id 大小判断新旧**：库里确实有这样的节点，它生效的那一行底下还压着
          id 更大的废弃行（核过，十几个），照 id 挑会静默拿到早就作废的正文。
          条件必须写成 `deprecated = 0` 而不是 `COALESCE(deprecated, 0) = 0`——
          后者不匹配那个部分索引的条件，会退化成全表扫描（实测：SCAN vs COVERING INDEX）。
    输出：命中返回该行（id / title / content / created_at）；该节点没有生效版本返回 None。
    """
    return nm_conn.execute(
        "SELECT id, title, content, created_at FROM memories "
        "WHERE node_uuid = ? AND deprecated = 0",
        (node_uuid,)
    ).fetchone()

def resolve_horon_concept(h_conn: sqlite3.Connection, query: str) -> sqlite3.Row | None:
    """根据 ID 或名称在 Horon 中查找概念"""
    query = query.strip()
    if query.isdigit():
        row = h_conn.execute(
            "SELECT id, name, content, disclosure, role, updated_at FROM concepts WHERE id = ?",
            (int(query),)
        ).fetchone()
        if row:
            return row

    row = h_conn.execute(
        "SELECT id, name, content, disclosure, role, updated_at FROM concepts WHERE name = ?",
        (query,)
    ).fetchone()
    if row:
        return row

    alias_row = h_conn.execute(
        "SELECT c.id, c.name, c.content, c.disclosure, c.role, c.updated_at "
        "FROM aliases a JOIN concepts c ON a.concept_id = c.id WHERE a.alias = ?",
        (query,)
    ).fetchone()
    if alias_row:
        return alias_row

    return None

# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------

ALERT_LIMIT = 20  # 每类警报明细最多展开多少条，避免台账变大后刷屏

def _print_omitted(total: int, shown: int) -> None:
    """列表被截断时补一行说明，免得我以为只有这么多。"""
    if total > shown:
        print(dim(f"    ... 其余 {total - shown} 条未展开。"))


def _prefix_hit(uri: str, norm_prefix: str, domain: str | None) -> bool:
    """判断一条完整 URI 是否落在 --prefix 指定的范围内。

    输入：uri = 形如 `core://salem/xxx` 的完整路径；
          norm_prefix = 规范化后的前缀（写了 `://` 就是完整前缀，没写就只是 path 部分）；
          domain = --domain 的取值，没指定则为 None。
    行为：按层级边界判定，`core://salem` 命中 `core://salem` 与 `core://salem/...`，
          但不会命中 `core://salemx`。前缀以 `://` 结尾（如 `core://`）表示整个域。
    输出：命中返回 True，否则 False。
    """
    if "://" in norm_prefix:
        expected = norm_prefix
    elif domain:
        expected = f"{domain}://{norm_prefix}"
    else:
        # 没指定域：只比对 path 部分，任意域下的同名路径都算命中
        p_path = parse_uri(uri)[1]
        return p_path == norm_prefix or p_path.startswith(norm_prefix + "/")

    if expected.endswith("://"):
        return uri.startswith(expected)
    return uri == expected or uri.startswith(expected + "/")


def cmd_scan(args: argparse.Namespace) -> None:
    """全盘扫描：统计迁移进度，识别未迁移路径与 MCP 侧变动"""
    with get_nm_conn() as nm_conn, get_horon_conn() as h_conn:
        ledger = load_ledger()

        # 取出所有"还有生效正文"的路径。条件必须写成 m.deprecated = 0：
        # 写成 COALESCE(m.deprecated, 0) = 0 会不匹配 idx_unique_active_memory 的索引条件，
        # 于是 SQLite 为 paths 的每一行重扫一遍 memories 全表，实测 129 秒；
        # 写成 m.deprecated = 0 走覆盖索引，实测 0.003 秒。库里 deprecated 没有 NULL，
        # 字段本身还带 DEFAULT 0 —— COALESCE 防的是不存在的情况，代价是全表扫描。
        # 不需要 DISTINCT：一个 node_uuid 只有一行生效正文，JOIN 不会放大行数。
        raw_rows = nm_conn.execute(
            """
            SELECT p.node_uuid,
                   p.domain || '://' || COALESCE(p.path, '') AS uri
            FROM paths p
            JOIN memories m ON m.node_uuid = p.node_uuid AND m.deprecated = 0
            WHERE p.namespace = ?
            ORDER BY p.node_uuid, LENGTH(COALESCE(p.path, '')) ASC, p.path ASC
            """,
            (NAMESPACE,)
        ).fetchall()

        uuid_to_paths: dict[str, list[str]] = {}
        for r in raw_rows:
            uuid_to_paths.setdefault(r["node_uuid"], []).append(r["uri"])

        # 作用域过滤（前缀与域名），按层级边界精确判定
        norm_prefix = None
        if args.prefix:
            p_strip = args.prefix.strip()
            if "://" in p_strip:
                scheme, rest = p_strip.split("://", 1)
                rest = rest.strip("/")
                norm_prefix = f"{scheme}://{rest}" if rest else f"{scheme}://"
            else:
                norm_prefix = p_strip.strip("/")

        # 一个实体可能挂在多条等价路径下（别名）。筛选时只要有一条命中即保留，
        # 但列表里要显示的是**命中筛选条件的那一条**，否则会出现
        # `--prefix core://salem` 却列出 core://kurou/... 的观感（其实筛选是对的）。
        filtered_nodes: dict[str, list[str]] = {}
        display_path: dict[str, str] = {}
        for uuid, paths in uuid_to_paths.items():
            in_domain = [p for p in paths if p.startswith(f"{args.domain}://")] if args.domain else paths
            if args.domain and not in_domain:
                continue
            hit = in_domain[0] if in_domain else paths[0]
            if norm_prefix:
                matched = next((p for p in paths if _prefix_hit(p, norm_prefix, args.domain)), None)
                if matched is None:
                    continue
                hit = matched
            filtered_nodes[uuid] = paths
            display_path[uuid] = hit

        total_nodes = len(filtered_nodes)
        migrated_count = 0
        ignored_count = 0
        pending_nodes: list[dict[str, Any]] = []
        migrated_entries: list[tuple[str, list[str], dict[str, Any]]] = []

        for uuid, paths in filtered_nodes.items():
            entry = ledger.get(uuid)
            if not entry:
                pending_nodes.append({
                    "uuid": uuid,
                    "paths": paths,
                    "display": display_path[uuid]
                })
                continue

            status = entry.get("status")
            if status == "ignored":
                ignored_count += 1
            elif status == "migrated":
                migrated_count += 1
                migrated_entries.append((uuid, paths, entry))

        pending_count = len(pending_nodes)

        # 每个实体必须正好落进三类中的一类。对不上说明有实体没被归类（例如将来加了第四种
        # 状态却忘了改这里），而"漏掉的东西不出现在任何清单里"正是台账最危险的失效方式。
        if migrated_count + ignored_count + pending_count != total_nodes:
            print(bold(red(
                f"[!] 内部统计对不上: 已迁移 {migrated_count} + 已忽略 {ignored_count} + "
                f"待迁移 {pending_count} != 总数 {total_nodes}，有实体没被归类，下面的清单不完整。"
            )), file=sys.stderr)

        # 下面两项只在全盘扫描时算：加了 --domain/--prefix 时手上只有局部视野。
        #
        # 其一，还有正文、但一条路径都没有的节点（路径被剪断、本体还在的孤儿）。
        # 上面所有统计都从 paths 出发，这些节点不在 paths 里 => 整个工具看不见它们。
        # 不报出来的话，我照着"待迁移 0"收工，实际漏掉的内容不会有任何提示。
        orphan_all: list[sqlite3.Row] = []
        if not args.domain and not args.prefix:
            orphan_all = nm_conn.execute(
                """
                SELECT m.id, m.node_uuid, m.title, m.content
                FROM memories m
                WHERE m.deprecated = 0
                  AND NOT EXISTS (
                        SELECT 1 FROM paths p
                        WHERE p.node_uuid = m.node_uuid AND p.namespace = ?
                  )
                ORDER BY m.id
                """,
                (NAMESPACE,)
            ).fetchall()
        orphan_uuids = {r["node_uuid"] for r in orphan_all}

        # 其二，台账里记着、但这条记忆在 MCP 里真的没了。判据是两条都不满足：
        # 既没有路径，也没有生效正文。只看"没有路径"会把上面那些孤儿误报成已删除
        # —— 它们没丢，只是访问不到，处理方式完全不同。
        deleted_in_mcp: list[str] = []
        if not args.domain and not args.prefix:
            deleted_in_mcp = sorted(
                u for u in ledger if u not in uuid_to_paths and u not in orphan_uuids
            )
        deleted_summary = f" | MCP 里已删除: {dim(str(len(deleted_in_mcp)))}" if deleted_in_mcp else ""

        # 已经登记过的孤儿不必再催我处理
        orphan_rows = [r for r in orphan_all if r["node_uuid"] not in ledger]
        orphan_summary = f" | 无路径孤儿: {cyan(str(len(orphan_rows)))}" if orphan_rows else ""

        # 打印总体统计
        print(bold("\n=== Horon 记忆迁移台账全盘审计 ==="))
        filter_desc = []
        if args.domain: filter_desc.append(f"域={args.domain}")
        if args.prefix: filter_desc.append(f"前缀={args.prefix}")
        if filter_desc:
            print(dim(f"筛选范围: {', '.join(filter_desc)}"))
            # 这两项要全局视野才能判断，筛选时算不了。必须说出来：
            # 只按前缀分批扫的话，不提示就等于这两类问题永远不会进入我的视野。
            print(dim("（本次跳过了「无路径孤儿」与「MCP 里已删除」两项检查，它们需要不带筛选的全盘扫描）"))

        # stat_only 模式快速返回，无需触碰正文与长字段
        if args.stat_only:
            print(f"总活跃实体数: {bold(str(total_nodes))} | "
                  f"已迁移: {green(str(migrated_count))} | "
                  f"已忽略: {yellow(str(ignored_count))} | "
                  f"待迁移: {cyan(str(pending_count))}"
                  f"{deleted_summary}"
                  f"{orphan_summary}")
            return

        mcp_drift_alerts: list[dict[str, Any]] = []
        horon_missing_alerts: list[dict[str, Any]] = []
        content_diff_alerts: list[dict[str, Any]] = []

        # 按需批量加载已迁移实体的 MCP 正文与 Horon 概念，避免全表无界常驻内存
        if migrated_entries:
            migrated_uuids = [e[0] for e in migrated_entries]
            # 判定当前版的规则和 get_mcp_active_memory 一致：认 deprecated = 0 这一行，不认 id 大小。
            # 每个 uuid 最多命中一行（idx_unique_active_memory 保证），所以直接建映射即可。
            active_mcp_map: dict[str, sqlite3.Row] = {}
            for i in range(0, len(migrated_uuids), 500):
                batch = migrated_uuids[i:i + 500]
                placeholders = ",".join(["?"] * len(batch))
                rows = nm_conn.execute(
                    f"""
                    SELECT id, node_uuid, title, content, created_at
                    FROM memories
                    WHERE deprecated = 0 AND node_uuid IN ({placeholders})
                    """,
                    batch
                ).fetchall()
                for r in rows:
                    active_mcp_map[r["node_uuid"]] = r

            horon_ids = list({e[2].get("horon_id") for e in migrated_entries if e[2].get("horon_id") is not None})
            horon_concepts_map: dict[int, sqlite3.Row] = {}
            for i in range(0, len(horon_ids), 500):
                batch = horon_ids[i:i + 500]
                placeholders = ",".join(["?"] * len(batch))
                rows = h_conn.execute(
                    f"SELECT id, name, content, updated_at FROM concepts WHERE id IN ({placeholders})",
                    batch
                ).fetchall()
                for r in rows:
                    horon_concepts_map[r["id"]] = r

            for uuid, paths, entry in migrated_entries:
                active_mcp = active_mcp_map.get(uuid)
                if not active_mcp:
                    continue

                current_mcp_id = active_mcp["id"]
                recorded_mcp_id = entry.get("mcp_version_id")

                # 检查关联的 Horon 概念
                horon_id = entry.get("horon_id")
                horon_concept = horon_concepts_map.get(horon_id)

                if not horon_concept:
                    horon_missing_alerts.append({
                        "uuid": uuid,
                        "paths": paths,
                        "ledger_entry": entry
                    })
                    continue

                # 版本号校验：只判"和台账锁的那一版是不是同一版"，不判方向。
                # id 大小不代表新旧（生效行底下可能压着 id 更大的废弃行），
                # 拿 id 比出来的"升级/回滚"会说反话；反正两种情况我都得去看 diff 再决定。
                if recorded_mcp_id is not None and current_mcp_id != recorded_mcp_id:
                    mcp_drift_alerts.append({
                        "uuid": uuid,
                        "paths": paths,
                        "horon_id": horon_concept["id"],
                        "horon_name": horon_concept["name"],
                        "recorded_id": recorded_mcp_id,
                        "current_id": current_mcp_id,
                        "note": entry.get("note")
                    })

                # 正文一致性检测 (换行符与空白规范化，先做字符串全等快速短路)
                mcp_raw = active_mcp["content"] or ""
                horon_raw = horon_concept["content"] or ""
                if mcp_raw != horon_raw and normalize_text(mcp_raw) != normalize_text(horon_raw):
                    content_diff_alerts.append({
                        "uuid": uuid,
                        "paths": paths,
                        "horon_id": horon_concept["id"],
                        "horon_name": horon_concept["name"],
                        "mcp_id": current_mcp_id,
                        "recorded_id": recorded_mcp_id
                    })

        diff_summary = f" | 正文存在差异: {yellow(str(len(content_diff_alerts)))}" if content_diff_alerts else ""
        print(f"总活跃实体数: {bold(str(total_nodes))} | "
              f"已迁移: {green(str(migrated_count))} | "
              f"已忽略: {yellow(str(ignored_count))} | "
              f"待迁移: {cyan(str(pending_count))}"
              f"{diff_summary}"
              f"{deleted_summary}"
              f"{orphan_summary}")

        # 有正文、没路径：这些节点只能用 uuid 操作，用 URI 是找不到的
        if orphan_rows:
            print(bold(cyan(f"\n[?] 有正文但没有任何路径的节点 ({len(orphan_rows)} 条)，"
                            f"URI 访问不到，只能用 uuid 操作:")))
            for r in orphan_rows[:ALERT_LIMIT]:
                # 纯空白的正文 normalize 后是空串，splitlines() 会是空列表，不能直接取 [0]
                lines = normalize_text(r["content"]).splitlines()
                head = lines[0][:60] if lines else "(空正文)"
                print(f"  - UUID {r['node_uuid']} (v{r['id']})")
                print(dim(f"    首行: {head}"))
            _print_omitted(len(orphan_rows), ALERT_LIMIT)

        # 明细：这些台账条目的记忆在 MCP 里已经没了，需要你决定是删台账还是恢复记忆
        if deleted_in_mcp:
            print(bold(yellow(f"\n[!] 台账里有记录，但这些记忆在 MCP 里已经找不到了 ({len(deleted_in_mcp)} 条):")))
            for u in deleted_in_mcp[:ALERT_LIMIT]:
                e = ledger[u]
                print(f"  - UUID {u}")
                if e.get("status") == "ignored":
                    print(dim("    台账状态: 已忽略"))
                else:
                    print(dim(f"    台账绑定: Horon #{e.get('horon_id')} '{e.get('horon_name')}' (锁定 MCP 版本 v{e.get('mcp_version_id')})"))
                if e.get("note"):
                    print(dim(f"    迁移方针/备注: {e['note']}"))
            _print_omitted(len(deleted_in_mcp), ALERT_LIMIT)

        # 打印严重警报（Horon 概念丢失）
        if horon_missing_alerts:
            print(bold(red(f"\n[!] 异常: 台账中记录的 Horon 概念在库中丢失 ({len(horon_missing_alerts)} 条):")))
            for a in horon_missing_alerts[:ALERT_LIMIT]:
                print(f"  - UUID {a['uuid']} -> 原记录 Horon #{a['ledger_entry'].get('horon_id')} '{a['ledger_entry'].get('horon_name')}'")
                print(dim(f"    路径: {', '.join(a['paths'])}"))
            _print_omitted(len(horon_missing_alerts), ALERT_LIMIT)

        # 打印 MCP 版本漂移警报 (不等于)
        if mcp_drift_alerts:
            print(bold(magenta(f"\n[*] 待确认: MCP 侧已经不是台账锁定的那一版了 ({len(mcp_drift_alerts)} 条):")))
            for a in mcp_drift_alerts[:ALERT_LIMIT]:
                print(f"  - [台账锁 v{a['recorded_id']} / MCP 现行 v{a['current_id']}] Horon #{a['horon_id']} '{a['horon_name']}'")
                print(dim(f"    UUID: {a['uuid']} | 路径: {', '.join(a['paths'])}"))
                if a.get("note"):
                    print(dim(f"    迁移方针/备注: {a['note']}"))
                print(dim(f"    审查: `python memory_migration.py diff {a['uuid']}`"))
                # 提示里用 Horon 的数字 id 而不是名字：名字可能带空格或引号，粘出去就坏了
                print(dim(f"    对齐: `python memory_migration.py update {a['uuid']} --horon {a['horon_id']}`"))
            _print_omitted(len(mcp_drift_alerts), ALERT_LIMIT)

        # 打印正文差异提示 (版本号可能对齐，但正文有微调或方针裁剪)
        if content_diff_alerts:
            print(bold(yellow(f"\n[~] 正文差异: 存在已迁移但内容与 MCP 不完全一致的节点 ({len(content_diff_alerts)} 条):")))
            for c in content_diff_alerts[:ALERT_LIMIT]:
                print(f"  - Horon #{c['horon_id']} '{bold(c['horon_name'])}' <-> MCP UUID {c['uuid']}")
                print(dim(f"    路径: {', '.join(c['paths'])}"))
                print(dim(f"    对比: `python memory_migration.py diff {c['uuid']}`"))
            _print_omitted(len(content_diff_alerts), ALERT_LIMIT)

        # 打印待迁移节点清单
        if pending_nodes:
            limit = len(pending_nodes) if args.all else args.limit
            print(bold(cyan(f"\n[>] 待迁移节点 (展示前 {min(limit, len(pending_nodes))} / {len(pending_nodes)} 条):")))
            pending_nodes.sort(key=lambda x: x["display"])
            for p in pending_nodes[:limit]:
                primary_view = p["display"]
                extra_count = len(p["paths"]) - 1
                extra_str = f" {dim(f'[共 {len(p['paths'])} 条接入路径]')}" if extra_count > 0 else ""
                print(f"  - {bold(primary_view)}{extra_str}")
                print(dim(f"    UUID: {p['uuid']}"))
                if args.details:
                    for alt_path in p["paths"]:
                        if alt_path != primary_view:
                            print(dim(f"    关联路径: {alt_path}"))
            if len(pending_nodes) > limit:
                print(dim(f"\n  ... 还有 {len(pending_nodes) - limit} 条未列出，使用 `--all` 或 `--limit <N>` 查看更多。"))
        else:
            print(green("\n恭喜：当前范围内没有待迁移的记忆节点！"))


def cmd_show(args: argparse.Namespace) -> None:
    """查看单个实体在 MCP、Horon 与台账中的详尽状态及迁移方针备注"""
    with get_nm_conn() as nm_conn, get_horon_conn() as h_conn:
        ledger = load_ledger()

        target = args.target.strip()
        if not target:
            print(red("错误: 查询目标不能为空"), file=sys.stderr)
            sys.exit(1)

        uuid = None
        horon_concept = None

        # 1. 若 target 具有明确的 MCP 特征（UUID 或带 :// 协议头），优先解析为 MCP 实体
        is_mcp_explicit = bool(UUID_REGEX.match(target) or "://" in target)
        matched_ledger_entries: list[tuple[str, dict[str, Any]]] = []
        if is_mcp_explicit:
            uuid = resolve_node_uuid(nm_conn, target)
            # 若 MCP 中已无记录但 target 为 UUID 且存在于台账中，允许回退展示台账记录
            if not uuid and UUID_REGEX.match(target):
                t_lower = target.lower()
                if t_lower in ledger:
                    uuid = t_lower
        else:
            # 2. 否则先看是否匹配 Horon 概念
            horon_concept = resolve_horon_concept(h_conn, target)
            if horon_concept:
                for u, entry in ledger.items():
                    if entry.get("horon_id") == horon_concept["id"]:
                        matched_ledger_entries.append((u, entry))
                if matched_ledger_entries:
                    uuid = matched_ledger_entries[0][0]

            # 3. 若 Horon 未匹配，或 Horon 概念在台账中未关联任何实体，回退尝试解析为 MCP 实体
            if not uuid:
                uuid = resolve_node_uuid(nm_conn, target)

        # 4. 若既没有对应 MCP uuid，但命中了未关联的 Horon 概念，展示该 Horon 概念实况
        if not uuid and horon_concept:
            print(bold(f"\n=== Horon 概念: [{horon_concept['id']}] {horon_concept['name']} ==="))
            print(f"角色: {horon_concept['role']}")
            print(f"书腰 (disclosure): {horon_concept['disclosure'] or '(无)'}")
            print(f"更新时间: {horon_concept['updated_at']}")
            print(dim(f"正文长度: {len(horon_concept['content'] or '')} 字符"))
            print(yellow("\n台账状态: 该 Horon 概念在迁移台账中尚未关联任何 MCP 实体。"))
            return

        if not uuid:
            print(red(f"未能解析查询目标: {target} (在 MCP 实体与 Horon 概念中均未找到)"), file=sys.stderr)
            sys.exit(1)

        all_paths = get_node_paths(nm_conn, uuid)
        active_mcp = get_mcp_active_memory(nm_conn, uuid)
        entry = ledger.get(uuid)

        if horon_concept and len(matched_ledger_entries) > 1:
            print(bold(yellow(f"\n[!] 提示: 该 Horon 概念由 {len(matched_ledger_entries)} 个 MCP 实体合并/拆分关联而来:")))
            for u, e in matched_ledger_entries:
                u_paths = get_node_paths(nm_conn, u)
                primary = u_paths[0] if u_paths else "(无路径/已退役)"
                active_note = f" | 方针: {e.get('note')}" if e.get("note") else ""
                cur_marker = " [当前展示]" if u == uuid else ""
                print(f"  - UUID {u}: {cyan(primary)}{active_note}{bold(cur_marker)}")

        print(bold(f"\n=== 实体详情: {uuid} ==="))
        print(f"MCP 关联路径 ({len(all_paths)} 条):")
        for p in all_paths:
            print(f"  - {cyan(p)}")

        if active_mcp:
            print(f"MCP 最新版本: ID={bold(str(active_mcp['id']))} | 标题={active_mcp['title']} | 创建时间={active_mcp['created_at']}")
            print(dim(f"正文长度: {len(active_mcp['content'] or '')} 字符"))
        else:
            print(yellow("MCP 侧无活跃正文 (可能已废弃或不存在)"))

        print("\n台账状态:")
        if not entry:
            print(yellow("  [未迁移] 该节点尚未登记进台账。"))
            if horon_concept and not is_mcp_explicit:
                print(dim(f"  提示: Horon 中存在同名未关联概念 [{horon_concept['id']}] '{horon_concept['name']}'，可用 update 命令关联。"))
        else:
            status = entry.get("status")
            if status == "ignored":
                print(f"  状态: {yellow('已忽略 (ignored)')}")
                print(f"  备注/理由: {cyan(entry.get('note', '(无)'))}")
                print(f"  登记时间: {entry.get('updated_at')}")
            elif status == "migrated":
                print(f"  状态: {green('已迁移 (migrated)')}")
                recorded_v = entry.get("mcp_version_id")
                if active_mcp and recorded_v is not None and active_mcp["id"] != recorded_v:
                    # 同上：不拿 id 大小判断"升级还是回滚"，只报"已经不是同一版了"
                    drift_msg = bold(magenta(f"[!] MCP 现行是 v{active_mcp['id']}，已不是台账锁的这一版 — 用 diff 看正文"))
                    print(f"  锁定 MCP 版本 ID: {yellow(str(recorded_v))} {drift_msg}")
                else:
                    print(f"  锁定 MCP 版本 ID: {recorded_v}")
                print(f"  关联 Horon 概念: ID={entry.get('horon_id')}, 名称='{entry.get('horon_name')}'")
                if entry.get("note"):
                    print(f"  迁移方针/备注: {cyan(entry['note'])}")
                print(f"  首迁时间: {entry.get('migrated_at')}")
                if "updated_at" in entry:
                    print(f"  台账更新: {entry.get('updated_at')}")

                # 对比 Horon 实况
                h_id = entry.get("horon_id")
                cur_h = h_conn.execute("SELECT id, name, content, updated_at FROM concepts WHERE id = ?", (h_id,)).fetchone()
                if cur_h:
                    print("\nHoron 当前概念实况:")
                    print(f"  名称: {bold(cur_h['name'])}")
                    print(f"  更新时间: {cur_h['updated_at']}")
                    print(dim(f"  正文长度: {len(cur_h['content'] or '')} 字符"))
                    if active_mcp:
                        identical = normalize_text(active_mcp['content']) == normalize_text(cur_h['content'])
                        if identical:
                            print(f"  正文一致性: {green('完全一致 (Synced)')}")
                        else:
                            print(f"  正文一致性: {yellow('存在差异 (Drifted) - 可使用 diff 命令查看')}")
                else:
                    print(red(f"  [!] 警告: Horon 中找不到概念 ID={h_id}！"))


def cmd_diff(args: argparse.Namespace) -> None:
    """比对 MCP 与 Horon 中对应实体的正文差异"""
    with get_nm_conn() as nm_conn, get_horon_conn() as h_conn:
        ledger = load_ledger()

        target = args.target.strip()
        if not target:
            print(red("错误: 查询目标不能为空"), file=sys.stderr)
            sys.exit(1)

        uuid = None
        horon_concept = None

        is_mcp_explicit = bool(UUID_REGEX.match(target) or "://" in target)
        if is_mcp_explicit:
            uuid = resolve_node_uuid(nm_conn, target)
        else:
            horon_concept = resolve_horon_concept(h_conn, target)
            matched_ledger_entries: list[tuple[str, dict[str, Any]]] = []
            if horon_concept:
                for u, entry in ledger.items():
                    if entry.get("horon_id") == horon_concept["id"]:
                        matched_ledger_entries.append((u, entry))
                if matched_ledger_entries:
                    uuid = matched_ledger_entries[0][0]
                    if len(matched_ledger_entries) > 1:
                        print(yellow(f"提示: 该 Horon 概念关联了 {len(matched_ledger_entries)} 个 MCP 实体 (N:1 拆分合并)。正在比对首个实体 {uuid}；如需比对其他实体请直接指定其 UUID/URI。"))
            if not uuid:
                uuid = resolve_node_uuid(nm_conn, target)

        if not uuid:
            if horon_concept:
                print(yellow(f"Horon 概念 [{horon_concept['id']}] '{horon_concept['name']}' 存在，但在迁移台账中未关联任何 MCP 实体。无法进行 diff。"), file=sys.stderr)
            else:
                print(red(f"未能解析查询目标: {target}"), file=sys.stderr)
            sys.exit(1)

        active_mcp = get_mcp_active_memory(nm_conn, uuid)
        if not active_mcp:
            print(red(f"MCP 侧找不到 UUID {uuid} 的活跃正文"), file=sys.stderr)
            sys.exit(1)

        entry = ledger.get(uuid)
        if entry and entry.get("status") == "ignored":
            print(yellow(f"该节点 (UUID {uuid}) 在台账中已被标记为已忽略 (ignored)，无需比对正文。"))
            if entry.get("note"):
                print(dim(f"忽略原因: {entry['note']}"))
            return

        if not horon_concept and entry and entry.get("status") == "migrated":
            horon_concept = h_conn.execute(
                "SELECT id, name, content FROM concepts WHERE id = ?",
                (entry.get("horon_id"),)
            ).fetchone()

        if not horon_concept:
            print(red(f"台账中未记录此节点 (UUID {uuid}) 的关联 Horon 概念，或对应概念不存在。请先使用 update 关联。"), file=sys.stderr)
            sys.exit(1)

        norm_mcp = normalize_text(active_mcp["content"])
        norm_horon = normalize_text(horon_concept["content"])

        if entry and entry.get("note"):
            print(dim(f"迁移方针/历史备注: {entry['note']}"))

        if norm_mcp == norm_horon:
            print(green(f"正文完全一致！(MCP v{active_mcp['id']} == Horon #{horon_concept['id']} '{horon_concept['name']}')"))
            return

        diff = list(difflib.unified_diff(
            norm_mcp.splitlines(),
            norm_horon.splitlines(),
            fromfile=f"MCP: {uuid} (v{active_mcp['id']})",
            tofile=f"Horon: [{horon_concept['id']}] {horon_concept['name']}",
            lineterm=""
        ))

        print(bold("\n--- 正文差异对比 ---"))
        for line in diff:
            if line.startswith("+"):
                print(green(line))
            elif line.startswith("-"):
                print(red(line))
            elif line.startswith("@"):
                print(cyan(line))
            else:
                print(line)


def cmd_update(args: argparse.Namespace) -> None:
    """登记或更新台账条目 (唯一修改入口：状态变更、概念绑定、方针备注、版本追平)"""
    if args.status == "ignored" and args.horon:
        print(red("错误: `--status ignored` 与 `--horon` 互斥，被忽略的节点不能绑定 Horon 概念！"), file=sys.stderr)
        sys.exit(1)

    with get_nm_conn() as nm_conn, get_horon_conn() as h_conn:
        ledger = load_ledger()

        uuid = resolve_node_uuid(nm_conn, args.target)

        if not uuid and UUID_REGEX.match(args.target):
            t_lower = args.target.lower()
            if t_lower in ledger:
                uuid = t_lower

        if not uuid:
            print(red(f"未能定位 MCP 实体: {args.target}"), file=sys.stderr)
            print(dim("提示：只接受完整 URI (如 core://nocturne/bluesky) 或完整 node_uuid，不做模糊匹配。"), file=sys.stderr)
            sys.exit(1)

        if uuid in ledger and args.horon is None and args.status is None and args.note is None:
            print(yellow("未提供任何更新选项 (--horon, --status, --note)，台账保持不变。"))
            return

        active_mcp = get_mcp_active_memory(nm_conn, uuid)
        current_mcp_id = active_mcp["id"] if active_mcp else None

        entry = ledger.get(uuid, {})
        now_iso = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat()

        # 1. 决定目标状态
        new_status = args.status
        if args.horon and not new_status:
            new_status = "migrated"
        elif not new_status:
            new_status = entry.get("status")

        if not new_status:
            print(red("无法确定条目状态：新登记必须指定 `--horon <名/ID>` (自动为 migrated) 或 `--status ignored`！"), file=sys.stderr)
            sys.exit(1)

        prev_status = entry.get("status")

        # 2. 根据状态组织字段
        if new_status == "migrated":
            if not args.horon and "horon_id" not in entry:
                print(red("该实体尚未关联 Horon 概念，首次迁入必须提供 `--horon <名/ID>`！"), file=sys.stderr)
                sys.exit(1)

            if args.horon:
                horon_concept = resolve_horon_concept(h_conn, args.horon)
                if not horon_concept:
                    print(red(f"在 Horon 中找不到概念: '{args.horon}'"), file=sys.stderr)
                    print(dim("提示：请先通过 `python frontend/cli.py create_concept` 创建概念。"), file=sys.stderr)
                    sys.exit(1)
                concept_id = horon_concept["id"]
                concept_name = horon_concept["name"]

                # 显式提供了 --horon，视为进行概念绑定/改绑/或对齐确认，版本号刷新到当前最新版
                if current_mcp_id is None:
                    print(red("MCP 中无活跃正文版本，无法锁定版本号。"), file=sys.stderr)
                    sys.exit(1)
                version_id = current_mcp_id
            else:
                # 未提供 --horon，属于仅修改 --note 场景，沿用已有概念与已有版本号
                concept_id = entry["horon_id"]
                cur_h = h_conn.execute("SELECT name FROM concepts WHERE id = ?", (concept_id,)).fetchone()
                concept_name = cur_h["name"] if cur_h else entry.get("horon_name", "")
                version_id = entry.get("mcp_version_id", current_mcp_id)

            updated_entry = {
                "status": "migrated",
                "horon_id": concept_id,
                "horon_name": concept_name,
                "mcp_version_id": version_id,
                "migrated_at": entry.get("migrated_at", now_iso),
                "updated_at": now_iso
            }
        else:  # ignored
            updated_entry = {
                "status": "ignored",
                "updated_at": now_iso
            }
            if "migrated_at" in entry:
                updated_entry["originally_migrated_at"] = entry["migrated_at"]
            elif "originally_migrated_at" in entry:
                updated_entry["originally_migrated_at"] = entry["originally_migrated_at"]

        # 3. 处理通用 note 备注
        if args.note is not None:
            updated_entry["note"] = args.note.strip()
        elif prev_status == "ignored" and new_status == "migrated":
            # 状态从 ignored 切回 migrated，旧的忽略理由不再默认继承，避免污染迁移方针
            print(yellow("提示：实体状态已从 ignored 切换为 migrated。原有忽略原因已清空，建议使用 `--note` 记录新的迁移方针。"))
        elif prev_status == "migrated" and new_status == "ignored":
            # 状态从 migrated 切回 ignored，旧的迁移方针不再默认继承，避免污染忽略理由
            print(yellow("提示：实体状态已从 migrated 切换为 ignored。原有迁移方针已清空，建议使用 `--note` 说明忽略理由。"))
        elif "note" in entry:
            updated_entry["note"] = entry["note"]
        elif new_status == "ignored":
            print(yellow("提示：建议提供 `--note` 说明为何忽略该节点。"))

        old_v = entry.get("mcp_version_id")
        new_v = updated_entry.get("mcp_version_id")

        ledger[uuid] = updated_entry
        save_ledger(ledger)

        print(green("台账已更新！"))
        print(f"  UUID: {uuid}")
        print(f"  状态: {bold(new_status)}")
        if new_status == "migrated":
            print(f"  Horon 概念: [{updated_entry['horon_id']}] {bold(updated_entry['horon_name'])}")
            if old_v is not None and new_v is not None and old_v != new_v:
                print(f"  MCP 版本同步: v{old_v} -> {bold(f'v{new_v}')}")
            else:
                print(f"  锁定 MCP 版本: v{new_v}")
        if updated_entry.get("note"):
            print(f"  迁移方针/备注: {cyan(updated_entry['note'])}")


# ---------------------------------------------------------------------------
# CLI 参数解析入口
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        prog="memory_migration.py",
        description="Nocturne Memory -> Horon 记忆迁移与对账台账工具",
        allow_abbrev=False
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # scan
    p_scan = sub.add_parser("scan", help="全盘扫描迁移进度与一致性")
    p_scan.add_argument("--domain", help="限定 domain (如 core, writer)")
    p_scan.add_argument("--prefix", help="限定 URI 前缀 (如 core://salem)")
    p_scan.add_argument("--limit", type=int, default=50, help="展示待迁移节点上限 (默认 50)")
    p_scan.add_argument("--all", action="store_true", help="展示全部待迁移节点")
    p_scan.add_argument("--details", action="store_true", help="展示每个节点的完整接入路径")
    p_scan.add_argument("--stat-only", action="store_true", help="仅输出汇总数字")

    # show
    p_show = sub.add_parser("show", help="查看单个节点在 MCP/Horon/台账中的对齐实况及方针")
    p_show.add_argument("target", help="node_uuid, URI 路径, 或 Horon 概念名/ID")

    # diff
    p_diff = sub.add_parser("diff", help="比对 MCP 与 Horon 的正文 diff")
    p_diff.add_argument("target", help="node_uuid, URI 路径, 或 Horon 概念名/ID")

    # update (唯一修改入口)
    p_up = sub.add_parser("update", help="登记或更新台账条目 (状态、概念绑定、方针备注、版本追平)")
    p_up.add_argument("target", help="node_uuid 或 URI 路径")
    p_up.add_argument("--horon", default=None, help="绑定的 Horon 概念名或 ID (自动将状态置为 migrated，并追平 MCP 版本号)")
    p_up.add_argument("--status", choices=["migrated", "ignored"], default=None, help="显式设置状态 (migrated 或 ignored)")
    p_up.add_argument("--note", default=None, help="记录或更新迁移方针、删减合并原因、或忽略理由")

    args = parser.parse_args()

    try:
        if args.command == "scan":
            cmd_scan(args)
        elif args.command == "show":
            cmd_show(args)
        elif args.command == "diff":
            cmd_diff(args)
        elif args.command == "update":
            cmd_update(args)
    except FileNotFoundError as e:
        print(red(f"文件未找到错误: {e}"), file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(red(f"执行出错: {e}"), file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
