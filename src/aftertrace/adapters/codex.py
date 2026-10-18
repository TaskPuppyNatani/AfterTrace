"""Read-only plaintext Codex rollout JSONL adapter. No imports or analysis are triggered.

See tests/fixtures/codex/README.md for confirmed wire structures and supported limits.
"""

import json
import os
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from aftertrace.adapters.base import RawSession, SourceCandidate
from aftertrace.core.errors import AfterTraceError
from aftertrace.core.models import (
    ContentFormat,
    Harness,
    Message,
    MessageRole,
    Session,
    SessionReference,
    Source,
    ToolCall,
    ToolStatus,
)
from aftertrace.platform.paths import codex_history_directories

_HEADER_LIMIT = 1024 * 1024
_META_FIELDS = (
    "cli_version",
    "originator",
    "model_provider",
    "source",
    "git",
    "session_id",
    "forked_from_id",
    "parent_thread_id",
    "history_mode",
)


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return None


def _fingerprint(info: os.stat_result) -> str:
    # Cheap change token, not a content hash: size/mtime changes invalidate it.
    return f"stat-v1:{info.st_size}:{info.st_mtime_ns}"


def _error(operation: str, detail: str) -> AfterTraceError:
    return AfterTraceError(
        "Codex session data could not be read."
        if operation == "load_raw_session"
        else "Codex session data could not be normalized."
        if operation == "normalize_session"
        else "Codex sessions could not be discovered.",
        operation=operation,
        source="codex",
        detail=detail,
    )


def _invalid_constant(value: str) -> None:
    raise ValueError("Non-JSON numeric constant")


def _record(line: bytes) -> dict:
    record = json.loads(line, parse_constant=_invalid_constant)
    if not isinstance(record, dict) or not isinstance(record.get("type"), str):
        raise ValueError("Invalid record envelope")
    if not isinstance(record.get("payload"), dict):
        raise ValueError("Invalid record payload")
    return record


def _author(payload: dict, part_index: int, envelope: dict) -> tuple[str | None, dict]:
    role = payload.get("role")
    attribution = payload.get("internal_chat_message_metadata_passthrough")
    attribution = attribution if isinstance(attribution, dict) else {}
    entries = attribution.get("content_item_metadata")
    entry = entries[part_index] if isinstance(entries, list) and part_index < len(entries) else {}
    entry = entry if isinstance(entry, dict) else {}
    provenance = entry.get("provenance")
    kind = provenance.get("type") if isinstance(provenance, dict) else None
    if envelope.get("inherited_user_message") or envelope.get("compaction_output"):
        return "harness", entry
    if kind in ("developer_instructions", "skills", "agents_md", "harness"):
        return "harness", entry
    if kind == "tool" or entry.get("source_tool_namespace") is not None:
        return "tool", entry
    if entry.get("harness_injected") is True:
        return "harness", entry
    if role in ("system", "developer"):
        return "harness", entry
    if role == "tool":
        return "tool", entry
    if role == "assistant":
        return "assistant", entry
    return ("user" if kind == "user" and role == "user" else None), entry


