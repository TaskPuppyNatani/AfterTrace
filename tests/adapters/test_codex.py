import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aftertrace.adapters import codex
from aftertrace.adapters.base import HarnessAdapter, RawSession
from aftertrace.adapters.claude_code import ClaudeCodeAdapter
from aftertrace.adapters.codex import CodexAdapter
from aftertrace.adapters.opencode import OpenCodeAdapter
from aftertrace.core.errors import AfterTraceError, UnsupportedOperationError
from aftertrace.core.models import Harness, MessageRole, SessionReference, Source, ToolStatus

FIXTURES = Path(__file__).parents[1] / "fixtures" / "codex"


def source_at(path):
    return Source(
        id="source-example", harness=Harness.CODEX, display_name="Example", location=str(path)
    )


def raw_fixture(name):
    return RawSession(
        SessionReference(source_id="source-example", source_session_id=None, source_locator=name),
        (FIXTURES / name).read_bytes(),
    )


def normalize_records(records):
    payload = "\n".join(json.dumps(record) for record in records).encode()
    return CodexAdapter().normalize_session(
        RawSession(
            SessionReference(
                source_id="example", source_session_id=None, source_locator="synthetic"
            ),
            payload,
        )
    )


def record(kind, payload, **extra):
    return {"type": kind, "payload": payload, **extra}


def message(role, text):
    return record(
        "response_item",
        {
            "type": "message",
            "role": role,
            "content": [{"type": "input_text", "text": text}],
        },
    )


def test_codex_contract_and_detect_active_archive_without_creating_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex home"))
    adapter = CodexAdapter()
    assert isinstance(adapter, HarnessAdapter)
    assert adapter.detect() == []
    active = tmp_path / "codex home" / "sessions"
    archive = active.parent / "archived_sessions"
    active.mkdir(parents=True)
    archive.mkdir()
    candidates = adapter.detect()
    assert [candidate.location for candidate in candidates] == [str(active), str(archive)]
    assert all(candidate.harness is Harness.CODEX for candidate in candidates)
    assert list(active.iterdir()) == list(archive.iterdir()) == []


def test_discovery_missing_empty_disabled_and_wrong_harness(tmp_path):
    adapter = CodexAdapter()
    assert adapter.discover_sessions(source_at(tmp_path / "missing")) == []
    assert adapter.discover_sessions(source_at(tmp_path)) == []
    source = source_at(tmp_path)
    source.enabled = False
    assert adapter.discover_sessions(source) == []
    source.harness = Harness.CLAUDE_CODE
    with pytest.raises(AfterTraceError) as caught:
        adapter.discover_sessions(source)
    assert caught.value.operation == "discover_sessions"


def test_enumeration_is_sorted_lightweight_and_isolates_bad_headers(tmp_path, monkeypatch):
    nested = tmp_path / "2026" / "01" / "02"
    nested.mkdir(parents=True)
    good = nested / "rollout-z.jsonl"
    good.write_bytes((FIXTURES / "normal.jsonl").read_bytes())
    bad = tmp_path / "rollout-a.jsonl"
    bad.write_bytes(b"not JSON\n")
    (tmp_path / "history.jsonl").write_bytes(b"irrelevant")
    (tmp_path / "rollout-ignore.txt").write_bytes(b"irrelevant")
    (tmp_path / "rollout-compressed.jsonl.zst").write_bytes(b"unsupported")
    (tmp_path / "rollout-not-file.jsonl").mkdir()
    seen_modes = []
    original_open = Path.open

    class HeaderReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def readline(self, limit):
            assert 0 < limit <= 1024 * 1024 + 1
            return self.stream.readline(limit)

    def bounded_open(path, mode="r", *args, **kwargs):
        seen_modes.append(mode)
        return HeaderReader(original_open(path, mode, *args, **kwargs))

    monkeypatch.setattr(Path, "open", bounded_open)
    references = CodexAdapter().discover_sessions(source_at(tmp_path))
    assert len(references) == 2
    assert [ref.source_locator for ref in references] == sorted([str(good), str(bad)])
    good_ref = next(ref for ref in references if ref.source_locator == str(good))
    assert good_ref.source_id == "source-example"
    assert good_ref.source_session_id == "00000000-0000-4000-8000-000000000001"
    assert good_ref.project_hint == "/example/workspace"
    assert good_ref.started_at_hint == datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    bad_ref = next(ref for ref in references if ref.source_locator == str(bad))
    assert bad_ref.metadata["codex"]["header_unavailable"] is True
    assert seen_modes == ["rb", "rb"]


