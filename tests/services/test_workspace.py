import json
import subprocess
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


def test_moved_repository_and_reused_path_preserve_identity_history_and_privacy(workspace):
    from aftertrace.services.findings import FindingPublicationService

    service, repo, source = workspace
    set_origin(repo, None)
    for number in range(1, 4):
        write_session(source, repo, number)
    service.discover()
    old_id = included(service)
    assert service.import_included().imported == 3
    assert FindingPublicationService(service.repository).run()
    with open_database(service.repository.path) as connection:
        history = {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("sessions", "messages", "tool_calls", "findings", "finding_evidence")
        }
        schema = connection.execute("PRAGMA schema_version").fetchone()[0]

    moved = repo.with_name(repo.name + "-legacy")
    repo.rename(moved)
    git_repository(repo)
    # The imported history remains durable even when its source is no longer discoverable.
    source.rename(source.with_name("retired-synthetic-history"))
    for number in range(4, 6):
        write_session(source, repo, number)
    service.discover()

    def project_state(workspace_service):
        snapshot = workspace_service.snapshot()
        assert snapshot.identity_conflicts == snapshot.identity_warnings == ()
        assert len(snapshot.projects) == 2
        return {
            view.project.id: (
                view.project.canonical_identity,
                view.project.privacy_state,
                view.project.primary_location,
                view.locations,
                view.discovered,
                view.imported,
            )
            for view in snapshot.projects
        }

    state = project_state(service)
    old = state[old_id]
    new_id = next(project_id for project_id in state if project_id != old_id)
    assert old == (
        service.identify(str(moved)).canonical_identity,
        Privacy.INCLUDED,
        str(moved),
        (str(moved),),
        0,
        3,
    )
    assert state[new_id] == (
        service.identify(str(repo)).canonical_identity,
        Privacy.EXCLUDED,
        str(repo),
        (str(repo),),
        2,
        0,
    )
    service.discover()
    assert project_state(service) == state
    restarted = WorkspaceService(service.repository.path)
    assert project_state(restarted) == state
    restarted.discover()
    assert project_state(restarted) == state
    with open_database(service.repository.path) as connection:
        for table, before in history.items():
            assert [
                dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")
            ] == before
        assert connection.execute("PRAGMA schema_version").fetchone()[0] == schema
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT count(*) FROM project_sources WHERE project_id=?", (old_id,)
        ).fetchone()[0] == 1


def test_verified_session_location_repairs_reused_remote_location(workspace):
    service, repo, source = workspace
    write_session(source, repo)
    service.discover()
    old_id = included(service)
    assert service.import_included().imported == 1
    moved = repo.with_name("moved")
    repo.rename(moved)
    git_repository(repo, "https://example.invalid/team/different.git")
    write_session(source, moved, 2)
    service.discover()
    old = next(view for view in service.snapshot().projects if view.project.id == old_id)
    assert old.project.primary_location == str(moved)
    assert old.locations == (str(moved),)
    assert old.discovered == old.imported == 1
    assert old.project.privacy_state is Privacy.INCLUDED
    assert service.snapshot().identity_warnings == service.snapshot().identity_conflicts == ()
    with open_database(service.repository.path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM project_sources WHERE project_id=?", (old_id,)
        ).fetchone()[0] == 1


def test_unavailable_remote_location_is_not_replaced_by_another_clone(workspace):
    service, repo, source = workspace
    write_session(source, repo)
    service.discover()
    repo.rename(repo.with_name("inaccessible-location"))
    other = git_repository(repo.with_name("clone"), "https://example.invalid/team/example.git")
    write_session(source, other, 2)
    service.discover()
    (project,) = service.snapshot().projects
    assert project.project.primary_location == str(repo)
    assert set(project.locations) == {str(repo), str(other)}


def test_location_repair_rolls_back_if_primary_location_write_fails(workspace):
    service, repo, source = workspace
    set_origin(repo, None)
    write_session(source, repo)
    service.discover()
    project_id = included(service)
    moved = repo.with_name("moved")
    repo.rename(moved)
    git_repository(repo)
    with open_database(service.repository.path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_relocation BEFORE UPDATE OF primary_location ON projects "
            "BEGIN SELECT RAISE(ABORT, 'synthetic relocation rollback'); END"
        )
        before = list(connection.iterdump())
    with pytest.raises(DatabaseError):
        service.repository.reconcile_project_locations(
            project_id, service.identify(str(moved)), [str(repo)]
        )
    with open_database(service.repository.path) as connection:
        assert list(connection.iterdump()) == before
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


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


