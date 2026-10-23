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

    def resolve_project_privacy_conflict(self, conflict, keep_state) -> None:
        self.run(
            lambda: self.service.resolve_project_privacy_conflict(conflict, keep_state)
        )

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
        self.conflict_panel = QWidget()
        self.conflict_panel.setObjectName("projectPrivacyConflicts")
        self.conflict_layout = QVBoxLayout(self.conflict_panel)
        self.conflict_layout.setContentsMargins(0, 0, 0, 0)
        self.conflict_layout.setSpacing(8)
        self.conflict_action_buttons = []
        self.conflict_panel.hide()
        if projects:
            layout.addWidget(self.conflict_panel)
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
            conflict_count = len(state.identity_conflicts)
            conflict_summary = (
                f" · {conflict_count} privacy conflict{'s' if conflict_count != 1 else ''} "
                "require a decision"
                if conflict_count
                else ""
            )
            warning_count = len(state.identity_warnings)
            warning_summary = (
                f" · {warning_count} project identity warning"
                f"{'s' if warning_count != 1 else ''}"
                if warning_count
                else ""
            )
            self.summary.setText(
                f"{len(rows)} projects · Unclassified Sessions: {state.unclassified} "
                f"(excluded from import){conflict_summary}{warning_summary}"
            )
            self._render_conflicts(state.identity_conflicts, state.identity_warnings)
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
                unresolved_ids = {
                    project_id
                    for conflict in state.identity_conflicts
                    for project_id in conflict.project_ids
                }
                combo.setEnabled(view.project.id not in unresolved_ids)
                combo.currentIndexChanged.connect(
                    lambda index, project_id=view.project.id, control=combo: (
                        self.controller.set_privacy(project_id, control.itemData(index))
                    )
                )
                self.table.setCellWidget(index, 5, combo)
        self.table.resizeRowsToContents()
        self.set_busy(self.controller.busy)

    def _render_conflicts(self, conflicts, warnings):
        while self.conflict_layout.count():
            item = self.conflict_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.conflict_action_buttons = []
        for warning in warnings:
            heading = _label("Project identity mismatch")
            heading.setObjectName(f"projectIdentityWarning_{warning.warning_id}")
            self.conflict_layout.addWidget(heading)
            self.conflict_layout.addWidget(
                _label(
                    "Two project records use the same location, but their Git identities differ. "
                    "AfterTrace is keeping them separate because a shared path alone does not "
                    "prove they are the same repository."
                )
            )
            self.conflict_layout.addWidget(_label(f"Location: {warning.location}"))
            for index, name in enumerate(warning.project_names):
                privacy = warning.privacy_states[index].value.replace("_", " ").title()
                self.conflict_layout.addWidget(
                    _label(
                        f"{name} · {warning.discovered_sessions[index]} discovered · "
                        f"{warning.imported_sessions[index]} imported · {privacy}"
                    )
                )
        for conflict in conflicts:
            heading = _label("Privacy conflict · explicit choice required")
            heading.setObjectName(f"privacyConflictTitle_{conflict.conflict_id}")
            self.conflict_layout.addWidget(heading)
            self.conflict_layout.addWidget(
                _label(
                    "Current Git identity evidence matches these records, but their privacy "
                    "settings differ. Choose the policy to use before reconciling them."
                )
            )
            self.conflict_layout.addWidget(_label(f"Location: {conflict.location}"))
            for index, name in enumerate(conflict.project_names):
                privacy = conflict.privacy_states[index].value.replace("_", " ").title()
                self.conflict_layout.addWidget(
                    _label(
                        f"{name} · {privacy} · {conflict.discovered_sessions[index]} discovered · "
                        f"{conflict.imported_sessions[index]} imported"
                    )
                )
            actions = QHBoxLayout()
            if conflict.can_merge_without_purging:
                for title, privacy in (
                    ("Keep Excluded", ProjectPrivacyState.EXCLUDED),
                    ("Keep Included", ProjectPrivacyState.INCLUDED),
                ):
                    button = QPushButton(title)
                    button.setObjectName(
                        f"resolve_{conflict.conflict_id}_{privacy.value}"
                    )
                    button.clicked.connect(
                        lambda _checked=False, current=conflict, state=privacy: (
                            self.controller.resolve_project_privacy_conflict(current, state)
                        )
                    )
                    actions.addWidget(button)
                    self.conflict_action_buttons.append(button)
            else:
                self.conflict_layout.addWidget(
                    _label(
                        "Never Analyze cannot be merged without weakening its privacy "
                        "guarantee or purging retained history. Keep these records separate."
                    )
                )
            separate = QPushButton("Keep Separate")
            separate.setObjectName(f"resolve_{conflict.conflict_id}_separate")
            separate.clicked.connect(
                lambda _checked=False, current=conflict: (
                    self.controller.resolve_project_privacy_conflict(current, None)
                )
            )
            actions.addWidget(separate)
            self.conflict_action_buttons.append(separate)
            actions.addStretch()
            self.conflict_layout.addLayout(actions)
        self.conflict_panel.setVisible(bool(conflicts or warnings))

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
        for button in self.conflict_action_buttons:
            button.setEnabled(not busy)
