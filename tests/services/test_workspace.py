import json
import threading

import pytest

from aftertrace.core.errors import AfterTraceError, DatabaseError
from aftertrace.core.models import ProjectPrivacyState as Privacy
from aftertrace.services.database import open_database
from aftertrace.services.workspace import WorkspaceService

from .conftest import git_repository, write_session


def counts(service):
    with open_database(service.repository.path) as connection:
        return {
            name: connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in ("sessions", "messages", "tool_calls", "scan_state", "discovered_sessions")
        }


def included(service):
    (project,) = service.snapshot().projects
    service.set_privacy(project.project.id, Privacy.INCLUDED)
    return project.project.id


def test_discovery_groups_strong_projects_without_persisting_transcripts(workspace):
    service, repo, source = workspace
    write_session(source, repo, 1, "SYNTHETIC_PRIVATE_DISCOVERY_BODY")
    write_session(source, repo, 2)
    write_session(source, None, 3)
    write_session(source, repo.parent / "plain-path", 4)
    notice = service.discover()
    snapshot = service.snapshot()
    assert "No content imported" in notice
    (project,) = snapshot.projects
    assert project.discovered == 2 and project.imported == 0
    assert project.project.privacy_state is Privacy.EXCLUDED
    assert snapshot.unclassified == 2
    assert counts(service) == dict(
        sessions=0, messages=0, tool_calls=0, scan_state=0, discovered_sessions=4
    )
    with open_database(service.repository.path) as connection:
        assert "SYNTHETIC_PRIVATE_DISCOVERY_BODY" not in "\n".join(connection.iterdump())


def test_remote_identity_across_locations_reuses_logical_project_and_decision(workspace):
    service, repo, source = workspace
    write_session(source, repo, 1)
    service.discover()
    project_id = included(service)
    other = git_repository(repo.parent / "other-location", "git@example.invalid:team/example.git")
    write_session(source, other, 2)
    service.discover()
    reloaded = WorkspaceService(service.repository.path)
    (project,) = reloaded.snapshot().projects
    assert project.project.id == project_id and project.project.privacy_state is Privacy.INCLUDED
    assert set(project.locations) == {str(repo), str(other)}
    assert project.discovered == 2


@pytest.mark.parametrize("state", list(Privacy))
def test_privacy_state_survives_reload_and_rediscovery(workspace, state):
    service, repo, source = workspace
    write_session(source, repo)
    service.discover()
    (project,) = service.snapshot().projects
    service.set_privacy(project.project.id, state)
    reload = WorkspaceService(service.repository.path)
    reload.discover()
    assert reload.snapshot().projects[0].project.privacy_state is state


def test_never_analyze_and_excluded_gate_adapter_before_loading_or_normalization(
    workspace, monkeypatch
):
    service, repo, source = workspace
    write_session(source, repo)
    write_session(source, None, 2)
    service.discover()
    reference_ids = service.repository.reference_ids()
    calls = []
    monkeypatch.setattr(service.adapter, "load_raw_session", lambda ref: calls.append("load"))
    monkeypatch.setattr(service.adapter, "normalize_session", lambda raw: calls.append("normalize"))
    # A caller changing a GUI/snapshot object cannot change the durable import policy.
    service.snapshot().projects[0].project.privacy_state = Privacy.INCLUDED
    assert service.import_included().blocked == 2
    project_id = service.snapshot().projects[0].project.id
    service.set_privacy(project_id, Privacy.NEVER_ANALYZE)
    assert service.import_included().blocked == 2
    for reference_id in reference_ids:
        assert (
            service.repository.import_reference(
                reference_id, lambda *args: calls.append("callback")
            )
            == "blocked"
        )
    assert calls == []
    assert (
        counts(service)["sessions"]
        == counts(service)["messages"]
        == counts(service)["tool_calls"]
        == 0
    )
    # No persisted session pointers for Never Analyze; only the unclassified reference remains.
    assert counts(service)["discovered_sessions"] == 1
    assert service.snapshot().projects[0].discovered == 1


