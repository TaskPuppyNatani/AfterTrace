"""GUI composition root. Opening the shell does not ingest data or contact providers."""

import sys

from PySide6.QtWidgets import QApplication

from aftertrace.ui.main_window import MainWindow


def main() -> int:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    app.setApplicationName("AfterTrace")
    app.setOrganizationName("TaskPuppy Kreations")
    window = MainWindow()
    window.show()
    result = app.exec()
    window.workspace_controller.finish_background_work()
    window.findings_controller.finish_background_work()
    return result