def test_oversized_discovery_header_still_allows_explicit_loading(tmp_path):
    path = tmp_path / "rollout-large-header.jsonl"
    path.write_text(json.dumps(record("session_meta", {"unused": "x" * (1024 * 1024)})))
    adapter = CodexAdapter()
    (reference,) = adapter.discover_sessions(source_at(tmp_path))
    assert reference.metadata["codex"]["header_unavailable"]
    assert adapter.normalize_session(adapter.load_raw_session(reference)).source_session_id is None


def test_discovery_does_not_follow_symlinks(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    target = real / "rollout-real.jsonl"
    target.write_bytes((FIXTURES / "minimal.jsonl").read_bytes())
    try:
        (tmp_path / "rollout-link.jsonl").symlink_to(target)
        (tmp_path / "linked-directory").symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is unavailable on this platform")
    references = CodexAdapter().discover_sessions(source_at(tmp_path))
    assert [ref.source_locator for ref in references] == [str(target)]


def test_loading_fingerprinting_and_read_only_source_safety(tmp_path):
    path = tmp_path / "rollout-example.jsonl"
    path.write_bytes((FIXTURES / "normal.jsonl").read_bytes())
    original = path.read_bytes()
    info = path.stat()
    adapter = CodexAdapter()
    (reference,) = adapter.discover_sessions(source_at(tmp_path))
    raw = adapter.load_raw_session(reference)
    session = adapter.normalize_session(raw)
    assert raw.payload == original
    assert (
        raw.reference.fingerprint_hint == reference.fingerprint_hint == session.source_fingerprint
    )
    assert (
        adapter.discover_sessions(source_at(tmp_path))[0].fingerprint_hint
        == reference.fingerprint_hint
    )
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == info.st_mtime_ns
    assert path.stat().st_mode == info.st_mode
    assert list(tmp_path.iterdir()) == [path]
    # Even a same-size change with a new mtime invalidates the cheap token.
    path.write_bytes(original.replace(b"example-model", b"changed-model"))
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
    changed = adapter.load_raw_session(reference)
    assert changed.reference.fingerprint_hint != reference.fingerprint_hint
    assert adapter.normalize_session(changed).model == "changed-model"


def test_missing_and_unreadable_loading_errors_are_content_free(tmp_path, monkeypatch):
    adapter = CodexAdapter()
    reference = SessionReference(
        source_id="example",
        source_session_id=None,
        source_locator=str(tmp_path / "missing.jsonl"),
    )
    with pytest.raises(AfterTraceError) as caught:
        adapter.load_raw_session(reference)
    assert caught.value.operation == "load_raw_session" and caught.value.source == "codex"
    path = tmp_path / "exists.jsonl"
    path.write_bytes(b"content that must not leak")
    reference.source_locator = str(path)

    def denied(*args, **kwargs):
        raise PermissionError("content that must not leak")

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(AfterTraceError) as caught:
        adapter.load_raw_session(reference)
    assert "content that must not leak" not in str(caught.value) + caught.value.detail


def test_loading_detects_concurrent_change(tmp_path, monkeypatch):
    path = tmp_path / "rollout-changing.jsonl"
    path.write_bytes((FIXTURES / "minimal.jsonl").read_bytes())
    (reference,) = CodexAdapter().discover_sessions(source_at(tmp_path))
    original = os.fstat
    count = 0

    def changing_stat(fd):
        nonlocal count
        count += 1
        info = original(fd)
        if count == 2:
            values = list(info)
            values[6] += 1  # st_size changes, with no actual source mutation.
            return os.stat_result(values)
        return info

    monkeypatch.setattr(codex.os, "fstat", changing_stat)
    with pytest.raises(AfterTraceError, match="could not be read") as caught:
        CodexAdapter().load_raw_session(reference)
    assert "changed while being read" in caught.value.detail


def test_normal_session_preserves_messages_timestamps_metadata_and_tool_result():
    raw = raw_fixture("normal.jsonl")
    session = CodexAdapter().normalize_session(raw)
    assert session.source_id == "source-example"
    assert session.source_session_id == "00000000-0000-4000-8000-000000000001"
    assert session.id == CodexAdapter().normalize_session(raw).id
    assert session.project_id is None
    assert session.working_directory == "/example/workspace"
    assert session.model == "example-model"
    assert session.started_at == datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    assert session.ended_at is None  # Task completion is not session closure.
    assert [m.role for m in session.messages] == [
        MessageRole.SYSTEM,
        MessageRole.DEVELOPER,
        MessageRole.USER,
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]
    assert [m.author_source for m in session.messages] == [
        "harness",
        "harness",
        "harness",
        "user",
        "assistant",
        "tool",
        "user",
        "assistant",
    ]
    assert session.messages[0].content == "System context.\n"
    assert session.messages[3].content == "Keep the change focused.\n  Preserve spacing. 🐾"
    assert session.messages[3].timestamp == datetime(2026, 1, 2, 3, 4, 6, tzinfo=UTC)
    assert [m.sequence for m in session.messages] == list(range(8))
    assert all(m.session_id == session.id for m in session.messages)
    assert session.metadata["codex"]["model_provider"] == "example"
    assert session.metadata["codex"]["session_id"] != session.source_session_id
    assert session.metadata["codex"]["partial"] is False
    (tool,) = session.tool_calls
    assert tool.arguments == '{"cmd":"python -m pytest tests/example.py"}'
    assert tool.name == "exec_command" and tool.status is ToolStatus.SUCCEEDED
    assert tool.result_summary == "1 passed\n"
    assert tool.message_id == session.messages[5].id
    json.dumps(session.to_dict(), allow_nan=False)


def test_explicit_per_part_and_legacy_authorship():
    session = CodexAdapter().normalize_session(raw_fixture("authorship.jsonl"))
    assert [message.content for message in session.messages] == [
        "Injected context.",
        "Actual user input.",
        "Tool-supplied context.",
        "Legacy injected context.",
        "Uncertain legacy input.",
        "Inherited input.",
        "Compaction-generated summary.",
    ]
    assert [m.author_source for m in session.messages] == [
        "harness",
        "user",
        "tool",
        "harness",
        None,
        "harness",
        "harness",
    ]
    assert [m.metadata["codex"]["content_index"] for m in session.messages[:3]] == [0, 1, 2]


def test_missing_optional_data_and_minimal_session_remain_missing():
    session = CodexAdapter().normalize_session(raw_fixture("missing-optional.jsonl"))
    assert session.source_session_id is None
    assert (
        session.started_at is session.ended_at is session.model is session.working_directory is None
    )
    assert session.imported_at is session.updated_at is session.project_id is None
    assert session.messages[0].author_source is None
    assert all(message.timestamp is None for message in session.messages)
    minimal = CodexAdapter().normalize_session(raw_fixture("minimal.jsonl"))
    assert minimal.messages == minimal.tool_calls == []


def test_malformed_and_truncated_lines_preserve_valid_neighbors_without_content_in_diagnostics():
    session = CodexAdapter().normalize_session(raw_fixture("malformed.jsonl"))
    assert [m.content for m in session.messages] == ["Keep valid evidence."]
    assert session.metadata["codex"]["malformed_line_count"] == 2
    assert session.metadata["codex"]["malformed_line_numbers"] == [2, 4]
    assert session.metadata["codex"]["partial"] is True


@pytest.mark.parametrize("payload", [b"", b"\n \n", b"{private malformed content", b"[]\n", b"{}"])
def test_empty_unsupported_or_unreadable_session_has_controlled_error(payload):
    with pytest.raises(AfterTraceError) as caught:
        CodexAdapter().normalize_session(
            RawSession(raw_fixture("minimal.jsonl").reference, payload)
        )
    assert caught.value.operation == "normalize_session"
    assert caught.value.source == "codex"
    assert "private malformed content" not in str(caught.value) + caught.value.detail


def test_bad_encoding_nonobjects_and_unexpected_field_types_degrade_gracefully():
    raw = raw_fixture("minimal.jsonl")
    payload = raw.payload + b'\xff\nnull\n{"type":"turn_context","payload":null}\n'
    session = CodexAdapter().normalize_session(RawSession(raw.reference, payload))
    assert session.metadata["codex"]["malformed_line_count"] == 3
    session = normalize_records(
        [
            record("session_meta", {"id": 123, "timestamp": "invalid"}),
            record("turn_context", {"model": None, "cwd": 5}),
            record("response_item", {"type": ["bad"]}),
            record("response_item", {"type": "message", "role": {}, "content": None}),
            record("response_item", {"type": "function_call", "call_id": [], "name": None}),
            message("unexpected", "Original text."),
        ]
    )
    assert session.source_session_id is session.started_at is session.model is None
    assert session.messages[0].role is MessageRole.UNKNOWN
    assert session.metadata["codex"]["partial"] is True


def test_first_metadata_is_canonical_last_model_context_is_preserved():
    session = normalize_records(
        [
            record("session_meta", {"id": "child", "cwd": r"C:\example\workspace"}),
            record("turn_context", {"model": "first"}),
            record(
                "session_meta",
                {"id": "parent", "cwd": "/other", "timestamp": "2026-01-01T00:00:00Z"},
            ),
            record("turn_context", {"model": "last"}),
        ]
    )
    assert session.source_session_id == "child"
    assert session.working_directory == r"C:\example\workspace"
    assert session.started_at is None
    assert session.model == "last"
    assert [context["model"] for context in session.metadata["codex"]["turn_contexts"]] == [
        "first",
        "last",
    ]


def test_event_mirrors_are_one_to_one_and_event_only_messages_survive():
    session = normalize_records(
        [
            record("session_meta", {}),
            record("event_msg", {"type": "user_message", "message": "Repeat."}),
            message("user", "Repeat."),
            record("event_msg", {"type": "user_message", "message": "Repeat."}),
            message("user", "Repeat."),
            record("event_msg", {"type": "agent_message", "message": "Event-only response."}),
            message("user", "Unattributed original."),
        ]
    )
    assert [m.content for m in session.messages] == [
        "Repeat.",
        "Repeat.",
        "Event-only response.",
        "Unattributed original.",
    ]
    assert [m.author_source for m in session.messages] == ["user", "user", "assistant", None]


@pytest.mark.parametrize(
    "code, expected",
    [(0, ToolStatus.SUCCEEDED), (2, ToolStatus.FAILED), (None, ToolStatus.UNKNOWN)],
)
def test_custom_tools_orphan_output_and_no_inferred_success(code, expected):
    records = [
        record("session_meta", {}),
        record(
            "response_item",
            {
                "type": "custom_tool_call",
                "name": "example_tool",
                "call_id": "a",
                "input": "original input",
                "status": "completed",
            },
        ),
    ]
    if code is not None:
        records.append(
            record("event_msg", {"type": "exec_command_end", "call_id": "a", "exit_code": code})
        )
    records.extend(
        [
            record(
                "response_item",
                {
                    "type": "custom_tool_call_output",
                    "call_id": "a",
                    "output": [{"type": "input_text", "text": "original output\n"}],
                },
            ),
            record(
                "response_item",
                {"type": "function_call_output", "call_id": "missing", "output": "orphan output"},
            ),
        ]
    )
    session = normalize_records(records)
    (tool,) = session.tool_calls
    assert tool.arguments == "original input"
    assert tool.result_summary == "original output\n"
    assert tool.status is expected
    assert [m.role for m in session.messages] == [MessageRole.TOOL, MessageRole.TOOL]
    assert session.messages[1].content == "orphan output"


def test_unknown_encrypted_and_nontext_records_do_not_become_instructions():
    session = normalize_records(
        [
            record("session_meta", {}),
            record(
                "response_item",
                {"type": "compaction", "encrypted_content": "synthetic opaque bytes"},
            ),
            record(
                "response_item",
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": "synthetic"}],
                },
            ),
            record("future_record", {"unknown": "synthetic"}),
        ]
    )
    assert session.messages == []
    assert session.metadata["codex"]["partial"] is True
    assert "synthetic opaque bytes" not in json.dumps(session.to_dict())


