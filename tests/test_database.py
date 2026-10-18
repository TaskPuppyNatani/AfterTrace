import sqlite3

import pytest

from aftertrace.core.errors import DatabaseError
from aftertrace.services import database
from aftertrace.services.database import SCHEMA_VERSION, initialize_database, open_database

EXPECTED_TABLES = {
    "sources",
    "projects",
    "project_sources",
    "sessions",
    "messages",
    "tool_calls",
    "instruction_sources",
    "rules",
    "findings",
    "finding_evidence",
    "rule_drafts",
    "applied_rules",
    "provider_configs",
    "scan_state",
}


def test_create_reopen_and_preserve_records(tmp_path):
    path = tmp_path / "nested" / "aftertrace.sqlite3"
    assert initialize_database(path) == SCHEMA_VERSION
    with open_database(path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert tables == EXPECTED_TABLES
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        connection.execute("INSERT INTO projects (id, display_name) VALUES ('p', 'Example')")
        connection.execute(
            "INSERT INTO sources (id, harness, display_name, location) "
            "VALUES ('s', 'codex', 'Example', 'external-location')"
        )
        connection.execute(
            "INSERT INTO sessions (id, source_id, project_id) VALUES ('session', 's', 'p')"
        )
        text = "Keep original text.\n  Sanitized example."
        connection.execute(
            "INSERT INTO messages (id, session_id, sequence, content) VALUES (?, ?, ?, ?)",
            ("m", "session", 0, text),
        )
    assert initialize_database(path) == SCHEMA_VERSION
    with open_database(path) as connection:
        assert connection.execute("SELECT privacy_state FROM projects").fetchone()[0] == "excluded"
        assert connection.execute("SELECT content FROM messages").fetchone()[0] == text
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_unknown_version_rejected_without_mutation(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(DatabaseError, match="version"):
        initialize_database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []


def test_unrecognized_database_is_not_adopted(tmp_path):
    path = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.execute("INSERT INTO unrelated VALUES ('preserved')")
    with pytest.raises(DatabaseError, match="not an initialized AfterTrace"):
        initialize_database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM unrelated").fetchone()[0] == "preserved"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0


def test_schema_creation_failure_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "failure.sqlite3"
    monkeypatch.setattr(
        database, "_TABLES", {"first_table": "value TEXT", "broken": "not valid sql !"}
    )
    with pytest.raises(DatabaseError) as caught:
        initialize_database(path)
    assert caught.value.detail
    assert isinstance(caught.value.__cause__, sqlite3.Error)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0


def test_corrupt_incomplete_and_unwritable_paths_surface_application_errors(tmp_path):
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a SQLite database")
    with pytest.raises(DatabaseError):
        initialize_database(corrupt)
    incomplete = tmp_path / "incomplete.sqlite3"
    with sqlite3.connect(incomplete) as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    with pytest.raises(DatabaseError):
        initialize_database(incomplete)
    # A file used as a parent directory fails on Windows, macOS, and Linux.
    with pytest.raises(DatabaseError):
        initialize_database(corrupt / "database.sqlite3")


def test_constraints_and_caller_failure_rollback(tmp_path):
    path = tmp_path / "constraints.sqlite3"
    with pytest.raises(DatabaseError):
        with open_database(path) as connection:
            connection.execute("INSERT INTO projects (id, display_name) VALUES ('p', 'Example')")
            connection.execute("INSERT INTO sessions (id, source_id) VALUES ('orphan', 'missing')")
    with open_database(path) as connection:
        assert connection.execute("SELECT count(*) FROM projects").fetchone()[0] == 0
    with pytest.raises(DatabaseError):
        with open_database(path) as connection:
            connection.execute(
                "INSERT INTO projects (id, display_name, privacy_state) "
                "VALUES ('p', 'E', 'invalid')"
            )
