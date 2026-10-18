"""SQLite repository for metadata discovery, durable scope, and guarded session import."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from aftertrace.adapters.base import SourceCandidate
from aftertrace.core.errors import AfterTraceError
from aftertrace.core.models import (
    Harness,
    Project,
    ProjectPrivacyState,
    ProjectSource,
    Session,
    SessionReference,
    Source,
)
from aftertrace.platform.projects import ProjectIdentity, contains_location, normalize_location
from aftertrace.services.database import open_database, purge_project_content
from aftertrace.services.state import ProjectView, SourceView, WorkspaceSnapshot


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _date(value):
    return datetime.fromisoformat(value) if value else None


def _source(row) -> Source:
    data = dict(row)
    data.update(
        harness=Harness(data["harness"]),
        metadata=json.loads(data["metadata"]),
        last_scan_at=_date(data["last_scan_at"]),
    )
    return Source(**data)


def _project(row) -> Project:
    data = dict(row)
    data.update(
        privacy_state=ProjectPrivacyState(data["privacy_state"]),
        metadata=json.loads(data["metadata"]),
        created_at=_date(data["created_at"]),
        updated_at=_date(data["updated_at"]),
    )
    return Project(**data)


def _save(connection, table: str, data: dict) -> None:
    values = {
        key: json.dumps(value, ensure_ascii=False, allow_nan=False)
        if isinstance(value, (list, dict))
        or (table == "tool_calls" and key == "arguments" and value is not None)
        else value
        for key, value in data.items()
    }
    names = ", ".join(f'"{key}"' for key in values)
    updates = ", ".join(f'"{key}" = excluded."{key}"' for key in values if key != "id")
    connection.execute(
        f"INSERT INTO {table} ({names}) VALUES ({', '.join('?' for _ in values)}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}",
        tuple(values.values()),
    )


class WorkspaceRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def source_for(self, candidate: SourceCandidate) -> Source:
        location = str(Path(candidate.location).absolute())
        with open_database(self.path) as connection:
            row = connection.execute(
                "SELECT * FROM sources WHERE harness = ? AND location = ?",
                (candidate.harness.value, location),
            ).fetchone()
            return (
                _source(row)
                if row
                else Source(
                    id=str(uuid5(NAMESPACE_URL, f"source:{candidate.harness.value}:{location}")),
                    harness=candidate.harness,
                    display_name=candidate.display_name,
                    location=location,
                    discovered_automatically=True,
                )
            )

    def mark_missing_sources(self, present: set[str]) -> None:
        with open_database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for row in connection.execute(
                "SELECT * FROM sources WHERE harness = 'codex'"
            ).fetchall():
                if row["id"] in present:
                    continue
                metadata = {"available": False, "discovered": 0, "warning": "Source not detected."}
                connection.execute(
                    "UPDATE sources SET metadata = ? WHERE id = ?",
                    (json.dumps(metadata), row["id"]),
                )
                connection.execute(
                    "UPDATE discovered_sessions SET available = 0 WHERE source_id = ?", (row["id"],)
                )
                connection.execute(
                    "UPDATE project_sources SET metadata = '{}' WHERE source_id = ?", (row["id"],)
                )

    def discovery_failed(self, source: Source) -> None:
        source.metadata = {"available": False, "warning": "Discovery failed; retry discovery."}
        with open_database(self.path) as connection:
            _save(connection, "sources", source.to_dict())

    def _recognize(self, connection, hint: str | None, identity: ProjectIdentity | None):
        if hint:
            # A known Never Analyze location wins even if Git is unavailable or its remote changed.
            for row in connection.execute(
                "SELECT p.*, ps.observed_path AS blocked_path FROM projects p "
                "JOIN project_sources ps ON ps.project_id = p.id "
                "WHERE p.privacy_state = 'never_analyze'"
            ):
                if row["blocked_path"] and contains_location(row["blocked_path"], hint):
                    return _project(
                        {key: row[key] for key in row.keys() if key != "blocked_path"}
                    ), row["blocked_path"]
        if identity is None:
            return None, None
        rows = connection.execute(
            "SELECT * FROM projects WHERE canonical_identity = ?", (identity.canonical_identity,)
        ).fetchall()
        if len(rows) > 1:
            return None, None  # Ambiguous persisted identity: no automatic merge or permission.
        if rows:
            return _project(rows[0]), identity.root
        project = Project(
            display_name=identity.display_name,
            canonical_identity=identity.canonical_identity,
            primary_location=identity.root,
            git_remote=identity.git_remote,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            metadata={"identity_kind": identity.kind},
        )
        _save(connection, "projects", project.to_dict())
        return project, identity.root

    def record_discovery(
        self, source: Source, entries: list[tuple[SessionReference, ProjectIdentity | None]]
    ) -> None:
        now = _now()
        with open_database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            source.last_scan_at = _date(now)
            source.metadata = {"available": True, "discovered": len(entries)}
            _save(connection, "sources", source.to_dict())
            connection.execute(
                "UPDATE discovered_sessions SET available = 0 WHERE source_id = ?", (source.id,)
            )
            connection.execute(
                "UPDATE project_sources SET metadata = '{}' WHERE source_id = ?", (source.id,)
            )
            for reference, identity in entries:
                project, location = self._recognize(connection, reference.project_hint, identity)
                if project:
                    relation_id = str(
                        uuid5(
                            NAMESPACE_URL,
                            f"{project.id}:{source.id}:{normalize_location(location)}",
                        )
                    )
                    old = connection.execute(
                        "SELECT * FROM project_sources WHERE id = ?", (relation_id,)
                    ).fetchone()
                    count = json.loads(old["metadata"]).get("discovered", 0) if old else 0
                    relation = ProjectSource(
                        id=relation_id,
                        project_id=project.id,
                        source_id=source.id,
                        source_project_identifier=project.canonical_identity,
                        observed_path=location,
                        first_seen_at=_date(old["first_seen_at"]) if old else _date(now),
                        last_seen_at=_date(now),
                        metadata={"discovered": count + 1},
                    )
                    _save(connection, "project_sources", relation.to_dict())
                if project and project.privacy_state is ProjectPrivacyState.NEVER_ANALYZE:
                    # Keep a count and project/source identity, without per-session metadata.
                    connection.execute(
                        "DELETE FROM discovered_sessions WHERE source_id = ? "
                        "AND source_locator = ?",
                        (source.id, reference.source_locator),
                    )
                    continue
                old = connection.execute(
                    "SELECT session_id, project_id FROM discovered_sessions "
                    "WHERE source_id = ? AND source_locator = ?",
                    (source.id, reference.source_locator),
                ).fetchone()
                project_id = project.id if project else None
                data = dict(
                    id=str(
                        uuid5(NAMESPACE_URL, f"reference:{source.id}:{reference.source_locator}")
                    ),
                    source_id=source.id,
                    source_session_id=reference.source_session_id,
                    source_locator=reference.source_locator,
                    project_id=project_id,
                    project_hint=reference.project_hint,
                    started_at_hint=reference.started_at_hint.isoformat()
                    if reference.started_at_hint
                    else None,
                    fingerprint_hint=reference.fingerprint_hint,
                    session_id=old["session_id"]
                    if old and old["project_id"] == project_id
                    else None,
                    discovered_at=now,
                    available=1,
                )
                _save(connection, "discovered_sessions", data)

    def set_privacy(self, project_id: str, state: ProjectPrivacyState) -> None:
        state = ProjectPrivacyState(state)
        with open_database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute(
                "SELECT 1 FROM projects WHERE id = ?", (project_id,)
            ).fetchone():
                raise AfterTraceError("Project is no longer available.", operation="set_privacy")
            if state is ProjectPrivacyState.NEVER_ANALYZE:
                purge_project_content(connection, project_id)
            connection.execute(
                "UPDATE projects SET privacy_state = ?, updated_at = ? WHERE id = ?",
                (state.value, _now(), project_id),
            )

    def reference_ids(self) -> list[str]:
        with open_database(self.path) as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM discovered_sessions WHERE available = 1 "
                    "ORDER BY source_id, source_locator"
                )
            ]

    def import_failure_label(self, reference_id: str) -> str:
        with open_database(self.path) as connection:
            row = connection.execute(
                "SELECT s.display_name, p.display_name FROM discovered_sessions d "
                "JOIN sources s ON s.id = d.source_id "
                "LEFT JOIN projects p ON p.id = d.project_id WHERE d.id = ?",
                (reference_id,),
            ).fetchone()
            return f"{row[0]} / {row[1] or 'Unclassified'}" if row else reference_id

    def import_reference(
        self,
        reference_id: str,
        build: Callable[[SessionReference, Project, str | None], Session | None],
    ) -> str:
        with open_database(self.path) as connection:
            # Permission and content ingestion share one session transaction. A concurrent scope
            # change commits before this gate or afterward, when Never Analyze purges the copy.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT d.*, s.enabled, s.harness, s.metadata AS source_metadata "
                "FROM discovered_sessions d JOIN sources s ON d.source_id = s.id "
                "WHERE d.id = ? AND d.available = 1",
                (reference_id,),
            ).fetchone()
            if row is None or not row["project_id"] or not row["enabled"]:
                return "blocked"
            project_row = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (row["project_id"],)
            ).fetchone()
            project = _project(project_row)
            if project.privacy_state is not ProjectPrivacyState.INCLUDED:
                return "blocked"
            if not json.loads(row["source_metadata"]).get("available", False):
                return "blocked"
            existing = connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (row["session_id"],)
            ).fetchall()
            if row["source_session_id"] is not None:
                existing = connection.execute(
                    "SELECT se.* FROM sessions se JOIN sources s ON se.source_id = s.id "
                    "WHERE s.harness = ? AND se.source_session_id = ?",
                    (row["harness"], row["source_session_id"]),
                ).fetchall()
            if len(existing) > 1 or (existing and existing[0]["project_id"] != project.id):
                raise AfterTraceError(
                    "Session has an ambiguous project association.", operation="import_session"
                )
            previous = existing[0] if existing else None
            reference = SessionReference(
                source_id=row["source_id"],
                source_session_id=row["source_session_id"],
                source_locator=row["source_locator"],
                project_hint=row["project_hint"],
                started_at_hint=_date(row["started_at_hint"]),
                fingerprint_hint=row["fingerprint_hint"],
            )
            session = build(
                reference, project, previous["source_fingerprint"] if previous else None
            )
            if session is None:
                if previous is None:
                    raise AfterTraceError(
                        "No existing session to reuse.", operation="import_session"
                    )
                session_id = previous["id"]
                outcome = "unchanged"
            else:
                if (
                    session.source_id != reference.source_id
                    or session.source_session_id != reference.source_session_id
                ):
                    raise AfterTraceError(
                        "Source identity changed; discover again.", operation="import_session"
                    )
                session_id = previous["id"] if previous else session.id
                session.id, session.project_id = session_id, project.id
                session.imported_at = (
                    _date(previous["imported_at"]) if previous else datetime.now(UTC)
                )
                session.updated_at = datetime.now(UTC)
                for table in ("tool_calls", "messages"):
                    connection.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
                data = session.to_dict()
                del data["messages"], data["tool_calls"]
                _save(connection, "sessions", data)
                for table, children in (
                    ("messages", session.messages),
                    ("tool_calls", session.tool_calls),
                ):
                    for child in children:
                        child.session_id = session_id
                        _save(connection, table, child.to_dict())
                outcome = "imported"
            connection.execute(
                "UPDATE discovered_sessions SET session_id = ? WHERE id = ?",
                (session_id, reference_id),
            )
            _save(
                connection,
                "scan_state",
                dict(
                    id=str(uuid5(NAMESPACE_URL, f"import:{reference_id}")),
                    source_id=reference.source_id,
                    source_session_id=reference.source_session_id,
                    source_locator=reference.source_locator,
                    fingerprint=reference.fingerprint_hint,
                    checkpoint="imported",
                    last_scan_at=_now(),
                    last_success_at=_now(),
                    metadata={},
                ),
            )
            return outcome

    def snapshot(self) -> WorkspaceSnapshot:
        with open_database(self.path) as connection:
            projects, sources = [], []
            never_count = 0
            for row in connection.execute(
                "SELECT * FROM projects ORDER BY display_name, id"
            ).fetchall():
                relations = connection.execute(
                    "SELECT ps.*, s.harness FROM project_sources ps JOIN sources s "
                    "ON ps.source_id = s.id WHERE ps.project_id = ?",
                    (row["id"],),
                ).fetchall()
                count = sum(json.loads(item["metadata"]).get("discovered", 0) for item in relations)
                if row["privacy_state"] == ProjectPrivacyState.NEVER_ANALYZE:
                    never_count += count
                projects.append(
                    ProjectView(
                        _project(row),
                        tuple(
                            sorted(
                                {
                                    item["observed_path"]
                                    for item in relations
                                    if item["observed_path"]
                                }
                            )
                        ),
                        tuple(sorted({Harness(item["harness"]) for item in relations})),
                        count,
                        connection.execute(
                            "SELECT count(*) FROM sessions WHERE project_id = ?", (row["id"],)
                        ).fetchone()[0],
                    )
                )
            for row in connection.execute(
                "SELECT * FROM sources ORDER BY display_name, location"
            ).fetchall():
                source = _source(row)
                sources.append(
                    SourceView(
                        source,
                        source.metadata.get("available", False),
                        source.metadata.get("discovered", 0),
                        connection.execute(
                            "SELECT count(DISTINCT session_id) FROM discovered_sessions "
                            "WHERE source_id = ? AND available = 1",
                            (source.id,),
                        ).fetchone()[0],
                        source.metadata.get("warning"),
                    )
                )
            unclassified = connection.execute(
                "SELECT count(*) FROM discovered_sessions "
                "WHERE project_id IS NULL AND available = 1"
            ).fetchone()[0]
            return WorkspaceSnapshot(tuple(projects), tuple(sources), unclassified, never_count)
