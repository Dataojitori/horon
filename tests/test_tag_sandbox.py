"""Tag plugin system tests (tag_sandbox.py + db.py plugin integration)."""
import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import backend.db as db_module
from backend.tag_sandbox import (
    load_plugin, TagPluginError, HookRejection,
    _PLUGINS_DIR, _validate_ast,
    ConceptProxy, VariationProxy, ClusterProxy,
    MutationContext, AuditContext,
)


@pytest.fixture
def horon_db(tmp_path, monkeypatch):
    db_path = tmp_path / "horon-test.db"
    monkeypatch.setattr(db_module, "_DB_PATH", db_path)
    db = db_module.HoronDB()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def plugin_dir(tmp_path, monkeypatch):
    """Redirects plugin loading to a temp directory."""
    plugins = tmp_path / "tag_plugins"
    plugins.mkdir()
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugins)
    return plugins


# ── 1-6: AST validation and loading ─────────────────────────────────────────

def test_ast_rejects_import(plugin_dir):
    (plugin_dir / "bad.py").write_text(
        "import os\ndef on_mutation(ctx): pass\ndef audit_cluster(ctx): pass\n")
    with pytest.raises(TagPluginError) as exc:
        load_plugin("bad")
    assert "import" in str(exc.value).lower()


def test_ast_rejects_eval_call(plugin_dir):
    (plugin_dir / "bad2.py").write_text(
        "def on_mutation(ctx): eval('1+1')\ndef audit_cluster(ctx): pass\n")
    with pytest.raises(TagPluginError) as exc:
        load_plugin("bad2")
    assert "eval" in str(exc.value)


def test_missing_audit_cluster_rejected(plugin_dir):
    (plugin_dir / "half.py").write_text("def on_mutation(ctx): pass\n")
    with pytest.raises(TagPluginError) as exc:
        load_plugin("half")
    assert "audit_cluster" in str(exc.value)


def test_missing_on_mutation_rejected(plugin_dir):
    (plugin_dir / "half2.py").write_text("def audit_cluster(ctx): pass\n")
    with pytest.raises(TagPluginError) as exc:
        load_plugin("half2")
    assert "on_mutation" in str(exc.value)


def test_misspelled_function_rejected(plugin_dir):
    (plugin_dir / "typo.py").write_text(
        "def on_mutate(ctx): pass\ndef audit_cluster(ctx): pass\n")
    with pytest.raises(TagPluginError) as exc:
        load_plugin("typo")
    assert "on_mutation" in str(exc.value)


def test_valid_plugin_loads(plugin_dir):
    (plugin_dir / "good.py").write_text(
        "HELPER = 42\ndef on_mutation(ctx): pass\ndef audit_cluster(ctx): pass\n")
    plugin = load_plugin("good")
    assert plugin is not None
    assert callable(plugin["on_mutation"])
    assert callable(plugin["audit_cluster"])


# ── 7-10: Context method behavior ───────────────────────────────────────────

