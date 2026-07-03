import time

from conftest import create_concepts, set_relation, compile_path
from frontend.cli import _format_compile


def edge_names(result):
    return [edge["name"] for edge in result["compiled_route"]]


def edge_statuses(result):
    return [edge["status"] for edge in result["compiled_route"]]


def concept_names(result):
    return [concept["name"] for concept in result["concept_order"]]


# 说明：compile 的旧模型是「有序途经点」(steps[0] 起点、steps[1:] 按序必经)。
# 新模型是无序集合：assume（燃料）、constraints（无序必经）、goal。因此纯粹
# 依赖输入顺序的用例已删除（顺序不再是输入的一部分）；其余用例改用
# assume + constraints 表达，并断言与顺序无关的稳健性质。


def test_compile_large_unordered_group_avoids_parent_factorial(horon_db):
    members = [f"M{i}" for i in range(8)]
    relations = [f"StartM{i}" for i in range(8)]
    create_concepts(horon_db, ["Start", "Group", *members, *relations])
    for member, relation in zip(members, relations):
        set_relation(horon_db, relation, f"Start → {member}")
    set_relation(horon_db, "Group", " & ".join(members))

    started = time.perf_counter()
    result = compile_path(horon_db, ["Start"], "Group", constraints=members)
    elapsed = time.perf_counter() - started

    assert result["passed"] is True
    order = concept_names(result)
    assert all(member in order for member in members)
    assert "Group" in order
    # The old n! enumeration takes tens of seconds for eight independent
    # parents; leave generous headroom for slower CI machines.
    assert elapsed < 5


def test_compile_emits_multi_position_members_then_expression_concept(horon_db):
    create_concepts(
        horon_db,
        ["Start", "A", "B", "Chain", "Goal", "StartA", "ChainGoal"],
    )
    set_relation(horon_db, "StartA", "Start → A")
    set_relation(horon_db, "Chain", "A → B → A")
    set_relation(horon_db, "ChainGoal", "Chain → Goal")

    result = compile_path(horon_db, ["Start"], "Goal", constraints=["A", "B"])

    assert result["passed"] is True
    assert edge_names(result) == ["StartA", "Chain", "ChainGoal"]
    # Chain 是 A → B → A：其后续位置在到达时依序展开为 B、A。
    assert concept_names(result) == [
        "Start", "A", "StartA", "B", "A", "Chain", "Goal", "ChainGoal",
    ]


def test_compile_keeps_all_pure_group_variations_for_arrival(horon_db):
    create_concepts(
        horon_db,
        [
            "Start", "A", "B", "C", "D", "N", "Goal",
            "StartN", "NGoal",
        ],
    )
    set_relation(horon_db, "N", "A & B")
    horon_db.add("N", "variation", "C & D")
    set_relation(horon_db, "StartN", "Start → N")
    set_relation(horon_db, "NGoal", "N → Goal")

    ab = compile_path(horon_db, ["Start"], "Goal", constraints=["A", "B"])
    cd = compile_path(horon_db, ["Start"], "Goal", constraints=["C", "D"])

    assert ab["passed"] is True
    assert cd["passed"] is True
    assert edge_statuses(ab) == ["confirmed", "confirmed"]
    assert edge_statuses(cd) == ["confirmed", "confirmed", "hypothesis"]
    assert edge_names(cd)[-1] == "N"
    assert _format_compile(cd).startswith("BLOCKED.")
    assert concept_names(ab) == ["Start", "A", "B", "N", "StartN", "Goal", "NGoal"]
    assert concept_names(cd) == ["Start", "C", "D", "N", "StartN", "Goal", "NGoal"]


def test_compile_prefers_confirmed_pure_group_when_matches_are_equal(horon_db):
    create_concepts(
        horon_db,
        [
            "Start", "A", "B", "C", "N", "Goal",
            "StartB", "BToC", "CToN", "NGoal",
        ],
    )
    set_relation(horon_db, "N", "A & B")
    horon_db.add("N", "variation", "A & C")
    set_relation(horon_db, "StartB", "Start → B")
    set_relation(horon_db, "BToC", "B → C")
    set_relation(horon_db, "CToN", "C → N")
    set_relation(horon_db, "NGoal", "N → Goal")

    result = compile_path(
        horon_db, ["Start"], "Goal", constraints=["B", "C", "A"])

    assert result["passed"] is True
    assert all(status == "confirmed" for status in edge_statuses(result))


