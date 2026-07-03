import pytest

from conftest import create_concepts, set_relation, compile_path
from frontend.cli import _format_compile, _build_parser, _dispatch


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

    result = compile_path(horon_db, ["A"], "Goal")

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

    result = compile_path(horon_db, ["A"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == ["Direct"]


def test_compile_returns_hypothesis_route_when_no_confirmed_route_exists(horon_db):
    create_concepts(horon_db, ["A", "Goal", "OnlyGuess"])
    set_relation(horon_db, "OnlyGuess", "A → Goal", status=None)

    result = compile_path(horon_db, ["A"], "Goal")

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

    result = compile_path(horon_db, ["A"], "Goal")

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

    # 起点 A 是燃料；B 是必经约束（须由真实边到达）。
    result = compile_path(horon_db, ["A"], "Goal", constraints=["B"])

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

    result = compile_path(horon_db, ["Start"], "Goal")

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

    result = compile_path(horon_db, ["Start"], "Goal")

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

    result = compile_path(horon_db, ["Start"], "Goal")

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

    result = compile_path(horon_db, ["Start"], "Goal")

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

    result = compile_path(horon_db, ["A"], "Goal", constraints=["B"])

    assert result["passed"] is True
    assert edge_names(result) == ["AtoB", "AB", "ABToGoal"]
    assert "ABC" not in edge_names(result)


def test_compile_can_activate_multi_member_group_after_all_members(horon_db):
    create_concepts(
        horon_db,
        ["A", "B", "C", "Goal", "AtoB", "BtoC", "ABC", "ABCToGoal"],
    )
    set_relation(horon_db, "AtoB", "A → B")
    set_relation(horon_db, "BtoC", "B → C")
    set_relation(horon_db, "ABC", "A & B & C")
    set_relation(horon_db, "ABCToGoal", "ABC → Goal")

    result = compile_path(horon_db, ["A"], "Goal", constraints=["B", "C"])

    assert result["passed"] is True
    assert edge_names(result) == ["AtoB", "BtoC", "ABC", "ABCToGoal"]
    assert result["compiled_route"][2]["from"]["name"] == "A & B & C"


def test_compile_ignores_negated_group_activation(horon_db):
    create_concepts(horon_db, ["A", "B", "Goal", "AB", "ABToGoal"])
    set_relation(horon_db, "AB", "A & B", status="negated")
    set_relation(horon_db, "ABToGoal", "AB → Goal", status=None)

    result = compile_path(horon_db, ["A"], "Goal", constraints=["B"])

    assert result["passed"] is False
    assert result["compiled_route"] == []
    assert result["detour"] is None


def test_compile_can_continue_from_activated_relation_concept(horon_db):
    create_concepts(horon_db, ["A", "B", "Goal", "AtoB", "AtoBToGoal"])
    set_relation(horon_db, "AtoB", "A → B")
    set_relation(horon_db, "AtoBToGoal", "AtoB → Goal")

    result = compile_path(horon_db, ["A"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == ["AtoB", "AtoBToGoal"]
    assert [
        (edge["from"]["name"], edge["to"]["name"])
        for edge in result["compiled_route"]
    ] == [("A", "B"), ("AtoB", "Goal")]


def test_compile_uses_activated_relation_from_constraint(horon_db):
    create_concepts(horon_db, ["A", "B", "Goal", "AtoB", "AtoBToGoal"])
    set_relation(horon_db, "AtoB", "A → B")
    set_relation(horon_db, "AtoBToGoal", "AtoB → Goal")

    result = compile_path(horon_db, ["A"], "Goal", constraints=["B"])

    assert result["passed"] is True
    assert edge_names(result) == ["AtoB", "AtoBToGoal"]
    assert [
        (edge["from"]["name"], edge["to"]["name"])
        for edge in result["compiled_route"]
    ] == [("A", "B"), ("AtoB", "Goal")]


def test_compile_reports_unmet_constraint_when_goal_still_reachable(horon_db):
    create_concepts(horon_db, ["A", "B", "C", "Goal", "AtoC", "CtoGoal"])
    set_relation(horon_db, "AtoC", "A → C")
    set_relation(horon_db, "CtoGoal", "C → Goal")

    # Goal 可达（A→C→Goal），但必经约束 B 无边可达。
    result = compile_path(horon_db, ["A"], "Goal", constraints=["B"])

    assert result["passed"] is False
    assert result["break"]["goal_reached"] is True
    assert [c["name"] for c in result["break"]["unmet_constraints"]] == ["B"]
    # 没有满足任何约束的部分路线；撤掉约束后到 goal 的兜底路线进 detour。
    assert edge_names(result) == []
    assert [e["name"] for e in result["detour"]] == ["AtoC", "CtoGoal"]


def test_compile_blocked_node_makes_goal_unreachable_and_is_named(horon_db):
    # 唯一到 Goal 的路是 A→B→Goal；把 B 排除，Goal 就断了。
    create_concepts(horon_db, ["A", "B", "Goal", "AtoB", "BtoGoal"])
    set_relation(horon_db, "AtoB", "A → B")
    set_relation(horon_db, "BtoGoal", "B → Goal")

    result = compile_path(horon_db, ["A"], "Goal", block=["B"])

    assert result["passed"] is False
    assert result["break"]["goal_reached"] is False
    # 排除项原样回带，供文案点名。
    assert [b["name"] for b in result["blocked"]] == ["B"]
    text = _format_compile(result)
    assert "excluded via --block: B" in text


def test_compile_unreachable_goal_without_block_has_clean_message(horon_db):
    create_concepts(horon_db, ["A", "Goal"])

    result = compile_path(horon_db, ["A"], "Goal")

    assert result["passed"] is False
    assert result["blocked"] == []
    text = _format_compile(result)
    assert "goal not reachable from your assumed state." in text
    assert "--block" not in text


def test_compile_unreachable_goal_with_block_has_clean_message_if_unreachable_regardless(horon_db):
    create_concepts(horon_db, ["A", "B", "Goal"])
    
    # Even without blocking B, Goal is unreachable from A.
    result = compile_path(horon_db, ["A"], "Goal", block=["B"])

    assert result["passed"] is False
    assert [b["name"] for b in result["blocked"]] == ["B"]
    assert result["break"]["goal_reachable_without_block"] is False
    
    text = _format_compile(result)
    assert "goal not reachable from your assumed state." in text
    assert "--block" not in text


def test_compile_contradictory_inputs(horon_db):
    create_concepts(horon_db, ["A", "B", "C", "Goal"])
    
    # assume and block intersect
    result = compile_path(horon_db, ["A", "B"], "Goal", block=["B", "C"])
    assert result["passed"] is False
    assert any("assume and block intersect: B" in e for e in result["errors"])
    
    # goal is blocked
    result = compile_path(horon_db, ["A"], "Goal", block=["Goal"])
    assert result["passed"] is False
    assert any("goal (Goal) is blocked" in e for e in result["errors"])
    
    # constraints and block intersect
    result = compile_path(horon_db, ["A"], "Goal", block=["C"], constraints=["C"])
    assert result["passed"] is False
    assert any("constraints and block intersect: C" in e for e in result["errors"])


def test_compile_assumed_constraints_are_met_initially(horon_db):
    create_concepts(horon_db, ["A", "B", "Goal"])
    
    result = compile_path(horon_db, ["A", "B"], "Goal", constraints=["A", "B"])
    
    assert result["passed"] is False
    assert result["break"] is not None
    assert result["break"]["goal_reached"] is False
    assert result["break"]["unmet_constraints"] == []


def test_cli_compile_raises_on_unresolvable_concept(horon_db):
    # 概念名解析失败是坏输入，不是编译结论：CLI 分派应抛异常
    # （走非零退出码 / batch 中止），而非静默返回带 errors 的结果。
    create_concepts(horon_db, ["Real"])
    parser = _build_parser()

    bad = parser.parse_args(["compile", "--goal", "Ghost"])
    with pytest.raises(ValueError, match="Concept not found: Ghost"):
        _dispatch(bad, horon_db)

    # 概念都能解析、只是没路 → 正常返回结论，不抛。
    ok = parser.parse_args(["compile", "--assume", "Real", "--goal", "Real"])
    result = _dispatch(ok, horon_db)
    assert result["passed"] is True  # goal 在 assume 中，平凡通过


def test_compile_constraint_and_goal_reachable_but_not_together(horon_db):
    # 约束 K 经 S→K 可踩到，但 K 是死胡同；Goal 只能走 S→Goal，不经过 K。
    # 两者各自可达，却不在同一条路上。
    create_concepts(horon_db, ["S", "K", "Goal", "StoK", "StoGoal"])
    set_relation(horon_db, "StoK", "S → K")
    set_relation(horon_db, "StoGoal", "S → Goal")

    result = compile_path(horon_db, ["S"], "Goal", constraints=["K"])

    assert result["passed"] is False
    assert result["break"]["goal_reached"] is True
    assert result["break"]["unmet_constraints"] == []
    text = _format_compile(result)
    assert "no route reaches goal while satisfying your constraints" in text
