import pytest

from conftest import create_concepts, set_relation


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


def test_add_variation_requires_explicit_short_code_after_ambiguity(horon_db):
    create_concepts(horon_db, ["A", "B", "C", "Poly"])
    horon_db.add("Poly", "variation", "A → B")

    with pytest.raises(ValueError, match="Multiple variations"):
        horon_db.update("Poly", "evidence", "ambiguous write")

    first = horon_db.read_concept("Poly").variations[0].short_code
    horon_db.update(f"Poly:{first}", "evidence", "specific write")

    assert horon_db.read_concept("Poly").variations[0].evidence == "specific write"


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
    assert horon_db.compile(["A"], "B")["passed"] is False


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
    assert target.inbound_confirmed[0].from_concept_disclosure == "source disclosure"
    assert [r.concept_name for r in target.inbound_hypotheses] == ["HypothesisIn"]
    assert [r.concept_name for r in target.outbound_negated] == ["NegatedOut"]
    assert target.outbound_negated[0].target_concept_disclosure == "other disclosure"


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

    assert len(result.alerts) == 1
    assert "Broken reference in 'Watcher'" in result.alerts[0]
    assert "A → B" in result.alerts[0]


def test_read_concept_does_not_alert_when_unless_status_does_not_match(horon_db):
    create_concepts(horon_db, ["A", "B", "Rel", "Watcher"])
    set_relation(horon_db, "Rel", "A → B", status="confirmed")
    horon_db.update("Watcher", "unless", "Recheck when ${A → B negated}.")

    result = horon_db.read_concept("Watcher")

    assert result.alerts == []
