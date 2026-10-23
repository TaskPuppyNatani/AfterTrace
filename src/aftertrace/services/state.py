"""Application read models; instruction evidence is requested separately, on demand."""

from dataclasses import dataclass, field

from aftertrace.core.models import (
    Finding,
    FindingEvidence,
    Harness,
    Project,
    ProjectPrivacyState,
    Source,
)


@dataclass(frozen=True)
class FindingsSnapshot:
    findings: tuple[Finding, ...] = ()
    eligible_session_count: int = 0


@dataclass(frozen=True)
class EvidenceView:
    evidence: FindingEvidence
    project_name: str
    source_name: str
    harness: Harness
    session_title: str | None


@dataclass(frozen=True)
class FindingDetail:
    finding: Finding
    evidence: tuple[EvidenceView, ...]


@dataclass(frozen=True)
class ProjectView:
    project: Project
    locations: tuple[str, ...]
    harnesses: tuple[Harness, ...]
    discovered: int
    imported: int


@dataclass(frozen=True)
class ProjectPrivacyConflict:
    """Two persisted project records needing an explicit privacy/identity decision."""

    conflict_id: str
    project_ids: tuple[str, str]
    project_names: tuple[str, str]
    privacy_states: tuple[ProjectPrivacyState, ProjectPrivacyState]
    canonical_identities: tuple[str | None, str | None]
    location: str
    match_basis: str
    imported_sessions: tuple[int, int]
    discovered_sessions: tuple[int, int]

    @property
    def can_merge_without_purging(self) -> bool:
        return self.match_basis in {"verified_identity", "canonical_identity"} and set(
            self.privacy_states
        ) == {
            ProjectPrivacyState.INCLUDED,
            ProjectPrivacyState.EXCLUDED,
        }


@dataclass(frozen=True)
class ProjectIdentityWarning:
    """Same-location project records with distinct Git identities; informational only."""

    warning_id: str
    project_ids: tuple[str, str]
    project_names: tuple[str, str]
    privacy_states: tuple[ProjectPrivacyState, ProjectPrivacyState]
    location: str
    imported_sessions: tuple[int, int]
    discovered_sessions: tuple[int, int]


@dataclass(frozen=True)
class SourceView:
    source: Source
    available: bool
    discovered: int
    imported: int
    warning: str | None


@dataclass(frozen=True)
class WorkspaceSnapshot:
    projects: tuple[ProjectView, ...] = ()
    sources: tuple[SourceView, ...] = ()
    unclassified: int = 0
    never_analyze: int = 0
    identity_conflicts: tuple[ProjectPrivacyConflict, ...] = ()
    identity_warnings: tuple[ProjectIdentityWarning, ...] = ()


@dataclass
class ImportResult:
    imported: int = 0
    unchanged: int = 0
    blocked: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        counts = (
            f"{self.imported} imported · {self.unchanged} unchanged · "
            f"{self.failed} failed · {self.blocked} not permitted"
        )
        return counts + (" · " + " ".join(self.failures[:3]) if self.failed else "")