class CodexAdapter:
    source_name = "Codex"

    def detect(self) -> list[SourceCandidate]:
        return [
            SourceCandidate(Harness.CODEX, f"Codex {path.name}", str(path))
            for path in codex_history_directories()
            if path.is_dir()
        ]

    def discover_sessions(self, source: Source) -> list[SessionReference]:
        if source.harness != Harness.CODEX:
            raise _error("discover_sessions", "Source harness is not Codex.")
        if not source.enabled:
            return []
        root = Path(source.location)
        if not root.is_dir():
            return []
        references = []

        # Do not follow directory symlinks or enumerate anything except plaintext rollouts.
        def walk_error(error: OSError) -> None:
            raise _error("discover_sessions", f"Directory access failed (errno {error.errno}).")

        for directory, subdirs, filenames in os.walk(root, followlinks=False, onerror=walk_error):
            subdirs[:] = sorted(
                name for name in subdirs if not (Path(directory) / name).is_symlink()
            )
            for name in sorted(filenames):
                if not (name.startswith("rollout-") and name.endswith(".jsonl")):
                    continue
                path = Path(directory) / name
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    info = path.stat()
                except OSError:
                    continue  # A vanished/unstatable file must not hide its neighbors.
                metadata = {}
                header = {}
                try:
                    with path.open("rb") as stream:
                        line = stream.readline(_HEADER_LIMIT + 1)
                    if len(line) > _HEADER_LIMIT:
                        raise ValueError("Header exceeds discovery limit")
                    record = _record(line)
                    if record["type"] == "session_meta":
                        header = record["payload"]
                    else:
                        raise ValueError("Session header unavailable")
                except (OSError, ValueError, UnicodeError, RecursionError):
                    metadata["header_unavailable"] = True
                references.append(
                    SessionReference(
                        source_id=source.id,
                        source_session_id=_string(header.get("id")),
                        source_locator=str(path.absolute()),
                        project_hint=_string(header.get("cwd")),
                        started_at_hint=_timestamp(header.get("timestamp")),
                        fingerprint_hint=_fingerprint(info),
                        metadata={"codex": metadata},
                    )
                )
        return sorted(references, key=lambda item: item.source_locator)

    def load_raw_session(self, reference: SessionReference) -> RawSession:
        path = Path(reference.source_locator)
        try:
            if not path.is_file():
                raise _error("load_raw_session", "The source is missing or is not a regular file.")
            with path.open("rb") as stream:
                before = os.fstat(stream.fileno())
                payload = stream.read()
                after = os.fstat(stream.fileno())
            if _fingerprint(before) != _fingerprint(after):
                raise _error(
                    "load_raw_session", "The source changed while being read; retry later."
                )
        except OSError as error:
            raise _error("load_raw_session", f"File access failed (errno {error.errno}).") from None
        # Fingerprint the snapshot actually read, not a potentially stale discovery hint.
        return RawSession(replace(reference, fingerprint_hint=_fingerprint(after)), payload)

    def normalize_session(self, raw: RawSession) -> Session:
        records = []
        malformed = []
        for index, line in enumerate(raw.payload.splitlines()):
            if not line.strip():
                continue
            try:
                records.append((index, _record(line)))
            except (ValueError, UnicodeError, RecursionError):
                malformed.append(index + 1)
        header = next((record for _, record in records if record["type"] == "session_meta"), None)
        if header is None:
            # Never include JSONDecodeError.doc, line text, or raw bytes in diagnostics.
            raise _error("normalize_session", "No readable session_meta record was found.")
        meta = header["payload"]
        reference = raw.reference
        native_id = _string(meta.get("id"))
        identity = native_id or reference.source_locator
        session = Session(
            id=str(uuid5(NAMESPACE_URL, f"aftertrace:codex:{reference.source_id}:{identity}")),
            source_id=reference.source_id,
            source_session_id=native_id,
            started_at=_timestamp(meta.get("timestamp")),
            working_directory=_string(meta.get("cwd")),
            source_fingerprint=reference.fingerprint_hint,
            metadata={"codex": {key: meta[key] for key in _META_FIELDS if key in meta}},
        )
        normalizer = _Normalizer(session)
        for index, record in records:
            normalizer.consume(index, record)
        normalizer.finish()
        diagnostics = session.metadata["codex"]
        diagnostics["malformed_line_count"] = len(malformed)
        diagnostics["malformed_line_numbers"] = malformed[:20]
        diagnostics["skipped_record_counts"] = dict(normalizer.skipped)
        diagnostics["partial"] = bool(malformed or normalizer.skipped)
        return session


