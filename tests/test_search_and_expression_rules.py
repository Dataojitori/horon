import pytest

from conftest import create_concepts, set_relation


def names(results):
    return [item.name for item in results]


def test_search_matches_alias_disclosure_and_evidence(horon_db):
    create_concepts(horon_db, ["Topic", "Other", "EvidenceOnly"])
    horon_db.add("Topic", "name", "AliasNeedle")
    horon_db.set("Other", "disclosure", "DisclosureNeedle is here")
    horon_db.update("EvidenceOnly", "evidence", "EvidenceNeedle is here")

    assert names(horon_db.search_concepts("AliasNeedle")) == ["Topic"]
    assert names(horon_db.search_concepts("DisclosureNeedle")) == ["Other"]
    assert names(horon_db.search_concepts("EvidenceNeedle")) == ["EvidenceOnly"]


def test_search_treats_percent_and_underscore_as_literal_characters(horon_db):
    create_concepts(horon_db, ["Alpha", "Beta", "Gamma"])
    horon_db.set("Alpha", "disclosure", "literal 100% marker")
    horon_db.set("Beta", "disclosure", "literal a_b marker")
    horon_db.set("Gamma", "disclosure", "plain marker")

    assert names(horon_db.search_concepts("100%")) == ["Alpha"]
    assert names(horon_db.search_concepts("a_b")) == ["Beta"]


def test_search_treats_backslash_as_literal_character(horon_db):
    horon_db.create_concept("Path")
    horon_db.set("Path", "disclosure", r"stored at C:\tmp\horon")

    assert names(horon_db.search_concepts(r"C:\tmp")) == ["Path"]


def test_expression_rejects_mixed_operators(horon_db):
    """一个变体只能用一种运算符，严禁混用 → / & / |。"""
    create_concepts(horon_db, ["A", "B", "C", "Rel"])

    with pytest.raises(ValueError, match="Mixed operators"):
        horon_db.set("Rel", "expression", "A & B → C")

    with pytest.raises(ValueError, match="Mixed operators"):
        horon_db.set("Rel", "expression", "A → B | C")


def test_expression_rejects_empty_operand_and_duplicate_members(horon_db):
    create_concepts(horon_db, ["A", "B", "Rel"])

    with pytest.raises(ValueError, match="empty operand"):
        horon_db.set("Rel", "expression", "A → ")

    with pytest.raises(ValueError, match="appear more than once"):
        horon_db.set("Rel", "expression", "A & A")


def test_unordered_composition_duplicate_detection_ignores_member_order(horon_db):
    create_concepts(horon_db, ["A", "B", "AB", "BA"])
    set_relation(horon_db, "AB", "A & B")

    with pytest.raises(ValueError, match="already .*same composition"):
        horon_db.set("BA", "expression", "B & A")


# ── Multi-position expression tests ──────────────────────────────────────────


def test_multi_position_three_segment_expression(horon_db):
    """A → B → C stores 3 ordered members and round-trips through get_expression."""
    create_concepts(horon_db, ["o", "p", "q", "Chain"])
    horon_db.set("Chain", "expression", "o → p → q")

    result = horon_db.read_concept("Chain")
    assert result.variations[0].type == "CHAIN"
    assert result.variations[0].expression == "o → p → q"

    members = result.variations[0].members
    assert [(m.order_index, m.name) for m in members] == [
        (1, "o"), (2, "p"), (3, "q"),
    ]


def test_multi_position_duplicate_detection(horon_db):
    """Same multi-position composition on a different concept is rejected."""
    create_concepts(horon_db, ["A", "B", "C", "Chain1", "Chain2"])
    horon_db.set("Chain1", "expression", "A → B → C")

    with pytest.raises(ValueError, match="already .*same composition"):
        horon_db.set("Chain2", "expression", "A → B → C")


