import time

from conftest import create_concepts, set_relation
from frontend.cli import _format_compile


def edge_names(result):
    return [edge["name"] for edge in result["compiled_route"]]


def edge_statuses(result):
    return [edge["status"] for edge in result["compiled_route"]]


def concept_names(result):
    return [concept["name"] for concept in result["concept_order"]]


def test_compile_large_unordered_position_avoids_parent_factorial(horon_db):
    members = [f"M{i}" for i in range(8)]
    relations = [f"StartM{i}" for i in range(8)]
    create_concepts(horon_db, ["Start", "Group", *members, *relations])
    for member, relation in zip(members, relations):
        set_relation(horon_db, relation, f"Start → {member}")
    set_relation(horon_db, "Group", " & ".join(members))

    requested = ["Start", *reversed(members)]
    started = time.perf_counter()
    result = horon_db.compile(requested, "Group")
    elapsed = time.perf_counter() - started

    assert result["passed"] is True
    order = concept_names(result)
    indexes = [order.index(name) for name in [*requested, "Group"]]
    assert indexes == sorted(indexes)
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

    result = horon_db.compile(["Start", "A", "B", "A", "Chain"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == ["StartA", "Chain", "ChainGoal"]
    assert concept_names(result) == [
        "Start", "A", "StartA", "B", "A", "Chain", "Goal", "ChainGoal",
    ]


def test_compile_orders_members_inside_each_position_to_match_steps(horon_db):
    create_concepts(
        horon_db,
        ["Start", "M", "N", "Q", "Goal", "StartM", "StartN", "QGoal"],
    )
    set_relation(horon_db, "StartM", "Start → M")
    set_relation(horon_db, "StartN", "Start → N")
    set_relation(horon_db, "Q", "M & N")
    set_relation(horon_db, "QGoal", "Q → Goal")

    mn = horon_db.compile(["Start", "M", "N", "Q"], "Goal")
    nm = horon_db.compile(["Start", "N", "M", "Q"], "Goal")

    assert mn["passed"] is True
    assert nm["passed"] is True
    assert concept_names(mn).index("M") < concept_names(mn).index("N")
    assert concept_names(nm).index("N") < concept_names(nm).index("M")


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

    ab = horon_db.compile(["Start", "A", "B", "N"], "Goal")
    cd = horon_db.compile(["Start", "C", "D", "N"], "Goal")

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

    result = horon_db.compile(["Start", "B", "C", "A", "N"], "Goal")

    assert result["passed"] is True
    assert all(status == "confirmed" for status in edge_statuses(result))


def test_compile_reaches_each_waypoint_in_input_order(horon_db):
    # C is deliberately created before B: database IDs must not define order.
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

    result = horon_db.compile(["A", "B", "C"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == ["AtoC", "CtoB", "BtoC", "CtoGoal"]


def test_compile_keeps_verified_prefix_before_break(horon_db):
    create_concepts(
        horon_db,
        ["A", "B", "C", "Goal", "AtoB", "AtoGoal"],
    )
    set_relation(horon_db, "AtoB", "A → B")
    set_relation(horon_db, "AtoGoal", "A → Goal")

    result = horon_db.compile(["A", "B", "C"], "Goal")

    assert result["passed"] is False
    assert edge_names(result) == ["AtoB"]
    assert result["break"]["from"]["name"] == "B"
    assert result["break"]["to"]["name"] == "C"


def test_compile_detour_starts_from_established_prefix(horon_db):
    create_concepts(
        horon_db,
        ["A", "B", "C", "Goal", "AtoB", "BtoGoal"],
    )
    set_relation(horon_db, "AtoB", "A → B")
    set_relation(horon_db, "BtoGoal", "B → Goal")

    result = horon_db.compile(["A", "B", "C"], "Goal")

    assert result["passed"] is False
    assert edge_names(result) == ["AtoB"]
    assert [edge["name"] for edge in result["detour"]] == ["BtoGoal"]


def test_compile_detour_reaches_relation_concept_from_readable_start(horon_db):
    create_concepts(
        horon_db,
        ["A", "B", "C", "Goal", "AtoB", "AtoBGoal"],
    )
    set_relation(horon_db, "AtoB", "A → B")
    set_relation(horon_db, "AtoBGoal", "AtoB → Goal")

    result = horon_db.compile(["A", "B", "C"], "Goal")

    assert result["passed"] is False
    assert edge_names(result) == ["AtoB"]
    assert [edge["name"] for edge in result["detour"]] == [
        "AtoB", "AtoBGoal",
    ]
    assert result["detour"][0]["from"]["name"] == "A"


def test_compile_allows_unconstrained_nodes_between_waypoints(horon_db):
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

    result = horon_db.compile(["A", "B", "C"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == [
        "AtoX", "XtoB", "BtoY", "YtoC", "CtoGoal",
    ]


def test_compile_reaches_parallel_steps_before_activating_and(horon_db):
    create_concepts(
        horon_db,
        ["Start", "A", "B", "Goal", "StartA", "StartB", "AB", "ABGoal"],
    )
    set_relation(horon_db, "StartA", "Start → A")
    set_relation(horon_db, "StartB", "Start → B")
    set_relation(horon_db, "AB", "A & B")
    set_relation(horon_db, "ABGoal", "AB → Goal")

    ab = horon_db.compile(["Start", "A", "B"], "Goal")
    ba = horon_db.compile(["Start", "B", "A"], "Goal")

    assert ab["passed"] is True
    assert ba["passed"] is True
    assert edge_names(ab) == ["StartA", "StartB", "AB", "ABGoal"]
    assert edge_names(ba) == ["StartB", "StartA", "AB", "ABGoal"]


def test_compile_arrow_to_concept_containing_steps_uses_them(horon_db):
    create_concepts(
        horon_db,
        ["Start", "A", "B", "N", "Goal", "StartN", "NGoal"],
    )
    set_relation(horon_db, "StartN", "Start → N")
    set_relation(horon_db, "N", "A & B")
    set_relation(horon_db, "NGoal", "N → Goal")

    result = horon_db.compile(["Start", "A", "B"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == ["StartN", "NGoal"]


def test_compile_requires_other_expression_members_to_be_reached(horon_db):
    create_concepts(
        horon_db,
        ["Start", "A", "B", "N", "Goal", "StartN", "NGoal"],
    )
    set_relation(horon_db, "StartN", "Start → N")
    set_relation(horon_db, "N", "A & B")
    set_relation(horon_db, "NGoal", "N → Goal")

    result = horon_db.compile(["Start", "B"], "Goal")

    assert result["passed"] is False
    assert result["compiled_route"] == []
    assert result["break"]["from"]["name"] == "Start"
    assert result["break"]["to"]["name"] == "B"


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

    result = horon_db.compile(["Start", "B"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == ["StartA", "AX", "XN", "NGoal"]


def test_compile_does_not_merge_untraveled_parallel_relation_names(horon_db):
    create_concepts(
        horon_db,
        ["Start", "B", "C", "Goal", "StartB", "StartC", "BC", "BCGoal"],
    )
    set_relation(horon_db, "StartB", "Start → B")
    set_relation(horon_db, "StartC", "Start → C")
    set_relation(horon_db, "BC", "B & C")
    set_relation(horon_db, "BCGoal", "BC → Goal")

    forward = horon_db.compile(["Start", "StartB", "StartC"], "Goal")
    reverse = horon_db.compile(["Start", "StartC", "StartB"], "Goal")

    # Relation concepts are now real ordered occurrences: StartB appears
    # after B, and StartC appears after C.
    assert forward["passed"] is True
    assert reverse["passed"] is True


def test_compile_can_skip_early_waypoint_occurrence(horon_db):
    create_concepts(
        horon_db,
        ["A", "B", "C", "Goal", "AtoC", "CtoB", "BtoC", "CtoGoal"],
    )
    set_relation(horon_db, "AtoC", "A → C")
    set_relation(horon_db, "CtoB", "C → B")
    set_relation(horon_db, "BtoC", "B → C")
    set_relation(horon_db, "CtoGoal", "C → Goal")

    result = horon_db.compile(["A", "B", "C"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == ["AtoC", "CtoB", "BtoC", "CtoGoal"]


def test_compile_rejects_waypoint_branch_that_never_rejoins_goal(horon_db):
    create_concepts(
        horon_db,
        ["A", "B", "C", "Goal", "AtoC", "CtoB", "CtoGoal"],
    )
    set_relation(horon_db, "AtoC", "A → C")
    set_relation(horon_db, "CtoB", "C → B")
    set_relation(horon_db, "CtoGoal", "C → Goal")

    result = horon_db.compile(["A", "B", "C"], "Goal")

    assert result["passed"] is False
    assert edge_names(result) == ["AtoC", "CtoB"]
    assert result["break"]["from"]["name"] == "B"
    assert result["break"]["to"]["name"] == "C"


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

    result = horon_db.compile(["Start", "A", "B", "C"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == ["StartA", "StartN", "Join", "JoinGoal"]


def test_compile_uses_a_concrete_arrow_for_each_ordered_waypoint(horon_db):
    create_concepts(
        horon_db,
        ["A", "B", "C", "Goal", "AtoB", "AtoC", "CGoal", "BC", "BCGoal"],
    )
    set_relation(horon_db, "AtoB", "A → B")
    set_relation(horon_db, "AtoC", "A → C")
    set_relation(horon_db, "CGoal", "C → Goal")
    set_relation(horon_db, "BC", "B & C")
    set_relation(horon_db, "BCGoal", "BC → Goal")

    result = horon_db.compile(["A", "B", "C"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == ["AtoB", "AtoC", "BC", "BCGoal"]


def test_compile_long_ordered_chain_does_not_expand_waypoint_subsets(horon_db):
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
    result = horon_db.compile(chain[:-1], "Goal")
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
    result = horon_db.compile(chain[:-1], "Goal")
    elapsed = time.perf_counter() - started

    assert result["passed"] is True
    assert edge_names(result) == relations
    assert elapsed < 2.0


def test_compile_long_ordered_chain_through_and_stays_compact(horon_db):
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
    result = horon_db.compile(chain, "Goal")
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

    result = horon_db.compile(["Start", "A", "B", "C"], "Goal")

    assert result["passed"] is True
    assert edge_names(result) == [
        "StartA", "StartB", "AB", "StartC", "ABC", "ABCGoal",
    ]


def test_compile_does_not_treat_relation_shadow_as_arrow_destination(horon_db):
    create_concepts(horon_db, ["A", "B", "Goal", "AtoB", "EdgeGoal"])
    set_relation(horon_db, "AtoB", "A → B")
    set_relation(horon_db, "EdgeGoal", "AtoB → Goal")

    destination_first = horon_db.compile(["A", "B", "AtoB"], "Goal")
    edge_first = horon_db.compile(["A", "AtoB", "B"], "Goal")

    # AtoB is defined to occur after B, rather than simultaneously with it.
    assert destination_first["passed"] is True
    assert edge_first["passed"] is False


def test_compile_allows_duplicate_waypoints_and_reports_break(horon_db):
    """重复 occurrence 是合法约束；没有对应路线时应正常报告断点。"""
    create_concepts(horon_db, ["A", "Goal"])

    result = horon_db.compile(["A", "A"], "Goal")

    assert result["passed"] is False
    assert result["errors"] == []
    assert result["break"]["to"]["name"] == "A"
