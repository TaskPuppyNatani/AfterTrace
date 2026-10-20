"""The shared QThread task pattern; result handoff stays on the owning Qt thread."""

from collections.abc import Callable

from PySide6.QtCore import QObject, QThread, Signal

from aftertrace.core.errors import AfterTraceError


class Job(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, action: Callable[[], object], parent: QObject):
        super().__init__(parent)
        self.action = action

    def run(self):
        try:
            result = self.action()
        except AfterTraceError as error:
            self.failed.emit(f"{error.operation}: Operation failed. You can retry.")
        except Exception:
            self.failed.emit("Operation failed. You can retry.")
        else:
            self.completed.emit(result)
