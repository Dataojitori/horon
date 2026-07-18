import sys
from pathlib import Path

# 把项目根目录加入 sys.path，以便直接执行 pytest 时能找到 backend 模块
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import backend.db as db_module


@pytest.fixture
def horon_db(tmp_path, monkeypatch):
    db_path = tmp_path / "horon-test.db"
    monkeypatch.setattr(db_module, "_DB_PATH", db_path)

    db = db_module.HoronDB()
    try:
        yield db
    finally:
        db.close()


def create_concepts(db, names):
    for name in names:
        db.create_concept(name)


def set_relation(db, name, expression, status="confirmed"):
    db.set(name, "expression", expression)
    if status == "confirmed":
        # 确认关系前，先确认其所有未确认的原子成员概念。
        # 组合概念（已有 expression 的）不动——它们的状态由测试显式控制。
        cid, sc = db._resolve_single_variation(name)
        members = db.conn.execute(
            "SELECT member_concept_id FROM compose_members "
            "WHERE concept_id=? AND short_code=?",
            (cid, sc)
        ).fetchall()
        for row in members:
            mid = row["member_concept_id"]
            is_atom = not db.conn.execute(
                "SELECT 1 FROM compose_members WHERE concept_id=?", (mid,)
            ).fetchone()
            if not is_atom:
                continue
            already_confirmed = db.conn.execute(
                "SELECT 1 FROM variations "
                "WHERE concept_id=? AND status='confirmed'", (mid,)
            ).fetchone()
            if already_confirmed:
                continue
            member_name = db._resolve_concept_name(mid)
            db.set(member_name, "status", "confirmed")
    if status is not None:
        db.set(name, "status", status)


def compile_path(db, assume, goal, *, constraints=(), block=()):
    """新版 compile API 的测试包装。

    旧模型是有序途经点 compile(steps, goal)：steps[0] 是起点、steps[1:] 是
    必经点。新模型改为无序集合：assume（燃料/公理）、constraints（必经，
    无序）、block（禁行）、goal。此包装把测试意图映射到新签名。
    """
    return db.compile(list(assume), list(block), list(constraints), goal)
