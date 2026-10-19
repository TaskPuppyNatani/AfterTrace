from datetime import UTC, datetime, timedelta, timezone

import pytest

from aftertrace.analysis.repetitions import (
    RepeatedInstructionAnalyzer,
    canonicalize_instruction,
    extract_instruction_units,
)
from aftertrace.core.errors import AfterTraceError
from aftertrace.core.models import Harness, ImportedMessage, Message, MessageRole


def imported_message(content="Run the focused tests.", session="s1", number=0, **kwargs):
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
            **kwargs,
        ),
        project_id=project,
        source_id=source,
        harness=harness,
    )


def analyze(messages):
    return RepeatedInstructionAnalyzer().analyze(messages)


def test_standalone_instruction_preserves_exact_evidence_span():
    content = "  Run   the focused tests!  \r\n"
    (unit,) = extract_instruction_units(content)
    assert unit.canonical_key == "run the focused tests"
    assert unit.original_text == "Run   the focused tests!"
    assert content[unit.start_offset : unit.end_offset] == unit.original_text


def test_multiple_instructions_prose_lists_and_continuations():
    content = (
        "Some task context.\r\n\r\n"
        "- Run the focused tests.\r\n"
        "2. Keep changes small\r\n  unless the requested behavior requires more.\r\n"
        "\r\nPreserve original evidence.\r\n\r\nDo not push changes."
    )
    units = extract_instruction_units(content)
    assert [unit.canonical_key for unit in units] == [
        "run the focused tests",
        "keep changes small unless the requested behavior requires more",
        "preserve original evidence",
        "do not push changes",
    ]
    for unit in units:
        assert content[unit.start_offset : unit.end_offset] == unit.original_text


@pytest.mark.parametrize("separator", [" ", "\n"])
def test_sentence_conditions_are_not_detached_or_rewritten(separator):
    content = f"Run the focused tests.{separator}Only after reviewing the diff."
    (unit,) = extract_instruction_units(content)
    assert unit.original_text == content


@pytest.mark.parametrize(
    "variant",
    [
        "Run the focused tests.",
        "run   the focused tests",
        "RUN THE FOCUSED TESTS!",
        "  Run\r\n the\tfocused tests. ",
    ],
)
def test_canonical_superficial_variations(variant):
    assert canonicalize_instruction(variant) == "run the focused tests"


def test_unicode_canonical_equivalence_without_compatibility_folding():
    assert canonicalize_instruction("Use cafe\u0301 examples.") == "use café examples"
    assert canonicalize_instruction("Use ① example.") != canonicalize_instruction("Use 1 example.")


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Run the focused tests.", "Run the full test suite."),
        ("Do not modify unrelated files.", "Do not delete unrelated files."),
        ("Run the focused tests.", "Do not run the focused tests."),
        ("Use --force only locally.", "Use force only locally."),
        ("Run the tests.", "Run the tests?"),
        ("Run the tests.", "Run the tests..."),
    ],
)
def test_meaningful_wording_and_punctuation_are_distinct(left, right):
    assert canonicalize_instruction(left) != canonicalize_instruction(right)


@pytest.mark.parametrize(
    "noise",
    [
        "thanks",
        "okay",
        "yes",
        "sounds good",
        "Hello there",
        "Goodbye for now",
        "How are you?",
        "Do you have time?",
        "That looks good to me.",
        "Run",
        "Use pytest",
        "README.md",
        "/home/example/README.md",
        "C:\\example\\README.md",
        "src/example.py",
        "https://example.com",
        "TASK-12345",
        "branch/example-42",
        "# Pasted specification\n- Run the focused tests.",
        "Issue: Synthetic pasted issue\n- Run the focused tests.",
        "Specification: Copied requirements\nRun the focused tests.",
        "<instructions>\nRun the focused tests.\n</instructions>",
        "```text\nRun the focused tests.\n```",
        "~~~text\nRun the focused tests.\n~~~",
        "> Run the focused tests.",
        "    Run the focused tests.",
        "Run " + "word " * 1000,
        "Run " + "word " * 100,
        "Run the focused tests.\n" * 41,
    ],
)
def test_noise_does_not_produce_candidates(noise):
    assert extract_instruction_units(noise) == ()
    assert analyze([imported_message(noise, f"s{i}") for i in range(3)]) == []


def test_code_quotes_and_prose_do_not_hide_a_separate_reusable_instruction():
    content = (
        "Example follows:\n\n```\nRun the full test suite.\n```\n"
        "> Do not modify unrelated files.\n\n- Run the focused tests."
    )
    assert [unit.original_text for unit in extract_instruction_units(content)] == [
        "Run the focused tests."
    ]


@pytest.mark.parametrize("role", list(MessageRole))
@pytest.mark.parametrize("author", ["user", None, "unknown", "harness", "system", "developer"])
def test_only_explicit_user_role_and_authorship_are_eligible(role, author):
    messages = [
        imported_message(session=f"s{i}", role=role, author_source=author) for i in range(3)
    ]
    assert bool(analyze(messages)) == (role is MessageRole.USER and author == "user")


