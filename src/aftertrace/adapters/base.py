"""Read-only harness boundary. Native payloads stop here."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from aftertrace.core.errors import UnsupportedOperationError
from aftertrace.core.models import Harness, Session, SessionReference, Source


@dataclass(frozen=True)
class SourceCandidate:
    harness: Harness
    display_name: str
    location: str


@dataclass(frozen=True)
class RawSession:
    reference: SessionReference
    payload: bytes


@runtime_checkable
class HarnessAdapter(Protocol):
    source_name: str

    def detect(self) -> list[SourceCandidate]: ...
    def discover_sessions(self, source: Source) -> list[SessionReference]: ...
    def load_raw_session(self, reference: SessionReference) -> RawSession: ...
    def normalize_session(self, raw: RawSession) -> Session: ...


class UnimplementedHarnessAdapter:
    """Explicit foundation stub; never probes or reads source histories."""

    source_name: str

    def _unsupported(self, operation: str) -> UnsupportedOperationError:
        return UnsupportedOperationError(
            f"{self.source_name} history support is not implemented yet.",
            operation=operation,
            source=self.source_name,
        )

    def detect(self) -> list[SourceCandidate]:
        raise self._unsupported("detect")

    def discover_sessions(self, source: Source) -> list[SessionReference]:
        raise self._unsupported("discover_sessions")

    def load_raw_session(self, reference: SessionReference) -> RawSession:
        raise self._unsupported("load_raw_session")

    def normalize_session(self, raw: RawSession) -> Session:
        raise self._unsupported("normalize_session")
