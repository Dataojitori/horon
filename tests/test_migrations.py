import backend.db as db_module


def test_existing_database_applies_a_new_migration(tmp_path, monkeypatch):
    db_path = tmp_path / "existing.db"
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    monkeypatch.setattr(db_module, "_DB_PATH", db_path)
    monkeypatch.setattr(db_module, "_MIGRATIONS_DIR", migrations_dir)

    db_module.HoronDB().close()
    (migrations_dir / "001_future_feature.sql").write_text(
        "CREATE TABLE future_feature (id INTEGER PRIMARY KEY);",
        encoding="utf-8",
    )

    db = db_module.HoronDB()
    try:
        assert db.conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='future_feature'"
        ).fetchone()
        assert db.conn.execute(
            "SELECT version FROM schema_migrations"
        ).fetchone()["version"] == "001_future_feature"
    finally:
        db.close()


def test_new_database_records_bundled_migrations_without_reapplying_them(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "new.db"
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    monkeypatch.setattr(db_module, "_DB_PATH", db_path)
    monkeypatch.setattr(db_module, "_MIGRATIONS_DIR", migrations_dir)
    (migrations_dir / "001_schema_baseline.sql").write_text(
        "ALTER TABLE tags ADD COLUMN source_concept_id INTEGER;",
        encoding="utf-8",
    )

    db = db_module.HoronDB()
    try:
        assert db.conn.execute(
            "SELECT version FROM schema_migrations"
        ).fetchone()["version"] == "001_schema_baseline"
        assert [
            row[1] for row in db.conn.execute("PRAGMA table_info(tags)")
        ].count("source_concept_id") == 1
    finally:
        db.close()
