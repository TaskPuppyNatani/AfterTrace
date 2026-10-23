from collections import defaultdict
from datetime import UTC, datetime
from itertools import permutations

import pytest

from aftertrace.analysis.near_duplicates import (
    NEAR_DUPLICATE_ANALYZER_VERSION,
    NearDuplicateInstructionAnalyzer,
    _lexical_tokens,
    _ordered_similarity,
)
from aftertrace.core.models import Harness, ImportedMessage, Message, MessageRole


def imported_message(content, session, number=0, **kwargs):
    project = kwargs.pop("project_id", "project")
    source = kwargs.pop("source_id", "source")
    harness = kwargs.pop("harness", Harness.CODEX)
    return ImportedMessage(
        message=Message(
            id=f"{session}-m{number}",
            session_id=session,
            sequence=number,
            content=content,
            role=kwargs.pop("role", MessageRole.USER),
            author_source=kwargs.pop("author_source", "user"),
            timestamp=kwargs.pop("timestamp", None),
            **kwargs,
        ),
        project_id=project,
        source_id=source,
        harness=harness,
    )


def analyze(messages):
    return NearDuplicateInstructionAnalyzer().analyze(messages)


def make_pair(left, right, left_sessions=("a1", "a2"), right_sessions=("b1",)):
    messages = []
    numbers = defaultdict(int)
    for text, sessions in ((left, left_sessions), (right, right_sessions)):
        for session in sessions:
            messages.append(imported_message(text, session, number=numbers[session]))
            numbers[session] += 1
    return messages


def test_groups_conservative_wording_variants_and_aggregates_below_threshold_members():
    messages = [
        imported_message("Run focused tests after implementation.", "a1"),
        imported_message("Run focused tests after implementation.", "a2"),
        imported_message("Please run the focused tests when you're done.", "b1"),
        imported_message("Please run the focused tests when you're done.", "b2"),
        imported_message("Make sure you run focused regression tests afterward.", "c1"),
    ]

    (candidate,) = analyze(messages)

    assert candidate.distinct_session_count == 5
    assert candidate.occurrence_count == 5
    assert len(candidate.variants) == 3
    assert candidate.member_variant_keys == tuple(
        sorted(variant.canonical_key for variant in candidate.variants)
    )
    assert candidate.representative_text == "Run focused tests after implementation."
    assert candidate.analyzer_version == NEAR_DUPLICATE_ANALYZER_VERSION
    assert candidate.candidate_id
    assert len(candidate.evidence) == 5
    originals = {message.message.id: message.message.content for message in messages}
    for evidence in candidate.evidence:
        assert (
            evidence.original_text
            == originals[evidence.message_id][evidence.start_offset : evidence.end_offset]
        )
    assert all(score >= 0.8 for _, _, score in candidate.pairwise_similarities)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Run focused tests.", "Run the full test suite."),
        ("Push the branch after implementation.", "Do not push the branch after implementation."),
        ("Modify unrelated files if needed.", "Do not modify unrelated files."),
        ("Use the local model.", "Use the remote model."),
        (
            "Use the local model for project analysis after implementation.",
            "Run the local model for project analysis after implementation.",
        ),
        ("Keep the existing behavior.", "Replace the existing behavior."),
        ("Only modify the requested files.", "Modify all files."),
        ("Run tests after implementation.", "Run tests before implementation."),
    ],
)
def test_meaning_changing_variants_do_not_form_a_cluster(left, right):
    assert analyze(make_pair(left, right, ("a1", "a2"), ("b1", "b2"))) == []


def test_exact_canonical_duplicates_collapse_before_lexical_grouping():
    messages = [
        imported_message("Run focused tests.", "a1"),
        imported_message("RUN FOCUSED TESTS!", "a2"),
        imported_message("Please run focused regression tests.", "b1"),
    ]

    (candidate,) = analyze(messages)

    assert len(candidate.variants) == 2
    assert candidate.occurrence_count == 3
    assert candidate.distinct_session_count == 3
    exact_group = next(
        variant for variant in candidate.variants if variant.distinct_session_count == 2
    )
    assert exact_group.canonical_key == "run focused tests"
    assert len(exact_group.evidence) == 2


def test_one_exact_group_alone_is_not_a_near_duplicate_candidate():
    messages = [imported_message("Run focused tests.", f"s{i}") for i in range(5)]
    assert analyze(messages) == []


def test_cluster_needs_three_distinct_sessions_and_counts_overlap_once():
    assert (
        analyze(
            make_pair(
                "Run focused tests.",
                "Please run focused regression tests.",
                ("shared", "a2"),
                ("shared",),
            )
        )
        == []
    )

    (candidate,) = analyze(
        make_pair(
            "Run focused tests.",
            "Please run focused regression tests.",
            ("shared", "a2"),
            ("shared", "b2"),
        )
    )
    assert candidate.occurrence_count == 4
    assert candidate.distinct_session_count == 3


def test_repeated_occurrences_in_one_session_do_not_meet_support_floor():
    messages = [
        imported_message("Run focused tests.", "a", number=0),
        imported_message("Run focused tests.", "a", number=1),
        imported_message("Please run focused regression tests.", "b"),
    ]
    assert analyze(messages) == []


