import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from aftertrace.analysis.repetitions import RepeatedInstructionAnalyzer
from aftertrace.core.errors import AfterTraceError, DatabaseError
from aftertrace.core.findings import ANALYSIS_VERSION, confidence_for_sessions
from aftertrace.core.models import FindingConfidence, FindingState, Harness, ProjectPrivacyState
from aftertrace.services import finding_store
from aftertrace.services.database import SCHEMA_VERSION, open_database
from aftertrace.services.findings import FindingPublicationService, RepetitionQualityGate
from aftertrace.services.repository import WorkspaceRepository


@pytest.fixture
def history(tmp_path):
    repository = WorkspaceRepository(tmp_path / "aftertrace.sqlite3")

    def add(
        session, *, project="p", source="s", harness=Harness.CODEX, text="Run the tests.", day=1
    ):
        # Synthetic AfterTrace-owned normalized imported data, with no raw harness history.
        timestamp = datetime(2026, 1, day, tzinfo=UTC).isoformat()
        with open_database(repository.path) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO sources (id, harness, display_name, location) "
                "VALUES (?, ?, 'Synthetic', 'synthetic')",
                (source, harness.value),
            )
            connection.execute(
                "INSERT OR IGNORE INTO projects (id, display_name, privacy_state) "
                "VALUES (?, 'Synthetic', 'included')",
                (project,),
            )
            connection.execute(
                "INSERT INTO sessions (id, source_id, project_id, imported_at) VALUES (?, ?, ?, ?)",
                (session, source, project, timestamp),
            )
            connection.execute(
                "INSERT INTO messages (id, session_id, sequence, role, author_source, content, "
                "timestamp) VALUES (?, ?, 0, 'user', 'user', ?, ?)",
                (f"{session}-m", session, text, timestamp),
            )

    for number in range(3):
        add(f"a{number}", day=number + 1)
    return repository, add


def candidates(repository):
    return RepeatedInstructionAnalyzer().analyze(repository.imported_messages_for_analysis())


def snapshot(repository):
    return [
        (finding.to_dict(), [e.to_dict() for e in repository.finding_evidence(finding.id)])
        for finding in repository.findings()
    ]


def test_quality_gate_accepts_three_session_candidate(history):
    repository, _ = history
    (candidate,) = candidates(repository)
    eligible = {item.message.id: item for item in repository.imported_messages_for_analysis()}
    assert RepetitionQualityGate().validate(candidate, eligible) is candidate


@pytest.mark.parametrize(
    "field,value",
    [
        ("canonical_key", ""),
        ("canonical_key", "RUN THE TESTS"),
        ("representative_text", ""),
        ("representative_text", "PRIVATE_INVALID_TEXT"),
        ("occurrence_count", 99),
        ("occurrence_count", True),
        ("distinct_session_count", 99),
        ("project_ids", ("wrong",)),
        ("harness_distribution", ()),
        ("source_distribution", ()),
        ("first_observed_at", None),
        ("last_observed_at", None),
        ("evidence", ()),
        ("evidence", None),
    ],
)
def test_quality_gate_rejects_malformed_candidates_without_publication(history, field, value):
    repository, _ = history
    candidate = replace(candidates(repository)[0], **{field: value})
    with pytest.raises(AfterTraceError, match="quality gate") as caught:
        FindingPublicationService(repository).publish([candidate])
    assert "PRIVATE_INVALID_TEXT" not in str(caught.value)
    assert repository.findings() == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("message_id", "missing"),
        ("session_id", "missing"),
        ("project_id", "wrong"),
        ("source_id", "wrong"),
        ("harness", Harness.OPENCODE),
        ("harness", "codex"),
        ("observed_at", None),
        ("original_text", "PRIVATE_INVALID_TEXT"),
        ("start_offset", -1),
        ("start_offset", True),
        ("end_offset", 9999),
    ],
)
def test_gate_checks_each_receipt_against_normalized_storage(history, field, value):
    repository, _ = history
    original = candidates(repository)[0]
    evidence = (replace(original.evidence[0], **{field: value}), *original.evidence[1:])
    with pytest.raises(AfterTraceError, match="quality gate"):
        FindingPublicationService(repository).publish([replace(original, evidence=evidence)])
    assert repository.findings() == []


def test_quality_gate_independently_rejects_insufficient_and_duplicate_support(history):
    repository, _ = history
    original = candidates(repository)[0]
    insufficient = replace(
        original, evidence=original.evidence[:2], occurrence_count=2, distinct_session_count=2
    )
    duplicate = replace(
        original, evidence=(*original.evidence, original.evidence[0]), occurrence_count=4
    )
    service = FindingPublicationService(repository)
    for invalid in (insufficient, duplicate, object(), None):
        with pytest.raises(AfterTraceError, match="quality gate"):
            service.publish([invalid])
    with pytest.raises(AfterTraceError, match="duplicate candidate"):
        service.publish([original, original])
    assert repository.findings() == []


