"""Metadata-only Sources/Projects views with asynchronous application-service calls."""

from collections.abc import Callable

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from aftertrace.core.models import ProjectPrivacyState
from aftertrace.services.state import WorkspaceSnapshot
from aftertrace.services.workspace import WorkspaceService
from aftertrace.ui.jobs import Job

SCOPES = (
    ("Included", ProjectPrivacyState.INCLUDED, "#50e5b3"),
    ("Excluded", ProjectPrivacyState.EXCLUDED, "#b3bed2"),
    ("Never Analyze", ProjectPrivacyState.NEVER_ANALYZE, "#ff9797"),
)


class WorkspaceController(QObject):
    changed = Signal(object)
    notice = Signal(str)
    busy_changed = Signal(bool)
    idle = Signal()

    def __init__(self, service: WorkspaceService, parent: QObject):
        super().__init__(parent)
        self.service = service
        self.state = WorkspaceSnapshot()
        self.job: Job | None = None

    @property
    def busy(self) -> bool:
        return self.job is not None

    def run(self, action: Callable[[], str]) -> None:
        if self.busy:
            return
        def work():
            notice = action()
            return self.service.snapshot(), notice

        self.job = Job(work, self)
        self.job.completed.connect(self._completed)
        self.job.failed.connect(self._failed)
        self.job.finished.connect(self._finished)
        self.busy_changed.emit(True)
        self.notice.emit("Working… Local Only. No analysis runs.")
        self.job.start()

    def reload(self) -> None:
        self.run(lambda: "")

    def discover(self) -> None:
        self.run(self.service.discover)

    def import_included(self) -> None:
        self.run(lambda: self.service.import_included().summary)

    def set_privacy(self, project_id, state) -> None:
        self.run(lambda: self.service.set_privacy(project_id, state))

    def _completed(self, result):
        state, notice = result
        self.state = state
        self.changed.emit(state)
        self.notice.emit(notice or "Saved workspace metadata loaded.")

    def _failed(self, message):
        self.changed.emit(self.state)  # Restore controls to the last durable scope snapshot.
        self.notice.emit(message)

    def _finished(self):
        job = self.job
        self.job = None
        job.deleteLater()
        self.busy_changed.emit(False)
        self.idle.emit()

    def finish_background_work(self):
        if self.job is not None:
            self.job.wait()  # Only for event-loop shutdown, not interactive operations.


def _label(text: str) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class WorkspacePage(QWidget):
    def __init__(self, controller: WorkspaceController, *, projects: bool):
        super().__init__()
        self.controller, self.projects = controller, projects
        name = "Projects" if projects else "Sources"
        self.setObjectName(f"page{name}")
        self.setStyleSheet("""
            QPushButton, QComboBox { background: #20283a; border: 1px solid #444e68;
                border-radius: 5px; padding: 8px; color: #edf0fa; }
            QPushButton:hover { border-color: #b894ff; }
            QPushButton:disabled, QComboBox:disabled { color: #7c879d; }
            QHeaderView::section { background: #1c2332; color: #bfcbe0; padding: 8px; border: 0; }
            QTableWidget { background: #141a25; gridline-color: #30394d; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(16)
        heading = _label(name)
        heading.setObjectName("title")
        layout.addWidget(heading)
        text = (
            "Discovery only records metadata. Include a project, then explicitly import it. "
            "Never Analyze prevents importing or retaining session content and removes existing "
            "AfterTrace copies. Original histories remain read-only."
            if projects
            else "Discover Codex history metadata. Discovery never imports transcripts. "
            "Claude Code and OpenCode parsing are not implemented."
        )
        self.explanation = _label(text)
        layout.addWidget(self.explanation)
        actions = QHBoxLayout()
        self.discover_button = QPushButton(
            "Discover Codex" if not projects else "Refresh discovery"
        )
        self.discover_button.setObjectName(f"discover{name}")
        self.discover_button.clicked.connect(controller.discover)
        actions.addWidget(self.discover_button)
        self.reload_button = QPushButton("Reload saved state")
        self.reload_button.clicked.connect(controller.reload)
        actions.addWidget(self.reload_button)
        self.import_button = QPushButton("Import Included Projects")
        self.import_button.setObjectName("importIncluded")
        self.import_button.clicked.connect(controller.import_included)
        if projects:
            actions.addWidget(self.import_button)
        else:
            self.import_button.hide()
        actions.addStretch()
        layout.addLayout(actions)
        self.summary = _label("No discovery metadata yet.")
        self.summary.setObjectName(f"summary{name}")
        layout.addWidget(self.summary)
        self.table = QTableWidget()
        self.table.setObjectName(f"table{name}")
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["Project", "Known locations", "Harness", "Discovered", "Imported", "Privacy"]
            if projects
            else ["Source", "Location", "Status", "Discovered", "Imported", "Last discovery"]
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        self.status = _label("Local Only · No analysis runs after import.")
        self.status.setObjectName(f"operationStatus{name}")
        layout.addWidget(self.status)
        controller.changed.connect(self.render)
        controller.notice.connect(self.status.setText)
        controller.busy_changed.connect(self.set_busy)
        self.render(controller.state)

    def render(self, state: WorkspaceSnapshot):
        rows = state.projects if self.projects else state.sources
        self.table.setRowCount(0)
        self.table.setRowCount(len(rows))
        if self.projects:
            self.summary.setText(
                f"{len(rows)} projects · Unclassified Sessions: {state.unclassified} "
                "(excluded from import)"
            )
        else:
            self.summary.setText(
                f"{len(rows)} recorded Codex sources. No sources detected yet."
                if not rows
                else f"{sum(source.available for source in rows)} Codex sources available."
            )
        for index, view in enumerate(rows):
            if self.projects:
                values = [
                    view.project.display_name,
                    "\n".join(view.locations),
                    ", ".join(h.value for h in view.harnesses),
                    str(view.discovered),
                    str(view.imported),
                ]
            else:
                source = view.source
                values = [
                    source.display_name,
                    source.location,
                    view.warning or ("Available" if view.available else "Not available"),
                    str(view.discovered),
                    str(view.imported),
                    source.last_scan_at.isoformat(timespec="seconds")
                    if source.last_scan_at
                    else "Not yet discovered",
                ]
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(value))
            if self.projects:
                combo = QComboBox()
                combo.setObjectName(f"scope_{view.project.id}")
                for title, scope, _ in SCOPES:
                    combo.addItem(title, scope)
                selected = next(
                    i
                    for i, (_, scope, _) in enumerate(SCOPES)
                    if scope is view.project.privacy_state
                )
                combo.setCurrentIndex(selected)
                combo.setStyleSheet(f"QComboBox {{ color: {SCOPES[selected][2]}; }}")
                combo.setToolTip(
                    "Never Analyze removes imported copies and prevents retaining session content."
                )
                combo.currentIndexChanged.connect(
                    lambda index, project_id=view.project.id, control=combo: (
                        self.controller.set_privacy(project_id, control.itemData(index))
                    )
                )
                self.table.setCellWidget(index, 5, combo)
        self.table.resizeRowsToContents()
        self.set_busy(self.controller.busy)

    def set_busy(self, busy: bool):
        self.discover_button.setEnabled(not busy)
        self.reload_button.setEnabled(not busy)
        self.import_button.setEnabled(
            not busy
            and any(
                view.project.privacy_state is ProjectPrivacyState.INCLUDED and view.discovered
                for view in self.controller.state.projects
            )
        )
        self.table.setEnabled(not busy)
