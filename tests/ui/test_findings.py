import ast
import inspect
import threading

import pytest
from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from aftertrace.core.errors import AfterTraceError
from aftertrace.core.models import FindingState, ProjectPrivacyState
from aftertrace.services.database import open_database
from aftertrace.services.findings import FindingsService
from aftertrace.services.repository import WorkspaceRepository
from aftertrace.services.workspace import WorkspaceService
from aftertrace.ui.findings import FindingsPage
from aftertrace.ui.main_window import MainWindow
from tests.services.test_findings import history as history


def texts(widget):
    return "\n".join(label.text() for label in widget.findChildren(QLabel))


@pytest.fixture
def make_window(qtbot, history):
    repository, _ = history
    windows = []

    def create(*, publish=True, repo=None, navigate=True):
        selected = repo or repository
        if publish:
            FindingsService(selected).analyze()
        window = MainWindow(service=WorkspaceService(selected.path))
        windows.append(window)
        qtbot.addWidget(window)
        with qtbot.waitSignal(window.workspace_controller.idle, timeout=3000):
            window.show()
        page = window.findChild(FindingsPage)
        if navigate:
            with qtbot.waitSignal(window.findings_controller.idle, timeout=3000):
                window.navigation.setCurrentRow(1)
        return window, page

    yield create
    for window in windows:
        window.close()
        qtbot.waitUntil(
            lambda target=window: not target.workspace_controller.busy
            and not target.findings_controller.busy,
            timeout=5000,
        )
        window.close()


def test_empty_states_distinguish_missing_history_from_insufficient_repeats(
    make_window, tmp_path, qtbot
):
    window, page = make_window(repo=WorkspaceRepository(tmp_path / "empty.sqlite3"), publish=False)
    assert "No imported history" in page.empty.text()
    assert "Projects" in page.empty.text()
    assert page.analyze_button.isEnabled()
    with qtbot.waitSignal(window.findings_controller.idle):
        qtbot.mouseClick(page.analyze_button, Qt.MouseButton.LeftButton)
    assert "No imported history" in page.empty.text()
    _, imported = make_window(publish=False)
    assert "No repeated instructions met" in imported.empty.text()
    assert "3 distinct sessions" in imported.empty.text()


def test_persisted_current_findings_render_without_startup_analysis(
    make_window, history, monkeypatch
):
    repository, _ = history
    (finding,) = FindingsService(repository).analyze().findings
    repository.update_finding_state(finding.id, FindingState.SAVED)

    def forbidden(*args):
        pytest.fail("Startup/navigation/refresh must not run analysis.")

    monkeypatch.setattr("aftertrace.services.findings.FindingPublicationService.run", forbidden)
    window, page = make_window(publish=False)
    rendered = texts(page)
    assert "Repeated Instruction" in rendered
    assert "Run the tests." in rendered
    assert "LOW CONFIDENCE" in rendered
    assert "State: Saved" in rendered
    assert "3 occurrences · 3 sessions · 1 project" in rendered
    assert "Codex · 1 source" in rendered
    assert "2026-01-03" in rendered
    assert len(page.findChildren(QPushButton, "viewEvidence")) == 1
    assert window.pages.currentWidget().widget() is page


def test_persisted_loading_state_is_responsive_and_read_runs_in_worker(
    make_window, qtbot, monkeypatch
):
    window, page = make_window(navigate=False)
    service = window.findings_controller.service
    snapshot = service.snapshot
    entered, release = threading.Event(), threading.Event()
    worker_threads = []

    def paused():
        worker_threads.append(QThread.currentThread() != QApplication.instance().thread())
        entered.set()
        assert release.wait(3)
        return snapshot()

    monkeypatch.setattr(service, "snapshot", paused)
    try:
        window.navigation.setCurrentRow(1)
        qtbot.waitUntil(entered.is_set)
        assert "Loading persisted findings" in page.status.text()
        assert page.cards.count() == 0
        assert window.navigation.isEnabled()
    finally:
        release.set()
    qtbot.waitUntil(lambda: not window.findings_controller.busy)
    assert worker_threads == [True]
    assert "Run the tests." in texts(page)


def test_unsupported_findings_stay_persisted_but_are_hidden_in_ui(make_window, history):
    repository, _ = history
    service = FindingsService(repository)
    (finding,) = service.analyze().findings
    repository.set_privacy("p", ProjectPrivacyState.EXCLUDED)
    service.analyze()
    _, page = make_window(publish=False)
    assert "Run the tests." not in texts(page)
    assert page.cards.count() == 0
    assert repository.findings()[0].id == finding.id


def test_evidence_is_on_demand_original_plain_text_with_metadata_and_provenance(
    make_window, history, qtbot, monkeypatch
):
    repository, _ = history
    original_text = "RUN   THE TESTS!"
    with open_database(repository.path) as connection:
        connection.execute("UPDATE messages SET content = ?", (original_text,))
        connection.execute("UPDATE projects SET display_name = 'Example project'")
        connection.execute("UPDATE sources SET display_name = 'Example source'")
        connection.execute("UPDATE sessions SET title = 'Example session'")
    window, page = make_window(navigate=False)
    service = window.findings_controller.service
    detail = service.detail
    calls = []

    def tracked(identity):
        calls.append(identity)
        return detail(identity)

    def forbidden(*args):
        pytest.fail("Findings must not read raw histories.")

    monkeypatch.setattr(service, "detail", tracked)
    for name in ("detect", "discover_sessions", "load_raw_session", "normalize_session"):
        monkeypatch.setattr(window.workspace_controller.service.adapter, name, forbidden)
    with qtbot.waitSignal(window.findings_controller.idle):
        window.navigation.setCurrentRow(1)
    assert calls == []
    with qtbot.waitSignal(window.findings_controller.idle):
        qtbot.mouseClick(page.findChild(QPushButton, "viewEvidence"), Qt.MouseButton.LeftButton)
    dialog = page.dialog
    assert dialog.isVisible() and len(calls) == 1
    assert len(dialog.findChildren(QLabel, "originalEvidence")) == 3
    for label in dialog.findChildren(QLabel, "originalEvidence"):
        assert label.text() == original_text
        assert label.textFormat() is Qt.TextFormat.PlainText
    content = texts(dialog)
    for expected in (
        "Example project",
        "Example source",
        "Codex",
        "Example session",
        "YOU",
        "AfterTrace deterministic engine",
        "repeated_instruction:v1",
        "Model used: None",
    ):
        assert expected in content
    assert repository.findings()[0].state is FindingState.NEW


def test_analysis_is_explicit_off_thread_and_duplicate_requests_are_guarded(
    make_window, qtbot, monkeypatch
):
    window, page = make_window(publish=False)
    service = window.findings_controller.service
    original = service.publisher.run
    entered, release = threading.Event(), threading.Event()
    calls, worker_threads = [], []

    def paused():
        calls.append(True)
        worker_threads.append(QThread.currentThread() != QApplication.instance().thread())
        entered.set()
        assert release.wait(3)
        return original()

    monkeypatch.setattr(service.publisher, "run", paused)
    try:
        qtbot.mouseClick(page.analyze_button, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(entered.is_set)
        assert "Analyzing imported history" in page.status.text()
        assert not page.analyze_button.isEnabled()
        window.findings_controller.analyze()
        heartbeat = []
        QTimer.singleShot(0, lambda: heartbeat.append(True))
        qtbot.waitUntil(lambda: bool(heartbeat))
        assert window.navigation.isEnabled()
    finally:
        release.set()
    qtbot.waitUntil(lambda: not window.findings_controller.busy)
    assert calls == [True] and worker_threads == [True]
    assert "Run the tests." in texts(page)
    assert page.analyze_button.isEnabled()


def test_analysis_failure_preserves_cards_and_hides_private_errors(make_window, qtbot, monkeypatch):
    window, page = make_window()
    before = texts(page)

    def fail():
        raise AfterTraceError("PRIVATE_SYNTHETIC_FAILURE", operation="publish_findings")

    monkeypatch.setattr(window.findings_controller.service.publisher, "run", fail)
    with qtbot.waitSignal(window.findings_controller.idle):
        qtbot.mouseClick(page.analyze_button, Qt.MouseButton.LeftButton)
    assert "Run the tests." in texts(page) and "Run the tests." in before
    assert "Your existing findings are still available" in page.status.text()
    assert "PRIVATE_SYNTHETIC_FAILURE" not in texts(page)
    assert page.analyze_button.isEnabled()


def test_refresh_only_reads_persisted_data_and_keeps_user_state(make_window, qtbot, monkeypatch):
    window, page = make_window()
    service = window.findings_controller.service
    (finding,) = service.repository.findings()
    service.repository.update_finding_state(finding.id, FindingState.APPLIED)

    def forbidden(*args):
        pytest.fail("Refresh must not analyze, discover, or import.")

    monkeypatch.setattr(service.publisher, "run", forbidden)
    monkeypatch.setattr(window.workspace_controller.service, "discover", forbidden)
    monkeypatch.setattr(window.workspace_controller.service, "import_included", forbidden)
    with qtbot.waitSignal(window.findings_controller.idle):
        qtbot.mouseClick(page.refresh_button, Qt.MouseButton.LeftButton)
    assert "State: Applied" in texts(page)
    assert service.repository.findings()[0].state is FindingState.APPLIED


@pytest.mark.parametrize("remaining", [0, 3])
def test_privacy_refresh_clears_old_widgets_and_shows_only_remaining_receipts(
    make_window, history, qtbot, remaining
):
    repository, add = history
    private = "RUN   THE TESTS!"
    with open_database(repository.path) as connection:
        connection.execute("UPDATE messages SET content = ?", (private,))
    for number in range(remaining):
        add(f"b{number}", project="other")
    window, page = make_window()
    with qtbot.waitSignal(window.findings_controller.idle):
        page.open_evidence(repository.findings()[0].id)
    labels = page.dialog.findChildren(QLabel, "originalEvidence")
    assert any(label.text() == private for label in labels)
    repository.set_privacy("p", ProjectPrivacyState.NEVER_ANALYZE)
    window.findings_controller.reload()
    assert page.dialog is None
    assert all(label.text() == "" for label in labels)
    qtbot.waitUntil(lambda: not window.findings_controller.busy)
    assert private not in texts(page)
    if remaining:
        with qtbot.waitSignal(window.findings_controller.idle):
            page.open_evidence(repository.findings()[0].id)
        assert private not in texts(page.dialog)
        assert len(page.dialog.findChildren(QLabel, "originalEvidence")) == remaining
    else:
        assert "No imported history" in page.empty.text()


def test_late_evidence_result_cannot_restore_private_widgets_after_refresh(
    make_window, history, qtbot, monkeypatch
):
    repository, _ = history
    window, page = make_window()
    service = window.findings_controller.service
    detail = service.detail
    entered, release = threading.Event(), threading.Event()

    def paused(identity):
        result = detail(identity)
        entered.set()
        assert release.wait(3)
        return result

    monkeypatch.setattr(service, "detail", paused)
    try:
        page.open_evidence(repository.findings()[0].id)
        qtbot.waitUntil(entered.is_set)
        assert "Loading evidence" in page.dialog.status.text()
        window.findings_controller.reload()
        repository.set_privacy("p", ProjectPrivacyState.NEVER_ANALYZE)
    finally:
        release.set()
    qtbot.waitUntil(lambda: not window.findings_controller.busy)
    assert page.dialog is None
    assert "Run the tests." not in texts(page)
    assert "No imported history" in page.empty.text()


def test_read_failure_is_safe_and_refresh_can_retry(make_window, qtbot, monkeypatch):
    window, page = make_window()
    service = window.findings_controller.service
    snapshot = service.snapshot

    def fail():
        raise RuntimeError("PRIVATE_DATABASE_FAILURE")

    monkeypatch.setattr(service, "snapshot", fail)
    with qtbot.waitSignal(window.findings_controller.idle):
        page.refresh_button.click()
    assert "Findings could not be loaded" in page.status.text()
    assert "PRIVATE_DATABASE_FAILURE" not in texts(page)
    assert page.cards.count() == 0
    monkeypatch.setattr(service, "snapshot", snapshot)
    with qtbot.waitSignal(window.findings_controller.idle):
        page.refresh_button.click()
    assert "Run the tests." in texts(page)


def test_missing_evidence_and_query_failure_are_meaningful_states(
    make_window, history, qtbot, monkeypatch
):
    repository, _ = history
    window, page = make_window()
    (finding,) = repository.findings()

    def fail(_identity):
        raise RuntimeError("PRIVATE_EVIDENCE_FAILURE")

    with monkeypatch.context() as patch:
        patch.setattr(window.findings_controller.service, "detail", fail)
        with qtbot.waitSignal(window.findings_controller.idle):
            page.open_evidence(finding.id)
        assert "could not be loaded" in page.dialog.status.text()
        assert "PRIVATE_EVIDENCE_FAILURE" not in texts(page.dialog)
    page.close_evidence()
    repository.set_privacy("p", ProjectPrivacyState.NEVER_ANALYZE)
    with qtbot.waitSignal(window.findings_controller.idle):
        page.open_evidence(finding.id)
    assert "no longer available" in page.dialog.status.text()
    assert page.dialog.findChildren(QLabel, "originalEvidence") == []


def test_close_defers_until_analysis_finishes_without_blocking_events(
    make_window, qtbot, monkeypatch
):
    window, page = make_window(publish=False)
    service = window.findings_controller.service
    analyze = service.analyze
    entered, release = threading.Event(), threading.Event()

    def paused():
        entered.set()
        assert release.wait(3)
        return analyze()

    monkeypatch.setattr(service, "analyze", paused)
    try:
        page.analyze_button.click()
        qtbot.waitUntil(entered.is_set)
        window.close()
        assert window.isVisible()
        heartbeat = []
        QTimer.singleShot(0, lambda: heartbeat.append(True))
        qtbot.waitUntil(lambda: bool(heartbeat))
    finally:
        release.set()
    qtbot.waitUntil(lambda: not window.isVisible())
    assert len(service.repository.findings()) == 1


def test_findings_widgets_have_no_sql_adapter_provider_or_analyzer_calls():
    from aftertrace.ui import findings

    tree = ast.parse(inspect.getsource(findings))
    modules = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not any(
        name.startswith(("aftertrace.adapters", "aftertrace.providers", "aftertrace.analysis"))
        for name in modules
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr
        in (
            "execute",
            "executescript",
            "load_raw_session",
            "normalize_session",
            "publish_repeated_findings",
        )
        for node in ast.walk(tree)
    )