@pytest.mark.parametrize("adapter_type", [ClaudeCodeAdapter, OpenCodeAdapter])
def test_other_adapters_remain_structural_stubs(adapter_type):
    adapter = adapter_type()
    assert isinstance(adapter, HarnessAdapter)
    source = source_at("nonexistent")
    reference = raw_fixture("minimal.jsonl").reference
    for method, args in [
        ("detect", ()),
        ("discover_sessions", (source,)),
        ("load_raw_session", (reference,)),
        ("normalize_session", (RawSession(reference, b""),)),
    ]:
        with pytest.raises(UnsupportedOperationError):
            getattr(adapter, method)(*args)


def test_unreadable_header_does_not_hide_other_sessions(tmp_path, monkeypatch):
    bad = tmp_path / "rollout-unreadable.jsonl"
    good = tmp_path / "rollout-readable.jsonl"
    bad.write_bytes((FIXTURES / "minimal.jsonl").read_bytes())
    good.write_bytes((FIXTURES / "minimal.jsonl").read_bytes())
    original = Path.open

    def selective_open(path, *args, **kwargs):
        if path == bad:
            raise PermissionError("sanitized access failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", selective_open)
    adapter = CodexAdapter()
    references = adapter.discover_sessions(source_at(tmp_path))
    assert len(references) == 2
    readable = next(ref for ref in references if ref.source_locator == str(good))
    assert (
        adapter.normalize_session(adapter.load_raw_session(readable)).source_session_id is not None
    )