def test_compile_reaches_every_constraint(horon_db):
    # C 故意先于 B 建立：数据库 ID 不能定义顺序。
    create_concepts(
        horon_db,
        [
            "A", "C", "B", "Goal",
            "AtoC", "CtoB", "BtoC", "CtoGoal",
        ],
    )
    set_relation(horon_db, "AtoC", "A → C")
    set_relation(horon_db, "CtoB", "C → B")
    set_relation(horon_db, "BtoC", "B → C")
    set_relation(horon_db, "CtoGoal", "C → Goal")

    result = compile_path(horon_db, ["A"], "Goal", constraints=["B", "C"])

    assert result["passed"] is True
    order = concept_names(result)
    assert "B" in order and "C" in order


def test_compile_reports_unmet_constraint_when_goal_reachable(horon_db):
    create_concepts(
        horon_db,
        ["A", "B", "C", "Goal", "AtoB", "AtoGoal"],
    )
    set_relation(horon_db, "AtoB", "A → B")
    set_relation(horon_db, "AtoGoal", "A → Goal")

    # Goal 可达（A→Goal），约束 B 可达（A→B），但 C 无边可达。
    result = compile_path(horon_db, ["A"], "Goal", constraints=["B", "C"])

    assert result["passed"] is False
    assert result["break"]["goal_reached"] is True
    unmet = {c["name"] for c in result["break"]["unmet_constraints"]}
    assert "C" in unmet


def test_compile_detour_reaches_goal_when_constraint_unmet(horon_db):
    create_concepts(
        horon_db,
        ["A", "B", "C", "Goal", "AtoB", "BtoGoal"],
    )
    set_relation(horon_db, "AtoB", "A → B")
    set_relation(horon_db, "BtoGoal", "B → Goal")

    result = compile_path(horon_db, ["A"], "Goal", constraints=["B", "C"])

    assert result["passed"] is False
    assert result["break"]["goal_reached"] is True
    assert {c["name"] for c in result["break"]["unmet_constraints"]} == {"C"}
    # 部分路线走到能满足的约束 B 为止；撤约束到 goal 的兜底路线进 detour。
    assert edge_names(result) == ["AtoB"]
    assert [e["name"] for e in result["detour"]] == ["AtoB", "BtoGoal"]


def test_compile_detour_reaches_goal_via_relation_concept(horon_db):
    create_concepts(
        horon_db,
        ["A", "B", "C", "Goal", "AtoB", "AtoBGoal"],
    )
    set_relation(horon_db, "AtoB", "A → B")
    set_relation(horon_db, "AtoBGoal", "AtoB → Goal")

    result = compile_path(horon_db, ["A"], "Goal", constraints=["B", "C"])

    assert result["passed"] is False
    assert result["break"]["goal_reached"] is True
    assert {c["name"] for c in result["break"]["unmet_constraints"]} == {"C"}
    assert edge_names(result) == ["AtoB"]
    detour = result["detour"]
    assert [e["name"] for e in detour] == ["AtoB", "AtoBGoal"]
    assert detour[0]["from"]["name"] == "A"


def test_compile_allows_unconstrained_nodes_between_constraints(horon_db):
    create_concepts(
        horon_db,
        [
            "A", "X", "B", "Y", "C", "Goal",
            "AtoX", "XtoB", "BtoY", "YtoC", "CtoGoal",
        ],
    )
    for name, expression in [
        ("AtoX", "A → X"),
        ("XtoB", "X → B"),
        ("BtoY", "B → Y"),
        ("YtoC", "Y → C"),
        ("CtoGoal", "C → Goal"),
    ]:
        set_relation(horon_db, name, expression)

    result = compile_path(horon_db, ["A"], "Goal", constraints=["B", "C"])

    assert result["passed"] is True
    assert edge_names(result) == [
        "AtoX", "XtoB", "BtoY", "YtoC", "CtoGoal",
    ]


def test_compile_reaches_constraints_before_activating_and(horon_db):
    create_concepts(
        horon_db,
        ["Start", "A", "B", "Goal", "StartA", "StartB", "AB", "ABGoal"],
    )
    set_relation(horon_db, "StartA", "Start → A")
    set_relation(horon_db, "StartB", "Start → B")
    set_relation(horon_db, "AB", "A & B")
    set_relation(horon_db, "ABGoal", "AB → Goal")

    result = compile_path(horon_db, ["Start"], "Goal", constraints=["A", "B"])

    assert result["passed"] is True
    # A、B 都必须先到达，AND 组才能激活并前往 Goal。
    assert set(edge_names(result)) == {"StartA", "StartB", "AB", "ABGoal"}
    assert edge_names(result)[-2:] == ["AB", "ABGoal"]