def set_origin(repo, value):
    args = ["--unset-all", "remote.origin.url"] if value is None else ["remote.origin.url", value]
    subprocess.run(
        ["git", "-C", str(repo), "config", "--local", *args], check=True, capture_output=True
    )


def legacy_identity_split(workspace, *, with_findings=True):
    """The field sequence: import locally, add origin, then discover with the old identity API."""
    from aftertrace.platform.projects import ProjectIdentity
    from aftertrace.services.findings import FindingPublicationService

    service, repo, source = workspace
    set_origin(repo, None)
    for number in range(1, 8):
        write_session(source, repo, number)
    service.discover()
    local_id = included(service)
    assert service.import_included().imported == 7
    set_origin(repo, "https://example.invalid/team/example.git")
    for number in range(8, 14):
        write_session(source, repo, number)
    identify = service.identify

    def legacy(hint):
        identity = identify(hint)
        return ProjectIdentity(
            identity.canonical_identity,
            identity.display_name,
            identity.root,
            identity.git_remote,
            identity.kind,
        )

    service.identify = legacy
    service.discover()
    for view in service.snapshot().projects:
        service.set_privacy(view.project.id, Privacy.INCLUDED)
    result = service.import_included()
    assert result.imported == 6 and result.failed == 7
    assert sorted((view.discovered, view.imported) for view in service.snapshot().projects) == [
        (0, 7),
        (13, 6),
    ]
    service.identify = identify
    finding_id = None
    if with_findings:
        (finding,) = FindingPublicationService(service.repository).run()
        from aftertrace.core.models import FindingState

        service.repository.update_finding_state(finding.id, FindingState.SAVED)
        finding_id = finding.id
    return service, local_id, finding_id


def test_git_remote_promotion_preserves_one_project_and_unchanged_imports(workspace):
    service, repo, source = workspace
    set_origin(repo, None)
    for number in range(1, 8):
        write_session(source, repo, number)
    service.discover()
    original = included(service)
    assert service.import_included().imported == 7
    set_origin(repo, "https://example.invalid/team/example.git")
    for number in range(8, 14):
        write_session(source, repo, number)
    service.discover()
    (project,) = service.snapshot().projects
    assert project.project.id == original
    assert project.project.canonical_identity == "git-remote:example.invalid/team/example"
    assert project.project.privacy_state is Privacy.INCLUDED
    result = service.import_included()
    assert result.imported == 6 and result.unchanged == 7 and result.failed == 0


def test_existing_six_seven_split_reconciles_without_replacing_messages_or_evidence(workspace):
    from aftertrace.core.models import FindingState
    from aftertrace.services.findings import FindingsService

    service, old_id, finding_id = legacy_identity_split(workspace)
    with open_database(service.repository.path) as connection:
        before = {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("sessions", "messages", "tool_calls", "finding_evidence")
        }
    service.discover()
    (project,) = service.snapshot().projects
    assert project.imported == project.discovered == 13
    assert project.project.privacy_state is Privacy.INCLUDED
    result = service.import_included()
    assert result.unchanged == 13 and result.failed == result.imported == 0
    with open_database(service.repository.path) as connection:
        for table in ("messages", "tool_calls"):
            assert [
                dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")
            ] == before[table]
        after = [dict(row) for row in connection.execute("SELECT * FROM sessions ORDER BY id")]
        for row, original in zip(after, before["sessions"], strict=True):
            assert row == {**original, "project_id": project.project.id}
        assert connection.execute("SELECT count(*) FROM project_sources").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    receipts = service.repository.finding_evidence(finding_id)
    assert {item.id for item in receipts} == {item["id"] for item in before["finding_evidence"]}
    assert all(item.metadata["project_id"] == project.project.id for item in receipts)
    (finding,) = service.repository.findings()
    assert finding.id == finding_id and finding.state is FindingState.SAVED
    assert finding.distinct_project_count == 1 and finding.distinct_session_count == 13
    assert (
        finding.metadata["project_ids"]
        == finding.metadata["origin_project_ids"]
        == [project.project.id]
    )
    assert len(FindingsService(service.repository).detail(finding_id).evidence) == 13
    for _ in range(2):
        reloaded = WorkspaceService(service.repository.path)
        assert len(reloaded.snapshot().projects) == 1
        reloaded.discover()
        assert reloaded.import_included().unchanged == 13
    service.set_privacy(project.project.id, Privacy.NEVER_ANALYZE)
    assert service.repository.findings() == []