def test_archive_candidate_uses_the_same_read_only_normalization(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    archive = tmp_path / "archived_sessions"
    archive.mkdir()
    file = archive / "rollout-example.jsonl"
    file.write_bytes((FIXTURES / "normal.jsonl").read_bytes())
    adapter = CodexAdapter()
    (candidate,) = adapter.detect()
    (reference,) = adapter.discover_sessions(source_at(candidate.location))
    session = adapter.normalize_session(adapter.load_raw_session(reference))
    assert session.messages[3].author_source == "user"
    assert list(archive.iterdir()) == [file]


def test_multipart_event_matching_preserves_original_text_parts():
    session = normalize_records(
        [
            record("session_meta", {}),
            record("event_msg", {"type": "user_message", "message": "First.\nSecond."}),
            record(
                "response_item",
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "First."},
                        {"type": "input_text", "text": "Second."},
                    ],
                },
            ),
        ]
    )
    assert [m.content for m in session.messages] == ["First.", "Second."]
    assert all(m.author_source == "user" for m in session.messages)


def test_encrypted_message_does_not_appear_complete_or_expose_opaque_payload():
    session = normalize_records(
        [
            record("session_meta", {}),
            record(
                "response_item",
                {
                    "type": "message",
                    "role": "user",
                    "content": [],
                    "encrypted_content": "opaque synthetic",
                },
            ),
        ]
    )
    assert session.messages == []
    assert session.metadata["codex"]["partial"] is True
    assert "opaque synthetic" not in json.dumps(session.to_dict())