def test_persistence_idempotence_receipts_provenance_and_no_write_churn(history):
    repository, _ = history
    service = FindingPublicationService(repository)
    (finding,) = service.run()
    assert finding.state is FindingState.NEW
    assert finding.finding_type.value == "repeated_instruction"
    assert finding.occurrence_count == finding.distinct_session_count == 3
    assert finding.distinct_project_count == 1
    assert finding.metadata["currently_supported"] is True
    assert finding.metadata["harness_distribution"] == {"codex": 3}
    assert finding.metadata["source_distribution"] == {"s": 3}
    assert finding.analysis_version == ANALYSIS_VERSION
    assert finding.provenance.analyzer_versions == {"repeated_instruction": "v1"}
    assert finding.provenance.provider_id is finding.provenance.model is None
    assert finding.recommendation is None
    assert finding.generated_at is not None and finding.last_evaluated_at is not None
    receipts = repository.finding_evidence(finding.id)
    assert len(receipts) == len({item.id for item in receipts}) == 3
    for receipt in receipts:
        assert receipt.finding_id == finding.id
        assert receipt.message_id == f"{receipt.session_id}-m"
        assert receipt.original_text == "Run the tests."
        assert receipt.metadata == {"project_id": "p", "source_id": "s", "harness": "codex"}
    before = snapshot(repository)
    assert service.run() == [finding]
    assert snapshot(repository) == before
    with open_database(repository.path) as connection:
        assert connection.execute("SELECT count(*) FROM findings").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM finding_evidence").fetchone()[0] == 3
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 2
    # Reopening and ordinary initialization preserve the backend result.
    assert snapshot(WorkspaceRepository(repository.path)) == before


@pytest.mark.parametrize("state", list(FindingState))
def test_evidence_growth_preserves_identity_first_seen_and_user_state(history, state):
    repository, add = history
    service = FindingPublicationService(repository)
    (first,) = service.run()
    repository.update_finding_state(first.id, state)
    add("b0", project="other", source="claude", harness=Harness.CLAUDE_CODE, day=5)
    add("b1", project="other", source="claude", harness=Harness.CLAUDE_CODE, day=6)
    (updated,) = service.run()
    assert updated.id == first.id
    assert updated.state is state
    assert updated.generated_at == first.generated_at
    assert updated.first_seen_at == first.first_seen_at
    assert updated.last_seen_at == datetime(2026, 1, 6, tzinfo=UTC)
    assert updated.occurrence_count == updated.distinct_session_count == 5
    assert updated.distinct_project_count == 2
    assert updated.metadata["harness_distribution"] == {"codex": 3, "claude_code": 2}
    assert updated.confidence is FindingConfidence.MEDIUM
    assert len(repository.finding_evidence(updated.id)) == 5
    assert len(repository.findings()) == 1


@pytest.mark.parametrize(
    "count,expected",
    [
        (3, FindingConfidence.LOW),
        (4, FindingConfidence.LOW),
        (5, FindingConfidence.MEDIUM),
        (9, FindingConfidence.MEDIUM),
        (10, FindingConfidence.HIGH),
        (11, FindingConfidence.HIGH),
    ],
)
def test_provisional_confidence_is_categorical(count, expected):
    assert confidence_for_sessions(count) is expected


def test_excluded_support_is_synchronized_and_history_remains_traceable_for_privacy(history):
    repository, add = history
    for number in range(3):
        add(f"b{number}", project="other", day=5)
    service = FindingPublicationService(repository)
    (first,) = service.run()
    repository.set_privacy("p", ProjectPrivacyState.EXCLUDED)
    (updated,) = service.run()
    assert updated.id == first.id and updated.distinct_session_count == 3
    assert {item.metadata["project_id"] for item in repository.finding_evidence(first.id)} == {
        "other"
    }
    repository.set_privacy("other", ProjectPrivacyState.EXCLUDED)
    assert service.run() == []
    (unsupported,) = repository.findings()
    assert unsupported.metadata["currently_supported"] is False
    assert unsupported.occurrence_count == unsupported.distinct_session_count == 0
    assert repository.finding_evidence(first.id) == ()
    assert unsupported.metadata["origin_project_ids"] == ["other", "p"]
    # Even with no current receipts, a later Never Analyze decision must find the history.
    repository.set_privacy("p", ProjectPrivacyState.NEVER_ANALYZE)
    assert repository.findings() == []


