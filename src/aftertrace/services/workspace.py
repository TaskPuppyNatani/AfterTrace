"""Discover → identify → durable user scope → explicit, policy-gated import."""

from collections.abc import Callable
from pathlib import Path

from aftertrace.adapters.base import HarnessAdapter
from aftertrace.adapters.codex import CodexAdapter
from aftertrace.core.errors import AfterTraceError
from aftertrace.core.models import ProjectPrivacyState
from aftertrace.platform.paths import database_path
from aftertrace.platform.projects import ProjectIdentity, identify_project, normalize_location
from aftertrace.services.repository import WorkspaceRepository
from aftertrace.services.state import ImportResult, WorkspaceSnapshot


class WorkspaceService:
    def __init__(
        self,
        path: Path | None = None,
        *,
        adapter: HarnessAdapter | None = None,
        identify: Callable[[str | None], ProjectIdentity | None] = identify_project,
    ) -> None:
        self.repository = WorkspaceRepository(path if path is not None else database_path())
        self.adapter = adapter if adapter is not None else CodexAdapter()
        self.identify = identify

    def snapshot(self) -> WorkspaceSnapshot:
        return self.repository.snapshot()

    def discover(self) -> str:
        try:
            candidates = self.adapter.detect()
        except Exception:
            raise AfterTraceError(
                "Codex discovery failed. You can retry.", operation="discover", source="codex"
            ) from None
        sources = [self.repository.source_for(candidate) for candidate in candidates]
        self.repository.mark_missing_sources({source.id for source in sources})
        identities = {}
        warnings = 0
        count = 0
        for source in sources:
            try:
                references = self.adapter.discover_sessions(source)
                entries = []
                for reference in references:
                    hint = reference.project_hint
                    if hint not in identities:
                        identities[hint] = self.identify(hint)
                    entries.append((reference, identities[hint]))
            except Exception:
                self.repository.discovery_failed(source)
                warnings += 1
                continue
            self.repository.record_discovery(source, entries)
            count += len(entries)
        return (
            f"{len(sources)} Codex sources · {count} sessions discovered · "
            f"{warnings} discovery warnings. No content imported."
        )

    def set_privacy(self, project_id: str, state: ProjectPrivacyState) -> str:
        self.repository.set_privacy(project_id, state)
        return "Scope saved. Import is explicit. Refresh discovery after lifting Never Analyze."

    def import_included(self) -> ImportResult:
        result = ImportResult(blocked=self.snapshot().never_analyze)

        def build(reference, project, previous_fingerprint):
            # Reconfirm the repository identity before loading any transcript. The persisted
            # reference/project mapping is authoritative; callers cannot nominate another project.
            identity = self.identify(reference.project_hint)
            if identity is None or identity.canonical_identity != project.canonical_identity:
                raise AfterTraceError(
                    "Project identity changed; discover again.", operation="import_session"
                )
            raw = self.adapter.load_raw_session(reference)
            if raw.reference.fingerprint_hint != reference.fingerprint_hint:
                raise AfterTraceError(
                    "Session changed; discover again before importing.", operation="import_session"
                )
            if (
                previous_fingerprint is not None
                and previous_fingerprint == raw.reference.fingerprint_hint
            ):
                return None
            session = self.adapter.normalize_session(raw)
            if normalize_location(session.working_directory) != normalize_location(
                reference.project_hint
            ):
                raise AfterTraceError(
                    "Session project changed; discover again.", operation="import_session"
                )
            return session

        for reference_id in self.repository.reference_ids():
            try:
                outcome = self.repository.import_reference(reference_id, build)
            except Exception:
                # No native error strings, raw records, prompts, or tool bodies cross into UI/logs.
                result.failed += 1
                try:
                    label = self.repository.import_failure_label(reference_id)
                except Exception:
                    label = reference_id
                result.failures.append(
                    f"import_session: {label} failed. Refresh discovery and retry."
                )
                continue
            setattr(result, outcome, getattr(result, outcome) + 1)
        return result
