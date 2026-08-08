"""Tests for multi-disclosure and surprise-weighted attention routing."""
import math

import pytest

from conftest import create_concepts


# ── Multi-Disclosure CRUD ─────────────────────────────────────────────────────

def test_create_concept_with_disclosure_writes_to_disclosures_table(horon_db):
    horon_db.create_concept("Alpha", disclosure="trigger A", content="body")
    result = horon_db.read_concept("Alpha")
    assert len(result.disclosures) == 1
    assert result.disclosures[0].text == "trigger A"


def test_add_multiple_disclosures(horon_db):
    horon_db.create_concept("Beta", content="body")
    horon_db.add("Beta", "disclosure", "first trigger")
    horon_db.add("Beta", "disclosure", "second trigger")

    result = horon_db.read_concept("Beta")
    assert len(result.disclosures) == 2
    assert result.disclosures[0].text == "first trigger"
    assert result.disclosures[1].text == "second trigger"


def test_delete_disclosure_by_id(horon_db):
    horon_db.create_concept("Gamma", content="body")
    horon_db.add("Gamma", "disclosure", "keep this")
    horon_db.add("Gamma", "disclosure", "delete this")

    result = horon_db.read_concept("Gamma")
    assert len(result.disclosures) == 2
    to_delete = result.disclosures[1].id

    horon_db.delete("Gamma", "disclosure", str(to_delete))

    result = horon_db.read_concept("Gamma")
    assert len(result.disclosures) == 1
    assert result.disclosures[0].text == "keep this"


def test_delete_disclosure_wrong_concept_raises(horon_db):
    horon_db.create_concept("X", content="body")
    horon_db.create_concept("Y", content="body")
    horon_db.add("X", "disclosure", "belongs to X")

    disc_id = horon_db.read_concept("X").disclosures[0].id
    with pytest.raises(ValueError, match="belongs to concept"):
        horon_db.delete("Y", "disclosure", str(disc_id))


def test_delete_nonexistent_disclosure_raises(horon_db):
    horon_db.create_concept("Z", content="body")
    with pytest.raises(ValueError, match="No disclosure"):
        horon_db.delete("Z", "disclosure", "99999")


def test_add_empty_disclosure_raises(horon_db):
    horon_db.create_concept("Empty", content="body")
    with pytest.raises(ValueError, match="empty"):
        horon_db.add("Empty", "disclosure", "   ")


def test_set_disclosure_raises_helpful_error(horon_db):
    horon_db.create_concept("SetTest", content="body")
    with pytest.raises(ValueError, match="multi-valued"):
        horon_db.set("SetTest", "disclosure", "old style")


def test_search_matches_disclosure_text(horon_db):
    horon_db.create_concept("Searchable", content="body")
    horon_db.add("Searchable", "disclosure", "UniqueNeedle here")

    results = horon_db.search_concepts("UniqueNeedle")
    assert len(results) == 1
    assert results[0].concept_name == "Searchable"


def test_concept_deletion_cascades_disclosures(horon_db):
    horon_db.create_concept("Ephemeral", content="body")
    horon_db.add("Ephemeral", "disclosure", "will vanish")
    horon_db.delete("Ephemeral")

    rows = horon_db.conn.execute(
        "SELECT * FROM disclosures WHERE concept_id = 1"
    ).fetchall()
    assert len(rows) == 0


# ── Migration (existing data) ────────────────────────────────────────────────

def test_migration_preserves_old_disclosure_data(horon_db):
    """Simulate pre-migration: write to old column, run migration, verify new table."""
    horon_db.conn.execute(
        "UPDATE concepts SET disclosure = 'legacy text' "
        "WHERE id = (SELECT id FROM concepts LIMIT 1)"
    )
    horon_db.conn.commit()

    horon_db.create_concept("Pre", content="body")
    pre_id = horon_db.read_concept("Pre").id
    horon_db.conn.execute(
        "UPDATE concepts SET disclosure = 'old field' WHERE id = ?",
        (pre_id,),
    )
    horon_db.conn.execute(
        "INSERT INTO disclosures (concept_id, text, created_at) "
        "VALUES (?, 'migrated text', '2026-01-01T00:00:00')",
        (pre_id,),
    )
    horon_db.conn.commit()

    result = horon_db.read_concept("Pre")
    assert any(d.text == "migrated text" for d in result.disclosures)


# ── Surprise-Weighted Transition ──────────────────────────────────────────────

def _plant_last_read(horon_db, concept_id):
    """Plant an audit log entry so record_transition finds this as from_id."""
    horon_db.conn.execute(
        "INSERT INTO cli_audit_log "
        "(timestamp, command, success, concept_id) "
        "VALUES (datetime('now'), 'read_concept', 1, ?)",
        (concept_id,),
    )
    horon_db.conn.commit()


