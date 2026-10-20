"""Explicit full-result publication: detector → defensive gate → synthesis → repository."""

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

from aftertrace.analysis.repetitions import (
    InstructionEvidence,
    RepeatedInstructionAnalyzer,
    RepeatedInstructionCandidate,
    canonicalize_instruction,
)
from aftertrace.core.errors import AfterTraceError
from aftertrace.core.findings import (
    MIN_SUPPORT,
    FindingBundle,
    evidence_identity,
    finding_identity,
    summarize_repetition,
)
from aftertrace.core.models import (
    Finding,
    FindingEvidence,
    FindingType,
    Harness,
    ImportedMessage,
    MessageRole,
)
from aftertrace.services.repository import WorkspaceRepository
from aftertrace.services.state import FindingDetail, FindingsSnapshot


class RepetitionQualityGate:
    """Validate detector output against a fresh, transactionally protected imported snapshot."""

    def validate(
        self, candidate: object, eligible: Mapping[str, ImportedMessage]
    ) -> RepeatedInstructionCandidate:
        try:
            if not isinstance(candidate, RepeatedInstructionCandidate):
                raise ValueError
            key = candidate.canonical_key
            if (
                not isinstance(key, str)
                or not key.strip()
                or key != canonicalize_instruction(key)
                or not isinstance(candidate.representative_text, str)
                or canonicalize_instruction(candidate.representative_text) != key
                or not candidate.evidence
            ):
                raise ValueError
            seen = set()
            spans = {}
            for occurrence in candidate.evidence:
                if not isinstance(occurrence, InstructionEvidence):
                    raise ValueError
                imported = eligible.get(occurrence.message_id)
                if imported is None:
                    raise ValueError
                message = imported.message
                start, end = occurrence.start_offset, occurrence.end_offset
                if (
                    message.role != MessageRole.USER
                    or message.author_source != "user"
                    or type(start) is not int
                    or type(end) is not int
                    or not 0 <= start < end <= len(message.content)
                    or end - start > 400
                    or not all(
                        value.strip()
                        for value in (
                            message.id,
                            message.session_id,
                            imported.project_id,
                            imported.source_id,
                        )
                    )
                    or occurrence.original_text != message.content[start:end]
                    or canonicalize_instruction(occurrence.original_text) != key
                    or occurrence.session_id != message.session_id
                    or occurrence.project_id != imported.project_id
                    or occurrence.source_id != imported.source_id
                    or not isinstance(occurrence.harness, Harness)
                    or occurrence.harness != imported.harness
                    or occurrence.observed_at != message.timestamp
                ):
                    raise ValueError
                identity = (occurrence.message_id, start, end)
                if identity in seen:
                    raise ValueError
                seen.add(identity)
                spans.setdefault(occurrence.message_id, []).append((start, end))
            for intervals in spans.values():
                intervals.sort()
                if any(
                    left[1] > right[0]
                    for left, right in zip(intervals, intervals[1:], strict=False)
                ):
                    raise ValueError
            evidence = candidate.evidence
            sessions = {item.session_id for item in evidence}
            dates = [item.observed_at for item in evidence if item.observed_at is not None]
            if (
                len(sessions) < MIN_SUPPORT
                or type(candidate.occurrence_count) is not int
                or type(candidate.distinct_session_count) is not int
                or candidate.occurrence_count != len(evidence)
                or candidate.distinct_session_count != len(sessions)
                or candidate.project_ids != tuple(sorted({item.project_id for item in evidence}))
                or candidate.harness_distribution
                != tuple(sorted(Counter(item.harness for item in evidence).items()))
                or candidate.source_distribution
                != tuple(sorted(Counter(item.source_id for item in evidence).items()))
                or candidate.first_observed_at != (min(dates) if dates else None)
                or candidate.last_observed_at != (max(dates) if dates else None)
                or candidate.representative_text not in {item.original_text for item in evidence}
            ):
                raise ValueError
            return candidate
        except (ValueError, TypeError, AttributeError):
            raise AfterTraceError(
                "Repeated-instruction candidate failed the evidence quality gate.",
                operation="publish_findings",
            ) from None


def synthesize_repetition(candidate: RepeatedInstructionCandidate, now: datetime) -> FindingBundle:
    finding_id = finding_identity(candidate.canonical_key)
    evidence = tuple(
        FindingEvidence(
            id=evidence_identity(finding_id, item.message_id, item.start_offset, item.end_offset),
            finding_id=finding_id,
            session_id=item.session_id,
            message_id=item.message_id,
            evidence_type="instruction_span",
            original_text=item.original_text,
            start_offset=item.start_offset,
            end_offset=item.end_offset,
            observed_at=item.observed_at,
            metadata={
                "project_id": item.project_id,
                "source_id": item.source_id,
                "harness": item.harness.value,
            },
        )
        for item in candidate.evidence
    )
    return summarize_repetition(candidate.canonical_key, evidence, now)


class FindingPublicationService:
    def __init__(self, repository: WorkspaceRepository) -> None:
        self.repository = repository
        self.gate = RepetitionQualityGate()

    def run(self) -> list[Finding]:
        """Explicit full analysis. Nothing invokes this at startup or after import."""
        return self._publish(None)

    def publish(self, candidates: Iterable[RepeatedInstructionCandidate]) -> list[Finding]:
        """Publish a COMPLETE result set, revalidating cached candidates before any writes.

        Invalid/stale candidates reject the entire transaction. Absent Findings become
        unsupported historical records; callers must not pass a project-filtered subset.
        """
        return self._publish(tuple(candidates))

    def _publish(
        self, candidates: tuple[RepeatedInstructionCandidate, ...] | None
    ) -> list[Finding]:
        def build(messages: list[ImportedMessage]) -> list[FindingBundle]:
            current = candidates
            if current is None:
                current = RepeatedInstructionAnalyzer().analyze(messages)
            eligible = {item.message.id: item for item in messages}
            now = datetime.now(UTC)
            validated = [self.gate.validate(item, eligible) for item in current]
            keys = [item.canonical_key for item in validated]
            if len(set(keys)) != len(keys):
                raise AfterTraceError(
                    "Publication contains duplicate candidate identities.",
                    operation="publish_findings",
                )
            return [synthesize_repetition(item, now) for item in validated]

        return self.repository.publish_repeated_findings(build)


class FindingsService:
    """Thin UI boundary: summary reads, on-demand receipts, and explicit full analysis."""

    def __init__(self, repository: WorkspaceRepository) -> None:
        self.repository = repository
        self.publisher = FindingPublicationService(repository)

    @staticmethod
    def _current(finding: Finding) -> bool:
        return (finding.finding_type == FindingType.REPEATED_INSTRUCTION
                and finding.metadata.get("currently_supported") is True)

    def snapshot(self) -> FindingsSnapshot:
        findings, count = self.repository.finding_overview()
        return FindingsSnapshot(tuple(item for item in findings if self._current(item)), count)

    def detail(self, finding_id: str) -> FindingDetail | None:
        detail = self.repository.finding_detail(finding_id)
        return detail if detail and self._current(detail.finding) else None

    def analyze(self) -> FindingsSnapshot:
        self.publisher.run()
        return self.snapshot()
