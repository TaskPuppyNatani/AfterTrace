"""Normalized, JSON-friendly records; no harness, filesystem, or UI dependencies."""

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from enum import StrEnum
from typing import TypeAlias
from uuid import uuid4

JSONValue: TypeAlias = str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]
Metadata: TypeAlias = dict[str, JSONValue]


class Harness(StrEnum):
    CODEX = "codex"
    CLAUDE_CODE = "claude_code"
    OPENCODE = "opencode"


class ProjectPrivacyState(StrEnum):
    INCLUDED = "included"
    EXCLUDED = "excluded"
    NEVER_ANALYZE = "never_analyze"


class ProviderLocality(StrEnum):
    LOCAL = "local"
    NETWORK = "network"
    REMOTE = "remote"


class FindingType(StrEnum):
    REPEATED_INSTRUCTION = "repeated_instruction"
    PERSISTENT_RULE_CANDIDATE = "persistent_rule_candidate"


class FindingConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FindingState(StrEnum):
    NEW = "new"
    REVIEWED = "reviewed"
    SAVED = "saved"
    APPLIED = "applied"
    DISMISSED = "dismissed"
    ARCHIVED = "archived"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    DEVELOPER = "developer"
    TOOL = "tool"
    UNKNOWN = "unknown"


class ContentFormat(StrEnum):
    TEXT = "text"
    MARKDOWN = "markdown"
    JSON = "json"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class ToolStatus(StrEnum):
    UNKNOWN = "unknown"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InstructionScope(StrEnum):
    GLOBAL = "global"
    PROJECT = "project"
    DIRECTORY = "directory"
    UNKNOWN = "unknown"


class DraftState(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    APPLIED = "applied"
    DISCARDED = "discarded"


def _json_value(value: object) -> JSONValue:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _json_value(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json_value(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported normalized value: {type(value).__name__}")


@dataclass(kw_only=True)
class NormalizedRecord:
    metadata: Metadata = field(default_factory=dict)

    def to_dict(self) -> dict[str, JSONValue]:
        """Encode enum values and ISO timestamps without changing original text."""
        return {f.name: _json_value(getattr(self, f.name)) for f in fields(self)}


@dataclass(kw_only=True)
class Entity(NormalizedRecord):
    # These IDs are AfterTrace-owned, not fabricated native source identifiers.
    id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(kw_only=True)
class Source(Entity):
    harness: Harness
    display_name: str
    location: str
    enabled: bool = True
    discovered_automatically: bool = False
    last_scan_at: datetime | None = None


@dataclass(kw_only=True)
class Project(Entity):
    display_name: str
    canonical_identity: str | None = None
    primary_location: str | None = None
    git_remote: str | None = None
    # Discovery never grants permission to analyze.
    privacy_state: ProjectPrivacyState = ProjectPrivacyState.EXCLUDED
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(kw_only=True)
class ProjectSource(Entity):
    project_id: str
    source_id: str
    source_project_identifier: str | None = None
    observed_path: str | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


@dataclass(kw_only=True)
class SessionReference(NormalizedRecord):
    source_id: str
    source_session_id: str | None
    source_locator: str
    project_hint: str | None = None
    started_at_hint: datetime | None = None
    fingerprint_hint: str | None = None


@dataclass(kw_only=True)
class Message(Entity):
    session_id: str
    sequence: int
    content: str
    role: MessageRole = MessageRole.UNKNOWN
    timestamp: datetime | None = None
    content_format: ContentFormat = ContentFormat.UNKNOWN
    author_source: str | None = None


@dataclass(kw_only=True)
class ToolCall(Entity):
    session_id: str
    sequence: int
    name: str
    message_id: str | None = None
    arguments: JSONValue = None
    result_summary: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    status: ToolStatus = ToolStatus.UNKNOWN


@dataclass(kw_only=True)
class Session(Entity):
    source_id: str
    source_session_id: str | None = None
    project_id: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    model: str | None = None
    title: str | None = None
    working_directory: str | None = None
    messages: list[Message] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    source_fingerprint: str | None = None
    source_available: bool = True
    imported_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(kw_only=True)
class InstructionSource(Entity):
    harness: Harness
    path: str
    project_id: str | None = None
    scope: InstructionScope = InstructionScope.UNKNOWN
    exists: bool = False
    content_fingerprint: str | None = None
    discovered_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(kw_only=True)
class Rule(Entity):
    text: str
    category: str | None = None
    scope: InstructionScope = InstructionScope.UNKNOWN
    project_id: str | None = None
    source_ids: list[str] = field(default_factory=list)
    target_harnesses: list[Harness] = field(default_factory=list)
    state: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(kw_only=True)
class AnalysisProvenance(NormalizedRecord):
    deterministic_steps: list[str] = field(default_factory=list)
    semantic_steps: list[str] = field(default_factory=list)
    provider_id: str | None = None
    provider_locality: ProviderLocality | None = None
    model: str | None = None
    request_purpose: str | None = None
    analyzer_versions: dict[str, str] = field(default_factory=dict)


@dataclass(kw_only=True)
class Finding(Entity):
    finding_type: FindingType
    canonical_pattern: str
    title: str
    summary: str
    confidence: FindingConfidence
    analysis_kind: str
    analysis_version: str
    state: FindingState = FindingState.NEW
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    occurrence_count: int = 0
    distinct_session_count: int = 0
    distinct_project_count: int = 0
    affected_harnesses: list[Harness] = field(default_factory=list)
    recommendation: str | None = None
    generated_at: datetime | None = None
    last_evaluated_at: datetime | None = None
    provenance: AnalysisProvenance = field(default_factory=AnalysisProvenance)


@dataclass(kw_only=True)
class FindingEvidence(Entity):
    finding_id: str
    session_id: str
    evidence_type: str
    original_text: str
    message_id: str | None = None
    start_offset: int | None = None
    end_offset: int | None = None
    observed_at: datetime | None = None
    explanation: str | None = None


@dataclass(kw_only=True)
class RuleDraft(Entity):
    text: str
    finding_id: str | None = None
    category: str | None = None
    scope: InstructionScope = InstructionScope.UNKNOWN
    project_id: str | None = None
    target_harnesses: list[Harness] = field(default_factory=list)
    target_instruction_sources: list[str] = field(default_factory=list)
    state: DraftState = DraftState.DRAFT
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(kw_only=True)
class AppliedRule(Entity):
    rule_draft_id: str
    rule_id: str
    applied_at: datetime
    targets: list[str] = field(default_factory=list)
    before_fingerprints: list[str | None] = field(default_factory=list)
    after_fingerprints: list[str | None] = field(default_factory=list)


@dataclass(kw_only=True)
class ProviderConfig(Entity):
    display_name: str
    endpoint: str
    locality: ProviderLocality = ProviderLocality.LOCAL
    protocol: str = "openai_compatible"
    model: str | None = None
    enabled: bool = False


@dataclass(kw_only=True)
class ScanState(Entity):
    source_id: str
    source_session_id: str | None = None
    source_locator: str | None = None
    fingerprint: str | None = None
    checkpoint: str | None = None
    last_scan_at: datetime | None = None
    last_success_at: datetime | None = None