class _Normalizer:
    """Per-session native reconstruction state; never escapes the adapter boundary."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.calls: dict[str, ToolCall] = {}
        self.groups: list[list[Message]] = []
        self.events: list[tuple[int, dict, datetime | None]] = []
        self.skipped: Counter[str] = Counter()
        self.has_settings_model = False

    def consume(self, index: int, record: dict) -> None:
        kind, payload = record["type"], record["payload"]
        timestamp = _timestamp(record.get("timestamp"))
        if kind == "session_meta":
            return
        if kind == "turn_context":
            model = _string(payload.get("model"))
            if model is not None and not self.has_settings_model:
                self.session.model = model
            if self.session.working_directory is None:
                self.session.working_directory = _string(payload.get("cwd"))
            context = {key: payload[key] for key in ("model", "cwd", "turn_id") if key in payload}
            self.session.metadata["codex"].setdefault("turn_contexts", []).append(context)
            return
        subtype = payload.get("type")
        if kind == "event_msg":
            if subtype == "thread_settings_applied":
                settings = payload.get("thread_settings")
                owner = payload.get("thread_id")
                if isinstance(settings, dict) and owner in (None, self.session.source_session_id):
                    model = _string(settings.get("model"))
                    if model is not None:
                        self.session.model = model
                        self.has_settings_model = True
                    if self.session.working_directory is None:
                        self.session.working_directory = _string(settings.get("cwd"))
                    self.session.metadata["codex"]["thread_settings"] = {
                        key: settings[key]
                        for key in ("model", "model_provider_id", "cwd")
                        if key in settings
                    }
            elif subtype in ("user_message", "agent_message") and isinstance(
                payload.get("message"), str
            ):
                self.events.append((index, payload, timestamp))
            elif subtype == "exec_command_end":
                call = self.calls.get(_string(payload.get("call_id")))
                code = payload.get("exit_code")
                if call and isinstance(code, int) and not isinstance(code, bool):
                    call.status = ToolStatus.SUCCEEDED if code == 0 else ToolStatus.FAILED
                    call.ended_at = timestamp
                    call.metadata["codex"]["exit_code"] = code
            # Other events are telemetry/context, not conversation messages.
            return
        if kind != "response_item":
            self.skipped[kind] += 1
            return
        if payload.get("encrypted_content") or payload.get("encrypted_function_args"):
            self.skipped["encrypted_content"] += 1
        if subtype == "message":
            envelope = record.get("metadata")
            envelope = envelope if isinstance(envelope, dict) else {}
            group = []
            parts = payload.get("content")
            if not isinstance(parts, list):
                self.skipped["message_content"] += 1
                return
            for part_index, part in enumerate(parts):
                if not isinstance(part, dict) or part.get("type") not in (
                    "input_text",
                    "output_text",
                ):
                    self.skipped["non_text_content"] += 1
                    continue
                if not isinstance(part.get("text"), str):
                    self.skipped["message_content"] += 1
                    continue
                author, attribution = _author(payload, part_index, envelope)
                role = _string(payload.get("role"))
                message = self.message(index, part_index, part["text"], role, timestamp, author)
                details = message.metadata["codex"]
                for key in ("id", "phase"):
                    if key in payload:
                        details[key] = payload[key]
                if attribution:
                    details["attribution"] = attribution
                for key in ("inherited_user_message", "compaction_output"):
                    if key in envelope:
                        details[key] = envelope[key]
                group.append(message)
            self.groups.append(group)
        elif subtype in ("function_call", "custom_tool_call"):
            name, call_id = _string(payload.get("name")), _string(payload.get("call_id"))
            if name is None or call_id is None:
                self.skipped["invalid_tool_call"] += 1
                return
            tool = ToolCall(
                id=self.identity(f"tool:{index}"),
                session_id=self.session.id,
                sequence=len(self.session.tool_calls),
                name=name,
                started_at=timestamp,
                arguments=payload.get("arguments" if subtype == "function_call" else "input"),
                metadata={"codex": {"record_index": index, "call_id": call_id, "type": subtype}},
            )
            for key in ("id", "namespace", "status"):
                if key in payload:
                    tool.metadata["codex"][key] = payload[key]
            self.calls[call_id] = tool
            self.session.tool_calls.append(tool)
        elif subtype in ("function_call_output", "custom_tool_call_output"):
            output = payload.get("output")
            parts = [{"type": "input_text", "text": output}] if isinstance(output, str) else output
            if not isinstance(parts, list):
                self.skipped["invalid_tool_output"] += 1
                return
            messages = []
            for part_index, part in enumerate(parts):
                if (
                    not isinstance(part, dict)
                    or part.get("type") not in ("input_text", "output_text")
                    or not isinstance(part.get("text"), str)
                ):
                    self.skipped["non_text_tool_output"] += 1
                    continue
                message = self.message(index, part_index, part["text"], "tool", timestamp, "tool")
                message.metadata["codex"]["call_id"] = _string(payload.get("call_id"))
                messages.append(message)
            call = self.calls.get(_string(payload.get("call_id")))
            if call and messages:
                call.result_summary = "".join(message.content for message in messages)
                call.message_id = messages[0].id
                call.ended_at = call.ended_at or timestamp
        else:
            self.skipped["unsupported_response_item"] += 1

    def identity(self, suffix: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"{self.session.id}:{suffix}"))

    def message(
        self,
        index: int,
        part_index: int,
        text: str,
        role: str | None,
        timestamp: datetime | None,
        author: str | None,
    ) -> Message:
        try:
            normalized_role = MessageRole(role)
        except ValueError:
            normalized_role = MessageRole.UNKNOWN
        message = Message(
            id=self.identity(f"message:{index}:{part_index}"),
            session_id=self.session.id,
            sequence=0,
            content=text,
            timestamp=timestamp,
            author_source=author,
            role=normalized_role,
            content_format=ContentFormat.TEXT,
            metadata={"codex": {"record_index": index, "content_index": part_index}},
        )
        self.session.messages.append(message)
        return message

    def finish(self) -> None:
        # Events mirror response items on some Codex versions. Match exact text one-to-one,
        # retaining repeated real turns and event-only messages. No substring heuristics.
        matched = set()
        for index, event, timestamp in self.events:
            role = MessageRole.USER if event["type"] == "user_message" else MessageRole.ASSISTANT
            candidates = []
            for group_index, group in enumerate(self.groups):
                eligible = [
                    m for m in group if m.role == role and m.author_source in (None, role.value)
                ]
                if not eligible or group_index in matched:
                    continue
                texts = [message.content for message in eligible]
                if event["message"] in ("".join(texts), "\n".join(texts)):
                    distance = abs(eligible[0].metadata["codex"]["record_index"] - index)
                    candidates.append((distance, group_index, eligible))
            if candidates:
                _, group_index, eligible = min(candidates, key=lambda item: (item[0], item[1]))
                matched.add(group_index)
                for message in eligible:
                    message.author_source = role.value
                    message.metadata["codex"]["author_event_index"] = index
            else:
                self.message(index, 0, event["message"], role.value, timestamp, role.value)
        self.session.messages.sort(
            key=lambda m: (
                m.metadata["codex"]["record_index"],
                m.metadata["codex"]["content_index"],
            )
        )
        for sequence, message in enumerate(self.session.messages):
            message.sequence = sequence