def test_explicit_import_persists_original_order_tools_fingerprints_and_is_idempotent(workspace):
    service, repo, source = workspace
    path = write_session(source, repo)
    original = path.read_bytes(), path.stat().st_mtime_ns
    service.discover()
    included(service)
    assert counts(service)["sessions"] == 0
    result = service.import_included()
    assert result.imported == 1 and result.failed == 0
    with open_database(service.repository.path) as connection:
        session = connection.execute("SELECT * FROM sessions").fetchone()
        messages = connection.execute("SELECT * FROM messages ORDER BY sequence").fetchall()
        assert messages[3]["content"] == "Keep the change focused.\n  Preserve spacing. 🐾"
        assert messages[3]["author_source"] == "user"
        tool = connection.execute("SELECT * FROM tool_calls").fetchone()
        assert json.loads(tool["arguments"]) == '{"cmd":"python -m pytest tests/example.py"}'
        assert tool["result_summary"] == "1 passed\n"
        assert (
            session["source_fingerprint"]
            == connection.execute("SELECT fingerprint FROM scan_state").fetchone()[0]
        )
    before = counts(service)
    assert service.import_included().unchanged == 1
    assert counts(service) == before
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original


def test_never_analyze_removes_existing_content_excerpts_and_import_metadata(workspace):
    service, repo, source = workspace
    path = write_session(source, repo, text="UNIQUE_PRIVATE_CONTENT_FOR_PURGE")
    service.discover()
    project_id = included(service)
    assert service.import_included().imported == 1
    with open_database(service.repository.path) as connection:
        session = connection.execute("SELECT id FROM sessions").fetchone()[0]
        message = connection.execute("SELECT id FROM messages LIMIT 1").fetchone()[0]
        connection.execute(
            "INSERT INTO findings (id, finding_type, canonical_pattern, title, summary, "
            "confidence, analysis_kind, analysis_version) VALUES ('f', 'repeated_instruction', "
            "'UNIQUE_PRIVATE_CONTENT_FOR_PURGE', 'Example', 'Example', 'high', 'example', '1')"
        )
        connection.execute(
            "INSERT INTO finding_evidence (id, finding_id, session_id, message_id, "
            "evidence_type, original_text) VALUES (?, ?, ?, ?, ?, ?)",
            ("e", "f", session, message, "example", "UNIQUE_PRIVATE_CONTENT_FOR_PURGE"),
        )
        connection.execute(
            "INSERT INTO rule_drafts (id, finding_id, text) VALUES "
            "('draft', 'f', 'UNIQUE_PRIVATE_CONTENT_FOR_PURGE')"
        )
    service.set_privacy(project_id, Privacy.NEVER_ANALYZE)
    assert counts(service) == dict(
        sessions=0, messages=0, tool_calls=0, scan_state=0, discovered_sessions=0
    )
    with open_database(service.repository.path) as connection:
        for table in ("findings", "finding_evidence", "rule_drafts"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert b"UNIQUE_PRIVATE_CONTENT_FOR_PURGE" not in service.repository.path.read_bytes()
    assert b"UNIQUE_PRIVATE_CONTENT_FOR_PURGE" in path.read_bytes()
    service.discover()
    assert counts(service)["discovered_sessions"] == 0
    assert service.snapshot().projects[0].project.privacy_state is Privacy.NEVER_ANALYZE
    # Direct SQL persistence callers are also stopped by the database guard.
    with pytest.raises(DatabaseError):
        with open_database(service.repository.path) as connection:
            source_id = connection.execute("SELECT id FROM sources").fetchone()[0]
            connection.execute(
                "INSERT INTO sessions (id, source_id, project_id) VALUES (?, ?, ?)",
                ("blocked", source_id, project_id),
            )


def test_never_location_survives_unavailable_git_and_changed_remote(workspace):
    service, repo, source = workspace
    write_session(source, repo)
    service.discover()
    project_id = service.snapshot().projects[0].project.id
    service.set_privacy(project_id, Privacy.NEVER_ANALYZE)
    service.identify = lambda hint: None
    service.discover()
    assert counts(service)["discovered_sessions"] == 0
    assert service.snapshot().projects[0].discovered == 1


def test_changed_session_requires_rediscovery_then_replaces_without_duplicates(workspace):
    service, repo, source = workspace
    write_session(source, repo)
    service.discover()
    included(service)
    service.import_included()
    before = counts(service)
    write_session(source, repo, text="Changed sanitized body.")
    assert service.import_included().failed == 1
    service.discover()
    assert service.import_included().imported == 1
    assert counts(service) == before
    with open_database(service.repository.path) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM messages WHERE content = 'Changed sanitized body.'"
            ).fetchone()[0]
            == 3
        )


