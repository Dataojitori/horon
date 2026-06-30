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
    set_relation(horon_db, "ABG", "AB → Goal", status=None)

    result = horon_db.compile(["A", "B"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == ["AtoB", "BX", "XG"]
    assert edge_statuses(result) == ["confirmed", "confirmed", "confirmed"]
    assert "AB" not in edge_names(result)


def test_compile_counts_hypotheses_across_parallel_and_branches(horon_db):
    create_concepts(
        horon_db,
        [
            "Start", "A", "B", "C", "X", "Y", "Z", "Join", "Goal",
            "StartA", "StartB", "StartC", "ABC", "ABCJoin",
            "StartX", "XY", "YZ", "ZJoin", "JoinGoal",
        ],
    )
    for name, expression in [
        ("StartA", "Start → A"),
        ("StartB", "Start → B"),
        ("StartC", "Start → C"),
    ]:
        set_relation(horon_db, name, expression, status=None)
    set_relation(horon_db, "ABC", "A & B & C")
    set_relation(horon_db, "ABCJoin", "ABC → Join")

    set_relation(horon_db, "StartX", "Start → X", status=None)
    set_relation(horon_db, "XY", "X → Y")
    set_relation(horon_db, "YZ", "Y → Z")
    set_relation(horon_db, "ZJoin", "Z → Join")
    set_relation(horon_db, "JoinGoal", "Join → Goal")

    result = horon_db.compile(["Start"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == [
        "StartX", "XY", "YZ", "ZJoin", "JoinGoal",
    ]
    assert edge_statuses(result).count("hypothesis") == 1


def test_compile_counts_total_jumps_across_parallel_and_branches(horon_db):
    create_concepts(
        horon_db,
        [
            "Start", "A", "B", "C", "X", "Y", "Z", "Join", "Goal",
            "StartA", "StartB", "StartC", "ABC", "ABCJoin",
            "StartX", "XY", "YZ", "ZJoin", "JoinGoal",
        ],
    )
    for name, expression in [
        ("StartA", "Start → A"),
        ("StartB", "Start → B"),
        ("StartC", "Start → C"),
        ("ABC", "A & B & C"),
        ("ABCJoin", "ABC → Join"),
        ("StartX", "Start → X"),
        ("XY", "X → Y"),
        ("YZ", "Y → Z"),
        ("ZJoin", "Z → Join"),
        ("JoinGoal", "Join → Goal"),
    ]:
        set_relation(horon_db, name, expression)

    result = horon_db.compile(["Start"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == [
        "StartX", "XY", "YZ", "ZJoin", "JoinGoal",
    ]


def test_compile_deduplicates_shared_prefix_when_costing_and_branches(
    horon_db,
):
    create_concepts(
        horon_db,
        [
            "Start", "Shared", "A", "B", "X", "Y", "Z", "V", "W",
            "Join",
            "Goal", "StartShared", "SharedA", "SharedB", "AB", "ABJoin",
            "StartX", "XY", "YZ", "ZV", "VW", "WJoin", "JoinGoal",
        ],
    )
    set_relation(horon_db, "StartShared", "Start → Shared", status=None)
    for name, expression in [
        ("SharedA", "Shared → A"),
        ("SharedB", "Shared → B"),
        ("AB", "A & B"),
        ("ABJoin", "AB → Join"),
        ("XY", "X → Y"),
        ("YZ", "Y → Z"),
        ("ZV", "Z → V"),
        ("VW", "V → W"),
        ("WJoin", "W → Join"),
        ("JoinGoal", "Join → Goal"),
    ]:
        set_relation(horon_db, name, expression)
    set_relation(horon_db, "StartX", "Start → X", status=None)

    result = horon_db.compile(["Start"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == [
        "StartShared", "SharedA", "SharedB", "AB", "ABJoin", "JoinGoal",
    ]
    assert edge_statuses(result).count("hypothesis") == 1


def test_compile_keeps_locally_longer_routes_that_share_a_hypothesis(
    horon_db,
):
    create_concepts(
        horon_db,
        [
            "Start", "Shared", "A", "B", "Join", "Goal",
            "DirectA", "DirectB", "StartShared", "SharedA", "SharedB",
            "AB", "ABJoin", "JoinGoal",
        ],
    )
    for name, expression in [
        ("DirectA", "Start → A"),
        ("DirectB", "Start → B"),
        ("StartShared", "Start → Shared"),
    ]:
        set_relation(horon_db, name, expression, status=None)
    for name, expression in [
        ("SharedA", "Shared → A"),
        ("SharedB", "Shared → B"),
        ("AB", "A & B"),
        ("ABJoin", "AB → Join"),
        ("JoinGoal", "Join → Goal"),
    ]:
        set_relation(horon_db, name, expression)

    result = horon_db.compile(["Start"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == [
        "StartShared", "SharedA", "SharedB", "AB", "ABJoin", "JoinGoal",
    ]
    assert edge_statuses(result).count("hypothesis") == 1


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
    set_relation(horon_db, "ABToGoal", "AB → Goal", status=None)

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
