"""SQLite repository for metadata discovery, durable scope, and guarded session import."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from aftertrace.adapters.base import SourceCandidate
from aftertrace.core.errors import AfterTraceError
from aftertrace.core.findings import FindingBundle
from aftertrace.core.models import (
    ContentFormat,
    Finding,
    FindingEvidence,
    FindingState,
    Harness,
    ImportedMessage,
    Message,
    MessageRole,
    Project,
    ProjectPrivacyState,
    ProjectSource,
    Session,
    SessionReference,
    Source,
)
from aftertrace.platform.projects import ProjectIdentity, contains_location, normalize_location
from aftertrace.services.database import open_database, purge_project_content
from aftertrace.services.finding_store import evidence_for, read_evidence, read_finding, synchronize
from aftertrace.services.project_reconciliation import (
    reconcile_project_identity,
    relocate_project_locations,
)
from aftertrace.services.state import (
    EvidenceView,
    FindingDetail,
    ProjectIdentityWarning,
    ProjectPrivacyConflict,
    ProjectView,
    SourceView,
    WorkspaceSnapshot,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _date(value):
    return datetime.fromisoformat(value) if value else None


_IDENTITY_CONFLICTS_KEY = "privacy_identity_conflicts"
_IDENTITY_SEPARATIONS_KEY = "identity_separations"
_SEPARATED_PROJECT_PAIRS_KEY = "separated_project_pairs"
_IDENTITY_RESOLUTIONS_KEY = "privacy_identity_resolutions"


def _project_pair_key(project_ids):
    return str(uuid5(NAMESPACE_URL, "separate-project-pair:" + ":".join(sorted(project_ids))))


def _privacy_conflict_id(project_ids, canonical_identities, location):
    normalized = normalize_location(location)
    if normalized is None:
        return None
    by_id = sorted(zip(project_ids, canonical_identities, strict=True))
    payload = json.dumps(
        {
            "project_ids": [item[0] for item in by_id],
            "canonical_identities": [item[1] for item in by_id],
            "location": normalized,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return str(uuid5(NAMESPACE_URL, "project-privacy-conflict:" + payload))


def _separation_values(metadata, key):
    value = metadata.get(key, [])
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return set(value)
    return set()


def _matching_project_rows(connection, identity):
    return connection.execute(
        "SELECT * FROM projects WHERE canonical_identity IN (?, ?)",
        (identity.canonical_identity, identity.local_identity),
    ).fetchall()


def _record_identity_conflicts(connection, identity, rows):
    for left, right in combinations(rows, 2):
        if left["privacy_state"] == right["privacy_state"]:
            continue
        pair = sorted((left, right), key=lambda row: row["id"])
        project_ids = tuple(row["id"] for row in pair)
        canonical_identities = tuple(row["canonical_identity"] for row in pair)
        conflict_id = _privacy_conflict_id(project_ids, canonical_identities, identity.root)
        if conflict_id is None:
            continue
        marker = {
            "project_ids": list(project_ids),
            "canonical_identities": list(canonical_identities),
            "location": identity.root,
            "match_basis": "verified_identity",
        }
        for row in pair:
            current = connection.execute(
                "SELECT metadata FROM projects WHERE id=?", (row["id"],)
            ).fetchone()
            metadata = json.loads(current["metadata"])
            conflicts = metadata.get(_IDENTITY_CONFLICTS_KEY, {})
            if not isinstance(conflicts, dict) or conflicts.get(conflict_id) == marker:
                continue
            conflicts[conflict_id] = marker
            metadata[_IDENTITY_CONFLICTS_KEY] = conflicts
            connection.execute(
                "UPDATE projects SET metadata=?,updated_at=? WHERE id=?",
                (json.dumps(metadata), _now(), row["id"]),
            )


def _identity_is_separated(rows):
    project_ids = {row["id"] for row in rows}
    for row in rows:
        separated = _separation_values(
            json.loads(row["metadata"]), _SEPARATED_PROJECT_PAIRS_KEY
        )
        if any(
            _project_pair_key((row["id"], other_id)) in separated
            for other_id in project_ids
            if other_id != row["id"]
        ):
            return True
    return False


def _project_privacy_conflicts(connection, projects):
    by_id = {view.project.id: view for view in projects}
    conflicts = {}

    def add(project_ids, location, match_basis):
        if match_basis not in {"verified_identity", "canonical_identity"}:
            return
        if len(project_ids) != 2 or any(project_id not in by_id for project_id in project_ids):
            return
        views = tuple(
            sorted((by_id[item] for item in project_ids), key=lambda item: item.project.id)
        )
        if views[0].project.privacy_state is views[1].project.privacy_state:
            return
        ids = tuple(view.project.id for view in views)
        canonical = tuple(view.project.canonical_identity for view in views)
        conflict_id = _privacy_conflict_id(ids, canonical, location)
        if conflict_id is None:
            return
        if any(
            conflict_id
            in _separation_values(view.project.metadata, _IDENTITY_SEPARATIONS_KEY)
            for view in views
        ):
            return
        conflict = ProjectPrivacyConflict(
            conflict_id=conflict_id,
            project_ids=ids,
            project_names=tuple(view.project.display_name for view in views),
            privacy_states=tuple(view.project.privacy_state for view in views),
            canonical_identities=canonical,
            location=location,
            match_basis=match_basis,
            imported_sessions=tuple(view.imported for view in views),
            discovered_sessions=tuple(view.discovered for view in views),
        )
        conflicts[conflict_id] = conflict

    for view in projects:
        markers = view.project.metadata.get(_IDENTITY_CONFLICTS_KEY, {})
        if not isinstance(markers, dict):
            continue
        for marker in markers.values():
            if isinstance(marker, dict):
                add(
                    tuple(marker.get("project_ids", ())),
                    marker.get("location"),
                    marker.get("match_basis", "verified_identity"),
                )

    for left, right in combinations(projects, 2):
        if left.project.privacy_state is right.project.privacy_state:
            continue
        if (
            left.project.canonical_identity
            and left.project.canonical_identity == right.project.canonical_identity
        ):
            current = max(
                (left, right),
                key=lambda view: (
                    view.discovered,
                    view.project.updated_at.isoformat() if view.project.updated_at else "",
                ),
            )
            location = current.project.primary_location or (
                current.locations[0] if current.locations else None
            )
            add((left.project.id, right.project.id), location, "canonical_identity")

    return tuple(
        sorted(
            conflicts.values(),
            key=lambda item: (item.project_names, item.location, item.project_ids),
        )
    )


def _project_identity_warnings(projects, privacy_conflicts):
    proven_pairs = {frozenset(conflict.project_ids) for conflict in privacy_conflicts}
    warnings = {}
    for left, right in combinations(projects, 2):
        if left.project.privacy_state is right.project.privacy_state:
            continue
        left_identity = left.project.canonical_identity
        right_identity = right.project.canonical_identity
        if not left_identity or not right_identity or left_identity == right_identity:
            continue
        ids = (left.project.id, right.project.id)
        pair = frozenset(ids)
        if pair in proven_pairs:
            continue
        pair_key = _project_pair_key(ids)
        if any(
            pair_key
            in _separation_values(view.project.metadata, _SEPARATED_PROJECT_PAIRS_KEY)
            for view in (left, right)
        ):
            continue
        left_locations = {
            normalize_location(path): path for path in left.locations if normalize_location(path)
        }
        right_locations = {
            normalize_location(path): path for path in right.locations if normalize_location(path)
        }
        common = sorted(left_locations.keys() & right_locations.keys())
        if not common:
            continue
        location = left_locations[common[0]]
        warning_id = _privacy_conflict_id(ids, (left_identity, right_identity), location)
        if warning_id is None:
            continue
        ordered = tuple(sorted((left, right), key=lambda view: view.project.id))
        warnings[warning_id] = ProjectIdentityWarning(
            warning_id=warning_id,
            project_ids=tuple(view.project.id for view in ordered),
            project_names=tuple(view.project.display_name for view in ordered),
            privacy_states=tuple(view.project.privacy_state for view in ordered),
            location=location,
            imported_sessions=tuple(view.imported for view in ordered),
            discovered_sessions=tuple(view.discovered for view in ordered),
        )
    return tuple(
        sorted(
            warnings.values(),
            key=lambda item: (item.project_names, item.location, item.project_ids),
        )
    )


def _project_has_unresolved_privacy_conflict(connection, project_id):
    project = connection.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None:
        return False
    metadata = json.loads(project["metadata"])
    separations = _separation_values(metadata, _IDENTITY_SEPARATIONS_KEY)
    others = connection.execute(
        "SELECT * FROM projects WHERE id != ? AND privacy_state != ?",
        (project_id, project["privacy_state"]),
    ).fetchall()
    for other in others:
        other_metadata = json.loads(other["metadata"])
        location = None
        for owner in (metadata, other_metadata):
            markers = owner.get(_IDENTITY_CONFLICTS_KEY, {})
            if not isinstance(markers, dict):
                continue
            for marker in markers.values():
                if (
                    isinstance(marker, dict)
                    and marker.get("match_basis", "verified_identity") == "verified_identity"
                    and set(marker.get("project_ids", ())) == {project["id"], other["id"]}
                    and marker.get("location")
                ):
                    location = marker["location"]
                    break
            if location is not None:
                break
        if (
            location is None
            and project["canonical_identity"]
            and project["canonical_identity"] == other["canonical_identity"]
        ):
            candidates = (project, other)

            def project_order(candidate):
                discovered = sum(
                    json.loads(relation["metadata"]).get("discovered", 0)
                    for relation in connection.execute(
                        "SELECT metadata FROM project_sources WHERE project_id=?",
                        (candidate["id"],),
                    )
                )
                return discovered, candidate["updated_at"] or ""

            current = max(candidates, key=project_order)
            location = current["primary_location"] or connection.execute(
                "SELECT min(observed_path) FROM project_sources WHERE project_id=?",
                (current["id"],),
            ).fetchone()[0]
        if location is None:
            continue
        conflict_id = _privacy_conflict_id(
            (project["id"], other["id"]),
            (project["canonical_identity"], other["canonical_identity"]),
            location,
        )
        if conflict_id and conflict_id not in separations and conflict_id not in _separation_values(
            other_metadata, _IDENTITY_SEPARATIONS_KEY
        ):
            return True
    return False


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

    def imported_messages_for_analysis(self) -> list[ImportedMessage]:
        """Read only explicit imports whose projects are currently Included.

        One query captures messages and durable scope in the same SQLite snapshot.
        Discovery references, raw source locations, and tool records are not read.
        Excluded projects may retain old imported copies, but cannot support analysis.
        """
        with open_database(self.path) as connection:
            return self._imported_messages(connection)

    @staticmethod
    def _imported_messages(connection) -> list[ImportedMessage]:
        rows = connection.execute(
            "SELECT m.*, se.project_id, se.source_id, s.harness "
            "FROM messages m JOIN sessions se ON se.id = m.session_id "
            "JOIN projects p ON p.id = se.project_id "
            "JOIN sources s ON s.id = se.source_id "
            "WHERE p.privacy_state = 'included' AND se.imported_at IS NOT NULL "
            "ORDER BY se.id, m.sequence, m.id"
        ).fetchall()
        result = []
        try:
            for row in rows:
                data = dict(row)
                context = {key: data.pop(key) for key in ("project_id", "source_id", "harness")}
                data.update(
                    role=MessageRole(data["role"]),
                    content_format=ContentFormat(data["content_format"]),
                    timestamp=_date(data["timestamp"]),
                    metadata=json.loads(data["metadata"]),
                )
                result.append(
                    ImportedMessage(
                        message=Message(**data),
                        project_id=context["project_id"],
                        source_id=context["source_id"],
                        harness=Harness(context["harness"]),
                    )
                )
        except (TypeError, ValueError):
            raise AfterTraceError(
                "Imported message data is invalid.", operation="read_imported_messages"
            ) from None
        return result

    def publish_repeated_findings(
        self, build: Callable[[list[ImportedMessage]], list[FindingBundle]]
    ) -> list[Finding]:
        with open_database(self.path) as connection:
            # Scope changes/imports cannot commit between snapshot validation and publication.
            connection.execute("BEGIN IMMEDIATE")
            return synchronize(connection, build(self._imported_messages(connection)))

    def findings(self) -> list[Finding]:
        with open_database(self.path) as connection:
            return [read_finding(row) for row in connection.execute(
                "SELECT * FROM findings ORDER BY distinct_session_count DESC, "
                "occurrence_count DESC, canonical_pattern, id"
            )]

    def finding_evidence(self, finding_id: str) -> tuple[FindingEvidence, ...]:
        with open_database(self.path) as connection:
            return evidence_for(connection, finding_id)

    def finding_overview(self) -> tuple[list[Finding], int]:
        """Read persisted summaries and an eligible-history count, without loading receipts."""
        with open_database(self.path) as connection:
            connection.execute("BEGIN")
            findings = [read_finding(row) for row in connection.execute(
                "SELECT * FROM findings ORDER BY distinct_session_count DESC, "
                "occurrence_count DESC, canonical_pattern, id"
            )]
            count = connection.execute(
                "SELECT count(*) FROM sessions se JOIN projects p ON p.id = se.project_id "
                "WHERE p.privacy_state = 'included' AND se.imported_at IS NOT NULL"
            ).fetchone()[0]
            return findings, count

    def finding_detail(self, finding_id: str) -> FindingDetail | None:
        """Read one Finding and its persisted instruction receipts in one snapshot."""
        with open_database(self.path) as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM findings WHERE id = ?", (finding_id,)
            ).fetchone()
            if row is None:
                return None
            finding = read_finding(row)
            rows = connection.execute(
                "SELECT e.*, p.display_name AS project_name, s.display_name AS source_name, "
                "s.harness AS harness, se.title AS session_title "
                "FROM finding_evidence e JOIN sessions se ON se.id = e.session_id "
                "JOIN messages m ON m.id = e.message_id AND m.session_id = se.id "
                "JOIN projects p ON p.id = se.project_id JOIN sources s ON s.id = se.source_id "
                "WHERE e.finding_id = ? ORDER BY e.session_id, e.message_id, "
                "e.start_offset, e.end_offset, e.id", (finding_id,),
            ).fetchall()
            evidence = []
            for row in rows:
                data = dict(row)
                context = {key: data.pop(key) for key in (
                    "project_name", "source_name", "harness", "session_title"
                )}
                evidence.append(EvidenceView(
                    evidence=read_evidence(data), project_name=context["project_name"],
                    source_name=context["source_name"], harness=Harness(context["harness"]),
                    session_title=context["session_title"],
                ))
            return FindingDetail(finding, tuple(evidence))

    def update_finding_state(self, finding_id: str, state: FindingState) -> None:
        state = FindingState(state)
        with open_database(self.path) as connection:
            if connection.execute(
                "UPDATE findings SET state = ? WHERE id = ?", (state.value, finding_id)
            ).rowcount != 1:
                raise AfterTraceError(
                    "Finding is no longer available.", operation="update_finding_state"
                )

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
                    if identity is not None:
                        matches = _matching_project_rows(connection, identity)
                        if (
                            len({item["privacy_state"] for item in matches}) > 1
                            and not _identity_is_separated(matches)
                        ):
                            _record_identity_conflicts(connection, identity, matches)
                    return _project(
                        {key: row[key] for key in row.keys() if key != "blocked_path"}
                    ), row["blocked_path"]
        if identity is None:
            return None, None
        rows = _matching_project_rows(connection, identity)
        if rows:
            if _identity_is_separated(rows):
                return None, None
            if len({row["privacy_state"] for row in rows}) > 1:
                _record_identity_conflicts(connection, identity, rows)
            row = reconcile_project_identity(connection, identity, rows)
            # Conflicting policy/metadata remains ambiguous and never grants permission.
            return (_project(row), identity.root) if row else (None, None)
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
                    if old and (old["project_id"] == project_id or project_id is None)
                    else None,
                    discovered_at=now,
                    available=1,
                )
                _save(connection, "discovered_sessions", data)

    def reconcile_project_locations(self, project_id, identity, old_locations) -> None:
        with open_database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            project = connection.execute(
                "SELECT * FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if (
                project is None
                or project["privacy_state"] == ProjectPrivacyState.NEVER_ANALYZE
                or project["canonical_identity"] not in (
                    identity.canonical_identity, identity.local_identity
                )
            ):
                return
            relocate_project_locations(connection, project, identity, old_locations)

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

    def resolve_project_privacy_conflict(
        self,
        conflict: ProjectPrivacyConflict,
        keep_state: ProjectPrivacyState | None,
        identity: ProjectIdentity | None = None,
    ) -> str:
        """Resolve a displayed conflict atomically; None records an explicit Keep Separate."""
        if not isinstance(conflict, ProjectPrivacyConflict) or conflict.match_basis not in {
            "verified_identity",
            "canonical_identity",
        }:
            raise AfterTraceError(
                "A location-only identity warning cannot be resolved as a project merge.",
                operation="resolve_project_conflict",
            )
        with open_database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM projects WHERE id IN (?, ?) ORDER BY id",
                conflict.project_ids,
            ).fetchall()
            selected_value = None if keep_state is None else ProjectPrivacyState(keep_state).value
            if len(rows) == 1:
                resolutions = json.loads(rows[0]["metadata"]).get(
                    _IDENTITY_RESOLUTIONS_KEY, {}
                )
                if isinstance(resolutions, dict) and conflict.conflict_id in resolutions:
                    if resolutions[conflict.conflict_id] == selected_value:
                        return "Project conflict was already resolved."
                    raise AfterTraceError(
                        "Project conflict was already merged with a different privacy choice.",
                        operation="resolve_project_conflict",
                    )
                raise AfterTraceError(
                    "Project conflict is no longer available; reload Projects.",
                    operation="resolve_project_conflict",
                )
            if len(rows) != 2:
                raise AfterTraceError(
                    "Project conflict is no longer available; reload Projects.",
                    operation="resolve_project_conflict",
                )
            ids = tuple(row["id"] for row in rows)
            canonical = tuple(row["canonical_identity"] for row in rows)
            states = tuple(ProjectPrivacyState(row["privacy_state"]) for row in rows)
            current_id = _privacy_conflict_id(ids, canonical, conflict.location)
            if (
                ids != conflict.project_ids
                or canonical != conflict.canonical_identities
                or states != conflict.privacy_states
                or current_id != conflict.conflict_id
            ):
                raise AfterTraceError(
                    "Project conflict changed; reload Projects before resolving it.",
                    operation="resolve_project_conflict",
                )
            if states[0] is states[1]:
                raise AfterTraceError(
                    "Project privacy no longer conflicts; reload Projects.",
                    operation="resolve_project_conflict",
                )

            marker_found = False
            for row in rows:
                metadata = json.loads(row["metadata"])
                markers = metadata.get(_IDENTITY_CONFLICTS_KEY, {})
                marker_found = marker_found or (
                    isinstance(markers, dict) and conflict.conflict_id in markers
                )
            pair_key = _project_pair_key(ids)
            if keep_state is None and any(
                conflict.conflict_id
                in _separation_values(json.loads(row["metadata"]), _IDENTITY_SEPARATIONS_KEY)
                and pair_key
                in _separation_values(json.loads(row["metadata"]), _SEPARATED_PROJECT_PAIRS_KEY)
                for row in rows
            ):
                return "Projects were already kept separate."
            same_canonical = bool(canonical[0]) and canonical[0] == canonical[1]
            identity_proven = (
                marker_found
                if conflict.match_basis == "verified_identity"
                else same_canonical
            )
            if not identity_proven:
                raise AfterTraceError(
                    "Current Git identity evidence does not prove these are the same project.",
                    operation="resolve_project_conflict",
                )

            if keep_state is None:
                for row in rows:
                    metadata = json.loads(row["metadata"])
                    metadata[_IDENTITY_SEPARATIONS_KEY] = sorted(
                        _separation_values(metadata, _IDENTITY_SEPARATIONS_KEY)
                        | {conflict.conflict_id}
                    )
                    metadata[_SEPARATED_PROJECT_PAIRS_KEY] = sorted(
                        _separation_values(metadata, _SEPARATED_PROJECT_PAIRS_KEY) | {pair_key}
                    )
                    markers = metadata.get(_IDENTITY_CONFLICTS_KEY, {})
                    if isinstance(markers, dict):
                        markers.pop(conflict.conflict_id, None)
                        if markers:
                            metadata[_IDENTITY_CONFLICTS_KEY] = markers
                        else:
                            metadata.pop(_IDENTITY_CONFLICTS_KEY, None)
                    connection.execute(
                        "UPDATE projects SET metadata=?,updated_at=? WHERE id=?",
                        (json.dumps(metadata), _now(), row["id"]),
                    )
                return "Projects remain separate. This identity conflict is saved as resolved."

            selected = ProjectPrivacyState(keep_state)
            if not conflict.can_merge_without_purging or selected not in (
                ProjectPrivacyState.INCLUDED,
                ProjectPrivacyState.EXCLUDED,
            ):
                raise AfterTraceError(
                    "This conflict cannot be merged without weakening Never Analyze "
                    "or purging data.",
                    operation="resolve_project_conflict",
                )
            if identity is None or normalize_location(identity.root) != normalize_location(
                conflict.location
            ):
                raise AfterTraceError(
                    "Current repository identity could not be reconfirmed; refresh discovery.",
                    operation="resolve_project_conflict",
                )
            if (
                identity.canonical_identity not in canonical
                and identity.local_identity not in canonical
            ):
                raise AfterTraceError(
                    "Current repository identity no longer matches these records.",
                    operation="resolve_project_conflict",
                )

            reconciliation_rows = []
            for row in rows:
                metadata = json.loads(row["metadata"])
                separations = _separation_values(metadata, _IDENTITY_SEPARATIONS_KEY)
                separations.discard(conflict.conflict_id)
                pairs = _separation_values(metadata, _SEPARATED_PROJECT_PAIRS_KEY)
                pairs.discard(pair_key)
                if separations:
                    metadata[_IDENTITY_SEPARATIONS_KEY] = sorted(separations)
                else:
                    metadata.pop(_IDENTITY_SEPARATIONS_KEY, None)
                if pairs:
                    metadata[_SEPARATED_PROJECT_PAIRS_KEY] = sorted(pairs)
                else:
                    metadata.pop(_SEPARATED_PROJECT_PAIRS_KEY, None)
                markers = metadata.get(_IDENTITY_CONFLICTS_KEY, {})
                if isinstance(markers, dict):
                    markers.pop(conflict.conflict_id, None)
                    if markers:
                        metadata[_IDENTITY_CONFLICTS_KEY] = markers
                    else:
                        metadata.pop(_IDENTITY_CONFLICTS_KEY, None)
                resolutions = metadata.get(_IDENTITY_RESOLUTIONS_KEY, {})
                if not isinstance(resolutions, dict):
                    raise AfterTraceError(
                        "Project resolution metadata is invalid; no changes were saved.",
                        operation="resolve_project_conflict",
                    )
                resolutions[conflict.conflict_id] = selected.value
                metadata[_IDENTITY_RESOLUTIONS_KEY] = resolutions
                row_data = dict(row)
                row_data["metadata"] = json.dumps(metadata)
                reconciliation_rows.append(row_data)
                connection.execute(
                    "UPDATE projects SET metadata=? WHERE id=?",
                    (json.dumps(metadata), row["id"]),
                )

            merged = reconcile_project_identity(
                connection,
                identity,
                reconciliation_rows,
                privacy_resolution=selected.value,
            )
            if merged is None:
                raise AfterTraceError(
                    "Project records could not be safely reconciled; no changes were saved.",
                    operation="resolve_project_conflict",
                )
            location = normalize_location(conflict.location)
            for reference in connection.execute(
                "SELECT id, source_id, source_session_id, project_hint FROM discovered_sessions "
                "WHERE available=1 AND project_id IS NULL"
            ).fetchall():
                if normalize_location(reference["project_hint"]) != location:
                    continue
                existing = None
                if reference["source_session_id"] is not None:
                    existing = connection.execute(
                        "SELECT id FROM sessions WHERE source_id=? AND source_session_id=? "
                        "AND project_id=?",
                        (reference["source_id"], reference["source_session_id"], merged["id"]),
                    ).fetchone()
                connection.execute(
                    "UPDATE discovered_sessions SET project_id=?,session_id=COALESCE(session_id,?) "
                    "WHERE id=?",
                    (merged["id"], existing["id"] if existing else None, reference["id"]),
                )
            for relation in connection.execute(
                "SELECT id,source_id,observed_path,metadata FROM project_sources "
                "WHERE project_id=?",
                (merged["id"],),
            ).fetchall():
                if normalize_location(relation["observed_path"]) != location:
                    continue
                discovered = sum(
                    normalize_location(item[0]) == location
                    for item in connection.execute(
                        "SELECT project_hint FROM discovered_sessions WHERE source_id=? "
                        "AND project_id=? AND available=1",
                        (relation["source_id"], merged["id"]),
                    )
                )
                relation_metadata = json.loads(relation["metadata"])
                relation_metadata["discovered"] = discovered
                connection.execute(
                    "UPDATE project_sources SET metadata=? WHERE id=?",
                    (json.dumps(relation_metadata), relation["id"]),
                )
            return f"Projects merged. Privacy is {selected.value.title()}; import remains explicit."

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
            if _project_has_unresolved_privacy_conflict(connection, project.id):
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
            return WorkspaceSnapshot(
                tuple(projects),
                tuple(sources),
                unclassified,
                never_count,
                conflicts := _project_privacy_conflicts(connection, projects),
                _project_identity_warnings(projects, conflicts),
            )
