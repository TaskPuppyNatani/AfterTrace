"""Persisted Findings and on-demand receipts; no analysis or persistence logic in widgets."""

from collections.abc import Callable
from datetime import datetime

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from aftertrace.core.models import Finding
from aftertrace.services.findings import FindingsService
from aftertrace.services.state import FindingDetail, FindingsSnapshot
from aftertrace.ui.jobs import Job

_STYLES = """
QPushButton { background: #20283a; border: 1px solid #444e68; border-radius: 5px;
    padding: 9px 14px; color: #edf0fa; }
QPushButton:hover { border-color: #b894ff; }
QPushButton:disabled { color: #7c879d; }
QPushButton#analyzeHistory { background: #30234e; border-color: #7855a4; }
QLabel#confidence { color: #6ddad0; border: 1px solid #285347; border-radius: 5px;
    padding: 5px 9px; }
QLabel#instruction { font-size: 18px; color: #edf0fa; }
QFrame#receipt { background: #141a25; border: 1px solid #2a3244; border-radius: 8px; }
"""


def _label(text: str, name: str = "") -> QLabel:
    label = QLabel(text)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _date(value: datetime | None) -> str:
    return value.isoformat(sep=" ", timespec="seconds") if value else "Not available"


def _quantity(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _counts(finding: Finding) -> str:
    return " · ".join(
        (
            _quantity(finding.occurrence_count, "occurrence"),
            _quantity(finding.distinct_session_count, "session"),
            _quantity(finding.distinct_project_count, "project"),
        )
    )


def _clear(layout):
    while layout.count():
        item = layout.takeAt(0)
        if widget := item.widget():
            for label in widget.findChildren(QLabel):
                label.clear()
            if isinstance(widget, QLabel):
                widget.clear()
            widget.setParent(None)
            widget.deleteLater()
        elif item.layout():
            _clear(item.layout())


class FindingsController(QObject):
    changed = Signal(object)
    detail_ready = Signal(object)
    detail_failed = Signal(str)
    cleared = Signal()
    evidence_invalidated = Signal()
    notice = Signal(str)
    busy_changed = Signal(bool)
    idle = Signal()

    def __init__(self, service: FindingsService, parent: QObject):
        super().__init__(parent)
        self.service = service
        self.state = FindingsSnapshot()
        self.job: Job | None = None
        self._generation = 0
        self._active_generation = 0
        self._kind = ""
        self._pending_reload = False
        self._workspace_busy = False
        self._closing = False

    @property
    def busy(self) -> bool:
        return self.job is not None

    def invalidate(self):
        self._generation += 1
        self.state = FindingsSnapshot()
        self.evidence_invalidated.emit()
        self.cleared.emit()

    def set_workspace_busy(self, busy: bool):
        self._workspace_busy = busy
        if busy:
            self.invalidate()
        self.busy_changed.emit(self.busy or busy)
        if not busy and self._pending_reload and not self.busy:
            self.reload()

    def reload(self):
        if self._closing:
            return
        self.invalidate()
        if self.busy or self._workspace_busy:
            self._pending_reload = True
            return
        self._pending_reload = False
        self._start("load", self.service.snapshot, "Loading persisted findings…")

    def analyze(self):
        if self.busy or self._workspace_busy or self._closing:
            return
        self.evidence_invalidated.emit()
        self._start("analyze", self.service.analyze, "Analyzing imported history…")

    def load_evidence(self, finding_id: str) -> bool:
        if self.busy or self._workspace_busy or self._closing:
            return False
        self._start("evidence", lambda: self.service.detail(finding_id), "Loading evidence…")
        return True

    def _start(self, kind: str, action: Callable[[], object], notice: str):
        self._generation += 1
        self._active_generation = self._generation
        self._kind = kind
        self.job = Job(action, self)
        self.job.completed.connect(self._completed)
        self.job.failed.connect(self._failed)
        self.job.finished.connect(self._finished)
        self.busy_changed.emit(True)
        self.notice.emit(notice)
        self.job.start()

    def _completed(self, result):
        if self._active_generation != self._generation or self._closing:
            return
        if self._kind == "evidence":
            self.detail_ready.emit(result)
            self.notice.emit("Evidence loaded." if result else "Evidence is no longer available.")
        else:
            self.state = result
            self.changed.emit(result)
            self.notice.emit(
                "Analysis complete." if self._kind == "analyze" else "Persisted findings loaded."
            )

    def _failed(self, _message):
        if self._active_generation != self._generation or self._closing:
            return
        if self._kind == "evidence":
            self.detail_failed.emit("Evidence could not be loaded. Close and try again.")
            self.notice.emit("Evidence could not be loaded.")
        elif self._kind == "analyze":
            self.notice.emit(
                "Analysis could not be completed. Your existing findings are still available."
            )
        else:
            self.notice.emit("Findings could not be loaded. Use Refresh to retry.")

    def _finished(self):
        job, self.job = self.job, None
        job.deleteLater()
        self.busy_changed.emit(self._workspace_busy)
        if self._pending_reload and not self._workspace_busy and not self._closing:
            self.reload()
        self.idle.emit()

    def begin_close(self):
        self._closing = True
        self._pending_reload = False
        self.invalidate()

    def finish_background_work(self):
        if self.job is not None:
            self.job.wait()


class EvidenceDialog(QDialog):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("findingEvidence")
        self.setWindowTitle("Finding evidence — AfterTrace")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(780, 650)
        self.setMinimumSize(450, 360)
        self.setStyleSheet(_STYLES)
        layout = QVBoxLayout(self)
        self.status = _label("Loading evidence…", "evidenceStatus")
        layout.addWidget(self.status)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        self.body = QVBoxLayout(body)
        self.body.setSpacing(16)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def render(self, detail: FindingDetail | None):
        _clear(self.body)
        if detail is None or not detail.evidence:
            self.status.setText(
                "Evidence is no longer available. Refresh Findings for the current state."
            )
            return
        finding = detail.finding
        self.status.setText("Repeated Instruction · Supporting user evidence")
        self.body.addWidget(_label(finding.title, "instruction"))
        self.body.addWidget(
            _label(f"{_counts(finding)} · {finding.confidence.value.upper()} confidence")
        )
        for view in detail.evidence:
            item = view.evidence
            card = QFrame()
            card.setObjectName("receipt")
            content = QVBoxLayout(card)
            content.setContentsMargins(18, 16, 18, 16)
            content.addWidget(
                _label(
                    f"{view.harness.value.replace('_', ' ').title()} · {view.source_name}\n"
                    f"{view.project_name} · {_date(item.observed_at)}",
                    "muted",
                )
            )
            content.addWidget(_label(f"Session: {view.session_title or item.session_id}", "muted"))
            content.addWidget(_label("YOU", "eyebrow"))
            content.addWidget(_label(item.original_text, "originalEvidence"))
            self.body.addWidget(card)
        provenance = finding.provenance
        method = (
            "AfterTrace deterministic engine"
            if provenance.deterministic_steps
            and not provenance.semantic_steps
            and provenance.model is None
            else "Persisted analysis provenance"
        )
        versions = ", ".join(
            f"{key}:{value}" for key, value in sorted(provenance.analyzer_versions.items())
        )
        self.body.addWidget(
            _label(
                f"Analysis\n{method}\nAnalyzer: {finding.analysis_version}\n"
                f"Versions: {versions or 'Not recorded'}\n"
                f"Steps: {', '.join(provenance.deterministic_steps) or 'Not recorded'}\n"
                f"Model used: {provenance.model or 'None'}",
                "analysisProvenance",
            )
        )
        self.body.addStretch()

    def reject(self):
        _clear(self.body)
        self.status.clear()
        super().reject()


class FindingsPage(QWidget):
    def __init__(self, controller: FindingsController):
        super().__init__()
        self.setObjectName("pageFindings")
        self.setStyleSheet(_STYLES)
        self.controller = controller
        self.dialog: EvidenceDialog | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(18)
        layout.addWidget(_label("Findings", "title"))
        layout.addWidget(
            _label("Patterns AfterTrace has found in your imported coding-agent history.", "muted")
        )
        actions = QHBoxLayout()
        self.analyze_button = QPushButton("Analyze Imported History")
        self.analyze_button.setObjectName("analyzeHistory")
        self.analyze_button.clicked.connect(controller.analyze)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(controller.reload)
        actions.addWidget(self.analyze_button)
        actions.addWidget(self.refresh_button)
        actions.addStretch()
        layout.addLayout(actions)
        self.status = _label("Open Findings to load persisted results.", "findingsStatus")
        layout.addWidget(self.status)
        self.empty = _label("", "findingsEmpty")
        layout.addWidget(self.empty)
        self.cards = QVBoxLayout()
        self.cards.setSpacing(16)
        layout.addLayout(self.cards)
        layout.addStretch()
        controller.changed.connect(self.render)
        controller.cleared.connect(self.clear)
        controller.notice.connect(self.status.setText)
        controller.busy_changed.connect(self.set_busy)
        controller.evidence_invalidated.connect(self.close_evidence)
        controller.detail_ready.connect(self.render_evidence)
        controller.detail_failed.connect(self.evidence_failed)

    def clear(self):
        _clear(self.cards)
        self.empty.clear()
        self.empty.hide()

    def render(self, state: FindingsSnapshot):
        self.clear()
        if not state.findings:
            self.empty.show()
            self.empty.setText(
                "No imported history is available for analysis.\n"
                "Choose projects in Projects and import their history first."
                if not state.eligible_session_count
                else "No repeated instructions met the current finding threshold.\n"
                "AfterTrace requires repeated support across at least 3 distinct sessions "
                "before publishing a finding."
            )
        for finding in state.findings:
            card = QFrame()
            card.setObjectName("card")
            content = QVBoxLayout(card)
            content.setContentsMargins(22, 20, 22, 20)
            content.setSpacing(12)
            heading = QHBoxLayout()
            heading.addWidget(_label("Repeated Instruction", "eyebrow"), 1)
            confidence = _label(f"{finding.confidence.value.upper()} CONFIDENCE", "confidence")
            confidence.setToolTip("Deterministic supporting evidence; not a model probability.")
            heading.addWidget(confidence)
            content.addLayout(heading)
            content.addWidget(_label(finding.title, "instruction"))
            content.addWidget(_label(_counts(finding), "findingCounts"))
            sources = finding.metadata.get("source_distribution", {})
            harnesses = (
                ", ".join(h.value.replace("_", " ").title() for h in finding.affected_harnesses)
                or "Not recorded"
            )
            content.addWidget(_label(f"{harnesses} · {_quantity(len(sources), 'source')}", "muted"))
            content.addWidget(
                _label(
                    f"State: {finding.state.value.title()} · "
                    f"Last seen: {_date(finding.last_seen_at)}",
                    "findingState",
                )
            )
            evidence = QPushButton("View Evidence")
            evidence.setObjectName("viewEvidence")
            evidence.clicked.connect(
                lambda _=False, identity=finding.id: self.open_evidence(identity)
            )
            content.addWidget(evidence, alignment=Qt.AlignmentFlag.AlignLeft)
            self.cards.addWidget(card)
        self.set_busy(self.controller.busy)

    def set_busy(self, busy: bool):
        self.analyze_button.setEnabled(not busy)
        self.refresh_button.setEnabled(not busy)
        for button in self.findChildren(QPushButton, "viewEvidence"):
            button.setEnabled(not busy)

    def open_evidence(self, finding_id: str):
        if self.controller.busy:
            return
        self.close_evidence()
        self.dialog = EvidenceDialog(self)
        self.dialog.show()
        if not self.controller.load_evidence(finding_id):
            self.close_evidence()

    def render_evidence(self, detail):
        if self.dialog is not None and self.dialog.isVisible():
            self.dialog.render(detail)

    def evidence_failed(self, message: str):
        if self.dialog is not None and self.dialog.isVisible():
            self.dialog.status.setText(message)

    def close_evidence(self):
        if self.dialog is not None:
            self.dialog.reject()
            self.dialog.deleteLater()
            self.dialog = None
