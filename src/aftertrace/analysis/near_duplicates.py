"""Conservative lexical near-duplicate candidates over imported user instructions.

This analyzer reuses M4A's instruction extraction and evidence model. It performs no
I/O and produces transient candidates only; callers should pass the same eligible
normalized messages used by :class:`RepeatedInstructionAnalyzer`.
"""

import json
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from uuid import NAMESPACE_URL, uuid5

from aftertrace.analysis.repetitions import InstructionEvidence, extract_instruction_units
from aftertrace.core.errors import AfterTraceError
from aftertrace.core.models import Harness, ImportedMessage, MessageRole

NEAR_DUPLICATE_ANALYZER_VERSION = "near_duplicate_lexical:v1"

_SIMILARITY_THRESHOLD = 0.8
_TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)
_CONTRACTIONS = {
    "ain't": ("not",),
    "aren't": ("are", "not"),
    "can't": ("can", "not"),
    "cannot": ("can", "not"),
    "couldn't": ("could", "not"),
    "didn't": ("did", "not"),
    "doesn't": ("does", "not"),
    "don't": ("do", "not"),
    "hadn't": ("had", "not"),
    "hasn't": ("has", "not"),
    "haven't": ("have", "not"),
    "isn't": ("is", "not"),
    "mustn't": ("must", "not"),
    "needn't": ("need", "not"),
    "shan't": ("shall", "not"),
    "shouldn't": ("should", "not"),
    "wasn't": ("was", "not"),
    "weren't": ("were", "not"),
    "won't": ("will", "not"),
    "wouldn't": ("would", "not"),
    "you're": ("you", "are"),
    "we're": ("we", "are"),
    "they're": ("they", "are"),
    "i'm": ("i", "am"),
    "we've": ("we", "have"),
    "they've": ("they", "have"),
    "you've": ("you", "have"),
    "we'll": ("we", "will"),
    "you'll": ("you", "will"),
    "they'll": ("they", "will"),
}
_POLITE_PREFIXES = (
    ("make", "sure", "you"),
    ("make", "sure"),
    ("could", "you"),
    ("would", "you"),
    ("can", "you"),
    ("please",),
)
_COMPLETION_SUFFIXES = (
    ("when", "you", "are", "finished"),
    ("when", "you", "are", "done"),
    ("when", "you", "finish"),
    ("when", "finished"),
    ("when", "done"),
    ("when", "complete"),
    ("afterward",),
    ("afterwards",),
)
_NEGATION = frozenset({"not", "no", "never"})
_ACTION_PREFIX_WORDS = frozenset(
    {
        "always",
        "can",
        "could",
        "did",
        "do",
        "does",
        "may",
        "might",
        "must",
        "never",
        "no",
        "not",
        "only",
        "shall",
        "should",
        "will",
        "would",
    }
)
# These small contrast sets guard modifiers that materially change instruction scope.
_MEANING_MARKERS = (
    frozenset({"focused", "full"}),
    frozenset({"local", "remote"}),
    frozenset({"only", "all", "any"}),
    frozenset({"before", "after"}),
    frozenset({"allow", "deny"}),
    frozenset({"keep", "replace"}),
)


@dataclass(frozen=True, kw_only=True)
class NearDuplicateInstructionVariant:
    """One M4A exact-canonical group, retained as a member of a lexical cluster."""

    canonical_key: str
    lexical_key: str
    representative_text: str
    original_variants: tuple[str, ...]
    occurrence_count: int
    distinct_session_count: int
    evidence: tuple[InstructionEvidence, ...]


