import threading
from dataclasses import replace

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QPushButton

from aftertrace.adapters.codex import CodexAdapter
from aftertrace.core.errors import AfterTraceError
from aftertrace.core.models import ProjectPrivacyState as Privacy
from aftertrace.services.database import open_database
from aftertrace.services.workspace import WorkspaceService
from aftertrace.ui.main_window import MainWindow
from aftertrace.ui.workspace import WorkspacePage
from tests.services.conftest import git_repository, write_session


def ready(qtbot, window):
    qtbot.waitUntil(lambda: not window.workspace_controller.busy)


def setup_window(qtbot, tmp_path, monkeypatch, *, with_session=True, adapter=None):
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    repo = git_repository(tmp_path / "project", "https://example.invalid/team/repo.git")
    if with_session:
        write_session(home / "sessions", repo)
    service = WorkspaceService(tmp_path / "ui.sqlite3", adapter=adapter)
    window = MainWindow(service=service)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(
        lambda: (
            bool(window.workspace_controller.state.sources) or not window.workspace_controller.busy
        )
    )
    # Process the scheduled metadata-only startup load before driving actions.
    qtbot.wait(20)
    ready(qtbot, window)
    pages = {page.objectName(): page for page in window.findChildren(WorkspacePage)}
    return window, service, pages


def create_location_mismatch(service):
    service.discover()
    (original,) = service.snapshot().projects
    service.set_privacy(original.project.id, Privacy.INCLUDED)
    identify = service.identify

    def distinct_identity(hint):
        identity = identify(hint)
        return replace(
            identity,
            canonical_identity="git-local:synthetic-distinct-repository",
            git_remote=None,
            kind="git_local",
            local_identity=None,
        )

    service.identify = distinct_identity
    service.discover()
    service.identify = identify
    other = next(
        view for view in service.snapshot().projects if view.project.id != original.project.id
    )
    (warning,) = service.snapshot().identity_warnings
    assert service.snapshot().identity_conflicts == ()
    return original.project.id, other.project.id, warning


def make_same_canonical_identity(service, project_id, other_id):
    with open_database(service.repository.path) as connection:
        canonical = connection.execute(
            "SELECT canonical_identity FROM projects WHERE id=?", (project_id,)
        ).fetchone()[0]
        connection.execute(
            "UPDATE projects SET canonical_identity=? WHERE id=?", (canonical, other_id)
        )


def test_sources_projects_scope_import_and_reload(qtbot, tmp_path, monkeypatch):
    window, service, pages = setup_window(qtbot, tmp_path, monkeypatch)
    source_page = pages["pageSources"]
    window.navigation.setCurrentRow(5)
    qtbot.mouseClick(source_page.discover_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: source_page.table.rowCount() == 1)
    ready(qtbot, window)
    assert source_page.table.item(0, 3).text() == "1"
    assert "Available" in source_page.table.item(0, 2).text()
    projects = pages["pageProjects"]
    window.navigation.setCurrentRow(3)
    assert projects.table.rowCount() == 1
    assert "Unclassified Sessions: 0" in projects.summary.text()
    combo = projects.table.cellWidget(0, 5)
    assert combo.currentData() == Privacy.EXCLUDED
    assert projects.import_button.isEnabled() is False
    combo.setCurrentIndex(0)
    qtbot.waitUntil(
        lambda: service.snapshot().projects[0].project.privacy_state is Privacy.INCLUDED
    )
    ready(qtbot, window)
    qtbot.mouseClick(projects.import_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: service.snapshot().projects[0].imported == 1)
    ready(qtbot, window)
    assert "1 imported" in projects.status.text()
    projects.controller.reload()
    ready(qtbot, window)
    assert projects.table.cellWidget(0, 5).currentData() == Privacy.INCLUDED
    projects.table.cellWidget(0, 5).setCurrentIndex(2)
    qtbot.waitUntil(
        lambda: service.snapshot().projects[0].project.privacy_state is Privacy.NEVER_ANALYZE
    )
    ready(qtbot, window)
    combo = projects.table.cellWidget(0, 5)
    assert "Never Analyze" in combo.currentText()
    assert "#ff9797" in combo.styleSheet()
    assert "removes" in combo.toolTip()
    assert service.snapshot().projects[0].imported == 0
    assert window.privacy_status.text() == "Local Only"


def test_zero_sessions_and_controlled_errors_keep_window_functional(qtbot, tmp_path, monkeypatch):
    class BrokenAdapter(CodexAdapter):
        def detect(self):
            raise AfterTraceError("private native failure", operation="detect")

    window, service, pages = setup_window(
        qtbot, tmp_path, monkeypatch, with_session=False, adapter=BrokenAdapter()
    )
    page = pages["pageSources"]
    assert page.table.rowCount() == 0
    window.navigation.setCurrentRow(5)
    qtbot.mouseClick(page.discover_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: "failed" in page.status.text())
    ready(qtbot, window)
    assert "private native failure" not in page.status.text()
    assert page.discover_button.isEnabled()
    window.navigation.setCurrentRow(0)
    assert window.pages.currentIndex() == 0


