import json
from datetime import UTC, datetime

from aftertrace.core.models import (
    AnalysisProvenance,
    AppliedRule,
    ContentFormat,
    DraftState,
    Finding,
    FindingConfidence,
    FindingEvidence,
    FindingState,
    FindingType,
    Harness,
    InstructionScope,
    InstructionSource,
    Message,
    MessageRole,
    Project,
    ProjectPrivacyState,
    ProjectSource,
    ProviderConfig,
    ProviderLocality,
    Rule,
    RuleDraft,
    ScanState,
    Session,
    SessionReference,
    Source,
    ToolCall,
    ToolStatus,
)


def test_stable_enum_values():
    assert list(Harness) == ["codex", "claude_code", "opencode"]
    assert list(ProjectPrivacyState) == ["included", "excluded", "never_analyze"]
    assert list(ProviderLocality) == ["local", "network", "remote"]
    assert list(FindingType) == ["repeated_instruction", "persistent_rule_candidate"]
    assert list(FindingConfidence) == ["high", "medium", "low"]
    assert list(FindingState) == ["new", "reviewed", "saved", "applied", "dismissed", "archived"]
    assert list(MessageRole) == ["user", "assistant", "system", "developer", "tool", "unknown"]


def test_missing_data_privacy_and_mutable_defaults():
    project = Project(display_name="Sanitized example")
    assert project.privacy_state is ProjectPrivacyState.EXCLUDED
    session = Session(source_id="source")
    assert session.project_id is None
    assert session.started_at is session.model is session.source_session_id is None
    assert session.source_available is True
    other = Session(source_id="source")
    session.metadata["example"] = True
    session.messages.append(Message(session_id=session.id, sequence=0, content="Original text"))
    assert other.messages == [] and other.metadata == {}
    assert session.id != other.id
    assert session.messages[0].role is MessageRole.UNKNOWN
    assert session.messages[0].content_format is ContentFormat.UNKNOWN
    assert ToolCall(session_id=session.id, sequence=0, name="test").status is ToolStatus.UNKNOWN
    config = ProviderConfig(display_name="Example", endpoint="http://localhost:1234/v1")
    assert config.enabled is False and config.locality is ProviderLocality.LOCAL


def test_normalized_records_serialize_without_altering_evidence():
    timestamp = datetime(2026, 1, 2, tzinfo=UTC)
    original = "Run focused tests.\n  Preserve whitespace. 🐾"
    source = Source(harness=Harness.CODEX, display_name="Example", location="configured-location")
    project = Project(display_name="Example")
    session = Session(source_id=source.id, started_at=timestamp)
    message = Message(
        session_id=session.id,
        sequence=0,
        role=MessageRole.USER,
        content=original,
        timestamp=timestamp,
        author_source="user",
    )
    session.messages.append(message)
    finding = Finding(
        finding_type=FindingType.REPEATED_INSTRUCTION,
        canonical_pattern="Run focused tests",
        title="Focused verification",
        summary="Sanitized example",
        confidence=FindingConfidence.HIGH,
        analysis_kind="example",
        analysis_version="test",
    )
    evidence = FindingEvidence(
        finding_id=finding.id,
        session_id=session.id,
        message_id=message.id,
        evidence_type="message",
        original_text=original,
    )
    draft = RuleDraft(text="Run focused tests", finding_id=finding.id)
    rule = Rule(text=draft.text)
    records = [
        source,
        project,
        session,
        finding,
        evidence,
        draft,
        rule,
        ProjectSource(project_id=project.id, source_id=source.id),
        SessionReference(source_id=source.id, source_session_id=None, source_locator="example"),
        InstructionSource(harness=Harness.CODEX, path="example/AGENTS.md"),
        AppliedRule(rule_draft_id=draft.id, rule_id=rule.id, applied_at=timestamp),
        ProviderConfig(display_name="Example", endpoint="http://localhost/v1"),
        ScanState(source_id=source.id),
        AnalysisProvenance(),
        ToolCall(session_id=session.id, sequence=1, name="test", arguments={"args": ["--help"]}),
    ]
    for record in records:
        assert json.loads(json.dumps(record.to_dict())) == record.to_dict()
    encoded = json.loads(json.dumps(session.to_dict()))
    assert encoded["messages"][0]["content"] == original
    assert encoded["started_at"] == "2026-01-02T00:00:00+00:00"
    assert encoded["messages"][0]["role"] == "user"
    assert evidence.to_dict()["original_text"] == original
    assert finding.provenance.provider_id is None
    assert draft.state is DraftState.DRAFT and draft.scope is InstructionScope.UNKNOWN
