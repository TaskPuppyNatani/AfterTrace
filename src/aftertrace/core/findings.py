"""Pure synthesis and evidence-strength policy for exact repeated-instruction Findings."""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from aftertrace.core.errors import AfterTraceError
from aftertrace.core.models import (
    AnalysisProvenance,
    Finding,
    FindingConfidence,
    FindingEvidence,
    FindingType,
    Harness,
)

ANALYSIS_KIND = "exact_repeated_instruction"
ANALYSIS_VERSION = "repeated_instruction:v1"
MIN_SUPPORT = 3
MEDIUM_SUPPORT = 5
HIGH_SUPPORT = 10


@dataclass(frozen=True)
class FindingBundle:
    finding: Finding
    evidence: tuple[FindingEvidence, ...]


def finding_identity(canonical_key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"finding:repeated_instruction:{canonical_key}"))


def evidence_identity(finding_id: str, message_id: str, start: int, end: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"evidence:{finding_id}:{message_id}:{start}:{end}"))


def confidence_for_sessions(count: int) -> FindingConfidence:
    """Provisional deterministic evidence strength, never a calibrated probability."""
    if count >= HIGH_SUPPORT:
        return FindingConfidence.HIGH
    if count >= MEDIUM_SUPPORT:
        return FindingConfidence.MEDIUM
    return FindingConfidence.LOW


def summarize_repetition(
    canonical_key: str,
    evidence: tuple[FindingEvidence, ...],
    now: datetime,
    *,
    previous: Finding | None = None,
    preserve_first_seen: bool = True,
) -> FindingBundle:
    """Recompute every current aggregate from receipts; retain only explicit history.

    Metadata holds current distributions/support and content-free project lineage.
    Lineage survives evidence synchronization so later privacy removal can still find
    unsupported historical Findings. Privacy refreshes disable first-seen retention.
    """
    evidence = tuple(
        sorted(
            evidence,
            key=lambda e: (
                e.session_id,
                e.message_id or "",
                e.start_offset or 0,
                e.end_offset or 0,
            ),
        )
    )
    sessions = {e.session_id for e in evidence}
    projects = sorted({e.metadata["project_id"] for e in evidence})
    harnesses = Counter(e.metadata["harness"] for e in evidence)
    sources = Counter(e.metadata["source_id"] for e in evidence)
    timestamps = [e.observed_at for e in evidence if e.observed_at is not None]
    try:
        first = min(timestamps) if timestamps else None
        last = max(timestamps) if timestamps else None
        if previous and preserve_first_seen:
            known = [value for value in (previous.first_seen_at, first) if value is not None]
            first = min(known) if known else None
            if not evidence:
                last = previous.last_seen_at
    except TypeError:
        raise AfterTraceError(
            "Finding timestamps have inconsistent timezone information.",
            operation="synthesize_finding",
        ) from None
    origins = set(previous.metadata.get("origin_project_ids", [])) if previous else set()
    origins.update(projects)
    finding = Finding(
        id=previous.id if previous else finding_identity(canonical_key),
        finding_type=FindingType.REPEATED_INSTRUCTION,
        canonical_pattern=canonical_key,
        title=evidence[0].original_text if evidence else previous.title,
        summary=f"{len(evidence)} occurrences across {len(sessions)} sessions and "
        f"{len(projects)} projects.",
        confidence=confidence_for_sessions(len(sessions)),
        analysis_kind=ANALYSIS_KIND,
        analysis_version=ANALYSIS_VERSION,
        first_seen_at=first,
        last_seen_at=last,
        occurrence_count=len(evidence),
        distinct_session_count=len(sessions),
        distinct_project_count=len(projects),
        affected_harnesses=[Harness(value) for value in sorted(harnesses)],
        generated_at=previous.generated_at if previous else now,
        last_evaluated_at=now,
        provenance=AnalysisProvenance(
            deterministic_steps=["canonical exact repetition", "evidence quality gate"],
            analyzer_versions={"repeated_instruction": "v1"},
        ),
        metadata={
            "currently_supported": len(sessions) >= MIN_SUPPORT,
            "project_ids": projects,
            "harness_distribution": dict(sorted(harnesses.items())),
            "source_distribution": dict(sorted(sources.items())),
            "origin_project_ids": sorted(origins),
        },
    )
    if previous:
        finding.state = previous.state
    return FindingBundle(finding, evidence)