def test_projects_page_shows_path_only_identity_warning_without_actions(
    qtbot, tmp_path, monkeypatch
):
    window, service, pages = setup_window(qtbot, tmp_path, monkeypatch)
    original_id, other_id, warning = create_location_mismatch(service)
    window.navigation.setCurrentRow(3)
    window.workspace_controller.reload()
    ready(qtbot, window)
    page = pages["pageProjects"]
    assert page.table.rowCount() == 2
    assert not page.conflict_panel.isHidden()
    panel_text = [
        label.text() for label in page.conflict_panel.findChildren(type(page.explanation))
    ]
    assert any("Project identity mismatch" in text for text in panel_text)
    assert any("same location" in text and "Git identities differ" in text for text in panel_text)
    assert "1 project identity warning" in page.summary.text()
    assert "decision" not in page.summary.text().lower()
    assert service.snapshot().identity_conflicts == ()
    assert service.snapshot().identity_warnings[0].warning_id == warning.warning_id
    assert (
        service.snapshot().projects[0].project.privacy_state
        is not service.snapshot().projects[1].project.privacy_state
    )
    assert page.table.cellWidget(0, 5).isEnabled()
    assert page.table.cellWidget(1, 5).isEnabled()
    assert not any(
        button.objectName().startswith("resolve_") for button in page.findChildren(QPushButton)
    )
    assert {view.project.id for view in service.snapshot().projects} == {original_id, other_id}
    states = {view.project.id: view.project.privacy_state for view in service.snapshot().projects}
    service.discover()
    window.workspace_controller.reload()
    ready(qtbot, window)
    assert service.snapshot().identity_conflicts == ()
    assert len(service.snapshot().identity_warnings) == 1
    assert {
        view.project.id: view.project.privacy_state for view in service.snapshot().projects
    } == states


def test_proven_privacy_conflict_ui_offers_resolution_actions(qtbot, tmp_path, monkeypatch):
    window, service, pages = setup_window(qtbot, tmp_path, monkeypatch)
    original_id, other_id, _ = create_location_mismatch(service)
    make_same_canonical_identity(service, original_id, other_id)
    window.navigation.setCurrentRow(3)
    window.workspace_controller.reload()
    ready(qtbot, window)
    page = pages["pageProjects"]
    (conflict,) = service.snapshot().identity_conflicts
    assert conflict.match_basis == "canonical_identity"
    assert service.snapshot().identity_warnings == ()
    assert "privacy conflict" in page.summary.text()
    assert "require a decision" in page.summary.text()
    assert page.findChild(
        QPushButton, f"resolve_{conflict.conflict_id}_included"
    ) is not None
    assert page.findChild(
        QPushButton, f"resolve_{conflict.conflict_id}_excluded"
    ) is not None
    assert page.findChild(
        QPushButton, f"resolve_{conflict.conflict_id}_separate"
    ) is not None


def test_never_analyze_conflict_ui_only_offers_keep_separate(qtbot, tmp_path, monkeypatch):
    window, service, pages = setup_window(qtbot, tmp_path, monkeypatch)
    original_id, other_id, _ = create_location_mismatch(service)
    make_same_canonical_identity(service, original_id, other_id)
    service.set_privacy(other_id, Privacy.NEVER_ANALYZE)
    window.navigation.setCurrentRow(3)
    window.workspace_controller.reload()
    ready(qtbot, window)
    page = pages["pageProjects"]
    (conflict,) = service.snapshot().identity_conflicts
    assert conflict.can_merge_without_purging is False
    action_names = {
        button.objectName()
        for button in page.findChildren(QPushButton)
        if button.objectName().startswith(f"resolve_{conflict.conflict_id}_")
    }
    assert action_names == {f"resolve_{conflict.conflict_id}_separate"}
    panel_text = [
        label.text() for label in page.conflict_panel.findChildren(type(page.explanation))
    ]
    assert any("Never Analyze cannot be merged" in text for text in panel_text)

    button = page.findChild(QPushButton, next(iter(action_names)))
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not service.snapshot().identity_conflicts)
    ready(qtbot, window)
    assert len(service.snapshot().projects) == 2
    assert Privacy.NEVER_ANALYZE in {
        view.project.privacy_state for view in service.snapshot().projects
    }


def test_import_runs_outside_ui_thread_and_shutdown_waits_for_completion(
    qtbot, tmp_path, monkeypatch
):
    window, service, pages = setup_window(qtbot, tmp_path, monkeypatch)
    service.discover()
    service.set_privacy(service.snapshot().projects[0].project.id, Privacy.INCLUDED)
    window.workspace_controller.reload()
    ready(qtbot, window)
    entered, release = threading.Event(), threading.Event()
    original = service.adapter.load_raw_session

    def pause(reference):
        entered.set()
        assert release.wait(3)
        return original(reference)

    monkeypatch.setattr(service.adapter, "load_raw_session", pause)
    window.workspace_controller.import_included()
    qtbot.waitUntil(entered.is_set)
    heartbeat = []
    QTimer.singleShot(0, lambda: heartbeat.append(True))
    qtbot.waitUntil(lambda: bool(heartbeat))
    assert window.navigation.isEnabled()
    assert not pages["pageProjects"].import_button.isEnabled()
    window.close()  # Close is deferred, without blocking the UI or destroying a running thread.
    assert window.isVisible()
    release.set()
    qtbot.waitUntil(lambda: not window.isVisible())
    with open_database(service.repository.path) as connection:
        assert connection.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
