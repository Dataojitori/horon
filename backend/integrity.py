"""Database integrity checks run at session start.

Two things can go wrong in the database that no write path catches:
- a when_to_read has no embedding, or one from an older embedding model. Setting a
  when_to_read embeds it right after commit, but if that call fails (no key, network,
  API error) the failure is only logged, and the node silently drops out of
  search_semantic. Embedding takes an API call per node, so it is repaired in a
  background process (`python -m backend.integrity`) instead of inside the hook.
- a composition member (compose_members) is not a sensor or logic node. Only a judgement
  call can fix that, so it is reported in the session-start broadcast.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from backend.embedding import sync_single_embedding
from backend.llm import EMBEDDING_MODEL, CostMeter

logger = logging.getLogger("horon.integrity")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 同一个库同一时刻只跑一个补向量进程：重复起的进程会把同一批书腰各嵌一遍，白花钱。
# 进程被杀掉会留下锁文件，超过这个秒数的锁当作过期。
_BACKFILL_LOCK_STALE_SECONDS = 15 * 60


def _backfill_lock_path(db_path: Path) -> Path:
    """db_path（已 resolve）对应的补向量锁文件。放在 .horon-local/ 下（已被 git 忽略），
    不放库旁边：库默认就在仓库根目录。文件名带库的完整路径的哈希，同名的不同库各用各的锁。"""
    digest = hashlib.sha1(str(db_path).encode("utf-8")).hexdigest()[:16]
    return _PROJECT_ROOT / ".horon-local" / f"backfill-{db_path.name}-{digest}.lock"


def _claim_backfill(db_path: Path) -> bool:
    """抢这个库的补向量锁；抢到返回 True，已有进程在补返回 False。"""
    lock = _backfill_lock_path(db_path)
    lock.parent.mkdir(exist_ok=True)
    for _ in range(2):
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime < _BACKFILL_LOCK_STALE_SECONDS:
                    return False
                lock.unlink()
            except FileNotFoundError:
                pass
    return False


def stale_embeddings(db) -> list[tuple[int, str]]:
    """Nodes whose when_to_read needs (re-)embedding.

    Input: an open HoronDB.
    Output: (id, when_to_read) for every node with a non-empty when_to_read that has no
    row in concept_embeddings, or whose row was made by a model other than EMBEDDING_MODEL.
    """
    rows = db.conn.execute(
        "SELECT c.id, c.when_to_read FROM concepts c "
        "LEFT JOIN concept_embeddings ce ON c.id = ce.concept_id "
        "WHERE c.when_to_read IS NOT NULL AND c.when_to_read != '' "
        "AND (ce.embedding IS NULL OR ce.embedding_model IS NOT ?)",
        (EMBEDDING_MODEL,),
    ).fetchall()
    return [(r["id"], r["when_to_read"]) for r in rows]


def circuit_violations(db) -> list[str]:
    """Composition members that are not sensor or logic nodes.

    Input: an open HoronDB.
    Output: one line per violation, "[parent id] parent name (role) → [member id] member
    name (role)"; empty when every member is a circuit node.
    """
    rows = db.conn.execute(
        "SELECT cm.parent_concept_id AS pid, p.name AS pname, p.role AS prole, "
        "cm.member_concept_id AS mid, m.name AS mname, m.role AS mrole "
        "FROM compose_members cm "
        "JOIN concepts p ON cm.parent_concept_id = p.id "
        "JOIN concepts m ON cm.member_concept_id = m.id "
        "WHERE m.role NOT IN ('sensor', 'logic') ORDER BY cm.parent_concept_id"
    ).fetchall()
    return [f"[{r['pid']}] {r['pname']}（{r['prole']}）→ [{r['mid']}] {r['mname']}（{r['mrole']}）"
            for r in rows]


def check_at_session_start(db) -> str | None:
    """Run both checks; start the embedding repair in the background if needed.

    Input: an open HoronDB (called once per new session from sync_session).
    Side effect: when stale_embeddings is non-empty (and no repair is already running on this
    database), spawns `python -m backend.integrity --db <this database>` detached from the
    hook, so the hook does not wait on embedding API calls.
    Output: a broadcast line listing circuit violations, or None when there are none.
    The embedding repair is not announced; failures land in the repair process's log.
    """
    if stale_embeddings(db) and _claim_backfill(db.db_path):
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0
        try:
            subprocess.Popen([sys.executable, "-m", "backend.integrity", "--db", str(db.db_path)],
                             cwd=_PROJECT_ROOT,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=flags, start_new_session=sys.platform != "win32")
        except Exception:
            _backfill_lock_path(db.db_path).unlink(missing_ok=True)
            raise
    violations = circuit_violations(db)
    if not violations:
        return None
    return ("电路组合里混进了不是 sensor/logic 的成员，求值会出错，读这些节点改掉规则："
            + "；".join(violations))


def repair_embeddings(db) -> tuple[int, int]:
    """Embed every node returned by stale_embeddings.

    Input: an open HoronDB.
    Output: (written, failed). A node whose when_to_read changed meanwhile counts as
    neither; its own write path embeds the new text.
    """
    written = failed = 0
    for cid, text in stale_embeddings(db):
        try:
            if sync_single_embedding(db, cid, text):
                written += 1
        except Exception as e:
            failed += 1
            logger.warning("embedding for concept %s failed: %s", cid, e)
    return written, failed


if __name__ == "__main__":
    import argparse
    from contextlib import closing

    from dotenv import load_dotenv

    from backend.db import HoronDB, database_exists

    # 不靠从启动它的进程继承 OPENROUTER_API_KEY：手动跑、或由没读过 .env 的进程拉起时也能嵌。
    load_dotenv(_PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description="Embed every node whose when_to_read has no current embedding.")
    parser.add_argument("--db", required=True, help="database file to repair")
    cli_args = parser.parse_args()

    (_PROJECT_ROOT / ".horon-local").mkdir(exist_ok=True)
    logging.basicConfig(filename=_PROJECT_ROOT / ".horon-local" / "integrity.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    # 这个 try 只管一件事：进程不管怎么结束（补完、库不在、出错），都删掉自己的锁文件。
    try:
        if not database_exists(cli_args.db):
            # 库在补向量进程启动前已被删掉：不重建
            logger.info("embedding repair skipped: database %s no longer exists", cli_args.db)
            sys.exit(0)
        # 这个进程脱离了发起它的请求，开销没法随那次请求返回，所以只记在日志里。
        with closing(HoronDB(session_id="integrity-repair", db_path=cli_args.db)) as repair_db, \
                CostMeter() as meter:
            written, failed = repair_embeddings(repair_db)
        logger.info("embedding repair of %s: %d written, %d failed, cost_usd=%.8f, input_tokens=%d",
                    cli_args.db, written, failed, meter.cost_usd, meter.summary()["input_tokens"])
    finally:
        _backfill_lock_path(Path(cli_args.db).resolve()).unlink(missing_ok=True)