@pytest.mark.parametrize(
    ("counts", "qualifies"),
    [
        ([1, 1, 1], True),
        ([8], False),
        ([4, 1], False),
        ([3, 1, 1], True),
    ],
)
def test_distinct_session_threshold_separate_from_occurrences(counts, qualifies):
    messages = [
        imported_message(session=f"s{session}", number=number)
        for session, count in enumerate(counts)
        for number in range(count)
    ]
    candidates = analyze(messages)
    assert bool(candidates) is qualifies
    if qualifies:
        (candidate,) = candidates
        assert candidate.occurrence_count == sum(counts)
        assert candidate.distinct_session_count == len(counts)


def test_duplicates_in_one_message_are_occurrences_but_only_one_session():
    repeated = "- Run the focused tests.\n" * 8
    assert analyze([imported_message(repeated)]) == []
    (candidate,) = analyze(
        [
            imported_message(repeated),
            imported_message(session="s2"),
            imported_message(session="s3"),
        ]
    )
    assert candidate.occurrence_count == 10
    assert candidate.distinct_session_count == 3
    assert len({(e.message_id, e.start_offset) for e in candidate.evidence}) == 10


def test_distribution_timestamps_and_original_traceable_evidence():
    first = datetime(2026, 1, 1, tzinfo=UTC)
    last = datetime(2026, 1, 3, tzinfo=timezone(timedelta(hours=-8)))
    messages = [
        imported_message("RUN   THE FOCUSED TESTS!", "a", timestamp=last),
        imported_message(session="a", number=1, timestamp=first),
        imported_message(session="a", number=2),
        imported_message(
            session="b",
            project_id="other",
            source_id="claude",
            harness=Harness.CLAUDE_CODE,
            timestamp=first + timedelta(days=1),
        ),
        imported_message(
            session="c", project_id="other", source_id="open", harness=Harness.OPENCODE
        ),
    ]
    (candidate,) = analyze(messages)
    assert candidate.occurrence_count == 5
    assert candidate.distinct_session_count == 3
    assert candidate.project_ids == ("other", "project")
    assert candidate.distinct_project_count == 2
    assert dict(candidate.harness_distribution) == {
        Harness.CODEX: 3,
        Harness.CLAUDE_CODE: 1,
        Harness.OPENCODE: 1,
    }
    assert dict(candidate.source_distribution) == {"source": 3, "claude": 1, "open": 1}
    assert candidate.first_observed_at == first
    assert candidate.last_observed_at == last
    assert candidate.representative_text == "RUN   THE FOCUSED TESTS!"
    by_id = {imported.message.id: imported for imported in messages}
    assert len(candidate.evidence) == 5
    for evidence in candidate.evidence:
        imported = by_id[evidence.message_id]
        message = imported.message
        assert evidence.session_id == message.session_id
        assert evidence.project_id == imported.project_id
        assert evidence.source_id == imported.source_id
        assert evidence.harness == imported.harness
        assert evidence.observed_at == message.timestamp
        assert (
            message.content[evidence.start_offset : evidence.end_offset] == evidence.original_text
        )
    assert messages[0].message.content == "RUN   THE FOCUSED TESTS!"


def test_absent_timestamps_remain_absent():
    (candidate,) = analyze([imported_message(session=f"s{i}") for i in range(3)])
    assert candidate.first_observed_at is candidate.last_observed_at is None


def test_naive_timestamps_are_preserved_without_inventing_timezone():
    timestamp = datetime(2026, 1, 1)
    (candidate,) = analyze(
        [imported_message(session=f"s{i}", timestamp=timestamp) for i in range(3)]
    )
    assert candidate.first_observed_at == timestamp
    assert candidate.first_observed_at.tzinfo is None


def test_incomparable_timestamps_fail_safely():
    messages = [
        imported_message(session="a", timestamp=datetime(2026, 1, 1)),
        imported_message(session="b", timestamp=datetime(2026, 1, 2, tzinfo=UTC)),
        imported_message(session="c"),
    ]
    with pytest.raises(AfterTraceError, match="timezone") as caught:
        analyze(messages)
    assert "Run the focused tests" not in str(caught.value)


def test_deterministic_order_uses_sessions_then_occurrences_then_canonical_key():
    messages = []
    patterns = [
        ("Run the focused tests.", [1, 1, 1, 1]),
        ("Keep the changes small.", [4, 1, 1]),
        ("Preserve original evidence.", [1, 1, 1]),
        ("Do not push changes.", [1, 1, 1]),
    ]
    for pattern, counts in patterns:
        for session, count in enumerate(counts):
            for number in range(count):
                messages.append(imported_message(pattern, f"{pattern}-{session}", number))
    expected = analyze(messages)
    assert [item.canonical_key for item in expected] == [
        "run the focused tests",
        "keep the changes small",
        "do not push changes",
        "preserve original evidence",
    ]
    assert analyze(messages) == expected
    assert analyze(reversed(messages)) == expected
    assert analyze(messages[10:] + messages[:10]) == expected


@pytest.mark.parametrize("field", ["id", "session_id", "project_id", "source_id"])
def test_missing_required_identity_is_not_fabricated(field):
    imported = imported_message()
    if field in ("id", "session_id"):
        setattr(imported.message, field, "")
    else:
        imported = ImportedMessage(
            message=imported.message,
            project_id="" if field == "project_id" else "project",
            source_id="" if field == "source_id" else "source",
            harness=Harness.CODEX,
        )
    with pytest.raises(AfterTraceError, match="incomplete"):
        analyze([imported])


def test_duplicate_message_identity_is_an_invariant_error():
    message = imported_message()
    with pytest.raises(AfterTraceError, match="duplicated"):
        analyze([message, message])