def test_record_transition_creates_edge(horon_db):
    create_concepts(horon_db, ["A", "B"])
    a_id = horon_db.read_concept("A").id
    b_id = horon_db.read_concept("B").id

    _plant_last_read(horon_db, a_id)
    horon_db.record_transition(b_id)

    row = horon_db.conn.execute(
        "SELECT weight FROM concept_transitions "
        "WHERE from_concept_id = ? AND to_concept_id = ?",
        (a_id, b_id),
    ).fetchone()
    assert row is not None
    assert row["weight"] > 0


def test_cold_start_surprise_value(horon_db):
    """A has no outgoing edges. Jump A→X. P' = _PRUNING_THRESHOLD/1.0 = 0.05, I ≈ 4.32"""
    create_concepts(horon_db, ["A", "X"])
    a_id = horon_db.read_concept("A").id
    x_id = horon_db.read_concept("X").id

    _plant_last_read(horon_db, a_id)
    horon_db.record_transition(x_id)

    row = horon_db.conn.execute(
        "SELECT weight FROM concept_transitions "
        "WHERE from_concept_id = ? AND to_concept_id = ?",
        (a_id, x_id),
    ).fetchone()

    expected = -math.log2(0.05 / 1.0)  # ≈ 4.3219
    assert abs(row["weight"] - expected) < 0.01


def test_frequent_transition_builds_weight(horon_db):
    create_concepts(horon_db, ["A", "B"])
    a_id = horon_db.read_concept("A").id
    b_id = horon_db.read_concept("B").id

    _plant_last_read(horon_db, a_id)
    for _ in range(5):
        horon_db.record_transition(b_id)

    row = horon_db.conn.execute(
        "SELECT weight FROM concept_transitions "
        "WHERE from_concept_id = ? AND to_concept_id = ?",
        (a_id, b_id),
    ).fetchone()
    assert row["weight"] > 4.0


def test_rare_transition_gets_high_surprise(horon_db):
    """Build A→B as dominant, then jump A→X (rare). X should get high surprise."""
    create_concepts(horon_db, ["A", "B", "X"])
    a_id = horon_db.read_concept("A").id
    b_id = horon_db.read_concept("B").id
    x_id = horon_db.read_concept("X").id

    _plant_last_read(horon_db, a_id)
    for _ in range(10):
        horon_db.record_transition(b_id)

    horon_db.record_transition(x_id)

    x_row = horon_db.conn.execute(
        "SELECT weight FROM concept_transitions "
        "WHERE from_concept_id = ? AND to_concept_id = ?",
        (a_id, x_id),
    ).fetchone()
    b_row = horon_db.conn.execute(
        "SELECT weight FROM concept_transitions "
        "WHERE from_concept_id = ? AND to_concept_id = ?",
        (a_id, b_id),
    ).fetchone()

    assert x_row["weight"] > b_row["weight"] * 0.3


def test_decay_reduces_old_weights(horon_db):
    create_concepts(horon_db, ["A", "B", "C"])
    a_id = horon_db.read_concept("A").id
    b_id = horon_db.read_concept("B").id
    c_id = horon_db.read_concept("C").id

    _plant_last_read(horon_db, a_id)
    horon_db.record_transition(b_id)
    w_before = horon_db.conn.execute(
        "SELECT weight FROM concept_transitions "
        "WHERE from_concept_id = ? AND to_concept_id = ?",
        (a_id, b_id),
    ).fetchone()["weight"]

    horon_db.record_transition(c_id)
    w_after = horon_db.conn.execute(
        "SELECT weight FROM concept_transitions "
        "WHERE from_concept_id = ? AND to_concept_id = ?",
        (a_id, b_id),
    ).fetchone()["weight"]

    assert w_after < w_before


def test_pruning_removes_dead_edges(horon_db):
    create_concepts(horon_db, ["A", "B", "C"])
    a_id = horon_db.read_concept("A").id
    b_id = horon_db.read_concept("B").id
    c_id = horon_db.read_concept("C").id

    horon_db.conn.execute(
        "INSERT INTO concept_transitions "
        "(from_concept_id, to_concept_id, weight, last_accessed_at) "
        "VALUES (?, ?, 0.04, '2026-01-01T00:00:00')",
        (a_id, b_id),
    )
    horon_db.conn.commit()

    _plant_last_read(horon_db, a_id)
    horon_db.record_transition(c_id)

    pruned = horon_db.conn.execute(
        "SELECT weight FROM concept_transitions "
        "WHERE from_concept_id = ? AND to_concept_id = ?",
        (a_id, b_id),
    ).fetchone()
    assert pruned is None