def test_compile_arrow_to_concept_containing_constraints_uses_them(horon_db):
    create_concepts(
        horon_db,
        ["Start", "A", "B", "N", "Goal", "StartN", "NGoal"],
    )
    set_relation(horon_db, "StartN", "Start → N")
    set_relation(horon_db, "N", "A & B")
    set_relation(horon_db, "NGoal", "N → Goal")

    result = compile_path(horon_db, ["Start"], "Goal", constraints=["A", "B"])

    assert result["passed"] is True
    assert edge_names(result) == ["StartN", "NGoal"]


def test_compile_unreachable_member_deadlocks_and_group(horon_db):
    create_concepts(
        horon_db,
        ["Start", "A", "B", "N", "Goal", "StartN", "NGoal"],
    )
    set_relation(horon_db, "StartN", "Start → N")
    set_relation(horon_db, "N", "A & B")
    set_relation(horon_db, "NGoal", "N → Goal")

    result = compile_path(horon_db, ["Start"], "Goal", constraints=["B"])

    assert result["passed"] is False
    # Goal 可经 Start→N→Goal 直达，但 N 的成员 A 无法到达，
    # 因此约束 B 无法通过 AND 展开被满足。
    assert {c["name"] for c in result["break"]["unmet_constraints"]} == {"B"}


def test_compile_uses_expression_after_other_members_are_reached(horon_db):
    create_concepts(
        horon_db,
        [
            "Start", "A", "B", "X", "N", "Goal",
            "StartA", "AX", "XN", "NGoal",
        ],
    )
    set_relation(horon_db, "StartA", "Start → A")
    set_relation(horon_db, "AX", "A → X")
    set_relation(horon_db, "XN", "X → N")
    set_relation(horon_db, "N", "A & B")
    set_relation(horon_db, "NGoal", "N → Goal")

    result = compile_path(horon_db, ["Start"], "Goal", constraints=["B"])

    assert result["passed"] is True
    assert edge_names(result) == ["StartA", "AX", "XN", "NGoal"]


def test_compile_reaches_goal_with_relation_concepts_as_constraints(horon_db):
    create_concepts(
        horon_db,
        ["Start", "B", "C", "Goal", "StartB", "StartC", "BC", "BCGoal"],
    )
    set_relation(horon_db, "StartB", "Start → B")
    set_relation(horon_db, "StartC", "Start → C")
    set_relation(horon_db, "BC", "B & C")
    set_relation(horon_db, "BCGoal", "BC → Goal")

    result = compile_path(
        horon_db, ["Start"], "Goal", constraints=["StartB", "StartC"])

    assert result["passed"] is True


def test_compile_tries_another_arrival_when_first_choice_is_a_dead_end(
    horon_db,
):
    create_concepts(
        horon_db,
        [
            "Start", "A", "B", "C", "N", "Join", "Goal",
            "StartA", "AtoB", "StartN", "JoinGoal",
        ],
    )
    set_relation(horon_db, "StartA", "Start → A")
    set_relation(horon_db, "AtoB", "A → B")
    set_relation(horon_db, "StartN", "Start → N")
    set_relation(horon_db, "N", "B & C")
    set_relation(horon_db, "Join", "A & N")
    set_relation(horon_db, "JoinGoal", "Join → Goal")

    result = compile_path(
        horon_db, ["Start"], "Goal", constraints=["A", "B", "C"])

    assert result["passed"] is True
    assert edge_names(result) == ["StartA", "StartN", "Join", "JoinGoal"]


def test_compile_uses_a_concrete_arrow_for_each_constraint(horon_db):
    create_concepts(
        horon_db,
        ["A", "B", "C", "Goal", "AtoB", "AtoC", "CGoal", "BC", "BCGoal"],
    )
    set_relation(horon_db, "AtoB", "A → B")
    set_relation(horon_db, "AtoC", "A → C")
    set_relation(horon_db, "CGoal", "C → Goal")
    set_relation(horon_db, "BC", "B & C")
    set_relation(horon_db, "BCGoal", "BC → Goal")

    result = compile_path(horon_db, ["A"], "Goal", constraints=["B", "C"])

    assert result["passed"] is True
    # 约束 B 必须到达，因此走 B&C 组而非 C→Goal 捷径。
    assert edge_names(result) == ["AtoB", "AtoC", "BC", "BCGoal"]


