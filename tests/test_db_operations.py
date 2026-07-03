import pytest

from conftest import create_concepts, set_relation, compile_path


def variation_ref(db, concept):
    cid, short_code = db._resolve_single_variation(concept)
    return f"{concept}:{short_code}"


def test_create_rename_and_alias_resolution(horon_db):
    horon_db.create_concept("Original", disclosure="first meaning")
    horon_db.add("Original", "name", "Alias")
    horon_db.set("Original", "name", "Renamed")

    result = horon_db.read_concept("Alias")

    assert result.name == "Renamed"
    assert result.disclosure == "first meaning"
    assert set(result.aliases) == {"Original", "Alias", "Renamed"}


def test_names_reject_operator_characters_and_pure_numbers(horon_db):
    with pytest.raises(ValueError, match="operator characters"):
        horon_db.create_concept("A → B")

    with pytest.raises(ValueError, match="purely numeric"):
        horon_db.create_concept("123")


def test_set_expression_resets_status_and_prevents_duplicate_composition(horon_db):
    create_concepts(horon_db, ["A", "B", "C", "RelOne", "RelTwo"])
    set_relation(horon_db, "RelOne", "A → B")

    horon_db.set("RelOne", "expression", "A → C")
    rel_one = horon_db.read_concept("RelOne")

    assert rel_one.variations[0].expression == "A → C"
    assert rel_one.variations[0].status is None

    with pytest.raises(ValueError, match="already .*same composition"):
        horon_db.set("RelTwo", "expression", "A → C")


def test_losing_last_confirmed_variation_cascades_without_deleting_content(
    horon_db,
):
    create_concepts(horon_db, ["A", "B", "C", "AtoB", "AtoBToC"])
    set_relation(horon_db, "AtoB", "A → B")
    set_relation(horon_db, "AtoBToC", "AtoB → C")
    horon_db.update("AtoB", "content", "content for A to B")
    horon_db.update("AtoBToC", "content", "content for the parent")

    result = horon_db.set("A", "status", "hypothesis")

    child = horon_db.read_concept("AtoB").variations[0]
    parent = horon_db.read_concept("AtoBToC").variations[0]
    assert child.status == "hypothesis"
    assert parent.status == "hypothesis"
    assert child.expression == "A → B"
    assert parent.expression == "AtoB → C"
    assert child.content == "content for A to B"
    assert parent.content == "content for the parent"
    assert "Cascaded downgrades:" in result.message
    assert "Downgraded 'AtoB'" in result.message
    assert "Downgraded 'AtoBToC'" in result.message


def test_parent_stays_confirmed_while_member_has_another_confirmed_variation(
    horon_db,
):
    create_concepts(horon_db, ["X", "Y", "A", "B", "AtoB"])
    for name in ("X", "Y", "A", "B"):
        horon_db.set(name, "status", "confirmed")

    horon_db.add("A", "variation", "X → Y")
    variations = horon_db.read_concept("A").variations
    atomic = next(v for v in variations if v.expression is None)
    composed = next(v for v in variations if v.expression == "X → Y")
    horon_db.set(f"A:{composed.short_code}", "status", "confirmed")
    set_relation(horon_db, "AtoB", "A → B")

    result = horon_db.set(
        f"A:{atomic.short_code}", "status", "hypothesis"
    )

    assert horon_db.read_concept("AtoB").variations[0].status == "confirmed"
    assert "Cascaded downgrades:" not in result.message


def test_confirm_or_variation_needs_only_one_confirmed_member(horon_db):
    """OR 是「择一」：任一成员 confirmed 即可确认，无需全部。"""
    create_concepts(horon_db, ["A", "B", "K"])
    horon_db.set("A", "status", "confirmed")  # B 保持 hypothesis
    horon_db.set("K", "expression", "A | B")

    result = horon_db.set("K", "status", "confirmed")

    assert horon_db.read_concept("K").variations[0].status == "confirmed"
    assert "Cascaded downgrades:" not in result.message


def test_confirm_or_variation_rejected_when_no_member_confirmed(horon_db):
    """OR 的所有成员都未确认时，不允许确认该 OR。"""
    create_concepts(horon_db, ["A", "B", "K"])
    horon_db.set("K", "expression", "A | B")

    with pytest.raises(ValueError, match="Cannot confirm OR variation"):
        horon_db.set("K", "status", "confirmed")


def test_confirm_and_variation_still_requires_all_members(horon_db):
    """AND 维持原语义：成员缺一不可。"""
    create_concepts(horon_db, ["A", "B", "M"])
    horon_db.set("A", "status", "confirmed")  # B 未确认
    horon_db.set("M", "expression", "A & B")

    with pytest.raises(ValueError, match="has no confirmed variations"):
        horon_db.set("M", "status", "confirmed")


