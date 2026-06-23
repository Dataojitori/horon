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
    db_module.init_db()

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
    if status is not None:
        db.set(name, "status", status)
