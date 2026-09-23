"""Plugin infrastructure mixin for HoronDB."""
from __future__ import annotations

from typing import Any, Callable

from . import tag_sandbox
from .tag_sandbox import (
    AuditContext,
    ClusterProxy,
    ConceptProxy,
    HookRejection,
    MutationContext,
    TagPluginError,
    load_plugin,
)


class PluginMixin:
    """Proxy factories, mutation hooks, and cluster audit."""

    def _get_plugin(self, tag_name: str) -> dict | None:
        """Load plugin with mtime-based hot-reload. Returns None for pure tags."""
        filepath = tag_sandbox._PLUGINS_DIR / f"{tag_name}.py"

        in_txn = getattr(self, '_in_transaction', False)
        if in_txn and tag_name in self._txn_plugin_snapshot:
            return self._txn_plugin_snapshot[tag_name]

        current_exists = filepath.exists()
        current_mtime = filepath.stat().st_mtime if current_exists else None

        if tag_name in self._plugin_cache:
            cached_plugin, cached_mtime = self._plugin_cache[tag_name]
            if current_mtime == cached_mtime:
                plugin = cached_plugin
            else:
                plugin = load_plugin(tag_name)
                self._plugin_cache[tag_name] = (plugin, current_mtime)
        else:
            plugin = load_plugin(tag_name)
            self._plugin_cache[tag_name] = (plugin, current_mtime)

        if in_txn:
            self._txn_plugin_snapshot[tag_name] = plugin

        return plugin

    def _make_concept_proxy(self, concept_id: int) -> ConceptProxy:
        """Factory: build a lazy ConceptProxy for `concept_id`.

        Input: a concept id (may not exist).
        Behavior: scalar fields (name/content/role/...) come from ONE
        `SELECT * FROM concepts` row, fetched on first access and shared by
        all scalar fetchers; tags / inputs / downstream are separate queries.
        Output: ConceptProxy. For a missing id, scalars fall back to the
        proxy defaults ("" name, "plain" role, 0 is_active, None otherwise)
        and list fields are empty.
        """
        conn = self.conn
        _row_cache: dict[str, Any] = {}

        def row():
            if "row" not in _row_cache:
                _row_cache["row"] = conn.execute(
                    "SELECT * FROM concepts WHERE id=?", (concept_id,)
                ).fetchone()
            return _row_cache["row"]

        def field(col: str, default: Any) -> Callable[[], Any]:
            def fetch():
                r = row()
                return r[col] if r else default
            return fetch

        def fetch_tags():
            rows = conn.execute(
                "SELECT tag FROM concept_tags WHERE concept_id=? ORDER BY tag",
                (concept_id,)
            ).fetchall()
            return [r["tag"] for r in rows]

        def fetch_inputs():
            # Duplicates kept on purpose: CHAIN order and revisits (A -> B -> A) are meaningful.
            rows = conn.execute(
                "SELECT member_concept_id FROM compose_members "
                "WHERE parent_concept_id=? ORDER BY order_index",
                (concept_id,)
            ).fetchall()
            return [self._make_concept_proxy(r["member_concept_id"]) for r in rows]

        def fetch_downstream():
            # DISTINCT: a CHAIN may list this concept at several order_index slots.
            rows = conn.execute(
                "SELECT DISTINCT parent_concept_id FROM compose_members "
                "WHERE member_concept_id=? ORDER BY parent_concept_id",
                (concept_id,)
            ).fetchall()
            return [self._make_concept_proxy(r["parent_concept_id"]) for r in rows]

        return ConceptProxy(
            concept_id,
            fetch_name=field("name", ""),
            fetch_content=field("content", None),
            fetch_role=field("role", "plain"),
            fetch_is_active=field("is_active", 0),
            fetch_lifespan=field("lifespan", None),
            fetch_activation_type=field("activation_type", None),
            fetch_on_fire=field("on_fire", None),
            fetch_disclosure=field("disclosure", None),
            fetch_tags=fetch_tags,
            fetch_inputs=fetch_inputs,
            fetch_downstream=fetch_downstream,
        )

    def _make_cluster_proxy(self, tag_name: str) -> ClusterProxy:
        """Factory: build a ClusterProxy for all concepts carrying tag_name."""
        conn = self.conn

        def fetch_concepts():
            rows = conn.execute(
                "SELECT concept_id FROM concept_tags WHERE tag=? ORDER BY concept_id",
                (tag_name,)
            ).fetchall()
            return [self._make_concept_proxy(r["concept_id"]) for r in rows]

        def fetch_count():
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM concept_tags WHERE tag=?",
                (tag_name,)
            ).fetchone()
            return row["cnt"]

        return ClusterProxy(fetch_concepts=fetch_concepts, fetch_count=fetch_count)

    def _run_mutation_hook_for_tag(self, concept_id: int, tag: str,
                                   changed: dict | None = None,
                                   proxy: ConceptProxy | None = None) -> list[str]:
        """Run on_mutation for a specific tag on a concept."""
        plugin = self._get_plugin(tag)
        if plugin is None:
            return []
        if proxy is None:
            proxy = self._make_concept_proxy(concept_id)
        ctx = MutationContext(tag, proxy, changed, get_concept=self._make_concept_proxy)
        try:
            plugin["on_mutation"](ctx)
        except HookRejection as e:
            raise ValueError(f"[Plugin '{tag}'] {e}")
        return ctx._infos

    def _run_mutation_hooks(self, concept_id: int,
                           changed: dict | None = None) -> list[str]:
        """Run on_mutation for all tags on concept_id. Returns collected infos.

        Raises ValueError (converted from HookRejection) if any plugin rejects.
        """
        tag_rows = self.conn.execute(
            "SELECT tag FROM concept_tags WHERE concept_id=?", (concept_id,)
        ).fetchall()
        all_infos: list[str] = []
        for row in tag_rows:
            all_infos.extend(self._run_mutation_hook_for_tag(concept_id, row["tag"], changed))
        return all_infos

    def audit_cluster(self, tag_name: str) -> list[str]:
        """Run one tag's audit_cluster hook over its concept cluster.

        Input: a tag name. Returns the warning strings the plugin emitted via
        ctx.warn; [] when the cluster is clean or the tag has no plugin. This is
        the executor; audit_all_clusters() renders these into a human report.
        """
        plugin = self._get_plugin(tag_name)
        if plugin is None:
            return []
        ctx = AuditContext(tag_name, self._make_cluster_proxy(tag_name), get_concept=self._make_concept_proxy)
        plugin["audit_cluster"](ctx)
        return ctx._warnings

    def audit_clusters_report(self, tag_name: str | None = None) -> str:
        """Audit tag clusters and return a formatted human report.

        Input: optional tag_name. If provided, audits only that cluster;
        if None, sweeps all plugin-bearing clusters with a coverage footer.
        """
        if tag_name is not None:
            registered = self.conn.execute(
                "SELECT 1 FROM tags WHERE name=?", (tag_name,)
            ).fetchone()
            if registered is None:
                raise ValueError(f"Tag '{tag_name}' is not registered.")
            try:
                plugin = self._get_plugin(tag_name)
                if plugin is None:
                    return f"## {tag_name}\n  (无插件)"
                warnings = self.audit_cluster(tag_name)
            except TagPluginError as e:
                return f"## {tag_name}\n  [插件加载失败] {e}"
            except Exception as e:
                return f"## {tag_name}\n  [审计失败] {e}"
            if not warnings:
                return f"## {tag_name}\n  {tag_name} ✓"
            lines = [f"## {tag_name}"] + [f"  - {w}" for w in warnings]
            return "\n".join(lines)

        rows = self.conn.execute("SELECT name FROM tags").fetchall()
        tags = sorted(r["name"] for r in rows
                      if (tag_sandbox._PLUGINS_DIR / f"{r['name']}.py").exists())
        if not tags:
            return "(没有插件可审计)"
        blocks, summary = [], []
        for tag in tags:
            try:
                warnings = self.audit_cluster(tag)
            except TagPluginError as e:
                blocks.append(f"## {tag}\n  [插件加载失败] {e}")
                summary.append(f"{tag} ⚠load")
                continue
            except Exception as e:
                blocks.append(f"## {tag}\n  [审计失败] {e}")
                summary.append(f"{tag} ⚠err")
                continue
            if warnings:
                blocks.append("\n".join(
                    [f"## {tag}"] + [f"  - {w}" for w in warnings]))
                summary.append(f"{tag} ⚠{len(warnings)}")
            else:
                summary.append(f"{tag} ✓")
        footer = f"— 扫了 {len(tags)} 个带插件的 tag：" + " / ".join(summary) + " —"
        return ("\n\n".join(blocks) + "\n\n" + footer) if blocks else footer
