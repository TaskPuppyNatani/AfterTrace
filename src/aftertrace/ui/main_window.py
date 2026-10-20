"""Thin desktop shell: navigation and honest foundation empty states."""

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from aftertrace import __version__
from aftertrace.platform.paths import publisher_logo_path
from aftertrace.services.findings import FindingsService
from aftertrace.services.workspace import WorkspaceService
from aftertrace.ui.findings import FindingsController, FindingsPage
from aftertrace.ui.workspace import WorkspaceController, WorkspacePage

DESTINATIONS = (
    "Overview",
    "Findings",
    "Instructions",
    "Projects",
    "Sessions",
    "Sources",
    "Settings",
)

_PAGE_COPY = {
    "Instructions": (
        "Turn recurring instructions into reviewable rules.",
        "No instruction drafts yet",
        "Active instructions, drafts, and applied history will live here. "
        "Instruction changes will require a diff and your confirmation.",
    ),
    "Sessions": (
        "Source material for evidence-backed findings.",
        "Session details are not available yet",
        "Projects shows imported session counts. A session viewer is deferred. "
        "Original harness histories stay read-only.",
    ),
    "Settings": (
        "A local-first workspace.",
        "Local Only",
        "No model connections or outbound processing are enabled. "
        "Provider configuration and privacy controls will arrive in a later milestone.",
    ),
}

_STYLES = """
QMainWindow, QWidget { background: #0c1017; color: #edf0fa; font-size: 14px; }
QFrame#sidebar { background: #10141e; border-right: 1px solid #252c3c; }
QLabel { background: transparent; }
QLabel#wordmark { font-size: 30px; font-weight: 700; color: #d9c9ff; }
QLabel#eyebrow { color: #6ddad0; font-size: 11px; font-weight: 600; }
QLabel#muted { color: #a8b2c9; }
QLabel#title { font-size: 30px; font-weight: 600; }
QLabel#cardTitle { font-size: 19px; font-weight: 600; }
QLabel#stat { font-size: 28px; font-weight: 600; }
QLabel#privacy { color: #50e5b3; border: 1px solid #285347;
    border-radius: 8px; padding: 8px 15px; background: #10251f; }
QLabel#badge { color: #b6a2e9; border: 1px solid #38314f;
    border-radius: 5px; padding: 5px 10px; }
QFrame#card { background: #141a25; border: 1px solid #2a3244; border-radius: 12px; }
QListWidget { background: transparent; border: 0; outline: 0; font-size: 15px; }
QListWidget::item { padding: 14px 16px; margin: 3px 0; border-radius: 7px; }
QListWidget::item:selected { background: #30234e; color: #f4edff; border-left: 3px solid #b894ff; }
QListWidget::item:hover:!selected { background: #1a2230; }
QScrollArea { border: 0; }
QStatusBar { color: #a8b2c9; border-top: 1px solid #252c3c; }
"""


def _label(text: str, name: str = "", *, wrap: bool = True) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setObjectName(name)
    label.setWordWrap(wrap)
    return label