@dataclass(frozen=True, kw_only=True)
class NearDuplicateInstructionCandidate:
    """Transient evidence-backed candidate; it is not a persisted Finding."""

    candidate_id: str
    analyzer_version: str
    representative_text: str
    variants: tuple[NearDuplicateInstructionVariant, ...]
    occurrence_count: int
    distinct_session_count: int
    project_ids: tuple[str, ...]
    harness_distribution: tuple[tuple[Harness, int], ...]
    source_distribution: tuple[tuple[str, int], ...]
    first_observed_at: datetime | None
    last_observed_at: datetime | None
    pairwise_similarities: tuple[tuple[str, str, float], ...]

    @property
    def member_variant_keys(self) -> tuple[str, ...]:
        return tuple(variant.canonical_key for variant in self.variants)

    @property
    def member_lexical_keys(self) -> tuple[str, ...]:
        return tuple(variant.lexical_key for variant in self.variants)

    @property
    def original_variants(self) -> tuple[str, ...]:
        return tuple(
            original for variant in self.variants for original in variant.original_variants
        )

    @property
    def distinct_project_count(self) -> int:
        return len(self.project_ids)

    @property
    def evidence(self) -> tuple[InstructionEvidence, ...]:
        return tuple(
            sorted(
                (item for variant in self.variants for item in variant.evidence),
                key=lambda item: (
                    item.session_id,
                    item.message_id,
                    item.start_offset,
                    item.end_offset,
                    item.original_text,
                ),
            )
        )


