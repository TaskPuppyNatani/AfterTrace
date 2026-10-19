"""Initial SQLite schema boundary for AfterTrace-owned data only."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from aftertrace.core.errors import DatabaseError
from aftertrace.core.models import (
    ContentFormat,
    DraftState,
    FindingConfidence,
    FindingState,
    FindingType,
    Harness,
    InstructionScope,
    MessageRole,
    ProjectPrivacyState,
    ProviderLocality,
    ToolStatus,
)
from aftertrace.platform.paths import database_path
from aftertrace.services.finding_store import lineage_findings, purge_affected_findings

SCHEMA_VERSION = 2


def _enum_column(name: str, values: type, default: str | None = None) -> str:
    options = ", ".join(f"'{item.value}'" for item in values)
    suffix = f" DEFAULT '{default}'" if default is not None else ""
    return f"{name} TEXT NOT NULL{suffix} CHECK ({name} IN ({options}))"


# Arrays, mappings, arguments and provenance use JSON text; dates use ISO 8601 text.
# No scanner, repository CRUD, or instruction application is introduced here.
_TABLES = {
    "sources": f"""
        {_enum_column("harness", Harness)},
        display_name TEXT NOT NULL, location TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
        discovered_automatically INTEGER NOT NULL DEFAULT 0
            CHECK (discovered_automatically IN (0, 1)), last_scan_at TEXT""",
    "projects": f"""
        display_name TEXT NOT NULL, canonical_identity TEXT, primary_location TEXT, git_remote TEXT,
        {_enum_column("privacy_state", ProjectPrivacyState, "excluded")},
        created_at TEXT, updated_at TEXT""",
    "project_sources": """
        project_id TEXT NOT NULL REFERENCES projects(id),
        source_id TEXT NOT NULL REFERENCES sources(id), source_project_identifier TEXT,
        observed_path TEXT, first_seen_at TEXT, last_seen_at TEXT""",
    "sessions": """
        source_id TEXT NOT NULL REFERENCES sources(id), source_session_id TEXT,
        project_id TEXT REFERENCES projects(id), started_at TEXT, ended_at TEXT, model TEXT,
        title TEXT, working_directory TEXT, source_fingerprint TEXT,
        source_available INTEGER NOT NULL DEFAULT 1 CHECK (source_available IN (0, 1)),
        imported_at TEXT, updated_at TEXT, UNIQUE (source_id, source_session_id)""",
    "messages": f"""
        session_id TEXT NOT NULL REFERENCES sessions(id), sequence INTEGER NOT NULL,
        {_enum_column("role", MessageRole, "unknown")}, timestamp TEXT, content TEXT NOT NULL,
        {_enum_column("content_format", ContentFormat, "unknown")}, author_source TEXT,
        UNIQUE (session_id, sequence)""",
    "tool_calls": f"""
        session_id TEXT NOT NULL REFERENCES sessions(id), message_id TEXT REFERENCES messages(id),
        sequence INTEGER NOT NULL, name TEXT NOT NULL, arguments TEXT, result_summary TEXT,
        started_at TEXT, ended_at TEXT, {_enum_column("status", ToolStatus, "unknown")},
        UNIQUE (session_id, sequence)""",
    "instruction_sources": f"""
        project_id TEXT REFERENCES projects(id), {_enum_column("harness", Harness)},
        {_enum_column("scope", InstructionScope, "unknown")}, path TEXT NOT NULL,
        "exists" INTEGER NOT NULL DEFAULT 0 CHECK ("exists" IN (0, 1)),
        content_fingerprint TEXT, discovered_at TEXT, updated_at TEXT""",
    "rules": f"""
        text TEXT NOT NULL, category TEXT, {_enum_column("scope", InstructionScope, "unknown")},
        project_id TEXT REFERENCES projects(id), source_ids TEXT NOT NULL DEFAULT '[]',
        target_harnesses TEXT NOT NULL DEFAULT '[]', state TEXT,
        created_at TEXT, updated_at TEXT""",
    "findings": f"""
        {_enum_column("finding_type", FindingType)}, canonical_pattern TEXT NOT NULL,
        title TEXT NOT NULL, summary TEXT NOT NULL, {_enum_column("confidence", FindingConfidence)},
        {_enum_column("state", FindingState, "new")}, first_seen_at TEXT, last_seen_at TEXT,
        occurrence_count INTEGER NOT NULL DEFAULT 0 CHECK (occurrence_count >= 0),
        distinct_session_count INTEGER NOT NULL DEFAULT 0 CHECK (distinct_session_count >= 0),
        distinct_project_count INTEGER NOT NULL DEFAULT 0 CHECK (distinct_project_count >= 0),
        affected_harnesses TEXT NOT NULL DEFAULT '[]', recommendation TEXT,
        analysis_kind TEXT NOT NULL, analysis_version TEXT NOT NULL,
        generated_at TEXT, last_evaluated_at TEXT, provenance TEXT NOT NULL DEFAULT '{{}}' """,
    "finding_evidence": """
        finding_id TEXT NOT NULL REFERENCES findings(id),
        session_id TEXT NOT NULL REFERENCES sessions(id), message_id TEXT REFERENCES messages(id),
        evidence_type TEXT NOT NULL, original_text TEXT NOT NULL,
        start_offset INTEGER CHECK (start_offset >= 0), end_offset INTEGER,
        observed_at TEXT, explanation TEXT,
        CHECK (end_offset IS NULL OR (start_offset IS NOT NULL AND end_offset >= start_offset))""",
    "rule_drafts": f"""
        finding_id TEXT REFERENCES findings(id), text TEXT NOT NULL, category TEXT,
        {_enum_column("scope", InstructionScope, "unknown")},
        project_id TEXT REFERENCES projects(id),
        target_harnesses TEXT NOT NULL DEFAULT '[]',
        target_instruction_sources TEXT NOT NULL DEFAULT '[]',
        {_enum_column("state", DraftState, "draft")}, created_at TEXT, updated_at TEXT""",
    "applied_rules": """
        rule_draft_id TEXT NOT NULL REFERENCES rule_drafts(id),
        rule_id TEXT NOT NULL REFERENCES rules(id), applied_at TEXT NOT NULL,
        targets TEXT NOT NULL DEFAULT '[]', before_fingerprints TEXT NOT NULL DEFAULT '[]',
        after_fingerprints TEXT NOT NULL DEFAULT '[]' """,
    "provider_configs": f"""
        display_name TEXT NOT NULL, endpoint TEXT NOT NULL,
        {_enum_column("locality", ProviderLocality, "local")},
        protocol TEXT NOT NULL DEFAULT 'openai_compatible', model TEXT,
        enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1))""",
    "scan_state": """
        source_id TEXT NOT NULL REFERENCES sources(id), source_session_id TEXT, source_locator TEXT,
        fingerprint TEXT, checkpoint TEXT, last_scan_at TEXT, last_success_at TEXT,
        UNIQUE (source_id, source_locator)""",
}


def schema_version(connection: sqlite3.Connection) -> int:
    return connection.execute("PRAGMA user_version").fetchone()[0]


def purge_project_content(connection: sqlite3.Connection, project_id: str) -> None:
    """Remove AfterTrace-owned content before committing a Never Analyze decision."""
    # Receipts and historical lineage both identify derived content requiring removal.
    connection.execute("CREATE TEMP TABLE IF NOT EXISTS purged_findings (id TEXT PRIMARY KEY)")
    connection.execute("DELETE FROM purged_findings")
    connection.execute(
        "INSERT INTO purged_findings SELECT DISTINCT finding_id FROM finding_evidence "
        "WHERE session_id IN (SELECT id FROM sessions WHERE project_id = ?)",
        (project_id,),
    )
    connection.executemany(
        "INSERT OR IGNORE INTO purged_findings VALUES (?)",
        ((identity,) for identity in lineage_findings(connection, project_id)),
    )
    connection.execute(
        "DELETE FROM applied_rules WHERE rule_draft_id IN (SELECT id FROM rule_drafts "
        "WHERE project_id = ? OR finding_id IN (SELECT id FROM purged_findings)) "
        "OR rule_id IN (SELECT id FROM rules WHERE project_id = ?)",
        (project_id, project_id),
    )
    connection.execute(
        "DELETE FROM rule_drafts WHERE project_id = ? "
        "OR finding_id IN (SELECT id FROM purged_findings)",
        (project_id,),
    )
    purge_affected_findings(connection, project_id)
    connection.execute("DELETE FROM rules WHERE project_id = ?", (project_id,))
    connection.execute("DELETE FROM instruction_sources WHERE project_id = ?", (project_id,))
    connection.execute(
        "DELETE FROM scan_state WHERE EXISTS (SELECT 1 FROM discovered_sessions d "
        "WHERE d.project_id = ? AND d.source_id = scan_state.source_id AND "
        "(d.source_locator = scan_state.source_locator OR "
        "d.source_session_id = scan_state.source_session_id))",
        (project_id,),
    )
    connection.execute(
        "DELETE FROM discovered_sessions WHERE project_id = ? OR session_id IN "
        "(SELECT id FROM sessions WHERE project_id = ?)",
        (project_id, project_id),
    )
    connection.execute(
        "DELETE FROM scan_state WHERE EXISTS (SELECT 1 FROM sessions se "
        "WHERE se.project_id = ? AND se.source_id = scan_state.source_id "
        "AND se.source_session_id = scan_state.source_session_id)",
        (project_id,),
    )
    for table in ("tool_calls", "messages"):
        connection.execute(
            f"DELETE FROM {table} WHERE session_id IN "
            "(SELECT id FROM sessions WHERE project_id = ?)",
            (project_id,),
        )
    connection.execute("DELETE FROM sessions WHERE project_id = ?", (project_id,))
    connection.execute("UPDATE projects SET metadata = '{}' WHERE id = ?", (project_id,))
    for row in connection.execute(
        "SELECT id, metadata FROM project_sources WHERE project_id = ?",
        (project_id,),
    ).fetchall():
        count = json.loads(row["metadata"]).get("discovered", 0)
        count = count if isinstance(count, int) else 0
        connection.execute(
            "UPDATE project_sources SET metadata = ? WHERE id = ?",
            (json.dumps({"discovered": count}), row["id"]),
        )


def _migrate_v2(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE discovered_sessions (
        id TEXT PRIMARY KEY NOT NULL, source_id TEXT NOT NULL REFERENCES sources(id),
        source_session_id TEXT, source_locator TEXT NOT NULL,
        project_id TEXT REFERENCES projects(id), project_hint TEXT, started_at_hint TEXT,
        fingerprint_hint TEXT, session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
        discovered_at TEXT NOT NULL, available INTEGER NOT NULL DEFAULT 1,
        UNIQUE (source_id, source_locator))""")
    connection.execute("CREATE INDEX discovered_project ON discovered_sessions(project_id)")
    connection.execute("CREATE INDEX project_identity ON projects(canonical_identity)")
    for row in connection.execute(
        "SELECT id FROM projects WHERE privacy_state = 'never_analyze'"
    ).fetchall():
        purge_project_content(connection, row[0])
    # Defense in depth for direct persistence callers, in addition to repository/service policy.
    for table in ("sessions", "messages", "tool_calls"):
        project = (
            "NEW.project_id"
            if table == "sessions"
            else "(SELECT project_id FROM sessions WHERE id = NEW.session_id)"
        )
        for operation in ("INSERT", "UPDATE"):
            connection.execute(f"""CREATE TRIGGER protect_{table}_{operation.lower()}
                BEFORE {operation} ON {table}
                WHEN (SELECT privacy_state FROM projects WHERE id = {project}) = 'never_analyze'
                BEGIN SELECT RAISE(ABORT, 'Never Analyze content is prohibited'); END""")
    connection.execute("""CREATE TRIGGER protect_never_transition
        BEFORE UPDATE OF privacy_state ON projects
        WHEN NEW.privacy_state = 'never_analyze'
        AND EXISTS (SELECT 1 FROM sessions WHERE project_id = NEW.id)
        BEGIN SELECT RAISE(ABORT, 'Remove imported content before Never Analyze'); END""")