def test_pairwise_complete_link_prevents_bridge_chain_merging():
    a = "Run focused tests after implementation."
    b = "Run focused tests after implementation reliably."
    c = "Run focused tests after implementation reliably with coverage checks."
    a_tokens, b_tokens, c_tokens = map(_lexical_tokens, (a, b, c))
    assert _ordered_similarity(a_tokens, b_tokens) >= 0.8
    assert _ordered_similarity(b_tokens, c_tokens) >= 0.8
    assert _ordered_similarity(a_tokens, c_tokens) < 0.8
    messages = make_pair(a, b, ("a1", "a2"), ("b1", "b2"))
    messages.extend(
        [
            imported_message(c, "c1"),
            imported_message(c, "c2", number=1),
        ]
    )

    candidates = analyze(messages)

    assert len(candidates) == 1
    assert len(candidates[0].variants) == 2
    assert set(candidates[0].member_variant_keys) == {
        "run focused tests after implementation",
        "run focused tests after implementation reliably",
    }
    assert all(score >= 0.8 for _, _, score in candidates[0].pairwise_similarities)


def test_ordered_similarity_threshold_has_cases_above_and_below_boundary():
    above = make_pair(
        "Run focused tests.",
        "Please run focused regression tests.",
        ("a1", "a2"),
        ("b1",),
    )
    below = make_pair(
        "Run focused tests after implementation.",
        "Run focused tests with coverage checks after implementation.",
        ("a1", "a2"),
        ("b1",),
    )

    above_score = _ordered_similarity(
        _lexical_tokens("Run focused tests."),
        _lexical_tokens("Please run focused regression tests."),
    )
    below_score = _ordered_similarity(
        _lexical_tokens("Run focused tests after implementation."),
        _lexical_tokens("Run focused tests with coverage checks after implementation."),
    )
    assert above_score > 0.8
    assert below_score < 0.8
    assert len(analyze(above)) == 1
    assert analyze(below) == []


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Don't push the branch.", "Do not push the branch."),
        ("Can't use the local model.", "Cannot use the local model."),
        ("Won't push the branch.", "Will not push the branch."),
    ],
)
def test_common_negation_contractions_match_their_expanded_forms(left, right):
    assert _lexical_tokens(left) == _lexical_tokens(right)


def test_m4_extractable_dont_contraction_forms_a_candidate_with_do_not():
    left = "Don't push the branch after implementation."
    right = "Do not push the branch when you're done."
    assert len(analyze(make_pair(left, right, ("a1", "a2"), ("b1",)))) == 1


def test_representative_is_observed_and_prefers_support_then_conciseness():
    longer = "Please run focused regression tests after implementation."
    shorter = "Run focused tests after implementation."
    higher_support = [imported_message(longer, f"a{i}") for i in range(4)] + [
        imported_message(shorter, f"b{i}") for i in range(3)
    ]

    (candidate,) = analyze(higher_support)
    assert candidate.representative_text == longer

    tied_support = [imported_message(longer, f"c{i}") for i in range(3)] + [
        imported_message(shorter, f"d{i}") for i in range(3)
    ]
    (candidate,) = analyze(tied_support)
    assert candidate.representative_text == shorter
    assert candidate.representative_text in {item.message.content for item in tied_support}


def test_results_identity_evidence_and_representative_are_order_independent():
    messages = [
        imported_message(
            "Run focused tests after implementation.",
            "a1",
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        imported_message("Run focused tests after implementation.", "a2"),
        imported_message("Please run the focused tests when you're done.", "b1"),
        imported_message("Make sure you run focused regression tests afterward.", "c1"),
    ]
    expected = analyze(messages)

    assert analyze(list(reversed(messages))) == expected
    assert all(analyze(order) == expected for order in permutations(messages))


@pytest.mark.parametrize(
    "noise",
    ["Thanks.", "Okay.", "Sounds good.", "Please."],
)
def test_existing_instruction_noise_policy_is_reused(noise):
    assert analyze([imported_message(noise, f"s{i}") for i in range(3)]) == []


def test_only_explicit_user_authored_messages_are_analyzed():
    messages = [
        imported_message("Run focused tests after implementation.", "a1"),
        imported_message("Please run the focused tests when you're done.", "b1"),
        imported_message("Make sure you run focused regression tests afterward.", "c1"),
        imported_message(
            "Run focused tests after implementation.", "assistant", role=MessageRole.ASSISTANT
        ),
        imported_message(
            "Run focused tests after implementation.", "injected", author_source="harness"
        ),
    ]
    (candidate,) = analyze(messages)
    assert candidate.distinct_session_count == 3
    assert {item.session_id for item in candidate.evidence} == {"a1", "b1", "c1"}


def test_candidate_aggregates_distributions_and_observed_time_bounds():
    first = datetime(2026, 1, 1, tzinfo=UTC)
    last = datetime(2026, 1, 3, tzinfo=UTC)
    messages = [
        imported_message(
            "Run focused tests.",
            "s1",
            project_id="p1",
            source_id="codex",
            timestamp=first,
        ),
        imported_message(
            "Run focused tests.",
            "s2",
            project_id="p1",
            source_id="codex",
            timestamp=first,
        ),
        imported_message(
            "Please run focused regression tests.",
            "s3",
            project_id="p2",
            source_id="claude",
            harness=Harness.CLAUDE_CODE,
            timestamp=last,
        ),
    ]

    (candidate,) = analyze(messages)

    assert candidate.project_ids == ("p1", "p2")
    assert candidate.distinct_project_count == 2
    assert dict(candidate.harness_distribution) == {
        Harness.CODEX: 2,
        Harness.CLAUDE_CODE: 1,
    }
    assert dict(candidate.source_distribution) == {"claude": 1, "codex": 2}
    assert candidate.first_observed_at == first
    assert candidate.last_observed_at == last