def test_first_seen_survives_normal_evidence_removal_but_not_privacy_removal(history):
    repository, add = history
    for number in range(3):
        add(f"b{number}", project="other", day=5)
    service = FindingPublicationService(repository)
    (first,) = service.run()
    repository.set_privacy("p", ProjectPrivacyState.EXCLUDED)
    (updated,) = service.run()
    assert updated.first_seen_at == first.first_seen_at
    repository.set_privacy("p", ProjectPrivacyState.NEVER_ANALYZE)
    (cleaned,) = repository.findings()
    assert cleaned.first_seen_at == datetime(2026, 1, 5, tzinfo=UTC)


@pytest.mark.parametrize("remaining", [0, 1, 2, 3])
def test_never_analyze_removes_private_wording_and_rebuilds_remaining_support(history, remaining):
    repository, add = history
    private = "RUN   THE TESTS!"
    with open_database(repository.path) as connection:
        connection.execute("UPDATE messages SET content = ?", (private,))
    for number in range(remaining):
        add(f"b{number}", project="other", day=5)
    (finding,) = FindingPublicationService(repository).run()
    assert finding.title == private
    repository.update_finding_state(finding.id, FindingState.DISMISSED)
    repository.set_privacy("p", ProjectPrivacyState.NEVER_ANALYZE)
    if remaining:
        (cleaned,) = repository.findings()
        assert cleaned.id == finding.id
        assert cleaned.state is FindingState.DISMISSED
        assert cleaned.title == "Run the tests."
        assert cleaned.first_seen_at == datetime(2026, 1, 5, tzinfo=UTC)
        assert cleaned.occurrence_count == cleaned.distinct_session_count == remaining
        assert cleaned.distinct_project_count == 1
        assert cleaned.metadata["origin_project_ids"] == ["other"]
        assert cleaned.metadata["source_distribution"] == {"s": remaining}
        assert cleaned.metadata["currently_supported"] is (remaining >= 3)
        assert len(repository.finding_evidence(cleaned.id)) == remaining
    else:
        assert repository.findings() == []
    with open_database(repository.path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            connection.execute("SELECT count(*) FROM sessions WHERE project_id = 'p'").fetchone()[0]
            == 0
        )
    assert private.encode() not in repository.path.read_bytes()


def test_stale_candidates_cannot_publish_after_scope_change(history):
    repository, _ = history
    saved = candidates(repository)
    repository.set_privacy("p", ProjectPrivacyState.EXCLUDED)
    with pytest.raises(AfterTraceError, match="quality gate"):
        FindingPublicationService(repository).publish(saved)
    assert repository.findings() == []


def test_publication_failure_rolls_back_aggregates_and_evidence(history, monkeypatch):
    repository, add = history
    service = FindingPublicationService(repository)
    service.run()
    before = snapshot(repository)
    add("a3", day=5)
    original = finding_store._upsert

    def fail_receipt(connection, table, record):
        if table == "finding_evidence":
            raise sqlite3.OperationalError("Synthetic receipt failure")
        original(connection, table, record)

    monkeypatch.setattr(finding_store, "_upsert", fail_receipt)
    with pytest.raises(DatabaseError):
        service.run()
    assert snapshot(repository) == before


def test_never_analyze_serializes_with_publication_and_purges_committed_result(
    history, monkeypatch
):
    repository, _ = history
    service = FindingPublicationService(repository)
    entered, release, revoked = threading.Event(), threading.Event(), threading.Event()
    validate = service.gate.validate
    errors = []

    def paused(*args):
        entered.set()
        assert release.wait(3)
        return validate(*args)

    def publish():
        try:
            service.run()
        except Exception as error:
            errors.append(error)

    def revoke():
        try:
            repository.set_privacy("p", ProjectPrivacyState.NEVER_ANALYZE)
            revoked.set()
        except Exception as error:
            errors.append(error)

    monkeypatch.setattr(service.gate, "validate", paused)
    publisher = threading.Thread(target=publish)
    publisher.start()
    assert entered.wait(3)
    revoker = threading.Thread(target=revoke)
    revoker.start()
    assert not revoked.wait(0.05)
    release.set()
    publisher.join(4)
    revoker.join(4)
    assert not publisher.is_alive() and not revoker.is_alive()
    assert errors == [] and revoked.is_set()
    assert repository.findings() == []