def test_multi_position_different_order_is_different(horon_db):
    """A → B → C vs A → C → B are distinct compositions."""
    create_concepts(horon_db, ["A", "B", "C", "Fwd", "Rev"])
    horon_db.set("Fwd", "expression", "A → B → C")
    horon_db.set("Rev", "expression", "A → C → B")

    fwd = horon_db.read_concept("Fwd")
    rev = horon_db.read_concept("Rev")
    assert fwd.variations[0].expression == "A → B → C"
    assert rev.variations[0].expression == "A → C → B"


def test_chain_allows_revisiting_a_concept_at_a_later_position(horon_db):
    """CHAIN 允许同一概念在不同位置重复出现（A → B → A 绕回起点）。"""
    create_concepts(horon_db, ["A", "B", "Rel"])

    horon_db.set("Rel", "expression", "A → B → A")

    members = horon_db.read_concept("Rel").variations[0].members
    assert [(member.order_index, member.name) for member in members] == [
        (1, "A"), (2, "B"), (3, "A"),
    ]


def test_chain_rejects_adjacent_self_repeat(horon_db):
    """相邻两段相同（A → A）是零跨度自环，无语义，应被拒绝。"""
    create_concepts(horon_db, ["A", "B", "Rel"])

    with pytest.raises(ValueError, match="immediately follow itself"):
        horon_db.set("Rel", "expression", "A → A")

    # 但非相邻的重复仍然合法。
    horon_db.set("Rel", "expression", "A → B → A")
    assert horon_db.read_concept("Rel").variations[0].expression == "A → B → A"


def test_multi_position_inbound_shows_at_max_position(horon_db):
    """read_concept on the max-position concept shows the relation as inbound."""
    create_concepts(horon_db, ["A", "B", "C", "Chain"])
    set_relation(horon_db, "Chain", "A → B → C")

    c_view = horon_db.read_concept("C")
    inbound_names = [r.concept_name for r in c_view.inbound_confirmed]
    assert "Chain" in inbound_names


def test_multi_position_outbound_shows_at_position_1(horon_db):
    """read_concept on the position-1 concept shows the relation as outbound."""
    create_concepts(horon_db, ["A", "B", "C", "Chain"])
    set_relation(horon_db, "Chain", "A → B → C")

    a_view = horon_db.read_concept("A")
    outbound_names = [r.concept_name for r in a_view.outbound_confirmed]
    assert "Chain" in outbound_names


def test_multi_position_is_loaded_as_one_expression_rule(horon_db):
    """多段表达式完整进入编译图，不被拆成虚假的二元边。"""
    create_concepts(horon_db, ["A", "B", "C", "Chain"])
    set_relation(horon_db, "Chain", "A → B → C")

    graph = horon_db._load_relation_graph()
    chain_cid = horon_db._resolve_id("Chain")[0]
    rule = next(rule for rule in graph.expressions
                if rule.concept_id == chain_cid)
    assert tuple(tuple(position) for position in rule.positions) == (
        (horon_db._resolve_id("A")[0],),
        (horon_db._resolve_id("B")[0],),
        (horon_db._resolve_id("C")[0],),
    )


def test_multi_position_overview_display(horon_db):
    """list_concepts overview correctly renders multi-position expressions."""
    create_concepts(horon_db, ["X", "Y", "Z", "XYZ"])
    horon_db.set("XYZ", "expression", "X → Y → Z")

    overviews = horon_db.get_all_concepts_overview()
    xyz = next(c for c in overviews if c["name"] == "XYZ")
    assert xyz["variations"][0]["expression"] == "X → Y → Z"


def test_multi_position_add_variation(horon_db):
    """add variation with multi-position expression."""
    create_concepts(horon_db, ["A", "B", "C", "D", "Multi"])
    horon_db.add("Multi", "variation", "A → B → C")

    result = horon_db.read_concept("Multi")
    composed = next(v for v in result.variations if v.expression is not None)
    assert composed.expression == "A → B → C"