def _card(title: str, body: str) -> QFrame:
    card = QFrame()
    card.setObjectName("card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(24, 24, 24, 24)
    layout.setSpacing(12)
    layout.addWidget(_label(title, "cardTitle"))
    layout.addWidget(_label(body, "muted"))
    return card


class MainWindow(QMainWindow):
    def __init__(
        self, *, logo_path: Path | None = None, service: WorkspaceService | None = None
    ) -> None:
        super().__init__()
        self.setWindowTitle("AfterTrace")
        self.resize(1240, 820)
        self.setMinimumSize(900, 620)
        self.setStyleSheet(_STYLES)
        self.workspace_controller = WorkspaceController(service or WorkspaceService(), self)
        self.findings_controller = FindingsController(
            FindingsService(self.workspace_controller.service.repository), self
        )
        self._close_pending = False
        self.workspace_controller.idle.connect(self._close_when_idle)
        self.findings_controller.idle.connect(self._close_when_idle)
        self.workspace_controller.busy_changed.connect(self.findings_controller.set_workspace_busy)
        self.workspace_controller.idle.connect(self._refresh_findings_if_visible)
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setCentralWidget(root)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(236)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(20, 28, 20, 20)
        side.setSpacing(14)
        side.addWidget(_label("AfterTrace", "wordmark"))
        side.addWidget(_label("TRACE  /  DISCOVER  /  RECOMMEND", "eyebrow"))
        self.navigation = QListWidget()
        self.navigation.setAccessibleName("Primary navigation")
        self.navigation.addItems(DESTINATIONS)
        side.addWidget(self.navigation, 1)
        self.publisher_logo = QLabel()
        self.publisher_logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        resolved_logo = logo_path if logo_path is not None else publisher_logo_path()
        pixmap = QPixmap(str(resolved_logo)) if resolved_logo is not None else QPixmap()
        if not pixmap.isNull():
            self.publisher_logo.setPixmap(
                pixmap.scaled(
                    144,
                    144,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            self.publisher_logo.setAccessibleName("TaskPuppy Kreations publisher logo")
            side.addWidget(self.publisher_logo)
        side.addWidget(_label("TaskPuppy Kreations", "muted"))
        side.addWidget(_label(f"AfterTrace {__version__}", "muted"))
        layout.addWidget(sidebar)

        content = QWidget()
        main = QVBoxLayout(content)
        main.setContentsMargins(30, 26, 30, 22)
        main.setSpacing(26)
        top = QHBoxLayout()
        top.addWidget(_label("YOUR WORKFLOW, WITH EVIDENCE", "eyebrow"), 1)
        self.privacy_status = _label("Local Only", "privacy", wrap=False)
        self.privacy_status.setToolTip("No outbound processing is enabled.")
        top.addWidget(self.privacy_status)
        main.addLayout(top)
        self.pages = QStackedWidget()
        self.pages.setAccessibleName("AfterTrace pages")
        for name in DESTINATIONS:
            self.pages.addWidget(self._page(name))
        main.addWidget(self.pages, 1)
        layout.addWidget(content, 1)
        self.navigation.currentRowChanged.connect(self._navigate)
        self.navigation.setCurrentRow(0)
        self.statusBar().showMessage(
            "Discover sources → scope projects → import explicitly → Analyze in Findings"
        )
        self._startup_timer = QTimer(self)
        self._startup_timer.setSingleShot(True)
        self._startup_timer.timeout.connect(self.workspace_controller.reload)
        self._startup_timer.start(0)

    def closeEvent(self, event):
        self._startup_timer.stop()
        self.findings_controller.begin_close()
        if self.workspace_controller.busy or self.findings_controller.busy:
            self._close_pending = True
            self.statusBar().showMessage("Finishing the current operation before closing…")
            event.ignore()
        else:
            super().closeEvent(event)

    def _close_when_idle(self):
        if self._close_pending:
            self.close()

    def _navigate(self, index: int):
        self.pages.setCurrentIndex(index)
        if index == DESTINATIONS.index("Findings"):
            self.findings_controller.reload()

    def _refresh_findings_if_visible(self):
        if self.pages.currentIndex() == DESTINATIONS.index("Findings"):
            self.findings_controller.reload()

    def _page(self, name: str) -> QScrollArea:
        if name == "Findings":
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(FindingsPage(self.findings_controller))
            return scroll
        if name in ("Projects", "Sources"):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(WorkspacePage(self.workspace_controller, projects=name == "Projects"))
            return scroll
        page = QWidget()
        page.setObjectName(f"page{name}")
        content = QVBoxLayout(page)
        content.setContentsMargins(0, 0, 8, 0)
        content.setSpacing(20)
        heading = QHBoxLayout()
        heading.addWidget(_label(name, "title"), 1)
        heading.addWidget(_label("FOUNDATION", "badge", wrap=False))
        content.addLayout(heading)
        if name == "Overview":
            content.addWidget(_label("Learn from how you work with coding agents.", "muted"))
            stats = QHBoxLayout()
            stats.setSpacing(16)
            for title, value, detail in (
                ("Repeated instructions", "→", "Open Findings"),
                ("Project scoping", "→", "Open Projects"),
                ("History discovery", "→", "Open Sources"),
            ):
                card = _card(title, detail)
                card.layout().insertWidget(0, _label(value, "stat"))
                stats.addWidget(card)
            content.addLayout(stats)
            content.addWidget(
                _card(
                    "No strong patterns yet",
                    "AfterTrace will connect repeated instructions to original evidence. "
                    "Use Sources to discover Codex metadata, then Projects to scope and "
                    "explicitly import permitted history, then analyze it from Findings.",
                )
            )
            content.addWidget(
                _card(
                    "Your data stays under your control",
                    "Local Only is the default. Imports require project scoping, "
                    "and source histories will remain read-only.",
                )
            )
        else:
            subtitle, title, body = _PAGE_COPY[name]
            content.addWidget(_label(subtitle, "muted"))
            content.addWidget(_card(title, body))
        content.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll
