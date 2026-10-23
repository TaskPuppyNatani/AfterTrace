"""Reconcile proven repository identities inside the caller's discovery transaction.

Only exact canonical matches or the currently verified Git-directory anchor qualify.
Paths/names are never sufficient. No source content or normalized children are replaced.
"""

import json
from collections import defaultdict
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from aftertrace.core.findings import ANALYSIS_KIND, summarize_repetition
from aftertrace.platform.projects import ProjectIdentity, normalize_location
from aftertrace.services.finding_store import evidence_for, read_finding, write_bundle


def _metadata(rows, ignored=()):
    result = {}
    for row in rows:
        for key, value in json.loads(row["metadata"]).items():
            if key in ignored:
                continue
            if key in result and result[key] != value:
                return None  # Preserve conflicting user metadata instead of choosing a winner.
            result[key] = value
    return result


def _relations(connection, rows, survivor_id, canonical):
    """Plan relation consolidation before changing anything; reject conflicting metadata."""
    aliases = {row["canonical_identity"] for row in rows}
    grouped = defaultdict(list)
    for row in rows:
        for relation in connection.execute(
            "SELECT * FROM project_sources WHERE project_id = ? ORDER BY id", (row["id"],)
        ):
            location = normalize_location(relation["observed_path"])
            grouped[(relation["source_id"], location or relation["id"])].append(relation)
    result = []
    for items in grouped.values():
        base = min(items, key=lambda item: (item["project_id"] != survivor_id, item["id"]))
        metadata = _metadata(items, ("discovered",))
        identifiers = {
            item["source_project_identifier"]
            for item in items
            if item["source_project_identifier"] not in aliases | {None}
        }
        if metadata is None or len(identifiers) > 1:
            return None
        counts = [json.loads(item["metadata"]).get("discovered", 0) for item in items]
        if any(type(count) is not int or count < 0 for count in counts):
            return None
        metadata["discovered"] = sum(counts)
        location = normalize_location(base["observed_path"])
        identity = (
            str(uuid5(NAMESPACE_URL, f"{survivor_id}:{base['source_id']}:{location}"))
            if location
            else base["id"]
        )
        first = [item["first_seen_at"] for item in items if item["first_seen_at"]]
        last = [item["last_seen_at"] for item in items if item["last_seen_at"]]
        result.append(
            (
                identity,
                survivor_id,
                base["source_id"],
                next(iter(identifiers)) if identifiers else canonical,
                base["observed_path"],
                min(first) if first else None,
                max(last) if last else None,
                json.dumps(metadata),
                [item["id"] for item in items],
            )
        )
    return result


def _finding_projects(connection, mapping):
    if not mapping:
        return
    affected = set()
    for row in connection.execute(
        "SELECT e.id,e.finding_id,e.metadata,se.project_id FROM finding_evidence e "
        "JOIN sessions se ON se.id=e.session_id"
    ).fetchall():
        metadata = json.loads(row["metadata"])
        if metadata.get("project_id") in mapping:
            metadata["project_id"] = row["project_id"]
            connection.execute(
                "UPDATE finding_evidence SET metadata=? WHERE id=?",
                (json.dumps(metadata), row["id"]),
            )
            affected.add(row["finding_id"])
    for row in connection.execute("SELECT * FROM findings").fetchall():
        metadata = json.loads(row["metadata"])
        for key in ("project_ids", "origin_project_ids"):
            if set(metadata.get(key, [])) & mapping.keys():
                metadata[key] = sorted({mapping.get(value, value) for value in metadata[key]})
                affected.add(row["id"])
        if row["id"] not in affected:
            continue
        if row["analysis_kind"] == ANALYSIS_KIND:
            finding = read_finding(row)
            finding.metadata = metadata
            write_bundle(
                connection,
                summarize_repetition(
                    finding.canonical_pattern,
                    evidence_for(connection, finding.id),
                    datetime.now(UTC),
                    previous=finding,
                ),
            )
        else:
            projects = connection.execute(
                "SELECT count(DISTINCT se.project_id) FROM finding_evidence e "
                "JOIN sessions se ON se.id=e.session_id WHERE e.finding_id=?",
                (row["id"],),
            ).fetchone()[0]
            connection.execute(
                "UPDATE findings SET metadata=?,distinct_project_count=? WHERE id=?",
                (json.dumps(metadata), projects, row["id"]),
            )


def reconcile_project_identity(connection, identity: ProjectIdentity, rows):
    if len({row["privacy_state"] for row in rows}) != 1:
        return None
    survivor = min(
        rows,
        key=lambda row: (
            row["canonical_identity"] != identity.canonical_identity,
            row["created_at"] or "",
            row["id"],
        ),
    )
    metadata = _metadata(rows, ("identity_kind",))
    if metadata is None:
        return None
    metadata["identity_kind"] = identity.kind
    if len(rows) == 1 and survivor["canonical_identity"] == identity.canonical_identity:
        return survivor
    relations = _relations(connection, rows, survivor["id"], identity.canonical_identity)
    if relations is None:
        return None
    mapping = {row["id"]: survivor["id"] for row in rows if row["id"] != survivor["id"]}
    # Identity-only updates preserve normalized Session/Message/ToolCall IDs and fingerprints.
    for old, new in mapping.items():
        for table in (
            "sessions",
            "discovered_sessions",
            "instruction_sources",
            "rules",
            "rule_drafts",
        ):
            connection.execute(f"UPDATE {table} SET project_id=? WHERE project_id=?", (new, old))
    for relation in relations:
        connection.execute(
            "INSERT INTO project_sources (id,project_id,source_id,source_project_identifier,"
            "observed_path,first_seen_at,last_seen_at,metadata) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET project_id=excluded.project_id, "
            "source_project_identifier=excluded.source_project_identifier,"
            "first_seen_at=excluded.first_seen_at,last_seen_at=excluded.last_seen_at,"
            "metadata=excluded.metadata",
            relation[:-1],
        )
        for old in relation[-1]:
            if old != relation[0]:
                connection.execute("DELETE FROM project_sources WHERE id=?", (old,))
    _finding_projects(connection, mapping)
    for old in mapping:
        connection.execute("DELETE FROM projects WHERE id=?", (old,))
    created = [row["created_at"] for row in rows if row["created_at"]]
    connection.execute(
        "UPDATE projects SET canonical_identity=?,git_remote=?,metadata=?,created_at=?,"
        "updated_at=? WHERE id=?",
        (
            identity.canonical_identity,
            identity.git_remote,
            json.dumps(metadata),
            min(created) if created else None,
            datetime.now(UTC).isoformat(),
            survivor["id"],
        ),
    )
    return connection.execute("SELECT * FROM projects WHERE id=?", (survivor["id"],)).fetchone()
