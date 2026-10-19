"""SQLite Finding/receipt operations shared by publication and privacy cleanup.

No detector or I/O entry point lives here: callers own the database transaction.
"""

import json
from datetime import UTC, datetime

from aftertrace.core.findings import ANALYSIS_KIND, FindingBundle, summarize_repetition
from aftertrace.core.models import (
    AnalysisProvenance,
    Finding,
    FindingConfidence,
    FindingEvidence,
    FindingState,
    FindingType,
    Harness,
)


def read_finding(row) -> Finding:
    data = dict(row)
    for key in ("first_seen_at", "last_seen_at", "generated_at", "last_evaluated_at"):
        data[key] = datetime.fromisoformat(data[key]) if data[key] else None
    data.update(
        metadata=json.loads(data["metadata"]),
        finding_type=FindingType(data["finding_type"]),
        confidence=FindingConfidence(data["confidence"]),
        state=FindingState(data["state"]),
        affected_harnesses=[Harness(item) for item in json.loads(data["affected_harnesses"])],
        provenance=AnalysisProvenance(**json.loads(data["provenance"])),
    )
    return Finding(**data)


def read_evidence(row) -> FindingEvidence:
    data = dict(row)
    data["metadata"] = json.loads(data["metadata"])
    data["observed_at"] = (
        datetime.fromisoformat(data["observed_at"]) if data["observed_at"] else None
    )
    return FindingEvidence(**data)


def evidence_for(connection, finding_id: str) -> tuple[FindingEvidence, ...]:
    return tuple(
        read_evidence(row)
        for row in connection.execute(
            "SELECT * FROM finding_evidence WHERE finding_id = ? "
            "ORDER BY session_id, message_id, start_offset, end_offset, id",
            (finding_id,),
        )
    )


def _upsert(connection, table, record):
    data = record.to_dict()
    values = {
        key: json.dumps(value, ensure_ascii=False, allow_nan=False)
        if isinstance(value, (list, dict))
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


def write_bundle(connection, bundle: FindingBundle) -> Finding:
    """Synchronize, with no writes or timestamp churn for an identical logical result."""
    finding = bundle.finding
    row = connection.execute("SELECT * FROM findings WHERE id = ?", (finding.id,)).fetchone()
    old = read_finding(row) if row else None
    old_evidence = evidence_for(connection, finding.id)
    if old:
        before, after = old.to_dict(), finding.to_dict()
        before.pop("last_evaluated_at"), after.pop("last_evaluated_at")
        if before == after and old_evidence == bundle.evidence:
            return old
    _upsert(connection, "findings", finding)
    desired = {item.id: item for item in bundle.evidence}
    previous = {item.id: item for item in old_evidence}
    for identity in previous.keys() - desired.keys():
        connection.execute("DELETE FROM finding_evidence WHERE id = ?", (identity,))
    for item in bundle.evidence:
        if previous.get(item.id) != item:
            _upsert(connection, "finding_evidence", item)
    return finding


def synchronize(connection, bundles: list[FindingBundle]) -> list[Finding]:
    """Reconcile a full detector result without overwriting user-controlled state."""
    previous = {
        row["id"]: read_finding(row)
        for row in connection.execute(
            "SELECT * FROM findings WHERE analysis_kind = ? "
            "AND finding_type = 'repeated_instruction'",
            (ANALYSIS_KIND,),
        )
    }
    result = []
    now = datetime.now(UTC)
    for bundle in bundles:
        old = previous.pop(bundle.finding.id, None)
        result.append(
            write_bundle(
                connection,
                summarize_repetition(
                    bundle.finding.canonical_pattern, bundle.evidence, now, previous=old
                ),
            )
        )
    for old in previous.values():
        write_bundle(connection, summarize_repetition(old.canonical_pattern, (), now, previous=old))
    return result


def lineage_findings(connection, project_id: str) -> list[str]:
    # An unsupported Finding may have no current receipts, but privacy lineage survives.
    return [
        row["id"]
        for row in connection.execute(
            "SELECT id, metadata FROM findings WHERE analysis_kind = ?", (ANALYSIS_KIND,)
        )
        if project_id in json.loads(row["metadata"]).get("origin_project_ids", [])
    ]


def purge_affected_findings(connection, project_id: str) -> None:
    """Refresh managed multi-project Findings using only remaining permitted receipts.

    Unknown/legacy derived content retains the original conservative full-deletion
    policy. Dependent drafts/applied rules have already been removed by the caller.
    """
    for row in connection.execute(
        "SELECT * FROM findings WHERE id IN (SELECT id FROM purged_findings)"
    ).fetchall():
        old = read_finding(row)
        remaining = []
        if old.analysis_kind == ANALYSIS_KIND:
            rows = connection.execute(
                "SELECT e.*, se.project_id AS current_project, se.source_id AS current_source, "
                "s.harness AS current_harness, m.content AS current_content "
                "FROM finding_evidence e JOIN sessions se ON se.id = e.session_id "
                "JOIN messages m ON m.id = e.message_id AND m.session_id = se.id "
                "JOIN projects p ON p.id = se.project_id JOIN sources s ON s.id = se.source_id "
                "WHERE e.finding_id = ? AND se.project_id != ? AND p.privacy_state = 'included' "
                "AND se.imported_at IS NOT NULL AND m.role = 'user' AND m.author_source = 'user' "
                "ORDER BY e.session_id, e.message_id, e.start_offset, e.end_offset, e.id",
                (old.id, project_id),
            ).fetchall()
            for item in rows:
                data = dict(item)
                project, source, harness, content = (
                    data.pop(key)
                    for key in (
                        "current_project",
                        "current_source",
                        "current_harness",
                        "current_content",
                    )
                )
                receipt = read_evidence(data)
                start, end = receipt.start_offset, receipt.end_offset
                if (
                    start is None
                    or end is None
                    or not 0 <= start < end <= len(content)
                    or content[start:end] != receipt.original_text
                ):
                    continue
                receipt.metadata = {"project_id": project, "source_id": source, "harness": harness}
                remaining.append(receipt)
        if remaining:
            old.metadata["origin_project_ids"] = [
                value for value in old.metadata.get("origin_project_ids", []) if value != project_id
            ]
            write_bundle(
                connection,
                summarize_repetition(
                    old.canonical_pattern,
                    tuple(remaining),
                    datetime.now(UTC),
                    previous=old,
                    preserve_first_seen=False,
                ),
            )
        else:
            connection.execute("DELETE FROM finding_evidence WHERE finding_id = ?", (old.id,))
            connection.execute("DELETE FROM findings WHERE id = ?", (old.id,))
