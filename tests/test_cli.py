import os
import sqlite3
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI = PROJECT_ROOT / "frontend" / "cli.py"
SCHEMA = PROJECT_ROOT / "backend" / "schema.sql"


def init_cli_db(tmp_path):
    db_path = tmp_path / "cli-test.db"
    # let HoronDB.__init__ create and migrate the database automatically
    return db_path


def run_cli(args, tmp_path, input_text=None, db_path=None):
    env = os.environ.copy()
    env["HORON_DB"] = str(db_path or tmp_path / "cli-test.db")
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=PROJECT_ROOT,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )


def read_audit_log(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT command, concept_id, concept_name, short_code, "
            "sub_action, success FROM cli_audit_log ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_cli_returns_nonzero_when_command_fails(tmp_path):
    result = run_cli(["read_concept", "__missing__"], tmp_path)

    assert result.returncode != 0
    assert "Fail." in result.stdout


def test_cli_batch_returns_nonzero_on_invalid_line(tmp_path):
    result = run_cli(["batch"], tmp_path, input_text="not_a_command\n")

    assert result.returncode != 0
    assert "Line 1: invalid command" in result.stdout


def test_cli_create_update_and_read_share_the_same_database(tmp_path):
    db_path = init_cli_db(tmp_path)

    created = run_cli(
        ["create_concept", "Topic", "--disclosure", "short note"],
        tmp_path,
        db_path=db_path,
    )
    updated = run_cli(
        ["update", "Topic", "content", "--append", "first content"],
        tmp_path,
        db_path=db_path,
    )
    read = run_cli(["read_concept", "Topic"], tmp_path, db_path=db_path)

    assert created.returncode == 0
    assert updated.returncode == 0
    assert read.returncode == 0
    assert "CONCEPT: Topic" in read.stdout
    assert "Disclosure: short note" in read.stdout
    assert "Content:\nfirst content" in read.stdout


def test_cli_batch_runs_multiple_commands_and_prints_only_last_by_default(tmp_path):
    db_path = init_cli_db(tmp_path)

    result = run_cli(
        ["batch"],
        tmp_path,
        input_text=(
            'create_concept Topic --disclosure "short note"\n'
            'update Topic content --append "batch content"\n'
            "read_concept Topic\n"
        ),
        db_path=db_path,
    )

    assert result.returncode == 0
    assert result.stdout.count("CONCEPT: Topic") == 1
    assert "Success. Created concept" not in result.stdout
    assert "Content:\nbatch content" in result.stdout


def test_cli_batch_all_prints_each_command_result(tmp_path):
    db_path = init_cli_db(tmp_path)

    result = run_cli(
        ["batch", "--all"],
        tmp_path,
        input_text=(
            "create_concept Topic\n"
            'update Topic content --append "batch content"\n'
            "read_concept Topic\n"
        ),
        db_path=db_path,
    )

    assert result.returncode == 0
    assert "Success. Created concept 'Topic'" in result.stdout
    assert "Success. Updated content" in result.stdout
    assert "CONCEPT: Topic" in result.stdout


def test_cli_batch_reads_commands_from_file(tmp_path):
    db_path = init_cli_db(tmp_path)
    batch_file = tmp_path / "commands.txt"
    batch_file.write_text(
        'create_concept Topic --disclosure "from file"\n'
        'update Topic content --append "file batch content"\n'
        "read_concept Topic\n",
        encoding="utf-8",
    )

    result = run_cli(["batch", "--file", str(batch_file)], tmp_path, db_path=db_path)

    assert result.returncode == 0
    assert "CONCEPT: Topic" in result.stdout
    assert "Disclosure: from file" in result.stdout
    assert "Content:\nfile batch content" in result.stdout


def test_cli_update_accepts_append_file(tmp_path):
    db_path = init_cli_db(tmp_path)
    append_file = tmp_path / "content.txt"
    append_file.write_text("content from file\n", encoding="utf-8")

    assert run_cli(["create_concept", "Topic"], tmp_path, db_path=db_path).returncode == 0
    updated = run_cli(
        ["update", "Topic", "content", "--append-file", str(append_file)],
        tmp_path,
        db_path=db_path,
    )
    read = run_cli(["read_concept", "Topic"], tmp_path, db_path=db_path)

    assert updated.returncode == 0
    assert read.returncode == 0
    assert "Content:\ncontent from file" in read.stdout


def test_cli_batch_handles_windows_paths_and_quotes(tmp_path):
    db_path = init_cli_db(tmp_path)
    append_file = tmp_path / "win_content.txt"
    append_file.write_text("content from windows path\n", encoding="utf-8")
    
    batch_text = (
        "create_concept Topic\n"
        f"update Topic content --append-file {str(append_file)}\n"
        "update Topic content --append 'hello \"world\"'\n"
        "read_concept Topic\n"
    )
    
    result = run_cli(
        ["batch"],
        tmp_path,
        input_text=batch_text,
        db_path=db_path
    )
    
    assert result.returncode == 0
    assert "content from windows path" in result.stdout
    assert 'hello "world"' in result.stdout


def test_cli_successful_write_records_target_in_audit_log(tmp_path):
    db_path = init_cli_db(tmp_path)

    created = run_cli(["create_concept", "Topic"], tmp_path, db_path=db_path)
    updated = run_cli(
        ["update", "Topic", "content", "--append", "proof"],
        tmp_path,
        db_path=db_path,
    )

    assert created.returncode == 0
    assert updated.returncode == 0
    rows = read_audit_log(db_path)
    assert len(rows) == 2
    assert rows[0]["command"] == "create_concept"
    assert rows[0]["concept_id"] is not None
    assert rows[0]["concept_name"] == "Topic"
    assert rows[0]["short_code"]
    assert rows[0]["sub_action"] is None
    assert rows[0]["success"] == 1
    assert dict(rows[1]) == {
        "command": "update",
        "concept_id": rows[0]["concept_id"],
        "concept_name": "Topic",
        "short_code": rows[0]["short_code"],
        "sub_action": "content",
        "success": 1,
    }


def test_cli_failed_write_records_failure_and_rolls_back(tmp_path):
    db_path = init_cli_db(tmp_path)
    assert run_cli(
        ["create_concept", "Topic"], tmp_path, db_path=db_path
    ).returncode == 0

    duplicate = run_cli(
        ["create_concept", "Topic"], tmp_path, db_path=db_path
    )

    assert duplicate.returncode != 0
    conn = sqlite3.connect(db_path)
    try:
        concept_count = conn.execute(
            "SELECT COUNT(*) FROM concepts WHERE name='Topic'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert concept_count == 1
    rows = read_audit_log(db_path)
    assert len(rows) == 2
    assert rows[-1]["command"] == "create_concept"
    assert rows[-1]["success"] == 0


def test_cli_delete_final_variation_keeps_deleted_target_in_audit_log(tmp_path):
    db_path = init_cli_db(tmp_path)
    assert run_cli(
        ["create_concept", "Disposable"], tmp_path, db_path=db_path
    ).returncode == 0
    created = read_audit_log(db_path)[0]

    deleted = run_cli(["delete", "Disposable"], tmp_path, db_path=db_path)

    assert deleted.returncode == 0
    conn = sqlite3.connect(db_path)
    try:
        concept_count = conn.execute(
            "SELECT COUNT(*) FROM concepts WHERE id=?",
            (created["concept_id"],),
        ).fetchone()[0]
    finally:
        conn.close()
    assert concept_count == 0
    row = read_audit_log(db_path)[-1]
    assert dict(row) == {
        "command": "delete",
        "concept_id": created["concept_id"],
        "concept_name": "Disposable",
        "short_code": created["short_code"],
        "sub_action": None,
        "success": 1,
    }


def test_cli_batch_records_each_executed_subcommand(tmp_path):
    db_path = init_cli_db(tmp_path)

    result = run_cli(
        ["batch"],
        tmp_path,
        input_text=(
            "create_concept Topic\n"
            'update Topic content --append "batch content"\n'
            "read_concept Topic\n"
        ),
        db_path=db_path,
    )

    assert result.returncode == 0
    rows = read_audit_log(db_path)
    assert [row["command"] for row in rows] == [
        "create_concept",
        "update",
        "read_concept",
    ]
    assert [row["success"] for row in rows] == [1, 1, 1]
    assert len({row["concept_id"] for row in rows}) == 1


def test_cli_rename_audit_uses_new_name_and_same_concept_id(tmp_path):
    db_path = init_cli_db(tmp_path)
    assert run_cli(
        ["create_concept", "OldName"], tmp_path, db_path=db_path
    ).returncode == 0
    created = read_audit_log(db_path)[0]

    renamed = run_cli(
        ["set", "OldName", "name", "NewName"], tmp_path, db_path=db_path
    )

    assert renamed.returncode == 0
    row = read_audit_log(db_path)[-1]
    assert row["command"] == "set"
    assert row["concept_id"] == created["concept_id"]
    assert row["concept_name"] == "NewName"
    assert row["short_code"] is None
    assert row["sub_action"] == "name"
    assert row["success"] == 1
