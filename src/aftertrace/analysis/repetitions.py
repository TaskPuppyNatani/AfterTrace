"""Exact/canonical candidates over privacy-scoped, imported normalized messages.

Call ``analyze(repository.imported_messages_for_analysis())`` explicitly. This module
does no I/O and produces no Findings. Its deliberately small English instruction
heuristic favors false negatives. Paragraphs/list items retain conditions and wording;
we do not split sentences, infer intent, or merge different wording.
"""

import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from aftertrace.core.errors import AfterTraceError
from aftertrace.core.models import Harness, ImportedMessage, MessageRole

_LIST_MARKER = re.compile(r"\s*(?:[-*+]|\d+[.)])\s+")
_HEADING = re.compile(r"^\s*#{1,6}\s+", re.MULTILINE)
_MARKUP_BLOCK = re.compile(r"^\s*</?[A-Za-z][^>]*>\s*$", re.MULTILINE)
_DOCUMENT_LABEL = re.compile(
    r"^\s*(?:issue|specification|acceptance criteria|steps to reproduce)\s*:",
    re.IGNORECASE | re.MULTILINE,
)
_INSTRUCTION_START = re.compile(
    r"^(?:(?:please|always)\s+)?"
    r"(?:run|use|keep|preserve|avoid|prefer|ensure|verify|check|test|add|write|read|"
    r"review|report|include|exclude|limit|only|never|do not|don't|make|leave|ask|"
    r"stop|follow)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, kw_only=True)
class InstructionUnit:
    canonical_key: str
    original_text: str
    start_offset: int
    end_offset: int


@dataclass(frozen=True, kw_only=True)
class InstructionEvidence:
    session_id: str
    message_id: str
    project_id: str
    source_id: str
    harness: Harness
    observed_at: datetime | None
    original_text: str
    start_offset: int
    end_offset: int


@dataclass(frozen=True, kw_only=True)
class RepeatedInstructionCandidate:
    canonical_key: str
    representative_text: str
    occurrence_count: int
    distinct_session_count: int
    project_ids: tuple[str, ...]
    # Distribution counts are raw occurrences, not distinct supporting sessions.
    harness_distribution: tuple[tuple[Harness, int], ...]
    source_distribution: tuple[tuple[str, int], ...]
    first_observed_at: datetime | None
    last_observed_at: datetime | None
    evidence: tuple[InstructionEvidence, ...]

    @property
    def distinct_project_count(self) -> int:
        return len(self.project_ids)


def canonicalize_instruction(text: str) -> str:
    """NFC, lowercase, whitespace collapse, and one terminal period/exclamation.

    Questions, ellipses, internal punctuation, and different wording stay distinct.
    NFC avoids compatibility folding of symbols that may carry different meanings.
    """
    key = " ".join(unicodedata.normalize("NFC", text).lower().split())
    if key.endswith((".", "!")) and key[-2:-1] not in (".", "!", "?"):
        key = key[:-1].rstrip()
    return key


def extract_instruction_units(content: str) -> tuple[InstructionUnit, ...]:
    """Extract bounded instruction paragraphs/list items with exact source spans.

    Headed/large/structured pasted documents are skipped conservatively. Fenced
    code, quoted text, and indented code are not user instructions. A small imperative
    prefix rule plus a three-word minimum excludes conversational filler and lone
    URLs, paths, filenames, and identifiers without maintaining a blacklist.
    """
    if (
        len(content) > 4000
        or len(content.splitlines()) > 40
        or _HEADING.search(content)
        or _MARKUP_BLOCK.search(content)
        or _DOCUMENT_LABEL.search(content)
    ):
        return ()

    units = []
    start = end = None
    fence = None

    def flush():
        nonlocal start, end
        if start is not None:
            original = content[start:end]
            key = canonicalize_instruction(original)
            if (
                len(original) <= 400
                and len(key.split()) >= 3
                and _INSTRUCTION_START.match(key)
                and not original.endswith(("?", ":"))
            ):
                units.append(
                    InstructionUnit(
                        canonical_key=key,
                        original_text=original,
                        start_offset=start,
                        end_offset=end,
                    )
                )
        start = end = None

    offset = 0
    for line in content.splitlines(keepends=True):
        stripped = line.strip()
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if marker:
            flush()
            delimiter = marker[0]
            if fence is None:
                fence = delimiter
            elif delimiter[0] == fence[0] and len(delimiter) >= len(fence):
                fence = None
        elif fence is not None:
            pass
        elif not stripped or stripped.startswith(">"):
            flush()
        else:
            bullet = _LIST_MARKER.match(line)
            if (line.startswith("    ") or line.startswith("\t")) and not bullet:
                flush()
            else:
                # Keep wrapped paragraphs intact so a following condition is not lost.
                if bullet:
                    flush()
                line_start = offset + (bullet.end() if bullet else len(line) - len(line.lstrip()))
                line_end = offset + len(line.rstrip())
                if start is None:
                    start = line_start
                end = line_end
        offset += len(line)
    flush()
    return tuple(units)


class RepeatedInstructionAnalyzer:
    """Group canonical keys, requiring at least three distinct normalized sessions."""

    def analyze(self, messages: Iterable[ImportedMessage]) -> list[RepeatedInstructionCandidate]:
        grouped = defaultdict(list)
        seen = set()
        for imported in messages:
            message = imported.message
            if message.role != MessageRole.USER or message.author_source != "user":
                continue
            if not all(
                isinstance(value, str) and value.strip()
                for value in (
                    message.id,
                    message.session_id,
                    imported.project_id,
                    imported.source_id,
                )
            ):
                raise AfterTraceError(
                    "Imported message context is incomplete.", operation="analyze_repetitions"
                )
            if message.id in seen:
                raise AfterTraceError(
                    "Imported message identity is duplicated.", operation="analyze_repetitions"
                )
            seen.add(message.id)
            for unit in extract_instruction_units(message.content):
                grouped[unit.canonical_key].append(
                    InstructionEvidence(
                        session_id=message.session_id,
                        message_id=message.id,
                        project_id=imported.project_id,
                        source_id=imported.source_id,
                        harness=imported.harness,
                        observed_at=message.timestamp,
                        original_text=unit.original_text,
                        start_offset=unit.start_offset,
                        end_offset=unit.end_offset,
                    )
                )

        candidates = []
        for key, occurrences in grouped.items():
            sessions = {item.session_id for item in occurrences}
            if len(sessions) < 3:
                continue
            evidence = tuple(
                sorted(
                    occurrences,
                    key=lambda item: (
                        item.session_id,
                        item.message_id,
                        item.start_offset,
                        item.end_offset,
                    ),
                )
            )
            timestamps = [item.observed_at for item in evidence if item.observed_at is not None]
            try:
                first = min(timestamps) if timestamps else None
                last = max(timestamps) if timestamps else None
            except TypeError:
                raise AfterTraceError(
                    "Imported timestamps have inconsistent timezone information.",
                    operation="analyze_repetitions",
                ) from None
            candidates.append(
                RepeatedInstructionCandidate(
                    canonical_key=key,
                    representative_text=evidence[0].original_text,
                    occurrence_count=len(evidence),
                    distinct_session_count=len(sessions),
                    project_ids=tuple(sorted({item.project_id for item in evidence})),
                    harness_distribution=tuple(
                        sorted(Counter(item.harness for item in evidence).items())
                    ),
                    source_distribution=tuple(
                        sorted(Counter(item.source_id for item in evidence).items())
                    ),
                    first_observed_at=first,
                    last_observed_at=last,
                    evidence=evidence,
                )
            )
        # Explicit ordering independent of input iteration and SQLite row order.
        return sorted(
            candidates,
            key=lambda item: (
                -item.distinct_session_count,
                -item.occurrence_count,
                item.canonical_key,
            ),
        )
