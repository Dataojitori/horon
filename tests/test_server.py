import backend.server as server
from conftest import create_concepts, set_relation


def test_shared_relation_graph_does_not_build_compile_only_indexes(horon_db):
    create_concepts(horon_db, ["A", "B", "AtoB", "AB"])
    set_relation(horon_db, "AtoB", "A → B")
    set_relation(horon_db, "AB", "A & B")

    graph = horon_db._load_relation_graph()

    assert set(vars(graph)) == {"adjacency", "and_groups"}


def test_graph_endpoint_uses_loaded_relation_graph(horon_db, monkeypatch):
    create_concepts(horon_db, ["A", "B", "AtoB", "AB"])
    set_relation(horon_db, "AtoB", "A → B")
    set_relation(horon_db, "AB", "A & B", status=None)
    monkeypatch.setattr(server, "db", horon_db)

    result = server.get_graph()

    assert [(node["name"], node["degree"]) for node in result["nodes"]] == [
        ("A", 2),
        ("B", 2),
        ("AtoB", 1),
        ("AB", 2),
    ]
    assert result["links"] == [
        {
            "source": 1,
            "target": 2,
            "relation_id": 3,
            "status": "confirmed",
            "kind": "directed",
        },
        {
            "source": 1,
            "target": 2,
            "relation_id": 4,
            "status": "hypothesis",
            "kind": "undirected",
        },
    ]