@pytest.mark.parametrize("conflicting", [Privacy.EXCLUDED, Privacy.NEVER_ANALYZE])
def test_identity_reconciliation_does_not_resolve_conflicting_privacy(
    workspace, conflicting, monkeypatch
):
    service, local_id, _ = legacy_identity_split(workspace)
    service.set_privacy(local_id, conflicting)
    before = {view.project.id: view.project.privacy_state for view in service.snapshot().projects}
    service.discover()
    assert {
        view.project.id: view.project.privacy_state for view in service.snapshot().projects
    } == before
    (conflict,) = service.snapshot().identity_conflicts
    assert conflict.match_basis == "verified_identity"
    assert conflict.can_merge_without_purging is (conflicting is Privacy.EXCLUDED)
    if conflicting is Privacy.NEVER_ANALYZE:
        with open_database(service.repository.path) as connection:
            sessions_before = connection.execute("SELECT count(*) FROM sessions").fetchone()[0]
        with pytest.raises(AfterTraceError, match="cannot be merged"):
            service.resolve_project_privacy_conflict(conflict, Privacy.EXCLUDED)
        assert len(service.snapshot().projects) == 2
        assert {
            view.project.id: view.project.privacy_state for view in service.snapshot().projects
        } == before
        with open_database(service.repository.path) as connection:
            assert (
                connection.execute("SELECT count(*) FROM sessions").fetchone()[0]
                == sessions_before
            )
    calls = []
    monkeypatch.setattr(service.adapter, "load_raw_session", lambda *args: calls.append(True))
    result = service.import_included()
    assert result.imported == 0 and calls == []


def privacy_conflict_repro(workspace):
    service, local_id, finding_id = legacy_identity_split(workspace)
    other_id = next(
        view.project.id for view in service.snapshot().projects if view.project.id != local_id
    )
    service.set_privacy(local_id, Privacy.EXCLUDED)
    service.discover()
    (conflict,) = service.snapshot().identity_conflicts
    assert conflict.project_ids == tuple(sorted((local_id, other_id)))
    return service, conflict, finding_id


def test_conflicting_duplicate_privacy_is_surfaced_without_choosing_a_policy(
    workspace, monkeypatch
):
    service, conflict, _ = privacy_conflict_repro(workspace)
    before = {
        view.project.id: view.project.privacy_state for view in service.snapshot().projects
    }
    assert len(before) == 2
    assert set(before.values()) == {Privacy.INCLUDED, Privacy.EXCLUDED}
    assert conflict.match_basis == "verified_identity"
    calls = []
    monkeypatch.setattr(service.adapter, "load_raw_session", lambda *args: calls.append(True))
    assert service.import_included().imported == 0
    assert calls == []
    assert {
        view.project.id: view.project.privacy_state for view in service.snapshot().projects
    } == before


