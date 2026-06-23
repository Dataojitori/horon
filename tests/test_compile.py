from conftest import create_concepts, set_relation


def edge_names(result):
    return [edge["name"] for edge in result["compiled_route"]]


def edge_statuses(result):
    return [edge["status"] for edge in result["compiled_route"]]


def test_compile_prefers_confirmed_route_over_shorter_hypothesis(horon_db):
    create_concepts(
        horon_db,
        ["A", "Mid", "Goal", "DirectHypothesis", "AtoMid", "MidToGoal"],
    )
    set_relation(horon_db, "DirectHypothesis", "A → Goal", status=None)
    set_relation(horon_db, "AtoMid", "A → Mid")
    set_relation(horon_db, "MidToGoal", "Mid → Goal")

    result = horon_db.compile(["A"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == ["AtoMid", "MidToGoal"]
    assert edge_statuses(result) == ["confirmed", "confirmed"]


def test_compile_prefers_shorter_route_when_all_candidates_confirmed(horon_db):
    create_concepts(
        horon_db,
        ["A", "Mid", "Goal", "Direct", "AtoMid", "MidToGoal"],
    )
    set_relation(horon_db, "Direct", "A → Goal")
    set_relation(horon_db, "AtoMid", "A → Mid")
    set_relation(horon_db, "MidToGoal", "Mid → Goal")

    result = horon_db.compile(["A"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == ["Direct"]


def test_compile_returns_hypothesis_route_when_no_confirmed_route_exists(horon_db):
    create_concepts(horon_db, ["A", "Goal", "OnlyGuess"])
    set_relation(horon_db, "OnlyGuess", "A → Goal", status=None)

    result = horon_db.compile(["A"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == ["OnlyGuess"]
    assert edge_statuses(result) == ["hypothesis"]


def test_compile_treats_negated_relation_as_wall(horon_db):
    create_concepts(
        horon_db,
        ["A", "Mid", "Goal", "BlockedDirect", "AtoMid", "MidToGoal"],
    )
    set_relation(horon_db, "BlockedDirect", "A → Goal", status="negated")
    set_relation(horon_db, "AtoMid", "A → Mid")
    set_relation(horon_db, "MidToGoal", "Mid → Goal")

    result = horon_db.compile(["A"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == ["AtoMid", "MidToGoal"]
    assert "BlockedDirect" not in edge_names(result)


def test_compile_counts_unconfirmed_and_group_as_hypothesis_cost(horon_db):
    create_concepts(
        horon_db,
        [
            "A",
            "B",
            "X",
            "Goal",
            "AtoB",
            "AB",
            "BX",
            "XG",
            "ABG",
        ],
    )
    set_relation(horon_db, "AtoB", "A → B")
    set_relation(horon_db, "AB", "A & B", status=None)
    set_relation(horon_db, "BX", "B → X")
    set_relation(horon_db, "XG", "X → Goal")
    set_relation(horon_db, "ABG", "AB → Goal")

    result = horon_db.compile(["A", "B"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == ["AtoB", "BX", "XG"]
    assert edge_statuses(result) == ["confirmed", "confirmed", "confirmed"]
    assert "AB" not in edge_names(result)


def test_compile_requires_all_group_members_before_activating_and_group(horon_db):
    create_concepts(
        horon_db,
        ["A", "B", "C", "Goal", "AtoB", "AB", "ABC", "ABToGoal", "ABCToGoal"],
    )
    set_relation(horon_db, "AtoB", "A → B")
    set_relation(horon_db, "AB", "A & B")
    set_relation(horon_db, "ABC", "A & B & C")
    set_relation(horon_db, "ABToGoal", "AB → Goal")
    set_relation(horon_db, "ABCToGoal", "ABC → Goal")

    result = horon_db.compile(["A", "B"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == ["AtoB", "AB", "ABToGoal"]
    assert "ABC" not in edge_names(result)


def test_compile_can_activate_multi_member_group_after_all_waypoints(horon_db):
    create_concepts(
        horon_db,
        ["A", "B", "C", "Goal", "AtoB", "BtoC", "ABC", "ABCToGoal"],
    )
    set_relation(horon_db, "AtoB", "A → B")
    set_relation(horon_db, "BtoC", "B → C")
    set_relation(horon_db, "ABC", "A & B & C")
    set_relation(horon_db, "ABCToGoal", "ABC → Goal")

    result = horon_db.compile(["A", "B", "C"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == ["AtoB", "BtoC", "ABC", "ABCToGoal"]
    assert result["compiled_route"][2]["from"]["name"] == "A & B & C"


def test_compile_ignores_negated_group_activation(horon_db):
    create_concepts(horon_db, ["A", "B", "Goal", "AB", "ABToGoal"])
    set_relation(horon_db, "AB", "A & B", status="negated")
    set_relation(horon_db, "ABToGoal", "AB → Goal")

    result = horon_db.compile(["A", "B"], "Goal")

    assert result["passed"] is False
    assert result["compiled_route"] == []
    assert result["detour"] is None


def test_compile_can_continue_from_activated_relation_concept(horon_db):
    create_concepts(horon_db, ["A", "B", "Goal", "AtoB", "AtoBToGoal"])
    set_relation(horon_db, "AtoB", "A → B")
    set_relation(horon_db, "AtoBToGoal", "AtoB → Goal")

    result = horon_db.compile(["A"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == ["AtoB", "AtoBToGoal"]
    assert [
        (edge["from"]["name"], edge["to"]["name"])
        for edge in result["compiled_route"]
    ] == [("A", "B"), ("AtoB", "Goal")]


def test_compile_uses_activated_relation_from_previous_waypoint_segment(horon_db):
    create_concepts(horon_db, ["A", "B", "Goal", "AtoB", "AtoBToGoal"])
    set_relation(horon_db, "AtoB", "A → B")
    set_relation(horon_db, "AtoBToGoal", "AtoB → Goal")

    result = horon_db.compile(["A", "B"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == ["AtoB", "AtoBToGoal"]
    assert [
        (edge["from"]["name"], edge["to"]["name"])
        for edge in result["compiled_route"]
    ] == [("A", "B"), ("AtoB", "Goal")]


def test_compile_reports_break_and_detour_separately(horon_db):
    create_concepts(horon_db, ["A", "B", "C", "Goal", "AtoC", "CtoGoal"])
    set_relation(horon_db, "AtoC", "A → C")
    set_relation(horon_db, "CtoGoal", "C → Goal")

    result = horon_db.compile(["A", "B"], "Goal")

    assert result["passed"] is False
    assert result["compiled_route"] == []
    assert result["break"] == {
        "from": {"concept_id": 1, "name": "A"},
        "to": {"concept_id": 2, "name": "B"},
    }
    assert [edge["name"] for edge in result["detour"]] == ["AtoC", "CtoGoal"]