def _lexical_tokens(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFC", text).casefold().replace("’", "'")
    expanded = []
    for token in _TOKEN.findall(normalized):
        expanded.extend(_CONTRACTIONS.get(token, (token,)))

    tokens = list(expanded)
    changed = True
    while changed:
        changed = False
        for prefix in _POLITE_PREFIXES:
            if tuple(tokens[: len(prefix)]) == prefix:
                del tokens[: len(prefix)]
                changed = True
                break

    for suffix in _COMPLETION_SUFFIXES:
        if tuple(tokens[-len(suffix) :]) == suffix:
            tokens = tokens[: -len(suffix)] + ["after"]
            break
    return tuple(tokens)


def _comparison_score(left: tuple[str, ...], right: tuple[str, ...]) -> float | None:
    if not left or not right:
        return None
    left_set = set(left)
    right_set = set(right)
    if len(left_set & right_set) < 2:
        return None
    if bool(left_set & _NEGATION) != bool(right_set & _NEGATION):
        return None
    if any((left_set & markers) != (right_set & markers) for markers in _MEANING_MARKERS):
        return None
    left_action = next((token for token in left if token not in _ACTION_PREFIX_WORDS), None)
    right_action = next((token for token in right if token not in _ACTION_PREFIX_WORDS), None)
    if left_action != right_action:
        return None

    score = _ordered_similarity(left, right)
    return score if score >= _SIMILARITY_THRESHOLD else None


def _ordered_similarity(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def _surface_support_key(
    text: str,
    surface_sessions: dict[str, set[str]],
    surface_occurrences: Counter[str],
):
    normalized = unicodedata.normalize("NFC", text)
    return (
        -len(surface_sessions[text]),
        -surface_occurrences[text],
        len(text),
        normalized.casefold(),
        normalized,
        text,
    )


def _make_variant(
    canonical_key: str, occurrences: list[InstructionEvidence]
) -> tuple[NearDuplicateInstructionVariant, tuple[str, ...]]:
    evidence = tuple(
        sorted(
            occurrences,
            key=lambda item: (
                item.session_id,
                item.message_id,
                item.start_offset,
                item.end_offset,
                item.original_text,
            ),
        )
    )
    _surface_sessions: defaultdict[str, set[str]] = defaultdict(set)
    _surface_occurrences: Counter[str] = Counter()
    for item in evidence:
        _surface_sessions[item.original_text].add(item.session_id)
        _surface_occurrences[item.original_text] += 1
    original_variants = tuple(
        sorted(
            {item.original_text for item in evidence},
            key=lambda text: _surface_support_key(text, _surface_sessions, _surface_occurrences),
        )
    )
    representative_text = original_variants[0]
    tokens = _lexical_tokens(canonical_key)
    variant = NearDuplicateInstructionVariant(
        canonical_key=canonical_key,
        lexical_key=" ".join(tokens),
        representative_text=representative_text,
        original_variants=original_variants,
        occurrence_count=len(evidence),
        distinct_session_count=len({item.session_id for item in evidence}),
        evidence=evidence,
    )
    return variant, tokens


def _timestamp_bounds(evidence: tuple[InstructionEvidence, ...]):
    timestamps = [item.observed_at for item in evidence if item.observed_at is not None]
    try:
        return (min(timestamps), max(timestamps)) if timestamps else (None, None)
    except TypeError:
        raise AfterTraceError(
            "Imported timestamps have inconsistent timezone information.",
            operation="analyze_near_duplicates",
        ) from None


class NearDuplicateInstructionAnalyzer:
    """Group conservative lexical variants with at least three distinct sessions."""

    def analyze(
        self, messages: Iterable[ImportedMessage]
    ) -> list[NearDuplicateInstructionCandidate]:
        exact_groups: dict[str, list[InstructionEvidence]] = defaultdict(list)
        seen_message_ids = set()
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
                    "Imported message context is incomplete.",
                    operation="analyze_near_duplicates",
                )
            if message.id in seen_message_ids:
                raise AfterTraceError(
                    "Imported message identity is duplicated.",
                    operation="analyze_near_duplicates",
                )
            seen_message_ids.add(message.id)
            for unit in extract_instruction_units(message.content):
                exact_groups[unit.canonical_key].append(
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

        variants = [_make_variant(key, exact_groups[key]) for key in sorted(exact_groups)]
        clusters: list[list[tuple[NearDuplicateInstructionVariant, tuple[str, ...]]]] = []
        for item in variants:
            for cluster in clusters:
                if all(_comparison_score(item[1], member[1]) is not None for member in cluster):
                    cluster.append(item)
                    break
            else:
                clusters.append([item])

        candidates = []
        for cluster in clusters:
            if len(cluster) < 2:
                continue
            members = tuple(item[0] for item in cluster)
            evidence = tuple(item for member in members for item in member.evidence)
            distinct_sessions = {item.session_id for item in evidence}
            if len(distinct_sessions) < 3:
                continue

            pairwise = []
            for index, left in enumerate(cluster):
                for right in cluster[index + 1 :]:
                    score = _comparison_score(left[1], right[1])
                    if score is None:  # Complete-link membership was checked above.
                        raise AfterTraceError(
                            "Near-duplicate cluster compatibility changed during analysis.",
                            operation="analyze_near_duplicates",
                        )
                    pairwise.append((left[0].canonical_key, right[0].canonical_key, score))

            representative_variant = min(
                members,
                key=lambda item: (
                    -item.distinct_session_count,
                    -item.occurrence_count,
                    len(item.representative_text),
                    item.lexical_key,
                    item.canonical_key,
                ),
            )
            first, last = _timestamp_bounds(evidence)
            project_ids = tuple(sorted({item.project_id for item in evidence}))
            candidates.append(
                NearDuplicateInstructionCandidate(
                    candidate_id=str(
                        uuid5(
                            NAMESPACE_URL,
                            "aftertrace:"
                            + NEAR_DUPLICATE_ANALYZER_VERSION
                            + ":"
                            + json.dumps(
                                [item.canonical_key for item in members],
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        )
                    ),
                    analyzer_version=NEAR_DUPLICATE_ANALYZER_VERSION,
                    representative_text=representative_variant.representative_text,
                    variants=members,
                    occurrence_count=len(evidence),
                    distinct_session_count=len(distinct_sessions),
                    project_ids=project_ids,
                    harness_distribution=tuple(
                        sorted(Counter(item.harness for item in evidence).items())
                    ),
                    source_distribution=tuple(
                        sorted(Counter(item.source_id for item in evidence).items())
                    ),
                    first_observed_at=first,
                    last_observed_at=last,
                    pairwise_similarities=tuple(pairwise),
                )
            )

        return sorted(
            candidates,
            key=lambda item: (
                -item.distinct_session_count,
                -item.occurrence_count,
                item.member_variant_keys,
                item.candidate_id,
            ),
        )


def cluster_near_duplicate_instructions(
    messages: Iterable[ImportedMessage],
) -> list[NearDuplicateInstructionCandidate]:
    """Convenience function matching the other pure analyzers in this package."""
    return NearDuplicateInstructionAnalyzer().analyze(messages)