def test_or_variation_survives_audit_while_one_member_stays_confirmed(horon_db):
    """降级 OR 的一个成员，只要还有另一个 confirmed，OR 不应被级联降级。"""
    create_concepts(horon_db, ["A", "B", "K"])
    horon_db.set("A", "status", "confirmed")
    horon_db.set("B", "status", "confirmed")
    horon_db.set("K", "expression", "A | B")
    horon_db.set("K", "status", "confirmed")

    result = horon_db.set("B", "status", "hypothesis")  # A 仍 confirmed

    assert horon_db.read_concept("K").variations[0].status == "confirmed"
    assert "Downgraded 'K'" not in result.message


def test_or_variation_downgraded_when_all_members_lose_confirmation(horon_db):
    """OR 的成员全部失去 confirmed 时，才级联降级该 OR。"""
    create_concepts(horon_db, ["A", "B", "K"])
    horon_db.set("A", "status", "confirmed")
    horon_db.set("B", "status", "confirmed")
    horon_db.set("K", "expression", "A | B")
    horon_db.set("K", "status", "confirmed")

    horon_db.set("A", "status", "hypothesis")  # B 还在，OR 仍成立
    assert horon_db.read_concept("K").variations[0].status == "confirmed"

    result = horon_db.set("B", "status", "hypothesis")  # 两者都掉

    assert horon_db.read_concept("K").variations[0].status == "hypothesis"
    assert "Downgraded 'K'" in result.message


def test_add_variation_requires_explicit_short_code_after_ambiguity(horon_db):
    create_concepts(horon_db, ["A", "B", "C", "Poly"])
    horon_db.add("Poly", "variation", "A → B")

    with pytest.raises(ValueError, match="multiple variations"):
        horon_db.update("Poly", "content", "ambiguous write")

    first = horon_db.read_concept("Poly").variations[0].short_code
    horon_db.update(f"Poly:{first}", "content", "specific write")

    assert horon_db.read_concept("Poly").variations[0].content == "specific write"


def test_delete_last_variation_rejects_still_referenced_concept(horon_db):
    create_concepts(horon_db, ["A", "B", "Rel"])
    set_relation(horon_db, "Rel", "A → B")

    with pytest.raises(ValueError, match="still referenced"):
        horon_db.delete("A")

    assert horon_db.read_concept("A").name == "A"
    assert horon_db.read_concept("Rel").variations[0].expression == "A → B"


def test_delete_one_variation_keeps_concept_and_other_variations(horon_db):
    create_concepts(horon_db, ["A", "B", "C", "Poly"])
    horon_db.add("Poly", "variation", "A → B")
    variations = horon_db.read_concept("Poly").variations
    atomic = next(v for v in variations if v.expression is None)
    composed = next(v for v in variations if v.expression == "A → B")

    horon_db.delete(f"Poly:{composed.short_code}")
    result = horon_db.read_concept("Poly")

    assert result.name == "Poly"
    assert [v.short_code for v in result.variations] == [atomic.short_code]
    assert result.variations[0].expression is None


def test_delete_final_variation_deletes_unreferenced_concept(horon_db):
    horon_db.create_concept("Disposable")

    horon_db.delete("Disposable")

    with pytest.raises(ValueError, match="Concept not found"):
        horon_db.read_concept("Disposable")


def test_delete_expression_clears_members_and_status(horon_db):
    create_concepts(horon_db, ["A", "B", "Rel"])
    set_relation(horon_db, "Rel", "A → B")

    horon_db.delete("Rel", "expression")
    result = horon_db.read_concept("Rel")

    assert result.variations[0].expression is None
    assert result.variations[0].status is None
    assert compile_path(horon_db, ["A"], "B")["passed"] is False


def test_delete_expression_rejects_creating_second_atomic_variation(horon_db):
    create_concepts(horon_db, ["A", "B", "Poly"])
    horon_db.add("Poly", "variation", "A → B")
    composed = next(
        v for v in horon_db.read_concept("Poly").variations
        if v.expression == "A → B"
    )

    with pytest.raises(ValueError, match="Only one atomic variation"):
        horon_db.delete(f"Poly:{composed.short_code}", "expression")


def test_read_concept_groups_relations_and_surfaces_disclosures(horon_db):
    create_concepts(
        horon_db,
        [
            "Source",
            "Target",
            "Other",
            "ConfirmedIn",
            "HypothesisIn",
            "NegatedOut",
        ],
    )
    horon_db.set("Source", "disclosure", "source disclosure")
    horon_db.set("Other", "disclosure", "other disclosure")
    set_relation(horon_db, "ConfirmedIn", "Source → Target")
    set_relation(horon_db, "HypothesisIn", "Other → Target", status=None)
    set_relation(horon_db, "NegatedOut", "Target → Other", status="negated")

    target = horon_db.read_concept("Target")

    assert [r.concept_name for r in target.inbound_confirmed] == ["ConfirmedIn"]
    assert target.inbound_confirmed[0].members[0].disclosure == "source disclosure"
    assert [r.concept_name for r in target.inbound_hypotheses] == ["HypothesisIn"]
    assert [r.concept_name for r in target.outbound_negated] == ["NegatedOut"]
    assert target.outbound_negated[0].members[0].disclosure == "other disclosure"


