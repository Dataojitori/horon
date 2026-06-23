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


def test_expression_rejects_mixed_arrow_and_ampersand(horon_db):
    create_concepts(horon_db, ["A", "B", "C", "Rel"])

    with pytest.raises(ValueError, match="Cannot mix"):
        horon_db.set("Rel", "expression", "A & B → C")


def test_expression_rejects_missing_side_and_duplicate_members(horon_db):
    create_concepts(horon_db, ["A", "B", "Rel"])

    with pytest.raises(ValueError, match="requires a concept on each side"):
        horon_db.set("Rel", "expression", "A → ")

    with pytest.raises(ValueError, match="appear more than once"):
        horon_db.set("Rel", "expression", "A & A")


def test_unordered_composition_duplicate_detection_ignores_member_order(horon_db):
    create_concepts(horon_db, ["A", "B", "AB", "BA"])
    set_relation(horon_db, "AB", "A & B")

    with pytest.raises(ValueError, match="already .*same composition"):
        horon_db.set("BA", "expression", "B & A")
