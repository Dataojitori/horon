"""Concept & tag lifecycle mixin for HoronDB."""
from __future__ import annotations

import logging
import secrets

from ._db_common import _validate_name, _now, transactional, SYSTEM_TAGS
from .embedding import sync_single_embedding
from .models import MutationResult
from .tag_sandbox import TagPluginError


class ConceptMixin:
    """Create / delete concepts and tag vocabulary; suppose."""

    @transactional
    def create_concept(self, name: str,
                       disclosure: str | None = None,
                       content: str | None = None) -> MutationResult:
        """創建概念 concept + 默認 variation + 同名 alias。

        Args:
            name: 概念名（自動注冊為 alias）。
            disclosure: 初始書腰（写入 disclosures 表）。
            content: 初始正文。
        """
        name = _validate_name(name)
        self._check_name_available(name)
        now = _now()
        cursor = self.conn.execute(
            "INSERT INTO concepts (name, created_at, updated_at) "
            "VALUES (?,?,?)",
            (name, now, now),
        )
        concept_id = cursor.lastrowid
        if disclosure and disclosure.strip():
            disc_text = disclosure.strip()
            cursor_disc = self.conn.execute(
                "INSERT INTO disclosures (concept_id, text, created_at) "
                "VALUES (?,?,?)",
                (concept_id, disc_text, now),
            )
            disc_id = cursor_disc.lastrowid
            
            def _sync_hook(d_id=disc_id, d_text=disc_text):
                sync_single_embedding(self, "disclosures", d_id, d_text)
            self._post_commit_hooks.append(_sync_hook)
        sc = secrets.token_hex(2)
        self.conn.execute(
            "INSERT INTO variations "
            "(concept_id, short_code, content, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (concept_id, sc, content.strip() if content else None, now, now),
        )
        self.conn.execute(
            "INSERT INTO aliases (alias, concept_id) VALUES (?,?)",
            (name, concept_id),
        )
        return MutationResult(
            message=f"Success. Created concept '{name}' ('{name}', id={concept_id}).",
            concept_id=concept_id,
            concept_name=name,
            short_code=sc,
        )

    @transactional
    def init_plan(self, name: str,
                  content: str | None = None) -> MutationResult:
        """创建计划概念并原子化盖上 plan tag，返回引导性提示词。"""
        if not content or not content.strip():
            raise ValueError("init_plan requires non-empty --content.")
        res = self.create_concept(name, content=content)
        self._add_tag(res.concept_id, "plan")
        res.message = (
            f"[OK] Concept '{name}' created with tag 'plan'.\n\n"
            f"[ACTION REQUIRED]\n"
            f"一个合法的计划必须包含具体的执行步骤。你现在必须补全其结构：\n"
            f"使用 `horon set \"{name}\" expression \"步骤A → 步骤B → ...\"` 为其设定一个由 CHAIN (→) 组成的表达式。"
        )
        return res

    @transactional
    def init_result(self, name: str,
                    content: str | None = None) -> MutationResult:
        """创建结果概念并原子化盖上 result tag，纯快捷方式，无多余引导。"""
        if not content or not content.strip():
            raise ValueError("init_result requires non-empty --content.")
        res = self.create_concept(name, content=content)
        self._add_tag(res.concept_id, "result")
        res.message = f"[OK] Concept '{name}' created with tag 'result'."
        return res

    @transactional
    def create_tag(self, concept_name: str) -> MutationResult:
        """Register a concept's display name as a tag.

        The name must be an exact match on concepts.name (aliases don't
        qualify).  The source concept is auto-enrolled under the new tag.
        If the tag already exists, returns an info message with usage count.
        """
        concept_name = concept_name.strip()
        if not concept_name:
            raise ValueError("Tag name cannot be empty.")
        row = self.conn.execute(
            "SELECT id FROM concepts WHERE name = ?",
            (concept_name,),
        ).fetchone()
        if not row:
            raise ValueError(
                f"No concept with display name '{concept_name}'. "
                f"Tag name must be a concept's display name, not an alias.")
        cid = row["id"]

        existing = self.conn.execute(
            "SELECT source_concept_id FROM tags WHERE name = ?",
            (concept_name,),
        ).fetchone()
        if existing:
            usage = self.conn.execute(
                "SELECT COUNT(*) AS cnt FROM concept_tags WHERE tag = ?",
                (concept_name,),
            ).fetchone()["cnt"]
            return MutationResult(
                message=(f"Tag '{concept_name}' already exists "
                         f"(used by {usage} concept(s))."),
                concept_id=cid, concept_name=concept_name,
            )

        self.conn.execute(
            "INSERT INTO tags (name, source_concept_id) VALUES (?,?)",
            (concept_name, cid),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO concept_tags (concept_id, tag) "
            "VALUES (?,?)",
            (cid, concept_name),
        )
        diff = {"tags": {"added": [{"concept_id": cid, "tag": concept_name}], "removed": []}}
        self._run_mutation_hooks(cid, diff)
        return MutationResult(
            message=(f"Success. Registered tag '{concept_name}' "
                     f"(source concept id={cid}). "
                     f"Concept '{concept_name}' auto-enrolled."),
            concept_id=cid, concept_name=concept_name,
        )

    @transactional
    def delete_tag(self, tag_name: str) -> MutationResult:
        """Unregister a user-created tag from the vocabulary.

        System tags (in SYSTEM_TAGS) cannot be deleted.
        Tags still carried by concepts other than the source are blocked.
        """
        tag_name = tag_name.strip()
        if not tag_name:
            raise ValueError("Tag name cannot be empty.")
        row = self.conn.execute(
            "SELECT name, source_concept_id FROM tags WHERE name = ?",
            (tag_name,),
        ).fetchone()
        if not row:
            raise ValueError(f"Tag '{tag_name}' is not registered.")
        if tag_name in SYSTEM_TAGS:
            raise ValueError(
                f"Tag '{tag_name}' is a system-reserved tag "
                f"and cannot be deleted.")
        source_cid = row["source_concept_id"]

        other_users = self.conn.execute(
            "SELECT c.name FROM concept_tags ct "
            "JOIN concepts c ON ct.concept_id = c.id "
            "WHERE ct.tag = ? AND ct.concept_id != ?",
            (tag_name, source_cid),
        ).fetchall()
        if other_users:
            names = ", ".join(f"'{r['name']}'" for r in other_users)
            raise ValueError(
                f"Cannot delete tag '{tag_name}': still carried by "
                f"{names}.")

        has_tag = self.conn.execute(
            "SELECT 1 FROM concept_tags WHERE tag = ? AND concept_id = ?",
            (tag_name, source_cid)
        ).fetchone() is not None

        self.conn.execute(
            "DELETE FROM concept_tags WHERE tag = ? AND concept_id = ?",
            (tag_name, source_cid),
        )
        self.conn.execute(
            "DELETE FROM tags WHERE name = ?", (tag_name,),
        )

        if has_tag:
            diff = {
                "tags": {
                    "added": [],
                    "removed": [{
                        "concept_id": source_cid,
                        "tag": tag_name,
                    }],
                },
            }
            self._run_mutation_hooks(source_cid, diff)
            self._run_mutation_hook_for_tag(source_cid, tag_name, diff)

        self._plugin_cache.pop(tag_name, None)

        return MutationResult(
            message=f"Success. Tag '{tag_name}' unregistered.",
            concept_id=source_cid,
            concept_name=self._resolve_concept_name(source_cid),
        )

    def list_tags(self) -> list[dict]:
        """Return all registered tags with usage counts (excluding source concept)."""
        rows = self.conn.execute(
            "SELECT t.name, t.source_concept_id, "
            "  COUNT(ct.concept_id) AS usage_count "
            "FROM tags t "
            "LEFT JOIN concept_tags ct ON t.name = ct.tag "
            "  AND (t.source_concept_id IS NULL "
            "       OR ct.concept_id != t.source_concept_id) "
            "GROUP BY t.name "
            "ORDER BY t.name"
        ).fetchall()
        result = [dict(r) for r in rows]
        for r in result:
            try:
                plugin = self._get_plugin(r["name"])
            except TagPluginError as e:
                r["plugin_description"] = f"[插件加载失败] {e}"
                continue
            r["plugin_description"] = (plugin or {}).get("description")
        return result

    @transactional
    def suppose(self, expression: str) -> MutationResult:
        """从表达式直接原子化创建概念+组合变体（自动命名）。"""
        vtype, member_ids = self._parse_expression(expression, allow_single=False)

        existing = self._find_composition_variation(vtype, member_ids)
        if existing is not None:
            exist_cid, exist_sc, _ = existing
            exist_name = self._resolve_concept_name(exist_cid)
            raise ValueError(
                f"This relation already exists: '{exist_name}:{exist_sc}' "
                f"(id={exist_cid}). Do not create a duplicate. To record a "
                f"new observation of this relation, update the content of "
                f"that variation ('{exist_name}:{exist_sc}')."
            )

        member_names = [self._resolve_concept_name(mid) for mid in member_ids]

        if vtype == "CHAIN":
            joiner = "-then-"
        elif vtype == "AND":
            joiner = "-and-"
        elif vtype == "OR":
            joiner = "-or-"
        else:
            raise ValueError(f"Unknown variation type: {vtype}")

        base_name = joiner.join(member_names)
        
        try:
            _validate_name(base_name)
            self._check_name_available(base_name)
        except ValueError as e:
            raise ValueError(
                f"Auto-naming failed for '{expression}': {e}. "
                f"Please fall back to manual creation: "
                f"use `horon create_concept <custom_name> --content \"...\"` then `horon add <custom_name> variation \"{expression}\"`."
            )

        final_name = base_name

        res = self.create_concept(final_name)
        set_res = self._set_expression(final_name, expression)

        plugin_part = set_res.message.partition("Status was also reset to null.")[2]

        set_res.message = (
            f"Success. Created concept '{final_name}' (id={res.concept_id}) "
            f"to represent this relation.\n"
            f"Variation {set_res.short_code} expression: {expression}"
            + plugin_part
        )
        return set_res
