"""Snapshots mixin for HoronDB — pre-mutation snapshotting & human review rollback."""
from __future__ import annotations

import logging
from typing import Any

from ._db_common import _now, transactional
from .embedding import sync_single_embedding
from .models import ConceptReviewItem, MutationResult, SnapshotChange

_logger = logging.getLogger(__name__)


class SnapshotMixin:
    """Snapshot management: pre-mutation snapshots, review listing, approval, and rollback."""

    @transactional
    def capture_snapshot(self, concept: Any, field: str) -> bool:
        """在修改 content 或 disclosure 之前记录基线快照。
        
        若当前 review 周期内该字段已有快照，则保留最早的基线值，不覆盖并返回 False。
        若成功插入新快照记录，返回 True。
        """
        if field not in ("content", "disclosure"):
            return False

        try:
            cid = self._resolve_id(concept)
            cname = self._resolve_concept_name(cid)
        except Exception:
            return False

        # 检查是否已存在对应快照
        existing = self.conn.execute(
            "SELECT id FROM snapshots WHERE concept_id = ? AND field = ?",
            (cid, field),
        ).fetchone()
        if existing:
            return False

        now = _now()
        row = self.conn.execute(
            f"SELECT {field} FROM concepts WHERE id = ?", (cid,)
        ).fetchone()
        orig_val = row[field] if row else None

        self.conn.execute(
            "INSERT INTO snapshots (concept_id, concept_name, field, original_value, is_creation, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (cid, cname, field, orig_val, now),
        )
        return True

    @transactional
    def capture_creation_snapshot(self, concept_id: int, concept_name: str) -> None:
        """在创建新概念后记录初始空快照 (original_value=None, is_creation=1)。"""
        now = _now()
        for field in ("content", "disclosure"):
            existing = self.conn.execute(
                "SELECT id FROM snapshots WHERE concept_id = ? AND field = ?",
                (concept_id, field),
            ).fetchone()
            if not existing:
                self.conn.execute(
                    "INSERT INTO snapshots (concept_id, concept_name, field, original_value, is_creation, created_at) "
                    "VALUES (?, ?, ?, NULL, 1, ?)",
                    (concept_id, concept_name, field, now),
                )

    @transactional
    def capture_deletion_snapshot(self, concept: Any) -> bool:
        """在删除概念之前捕获其当前的 content 与 disclosure 作为原始快照。
        
        若该概念本身为本次新建（snapshots 中存在 is_creation=1），
        则删除节点意味着创建被抵消，直接清除该概念的全部快照并返回 False。
        若为普通概念删除并成功记录了快照，返回 True。
        """
        try:
            cid = self._resolve_id(concept)
            row = self.conn.execute(
                "SELECT name, content, disclosure FROM concepts WHERE id = ?", (cid,)
            ).fetchone()
            if not row:
                return False
            cname = row["name"]
        except Exception:
            return False

        # 若是本次新建节点，删除操作与新建相抵消，直接抹除快照记录
        creation_snap = self.conn.execute(
            "SELECT 1 FROM snapshots WHERE concept_id = ? AND is_creation = 1",
            (cid,),
        ).fetchone()
        if creation_snap:
            self.conn.execute("DELETE FROM snapshots WHERE concept_id = ?", (cid,))
            return False

        now = _now()
        inserted_any = False
        for field in ("content", "disclosure"):
            existing = self.conn.execute(
                "SELECT id FROM snapshots WHERE concept_id = ? AND field = ?",
                (cid, field),
            ).fetchone()
            if not existing:
                self.conn.execute(
                    "INSERT INTO snapshots (concept_id, concept_name, field, original_value, is_creation, created_at) "
                    "VALUES (?, ?, ?, ?, 0, ?)",
                    (cid, cname, field, row[field], now),
                )
                inserted_any = True
        return inserted_any

    def list_snapshots(self) -> list[ConceptReviewItem]:
        """按概念归总获取所有待审核的快照记录（单次 LEFT JOIN 消除 N+1 查询）。"""
        rows = self.conn.execute("""
            SELECT 
                s.id AS snap_id,
                s.concept_id,
                s.concept_name AS snap_concept_name,
                s.field,
                s.original_value,
                s.is_creation,
                s.created_at AS snap_created_at,
                c.id AS live_concept_id,
                c.name AS live_concept_name,
                c.role AS live_role,
                c.content AS live_content,
                c.disclosure AS live_disclosure
            FROM snapshots s
            LEFT JOIN concepts c ON s.concept_id = c.id
            ORDER BY s.concept_id ASC, s.id ASC
        """).fetchall()

        if not rows:
            return []

        # 按 concept_id 分组
        grouped: dict[int, list[Any]] = {}
        for r in rows:
            grouped.setdefault(r["concept_id"], []).append(r)

        items: list[ConceptReviewItem] = []
        for cid, snap_rows in grouped.items():
            first = snap_rows[0]
            is_deleted = (first["live_concept_id"] is None)
            is_creation = any(r["is_creation"] == 1 for r in snap_rows)

            concept_name = first["live_concept_name"] if not is_deleted else first["snap_concept_name"]
            role = first["live_role"] if not is_deleted else None
            current_content = first["live_content"] if not is_deleted else None
            current_disclosure = first["live_disclosure"] if not is_deleted else None

            earliest_created = first["snap_created_at"]
            changes: list[SnapshotChange] = []

            for r in snap_rows:
                field = r["field"]
                if field == "content":
                    changes.append(
                        SnapshotChange(
                            field="content",
                            original_value=r["original_value"],
                            current_value=current_content,
                            created_at=r["snap_created_at"],
                        )
                    )
                elif field == "disclosure":
                    changes.append(
                        SnapshotChange(
                            field="disclosure",
                            original_value=r["original_value"],
                            current_value=current_disclosure,
                            created_at=r["snap_created_at"],
                        )
                    )

            items.append(
                ConceptReviewItem(
                    concept_id=cid,
                    concept_name=concept_name,
                    role=role,
                    is_deleted=is_deleted,
                    is_creation=is_creation,
                    changes=changes,
                    created_at=earliest_created,
                )
            )

        return items

    @transactional
    def approve_snapshots(self, concept_id: int) -> MutationResult:
        """人类同意该概念的所有修改：删除快照记录。"""
        rows = self.conn.execute(
            "SELECT concept_name FROM snapshots WHERE concept_id = ? LIMIT 1",
            (concept_id,),
        ).fetchone()
        if not rows:
            raise ValueError(f"No pending snapshots found for concept ID {concept_id}.")

        cname = rows["concept_name"]
        self.conn.execute("DELETE FROM snapshots WHERE concept_id = ?", (concept_id,))

        return MutationResult(
            message=f"Success. Approved changes for concept '{cname}' (id={concept_id}).",
            concept_id=concept_id,
            concept_name=cname,
        )

    @transactional
    def rollback_snapshots(self, concept_id: int) -> MutationResult:
        """人类点击回滚：让该概念的数据回归到修改前快照的状态，然后删除快照对应内容。
        
        - 若概念已在库中不存在（被删除）：
            - 若该概念原本就是新建概念（is_creation=1），回滚即恢复为不存在，直接清除快照；
            - 否则重新恢复该概念并将其 role 设为 'plain'（砖块）；
        - 若概念为本次新创建（is_creation=1 且概念仍存在），则执行整节点删除操作；若因被引用无法删除，抛出错误提示用户解除关联；
        - 若为普通字段更新，则将 content/disclosure 恢复至 original_value。
        """
        rows = self.conn.execute(
            "SELECT id, concept_name, field, original_value, is_creation FROM snapshots WHERE concept_id = ?",
            (concept_id,),
        ).fetchall()
        if not rows:
            raise ValueError(f"No pending snapshots found for concept ID {concept_id}.")

        cname = rows[0]["concept_name"]
        concept_row = self.conn.execute(
            "SELECT id, name, role, content, disclosure FROM concepts WHERE id = ?",
            (concept_id,),
        ).fetchone()

        now = _now()

        if concept_row is None:
            # 概念已被删除
            if any(r["is_creation"] == 1 for r in rows):
                # 1a. 概念原本就是本次新建而后被删除的，回滚到初始状态即为不存在，直接清除快照
                self.conn.execute("DELETE FROM snapshots WHERE concept_id = ?", (concept_id,))
                return MutationResult(
                    message=f"Success. Rolled back changes for concept '{cname}' (id={concept_id}).",
                    concept_id=concept_id,
                    concept_name=cname,
                )

            # 1b. 概念原本存在而后被删除：恢复为 plain 砖块
            content_row = next((r for r in rows if r["field"] == "content"), None)
            disclosure_row = next((r for r in rows if r["field"] == "disclosure"), None)

            orig_content = content_row["original_value"] if content_row else None
            orig_disclosure = disclosure_row["original_value"] if disclosure_row else None

            self.conn.execute(
                "INSERT INTO concepts (id, name, content, disclosure, role, is_active, lifespan, activation_type, on_fire, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'plain', 0, NULL, NULL, NULL, ?, ?)",
                (concept_id, cname, orig_content, orig_disclosure, now, now),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO aliases (alias, concept_id) VALUES (?, ?)",
                (cname, concept_id),
            )

            if orig_disclosure:
                def _sync_hook(cid=concept_id, disc=orig_disclosure):
                    sync_single_embedding(self, cid, disc)
                self._post_commit_hooks.append(_sync_hook)
            else:
                self.conn.execute(
                    "DELETE FROM concept_embeddings WHERE concept_id = ?", (concept_id,)
                )

        elif any(r["is_creation"] == 1 for r in rows):
            # 2. 概念为新建节点且仍存在：回滚时执行整节点删除
            # 若仍被其他概念引用，delete_concept 会直接抛出明确的 ValueError
            self.delete_concept(concept_id)

        else:
            # 3. 概念依然存在且为普通修改：恢复 content / disclosure
            content_row = next((r for r in rows if r["field"] == "content"), None)
            disclosure_row = next((r for r in rows if r["field"] == "disclosure"), None)

            if content_row:
                orig_content = content_row["original_value"]
                self.conn.execute(
                    "UPDATE concepts SET content = ?, updated_at = ? WHERE id = ?",
                    (orig_content, now, concept_id),
                )

            if disclosure_row:
                orig_disclosure = disclosure_row["original_value"]
                self.conn.execute(
                    "UPDATE concepts SET disclosure = ?, updated_at = ? WHERE id = ?",
                    (orig_disclosure, now, concept_id),
                )
                if orig_disclosure:
                    def _sync_hook(cid=concept_id, disc=orig_disclosure):
                        sync_single_embedding(self, cid, disc)
                    self._post_commit_hooks.append(_sync_hook)
                else:
                    self.conn.execute(
                        "DELETE FROM concept_embeddings WHERE concept_id = ?",
                        (concept_id,),
                    )

        # 清除快照
        self.conn.execute("DELETE FROM snapshots WHERE concept_id = ?", (concept_id,))

        return MutationResult(
            message=f"Success. Rolled back changes for concept '{cname}' (id={concept_id}).",
            concept_id=concept_id,
            concept_name=cname,
        )
