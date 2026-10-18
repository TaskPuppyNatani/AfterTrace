"""Application state passed to clients; contains counts/metadata, never transcripts."""

from dataclasses import dataclass, field

from aftertrace.core.models import Harness, Project, Source


@dataclass(frozen=True)
class ProjectView:
    project: Project
    locations: tuple[str, ...]
    harnesses: tuple[Harness, ...]
    discovered: int
    imported: int


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
