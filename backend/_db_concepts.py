"""Concept & tag lifecycle mixin for HoronDB."""
from __future__ import annotations

import logging

from . import tag_sandbox
from ._db_common import SYSTEM_TAGS, _now, _validate_name, _validate_and_normalize_on_fire, transactional
from .embedding import sync_single_embedding
from .models import MutationResult
from .tag_sandbox import TagPluginError

_logger = logging.getLogger(__name__)


class ConceptMixin:
    """Create / delete concepts and tag vocabulary."""

    @transactional
    def create_concept(
        self,
        name: str,
        disclosure: str | None = None,
        content: str | None = None,
        role: str = "plain",
        lifespan: str | None = None,
        activation_rule: str | None = None,
        on_fire: str | None = None,
    ) -> MutationResult:
        """创建扁平概念 concept + 自动注册同名 alias。

        Args:
            name: 概念名（自动注册为 alias）。
            disclosure: 初始书腰（1:1 挂在 concepts 上）。
            content: 初始正文。
            role: 'plain' | 'sensor' | 'logic' | 'guard'
            lifespan: 仅 role='sensor' 时有效 ('turn' | 'session' | 'permanent')，默认 'session'。
            activation_rule: 激活规则（仅 role='logic'/'guard' 有效）。
            on_fire: 发火动作配置 (JSON 字符串，仅 role='sensor'/'logic'/'guard' 有效)。
        """
        valid_roles = ("plain", "sensor", "logic", "guard")
        if role not in valid_roles:
            raise ValueError(
                f"Invalid role: '{role}'. Must be one of: {', '.join(valid_roles)}."
            )

        name = _validate_name(name)
        self._check_name_available(name)
        now = _now()

        # 处理 activation_rule 与 role 联动
        rule_str = activation_rule
        vtype: str | None = None
        member_ids: list[int] = []
        if rule_str and rule_str.strip():
            if role == "sensor":
                raise ValueError(
                    "Sensor nodes cannot have activation rules (in-degree must be 0)."
                )
            if role == "plain":
                raise ValueError(
                    "Plain concepts cannot have activation rules (in-degree must be 0). "
                    "Specify role='logic' or role='guard' when creating composite nodes."
                )
            vtype, member_ids = self._parse_activation_rule(rule_str.strip())

            # 全局激活规则唯一性校验（仅限 logic 节点，guard 节点对应不同物理工具出口，允许共享相同激活规则）
            if role == "logic":
                existing_cid = self._find_composition_concept(vtype, member_ids, role="logic")
                if existing_cid is not None:
                    exist_name = self._resolve_concept_name(existing_cid)
                    raise ValueError(
                        f"Concept '{exist_name}' (id={existing_cid}) already has the same composition: {rule_str.strip()}"
                    )
        else:
            if role in ("logic", "guard"):
                raise ValueError(
                    f"{role} node requires an activation rule (e.g. 'A & B', 'A → B', or 'A | B')."
                )

        # 处理 lifespan 与 is_active
        is_active = 0
        if role == "sensor":
            lifespan = lifespan or "session"
            valid_lifespans = ("turn", "session", "permanent")
            if lifespan not in valid_lifespans:
                raise ValueError(
                    f"Invalid sensor lifespan: '{lifespan}'. Must be one of: {', '.join(valid_lifespans)}."
                )
            activation_type = None
        else:
            if lifespan is not None:
                raise ValueError(
                    f"Only sensor concepts can have a lifespan. Role '{role}' cannot have lifespan."
                )
            lifespan = None
            activation_type = vtype if role in ("logic", "guard") else None
            is_active = 0

        # 处理 on_fire 与 role 联动
        clean_on_fire = _validate_and_normalize_on_fire(on_fire)
        if role == "plain" and clean_on_fire is not None:
            raise ValueError("Plain concepts cannot have on_fire actions.")

        clean_content = content.strip() if content else None
        clean_disclosure = disclosure.strip() if disclosure and disclosure.strip() else None

        cursor = self.conn.execute(
            "INSERT INTO concepts (name, content, disclosure, role, is_active, lifespan, activation_type, on_fire, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, clean_content, clean_disclosure, role, is_active, lifespan, activation_type, clean_on_fire, now, now),
        )
        concept_id = cursor.lastrowid

        # 写入 compose_members
        all_infos: list[str] = []
        if member_ids:
            for idx, member_cid in enumerate(member_ids, start=1):
                self.conn.execute(
                    "INSERT INTO compose_members (parent_concept_id, member_concept_id, order_index) "
                    "VALUES (?, ?, ?)",
                    (concept_id, member_cid, idx),
                )
            clean_rule = activation_rule.strip() if activation_rule else ""
            diff: dict = {
                "activation-rule": {
                    "concept_id": concept_id,
                    "old": None,
                    "new": clean_rule,
                }
            }
            all_infos.extend(self._run_mutation_hooks(concept_id, diff))
            for mid in set(member_ids):
                all_infos.extend(self._run_mutation_hooks(mid, diff))

        # 写入 aliases
        self.conn.execute(
            "INSERT INTO aliases (alias, concept_id) VALUES (?, ?)",
            (name, concept_id),
        )

        # 触发 embedding 同步
        if clean_disclosure:
            def _sync_hook(cid=concept_id, disc=clean_disclosure):
                sync_single_embedding(self, cid, disc)
            self._post_commit_hooks.append(_sync_hook)

        fired_actions = []
        if role in ("logic", "guard"):
            eval_res = self._evaluator().evaluate()
            fired_actions = eval_res.fired_actions

        msg = f"Success. Created concept '{name}' (id={concept_id}, role={role})."
        if all_infos:
            msg += "\n" + "\n".join(all_infos)

        return MutationResult(
            message=msg,
            concept_id=concept_id,
            concept_name=name,
            fired_actions=fired_actions,
        )

    @transactional
    def delete_concept(self, concept) -> MutationResult:
        """删除概念本体及其级联关联。"""
        cid = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"

        if cname in SYSTEM_TAGS:
            raise ValueError(
                f"Cannot delete {label}: it is a system-reserved concept and cannot be deleted."
            )

        # 检查是否被其他概念引用为 compose_member
        refs = self.conn.execute(
            "SELECT DISTINCT cm.parent_concept_id, c.name "
            "FROM compose_members cm "
            "JOIN concepts c ON cm.parent_concept_id = c.id "
            "WHERE cm.member_concept_id = ? AND cm.parent_concept_id != ?",
            (cid, cid),
        ).fetchall()
        if refs:
            ref_parts = []
            for r in refs:
                expr = self._get_activation_rule(r["parent_concept_id"])
                line = f"  - '{r['name']}' (id={r['parent_concept_id']})"
                if expr:
                    line += f"  [{expr}]"
                ref_parts.append(line)
            detail = "\n".join(ref_parts)
            raise ValueError(
                f"Cannot delete {label}: it is still referenced as a compose member by:\n{detail}\n"
                f"Use read_concept to review them before deciding how to proceed."
            )

        # 检查是否作为 tag 源概念且被其他概念使用
        tag_row = self.conn.execute(
            "SELECT name FROM tags WHERE source_concept_id = ?",
            (cid,),
        ).fetchone()
        if tag_row:
            tag_name = tag_row["name"]
            other_users = self.conn.execute(
                "SELECT c.name FROM concept_tags ct "
                "JOIN concepts c ON ct.concept_id = c.id "
                "WHERE ct.tag = ? AND ct.concept_id != ?",
                (tag_name, cid),
            ).fetchall()
            if other_users:
                names = ", ".join(f"'{r['name']}'" for r in other_users)
                raise ValueError(
                    f"Cannot delete {label}: concept name '{tag_name}' is a registered tag still carried by: {names}."
                )

        # 预先获取当前概念所携带的全部 tags，用于触发对应的 mutation hooks
        tag_rows = self.conn.execute(
            "SELECT tag FROM concept_tags WHERE concept_id = ?", (cid,)
        ).fetchall()
        existing_tags = [r["tag"] for r in tag_rows]
        deleted_proxy = self._make_concept_proxy(cid)

        # 获取关联信息用于 mutation hooks（仅用于通知解除关联的子成员，非级联删除）
        unlinked_member_rows = self.conn.execute(
            "SELECT member_concept_id FROM compose_members WHERE parent_concept_id = ?",
            (cid,),
        ).fetchall()
        unlinked_member_ids = [r["member_concept_id"] for r in unlinked_member_rows]

        # 在任何数据库或文件删除之前运行删除 hook（若插件拒绝则在此抛出异常并触发事务回滚）
        diff = {"concepts": {"removed": [{"concept_id": cid, "name": cname, "members": unlinked_member_ids}]}}
        for tag in existing_tags:
            self._run_mutation_hook_for_tag(cid, tag, diff, proxy=deleted_proxy)
        if unlinked_member_ids:
            for mid in set(unlinked_member_ids):
                self._run_mutation_hooks(mid, diff)

        # 执行删除（外键约束自动清理 compose_members, aliases, disclosures, reminders, sensor_hooks, tool_guards, inhibitions 等）
        self.conn.execute("DELETE FROM concepts WHERE id = ?", (cid,))

        eval_res = self._evaluator().evaluate()

        msg = f"Success. Deleted concept {label}."
        if tag_row:
            msg += f" Tag '{tag_row['name']}' auto-removed."

        result = MutationResult(
            message=msg,
            concept_id=cid,
            concept_name=cname,
            fired_actions=eval_res.fired_actions,
        )

        if tag_row:
            tag_name_to_remove = tag_row["name"]
            self.conn.execute("DELETE FROM tags WHERE name = ?", (tag_name_to_remove,))
            plugin_path = tag_sandbox._PLUGINS_DIR / f"{tag_name_to_remove}.py"
            if tag_name_to_remove not in SYSTEM_TAGS and plugin_path.exists():

                def _delete_plugin_hook(path=plugin_path, tname=tag_name_to_remove, res=result):
                    try:
                        if path.exists():
                            path.unlink()
                            res.message = res.message.replace(
                                f"Tag '{tname}' auto-removed.",
                                f"Tag '{tname}' and its plugin file '{tname}.py' auto-removed.",
                            )
                    except OSError as e:
                        _logger.warning(
                            "Failed to delete plugin file '%s' for removed tag '%s': %s",
                            path, tname, e,
                        )
                        res.message += (
                            f" (Warning: Failed to delete plugin file '{path}': {e}. "
                            f"Please remove it manually.)"
                        )
                    self._plugin_cache.pop(tname, None)

                self._post_commit_hooks.append(_delete_plugin_hook)

        return result

    @transactional
    def create_tag(self, concept_name: str) -> MutationResult:
        """Register a concept's display name as a tag.

        The name must be an exact match on concepts.name (aliases don't
        qualify). The source concept is auto-enrolled under the new tag.
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
            concept_name=self._resolve_concept_name(source_cid) if source_cid else None,
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