def test_keep_excluded_merge_preserves_imports_evidence_and_refresh_state(workspace):
    from aftertrace.services.findings import FindingsService

    service, conflict, finding_id = privacy_conflict_repro(workspace)
    with open_database(service.repository.path) as connection:
        before = {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("sessions", "messages", "tool_calls", "finding_evidence")
        }
    service.resolve_project_privacy_conflict(conflict, Privacy.EXCLUDED)
    assert service.resolve_project_privacy_conflict(
        conflict, Privacy.EXCLUDED
    ) == "Project conflict was already resolved."
    snapshot = service.snapshot()
    (project,) = snapshot.projects
    assert project.project.privacy_state is Privacy.EXCLUDED
    assert project.imported == 13
    assert snapshot.identity_conflicts == ()
    assert service.import_included().blocked == 13
    with open_database(service.repository.path) as connection:
        assert connection.execute("SELECT count(*) FROM projects").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM project_sources").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        after_sessions = [
            dict(row) for row in connection.execute("SELECT * FROM sessions ORDER BY id")
        ]
        assert [row["id"] for row in after_sessions] == [row["id"] for row in before["sessions"]]
        assert all(row["project_id"] == project.project.id for row in after_sessions)
        for table in ("messages", "tool_calls"):
            assert [
                dict(row)
                for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")
            ] == before[table]
        evidence = [
            dict(row)
            for row in connection.execute("SELECT * FROM finding_evidence ORDER BY id")
        ]
        assert [row["id"] for row in evidence] == [row["id"] for row in before["finding_evidence"]]
        assert [row["session_id"] for row in evidence] == [
            row["session_id"] for row in before["finding_evidence"]
        ]
        metadata = json.loads(
            connection.execute(
                "SELECT metadata FROM projects WHERE id=?", (project.project.id,)
            ).fetchone()[0]
        )
        assert "privacy_identity_conflicts" not in metadata
    detail = FindingsService(service.repository).detail(finding_id)
    assert len(detail.evidence) == 13
    assert {item.evidence.id for item in detail.evidence} == {
        row["id"] for row in before["finding_evidence"]
    }
    service.discover()
    reloaded = WorkspaceService(service.repository.path)
    assert len(reloaded.snapshot().projects) == 1
    assert reloaded.snapshot().projects[0].project.privacy_state is Privacy.EXCLUDED
    assert reloaded.snapshot().identity_conflicts == ()
    assert reloaded.import_included().blocked == 13


def test_keep_included_merge_waits_for_explicit_import(workspace, monkeypatch):
    service, conflict, _ = privacy_conflict_repro(workspace)
    _, repo, source = workspace
    write_session(source, repo, 14)
    service.discover()
    (conflict,) = service.snapshot().identity_conflicts
    calls = []
    original_loader = service.adapter.load_raw_session
    monkeypatch.setattr(
        service.adapter,
        "load_raw_session",
        lambda reference: (
            calls.append(reference.source_session_id),
            original_loader(reference),
        )[1],
    )
    service.resolve_project_privacy_conflict(conflict, Privacy.INCLUDED)
    assert service.resolve_project_privacy_conflict(
        conflict, Privacy.INCLUDED
    ) == "Project conflict was already resolved."
    (project,) = service.snapshot().projects
    assert project.project.privacy_state is Privacy.INCLUDED
    assert project.imported == 13 and project.discovered == 14
    assert calls == []
    result = service.import_included()
    assert result.imported == 1 and result.unchanged == 13 and result.failed == 0
    assert len(calls) == 14
    service.discover()
    reloaded = WorkspaceService(service.repository.path)
    assert len(reloaded.snapshot().projects) == 1
    assert reloaded.snapshot().identity_conflicts == ()
    assert reloaded.import_included().unchanged == 14


def test_keep_separate_is_persisted_and_resolution_is_idempotent(workspace):
    service, conflict, _ = privacy_conflict_repro(workspace)
    before = counts(service)
    before_states = {
        view.project.id: view.project.privacy_state for view in service.snapshot().projects
    }
    service.resolve_project_privacy_conflict(conflict, None)
    assert len(service.snapshot().projects) == 2
    assert service.snapshot().identity_conflicts == ()
    assert {
        view.project.id: view.project.privacy_state for view in service.snapshot().projects
    } == before_states
    service.resolve_project_privacy_conflict(conflict, None)
    service.discover()
    reloaded = WorkspaceService(service.repository.path)
    assert len(reloaded.snapshot().projects) == 2
    assert reloaded.snapshot().identity_conflicts == ()
    assert counts(reloaded) == before
    assert reloaded.import_included().imported == 0


def test_privacy_conflict_resolution_rolls_back_if_project_delete_fails(workspace):
    service, conflict, _ = privacy_conflict_repro(workspace)
    with open_database(service.repository.path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_conflict_merge BEFORE DELETE ON projects "
            "BEGIN SELECT RAISE(ABORT, 'synthetic rollback'); END"
        )
        before = list(connection.iterdump())
    with pytest.raises(DatabaseError):
        service.resolve_project_privacy_conflict(conflict, Privacy.INCLUDED)
    with open_database(service.repository.path) as connection:
        assert list(connection.iterdump()) == before
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert len(service.snapshot().projects) == 2
    assert len(service.snapshot().identity_conflicts) == 1


