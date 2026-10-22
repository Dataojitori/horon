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

import logging
import subprocess
import sys
from pathlib import Path

from backend.embedding import EMBEDDING_MODEL, sync_single_embedding

logger = logging.getLogger("horon.integrity")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


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
    Side effect: when stale_embeddings is non-empty, spawns `python -m backend.integrity`
    detached from the hook, so the hook does not wait on embedding API calls.
    Output: a broadcast line listing circuit violations, or None when there are none.
    The embedding repair is not announced; failures land in the repair process's log.
    """
    if stale_embeddings(db):
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0
        subprocess.Popen([sys.executable, "-m", "backend.integrity"], cwd=_PROJECT_ROOT,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags, start_new_session=sys.platform != "win32")
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
    from dotenv import load_dotenv
    from backend.db import HoronDB

    load_dotenv(_PROJECT_ROOT / ".env")
    logging.basicConfig(filename=_PROJECT_ROOT / ".horon-local" / "integrity.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    written, failed = repair_embeddings(HoronDB(session_id="integrity-repair"))
    logger.info("embedding repair: %d written, %d failed", written, failed)