def test_ctx_reject_in_on_mutation(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    (plugin_dir / "blocker.py").write_text(
        "def on_mutation(ctx): ctx.reject('nope')\n"
        "def audit_cluster(ctx): pass\n")
    horon_db.conn.execute("INSERT INTO tags (name, source_concept_id) VALUES ('blocker', NULL)")
    horon_db.conn.commit()

    horon_db.create_concept("victim")
    with pytest.raises(ValueError, match="nope"):
        horon_db._add_tag("victim", "blocker")


def test_ctx_info_appears_in_result(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    (plugin_dir / "advisor.py").write_text(
        "def on_mutation(ctx): ctx.info('helpful hint')\n"
        "def audit_cluster(ctx): pass\n")
    horon_db.conn.execute("INSERT INTO tags (name, source_concept_id) VALUES ('advisor', NULL)")
    horon_db.conn.commit()

    horon_db.create_concept("target")
    result = horon_db._add_tag("target", "advisor")
    assert "helpful hint" in result.message


def test_ctx_warn_in_audit(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    (plugin_dir / "checker.py").write_text(
        "def on_mutation(ctx): pass\n"
        "def audit_cluster(ctx): ctx.warn('issue found')\n")
    horon_db.conn.execute("INSERT INTO tags (name, source_concept_id) VALUES ('checker', NULL)")
    horon_db.conn.commit()

    horon_db.create_concept("audited")
    horon_db._add_tag("audited", "checker")
    warnings = horon_db.audit_cluster("checker")
    assert "issue found" in warnings[0]


def test_reject_in_audit_raises_runtime_error():
    cluster = ClusterProxy(fetch_concepts=lambda: [], fetch_count=lambda: 0)
    ctx = AuditContext("test", cluster)
    with pytest.raises(RuntimeError, match="reject.*not available"):
        ctx.reject("bad")


# ── 11: Lazy loading verification ───────────────────────────────────────────

def test_lazy_proxy_fetches_on_access(horon_db):
    horon_db.create_concept("lazy_test")
    cid = horon_db._resolve_id("lazy_test")[0]
    proxy = horon_db._make_concept_proxy(cid)
    # No DB query until property access
    assert proxy.concept_id == cid
    assert proxy.name == "lazy_test"
    # Second access uses cache
    assert proxy.name == "lazy_test"


# ── 12: No plugin file returns None ─────────────────────────────────────────

def test_no_plugin_file_returns_none(plugin_dir):
    assert load_plugin("nonexistent") is None


# ── 13: Plugin caching ──────────────────────────────────────────────────────

def test_get_plugin_caches(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    (plugin_dir / "cached.py").write_text(
        "def on_mutation(ctx): pass\ndef audit_cluster(ctx): pass\n")
    p1 = horon_db._get_plugin("cached")
    p2 = horon_db._get_plugin("cached")
    assert p1 is p2


# ── 14: Member concept hooks are triggered ──────────────────────────────────

def test_member_hooks_triggered(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    (plugin_dir / "watchdog.py").write_text(
        "def on_mutation(ctx):\n"
        "    for v in ctx.changed.get('variations', {}).get('added', []):\n"
        "        if ctx.this.concept_id in v.get('members', []):\n"
        "            ctx.info('member notified')\n"
        "def audit_cluster(ctx): pass\n")
    horon_db.conn.execute("INSERT INTO tags (name, source_concept_id) VALUES ('watchdog', NULL)")
    horon_db.conn.commit()

    horon_db.create_concept("A")
    horon_db.create_concept("B")
    horon_db._add_tag("A", "watchdog")
    horon_db.create_concept("relation")
    result = horon_db._set_expression("relation", "A → B")
    assert "member notified" in result.message


# ── 15: Member rejection ────────────────────────────────────────────────────

def test_member_reject_aborts_operation(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    (plugin_dir / "guardian.py").write_text(
        "def on_mutation(ctx):\n"
        "    for v in ctx.changed.get('variations', {}).get('added', []):\n"
        "        if ctx.this.concept_id in v.get('members', []):\n"
        "            ctx.reject('guardian says no')\n"
        "def audit_cluster(ctx): pass\n")
    horon_db.conn.execute("INSERT INTO tags (name, source_concept_id) VALUES ('guardian', NULL)")
    horon_db.conn.commit()

    horon_db.create_concept("Protected")
    horon_db._add_tag("Protected", "guardian")
    horon_db.create_concept("Other")
    horon_db.create_concept("link")
    with pytest.raises(ValueError, match="guardian says no"):
        horon_db._set_expression("link", "Protected → Other")


# ── 16: Diff passing ────────────────────────────────────────────────────────

def test_diff_passed_on_add_tag(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    (plugin_dir / "inspector.py").write_text(
        "def on_mutation(ctx):\n"
        "    tags_diff = ctx.changed.get('tags', {})\n"
        "    if tags_diff.get('added'):\n"
        "        ctx.info('saw_tags_added')\n"
        "def audit_cluster(ctx): pass\n")
    horon_db.conn.execute("INSERT INTO tags (name, source_concept_id) VALUES ('inspector', NULL)")
    horon_db.conn.commit()

    horon_db.create_concept("X")
    result = horon_db._add_tag("X", "inspector")
    assert "saw_tags_added" in result.message


def test_diff_passed_on_set_expression(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    (plugin_dir / "vardiff.py").write_text(
        "def on_mutation(ctx):\n"
        "    added = ctx.changed.get('variations', {}).get('added', [])\n"
        "    for v in added:\n"
        "        if v['concept_id'] == ctx.this.concept_id:\n"
        "            ctx.info(f\"got_var_{v['type']}\")\n"
        "def audit_cluster(ctx): pass\n")
    horon_db.conn.execute("INSERT INTO tags (name, source_concept_id) VALUES ('vardiff', NULL)")
    horon_db.conn.commit()

    horon_db.create_concept("P")
    horon_db.create_concept("Q")
    horon_db.create_concept("R")
    horon_db._add_tag("R", "vardiff")
    result = horon_db._set_expression("R", "P → Q")
    assert "got_var_CHAIN" in result.message


# ── 17: One-time hint doesn't repeat ────────────────────────────────────────

def test_plan_first_chain_hint_only_once(horon_db):
    horon_db.create_concept("步骤1")
    horon_db.create_concept("步骤2")
    horon_db.create_concept("步骤3")
    res1 = horon_db.init_plan("我的计划")
    res2 = horon_db._set_expression("我的计划", "步骤1 → 步骤2")
    assert "ACTION REQUIRED" in res2.message

    horon_db.create_concept("extra_relation")
    res3 = horon_db._set_expression("extra_relation", "我的计划 → 步骤3")
    # The first-chain hint should NOT appear again
    assert "你已经为计划确立了执行步骤" not in res3.message


# ── 18: Delete variation hooks ───────────────────────────────────────────────

def test_delete_variation_hooks_members(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    (plugin_dir / "delsense.py").write_text(
        "def on_mutation(ctx):\n"
        "    removed = ctx.changed.get('variations', {}).get('removed', [])\n"
        "    if removed:\n"
        "        ctx.reject('cannot remove my chain')\n"
        "def audit_cluster(ctx): pass\n")
    horon_db.conn.execute("INSERT INTO tags (name, source_concept_id) VALUES ('delsense', NULL)")
    horon_db.conn.commit()

    horon_db.create_concept("M1")
    horon_db.create_concept("M2")
    horon_db._add_tag("M1", "delsense")
    horon_db.create_concept("chain_holder")
    set_res = horon_db._set_expression("chain_holder", "M1 → M2")
    first_sc = set_res.short_code
    # Adding a second variation so deletion doesn't cascade to concept removal
    horon_db.add("chain_holder", "variation", "M2 → M1")

    with pytest.raises(ValueError, match="cannot remove my chain"):
        horon_db._delete_variation(f"chain_holder:{first_sc}")


# ── 19: Removed tag's last say ──────────────────────────────────────────────

def test_removed_tag_plugin_gets_last_say(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    (plugin_dir / "sticky.py").write_text(
        "def on_mutation(ctx):\n"
        "    removed = ctx.changed.get('tags', {}).get('removed', [])\n"
        "    for t in removed:\n"
        "        if t['tag'] == 'sticky':\n"
        "            ctx.reject('sticky tag cannot be removed')\n"
        "def audit_cluster(ctx): pass\n")
    horon_db.conn.execute("INSERT INTO tags (name, source_concept_id) VALUES ('sticky', NULL)")
    horon_db.conn.commit()

    horon_db.create_concept("stuck")
    horon_db._add_tag("stuck", "sticky")
    with pytest.raises(ValueError, match="sticky tag cannot be removed"):
        horon_db._delete_tag("stuck", "sticky")


# ── 20: Post-INSERT broadcast ────────────────────────────────────────────────

def test_add_tag_broadcasts_to_all_tags(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    (plugin_dir / "existingtag.py").write_text(
        "def on_mutation(ctx):\n"
        "    added = ctx.changed.get('tags', {}).get('added', [])\n"
        "    for t in added:\n"
        "        if t['tag'] != 'existingtag':\n"
        "            ctx.info(f\"saw_new_tag_{t['tag']}\")\n"
        "def audit_cluster(ctx): pass\n")
    (plugin_dir / "newtag.py").write_text(
        "def on_mutation(ctx):\n"
        "    if 'newtag' in ctx.this.tags:\n"
        "        ctx.info('newtag_sees_itself')\n"
        "def audit_cluster(ctx): pass\n")
    horon_db.conn.execute("INSERT INTO tags (name, source_concept_id) VALUES ('existingtag', NULL)")
    horon_db.conn.execute("INSERT INTO tags (name, source_concept_id) VALUES ('newtag', NULL)")
    horon_db.conn.commit()

    horon_db.create_concept("C")
    horon_db._add_tag("C", "existingtag")
    result = horon_db._add_tag("C", "newtag")
    assert "saw_new_tag_newtag" in result.message
    assert "newtag_sees_itself" in result.message


# ── 21: Rename invalidates cache ────────────────────────────────────────────

def test_rename_invalidates_plugin_cache(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    (plugin_dir / "oldname.py").write_text(
        "def on_mutation(ctx): ctx.info('old_version')\n"
        "def audit_cluster(ctx): pass\n")

    horon_db.create_concept("oldname")
    horon_db.create_tag("oldname")
    # Warm up cache
    p = horon_db._get_plugin("oldname")
    assert p is not None
    # Also cache "newname" as None
    horon_db._get_plugin("newname")

    horon_db.set("oldname", "name", "newname")

    # After rename, new name should load the renamed file
    p_new = horon_db._get_plugin("newname")
    assert p_new is not None
    # Old name should return None
    p_old = horon_db._get_plugin("oldname")
    assert p_old is None


# ── 22: Mtime hot-reload — edit ─────────────────────────────────────────────

def test_mtime_hot_reload_edit(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    plugin_path = plugin_dir / "hotfoo.py"
    plugin_path.write_text(
        "def on_mutation(ctx): ctx.info('v1')\n"
        "def audit_cluster(ctx): pass\n")

    p1 = horon_db._get_plugin("hotfoo")
    assert p1 is not None

    time.sleep(0.05)
    plugin_path.write_text(
        "def on_mutation(ctx): ctx.info('v2')\n"
        "def audit_cluster(ctx): pass\n")

    p2 = horon_db._get_plugin("hotfoo")
    assert p2 is not p1


# ── 23: Mtime hot-reload — create ───────────────────────────────────────────

def test_mtime_hot_reload_create(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)

    assert horon_db._get_plugin("brandnew") is None
    (plugin_dir / "brandnew.py").write_text(
        "def on_mutation(ctx): pass\ndef audit_cluster(ctx): pass\n")
    assert horon_db._get_plugin("brandnew") is not None


# ── 24: Mtime hot-reload — delete ───────────────────────────────────────────

def test_mtime_hot_reload_delete(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    plugin_path = plugin_dir / "ephemeral.py"
    plugin_path.write_text(
        "def on_mutation(ctx): pass\ndef audit_cluster(ctx): pass\n")

    assert horon_db._get_plugin("ephemeral") is not None
    plugin_path.unlink()
    assert horon_db._get_plugin("ephemeral") is None


# ── 25: Transaction-internal consistency ─────────────────────────────────────

def test_no_reload_within_transaction(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    plugin_path = plugin_dir / "txtest.py"
    plugin_path.write_text(
        "V = 'original'\ndef on_mutation(ctx): ctx.info(V)\n"
        "def audit_cluster(ctx): pass\n")
    horon_db.conn.execute("INSERT INTO tags (name, source_concept_id) VALUES ('txtest', NULL)")
    horon_db.conn.commit()

    horon_db.create_concept("TxConcept")
    horon_db._get_plugin("txtest")  # warm cache

    # Simulate being inside a transaction
    @db_module.transactional
    def run_txn(self):
        time.sleep(0.05)
        plugin_path.write_text(
            "V = 'modified'\ndef on_mutation(ctx): ctx.info(V)\n"
            "def audit_cluster(ctx): pass\n")
        p = self._get_plugin("txtest")
        # Should still be the original
        assert p is not None
        return p

    p = run_txn(horon_db)

    # Outside transaction, reload happens
    p2 = horon_db._get_plugin("txtest")
    assert p2 is not p


# ── 26: Exception class whitelist ────────────────────────────────────────────

def test_exception_class_whitelist(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    (plugin_dir / "raiser.py").write_text(
        "def on_mutation(ctx):\n"
        "    raise ValueError('plugin says no')\n"
        "def audit_cluster(ctx): pass\n")
    horon_db.conn.execute("INSERT INTO tags (name, source_concept_id) VALUES ('raiser', NULL)")
    horon_db.conn.commit()

    horon_db.create_concept("exc_test")
    # ValueError from plugin should propagate (not NameError)
    with pytest.raises(ValueError, match="plugin says no"):
        horon_db._add_tag("exc_test", "raiser")


def test_try_except_in_plugin(plugin_dir):
    (plugin_dir / "trier.py").write_text(
        "def on_mutation(ctx):\n"
        "    try:\n"
        "        x = int('not_a_number')\n"
        "    except ValueError:\n"
        "        ctx.info('caught_valueerror')\n"
        "def audit_cluster(ctx): pass\n")
    plugin = load_plugin("trier")
    proxy = ConceptProxy(1,
        fetch_name=lambda: "test",
        fetch_disclosure=lambda: None,
        fetch_tags=lambda: ["trier"],
        fetch_variations=lambda: [],
        fetch_used_in_variations=lambda: [])
    ctx = MutationContext("trier", proxy, {"tags": {"added": [{"concept_id": 1, "tag": "trier"}], "removed": []}})
    plugin["on_mutation"](ctx)
    assert "caught_valueerror" in ctx._infos


# ── 27: Rename rollback failure message ──────────────────────────────────────

def test_rename_rollback_failure_visible(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    (plugin_dir / "renametest.py").write_text(
        "def on_mutation(ctx): pass\ndef audit_cluster(ctx): pass\n")

    horon_db.create_concept("renametest")
    horon_db.create_tag("renametest")
    horon_db._get_plugin("renametest")  # warm cache

    # Mock os.rename: first call succeeds (file rename), second call (rollback) fails
    rename_calls = []
    original_rename = os.rename
    def mock_rename(src, dst):
        rename_calls.append((src, dst))
        if len(rename_calls) == 1:
            # First rename succeeds (forward)
            return original_rename(src, dst)
        else:
            # Rollback rename fails
            raise OSError("simulated rollback failure")

    monkeypatch.setattr(os, "rename", mock_rename)

    # Force the DB write to fail by making the concepts table read-only
    # Use a trigger that fails on UPDATE
    horon_db.conn.execute(
        "CREATE TRIGGER force_fail_rename BEFORE UPDATE OF name ON concepts "
        "BEGIN SELECT RAISE(ABORT, 'simulated DB failure'); END")
    horon_db.conn.commit()

    with pytest.raises(Exception) as exc:
        horon_db._set_name("renametest", "newname")
    msg = str(exc.value)
    assert "PLUGIN FILE INCONSISTENCY" in msg


# ── 28-30: Sandbox escape interception ───────────────────────────────────────

def test_ast_blocks_closure_access(plugin_dir):
    (plugin_dir / "escape1.py").write_text(
        "def on_mutation(ctx):\n"
        "    x = ctx.this._fetch_name.__closure__\n"
        "def audit_cluster(ctx): pass\n")
    with pytest.raises(TagPluginError) as exc:
        load_plugin("escape1")
    assert "__closure__" in str(exc.value)


def test_ast_blocks_class_mro(plugin_dir):
    (plugin_dir / "escape2.py").write_text(
        "def on_mutation(ctx):\n"
        "    ctx.this.__class__.__mro__[-1].__subclasses__()\n"
        "def audit_cluster(ctx): pass\n")
    with pytest.raises(TagPluginError) as exc:
        load_plugin("escape2")
    assert "__class__" in str(exc.value) or "__mro__" in str(exc.value)


def test_ast_blocks_dunder_write(plugin_dir):
    (plugin_dir / "escape3.py").write_text(
        "def on_mutation(ctx):\n"
        "    ctx.this.__dict__['concept_id'] = 999\n"
        "def audit_cluster(ctx): pass\n")
    with pytest.raises(TagPluginError) as exc:
        load_plugin("escape3")
    assert "__dict__" in str(exc.value)


# ── 31: Proxy doesn't hold connection ───────────────────────────────────────

def test_proxy_no_connection(horon_db):
    horon_db.create_concept("proxytest")
    cid = horon_db._resolve_id("proxytest")[0]
    proxy = horon_db._make_concept_proxy(cid)
    # Check that no attribute on the instance is a Connection
    for val in vars(proxy).values():
        assert not isinstance(val, sqlite3.Connection)


# ── 32: Single underscore not blocked ────────────────────────────────────────

def test_single_underscore_allowed(plugin_dir):
    (plugin_dir / "singleus.py").write_text(
        "class _Helper:\n"
        "    _value = 42\n"
        "def on_mutation(ctx):\n"
        "    h = _Helper()\n"
        "    x = h._value\n"
        "def audit_cluster(ctx): pass\n")
    plugin = load_plugin("singleus")
    assert plugin is not None


# ── 33: Source concept deletion cleans plugin ────────────────────────────────

def test_source_concept_deletion_cleans_plugin(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    (plugin_dir / "mytag.py").write_text(
        "def on_mutation(ctx): pass\ndef audit_cluster(ctx): pass\n")

    horon_db.create_concept("mytag")
    horon_db.create_tag("mytag")
    # Verify file exists
    assert (plugin_dir / "mytag.py").exists()

    # Delete the concept (triggers _delete_variation → concept removal → tag removal)
    horon_db._delete_variation("mytag")

    assert not (plugin_dir / "mytag.py").exists()
    assert "mytag" not in horon_db._plugin_cache


# ── 34: Unlink failure rolls back DB ────────────────────────────────────────

def test_unlink_failure_rolls_back(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    (plugin_dir / "cantdel.py").write_text(
        "def on_mutation(ctx): pass\ndef audit_cluster(ctx): pass\n")

    horon_db.create_concept("cantdel")
    horon_db.create_tag("cantdel")

    # Mock unlink to fail
    def fail_unlink(self):
        raise PermissionError("simulated permission denied")

    monkeypatch.setattr(Path, "unlink", fail_unlink)

    with pytest.raises(RuntimeError) as exc:
        horon_db._delete_variation("cantdel")
    assert "cantdel" in str(exc.value)

    # DB should have rolled back - concept and tag still exist
    row = horon_db.conn.execute(
        "SELECT 1 FROM tags WHERE name='cantdel'").fetchone()
    assert row is not None
    row = horon_db.conn.execute(
        "SELECT 1 FROM concepts WHERE name='cantdel'").fetchone()
    assert row is not None


# ── 35: System tag immune to cleanup ────────────────────────────────────────

def test_system_tag_immune_to_cleanup(horon_db):
    horon_db.create_concept("plan")
    with pytest.raises(ValueError, match="system-reserved"):
        horon_db._delete_variation("plan")
    # plan.py must still exist in real plugins dir
    assert (_PLUGINS_DIR / "plan.py").exists()


# ── 36: Reject discards infos ───────────────────────────────────────────────

def test_reject_discards_infos(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    (plugin_dir / "hinter.py").write_text(
        "def on_mutation(ctx):\n"
        "    for v in ctx.changed.get('variations', {}).get('added', []):\n"
        "        if ctx.this.concept_id in v.get('members', []):\n"
        "            ctx.info('some hint')\n"
        "def audit_cluster(ctx): pass\n")
    (plugin_dir / "blocker2.py").write_text(
        "def on_mutation(ctx):\n"
        "    for v in ctx.changed.get('variations', {}).get('added', []):\n"
        "        if ctx.this.concept_id in v.get('members', []):\n"
        "            ctx.reject('blocked')\n"
        "def audit_cluster(ctx): pass\n")
    horon_db.conn.execute("INSERT INTO tags (name, source_concept_id) VALUES ('hinter', NULL)")
    horon_db.conn.execute("INSERT INTO tags (name, source_concept_id) VALUES ('blocker2', NULL)")
    horon_db.conn.commit()

    horon_db.create_concept("A")
    horon_db.create_concept("B")
    horon_db.create_concept("C")
    horon_db._add_tag("A", "hinter")
    horon_db._add_tag("C", "blocker2")
    horon_db.create_concept("chain")

    with pytest.raises(ValueError) as exc:
        horon_db._set_expression("chain", "A → B → C")
    msg = str(exc.value)
    assert "blocked" in msg
    assert "some hint" not in msg


# ── 37: Case-only rename ────────────────────────────────────────────────────

def test_case_only_rename_allowed(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    (plugin_dir / "mytag2.py").write_text(
        "def on_mutation(ctx): pass\ndef audit_cluster(ctx): pass\n")

    horon_db.create_concept("mytag2")
    horon_db.create_tag("mytag2")

    # Case-only rename should succeed (not reject due to "file exists")
    result = horon_db.set("mytag2", "name", "MyTag2")
    assert "Renamed" in result.message

    # Old name cache should be invalidated
    assert "mytag2" not in horon_db._plugin_cache


def test_audit_clusters_report_catches_plugin_load_error(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    # 先写入合法插件完成建 tag
    (plugin_dir / "brokentag.py").write_text(
        "def on_mutation(ctx): pass\ndef audit_cluster(ctx): pass\n"
    )
    horon_db.create_concept("brokentag")
    horon_db.create_tag("brokentag")

    # 模拟后续修改插件文件时写入语法错误
    (plugin_dir / "brokentag.py").write_text("def invalid python syntax !!!")

    report = horon_db.audit_clusters_report("brokentag")
    assert "## brokentag" in report
    assert "[插件加载失败]" in report


def test_audit_clusters_report_rejects_unregistered_tag_with_plugin(
        horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    (plugin_dir / "orphan.py").write_text(
        "def on_mutation(ctx): pass\ndef audit_cluster(ctx): pass\n"
    )

    with pytest.raises(ValueError, match="Tag 'orphan' is not registered"):
        horon_db.audit_clusters_report("orphan")


def test_audit_clusters_report_sweep_catches_runtime_error(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    # 写入一个加载正常但运行时 raise 的插件
    (plugin_dir / "errortag.py").write_text(
        "def on_mutation(ctx): pass\ndef audit_cluster(ctx): raise RuntimeError('boom')\n"
    )
    horon_db.create_concept("errortag")
    horon_db.create_tag("errortag")

    report = horon_db.audit_clusters_report()
    assert "## errortag" in report
    assert "[审计失败]" in report
    assert "errortag ⚠err" in report


def test_audit_clusters_report_sweep_ignores_unregistered_tags(horon_db, plugin_dir, monkeypatch):
    import backend.tag_sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "_PLUGINS_DIR", plugin_dir)
    # 建立一个 tag 及插件，然后注销该 tag
    (plugin_dir / "deletedtag.py").write_text(
        "def on_mutation(ctx): pass\ndef audit_cluster(ctx): pass\n"
    )
    horon_db.create_concept("deletedtag")
    horon_db.create_tag("deletedtag")
    horon_db.delete_tag("deletedtag")

    # 全量审计不应出现已注销的 tag
    report = horon_db.audit_clusters_report()
    assert "deletedtag" not in report