@pytest.mark.parametrize(
    "identity", ["git-local:unrelated:directory", "git-remote:example.invalid/other"]
)
def test_same_visible_path_is_not_equivalence_evidence(workspace, identity):
    service, local_id, _ = legacy_identity_split(workspace)
    service.set_privacy(local_id, Privacy.EXCLUDED)
    with open_database(service.repository.path) as connection:
        connection.execute(
            "UPDATE projects SET canonical_identity=? WHERE id=?", (identity, local_id)
        )
    service.discover()
    snapshot = service.snapshot()
    assert len(snapshot.projects) == 2
    assert snapshot.identity_conflicts == ()
    (warning,) = snapshot.identity_warnings
    other_id = next(view.project.id for view in snapshot.projects if view.project.id != local_id)
    assert warning.project_ids == tuple(sorted((local_id, other_id)))
    assert set(warning.privacy_states) == {Privacy.EXCLUDED, Privacy.INCLUDED}
    assert warning.location == str(workspace[1])
    before_states = {
        view.project.id: view.project.privacy_state for view in snapshot.projects
    }

    def associations():
        with open_database(service.repository.path) as connection:
            sessions = {
                row["id"]: row["project_id"]
                for row in connection.execute("SELECT id,project_id FROM sessions")
            }
            sources = {
                row["id"]: (row["project_id"], row["source_id"], row["observed_path"])
                for row in connection.execute(
                    "SELECT id,project_id,source_id,observed_path FROM project_sources"
                )
            }
        return sessions, sources

    before_sessions, before_sources = associations()
    for keep_state in (Privacy.INCLUDED, Privacy.EXCLUDED, None):
        with pytest.raises(AfterTraceError, match="location-only identity warning"):
            service.resolve_project_privacy_conflict(warning, keep_state)
    assert len(service.snapshot().projects) == 2
    assert service.snapshot().identity_conflicts == ()
    assert len(service.snapshot().identity_warnings) == 1
    assert {
        view.project.id: view.project.privacy_state for view in service.snapshot().projects
    } == before_states
    assert associations() == (before_sessions, before_sources)
    service.discover()
    refreshed = service.snapshot()
    assert len(refreshed.projects) == 2
    assert refreshed.identity_conflicts == ()
    assert len(refreshed.identity_warnings) == 1


def test_equal_strong_canonical_identities_reconcile(workspace):
    service, local_id, _ = legacy_identity_split(workspace)
    with open_database(service.repository.path) as connection:
        connection.execute(
            "UPDATE projects SET canonical_identity=? WHERE id=?",
            (service.identify(str(workspace[1])).canonical_identity, local_id),
        )
    service.discover()
    (project,) = service.snapshot().projects
    assert project.imported == 13
    assert service.import_included().unchanged == 13


def test_reconciliation_preserves_project_metadata_and_project_foreign_keys(workspace):
    service, local_id, finding_id = legacy_identity_split(workspace)
    with open_database(service.repository.path) as connection:
        connection.execute(
            "UPDATE projects SET metadata=? WHERE id=?",
            ('{"identity_kind":"git_local","note":"preserve"}', local_id),
        )
        connection.execute(
            "INSERT INTO instruction_sources (id,project_id,harness,path) "
            "VALUES ('is',?,'codex','synthetic')",
            (local_id,),
        )
        connection.execute(
            "INSERT INTO rules (id,project_id,text) VALUES ('r',?,'Synthetic rule')", (local_id,)
        )
        connection.execute(
            "INSERT INTO rule_drafts (id,project_id,finding_id,text) "
            "VALUES ('d',?,?,'Synthetic draft')",
            (local_id, finding_id),
        )
        created = connection.execute("SELECT min(created_at) FROM projects").fetchone()[0]
        first = connection.execute("SELECT min(first_seen_at) FROM project_sources").fetchone()[0]
    service.discover()
    (view,) = service.snapshot().projects
    assert view.project.metadata["note"] == "preserve"
    assert view.project.created_at.isoformat() == created
    with open_database(service.repository.path) as connection:
        for table in ("instruction_sources", "rules", "rule_drafts"):
            assert (
                connection.execute(f"SELECT project_id FROM {table}").fetchone()[0]
                == view.project.id
            )
        assert (
            connection.execute("SELECT first_seen_at FROM project_sources").fetchone()[0] == first
        )