def test_privacy_refresh_failure_rolls_back_scope_and_derived_content(history, monkeypatch):
    repository, add = history
    for number in range(3):
        add(f"b{number}", project="other")
    FindingPublicationService(repository).run()
    before = snapshot(repository)

    def fail(*args):
        raise sqlite3.OperationalError("Synthetic refresh failure")

    monkeypatch.setattr(finding_store, "write_bundle", fail)
    with pytest.raises(DatabaseError):
        repository.set_privacy("p", ProjectPrivacyState.NEVER_ANALYZE)
    assert snapshot(repository) == before
    with open_database(repository.path) as connection:
        assert (
            connection.execute("SELECT privacy_state FROM projects WHERE id = 'p'").fetchone()[0]
            == "included"
        )


def test_missing_finding_state_update_is_safe(history):
    repository, _ = history
    with pytest.raises(AfterTraceError, match="no longer available"):
        repository.update_finding_state("missing", FindingState.REVIEWED)


def test_raw_occurrences_do_not_inflate_session_support_or_confidence(history):
    repository, _ = history
    with open_database(repository.path) as connection:
        connection.execute(
            "UPDATE messages SET content = ? WHERE session_id = 'a0'",
            ("- Run the tests.\n" * 3,),
        )
    (finding,) = FindingPublicationService(repository).run()
    assert finding.occurrence_count == 5
    assert finding.distinct_session_count == 3
    assert finding.confidence is FindingConfidence.LOW
    assert len(repository.finding_evidence(finding.id)) == 5


def test_gate_rejects_overlapping_occurrences(history):
    repository, _ = history
    with open_database(repository.path) as connection:
        connection.execute("UPDATE messages SET content = 'Run the tests. '")
    (candidate,) = candidates(repository)
    original = candidate.evidence[0]
    overlap = replace(original, original_text="Run the tests. ", end_offset=original.end_offset + 1)
    candidate = replace(candidate, evidence=(*candidate.evidence, overlap), occurrence_count=4)
    with pytest.raises(AfterTraceError, match="quality gate"):
        FindingPublicationService(repository).publish([candidate])


def test_unsupported_finding_can_regain_support_without_resetting_identity_or_state(history):
    repository, _ = history
    service = FindingPublicationService(repository)
    (first,) = service.run()
    repository.update_finding_state(first.id, FindingState.SAVED)
    repository.set_privacy("p", ProjectPrivacyState.EXCLUDED)
    assert service.run() == []
    repository.set_privacy("p", ProjectPrivacyState.INCLUDED)
    (restored,) = service.run()
    assert restored.id == first.id
    assert restored.state is FindingState.SAVED
    assert restored.generated_at == first.generated_at
    assert restored.first_seen_at == first.first_seen_at
    assert restored.metadata["currently_supported"] is True


def test_timestamp_absence_does_not_block_persistence(history):
    repository, _ = history
    with open_database(repository.path) as connection:
        connection.execute("UPDATE messages SET timestamp = NULL")
    (finding,) = FindingPublicationService(repository).run()
    assert finding.first_seen_at is finding.last_seen_at is None
    assert all(item.observed_at is None for item in repository.finding_evidence(finding.id))


def test_high_confidence_persists_and_is_categorical(history):
    repository, add = history
    for number in range(3, 10):
        add(f"a{number}")
    (finding,) = FindingPublicationService(repository).run()
    assert finding.confidence is FindingConfidence.HIGH
    assert repository.findings()[0].confidence is FindingConfidence.HIGH


def test_full_result_transaction_rolls_back_preceding_finding_writes(history, monkeypatch):
    repository, add = history
    service = FindingPublicationService(repository)
    service.run()
    before = snapshot(repository)
    add("a3")
    for number in range(3):
        add(f"b{number}", text="Use the existing architecture.")
    upsert = finding_store._upsert
    preceding_writes = []

    def fail_second_finding(connection, table, record):
        if table == "findings":
            if preceding_writes:
                raise sqlite3.OperationalError("Synthetic second Finding failure")
            preceding_writes.append(record.id)
        upsert(connection, table, record)

    monkeypatch.setattr(finding_store, "_upsert", fail_second_finding)
    with pytest.raises(DatabaseError):
        service.run()
    assert len(preceding_writes) == 1
    assert snapshot(repository) == before


def test_incomparable_historical_timestamps_fail_with_domain_error_and_preserve_state(history):
    repository, add = history
    service = FindingPublicationService(repository)
    service.run()
    before = snapshot(repository)
    for number in range(3):
        add(f"b{number}", project="other", day=5)
    repository.set_privacy("p", ProjectPrivacyState.EXCLUDED)
    with open_database(repository.path) as connection:
        connection.execute(
            "UPDATE messages SET timestamp = '2026-01-05T00:00:00' "
            "WHERE session_id IN (SELECT id FROM sessions WHERE project_id = 'other')"
        )
    with pytest.raises(AfterTraceError, match="timezone"):
        service.run()
    assert snapshot(repository) == before