def test_read_concept_alerts_when_unless_condition_is_met(horon_db):
    create_concepts(horon_db, ["A", "B", "Rel", "Watcher"])
    set_relation(horon_db, "Rel", "A → B", status="negated")
    horon_db.update("Watcher", "unless", "Recheck when ${A → B negated}.")

    result = horon_db.read_concept("Watcher")

    assert len(result.alerts) == 1
    assert "Unless triggered on 'Watcher'" in result.alerts[0]
    assert "'Rel' is now negated" in result.alerts[0]


def test_read_concept_alerts_when_unless_reference_breaks_legally(horon_db):
    create_concepts(horon_db, ["A", "B", "C", "Rel", "Watcher"])
    set_relation(horon_db, "Rel", "A → B", status="confirmed")
    horon_db.update("Watcher", "unless", "Recheck when ${A → B confirmed}.")
    horon_db.set("Rel", "expression", "A → C")

    result = horon_db.read_concept("Watcher")

    # A → B is no longer confirmed (in fact, it doesn't exist anymore).
    # Thus the condition ${A → B confirmed} is simply NOT met.
    # It should not complain about "Broken reference" for a missing relation.
    assert len(result.alerts) == 0

def test_read_concept_alerts_broken_reference_for_missing_concept(horon_db):
    create_concepts(horon_db, ["A", "Watcher"])
    horon_db.update("Watcher", "unless", "Recheck when ${NonExistent → A confirmed}.")

    result = horon_db.read_concept("Watcher")

    assert len(result.alerts) == 1
    assert "Broken reference in 'Watcher'" in result.alerts[0]
    assert "could not be resolved" in result.alerts[0]


def test_read_concept_does_not_alert_when_unless_status_does_not_match(horon_db):
    create_concepts(horon_db, ["A", "B", "Rel", "Watcher"])
    set_relation(horon_db, "Rel", "A → B", status="confirmed")
    horon_db.update("Watcher", "unless", "Recheck when ${A → B negated}.")

    result = horon_db.read_concept("Watcher")

    assert result.alerts == []

def test_read_concept_alerts_for_single_concept_and_or_condition(horon_db):
    create_concepts(horon_db, ["A", "B", "C", "RelOr", "Watcher1", "Watcher2"])
    horon_db.set("A", "status", "confirmed")
    
    set_relation(horon_db, "RelOr", "B | C", status="negated")
    
    horon_db.update("Watcher1", "unless", "Recheck when ${A confirmed}.")
    horon_db.update("Watcher2", "unless", "Recheck when ${B | C negated}.")
    
    res1 = horon_db.read_concept("Watcher1")
    assert len(res1.alerts) == 1
    assert "Unless triggered on 'Watcher1'" in res1.alerts[0]
    assert "'A' has a confirmed variation" in res1.alerts[0]
    
    res2 = horon_db.read_concept("Watcher2")
    assert len(res2.alerts) == 1
    assert "Invalid condition in 'Watcher2'" in res2.alerts[0]
    assert "'B | C negated' is not supported" in res2.alerts[0]


def test_read_concept_alerts_when_any_or_member_is_confirmed(horon_db):
    create_concepts(horon_db, ["A", "B", "Watcher"])
    horon_db.set("B", "status", "confirmed")
    horon_db.update("Watcher", "unless", "Recheck when ${A | B confirmed}.")

    result = horon_db.read_concept("Watcher")

    assert len(result.alerts) == 1
    assert "Unless triggered on 'Watcher'" in result.alerts[0]
    assert "'B' has a confirmed variation" in result.alerts[0]


def test_read_concept_rejects_and_condition(horon_db):
    create_concepts(horon_db, ["A", "B", "Group", "Watcher"])
    horon_db.set("A", "status", "confirmed")
    horon_db.set("B", "status", "confirmed")
    set_relation(horon_db, "Group", "A & B", status="confirmed")
    horon_db.update("Watcher", "unless", "Recheck when ${A & B confirmed}.")

    result = horon_db.read_concept("Watcher")

    assert len(result.alerts) == 1
    assert "Invalid condition in 'Watcher'" in result.alerts[0]
    assert "AND conditions" in result.alerts[0]


def test_unless_cache_separates_same_expression_by_expected_status(horon_db):
    create_concepts(horon_db, ["A", "Watcher"])
    horon_db.set("A", "status", "confirmed")
    horon_db.update(
        "Watcher",
        "unless",
        "Recheck ${A confirmed}; invalid condition ${A negated}.",
    )

    result = horon_db.read_concept("Watcher")

    assert len(result.alerts) == 2
    assert "Unless triggered on 'Watcher'" in result.alerts[0]
    assert "Invalid condition in 'Watcher'" in result.alerts[1]
    assert "Single-concept and OR conditions do not support 'negated'" in result.alerts[1]
