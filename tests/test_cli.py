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
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    finally:
        conn.close()
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


def test_cli_returns_nonzero_when_command_fails(tmp_path):
    result = run_cli(["read_concept", "__missing__"], tmp_path)

    assert result.returncode != 0
    assert "Error:" in result.stdout


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
        ["update", "Topic", "evidence", "--append", "first evidence"],
        tmp_path,
        db_path=db_path,
    )
    read = run_cli(["read_concept", "Topic"], tmp_path, db_path=db_path)

    assert created.returncode == 0
    assert updated.returncode == 0
    assert read.returncode == 0
    assert "CONCEPT: Topic" in read.stdout
    assert "Disclosure: short note" in read.stdout
    assert "Evidence:\nfirst evidence" in read.stdout


def test_cli_batch_runs_multiple_commands_and_prints_only_last_by_default(tmp_path):
    db_path = init_cli_db(tmp_path)

    result = run_cli(
        ["batch"],
        tmp_path,
        input_text=(
            'create_concept Topic --disclosure "short note"\n'
            'update Topic evidence --append "batch evidence"\n'
            "read_concept Topic\n"
        ),
        db_path=db_path,
    )

    assert result.returncode == 0
    assert result.stdout.count("CONCEPT: Topic") == 1
    assert "Success. Created concept" not in result.stdout
    assert "Evidence:\nbatch evidence" in result.stdout


def test_cli_batch_all_prints_each_command_result(tmp_path):
    db_path = init_cli_db(tmp_path)

    result = run_cli(
        ["batch", "--all"],
        tmp_path,
        input_text=(
            "create_concept Topic\n"
            'update Topic evidence --append "batch evidence"\n'
            "read_concept Topic\n"
        ),
        db_path=db_path,
    )

    assert result.returncode == 0
    assert "Success. Created concept 'Topic'" in result.stdout
    assert "Success. Updated evidence" in result.stdout
    assert "CONCEPT: Topic" in result.stdout


def test_cli_batch_reads_commands_from_file(tmp_path):
    db_path = init_cli_db(tmp_path)
    batch_file = tmp_path / "commands.txt"
    batch_file.write_text(
        'create_concept Topic --disclosure "from file"\n'
        'update Topic evidence --append "file batch evidence"\n'
        "read_concept Topic\n",
        encoding="utf-8",
    )

    result = run_cli(["batch", "--file", str(batch_file)], tmp_path, db_path=db_path)

    assert result.returncode == 0
    assert "CONCEPT: Topic" in result.stdout
    assert "Disclosure: from file" in result.stdout
    assert "Evidence:\nfile batch evidence" in result.stdout


def test_cli_update_accepts_append_file(tmp_path):
    db_path = init_cli_db(tmp_path)
    append_file = tmp_path / "evidence.txt"
    append_file.write_text("evidence from file\n", encoding="utf-8")

    assert run_cli(["create_concept", "Topic"], tmp_path, db_path=db_path).returncode == 0
    updated = run_cli(
        ["update", "Topic", "evidence", "--append-file", str(append_file)],
        tmp_path,
        db_path=db_path,
    )
    read = run_cli(["read_concept", "Topic"], tmp_path, db_path=db_path)

    assert updated.returncode == 0
    assert read.returncode == 0
    assert "Evidence:\nevidence from file" in read.stdout


def test_cli_batch_handles_windows_paths_and_quotes(tmp_path):
    db_path = init_cli_db(tmp_path)
    append_file = tmp_path / "win_evidence.txt"
    append_file.write_text("evidence from windows path\n", encoding="utf-8")
    
    batch_text = (
        "create_concept Topic\n"
        f"update Topic evidence --append-file {str(append_file)}\n"
        "update Topic evidence --append 'hello \"world\"'\n"
        "read_concept Topic\n"
    )
    
    result = run_cli(
        ["batch"],
        tmp_path,
        input_text=batch_text,
        db_path=db_path
    )
    
    assert result.returncode == 0
    assert "evidence from windows path" in result.stdout
    assert 'hello "world"' in result.stdout
