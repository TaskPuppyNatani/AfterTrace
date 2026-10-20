import os
import subprocess
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPixmap

from aftertrace.ui.findings import FindingsPage
from aftertrace.ui.main_window import DESTINATIONS, MainWindow


def test_navigation_privacy_and_missing_branding(qtbot, tmp_path):
    window = MainWindow(logo_path=tmp_path / "missing.png")
    qtbot.addWidget(window)
    window.show()
    assert window.isVisible()
    assert [window.navigation.item(i).text() for i in range(window.navigation.count())] == list(
        DESTINATIONS
    )
    assert window.privacy_status.text() == "Local Only"
    assert window.privacy_status.isVisible()
    assert window.publisher_logo.pixmap().isNull()
    for index, name in enumerate(DESTINATIONS):
        item = window.navigation.item(index)
        qtbot.mouseClick(
            window.navigation.viewport(),
            Qt.MouseButton.LeftButton,
            pos=window.navigation.visualItemRect(item).center(),
        )
        assert window.pages.currentIndex() == index
        assert window.pages.currentWidget().widget().objectName() == f"page{name}"
        assert window.privacy_status.isVisible()
    assert isinstance(window.pages.widget(1).widget(), FindingsPage)
    qtbot.waitUntil(
        lambda: not window.workspace_controller.busy and not window.findings_controller.busy
    )


def test_optional_branding_loads(qtbot, tmp_path):
    path = tmp_path / "logo.png"
    pixmap = QPixmap(40, 40)
    pixmap.fill(QColor("#51d6b0"))
    assert pixmap.save(str(path))
    window = MainWindow(logo_path=path)
    qtbot.addWidget(window)
    window.show()
    assert not window.publisher_logo.pixmap().isNull()
    assert window.publisher_logo.isVisible()


def test_gui_entry_point_runs_event_loop(tmp_path):
    # Quitting a shared pytest QApplication interferes with later queued worker signals.
    # Exercise the real entry function and event-loop shutdown in its own process.
    script = """
import sys
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from aftertrace.services import workspace
from aftertrace.ui import app as gui_app
workspace.database_path = lambda: Path(sys.argv[1])
app = QApplication([])
QTimer.singleShot(100, app.quit)
raise SystemExit(gui_app.main())
"""
    subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "entry.sqlite3")],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        check=True,
        timeout=10,
        capture_output=True,
        text=True,
    )