def test_reconciliation_rejects_conflicting_project_metadata_without_data_loss(workspace):
    service, local_id, _ = legacy_identity_split(workspace)
    with open_database(service.repository.path) as connection:
        for row in connection.execute("SELECT id FROM projects").fetchall():
            connection.execute(
                "UPDATE projects SET metadata=? WHERE id=?", (json.dumps({"note": row[0]}), row[0])
            )
    service.discover()
    assert len(service.snapshot().projects) == 2
    assert service.snapshot().unclassified == 13


def test_reconciliation_rolls_back_every_association_and_receipt_on_failure(workspace):
    service, _, _ = legacy_identity_split(workspace)
    source = service.snapshot().sources[0].source
    entries = [
        (ref, service.identify(ref.project_hint))
        for ref in service.adapter.discover_sessions(source)
    ]
    with open_database(service.repository.path) as connection:
        connection.execute(
            "CREATE TRIGGER synthetic_failure BEFORE DELETE ON projects "
            "BEGIN SELECT RAISE(ABORT,'Synthetic reconciliation failure'); END"
        )
        before = list(connection.iterdump())
    with pytest.raises(DatabaseError):
        service.repository.record_discovery(source, entries)
    with open_database(service.repository.path) as connection:
        assert list(connection.iterdump()) == before
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_changed_fingerprint_after_reconciliation_uses_existing_update_contract(workspace):
    service, _, _ = legacy_identity_split(workspace, with_findings=False)
    with open_database(service.repository.path) as connection:
        original = connection.execute(
            "SELECT id FROM sessions WHERE source_session_id='synthetic-1'"
        ).fetchone()[0]
    write_session(workspace[2], workspace[1], 1, "Keep this changed synthetic instruction.")
    service.discover()
    result = service.import_included()
    assert result.imported == 1 and result.unchanged == 12 and result.failed == 0
    with open_database(service.repository.path) as connection:
        assert (
            connection.execute(
                "SELECT id FROM sessions WHERE source_session_id='synthetic-1'"
            ).fetchone()[0]
            == original
        )
    assert service.import_included().unchanged == 13


def test_changed_evidence_protected_session_still_fails_safely_after_reconciliation(workspace):
    service, _, finding_id = legacy_identity_split(workspace)
    receipts = service.repository.finding_evidence(finding_id)
    write_session(workspace[2], workspace[1], 1, "Keep this changed synthetic instruction.")
    service.discover()
    before = {item.id: item.message_id for item in service.repository.finding_evidence(finding_id)}
    result = service.import_included()
    assert result.unchanged == 12 and result.failed == 1
    assert before == {
        item.id: item.message_id for item in service.repository.finding_evidence(finding_id)
    }
    assert {item.id for item in receipts} == set(before)


def test_reconciliation_does_not_import_or_permit_unclassified_and_excluded_history(workspace):
    service, _, _ = legacy_identity_split(workspace)
    other = git_repository(workspace[1].parent / "excluded")
    for number in range(100, 140):
        write_session(workspace[2], None, number)
    for number in (200, 201):
        write_session(workspace[2], other, number)
    service.discover()
    assert service.snapshot().unclassified == 40
    result = service.import_included()
    assert result.unchanged == 13 and result.blocked == 42 and result.failed == 0
    with open_database(service.repository.path) as connection:
        assert connection.execute("SELECT count(*) FROM sessions").fetchone()[0] == 13


@pytest.mark.parametrize("state", [Privacy.EXCLUDED, Privacy.NEVER_ANALYZE])
def test_origin_addition_does_not_lift_existing_scope(workspace, state, monkeypatch):
    service, repo, source = workspace
    set_origin(repo, None)
    write_session(source, repo)
    service.discover()
    (original,) = service.snapshot().projects
    service.set_privacy(original.project.id, state)
    set_origin(repo, "https://example.invalid/team/example.git")
    service.discover()
    (current,) = service.snapshot().projects
    assert current.project.id == original.project.id
    assert current.project.privacy_state is state
    calls = []
    monkeypatch.setattr(service.adapter, "load_raw_session", lambda *args: calls.append(True))
    result = service.import_included()
    assert result.imported == 0 and result.blocked == 1 and calls == []
