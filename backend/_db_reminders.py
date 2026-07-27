"""Reminder system mixin for HoronDB."""
from __future__ import annotations

import time

from ._db_common import _validate_condition_ast, _now, transactional
from .models import MutationResult


class ReminderMixin:
    """Reminder CRUD, sandbox condition eval, and inbox."""

    @transactional
    def add_reminder(self, concept: str,
                     condition: str, message: str) -> MutationResult:
        """Create a reminder rule attached to a concept.

        Validates the condition expression via AST whitelist before persisting.
        """
        condition = condition.strip()
        message = message.strip()
        if not condition:
            raise ValueError("Condition cannot be empty.")
        if not message:
            raise ValueError("Message cannot be empty.")

        _validate_condition_ast(condition)

        cid, _ = self._resolve_id(concept)
        cname = self._resolve_concept_name(cid)
        now = _now()
        cursor = self.conn.execute(
            "INSERT INTO reminders "
            "(concept_id, condition, message, created_at) "
            "VALUES (?, ?, ?, ?)",
            (cid, condition, message, now),
        )
        rid = cursor.lastrowid
        return MutationResult(
            message=(
                f"Success. Reminder #{rid} created for "
                f"'{cname}' (id={cid}).\n"
                f"  condition: {condition}\n"
                f"  message: {message}"
            ),
            concept_id=cid, concept_name=cname,
        )

    @transactional
    def delete_reminder(self, reminder_id: int) -> MutationResult:
        row = self.conn.execute(
            "SELECT r.id, r.concept_id, c.name AS concept_name "
            "FROM reminders r "
            "JOIN concepts c ON r.concept_id = c.id "
            "WHERE r.id = ?",
            (reminder_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"Reminder #{reminder_id} not found.")
        self.conn.execute("DELETE FROM reminders WHERE id = ?",
                          (reminder_id,))
        return MutationResult(
            message=f"Success. Reminder #{reminder_id} deleted.",
            concept_id=row["concept_id"],
            concept_name=row["concept_name"],
        )

    def list_reminders(self, limit: int = 50,
                       offset: int = 0) -> list[dict]:
        rows = self.conn.execute(
            "SELECT r.*, c.name AS concept_name "
            "FROM reminders r "
            "JOIN concepts c ON r.concept_id = c.id "
            "ORDER BY r.id LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def _build_sandbox_globals(self) -> dict:
        """Build the restricted globals dict for reminder condition eval."""

        def _exists(name_or_expr: str) -> bool:
            name_or_expr = name_or_expr.strip()
            if any(op in name_or_expr for op in ("\u2192", "&", "|")):
                try:
                    vtype, member_ids = self._parse_expression(
                        name_or_expr, allow_single=False)
                    return self._find_composition_variation(
                        vtype, member_ids) is not None
                except ValueError:
                    return False
            try:
                self._resolve_id(name_or_expr)
                return True
            except ValueError:
                return False

        def _status(name_or_expr: str) -> str | None:
            name_or_expr = name_or_expr.strip()
            if any(op in name_or_expr for op in ("\u2192", "&", "|")):
                try:
                    vtype, member_ids = self._parse_expression(
                        name_or_expr, allow_single=False)
                    result = self._find_composition_variation(
                        vtype, member_ids)
                    if result is None:
                        return None
                    return result[2] or "hypothesis"
                except ValueError:
                    return None
            try:
                cid, _ = self._resolve_id(name_or_expr)
            except ValueError:
                return None
            rows = self.conn.execute(
                "SELECT status FROM variations WHERE concept_id = ?",
                (cid,),
            ).fetchall()
            if not rows:
                return None
            statuses = {r["status"] or "hypothesis" for r in rows}
            if "confirmed" in statuses:
                return "confirmed"
            if "hypothesis" in statuses:
                return "hypothesis"
            return "negated"

        def _tags(concept_name: str) -> set:
            concept_name = concept_name.strip()
            try:
                cid, _ = self._resolve_id(concept_name)
            except ValueError:
                return set()
            rows = self.conn.execute(
                "SELECT tag FROM concept_tags WHERE concept_id = ?",
                (cid,),
            ).fetchall()
            return {r["tag"] for r in rows}

        return {
            "__builtins__": {},
            "exists": _exists,
            "status": _status,
            "tags": _tags,
            "TODAY": time.strftime("%Y-%m-%d"),
            "NOW": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "True": True,
            "False": False,
            "None": None,
        }

    @transactional
    def evaluate_inbox(self) -> dict:
        """Pull all reminders, eval conditions in sandbox, return inbox.

        Returns dict with keys: triggered (list), errors (list),
        quiet_count (int).  Triggered reminders get last_fired_at updated.
        """
        rows = self.conn.execute(
            "SELECT r.*, c.name AS concept_name "
            "FROM reminders r "
            "JOIN concepts c ON r.concept_id = c.id "
            "ORDER BY r.id"
        ).fetchall()
        if not rows:
            return {"triggered": [], "errors": [], "quiet_count": 0}

        sandbox_globals = self._build_sandbox_globals()
        triggered: list[dict] = []
        errors: list[dict] = []
        quiet_count = 0
        fired_ids: list[int] = []

        for row in rows:
            d = dict(row)
            condition = d["condition"]
            try:
                _validate_condition_ast(condition)
            except ValueError as e:
                errors.append({**d, "error": str(e)})
                continue
            try:
                result = eval(
                    compile(condition, "<reminder>", "eval"),
                    sandbox_globals,
                )
            except Exception as e:
                errors.append({**d, "error": f"{type(e).__name__}: {e}"})
                continue

            if result:
                triggered.append(d)
                fired_ids.append(d["id"])
            else:
                quiet_count += 1

        if fired_ids:
            now = _now()
            ph = ",".join("?" * len(fired_ids))
            self.conn.execute(
                f"UPDATE reminders SET last_fired_at = ? "
                f"WHERE id IN ({ph})",
                [now, *fired_ids],
            )

        return {"triggered": triggered, "errors": errors,
                "quiet_count": quiet_count}