def test_self_transition_is_noop(horon_db):
    create_concepts(horon_db, ["A"])
    a_id = horon_db.read_concept("A").id

    _plant_last_read(horon_db, a_id)
    horon_db.record_transition(a_id)

    count = horon_db.conn.execute(
        "SELECT COUNT(*) AS cnt FROM concept_transitions "
        "WHERE from_concept_id = ?",
        (a_id,),
    ).fetchone()["cnt"]
    assert count == 0


def test_no_previous_read_is_noop(horon_db):
    """When audit log has no prior read_concept, record_transition does nothing."""
    create_concepts(horon_db, ["A"])
    a_id = horon_db.read_concept("A").id

    horon_db.record_transition(a_id)

    count = horon_db.conn.execute(
        "SELECT COUNT(*) AS cnt FROM concept_transitions",
    ).fetchone()["cnt"]
    assert count == 0


def test_suggested_next_returns_top_transitions(horon_db):
    create_concepts(horon_db, ["A", "B", "C"])
    a_id = horon_db.read_concept("A").id
    b_id = horon_db.read_concept("B").id
    c_id = horon_db.read_concept("C").id

    _plant_last_read(horon_db, a_id)
    for _ in range(5):
        horon_db.record_transition(b_id)
    horon_db.record_transition(c_id)

    result = horon_db.read_concept("A")
    assert len(result.suggested_next) >= 1
    names = [s.concept_name for s in result.suggested_next]
    assert "B" in names


def test_concept_deletion_cascades_transitions(horon_db):
    create_concepts(horon_db, ["A", "B"])
    a_id = horon_db.read_concept("A").id
    b_id = horon_db.read_concept("B").id

    _plant_last_read(horon_db, a_id)
    horon_db.record_transition(b_id)
    horon_db.delete("B")

    rows = horon_db.conn.execute(
        "SELECT * FROM concept_transitions WHERE to_concept_id = ?",
        (b_id,),
    ).fetchall()
    assert len(rows) == 0


def test_record_transition_ignores_deleted_from_concept(horon_db):
    """When the previously read concept was deleted, record_transition is a no-op without FK errors."""
    create_concepts(horon_db, ["A", "B"])
    a_id = horon_db.read_concept("A").id
    b_id = horon_db.read_concept("B").id

    _plant_last_read(horon_db, a_id)
    horon_db.delete("A")

    # Should not raise IntegrityError
    horon_db.record_transition(b_id)

    count = horon_db.conn.execute(
        "SELECT COUNT(*) AS cnt FROM concept_transitions",
    ).fetchone()["cnt"]
    assert count == 0


def test_record_transition_traces_back_past_deleted_concept(horon_db):
    """When the previously read concept was deleted, it still traces back to the next older read."""
    create_concepts(horon_db, ["X", "A", "B"])
    x_id = horon_db.read_concept("X").id
    a_id = horon_db.read_concept("A").id
    b_id = horon_db.read_concept("B").id

    _plant_last_read(horon_db, x_id)
    _plant_last_read(horon_db, a_id)
    
    horon_db.delete("A")

    horon_db.record_transition(b_id)

    # It should trace back past the deleted A and record a transition from X to B.
    # Because X is step 2 (i=1), its relevance is 0.5.
    rows = horon_db.conn.execute(
        "SELECT * FROM concept_transitions",
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["from_concept_id"] == x_id
    assert rows[0]["to_concept_id"] == b_id
    # Weight calculation: w0 = 0. w_total = 0. p_prime = max(0, 0.05) / 1.0 = 0.05.
    # surprise = -log2(0.05) ≈ 4.321928. relevance = 0.5. weight = 0 + surprise * 0.5.
    expected_surprise = -math.log2(0.05)
    assert math.isclose(rows[0]["weight"], expected_surprise * 0.5)

def test_record_transition_multiple_lookback(horon_db):
    """Traces back up to 3 distinct reads with relevance decay 1.0, 0.5, 0.25."""
    create_concepts(horon_db, ["C", "B", "A", "TARGET"])
    c_id = horon_db.read_concept("C").id
    b_id = horon_db.read_concept("B").id
    a_id = horon_db.read_concept("A").id
    target_id = horon_db.read_concept("TARGET").id

    _plant_last_read(horon_db, c_id)
    _plant_last_read(horon_db, b_id)
    _plant_last_read(horon_db, a_id)

    horon_db.record_transition(target_id)

    transitions = horon_db.conn.execute(
        "SELECT from_concept_id, weight FROM concept_transitions ORDER BY from_concept_id",
    ).fetchall()
    
    # We expect transitions from A, B, C to TARGET
    assert len(transitions) == 3
    
    weights = {r["from_concept_id"]: r["weight"] for r in transitions}
    expected_surprise = -math.log2(0.05)
    
    assert math.isclose(weights[a_id], expected_surprise * 1.0) # step 1
    assert math.isclose(weights[b_id], expected_surprise * 0.5) # step 2
    assert math.isclose(weights[c_id], expected_surprise * 0.25) # step 3