def test_core_models_do_not_import_native_adapters():
    import ast
    import inspect

    from aftertrace.core import models

    tree = ast.parse(inspect.getsource(models))
    imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not any(name and name.startswith("aftertrace.adapters") for name in imports)


def test_thread_owned_settings_supply_model_and_ignore_copied_context():
    session = normalize_records(
        [
            record("session_meta", {"id": "child"}),
            record(
                "event_msg",
                {
                    "type": "thread_settings_applied",
                    "thread_id": "child",
                    "thread_settings": {
                        "model": "configured-model",
                        "model_provider_id": "example",
                        "cwd": "/example",
                    },
                },
            ),
            record("turn_context", {"model": "older-frozen-model"}),
            record(
                "event_msg",
                {
                    "type": "thread_settings_applied",
                    "thread_id": "parent",
                    "thread_settings": {"model": "parent-model"},
                },
            ),
        ]
    )
    assert session.model == "configured-model"
    assert session.working_directory == "/example"
    assert session.metadata["codex"]["thread_settings"]["model_provider_id"] == "example"
    assert session.metadata["codex"]["turn_contexts"][0]["model"] == "older-frozen-model"


def test_legacy_settings_without_owner_can_update_the_recorded_model():
    session = normalize_records(
        [
            record("session_meta", {}),
            record("turn_context", {"model": "first"}),
            record(
                "event_msg",
                {"type": "thread_settings_applied", "thread_settings": {"model": "latest"}},
            ),
        ]
    )
    assert session.model == "latest"
