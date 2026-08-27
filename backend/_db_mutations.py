"""Add / Delete / Set / Update mixin for HoronDB."""
from __future__ import annotations

import logging
import os
from typing import Any

from . import tag_sandbox
from ._db_common import SYSTEM_TAGS, _now, _validate_name, transactional
from .embedding import sync_single_embedding
from .models import MutationResult

_logger = logging.getLogger(__name__)


class MutationMixin:
    """All graph-mutating operations: add, delete, set, update."""

    # ── Add ──────────────────────────────────────────────────────────────────

    def add(self, concept, kind: str, value: str, **kwargs) -> MutationResult:
        """给概念添加多值附属组件或关系：name（别名）、tag（标签）、inhibition（抑制边）。"""
        if kind == "name":
            return self._add_name(concept, value)
        elif kind == "tag":
            return self._add_tag(concept, value)
        elif kind == "disclosure":
            raise ValueError(
                "disclosure is a 1:1 property. "
                "Use 'set <concept> disclosure \"...\"' instead."
            )
        elif kind in ("sensor_hook", "sensor-hook"):
            raise ValueError(
                "sensor_hook is a 1:1 property. "
                "Use 'set <concept> sensor_hook <event_type> --match-pattern \"...\" [--tool <tool>]' instead."
            )
        elif kind in ("tool_guard", "tool-guard"):
            raise ValueError(
                "tool_guard is a 1:1 property. "
                "Use 'set <concept> tool_guard <tool> [--args-pattern \"...\"]' instead."
            )
        elif kind == "inhibition":
            return self._add_inhibition(target_concept=concept, inhibitor_concept=value)
        raise ValueError(
            f"Unknown type: '{kind}'. Use 'name', 'tag', or 'inhibition'."
        )

    @transactional
    def _add_name(self, concept, name: str) -> MutationResult:
        """给 concept 加一个 alias。"""
        name = _validate_name(name)
        cid = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"
        existing = self.conn.execute(
            "SELECT concept_id FROM aliases WHERE alias=?", (name,)
        ).fetchone()
        if existing:
            if existing["concept_id"] == cid:
                return MutationResult(
                    message=f"Success. '{name}' is already an alias for {label}.",
                    concept_id=cid, concept_name=cname,
                )
            raise ValueError(
                f"Name '{name}' already resolves to concept "
                f"{existing['concept_id']}.")
        self.conn.execute(
            "INSERT INTO aliases (alias, concept_id) VALUES (?,?)",
            (name, cid),
        )
        diff = {"names": {"added": [{"concept_id": cid, "name": name}], "removed": []}}
        self._run_mutation_hooks(cid, diff)
        return MutationResult(
            message=f"Success. Added alias '{name}' to {label}.",
            concept_id=cid, concept_name=cname,
        )

    @transactional
    def _add_tag(self, concept, tag: str) -> MutationResult:
        """给概念盖一个 tag。

        输入：concept —— 概念名/别名/ID；tag —— 必须已在 tags 词表注册。
        行为：未注册的 tag 直接报错并列出已知词表；概念已有该 tag 时幂等成功。
        输出：MutationResult。
        """
        tag = tag.strip()
        if not tag:
            raise ValueError("Tag cannot be empty.")
        cid = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"
        registered = self.conn.execute(
            "SELECT 1 FROM tags WHERE name = ?", (tag,)
        ).fetchone()
        if not registered:
            known = [row["name"] for row in self.conn.execute(
                "SELECT name FROM tags ORDER BY name")]
            raise ValueError(
                f"Tag '{tag}' is not registered. "
                f"Known tags: {', '.join(known)}. "
                f"Use create_tag to register a new tag.")
        already = self.conn.execute(
            "SELECT 1 FROM concept_tags WHERE concept_id = ? AND tag = ?",
            (cid, tag),
        ).fetchone()
        if already:
            return MutationResult(
                message=f"Success. {label} already has tag '{tag}'.",
                concept_id=cid, concept_name=cname,
            )
        self.conn.execute(
            "INSERT INTO concept_tags (concept_id, tag) VALUES (?,?)",
            (cid, tag),
        )
        diff = {"tags": {"added": [{"concept_id": cid, "tag": tag}], "removed": []}}
        infos = self._run_mutation_hooks(cid, diff)
        msg = f"Success. Tagged {label} as '{tag}'."
        if infos:
            msg += "\n".join([""] + infos)
        return MutationResult(
            message=msg,
            concept_id=cid, concept_name=cname,
        )

    @transactional
    def _set_disclosure(self, concept, text: str) -> MutationResult:
        """设置/更新概念的书腰 (1:1 覆盖)。"""
        text = text.strip()
        if not text:
            raise ValueError("Disclosure text cannot be empty.")
        cid = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"
        now = _now()
        old_row = self.conn.execute(
            "SELECT disclosure FROM concepts WHERE id=?", (cid,)
        ).fetchone()
        old_disc = old_row["disclosure"] if old_row else None

        self.conn.execute(
            "UPDATE concepts SET disclosure=?, updated_at=? WHERE id=?",
            (text, now, cid),
        )
        self.conn.execute(
            "DELETE FROM concept_embeddings WHERE concept_id=?",
            (cid,),
        )

        def _sync_hook(d_cid=cid, d_text=text):
            sync_single_embedding(self, d_cid, d_text)
        self._post_commit_hooks.append(_sync_hook)

        diff = {"disclosure": {"concept_id": cid, "old": old_disc, "new": text}}
        self._run_mutation_hooks(cid, diff)
        return MutationResult(
            message=f"Success. Set disclosure of {label} to: {text}",
            concept_id=cid, concept_name=cname,
        )

    @transactional
    def _set_sensor_hook(
        self, concept, event_type: str, match_pattern: str, tool: str | None = None
    ) -> MutationResult:
        """给传感器绑定被动感知钩子 (1:1 覆盖)。"""
        cid = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"

        row = self.conn.execute(
            "SELECT role, lifespan FROM concepts WHERE id = ?", (cid,)
        ).fetchone()
        if not row or row["role"] != "sensor":
            raise ValueError(f"只有 sensor 传感器允许绑定感知钩子。概念 {label} 的角色为 '{row['role'] if row else 'unknown'}'。")

        if row["lifespan"] == "permanent":
            raise ValueError(
                "永久传感器不可绑定感知钩子；可自动验证的事实请将生命周期设为 session 或 turn。"
            )

        valid_events = ("user_message", "model_message", "tool_call", "tool_result")
        if event_type not in valid_events:
            raise ValueError(
                f"Invalid event_type: '{event_type}'. Must be one of: {', '.join(valid_events)}."
            )

        if event_type in ("user_message", "model_message") and tool and tool.strip():
            raise ValueError(f"tool cannot be specified for message event '{event_type}'.")

        if not match_pattern or not match_pattern.strip():
            raise ValueError("match_pattern cannot be empty.")

        clean_tool = tool.strip() if (tool and event_type in ("tool_call", "tool_result")) else None

        old_hook = self.conn.execute(
            "SELECT id FROM sensor_hooks WHERE sensor_concept_id = ?", (cid,)
        ).fetchone()

        conflict = self.conn.execute(
            "SELECT sh.sensor_concept_id, c.name FROM sensor_hooks sh "
            "JOIN concepts c ON sh.sensor_concept_id = c.id "
            "WHERE sh.event_type = ? AND IFNULL(sh.tool, '') = IFNULL(?, '') AND sh.match_pattern = ? "
            "AND sh.sensor_concept_id != ?",
            (event_type, clean_tool, match_pattern.strip(), cid),
        ).fetchone()
        if conflict:
            raise ValueError(
                f"Sensor hook rule (event_type='{event_type}', tool={clean_tool!r}, match_pattern={match_pattern.strip()!r}) "
                f"already bound to sensor '{conflict['name']}' (id={conflict['sensor_concept_id']}). "
                f"Each (event_type, tool, match_pattern) rule can only be managed by one sensor concept."
            )

        # 检查是否已存在 hook 并覆盖
        self.conn.execute("DELETE FROM sensor_hooks WHERE sensor_concept_id = ?", (cid,))
        now = _now()
        cursor = self.conn.execute(
            "INSERT INTO sensor_hooks (sensor_concept_id, event_type, tool, match_pattern, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (cid, event_type, clean_tool, match_pattern.strip(), now),
        )
        hook_id = cursor.lastrowid
        tool_info = f", tool: '{clean_tool}'" if clean_tool else ""
        if old_hook:
            msg = f"Success. Replaced sensor hook #{old_hook['id']} on {label} with #{hook_id} ({event_type}: {match_pattern}{tool_info})."
        else:
            msg = f"Success. Set sensor hook #{hook_id} on {label} ({event_type}: {match_pattern}{tool_info})."
        return MutationResult(
            message=msg,
            concept_id=cid, concept_name=cname,
        )

    @transactional
    def _set_tool_guard(
        self, concept, tool: str, args_pattern: str | None = None
    ) -> MutationResult:
        """给放行守卫绑定工具放行规则 (1:1 覆盖)。"""
        cid = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"

        row = self.conn.execute(
            "SELECT role FROM concepts WHERE id = ?", (cid,)
        ).fetchone()
        if not row or row["role"] != "guard":
            raise ValueError(f"只有 guard 放行守卫允许绑定工具规则。概念 {label} 的角色为 '{row['role'] if row else 'unknown'}'。")

        if not tool or not tool.strip():
            raise ValueError("tool name cannot be empty.")

        clean_args = args_pattern.strip() if args_pattern and args_pattern.strip() else None

        old_guard = self.conn.execute(
            "SELECT id FROM tool_guards WHERE guard_concept_id = ?", (cid,)
        ).fetchone()

        conflict = self.conn.execute(
            "SELECT tg.guard_concept_id, c.name FROM tool_guards tg "
            "JOIN concepts c ON tg.guard_concept_id = c.id "
            "WHERE tg.tool = ? AND IFNULL(tg.args_pattern, '') = IFNULL(?, '') "
            "AND tg.guard_concept_id != ?",
            (tool.strip(), clean_args, cid),
        ).fetchone()
        if conflict:
            raise ValueError(
                f"Tool guard rule (tool='{tool.strip()}', args_pattern={clean_args!r}) "
                f"already bound to guard '{conflict['name']}' (id={conflict['guard_concept_id']}). "
                f"Each (tool, args_pattern) pair can only be managed by one guard concept."
            )

        self.conn.execute("DELETE FROM tool_guards WHERE guard_concept_id = ?", (cid,))
        now = _now()
        cursor = self.conn.execute(
            "INSERT INTO tool_guards (guard_concept_id, tool, args_pattern, created_at) "
            "VALUES (?, ?, ?, ?)",
            (cid, tool.strip(), clean_args, now),
        )
        guard_id = cursor.lastrowid
        pattern_info = f", args_pattern: '{clean_args}'" if clean_args else ""
        if old_guard:
            msg = f"Success. Replaced tool guard #{old_guard['id']} on {label} with #{guard_id} (tool: '{tool.strip()}'{pattern_info})."
        else:
            msg = f"Success. Set tool guard #{guard_id} on {label} (tool: '{tool.strip()}'{pattern_info})."
        return MutationResult(
            message=msg,
            concept_id=cid, concept_name=cname,
        )

    @transactional
    def _add_inhibition(self, target_concept, inhibitor_concept) -> MutationResult:
        """建立负向抑制边 (inhibitor -> target)。"""
        target_id = self._resolve_id(target_concept)
        inhibitor_id = self._resolve_id(inhibitor_concept)
        target_name = self._resolve_concept_name(target_id)
        inhibitor_name = self._resolve_concept_name(inhibitor_id)

        if target_id == inhibitor_id:
            raise ValueError("自抑制（Target == Inhibitor）无效，严禁建立自环抑制边。")

        t_row = self.conn.execute("SELECT role FROM concepts WHERE id = ?", (target_id,)).fetchone()
        if not t_row or t_row["role"] not in ("logic", "guard"):
            raise ValueError(
                f"抑制目标 (target) 必须为 logic 或 guard 节点，当前 '{target_name}' 角色为 '{t_row['role'] if t_row else 'unknown'}'。"
            )

        i_row = self.conn.execute("SELECT role FROM concepts WHERE id = ?", (inhibitor_id,)).fetchone()
        if not i_row or i_row["role"] not in ("sensor", "logic"):
            raise ValueError(
                f"抑制源 (inhibitor) 必须为 sensor 或 logic 节点，当前 '{inhibitor_name}' 角色为 '{i_row['role'] if i_row else 'unknown'}'。"
            )

        now = _now()
        self.conn.execute(
            "INSERT OR IGNORE INTO inhibitions (target_concept_id, inhibitor_concept_id, created_at) "
            "VALUES (?, ?, ?)",
            (target_id, inhibitor_id, now),
        )
        return MutationResult(
            message=f"Success. Added inhibition: '{inhibitor_name}' (id={inhibitor_id}) ─⊣ '{target_name}' (id={target_id}).",
            concept_id=target_id, concept_name=target_name,
        )

    # ── Delete ───────────────────────────────────────────────────────────────

    def delete(self, target, kind: str | None = None,
               value: str | None = None) -> MutationResult:
        """删除操作。

        kind 省略:      删除概念本体。
        name:          删除别名（value=要删的别名，必填）。
        activation-rule: 清除激活规则（降级为 plain）。
        tag:           揭掉概念上的 tag（value=要揭的 tag，必填）。
        disclosure:    清除概念上的书腰。
        sensor_hook:   删除感知钩子。
        tool_guard:    删除工具守卫规则。
        inhibition:    解除抑制关系（value=inhibitor_concept，必填）。
        """
        if kind is None:
            return self.delete_concept(target)
        elif kind == "name":
            if value is None:
                raise ValueError("Specify which name to delete.")
            return self._delete_name(target, value)
        elif kind in ("activation_rule", "activation-rule"):
            if value is not None:
                raise ValueError(
                    f"delete {kind} takes no extra argument, but got: '{value}'.")
            return self._delete_activation_rule(target)
        elif kind == "tag":
            if value is None:
                raise ValueError("Specify which tag to delete.")
            return self._delete_tag(target, value)
        elif kind == "disclosure":
            if value is not None:
                raise ValueError(
                    f"delete disclosure takes no extra argument, but got: '{value}'. "
                    f"Use 'delete <concept> disclosure' to clear the disclosure.")
            return self._delete_disclosure(target)
        elif kind in ("sensor_hook", "sensor-hook"):
            if value is not None:
                raise ValueError(
                    f"delete {kind} takes no extra argument, but got: '{value}'.")
            return self._delete_sensor_hook(target)
        elif kind in ("tool_guard", "tool-guard"):
            if value is not None:
                raise ValueError(
                    f"delete {kind} takes no extra argument, but got: '{value}'.")
            return self._delete_tool_guard(target)
        elif kind == "inhibition":
            if value is None:
                raise ValueError("Specify inhibitor concept to delete inhibition from.")
            return self._delete_inhibition(target, value)
        raise ValueError(
            f"Unknown type: '{kind}'. "
            f"Use 'name', 'activation_rule', 'tag', 'disclosure', 'sensor_hook', 'tool_guard', or 'inhibition'.")

    @transactional
    def _delete_name(self, concept, name: str) -> MutationResult:
        """删一个 alias。拒删当前显示名。"""
        if not name or not name.strip():
            raise ValueError("Alias name cannot be empty.")
        name = name.strip()
        cid = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"
        row = self.conn.execute(
            "SELECT concept_id FROM aliases WHERE alias=?", (name,)
        ).fetchone()
        if not row:
            raise ValueError(f"No such alias: '{name}'.")
        if row["concept_id"] != cid:
            raise ValueError(
                f"Alias '{name}' belongs to concept "
                f"{row['concept_id']}, not {label}.")
        if cname == name:
            raise ValueError(
                f"'{name}' is the display name. "
                f"Use 'set name' to change it first.")
        self.conn.execute(
            "DELETE FROM aliases WHERE alias=?", (name,))
        diff = {"names": {"added": [], "removed": [{"concept_id": cid, "name": name}]}}
        self._run_mutation_hooks(cid, diff)
        return MutationResult(
            message=f"Success. Removed alias '{name}' from {label}.",
            concept_id=cid, concept_name=cname,
        )

    @transactional
    def _delete_tag(self, concept, tag: str) -> MutationResult:
        """揭掉概念上的一个 tag。

        输入：concept —— 概念名/别名/ID；tag —— 该概念身上现有的 tag。
        行为：概念没有这个 tag 时报错（与 _delete_name 同待遇，删不存在
              的东西是调用方认知错误，不静默吞掉）。只揭概念与 tag 的
              关联，不动 tags 词表本身。
        输出：MutationResult。
        """
        tag = tag.strip()
        cid = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"
        existing = self.conn.execute(
            "SELECT 1 FROM concept_tags WHERE concept_id = ? AND tag = ?",
            (cid, tag),
        ).fetchone()
        if not existing:
            raise ValueError(f"{label} does not have tag '{tag}'.")
        self.conn.execute(
            "DELETE FROM concept_tags WHERE concept_id = ? AND tag = ?",
            (cid, tag),
        )
        diff = {"tags": {"added": [], "removed": [{"concept_id": cid, "tag": tag}]}}
        self._run_mutation_hooks(cid, diff)
        self._run_mutation_hook_for_tag(cid, tag, diff)
        return MutationResult(
            message=f"Success. Removed tag '{tag}' from {label}.",
            concept_id=cid, concept_name=cname,
        )

    @transactional
    def _delete_disclosure(self, concept) -> MutationResult:
        """清除概念的书腰。"""
        cid = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"
        row = self.conn.execute(
            "SELECT disclosure FROM concepts WHERE id = ?",
            (cid,),
        ).fetchone()
        if not row or not row["disclosure"]:
            raise ValueError(f"Concept {label} has no disclosure to delete.")
        old_text = row["disclosure"]
        now = _now()
        self.conn.execute(
            "UPDATE concepts SET disclosure=NULL, updated_at=? WHERE id=?",
            (now, cid),
        )
        self.conn.execute(
            "DELETE FROM concept_embeddings WHERE concept_id=?",
            (cid,),
        )
        diff = {"disclosure": {"concept_id": cid, "old": old_text, "new": None}}
        self._run_mutation_hooks(cid, diff)
        return MutationResult(
            message=f"Success. Removed disclosure from {label}.",
            concept_id=cid, concept_name=cname,
        )

    @transactional
    def _delete_activation_rule(self, concept) -> MutationResult:
        """清除激活规则，原子降级为 plain 砖块。"""
        cid = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"

        row = self.conn.execute(
            "SELECT role, activation_type FROM concepts WHERE id = ?", (cid,)
        ).fetchone()
        if not row:
            raise ValueError(f"Concept {label} does not exist.")

        unlinked_member_rows = self.conn.execute(
            "SELECT member_concept_id FROM compose_members WHERE parent_concept_id = ? ORDER BY order_index",
            (cid,),
        ).fetchall()
        unlinked_member_ids = [r["member_concept_id"] for r in unlinked_member_rows]
        old_activation_type = row["activation_type"]
        old_role = row["role"]
        old_rule = self._get_activation_rule(cid)

        if old_role == "sensor":
            raise ValueError(f"Concept {label} is a sensor and has no activation rule to delete.")
        if not old_activation_type and not unlinked_member_ids:
            raise ValueError(f"Concept {label} has no activation rule to delete.")

        self.conn.execute(
            "DELETE FROM compose_members WHERE parent_concept_id = ?", (cid,)
        )
        self.conn.execute(
            "DELETE FROM tool_guards WHERE guard_concept_id = ?", (cid,)
        )
        self.conn.execute(
            "DELETE FROM inhibitions WHERE target_concept_id = ? OR inhibitor_concept_id = ?", (cid, cid)
        )
        self.conn.execute(
            "UPDATE concepts SET role = 'plain', activation_type = NULL, lifespan = NULL, is_active = 0, on_fire = NULL, updated_at = ? "
            "WHERE id = ?",
            (_now(), cid),
        )

        diff: dict = {
            "activation-rule": {
                "concept_id": cid,
                "old": old_rule,
                "new": None,
            }
        }
        if old_role != "plain":
            diff["role"] = {"concept_id": cid, "old": old_role, "new": "plain"}

        all_infos: list[str] = []
        all_infos.extend(self._run_mutation_hooks(cid, diff))
        if unlinked_member_ids:
            for mid in set(unlinked_member_ids):
                all_infos.extend(self._run_mutation_hooks(mid, diff))

        if old_role != "plain":
            msg = f"Success. Cleared activation rule of {label}; automatically downgraded role from '{old_role}' to 'plain'."
        else:
            msg = f"Success. Cleared activation rule of {label}."
        if all_infos:
            msg += "\n" + "\n".join(all_infos)

        return MutationResult(
            message=msg,
            concept_id=cid, concept_name=cname,
        )

    @transactional
    def _delete_sensor_hook(self, concept) -> MutationResult:
        """删除感知钩子。"""
        cid = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"
        existing = self.conn.execute(
            "SELECT id FROM sensor_hooks WHERE sensor_concept_id = ?", (cid,)
        ).fetchone()
        if not existing:
            raise ValueError(f"{label} has no sensor hook to delete.")
        self.conn.execute("DELETE FROM sensor_hooks WHERE sensor_concept_id = ?", (cid,))
        return MutationResult(
            message=f"Success. Removed sensor hook from {label}.",
            concept_id=cid, concept_name=cname,
        )

    @transactional
    def _delete_tool_guard(self, concept) -> MutationResult:
        """删除工具守卫规则。"""
        cid = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"
        existing = self.conn.execute(
            "SELECT id FROM tool_guards WHERE guard_concept_id = ?", (cid,)
        ).fetchone()
        if not existing:
            raise ValueError(f"{label} has no tool guard to delete.")
        self.conn.execute("DELETE FROM tool_guards WHERE guard_concept_id = ?", (cid,))
        return MutationResult(
            message=f"Success. Removed tool guard from {label}.",
            concept_id=cid, concept_name=cname,
        )

    @transactional
    def _delete_inhibition(self, target_concept, inhibitor_concept) -> MutationResult:
        """解除抑制关系。"""
        target_id = self._resolve_id(target_concept)
        inhibitor_id = self._resolve_id(inhibitor_concept)
        target_name = self._resolve_concept_name(target_id)
        inhibitor_name = self._resolve_concept_name(inhibitor_id)

        existing = self.conn.execute(
            "SELECT 1 FROM inhibitions WHERE target_concept_id = ? AND inhibitor_concept_id = ?",
            (target_id, inhibitor_id),
        ).fetchone()
        if not existing:
            raise ValueError(
                f"No inhibition from '{inhibitor_name}' (id={inhibitor_id}) "
                f"to '{target_name}' (id={target_id}) exists."
            )

        self.conn.execute(
            "DELETE FROM inhibitions WHERE target_concept_id = ? AND inhibitor_concept_id = ?",
            (target_id, inhibitor_id),
        )
        return MutationResult(
            message=f"Success. Removed inhibition: '{inhibitor_name}' ─⊣ '{target_name}'.",
            concept_id=target_id, concept_name=target_name,
        )

    # ── Set ──────────────────────────────────────────────────────────────────

    def set(self, target, prop: str, value: Any, **kwargs) -> MutationResult:
        """设置属性或单值配置：name, disclosure, activation_rule, role, lifespan, active, on_fire, sensor_hook, tool_guard。"""
        if prop == "tag":
            raise ValueError(
                "Tag is multi-valued. "
                "Use 'add <concept> tag <tag_name>' to add, "
                "or 'delete <concept> tag <tag_name>' to remove.")
        elif prop == "inhibition":
            raise ValueError(
                "Inhibition is multi-valued. "
                "Use 'add <target> inhibition <inhibitor>' to add, "
                "or 'delete <target> inhibition <inhibitor>' to remove.")
        elif prop == "disclosure":
            return self._set_disclosure(target, str(value))
        elif prop == "name":
            return self._set_name(target, str(value))
        elif prop in ("activation_rule", "activation-rule"):
            return self._set_activation_rule(target, str(value))
        elif prop == "role":
            return self._set_role(
                target, str(value),
                lifespan=kwargs.get("lifespan"),
                activation_rule=kwargs.get("activation_rule") or kwargs.get("activation-rule"),
            )
        elif prop == "lifespan":
            return self._set_lifespan(target, str(value))
        elif prop == "active":
            return self._set_active(target, value)
        elif prop in ("on_fire", "on-fire"):
            return self._set_on_fire(target, str(value) if value is not None else None)
        elif prop in ("sensor_hook", "sensor-hook"):
            return self._set_sensor_hook(
                target,
                event_type=str(value),
                match_pattern=kwargs.get("match_pattern", ""),
                tool=kwargs.get("tool"),
            )
        elif prop in ("tool_guard", "tool-guard"):
            return self._set_tool_guard(
                target,
                tool=str(value),
                args_pattern=kwargs.get("args_pattern"),
            )
        raise ValueError(
            f"Unknown property: '{prop}'. "
            f"Use 'name', 'disclosure', 'activation_rule', 'role', 'lifespan', 'active', 'on_fire', 'sensor_hook', or 'tool_guard'.")

    @transactional
    def _set_active(self, concept, is_active: Any) -> MutationResult:
        """手动切换永久传感器电位（仅限 lifespan == 'permanent'）。"""
        cid = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"

        row = self.conn.execute(
            "SELECT role, lifespan FROM concepts WHERE id = ?", (cid,)
        ).fetchone()
        if not row:
            raise ValueError(f"Concept not found: {concept}")

        if row["role"] != "sensor":
            raise ValueError(
                "只有 permanent 传感器允许手动拨动开关；plain/logic/guard 节点的电位由系统拓扑计算派生，严禁手动设置。"
            )

        if row["lifespan"] != "permanent":
            raise ValueError(
                "session/turn 传感器必须通过 sensor_hooks 自动感知激活，严禁手动点亮；只有 permanent 传感器允许手动拨动开关。"
            )

        truthy = {1, "1", "true", "active", "on"}
        falsy = {0, "0", "false", "inactive", "off"}

        norm_val: Any = is_active
        if isinstance(is_active, bool):
            norm_val = 1 if is_active else 0
        elif isinstance(is_active, str):
            norm_val = is_active.strip().lower()
        elif isinstance(is_active, int):
            norm_val = is_active

        if norm_val in truthy:
            val = 1
        elif norm_val in falsy:
            val = 0
        else:
            raise ValueError(
                f"Invalid active value: {is_active!r}. "
                f"Expected truthy (1, '1', 'true', 'active', 'on') "
                f"or falsy (0, '0', 'false', 'inactive', 'off')."
            )

        self.conn.execute(
            "UPDATE concepts SET is_active = ?, updated_at = ? WHERE id = ?",
            (val, _now(), cid),
        )
        return MutationResult(
            message=f"Success. Set active state of permanent sensor {label} to: {val}.",
            concept_id=cid, concept_name=cname,
        )

    @transactional
    def _set_lifespan(self, concept, lifespan: str) -> MutationResult:
        """设置传感器的生命周期（turn / session / permanent）。"""
        cid = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"

        row = self.conn.execute(
            "SELECT role, lifespan, is_active FROM concepts WHERE id = ?", (cid,)
        ).fetchone()
        if not row or row["role"] != "sensor":
            raise ValueError(
                f"只有 sensor 传感器拥有 lifespan 属性。概念 {label} 的角色为 '{row['role'] if row else 'unknown'}'。"
            )

        ls = lifespan.strip().lower()
        valid_lifespans = ("turn", "session", "permanent")
        if ls not in valid_lifespans:
            raise ValueError(
                f"Invalid lifespan: '{lifespan}'. Must be one of: {', '.join(valid_lifespans)}."
            )

        if ls == "permanent":
            hook = self.conn.execute(
                "SELECT 1 FROM sensor_hooks WHERE sensor_concept_id = ?", (cid,)
            ).fetchone()
            if hook:
                raise ValueError(
                    "永久传感器不可绑定感知钩子；目标传感器已绑定 sensor_hooks，请先解除 Hook 绑定后再设为 permanent。"
                )

        old_ls = row["lifespan"]
        if ls != old_ls and ls in ("turn", "session"):
            self.conn.execute(
                "UPDATE concepts SET lifespan = ?, is_active = 0, updated_at = ? WHERE id = ?",
                (ls, _now(), cid),
            )
        else:
            self.conn.execute(
                "UPDATE concepts SET lifespan = ?, updated_at = ? WHERE id = ?",
                (ls, _now(), cid),
            )

        all_infos: list[str] = []
        if old_ls != ls:
            diff = {"lifespan": {"concept_id": cid, "old": old_ls, "new": ls}}
            all_infos.extend(self._run_mutation_hooks(cid, diff))

        msg = f"Success. Set lifespan of {label} to '{ls}'."
        if all_infos:
            msg += "\n" + "\n".join(all_infos)
        return MutationResult(
            message=msg,
            concept_id=cid, concept_name=cname,
        )

    @transactional
    def _set_on_fire(self, concept, on_fire: str | None) -> MutationResult:
        """配置节点的发火动作 JSON。"""
        cid = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"

        row = self.conn.execute("SELECT role FROM concepts WHERE id = ?", (cid,)).fetchone()
        if not row:
            raise ValueError(f"Concept not found: {concept}")

        clean_on_fire = on_fire.strip() if on_fire and on_fire.strip() else None
        if row["role"] == "plain" and clean_on_fire is not None:
            raise ValueError(f"Plain concept {label} cannot have on_fire actions.")

        self.conn.execute(
            "UPDATE concepts SET on_fire = ?, updated_at = ? WHERE id = ?",
            (clean_on_fire, _now(), cid),
        )
        if clean_on_fire is not None:
            msg = f"Success. Set on_fire action for {label}."
        else:
            msg = f"Success. Cleared on_fire action of {label}."
        return MutationResult(
            message=msg,
            concept_id=cid, concept_name=cname,
        )

    @transactional
    def _set_role(
        self,
        concept,
        new_role: str,
        lifespan: str | None = None,
        activation_rule: str | None = None,
    ) -> MutationResult:
        """角色切换实行原子复合更新（防 CHECK 死锁）。"""
        cid = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"

        valid_roles = ("plain", "sensor", "logic", "guard")
        if new_role not in valid_roles:
            raise ValueError(
                f"Invalid role: '{new_role}'. Must be one of: {', '.join(valid_roles)}."
            )

        row = self.conn.execute(
            "SELECT role, activation_type, lifespan, on_fire, is_active FROM concepts WHERE id = ?", (cid,)
        ).fetchone()

        old_role = row["role"]
        old_lifespan = row["lifespan"]
        old_is_active = row["is_active"]
        had_members = self.conn.execute(
            "SELECT 1 FROM compose_members WHERE parent_concept_id = ?", (cid,)
        ).fetchone() is not None
        had_hooks = self.conn.execute(
            "SELECT 1 FROM sensor_hooks WHERE sensor_concept_id = ?", (cid,)
        ).fetchone() is not None
        had_guards = self.conn.execute(
            "SELECT 1 FROM tool_guards WHERE guard_concept_id = ?", (cid,)
        ).fetchone() is not None
        had_on_fire = row["on_fire"] is not None

        now = _now()
        side_effects: list[str] = []
        all_infos: list[str] = []

        old_member_rows = self.conn.execute(
            "SELECT member_concept_id FROM compose_members WHERE parent_concept_id = ? ORDER BY order_index",
            (cid,),
        ).fetchall()
        old_member_ids = [r["member_concept_id"] for r in old_member_rows]
        old_rule = self._get_activation_rule(cid)

        rule_str = activation_rule
        diff: dict = {}
        affected_members: set[int] = set()

        if new_role == "plain":
            if rule_str is not None and rule_str.strip():
                raise ValueError("Plain concepts cannot have activation rules (in-degree must be 0).")
            if lifespan is not None:
                raise ValueError("Plain concepts cannot have a lifespan.")
            if had_members:
                self.conn.execute("DELETE FROM compose_members WHERE parent_concept_id = ?", (cid,))
                side_effects.append("cleared activation rule")
            if had_hooks:
                self.conn.execute("DELETE FROM sensor_hooks WHERE sensor_concept_id = ?", (cid,))
                side_effects.append("cleared sensor hooks")
            if had_guards:
                self.conn.execute("DELETE FROM tool_guards WHERE guard_concept_id = ?", (cid,))
                side_effects.append("cleared tool guards")
            if had_on_fire:
                side_effects.append("cleared on_fire action")
            self.conn.execute(
                "DELETE FROM inhibitions WHERE target_concept_id = ? OR inhibitor_concept_id = ?",
                (cid, cid),
            )
            self.conn.execute(
                "UPDATE concepts SET role = 'plain', activation_type = NULL, lifespan = NULL, is_active = 0, on_fire = NULL, updated_at = ? "
                "WHERE id = ?",
                (now, cid),
            )
            if old_role != "plain":
                diff["role"] = {"concept_id": cid, "old": old_role, "new": "plain"}
            if old_lifespan is not None:
                diff["lifespan"] = {"concept_id": cid, "old": old_lifespan, "new": None}
            if old_rule is not None or old_member_ids:
                diff["activation-rule"] = {"concept_id": cid, "old": old_rule, "new": None}
                affected_members.update(old_member_ids)
        elif new_role == "sensor":
            if rule_str is not None and rule_str.strip():
                raise ValueError("Sensor nodes cannot have activation rules (in-degree must be 0).")
            if lifespan is not None and lifespan.strip():
                ls = lifespan.strip().lower()
                explicit_lifespan = True
            else:
                ls = old_lifespan if old_role == "sensor" and old_lifespan else "session"
                explicit_lifespan = False

            valid_lifespans = ("turn", "session", "permanent")
            if ls not in valid_lifespans:
                raise ValueError(
                    f"Invalid lifespan: '{lifespan}'. Must be one of: {', '.join(valid_lifespans)}."
                )
            if ls == "permanent" and had_hooks:
                raise ValueError(
                    "永久传感器不可绑定感知钩子；目标传感器已绑定 sensor_hooks，请先解除 Hook 绑定后再设为 permanent。"
                )
            if had_members:
                self.conn.execute("DELETE FROM compose_members WHERE parent_concept_id = ?", (cid,))
                side_effects.append("cleared activation rule")
            if had_guards:
                self.conn.execute("DELETE FROM tool_guards WHERE guard_concept_id = ?", (cid,))
                side_effects.append("cleared tool guards")
            self.conn.execute(
                "DELETE FROM inhibitions WHERE target_concept_id = ?", (cid,)
            )

            if old_role == "sensor" and (not explicit_lifespan or ls == old_lifespan):
                new_is_active = old_is_active
            else:
                new_is_active = 0

            self.conn.execute(
                "UPDATE concepts SET role = 'sensor', activation_type = NULL, lifespan = ?, is_active = ?, updated_at = ? "
                "WHERE id = ?",
                (ls, new_is_active, now, cid),
            )
            if old_role != "sensor" or explicit_lifespan:
                side_effects.append(f"lifespan set to '{ls}'")
            if old_role != "sensor":
                diff["role"] = {"concept_id": cid, "old": old_role, "new": "sensor"}
            if old_lifespan != ls:
                diff["lifespan"] = {"concept_id": cid, "old": old_lifespan, "new": ls}
            if old_rule is not None or old_member_ids:
                diff["activation-rule"] = {"concept_id": cid, "old": old_rule, "new": None}
                affected_members.update(old_member_ids)
        elif new_role in ("logic", "guard"):
            if lifespan is not None:
                raise ValueError(f"Role '{new_role}' cannot have a lifespan.")
            if had_hooks:
                self.conn.execute("DELETE FROM sensor_hooks WHERE sensor_concept_id = ?", (cid,))
                side_effects.append("cleared sensor hooks")
            if new_role == "logic" and had_guards:
                self.conn.execute("DELETE FROM tool_guards WHERE guard_concept_id = ?", (cid,))
                side_effects.append("cleared tool guards")
            if new_role == "guard":
                self.conn.execute(
                    "DELETE FROM inhibitions WHERE inhibitor_concept_id = ?", (cid,)
                )
            if rule_str and rule_str.strip():
                clean_rule = rule_str.strip()
                vtype, member_ids = self._parse_activation_rule(clean_rule)
                if cid in member_ids:
                    raise ValueError("A concept cannot appear in its own activation rule.")
                # 全局激活规则唯一性校验（仅限 logic 节点）
                if new_role == "logic":
                    existing_cid = self._find_composition_concept(vtype, member_ids, role="logic")
                    if existing_cid is not None and existing_cid != cid:
                        exist_name = self._resolve_concept_name(existing_cid)
                        raise ValueError(
                            f"Concept '{exist_name}' (id={existing_cid}) already has the same composition: {clean_rule}"
                        )
                self.conn.execute("DELETE FROM compose_members WHERE parent_concept_id = ?", (cid,))
                for idx, mid in enumerate(member_ids, start=1):
                    self.conn.execute(
                        "INSERT INTO compose_members (parent_concept_id, member_concept_id, order_index) VALUES (?, ?, ?)",
                        (cid, mid, idx),
                    )
                self.conn.execute(
                    "UPDATE concepts SET role = ?, activation_type = ?, lifespan = NULL, is_active = 0, updated_at = ? WHERE id = ?",
                    (new_role, vtype, now, cid),
                )
                side_effects.append(f"activation rule set to: {clean_rule}")

                if old_role != new_role:
                    diff["role"] = {"concept_id": cid, "old": old_role, "new": new_role}
                if old_lifespan is not None:
                    diff["lifespan"] = {"concept_id": cid, "old": old_lifespan, "new": None}
                if old_rule != clean_rule:
                    diff["activation-rule"] = {"concept_id": cid, "old": old_rule, "new": clean_rule}
                affected_members.update(member_ids)
                affected_members.update(old_member_ids)
            else:
                if not row["activation_type"]:
                    raise ValueError(
                        f"Switching role to '{new_role}' requires specifying an activation rule (e.g. set <concept> role {new_role} --activation-rule 'A & B')."
                    )
                self.conn.execute(
                    "UPDATE concepts SET role = ?, lifespan = NULL, is_active = 0, updated_at = ? WHERE id = ?",
                    (new_role, now, cid),
                )
                if old_role != new_role:
                    diff["role"] = {"concept_id": cid, "old": old_role, "new": new_role}
                if old_lifespan is not None:
                    diff["lifespan"] = {"concept_id": cid, "old": old_lifespan, "new": None}
                affected_members.update(old_member_ids)

        if diff:
            all_infos.extend(self._run_mutation_hooks(cid, diff))
            for mid in affected_members:
                all_infos.extend(self._run_mutation_hooks(mid, diff))

        details = f" ({'; '.join(side_effects)})" if side_effects else ""
        msg = f"Success. Switched role of {label} from '{old_role}' to '{new_role}'.{details}"
        if all_infos:
            msg += "\n" + "\n".join(all_infos)
        return MutationResult(
            message=msg,
            concept_id=cid, concept_name=cname,
        )

    @transactional
    def _set_activation_rule(self, concept, activation_rule: str) -> MutationResult:
        """设置/覆盖组合逻辑激活规则（若当前为 plain 则自动提升为 logic）。"""
        if not activation_rule or not activation_rule.strip():
            raise ValueError("Activation rule cannot be empty.")
        cid = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"

        clean_rule = activation_rule.strip()
        vtype, member_ids = self._parse_activation_rule(clean_rule)

        if cid in member_ids:
            raise ValueError("A concept cannot appear in its own activation rule.")

        row = self.conn.execute(
            "SELECT role, activation_type FROM concepts WHERE id = ?", (cid,)
        ).fetchone()

        if row["role"] == "sensor":
            raise ValueError("传感器节点入度恒为 0，严禁定义激活规则上游依赖。")

        orig_role = row["role"]
        target_role = "logic" if orig_role == "plain" else orig_role

        # 全局激活规则唯一性校验（仅限 logic 节点）
        if target_role == "logic":
            existing_cid = self._find_composition_concept(vtype, member_ids, role="logic")
            if existing_cid is not None and existing_cid != cid:
                exist_name = self._resolve_concept_name(existing_cid)
                raise ValueError(
                    f"Concept '{exist_name}' (id={existing_cid}) already has the same composition: {clean_rule}"
                )

        # 获取旧成员用于 hook diff
        old_member_rows = self.conn.execute(
            "SELECT member_concept_id FROM compose_members WHERE parent_concept_id = ? ORDER BY order_index",
            (cid,),
        ).fetchall()
        old_member_ids = [r["member_concept_id"] for r in old_member_rows]
        old_rule = self._get_activation_rule(cid)

        self.conn.execute("DELETE FROM compose_members WHERE parent_concept_id = ?", (cid,))
        for idx, mid in enumerate(member_ids, start=1):
            self.conn.execute(
                "INSERT INTO compose_members (parent_concept_id, member_concept_id, order_index) VALUES (?, ?, ?)",
                (cid, mid, idx),
            )

        now = _now()
        self.conn.execute(
            "UPDATE concepts SET role = ?, activation_type = ?, lifespan = NULL, is_active = 0, updated_at = ? WHERE id = ?",
            (target_role, vtype, now, cid),
        )

        diff: dict = {
            "activation-rule": {
                "concept_id": cid,
                "old": old_rule,
                "new": clean_rule,
            }
        }
        if orig_role != target_role:
            diff["role"] = {"concept_id": cid, "old": orig_role, "new": target_role}

        all_infos: list[str] = []
        all_infos.extend(self._run_mutation_hooks(cid, diff))
        all_affected = set(member_ids) | set(old_member_ids)
        for mid in all_affected:
            all_infos.extend(self._run_mutation_hooks(mid, diff))

        if orig_role == "plain":
            msg = f"Success. Set activation rule of {label} to: {activation_rule}; automatically promoted role from 'plain' to 'logic'."
        else:
            msg = f"Success. Set activation rule of {label} to: {activation_rule}."
        if all_infos:
            msg += "\n" + "\n".join(all_infos)

        return MutationResult(
            message=msg,
            concept_id=cid, concept_name=cname,
        )

    @transactional
    def _set_name(self, concept, new_name: str) -> MutationResult:
        """修改概念显示名。旧显示名降级为 alias。若为 tag 源概念则级联重命名 tag 及插件文件。"""
        new_name = _validate_name(new_name)
        cid = self._resolve_id(concept)
        old_name = self._resolve_concept_name(cid)

        if old_name in SYSTEM_TAGS:
            raise ValueError(
                f"Cannot rename '{old_name}': it is a system-reserved concept."
            )

        if new_name == old_name:
            return MutationResult(
                message=f"Name is already '{new_name}'.",
                concept_id=cid, concept_name=new_name,
            )

        label = f"'{concept}' ('{old_name}', id={cid})"
        self._check_name_available(new_name, exclude_concept_id=cid)

        is_tag_source = self.conn.execute(
            "SELECT 1 FROM tags WHERE source_concept_id=?", (cid,)
        ).fetchone() is not None

        collision = self.conn.execute(
            "SELECT 1 FROM tags WHERE name = ?", (new_name,),
        ).fetchone()
        if collision:
            raise ValueError(
                f"Cannot rename: '{new_name}' is already a registered tag name."
            )

        plugin_renamed = False
        old_plugin_path = tag_sandbox._PLUGINS_DIR / f"{old_name}.py"
        new_plugin_path = tag_sandbox._PLUGINS_DIR / f"{new_name}.py"
        if is_tag_source and old_plugin_path.exists():
            if new_plugin_path.exists() and not os.path.samefile(
                    str(old_plugin_path), str(new_plugin_path)):
                raise ValueError(
                    f"Cannot rename plugin: '{new_name}.py' already exists and is a different file."
                )
            os.rename(str(old_plugin_path), str(new_plugin_path))
            plugin_renamed = True

        try:
            self.conn.execute(
                "UPDATE concepts SET name=?, updated_at=? WHERE id=?",
                (new_name, _now(), cid),
            )
            existing = self.conn.execute(
                "SELECT concept_id FROM aliases WHERE alias=?", (new_name,)
            ).fetchone()
            added_aliases = []
            if not existing:
                self.conn.execute(
                    "INSERT INTO aliases (alias, concept_id) VALUES (?,?)",
                    (new_name, cid),
                )
                added_aliases.append({"concept_id": cid, "name": new_name})

            tag_msg = ""
            if is_tag_source:
                self.conn.execute(
                    "UPDATE tags SET name = ? WHERE name = ?",
                    (new_name, old_name),
                )
                tag_msg += f" Tag '{old_name}' renamed to '{new_name}'."

            if plugin_renamed:
                self._plugin_cache.pop(old_name, None)
                self._plugin_cache.pop(new_name, None)
                tag_msg += f" Plugin file '{old_name}.py' renamed to '{new_name}.py'."

            diff = {}
            if added_aliases:
                diff["names"] = {"added": added_aliases, "removed": []}
            self._run_mutation_hooks(cid, diff)

        except Exception as db_err:
            if plugin_renamed:
                try:
                    os.rename(str(new_plugin_path), str(old_plugin_path))
                except Exception as rollback_err:
                    _logger.error(
                        "PLUGIN FILE INCONSISTENCY: failed to rollback rename %s -> %s: %s",
                        old_plugin_path, new_plugin_path, rollback_err,
                    )
                    raise type(db_err)(
                        f"{db_err} | PLUGIN FILE INCONSISTENCY: database rolled back but file rename could not be reverted."
                    ) from db_err
            raise

        return MutationResult(
            message=f"Success. Renamed {label} to '{new_name}'.{tag_msg}",
            concept_id=cid, concept_name=new_name,
        )

    # ── Update ───────────────────────────────────────────────────────────────

    def get_concept_field(self, concept, field: str) -> tuple[int, str, str | None]:
        """获取 concept 指定字段当前值，供 CLI patch mode 使用。"""
        valid_fields = ("content",)
        if field not in valid_fields:
            raise ValueError(
                f"Unknown field: '{field}'. Use one of: {', '.join(valid_fields)}."
            )
        cid = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        row = self.conn.execute(
            f"SELECT {field} FROM concepts WHERE id=?",
            (cid,),
        ).fetchone()
        return cid, cname, row[field] if row else None

    @transactional
    def update(self, concept, field: str, value: str) -> MutationResult:
        """更新概念正文 content。"""
        valid_fields = ("content",)
        if field not in valid_fields:
            raise ValueError(
                f"Unknown field: '{field}'. Use one of: {', '.join(valid_fields)}."
            )
        cid = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"

        old_row = self.conn.execute(
            "SELECT content FROM concepts WHERE id=?", (cid,)
        ).fetchone()
        old_val = old_row["content"] if old_row else None

        self.conn.execute(
            f"UPDATE concepts SET {field}=?, updated_at=? WHERE id=?",
            (value, _now(), cid),
        )

        diff = {"content": {"concept_id": cid, "old": old_val, "new": value}}
        self._run_mutation_hooks(cid, diff)

        return MutationResult(
            message=f"Success. Updated {field} of {label}.",
            concept_id=cid, concept_name=cname,
        )