def _initialize(connection: sqlite3.Connection) -> None:
    # Lock before checking the version so simultaneous first opens cannot race.
    connection.execute("BEGIN IMMEDIATE")
    try:
        version = schema_version(connection)
        if version not in (0, 1, SCHEMA_VERSION):
            raise DatabaseError(
                "This database version is not supported by this AfterTrace build.",
                operation="initialize_database",
                detail=f"Schema version: {version}",
            )
        if version == 0:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
            ).fetchone():
                raise DatabaseError(
                    "The selected database is not an initialized AfterTrace database.",
                    operation="initialize_database",
                )
            for table, columns in _TABLES.items():
                connection.execute(
                    f"CREATE TABLE {table} (id TEXT PRIMARY KEY NOT NULL, "
                    f"metadata TEXT NOT NULL DEFAULT '{{}}', {columns})"
                )
            version = 1
        else:
            # Reject incomplete versioned databases rather than claiming successful initialization.
            for table in _TABLES:
                connection.execute(f"SELECT id, metadata FROM {table} LIMIT 0")
        if version == 1:
            _migrate_v2(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.execute("SELECT id FROM discovered_sessions LIMIT 0")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


@contextmanager
def open_database(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open, initialize, and close an AfterTrace database; commit caller writes on success."""
    connection = None
    try:
        target = database_path() if path is None else Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(target, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA secure_delete = ON")
        _initialize(connection)
        yield connection
        connection.commit()
    except (sqlite3.Error, OSError) as error:
        if connection is not None:
            connection.rollback()
        raise DatabaseError(
            "AfterTrace could not open or initialize its local database.",
            operation="open_database",
            detail=str(error),
        ) from error
    finally:
        if connection is not None:
            connection.close()


def initialize_database(path: Path | None = None) -> int:
    with open_database(path) as connection:
        return schema_version(connection)