def test_failed_update_rolls_back_and_other_session_successes_remain(workspace, monkeypatch):
    service, repo, source = workspace
    write_session(source, repo, 1)
    write_session(source, repo, 2)
    service.discover()
    included(service)
    assert service.import_included().imported == 2
    write_session(source, repo, 1, "New body to roll back.")
    service.discover()
    original = service.adapter.normalize_session

    def invalid_tool(raw):
        session = original(raw)
        if raw.reference.source_session_id == "synthetic-1":
            session.tool_calls[0].message_id = "missing-message"
        return session

    monkeypatch.setattr(service.adapter, "normalize_session", invalid_tool)
    result = service.import_included()
    assert result.failed == 1 and result.unchanged == 1
    with open_database(service.repository.path) as connection:
        assert connection.execute("SELECT count(*) FROM sessions").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT count(*) FROM messages WHERE content = 'New body to roll back.'"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_import_never_calls_providers_or_analysis_and_partial_failures_are_safe(
    workspace, monkeypatch
):
    service, repo, source = workspace
    for number in (1, 2, 3):
        write_session(source, repo, number)
    service.discover()
    included(service)
    original = service.adapter.normalize_session

    def fail_one(raw):
        if raw.reference.source_session_id == "synthetic-2":
            raise AfterTraceError("PRIVATE_SYNTHETIC_ERROR_BODY", operation="normalize_session")
        return original(raw)

    monkeypatch.setattr(service.adapter, "normalize_session", fail_one)
    result = service.import_included()
    assert result.imported == 2 and result.failed == 1
    assert "PRIVATE_SYNTHETIC_ERROR_BODY" not in result.summary
    with open_database(service.repository.path) as connection:
        assert connection.execute("SELECT count(*) FROM findings").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM provider_configs").fetchone()[0] == 0


def test_scope_revocation_serializes_with_import_and_purges_a_concurrent_copy(
    workspace, monkeypatch
):
    service, repo, source = workspace
    write_session(source, repo)
    service.discover()
    project_id = included(service)
    entered, release, revoked = threading.Event(), threading.Event(), threading.Event()
    original = service.adapter.load_raw_session

    def pause(reference):
        entered.set()
        assert release.wait(3)
        return original(reference)

    monkeypatch.setattr(service.adapter, "load_raw_session", pause)
    importer = threading.Thread(target=service.import_included)
    importer.start()
    assert entered.wait(3)

    def revoke():
        WorkspaceService(service.repository.path).set_privacy(project_id, Privacy.NEVER_ANALYZE)
        revoked.set()

    revoker = threading.Thread(target=revoke)
    revoker.start()
    assert not revoked.wait(0.05)
    release.set()
    importer.join(4)
    revoker.join(4)
    assert not importer.is_alive() and not revoker.is_alive() and revoked.is_set()
    assert (
        counts(service)["sessions"]
        == counts(service)["messages"]
        == counts(service)["tool_calls"]
        == 0
    )


def test_different_repositories_and_mixed_scopes_import_only_included_content(
    workspace, monkeypatch
):
    service, repo, source = workspace
    write_session(source, repo, 1)
    other = git_repository(repo.parent / "other", "https://example.invalid/team/different.git")
    write_session(source, other, 2)
    write_session(source, None, 3)
    service.discover()
    assert len(service.snapshot().projects) == 2
    for view in service.snapshot().projects:
        service.set_privacy(
            view.project.id,
            Privacy.INCLUDED
            if view.project.primary_location == str(repo)
            else Privacy.NEVER_ANALYZE,
        )
    loaded = []
    original = service.adapter.load_raw_session

    def tracked(reference):
        loaded.append(reference.source_session_id)
        return original(reference)

    monkeypatch.setattr(service.adapter, "load_raw_session", tracked)
    result = service.import_included()
    assert result.imported == 1 and result.blocked == 2
    assert loaded == ["synthetic-1"]
    assert counts(service)["sessions"] == 1


def test_application_and_ui_boundaries_have_no_analysis_provider_or_direct_sql_calls():
    import ast
    import inspect

    from aftertrace.services import repository, workspace
    from aftertrace.ui import workspace as workspace_ui

    for module in (repository, workspace):
        tree = ast.parse(inspect.getsource(module))
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not any(
            name.startswith(("aftertrace.providers", "aftertrace.analysis")) for name in imports
        )
    tree = ast.parse(inspect.getsource(workspace_ui))
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("execute", "executescript", "load_raw_session", "normalize_session")
        for node in ast.walk(tree)
    )


