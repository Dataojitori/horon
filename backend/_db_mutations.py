"""Add / Delete / Set / Update mixin for HoronDB."""
from __future__ import annotations

import logging
import os
import secrets

from ._db_common import _validate_name, _now, transactional, SYSTEM_TAGS
from .models import MutationResult
from . import tag_sandbox

_logger = logging.getLogger(__name__)


class MutationMixin:
    """All graph-mutating operations: add, delete, set, update."""

    # ── Add ──────────────────────────────────────────────────────────────────

    def add(self, concept, kind: str, value: str) -> MutationResult:
        """给概念添加 name（别名）、variation（变种）、tag（分类标签）或 disclosure（书腰）。"""
        if kind == "name":
            return self._add_name(concept, value)
        elif kind == "variation":
            return self._add_variation(concept, value)
        elif kind == "tag":
            return self._add_tag(concept, value)
        elif kind == "disclosure":
            return self._add_disclosure(concept, value)
        raise ValueError(
            f"Unknown type: '{kind}'. Use 'name', 'variation', 'tag' or 'disclosure'.")

    @transactional
    def _add_name(self, concept, name: str) -> MutationResult:
        """给 concept 加一个 alias。"""
        name = _validate_name(name)
        cid, _ = self._resolve_id(concept)
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
    def _add_variation(self, concept, expression: str) -> MutationResult:
        """给 concept 新增变种，必须带 expression。"""
        if not expression or not expression.strip():
            raise ValueError("Expression cannot be empty.")
        cid, _ = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"

        vtype, member_ids = self._parse_expression(expression)

        if cid in member_ids:
            raise ValueError(
                "A concept cannot appear in its own expression.")

        existing = self._find_composition_variation(vtype, member_ids)
        if existing is not None:
            exist_cid, exist_sc, _ = existing
            exist_name = self._resolve_concept_name(exist_cid)
            raise ValueError(
                f"Variation '{exist_name}:{exist_sc}' already has "
                f"the same composition: {expression}")

        sc = self._next_short_code(cid)
        now = _now()
        self.conn.execute(
            "INSERT INTO variations "
            "(concept_id, short_code, type, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (cid, sc, vtype, now, now),
        )
        for idx, member_cid in enumerate(member_ids, start=1):
            self.conn.execute(
                "INSERT INTO compose_members "
                "(concept_id, short_code, member_concept_id, order_index) "
                "VALUES (?,?,?,?)",
                (cid, sc, member_cid, idx),
            )

        diff = {"variations": {"added": [
            {"concept_id": cid, "short_code": sc, "type": vtype, "members": member_ids}
        ], "removed": []}}
        
        existing_members = self.conn.execute(
            "SELECT DISTINCT member_concept_id FROM compose_members WHERE concept_id=?",
            (cid,)
        ).fetchall()
        existing_member_ids = [r["member_concept_id"] for r in existing_members]

        all_infos: list[str] = []
        all_infos.extend(self._run_mutation_hooks(cid, diff))
        all_affected = set(member_ids) | set(existing_member_ids)
        for mid in all_affected:
            all_infos.extend(self._run_mutation_hooks(mid, diff))

        msg = f"Success. Added variation {sc} to {label}."
        if all_infos:
            msg += "\n".join([""] + all_infos)

        return MutationResult(
            message=msg,
            concept_id=cid, concept_name=cname, short_code=sc,
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
        cid, _ = self._resolve_id(concept)
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
    def _add_disclosure(self, concept, text: str) -> MutationResult:
        """给概念追加一条 disclosure（书腰）。"""
        text = text.strip()
        if not text:
            raise ValueError("Disclosure text cannot be empty.")
        cid, _ = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"
        now = _now()
        cursor = self.conn.execute(
            "INSERT INTO disclosures (concept_id, text, created_at) "
            "VALUES (?,?,?)",
            (cid, text, now),
        )
        disc_id = cursor.lastrowid
        self.conn.execute(
            "UPDATE concepts SET updated_at=? WHERE id=?", (now, cid))
        diff = {"disclosures": {"added": [{"concept_id": cid, "text": text}], "removed": []}}
        self._run_mutation_hooks(cid, diff)
        return MutationResult(
            message=f"Success. Added disclosure #{disc_id} to {label}: {text}",
            concept_id=cid, concept_name=cname,
        )

    # ── Delete ───────────────────────────────────────────────────────────────

    def delete(self, target, kind: str | None = None,
               value: str | None = None) -> MutationResult:
        """删除操作。

        kind 省略:   删除 variation（target 为概念名或 概念:sc）。
        name:       删除别名（value=要删的别名，必填）。
        expression: 清除组合回原子态（target 为概念名或 概念:sc）。
        tag:        揭掉概念上的 tag（value=要揭的 tag，必填）。
        disclosure: 删除一条书腰（value=disclosure 的 DB id，必填）。
        """
        if kind is None:
            return self._delete_variation(target)
        elif kind == "name":
            if value is None:
                raise ValueError("Specify which name to delete.")
            return self._delete_name(target, value)
        elif kind == "expression":
            if value is not None:
                raise ValueError(
                    f"delete expression takes no extra argument, "
                    f"but got: '{value}'.")
            return self._delete_expression(target)
        elif kind == "tag":
            if value is None:
                raise ValueError("Specify which tag to delete.")
            return self._delete_tag(target, value)
        elif kind == "disclosure":
            if value is None:
                raise ValueError("Specify which disclosure ID to delete.")
            return self._delete_disclosure(target, value)
        raise ValueError(
            f"Unknown type: '{kind}'. "
            f"Use 'name', 'expression', 'tag' or 'disclosure'.")

    @transactional
    def _delete_name(self, concept, name: str) -> MutationResult:
        """删一个 alias。拒删当前显示名。"""
        cid, _ = self._resolve_id(concept)
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
        cid, _ = self._resolve_id(concept)
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
    def _delete_disclosure(self, concept, disc_id_str: str) -> MutationResult:
        """删除一条 disclosure by DB id。"""
        try:
            disc_id = int(disc_id_str)
        except (ValueError, TypeError):
            raise ValueError(
                f"Disclosure ID must be an integer, got: '{disc_id_str}'.")
        cid, _ = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        label = f"'{concept}' ('{cname}', id={cid})"
        row = self.conn.execute(
            "SELECT id, concept_id, text FROM disclosures WHERE id = ?",
            (disc_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"No disclosure with id #{disc_id}.")
        if row["concept_id"] != cid:
            raise ValueError(
                f"Disclosure #{disc_id} belongs to concept "
                f"{row['concept_id']}, not {label}.")
        old_text = row["text"]
        self.conn.execute("DELETE FROM disclosures WHERE id = ?", (disc_id,))
        self.conn.execute(
            "UPDATE concepts SET updated_at=? WHERE id=?", (_now(), cid))
        diff = {"disclosures": {"added": [], "removed": [{"concept_id": cid, "text": old_text}]}}
        self._run_mutation_hooks(cid, diff)
        return MutationResult(
            message=f"Success. Removed disclosure #{disc_id} from {label}.",
            concept_id=cid, concept_name=cname,
        )

    @transactional
    def _delete_variation(self, node) -> MutationResult:
        """删除 variation。最后一个 → concept 也删。

        删除最后一个 variation 会连带删除 concept 本体。
        删除前检查是否有其他 concept 的 variation 引用该 concept
        作为 compose_member；有则拒绝，防止产生残缺的死组合。
        """
        cid, sc = self._resolve_single_variation(node)
        cname = self._resolve_concept_name(cid)
        label = f"'{node}' ('{cname}', id={cid})"

        var_count = self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM variations "
            "WHERE concept_id=?",
            (cid,),
        ).fetchone()["cnt"]

        tag_row = None
        if var_count == 1:
            if cname in SYSTEM_TAGS:
                raise ValueError(
                    f"Cannot delete {label}: it is a system-reserved concept "
                    f"and cannot be deleted.")

            refs = self.conn.execute(
                "SELECT DISTINCT cm.concept_id, cm.short_code, "
                "  c.name "
                "FROM compose_members cm "
                "JOIN concepts c ON cm.concept_id = c.id "
                "WHERE cm.member_concept_id = ? "
                "AND cm.concept_id != ?",
                (cid, cid),
            ).fetchall()
            if refs:
                ref_parts = []
                for r in refs:
                    expr = self._get_expression(
                        r["concept_id"], r["short_code"])
                    line = f"  - '{r['name']}:{r['short_code']}'" \
                           f" (id={r['concept_id']})"
                    if expr:
                        line += f"  [{expr}]"
                    ref_parts.append(line)
                detail = "\n".join(ref_parts)
                raise ValueError(
                    f"Cannot delete {label}: it is still referenced "
                    f"as a compose member by:\n{detail}\n"
                    f"Use read_concept to review them before deciding "
                    f"how to proceed.")

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
                    names = ", ".join(
                        f"'{r['name']}'" for r in other_users)
                    raise ValueError(
                        f"Cannot delete {label}: concept name "
                        f"'{tag_name}' is a registered tag still "
                        f"carried by: {names}.")

        member_rows = self.conn.execute(
            "SELECT member_concept_id FROM compose_members "
            "WHERE concept_id=? AND short_code=? ORDER BY order_index",
            (cid, sc)
        ).fetchall()
        del_member_ids = [r["member_concept_id"] for r in member_rows]
        del_type_row = self.conn.execute(
            "SELECT type FROM variations WHERE concept_id=? AND short_code=?",
            (cid, sc)
        ).fetchone()
        del_type = del_type_row["type"] if del_type_row else None

        self.conn.execute(
            "DELETE FROM variations "
            "WHERE concept_id=? AND short_code=?",
            (cid, sc),
        )

        diff = {"variations": {"added": [], "removed": [
            {"concept_id": cid, "short_code": sc, "type": del_type,
             "members": del_member_ids}
        ]}}
        self._run_mutation_hooks(cid, diff)
        
        if del_member_ids:
            for mid in set(del_member_ids):
                self._run_mutation_hooks(mid, diff)

        downgraded = self.audit_status_integrity()

        remaining = self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM variations WHERE concept_id=?",
            (cid,),
        ).fetchone()

        if remaining["cnt"] == 0:
            self.conn.execute(
                "DELETE FROM concepts WHERE id=?", (cid,))
            if tag_row:
                tag_name_del = tag_row["name"]
                self.conn.execute(
                    "DELETE FROM tags WHERE name = ?",
                    (tag_name_del,))
                plugin_path = tag_sandbox._PLUGINS_DIR / f"{tag_name_del}.py"
                if tag_name_del not in SYSTEM_TAGS and plugin_path.exists():
                    try:
                        plugin_path.unlink()
                    except Exception as e:
                        raise RuntimeError(
                            f"Failed to delete plugin file '{plugin_path}' "
                            f"for removed tag '{tag_name_del}': {e}") from e
                    self._plugin_cache.pop(tag_name_del, None)
            msg = f"Success. Deleted concept {label} (last variation {sc} removed)."
            if tag_row:
                msg += f" Tag '{tag_row['name']}' auto-removed."
        else:
            msg = (f"Success. Deleted variation {sc} from {label}. "
                   f"{remaining['cnt']} variation(s) remaining.")

        if downgraded:
            msg += "\nCascaded downgrades:\n" + "\n".join(f"  - {log}" for log in downgraded)

        return MutationResult(
            message=msg,
            concept_id=cid, concept_name=cname, short_code=sc,
        )

    @transactional
    def _delete_expression(self, node) -> MutationResult:
        """清除 variation 的组合，回到原子态。status 一并清除。"""
        cid, sc = self._resolve_single_variation(node)
        cname = self._resolve_concept_name(cid)
        label = f"'{node}' ('{cname}', id={cid})"

        if not self._get_expression(cid, sc):
            raise ValueError(
                f"Variation {sc} of {label} "
                f"has no expression to delete.")

        existing_atomic = self.conn.execute(
            "SELECT v.short_code FROM variations v "
            "WHERE v.concept_id = ? AND v.short_code != ? "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM compose_members cm "
            "  WHERE cm.concept_id = v.concept_id "
            "  AND cm.short_code = v.short_code"
            ") LIMIT 1",
            (cid, sc),
        ).fetchone()
        if existing_atomic:
            raise ValueError(
                f"Cannot clear expression: variation "
                f"{existing_atomic['short_code']} already has no "
                f"expression. Only one atomic variation allowed "
                f"per concept, because two would be logically "
                f"identical.")

        del_member_rows = self.conn.execute(
            "SELECT member_concept_id FROM compose_members "
            "WHERE concept_id=? AND short_code=? ORDER BY order_index",
            (cid, sc)
        ).fetchall()
        del_member_ids = [r["member_concept_id"] for r in del_member_rows]
        del_type_row = self.conn.execute(
            "SELECT type FROM variations WHERE concept_id=? AND short_code=?",
            (cid, sc)
        ).fetchone()
        del_type = del_type_row["type"] if del_type_row else None

        self.conn.execute(
            "DELETE FROM compose_members "
            "WHERE concept_id=? AND short_code=?",
            (cid, sc),
        )
        self.conn.execute(
            "UPDATE variations SET type=NULL, status=NULL, updated_at=? "
            "WHERE concept_id=? AND short_code=?",
            (_now(), cid, sc),
        )

        if del_member_ids:
            diff = {"variations": {"added": [], "removed": [
                {"concept_id": cid, "short_code": sc, "type": del_type,
                 "members": del_member_ids}
            ]}}
            self._run_mutation_hooks(cid, diff)
            for mid in set(del_member_ids):
                self._run_mutation_hooks(mid, diff)

        downgraded = self.audit_status_integrity()
        msg = (f"Success. Cleared expression and status for "
               f"variation {sc} of {label}.")
        if downgraded:
            msg += "\nCascaded downgrades:\n" + "\n".join(f"  - {log}" for log in downgraded)

        return MutationResult(
            message=msg,
            concept_id=cid, concept_name=cname, short_code=sc,
        )

    # ── Set ──────────────────────────────────────────────────────────────────

    def set(self, target, prop: str, value: str) -> MutationResult:
        """设置属性：status、name（rename）、expression。"""
        if prop == "disclosure":
            raise ValueError(
                "Disclosure is now multi-valued. "
                "Use 'add <concept> disclosure \"text\"' to append, "
                "or 'delete <concept> disclosure <id>' to remove.")
        elif prop == "status":
            return self._set_status(target, value)
        elif prop == "name":
            return self._set_name(target, value)
        elif prop == "expression":
            return self._set_expression(target, value)
        raise ValueError(
            f"Unknown property: '{prop}'. "
            f"Use 'status', 'name', or 'expression'.")

    @transactional
    def _set_status(self, node, value: str) -> MutationResult:
        valid = ("hypothesis", "confirmed", "negated")
        if value not in valid:
            raise ValueError(
                f"Invalid status: '{value}'. "
                f"Must be one of: {', '.join(valid)}.")
        cid, sc = self._resolve_single_variation(node)
        cname = self._resolve_concept_name(cid)
        label = f"'{node}' ('{cname}', id={cid})"

        if value == "confirmed":
            members = self.conn.execute(
                "SELECT member_concept_id FROM compose_members "
                "WHERE concept_id=? AND short_code=?",
                (cid, sc)
            ).fetchall()
            if members:
                vtype_row = self.conn.execute(
                    "SELECT type FROM variations "
                    "WHERE concept_id=? AND short_code=?",
                    (cid, sc)
                ).fetchone()
                vtype = vtype_row["type"] if vtype_row else None
                member_ids = [row["member_concept_id"] for row in members]

                def _member_confirmed(mid: int) -> bool:
                    return self.conn.execute(
                        "SELECT 1 FROM variations "
                        "WHERE concept_id=? AND status='confirmed'",
                        (mid,)
                    ).fetchone() is not None

                if vtype == "OR":
                    if not any(_member_confirmed(mid) for mid in member_ids):
                        names = ", ".join(
                            f"'{self._resolve_concept_name(mid)}'"
                            for mid in member_ids)
                        raise ValueError(
                            f"Cannot confirm OR variation. None of its members "
                            f"({names}) has a confirmed variation.")
                else:
                    for mid in member_ids:
                        if not _member_confirmed(mid):
                            member_name = self._resolve_concept_name(mid)
                            raise ValueError(
                                f"Cannot confirm variation. Member concept "
                                f"'{member_name}' (id={mid}) has no confirmed "
                                f"variations.")

        old_status_row = self.conn.execute(
            "SELECT status FROM variations WHERE concept_id=? AND short_code=?",
            (cid, sc)
        ).fetchone()
        old_status = old_status_row["status"] if old_status_row else None

        self.conn.execute(
            "UPDATE variations SET status=?, updated_at=? "
            "WHERE concept_id=? AND short_code=?",
            (value, _now(), cid, sc),
        )

        downgraded = []
        if value != "confirmed":
            downgraded = self.audit_status_integrity()

        diff = {"status": {"concept_id": cid, "short_code": sc,
                           "old": old_status, "new": value}}
        status_member_rows = self.conn.execute(
            "SELECT member_concept_id FROM compose_members "
            "WHERE concept_id=? AND short_code=?",
            (cid, sc)
        ).fetchall()
        self._run_mutation_hooks(cid, diff)
        member_ids_to_notify = set(r["member_concept_id"] for r in status_member_rows)
        for mid in member_ids_to_notify:
            self._run_mutation_hooks(mid, diff)

        msg = f"Success. Status of {label} variation {sc} set to: {value}"
        if downgraded:
            msg += "\nCascaded downgrades:\n" + "\n".join(f"  - {log}" for log in downgraded)

        return MutationResult(
            message=msg,
            concept_id=cid, concept_name=cname, short_code=sc,
        )

    @transactional
    def audit_status_integrity(self) -> list[str]:
        """
        审计数据库，降级违规的 confirmed 组合变种（即当其子元素没有任何 confirmed 变种时）。
        由于子元素的降级可能导致父元素违规，本方法会循环执行级联降级，直到全库合规。
        返回所有被降级的变种记录信息。
        """
        downgraded_logs = []
        max_iterations = 100
        for _ in range(max_iterations):
            violating_variations = self.conn.execute("""
                -- CHAIN / AND（含兼容的 NULL 类型）：成员缺一不可，
                -- 任一成员没有 confirmed 变体即违规。
                SELECT DISTINCT v.concept_id, v.short_code, c.name
                FROM variations v
                JOIN concepts c ON v.concept_id = c.id
                JOIN compose_members cm ON v.concept_id = cm.concept_id AND v.short_code = cm.short_code
                WHERE v.status = 'confirmed'
                  AND IFNULL(v.type, 'AND') <> 'OR'
                  AND NOT EXISTS (
                      SELECT 1 FROM variations child_v
                      WHERE child_v.concept_id = cm.member_concept_id
                        AND child_v.status = 'confirmed'
                  )
                UNION
                -- OR 是「择一」：只有当所有成员都没有 confirmed 变体时才违规。
                SELECT v.concept_id, v.short_code, c.name
                FROM variations v
                JOIN concepts c ON v.concept_id = c.id
                WHERE v.status = 'confirmed'
                  AND v.type = 'OR'
                  AND NOT EXISTS (
                      SELECT 1 FROM compose_members cm
                      JOIN variations child_v
                        ON child_v.concept_id = cm.member_concept_id
                      WHERE cm.concept_id = v.concept_id
                        AND cm.short_code = v.short_code
                        AND child_v.status = 'confirmed'
                  )
            """).fetchall()

            if not violating_variations:
                break

            for row in violating_variations:
                cid = row["concept_id"]
                sc = row["short_code"]
                cname = row["name"]
                
                self.conn.execute(
                    "UPDATE variations SET status = 'hypothesis', updated_at = ? "
                    "WHERE concept_id = ? AND short_code = ?",
                    (_now(), cid, sc)
                )
                downgraded_logs.append(
                    f"Downgraded '{cname}' (id={cid}, sc={sc}) to 'hypothesis' "
                    f"due to unconfirmed child members."
                )
        else:
            raise RuntimeError(
                f"audit_status_integrity exceeded max_iterations ({max_iterations}). "
                "Possible circular dependency or loop bug detected."
            )

        return downgraded_logs

    @transactional
    def _set_name(self, concept, new_name: str) -> MutationResult:
        """改显示名。旧显示名降级为 alias，保留在名字集合里。
        If old name was a registered tag source, the tag is auto-renamed
        (ON UPDATE CASCADE propagates to concept_tags).
        Also renames the plugin file if present."""
        new_name = _validate_name(new_name)
        cid, _ = self._resolve_id(concept)
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
                f"Cannot rename: '{new_name}' is already a "
                    f"registered tag name.")

        plugin_renamed = False
        old_plugin_path = tag_sandbox._PLUGINS_DIR / f"{old_name}.py"
        new_plugin_path = tag_sandbox._PLUGINS_DIR / f"{new_name}.py"
        if is_tag_source and old_plugin_path.exists():
            if new_plugin_path.exists() and not os.path.samefile(
                    str(old_plugin_path), str(new_plugin_path)):
                raise ValueError(
                    f"Cannot rename plugin: '{new_name}.py' already exists "
                    f"and is a different file.")
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
                tag_msg = f" Tag '{old_name}' renamed to '{new_name}'."

            if plugin_renamed:
                self._plugin_cache.pop(old_name, None)
                self._plugin_cache.pop(new_name, None)

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
                        "PLUGIN FILE INCONSISTENCY: failed to rollback "
                        "rename %s -> %s: %s",
                        old_plugin_path, new_plugin_path, rollback_err)
                    raise type(db_err)(
                        f"{db_err} | PLUGIN FILE INCONSISTENCY: database "
                        f"rolled back but file rename could not be reverted. "
                        f"Manual fix needed: rename '{new_plugin_path}' back "
                        f"to '{old_plugin_path}'."
                    ) from db_err
            raise

        return MutationResult(
            message=f"Success. Renamed {label} to '{new_name}'.{tag_msg}",
            concept_id=cid, concept_name=new_name,
        )

    @transactional
    def _set_expression(self, node, expression: str) -> MutationResult:
        """覆盖 variation 的组合。"""
        if not expression or not expression.strip():
            raise ValueError("Expression cannot be empty.")
        cid, sc = self._resolve_single_variation(node)
        cname = self._resolve_concept_name(cid)
        label = f"'{node}' ('{cname}', id={cid})"

        vtype, member_ids = self._parse_expression(expression)

        if cid in member_ids:
            raise ValueError(
                "A concept cannot appear in its own expression.")

        existing = self._find_composition_variation(vtype, member_ids)
        if existing is not None:
            exist_cid, exist_sc, _ = existing
            if exist_cid != cid or exist_sc != sc:
                exist_name = self._resolve_concept_name(exist_cid)
                raise ValueError(
                    f"Variation '{exist_name}:{exist_sc}' already "
                    f"has the same composition: {expression}")

        old_member_rows = self.conn.execute(
            "SELECT member_concept_id FROM compose_members "
            "WHERE concept_id=? AND short_code=? ORDER BY order_index",
            (cid, sc)
        ).fetchall()
        old_member_ids = [r["member_concept_id"] for r in old_member_rows]
        old_type_row = self.conn.execute(
            "SELECT type FROM variations WHERE concept_id=? AND short_code=?",
            (cid, sc)
        ).fetchone()
        old_type = old_type_row["type"] if old_type_row else None

        self.conn.execute(
            "DELETE FROM compose_members "
            "WHERE concept_id=? AND short_code=?",
            (cid, sc),
        )
        for idx, member_cid in enumerate(member_ids, start=1):
            self.conn.execute(
                "INSERT INTO compose_members "
                "(concept_id, short_code, member_concept_id, order_index) "
                "VALUES (?,?,?,?)",
                (cid, sc, member_cid, idx),
            )
        self.conn.execute(
            "UPDATE variations SET type=?, status=NULL, updated_at=? "
            "WHERE concept_id=? AND short_code=?",
            (vtype, _now(), cid, sc),
        )

        diff: dict = {"variations": {
            "added": [{"concept_id": cid, "short_code": sc, "type": vtype, "members": member_ids}],
            "removed": [],
        }}
        if old_member_ids:
            diff["variations"]["removed"].append(
                {"concept_id": cid, "short_code": sc, "type": old_type, "members": old_member_ids})

        all_infos: list[str] = []
        all_infos.extend(self._run_mutation_hooks(cid, diff))
        all_affected = set(member_ids) | set(old_member_ids)
        for mid in all_affected:
            all_infos.extend(self._run_mutation_hooks(mid, diff))

        downgraded = self.audit_status_integrity()
        msg = (f"Success. Expression of {label} variation {sc} "
               f"set to: {expression}. "
               f"Status was also reset to null.")
        if downgraded:
            msg += "\nCascaded downgrades:\n" + "\n".join(f"  - {log}" for log in downgraded)
        if all_infos:
            msg += "\n".join([""] + all_infos)

        return MutationResult(
            message=msg,
            concept_id=cid, concept_name=cname, short_code=sc,
        )

    # ── Update ───────────────────────────────────────────────────────────────

    def get_variation_field(self, node, field: str) -> tuple[int, str, str | None]:
        """获取 variation 指定字段的当前值，供 CLI 层 patch mode 使用。避免 CLI 重复解析。"""
        valid_fields = ("content",)
        if field not in valid_fields:
            raise ValueError(
                f"Unknown field: '{field}'. "
                f"Use one of: {', '.join(valid_fields)}.")
        concept_id, sc = self._resolve_single_variation(node)
        row = self.conn.execute(
            f"SELECT {field} FROM variations WHERE concept_id=? AND short_code=?",
            (concept_id, sc),
        ).fetchone()
        return concept_id, sc, row[field]

    @transactional
    def update(self, node, field: str, value: str) -> MutationResult:
        """给 variation 写 content（patch/append 由 CLI 层处理）。

        node: concept 名/ID，或 "concept:short_code"。
        sole variation 时自动定位。
        """
        valid_fields = ("content",)
        if field not in valid_fields:
            raise ValueError(
                f"Unknown field: '{field}'. "
                f"Use one of: {', '.join(valid_fields)}.")
        concept_id, sc = self._resolve_single_variation(node)
        cname = self._resolve_concept_name(concept_id)
        label = f"'{node}' ('{cname}', id={concept_id})"
        old_val = None
        if field == "content":
            old_row = self.conn.execute(
                "SELECT content FROM variations "
                "WHERE concept_id=? AND short_code=?",
                (concept_id, sc)
            ).fetchone()
            old_val = old_row["content"] if old_row else None
        self.conn.execute(
            f"UPDATE variations SET {field}=?, updated_at=? "
            f"WHERE concept_id=? AND short_code=?",
            (value, _now(), concept_id, sc),
        )
        if field == "content":
            diff = {"content": {"concept_id": concept_id, "short_code": sc,
                                "old": old_val, "new": value}}
            self._run_mutation_hooks(concept_id, diff)
        return MutationResult(
            message=f"Success. Updated {field} of variation {sc} of {label}.",
            concept_id=concept_id, concept_name=cname, short_code=sc,
        )
