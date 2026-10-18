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
    "discovered_sessions",
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


def make_version_one(path, privacy="included"):
    # _TABLES intentionally remains the original foundation schema; v2 is a separate migration.
    with sqlite3.connect(path) as connection:
        for table, columns in database._TABLES.items():
            connection.execute(
                f"CREATE TABLE {table} (id TEXT PRIMARY KEY NOT NULL, "
                f"metadata TEXT NOT NULL DEFAULT '{{}}', {columns})"
            )
        connection.execute("PRAGMA user_version = 1")
        connection.execute(
            "INSERT INTO sources (id, harness, display_name, location) "
            "VALUES ('s', 'codex', 'Example', 'example')"
        )
        connection.execute(
            "INSERT INTO projects (id, display_name, privacy_state, metadata) VALUES (?, ?, ?, ?)",
            ("p", "Example", privacy, '{"custom":"preserved"}'),
        )
        connection.execute(
            "INSERT INTO project_sources (id, project_id, source_id, observed_path) "
            "VALUES ('ps', 'p', 's', '/example')"
        )
        connection.execute(
            "INSERT INTO sessions (id, source_id, source_session_id, project_id) "
            "VALUES ('se', 's', 'native', 'p')"
        )
        connection.execute(
            "INSERT INTO messages (id, session_id, sequence, content) "
            "VALUES ('m', 'se', 0, 'V1_SYNTHETIC_CONTENT')"
        )
        connection.execute(
            "INSERT INTO tool_calls (id, session_id, message_id, sequence, name) "
            "VALUES ('t', 'se', 'm', 0, 'example')"
        )
        connection.execute(
            "INSERT INTO scan_state (id, source_id, source_session_id, checkpoint) "
            "VALUES ('sc', 's', 'native', 'imported')"
        )


def test_version_one_advances_without_discarding_existing_records(tmp_path):
    path = tmp_path / "v1.sqlite3"
    make_version_one(path)
    assert initialize_database(path) == 2
    assert initialize_database(path) == 2
    with open_database(path) as connection:
        assert (
            connection.execute("SELECT content FROM messages").fetchone()[0]
            == "V1_SYNTHETIC_CONTENT"
        )
        assert (
            connection.execute("SELECT metadata FROM projects").fetchone()[0]
            == '{"custom":"preserved"}'
        )
        assert connection.execute("SELECT count(*) FROM tool_calls").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM discovered_sessions").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_version_one_never_analyze_decision_is_honored_during_upgrade(tmp_path):
    path = tmp_path / "never-v1.sqlite3"
    make_version_one(path, "never_analyze")
    initialize_database(path)
    with open_database(path) as connection:
        assert (
            connection.execute("SELECT privacy_state FROM projects").fetchone()[0]
            == "never_analyze"
        )
        for table in ("sessions", "messages", "tool_calls", "scan_state"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert b"V1_SYNTHETIC_CONTENT" not in path.read_bytes()


def test_migration_failure_rolls_back_schema_version_and_preserves_v1_data(tmp_path, monkeypatch):
    path = tmp_path / "migration-failure.sqlite3"
    make_version_one(path)
    original = database._migrate_v2

    def failure(connection):
        original(connection)
        connection.execute("invalid migration statement")

    monkeypatch.setattr(database, "_migrate_v2", failure)
    with pytest.raises(DatabaseError):
        initialize_database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            connection.execute("SELECT content FROM messages").fetchone()[0]
            == "V1_SYNTHETIC_CONTENT"
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'discovered_sessions'"
            ).fetchall()
            == []
        )