def test_compile_long_chain_does_not_expand_constraint_subsets(horon_db):
    waypoint_count = 30
    waypoints = [f"W{i}" for i in range(waypoint_count)]
    chain = ["Start", *waypoints, "Goal"]
    relations = [f"Edge{i}" for i in range(len(chain) - 1)]
    create_concepts(horon_db, [*chain, *relations, "U", "V", "UV"])
    for relation, source, target in zip(relations, chain, chain[1:]):
        set_relation(horon_db, relation, f"{source} → {target}")
    # An unrelated & elsewhere in the database must not select mask search.
    set_relation(horon_db, "UV", "U & V")

    started = time.perf_counter()
    result = compile_path(
        horon_db, ["Start"], "Goal", constraints=chain[1:-1])
    elapsed = time.perf_counter() - started

    assert result["passed"] is True
    assert edge_names(result) == relations
    # 旧的任意 mask 搜索会生成约 2^30 个状态，无法在这个上限内完成。
    assert elapsed < 2.0


def test_compile_ignores_reachable_and_group_that_cannot_lead_to_goal(horon_db):
    waypoint_count = 20
    waypoints = [f"W{i}" for i in range(waypoint_count)]
    chain = ["Start", *waypoints, "Goal"]
    relations = [f"Edge{i}" for i in range(len(chain) - 1)]
    create_concepts(
        horon_db,
        [*chain, *relations, "U", "V", "UV", "StartU", "StartV"],
    )
    for relation, source, target in zip(relations, chain, chain[1:]):
        set_relation(horon_db, relation, f"{source} → {target}")
    set_relation(horon_db, "StartU", "Start → U")
    set_relation(horon_db, "StartV", "Start → V")
    set_relation(horon_db, "UV", "U & V")

    started = time.perf_counter()
    result = compile_path(
        horon_db, ["Start"], "Goal", constraints=chain[1:-1])
    elapsed = time.perf_counter() - started

    assert result["passed"] is True
    assert edge_names(result) == relations
    assert elapsed < 2.0


def test_compile_long_chain_through_and_stays_compact(horon_db):
    waypoint_count = 30
    waypoints = [f"W{i}" for i in range(waypoint_count)]
    chain = ["Start", *waypoints]
    relations = [f"Edge{i}" for i in range(len(chain) - 1)]
    create_concepts(
        horon_db,
        [*chain, *relations, "U", "StartU", "Join", "JoinGoal", "Goal"],
    )
    for relation, source, target in zip(relations, chain, chain[1:]):
        set_relation(horon_db, relation, f"{source} → {target}")
    set_relation(horon_db, "StartU", "Start → U")
    set_relation(horon_db, "Join", f"{waypoints[-1]} & U")
    set_relation(horon_db, "JoinGoal", "Join → Goal")

    started = time.perf_counter()
    result = compile_path(horon_db, ["Start"], "Goal", constraints=waypoints)
    elapsed = time.perf_counter() - started

    assert result["passed"] is True
    names = edge_names(result)
    assert set(names) == {*relations, "StartU", "Join", "JoinGoal"}
    assert len(names) == len(relations) + 3
    assert names[-2:] == ["Join", "JoinGoal"]
    assert elapsed < 2.0


def test_compile_keeps_nested_and_groups_relevant_to_goal(horon_db):
    create_concepts(
        horon_db,
        [
            "Start", "A", "B", "C", "Goal",
            "StartA", "StartB", "StartC", "AB", "ABC", "ABCGoal",
        ],
    )
    set_relation(horon_db, "StartA", "Start → A")
    set_relation(horon_db, "StartB", "Start → B")
    set_relation(horon_db, "StartC", "Start → C")
    set_relation(horon_db, "AB", "A & B")
    set_relation(horon_db, "ABC", "AB & C")
    set_relation(horon_db, "ABCGoal", "ABC → Goal")

    result = compile_path(
        horon_db, ["Start"], "Goal", constraints=["A", "B", "C"])

    assert result["passed"] is True
    names = edge_names(result)
    # 约束无序：只断言与顺序无关的结构性质。
    assert set(names) == {
        "StartA", "StartB", "StartC", "AB", "ABC", "ABCGoal",
    }
    assert names.index("AB") < names.index("ABC") < names.index("ABCGoal")
    assert names[-1] == "ABCGoal"
