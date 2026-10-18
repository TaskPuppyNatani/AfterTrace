import threading

from PySide6.QtCore import Qt, QTimer

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