def test_repetition_analysis_reads_only_currently_permitted_explicit_imports(
    workspace, monkeypatch
):
    from aftertrace.analysis.repetitions import RepeatedInstructionAnalyzer
    from aftertrace.core.models import Harness

    service, repo, source = workspace
    excluded = git_repository(repo.parent / "excluded")
    never = git_repository(repo.parent / "never")
    for number, project in enumerate((repo, excluded, never), start=1):
        for occurrence in range(3):
            write_session(source, project, number * 10 + occurrence)
    write_session(source, None, 100)
    service.discover()
    for view in service.snapshot().projects:
        service.set_privacy(view.project.id, Privacy.INCLUDED)
    assert service.import_included().imported == 9
    permitted_id = None
    for view in service.snapshot().projects:
        if view.project.primary_location == str(excluded):
            service.set_privacy(view.project.id, Privacy.EXCLUDED)
        elif view.project.primary_location == str(never):
            service.set_privacy(view.project.id, Privacy.NEVER_ANALYZE)
        else:
            permitted_id = view.project.id

    # Newly discovered Included history still requires a separate explicit import.
    write_session(source, repo, 200)
    service.discover()
    with open_database(service.repository.path) as connection:
        source_id = connection.execute("SELECT id FROM sources LIMIT 1").fetchone()[0]
        # Foundation/partial rows without an import marker are not analysis input either.
        connection.execute(
            "INSERT INTO sessions (id, source_id, project_id) VALUES (?, ?, ?)",
            ("not-imported", source_id, permitted_id),
        )
        connection.execute(
            "INSERT INTO messages (id, session_id, sequence, role, author_source, content) "
            "VALUES ('not-imported-m', 'not-imported', 0, 'user', 'user', ?) ",
            ("Keep the change focused.",),
        )
    before = counts(service)
    assert before["sessions"] == 7  # Three permitted, three Excluded, one unimported.

    def forbidden(*args, **kwargs):
        pytest.fail("Analysis must not discover, load, or normalize raw history.")

    for method in ("detect", "discover_sessions", "load_raw_session", "normalize_session"):
        monkeypatch.setattr(service.adapter, method, forbidden)
    messages = service.repository.imported_messages_for_analysis()
    assert messages == service.repository.imported_messages_for_analysis()
    assert {item.project_id for item in messages} == {permitted_id}
    assert {item.harness for item in messages} == {Harness.CODEX}
    assert len({item.message.session_id for item in messages}) == 3
    assert all(item.message.session_id != "not-imported" for item in messages)

    candidates = RepeatedInstructionAnalyzer().analyze(messages)
    focused = next(
        item for item in candidates if item.canonical_key.startswith("keep the change focused.")
    )
    assert focused.occurrence_count == focused.distinct_session_count == 3
    assert focused.distinct_project_count == 1
    assert counts(service) == before
    with open_database(service.repository.path) as connection:
        stored = {row["id"]: dict(row) for row in connection.execute("SELECT * FROM messages")}
        for item in messages:
            original = stored[item.message.id]
            assert item.message.content == original["content"]
            assert item.message.author_source == original["author_source"]
            assert item.message.sequence == original["sequence"]
            assert item.message.timestamp.isoformat() == original["timestamp"]
        for table in ("findings", "finding_evidence", "provider_configs"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_repetition_analysis_has_no_history_before_explicit_import(workspace):
    from aftertrace.analysis.repetitions import RepeatedInstructionAnalyzer

    service, repo, source = workspace
    for number in range(3):
        write_session(source, repo, number)
    service.discover()
    included(service)
    assert service.repository.imported_messages_for_analysis() == []
    assert RepeatedInstructionAnalyzer().analyze(
        service.repository.imported_messages_for_analysis()
    ) == []


@pytest.mark.parametrize(("column", "invalid"), [
    ("timestamp", "PRIVATE_SYNTHETIC_INVALID_DATE"),
    ("metadata", "PRIVATE_SYNTHETIC_INVALID_JSON"),
])
def test_invalid_imported_message_fails_with_content_free_application_error(
    workspace, column, invalid
):
    service, repo, source = workspace
    write_session(source, repo)
    service.discover()
    included(service)
    assert service.import_included().imported == 1
    with open_database(service.repository.path) as connection:
        connection.execute(f"UPDATE messages SET {column} = ?", (invalid,))
    with pytest.raises(AfterTraceError, match="Imported message data is invalid") as caught:
        service.repository.imported_messages_for_analysis()
    assert invalid not in str(caught.value)
    assert caught.value.detail is None
    assert caught.value.__suppress_context__
