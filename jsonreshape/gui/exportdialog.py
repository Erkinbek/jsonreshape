"""Export dialog (Ctrl+E): docs/ARCHITECTURE.md §8, §11.8; AGENT_PROMPT §10.

:class:`ExportDialog` writes the current view, all records or the current node of a
session to JSON, JSON Lines, CSV, Excel (XLSX) or SQLite:

* the output file (save dialog with filters; the format follows the extension,
  :func:`jsonreshape.writer.format_for_path`, or the format combo, which then adjusts the
  extension); the source file and the document's data file are refused;
* the scope: the current view (filter/sort: ``session.view_recordset()``), all records,
  or the current node (JSON / JSON Lines only: :func:`export_node` re-indents the node's
  raw bytes token by token with ``core.Reformatter`` -- numbers and strings stay
  byte-identical -- or writes one compact line per array element);
* the options of the format (defaults: ``ExportOptions.from_settings``; the last used
  format and options are remembered in the GUI state under ``export_last``).

The export runs as a task (kind ``export``) started by the application-wide
:class:`ExportRunner` (:func:`export_runner`), which outlives the dialog: the dialog may be
closed meanwhile (the task goes on in the tasks panel) and the outcome is still reported --
the :class:`ExportReportDialog` (records, rows, files with sizes, time, speed, warnings,
"open folder") is shown by the runner without blocking (next to the dialog while it is
open, else next to the main window), errors go to ``app.report_error`` and the status bar
tells the result. The dialog only listens to the runner's signals (bound methods: nothing
calls into a deleted dialog). The task is not owned by the session: reopening the file
(F5) or closing its tab lets the export finish on the document it started with (the
session keeps that document and the exported view open until the export ended,
``DocSession.keep_document_for`` -- a lease taken when the export is started, so a view
replaced while the export still waits in the heavy-task queue is exported as it was).
While the export waits for its turn the dialog says so ("Navbatda"); a dialog opened
while an export of the file runs shows that export's progress (another one can still be
started).

The output is refused when it is the source / data file, also through another name
(``os.path.samefile``: junctions, shares, hard links); a relative path is taken relative to
the folder of the default output (the last export folder, else the source file's folder).
"Current view" cannot be exported while its filter / sort is still running.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from jsonreshape import core, writer
from jsonreshape.errors import Cancelled, JsonReshapeError, UserInputError
from jsonreshape.fileio import atomic_write, long_path, normalize_path
from jsonreshape.gui import copyfmt, theme
from jsonreshape.gui.searchpanel import check_not_source
from jsonreshape.gui.statusbar import StatusLabel
from jsonreshape.gui.widgets import confirm, format_count, format_size, keep_alive, open_local_path
from jsonreshape.i18n import t
from jsonreshape.settings import XLSX_MAX_ROWS
from jsonreshape.tasks import CancelToken, Progress, check, format_duration
from jsonreshape.writer import ExportOptions, ExportReport

log = logging.getLogger(__name__)

__all__ = ["ExportDialog", "ExportJob", "ExportOutcome", "ExportReportDialog", "ExportRunner", "FORMATS",
           "export_node", "export_runner"]

FORMATS = ("json", "jsonl", "csv", "xlsx", "sqlite")
NODE_FORMATS = ("json", "jsonl")
SCOPES = ("view", "all", "node")
EXTENSIONS = {"json": ".json", "jsonl": ".jsonl", "csv": ".csv", "xlsx": ".xlsx", "sqlite": ".sqlite"}
STATE_KEY = "export_last"
DIR_KEY = "last_export_dir"
READ_CHUNK = 1 << 20              # node bytes read (and fed to the Reformatter) at a time
NODE_BATCH = 4096                 # array elements fetched at a time (JSON Lines of a node)
RANGE_MAX = 8 << 20               # elements of a batch read with one read while their range is this small

_INDENTS: tuple[tuple[str, Any], ...] = (("2", 2), ("4", 4), ("tab", "\t"), ("compact", None))
_SORTS: tuple[tuple[str, Any], ...] = (("none", False), ("alpha", "alpha"), ("codepoint", "codepoint"))
_DELIMITERS: tuple[tuple[str, str], ...] = ((",", ","), (";", ";"), ("Tab", "\t"), ("|", "|"))
_KEYED_LINE = '{"id1": {…}}'
_KEYED_OBJECT = '{"id1": {…}, "id2": {…}}'


# ==================================================================================
# node export (worker thread)
# ==================================================================================
def _check_target(doc: Any, out: Path, extra: tuple = ()) -> None:
    """The writer's own check (same name, directory, missing folder, ``samefile`` with the
    source) plus ``samefile`` with the data file and ``extra`` paths."""
    writer._check_target(doc, out)
    check_not_source(out, (doc.source_path, doc.data_path) + tuple(p for p in extra if p is not None))


def _reformat(data: bytes, indent: int | str | None) -> bytes:
    """One complete JSON value re-indented (token bytes unchanged)."""
    rf = core.Reformatter(indent)
    if len(data) <= READ_CHUNK:
        return rf.feed(data) + rf.finish()
    parts = [rf.feed(data[i:i + READ_CHUNK]) for i in range(0, len(data), READ_CHUNK)]
    parts.append(rf.finish())
    return b"".join(parts)


def _stream_value(src: Any, start: int, end: int, indent: int | str | None, out: Any,
                  cancel: CancelToken | None, on_read: Any = None) -> None:
    """Re-indent the value at ``[start, end)`` of the open data file into ``out``,
    :data:`READ_CHUNK` bytes at a time."""
    rf = core.Reformatter(indent)
    pos = start
    src.seek(pos)
    while pos < end:
        check(cancel)
        chunk = src.read(min(READ_CHUNK, end - pos))
        if not chunk:
            raise UserInputError("gui.export.node_truncated")
        pos += len(chunk)
        out.write(rf.feed(chunk))
        if on_read is not None:
            on_read(pos)
    out.write(rf.finish())


def export_node(session: Any, ref: Any, out_path: str | os.PathLike[str], fmt: str = "json", *,
                indent: int | str | None = 2, progress: Progress | None = None,
                cancel: CancelToken | None = None, doc: Any = None) -> ExportReport:
    """Write one node of ``session`` (worker threads only).

    ``fmt="json"``: the node's value re-indented with ``core.Reformatter(indent)`` from its
    raw bytes, streamed in 1 MiB pieces (numbers and strings byte-identical, no trailing
    newline -- like ``writer.export``). ``fmt="jsonl"``: one compact line per element when
    the node is an array, else the node on one line (``\\n`` after every line). The
    synthetic root of JSON Lines / stream documents (no bytes of its own) is written with
    :func:`jsonreshape.writer.export` (all records). Atomic; the source must be unchanged
    before the commit. ``doc``: the document to read (default: the session's current one;
    the dialog passes the one it started with, so a reopen meanwhile does not matter)."""
    if fmt not in NODE_FORMATS:
        raise UserInputError("gui.export.node_formats")
    started = time.perf_counter()
    doc = doc if doc is not None else session.doc
    if doc is None:
        raise UserInputError("gui.session.not_open", name=getattr(session, "name", "?"))
    out = Path(os.path.abspath(os.fspath(out_path)))
    _check_target(doc, out, (getattr(session, "path", None),))
    node = session.node(ref, cancel, doc=doc)
    if node.is_virtual:
        opts = ExportOptions(fmt, indent=indent if fmt == "json" else None)
        return writer.export(doc, out, opts, None, progress=progress, cancel=cancel)
    total = max(0, node.end - node.start)
    if progress is not None:
        progress.set_message(t("gui.export.progress.node", name=out.name))
        progress.set_total(total)

    def on_read(pos: int) -> None:
        if progress is not None:
            progress.update(pos - node.start)

    records = 1
    with atomic_write(out, "wb", buffering=1 << 20) as f:
        with doc.open_data() as src:
            if fmt == "json":
                _stream_value(src, node.start, node.end, indent, f, cancel, on_read)
            elif node.kind != "array":
                _stream_value(src, node.start, node.end, None, f, cancel, on_read)
                f.write(b"\n")
            else:
                records = _write_lines(node, src, f, cancel, on_read)
        check(cancel)
        doc.ensure_unchanged()
    if progress is not None:
        progress.update(total)
        progress.finish()
    try:
        size = os.stat(long_path(out)).st_size
    except OSError:  # pragma: no cover - just written
        size = 0
    report = ExportReport(fmt=fmt, records=records, rows=records, bytes_written=size, files=[out],
                          elapsed=time.perf_counter() - started)
    log.info("exported node %s of %s to %s as %s: %d bytes", getattr(node, "path_text", "?"),
             doc.source_path.name, out, fmt, size)
    return report


def _write_lines(node: Any, src: Any, f: Any, cancel: CancelToken | None, on_read: Any) -> int:
    """JSON Lines of an array node: every element compact on its own line."""
    count = node.child_count(cancel=cancel)
    done = 0
    while done < count:
        check(cancel)
        kids = node.children(done, min(count, done + NODE_BATCH))
        if not kids:
            break
        lo, hi = kids[0].start, kids[-1].end
        if hi - lo <= RANGE_MAX:
            src.seek(lo)
            buf = src.read(hi - lo)
            if len(buf) < hi - lo:
                raise UserInputError("gui.export.node_truncated")
            for k, kid in enumerate(kids):
                if k % 256 == 0:
                    check(cancel)
                f.write(_reformat(buf[kid.start - lo:kid.end - lo], None))
                f.write(b"\n")
        else:
            for kid in kids:
                _stream_value(src, kid.start, kid.end, None, f, cancel)
                f.write(b"\n")
        done += len(kids)
        on_read(kids[-1].end)
    return done


# ==================================================================================
# report
# ==================================================================================
@dataclass
class ExportOutcome:
    """What an export task returns: the writer's report, the size of every file, the
    scope exported."""

    report: ExportReport
    file_sizes: list[int] = field(default_factory=list)
    scope: str = "view"
    path: str = ""


class ExportReportDialog(QDialog):
    """The report shown when an export is done (non-modal; "open folder")."""

    def __init__(self, outcome: ExportOutcome, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.outcome = outcome
        report = outcome.report
        self.setWindowTitle(t("gui.export.report.title"))
        self.setSizeGripEnabled(True)
        layout = QVBoxLayout(self)
        self.summary_label = QLabel(self.summary_text(), self)
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary_label)
        layout.addWidget(QLabel(t("gui.export.report.files", count=len(report.files)), self))
        self.files_view = QPlainTextEdit(self)
        self.files_view.setReadOnly(True)
        self.files_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.files_view.setPlainText(self.files_text())
        self.files_view.setMaximumHeight(self.files_view.fontMetrics().lineSpacing() * min(6, max(2, len(report.files)))
                                         + 16)
        layout.addWidget(self.files_view)
        self.warnings_view: QPlainTextEdit | None = None
        if report.warnings:
            label = QLabel(t("gui.export.report.warnings", count=len(report.warnings)), self)
            label.setStyleSheet(f"color: {theme.color('warn').name()};")
            layout.addWidget(label)
            self.warnings_view = QPlainTextEdit(self)
            self.warnings_view.setReadOnly(True)
            self.warnings_view.setPlainText(copyfmt.escape_surrogates("\n".join(report.warnings)))
            layout.addWidget(self.warnings_view, 1)
        buttons = QHBoxLayout()
        self.open_folder_button = QPushButton(t("gui.export.report.open_folder"), self)
        self.open_folder_button.setAutoDefault(False)
        self.open_folder_button.clicked.connect(self.open_folder)
        self.open_folder_button.setEnabled(bool(report.files))
        buttons.addWidget(self.open_folder_button)
        buttons.addStretch(1)
        self.close_button = QPushButton(t("gui.common.close"), self)
        self.close_button.setDefault(True)
        self.close_button.clicked.connect(self.accept)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self.resize(560, self.sizeHint().height())

    def summary_text(self) -> str:
        report = self.outcome.report
        text = t("gui.export.report.summary", records=format_count(report.records), rows=format_count(report.rows),
                 size=format_size(report.bytes_written), seconds=format_duration(report.elapsed),
                 rate=f"{report.mb_per_s:.1f}")
        if report.skipped:
            text += "\n" + t("gui.export.report.skipped", count=format_count(report.skipped))
        if report.tables:
            text += "\n" + t("gui.export.report.tables", tables=", ".join(report.tables[:20]))
        return text

    def files_text(self) -> str:
        sizes = self.outcome.file_sizes
        lines = []
        for i, p in enumerate(self.outcome.report.files):
            size = sizes[i] if i < len(sizes) else None
            lines.append(f"{p}  —  {format_size(size)}" if size is not None else str(p))
        return copyfmt.escape_surrogates("\n".join(lines))

    def report_text(self) -> str:
        """Everything the report says, as plain text."""
        parts = [self.summary_text(), self.files_text()]
        if self.outcome.report.warnings:
            parts.append("\n".join(self.outcome.report.warnings))
        return "\n".join(parts)

    def folder(self) -> str | None:
        files = self.outcome.report.files
        return str(Path(files[0]).parent) if files else None

    def open_folder(self) -> bool:
        folder = self.folder()
        return bool(folder) and open_local_path(folder)


# ==================================================================================
# the runner (outlives the dialogs)
# ==================================================================================
def _alive(widget: Any) -> bool:
    if widget is None:
        return False
    try:
        from shiboken6 import isValid

        return bool(isValid(widget))
    except Exception:  # pragma: no cover - shiboken always available with PySide6
        return True


@dataclass(eq=False)
class ExportJob:
    """One export started through :class:`ExportRunner`."""

    session: Any
    path: str
    scope: str
    task: Any = None
    state: str = "running"                  # running | done | failed | cancelled
    outcome: ExportOutcome | None = None
    error: BaseException | None = None
    parents: list = field(default_factory=list)   # report window parent candidates (first alive & visible)
    report_dialog: Any = None


class ExportRunner(QObject):
    """Runs export tasks and reports their outcome independently of the dialog that
    started them (one per application: :func:`export_runner`).

    Signals: ``job_started(job)``, ``job_finished(job)`` (``job.state``: ``done`` |
    ``failed`` | ``cancelled``; emitted after the report window is shown / the error is
    reported)."""

    job_started = Signal(object)
    job_finished = Signal(object)

    def __init__(self, app: Any, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self._jobs: list[ExportJob] = []

    def jobs(self, session: Any = None) -> list[ExportJob]:
        """Running jobs (of ``session``)."""
        return [job for job in self._jobs if session is None or job.session is session]

    def start(self, session: Any, work: Any, *, path: str, scope: str, title: str,
              parents: tuple = (), sets: tuple = ()) -> ExportJob:
        """Run ``work(ctx) -> ExportOutcome`` as an export task; the session keeps its
        document -- and the record sets in ``sets`` (the view the task writes) -- open
        until the task ended, also while it still waits in the heavy-task queue."""
        job = ExportJob(session=session, path=path, scope=scope, parents=list(parents))
        job.task = self.app.tasks.run(work, title=title, kind="export", owner=job, heavy=True,
                                      on_done=lambda outcome, j=job: self._done(j, outcome),
                                      on_error=lambda exc, j=job: self._failed(j, exc),
                                      on_cancel=lambda j=job: self._cancelled(j))
        keep = getattr(session, "keep_document_for", None)
        if callable(keep):
            held = tuple(rs for rs in sets if rs is not None)
            if held:
                keep(job.task, sets=held)
            else:
                keep(job.task)
        self._jobs.append(job)
        self.job_started.emit(job)
        return job

    def cancel_all(self, session: Any = None) -> int:
        """Cancel the running jobs (of ``session``); the number cancelled."""
        jobs = self.jobs(session)
        for job in jobs:
            job.task.cancel()
        return len(jobs)

    def _parent_for(self, job: ExportJob) -> Any:
        alive = [w for w in job.parents if _alive(w)]
        for widget in alive:
            if widget.isVisible():
                return widget
        return alive[-1] if alive else None

    def _end(self, job: ExportJob, state: str) -> None:
        job.state = state
        if job in self._jobs:
            self._jobs.remove(job)

    def _status(self, text: str) -> None:
        show = getattr(self.app, "show_status", None)
        if callable(show):
            show(text, 8000)

    def _done(self, job: ExportJob, outcome: ExportOutcome) -> None:
        self._end(job, "done")
        job.outcome = outcome
        report = outcome.report
        try:
            job.report_dialog = self.show_report(outcome, self._parent_for(job))
        except Exception as exc:  # pragma: no cover - defensive: the outcome is still told
            log.exception("showing the export report failed")
            self.app.report_error(exc)
        self._status(t("gui.export.status.done_path", records=format_count(report.records),
                       size=format_size(report.bytes_written), path=job.path))
        self.job_finished.emit(job)

    def _failed(self, job: ExportJob, exc: BaseException) -> None:
        self._end(job, "failed")
        job.error = exc
        self.job_finished.emit(job)
        if not isinstance(exc, Cancelled):
            self.app.report_error(exc, self._parent_for(job))

    def _cancelled(self, job: ExportJob) -> None:
        self._end(job, "cancelled")
        self._status(t("gui.export.status.cancelled_path", path=job.path))
        self.job_finished.emit(job)

    @staticmethod
    def show_report(outcome: ExportOutcome, parent: Any = None) -> ExportReportDialog:
        """The report window (non-modal)."""
        dialog = ExportReportDialog(outcome, parent if _alive(parent) else None)
        keep_alive(dialog)
        dialog.show()
        return dialog


def export_runner(app: Any) -> ExportRunner:
    """The application's :class:`ExportRunner` (created on first use, a child of ``app``)."""
    runner = getattr(app, "_export_runner", None)
    if runner is None or not _alive(runner):
        runner = ExportRunner(app, app if isinstance(app, QObject) else None)
        try:
            app._export_runner = runner
        except AttributeError:  # pragma: no cover - a context that takes no attributes
            pass
    return runner


# ==================================================================================
# the dialog
# ==================================================================================
def _parse_delimiter(text: str) -> str:
    s = text if text in ("\t", " ") else text.strip()
    if s.lower() in ("tab", "\\t"):
        return "\t"
    return s


def _set_item_enabled(combo: QComboBox, index: int, enabled: bool, tooltip: str | None = None) -> None:
    model = combo.model()
    if isinstance(model, QStandardItemModel):
        item = model.item(index)
        if item is not None:
            item.setEnabled(enabled)
            item.setToolTip(tooltip or "")


class ExportDialog(QDialog):
    """Export the view / all records / the current node of ``session`` (module docstring).

    ``columns``: the table's visible columns (CSV/XLSX column list; ``None`` = automatic);
    ``scope``: initial scope ``view`` | ``all`` | ``node``. Signals: ``export_started(task)``,
    ``export_finished(outcome)``, ``export_failed(exc)``.
    """

    export_started = Signal(object)
    export_finished = Signal(object)
    export_failed = Signal(object)

    def __init__(self, session: Any, parent: QWidget | None = None, *, columns: list[str] | None = None,
                 scope: str = "view") -> None:
        super().__init__(parent)
        self.session = session
        self.app = session.app
        self.columns = [str(c) for c in columns] if columns else None
        self.runner = export_runner(self.app)
        self.job: ExportJob | None = None
        self.task: Any = None
        self.outcome: ExportOutcome | None = None
        self.error: BaseException | None = None
        self.report_dialog: ExportReportDialog | None = None
        self._syncing = False
        self._valid = False
        self._adopted = False               # the job shown was started by another export dialog
        self._queued_shown = False
        self.setWindowTitle(t("gui.export.title", name=session.name))
        self.setSizeGripEnabled(True)
        self._build()
        self._load_defaults()
        self.set_scope(scope if scope in SCOPES else "view")
        if not self.path_edit.text():
            self.path_edit.setText(self._default_path())
        self.app.tasks.task_updated.connect(self._on_task_updated)
        self.runner.job_finished.connect(self._job_finished)
        theme.notifier().theme_changed.connect(self._style_labels)
        for signal in (session.view_changed, session.count_changed, session.view_busy_changed,
                       session.current_changed, session.state_changed):
            signal.connect(self._session_changed)
        self._validate()
        self._adopt_running_job()
        self.resize(620, self.sizeHint().height())

    def _session_changed(self, *_args: Any) -> None:
        self._update_scope_texts()
        if self.scope() == "node" and self.session.current is None:
            self.set_scope("view")
        self._update_format_widgets()
        self._validate()

    # -------------------------------------------------------------------------------- build
    def _build(self) -> None:
        layout = QVBoxLayout(self)

        top = QFormLayout()
        row = QHBoxLayout()
        self.path_edit = QLineEdit(self)
        self.path_edit.setPlaceholderText(t("gui.export.path.placeholder"))
        self.path_edit.textChanged.connect(self._on_path_changed)
        row.addWidget(self.path_edit, 1)
        self.browse_button = QPushButton(t("gui.export.browse"), self)
        self.browse_button.setAutoDefault(False)
        self.browse_button.clicked.connect(self.choose_file)
        row.addWidget(self.browse_button)
        top.addRow(t("gui.export.path.label"), row)
        self.format_combo = QComboBox(self)
        for fmt in FORMATS:
            self.format_combo.addItem(t(f"gui.export.format.{fmt}"), fmt)
        self.format_combo.currentIndexChanged.connect(self._on_format_changed)
        top.addRow(t("gui.export.format.label"), self.format_combo)
        layout.addLayout(top)

        scope_box = QGroupBox(t("gui.export.scope.title"), self)
        scope_layout = QVBoxLayout(scope_box)
        self.scope_group = QButtonGroup(self)
        self.scope_buttons: dict[str, QRadioButton] = {}
        for i, scope in enumerate(SCOPES):
            button = QRadioButton(scope_box)
            self.scope_group.addButton(button, i)
            self.scope_buttons[scope] = button
            scope_layout.addWidget(button)
        self.scope_group.idToggled.connect(self._on_scope_toggled)
        layout.addWidget(scope_box)

        self.options_box = QGroupBox(t("gui.export.options.title"), self)
        box_layout = QVBoxLayout(self.options_box)
        self.pages = QStackedWidget(self.options_box)
        self.pages.addWidget(self._json_page())
        self.pages.addWidget(self._jsonl_page())
        self.pages.addWidget(self._table_page())
        self.pages.addWidget(self._sqlite_page())
        box_layout.addWidget(self.pages)
        layout.addWidget(self.options_box, 1)

        self.warning_label = QLabel(self)
        self.warning_label.setWordWrap(True)
        self.warning_label.setTextFormat(Qt.TextFormat.PlainText)
        self.warning_label.setVisible(False)
        layout.addWidget(self.warning_label)
        self.error_label = QLabel(self)
        self.error_label.setWordWrap(True)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)
        self.status_label = StatusLabel(self, mode=Qt.TextElideMode.ElideMiddle, fixed=False)
        self.status_label.setContentsMargins(0, 0, 0, 0)
        self.status_label.update_metrics()
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        self.cancel_task_button = QPushButton(t("gui.export.stop"), self)
        self.cancel_task_button.setToolTip(t("gui.export.stop.tip"))
        self.cancel_task_button.setAutoDefault(False)
        self.cancel_task_button.clicked.connect(self.cancel_export)
        self.cancel_task_button.setVisible(False)
        buttons.addWidget(self.cancel_task_button)
        buttons.addStretch(1)
        self.export_button = QPushButton(t("gui.export.run"), self)
        self.export_button.setDefault(True)
        self.export_button.clicked.connect(self.start_export)
        buttons.addWidget(self.export_button)
        self.close_button = QPushButton(t("gui.common.close"), self)
        self.close_button.setAutoDefault(False)
        self.close_button.setToolTip(t("gui.export.close.tip"))
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self._style_labels()

    def _json_page(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        self.indent_combo = QComboBox(page)
        for key, value in _INDENTS:
            self.indent_combo.addItem(t(f"gui.export.indent.{key}"), value)
        self.indent_combo.currentIndexChanged.connect(self._validate)
        form.addRow(t("gui.export.indent.label"), self.indent_combo)
        self.json_sort_combo = self._sort_combo(page)
        form.addRow(t("gui.export.sort.label"), self.json_sort_combo)
        self.keep_wrapper_check = QCheckBox(t("gui.export.keep_wrapper"), page)
        self.keep_wrapper_check.setChecked(True)
        self.keep_wrapper_check.toggled.connect(self._validate)
        form.addRow("", self.keep_wrapper_check)
        self.as_keyed_check = QCheckBox(t("gui.export.as_keyed"), page)
        self.as_keyed_check.setToolTip(t("gui.export.as_keyed.tip", line=_KEYED_LINE, result=_KEYED_OBJECT))
        self.as_keyed_check.toggled.connect(self._on_as_keyed)
        form.addRow("", self.as_keyed_check)
        return page

    def _jsonl_page(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        self.jsonl_sort_combo = self._sort_combo(page)
        form.addRow(t("gui.export.sort.label"), self.jsonl_sort_combo)
        note = QLabel(t("gui.export.jsonl.note", line=_KEYED_LINE), page)
        note.setWordWrap(True)
        form.addRow(note)
        return page

    def _sort_combo(self, parent: QWidget) -> QComboBox:
        combo = QComboBox(parent)
        for key, value in _SORTS:
            combo.addItem(t(f"gui.export.sort.{key}"), value)
        combo.setToolTip(t("gui.export.sort.tip"))
        combo.currentIndexChanged.connect(self._validate)
        return combo

    def _table_page(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        self.table_form = form
        cols = QVBoxLayout()
        self.auto_columns_check = QCheckBox(t("gui.export.columns.auto"), page)
        self.auto_columns_check.setToolTip(t("gui.export.columns.auto.tip"))
        self.auto_columns_check.toggled.connect(self._on_auto_columns)
        cols.addWidget(self.auto_columns_check)
        self.columns_edit = QPlainTextEdit(page)
        self.columns_edit.setPlaceholderText(t("gui.export.columns.placeholder"))
        self.columns_edit.setToolTip(t("gui.export.columns.tip"))
        self.columns_edit.setFixedHeight(self.columns_edit.fontMetrics().lineSpacing() * 5 + 12)
        self.columns_edit.textChanged.connect(self._validate)
        cols.addWidget(self.columns_edit)
        form.addRow(t("gui.export.columns.label"), cols)
        self.array_mode_combo = QComboBox(page)
        self.array_mode_combo.addItem(t("gui.export.array.join"), "join")
        self.array_mode_combo.addItem(t("gui.export.array.rows"), "rows")
        self.array_mode_combo.setToolTip(t("gui.export.array.tip"))
        self.array_mode_combo.currentIndexChanged.connect(self._validate)
        form.addRow(t("gui.export.array.label"), self.array_mode_combo)
        self.explode_edit = QLineEdit(page)
        self.explode_edit.setPlaceholderText(t("gui.export.explode.placeholder"))
        self.explode_edit.setToolTip(t("gui.export.explode.tip"))
        self.explode_edit.textChanged.connect(self._validate)
        form.addRow(t("gui.export.explode.label"), self.explode_edit)
        self.join_sep_edit = QLineEdit(page)
        self.join_sep_edit.setToolTip(t("gui.export.join_sep.tip"))
        self.join_sep_edit.setMaximumWidth(120)
        self.join_sep_edit.textChanged.connect(self._validate)
        form.addRow(t("gui.export.join_sep.label"), self.join_sep_edit)
        self.delimiter_combo = QComboBox(page)
        self.delimiter_combo.setEditable(True)
        for text, value in _DELIMITERS:
            self.delimiter_combo.addItem(text, value)
        self.delimiter_combo.setMaximumWidth(120)
        self.delimiter_combo.setToolTip(t("gui.export.delimiter.tip"))
        self.delimiter_combo.currentTextChanged.connect(self._validate)
        form.addRow(t("gui.export.delimiter.label"), self.delimiter_combo)
        self.bom_check = QCheckBox(t("gui.export.bom"), page)
        self.bom_check.setToolTip(t("gui.export.bom.tip"))
        self.bom_check.toggled.connect(self._validate)
        form.addRow("", self.bom_check)
        self.split_combo = QComboBox(page)
        self.split_combo.addItem(t("gui.export.split.sheets"), "sheets")
        self.split_combo.addItem(t("gui.export.split.files"), "files")
        self.split_combo.setToolTip(t("gui.export.split.tip"))
        self.split_combo.currentIndexChanged.connect(self._validate)
        form.addRow(t("gui.export.split.label"), self.split_combo)
        self.max_rows_spin = QSpinBox(page)
        self.max_rows_spin.setRange(2, XLSX_MAX_ROWS)
        self.max_rows_spin.setGroupSeparatorShown(True)
        self.max_rows_spin.setToolTip(t("gui.export.max_rows.tip"))
        self.max_rows_spin.valueChanged.connect(self._validate)
        form.addRow(t("gui.export.max_rows.label"), self.max_rows_spin)
        return page

    def _sqlite_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        note = QLabel(t("gui.export.sqlite.note"), page)
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    def _style_labels(self) -> None:
        self.error_label.setStyleSheet(f"color: {theme.color('danger').name()};")
        self.warning_label.setStyleSheet(f"color: {theme.color('warn').name()};")

    # -------------------------------------------------------------------------------- defaults
    def _load_defaults(self) -> None:
        settings = self.app.settings
        last = self.app.state.get(STATE_KEY)
        last = last if isinstance(last, dict) else {}
        fmt = last.get("fmt") if last.get("fmt") in FORMATS else "json"
        try:
            base = ExportOptions.from_settings(fmt, settings)
        except JsonReshapeError:  # pragma: no cover - settings are validated
            base = ExportOptions(fmt)
        values = {
            "indent": base.indent, "sort_keys": base.sort_keys, "keep_wrapper": base.keep_wrapper,
            "array_mode": base.array_mode, "join_sep": base.join_sep, "csv_delimiter": base.csv_delimiter,
            "csv_bom": base.csv_bom, "xlsx_split": base.xlsx_split, "xlsx_max_rows": base.xlsx_max_rows,
        }
        for key in list(values):
            if key in last:
                try:
                    ExportOptions(fmt, **{key: last[key]})
                except (JsonReshapeError, TypeError, ValueError):
                    continue
                values[key] = last[key]
        self._syncing = True
        try:
            self.format_combo.setCurrentIndex(FORMATS.index(fmt))
            self.pages.setCurrentIndex(self._page_index(fmt))
            self._select_data(self.indent_combo, values["indent"], label=t("gui.export.indent.n", n=values["indent"]))
            sort = values["sort_keys"] if values["sort_keys"] in (False, "alpha", "codepoint") else False
            self._select_data(self.json_sort_combo, sort)
            self._select_data(self.jsonl_sort_combo, sort)
            self.keep_wrapper_check.setChecked(bool(values["keep_wrapper"]))
            self.as_keyed_check.setChecked(False)
            self._select_data(self.array_mode_combo, values["array_mode"])
            self.join_sep_edit.setText(str(values["join_sep"]))
            self._set_delimiter(str(values["csv_delimiter"]))
            self.bom_check.setChecked(bool(values["csv_bom"]))
            self._select_data(self.split_combo, values["xlsx_split"])
            self.max_rows_spin.setValue(int(values["xlsx_max_rows"]))
            if self.columns:
                self.auto_columns_check.setChecked(False)
                self.columns_edit.setPlainText("\n".join(self.columns))
            else:
                self.auto_columns_check.setChecked(True)
            self.columns_edit.setEnabled(not self.auto_columns_check.isChecked())
        finally:
            self._syncing = False
        self._update_format_widgets()

    @staticmethod
    def _select_data(combo: QComboBox, value: Any, label: str | None = None) -> None:
        for i in range(combo.count()):
            data = combo.itemData(i)
            if data == value and type(data) is type(value):
                combo.setCurrentIndex(i)
                return
        if label is not None:
            combo.addItem(label, value)
            combo.setCurrentIndex(combo.count() - 1)

    def _set_delimiter(self, value: str) -> None:
        for i in range(self.delimiter_combo.count()):
            if self.delimiter_combo.itemData(i) == value:
                self.delimiter_combo.setCurrentIndex(i)
                return
        self.delimiter_combo.setEditText(value)

    def base_dir(self) -> str:
        """The folder of the default output and of relative paths: the last export
        folder, else the source file's folder."""
        return str(self.app.state.get(DIR_KEY) or os.path.dirname(self.session.path))

    def _default_path(self) -> str:
        session = self.session
        folder = self.base_dir()
        stem = os.path.splitext(session.name)[0] or "export"
        if stem.lower().endswith(".json"):
            stem = stem[:-5]
        return os.path.join(folder, f"{stem}-export{EXTENSIONS[self.format()]}")

    # -------------------------------------------------------------------------------- accessors
    def format(self) -> str:
        return str(self.format_combo.currentData() or "json")

    def set_format(self, fmt: str) -> None:
        if fmt in FORMATS:
            self.format_combo.setCurrentIndex(FORMATS.index(fmt))

    def scope(self) -> str:
        for scope, button in self.scope_buttons.items():
            if button.isChecked():
                return scope
        return "view"

    def set_scope(self, scope: str) -> None:
        """Choose ``view`` | ``all`` | ``node`` (``node`` without a current node: ``view``)."""
        self._update_scope_texts()
        button = self.scope_buttons.get(scope)
        if button is not None and button.isEnabled():
            button.setChecked(True)
        else:
            self.scope_buttons["view"].setChecked(True)

    def output_path(self) -> str:
        """The absolute output path (a relative text is taken relative to :meth:`base_dir`,
        never to the process's working folder)."""
        text = os.path.expanduser(self.path_edit.text().strip())
        if not text:
            return ""
        if not os.path.isabs(text):
            text = os.path.join(self.base_dir(), text)
        return os.path.abspath(text)

    def set_output_path(self, path: str | os.PathLike[str]) -> None:
        self.path_edit.setText(os.fspath(path))

    def column_list(self) -> list[str] | None:
        """The CSV/XLSX columns (``None`` = automatic)."""
        if self.auto_columns_check.isChecked():
            return None
        cols = [line.strip() for line in self.columns_edit.toPlainText().splitlines() if line.strip()]
        return cols or None

    def set_columns(self, columns: list[str] | None) -> None:
        if columns:
            self.columns_edit.setPlainText("\n".join(columns))
            self.auto_columns_check.setChecked(False)
        else:
            self.auto_columns_check.setChecked(True)

    def export_options(self) -> ExportOptions:
        """The options described by the dialog (``UserInputError`` when invalid)."""
        fmt = self.format()
        node = self.scope() == "node"
        kw: dict[str, Any] = {}
        if fmt == "json":
            kw["indent"] = self.indent_combo.currentData()
            if not node:
                kw["sort_keys"] = self.json_sort_combo.currentData()
                kw["keep_wrapper"] = self.keep_wrapper_check.isChecked()
                kw["as_keyed"] = self.as_keyed_check.isChecked()
        elif fmt == "jsonl":
            if not node:
                kw["sort_keys"] = self.jsonl_sort_combo.currentData()
        elif fmt in ("csv", "xlsx"):
            if self.auto_columns_check.isChecked():
                kw["columns"] = None
            else:
                cols = self.column_list()
                if not cols:
                    raise UserInputError("gui.export.no_columns")
                kw["columns"] = cols
            kw["array_mode"] = self.array_mode_combo.currentData()
            explode = self.explode_edit.text().strip()
            kw["explode"] = explode or None
            kw["join_sep"] = self.join_sep_edit.text()
            if fmt == "csv":
                kw["csv_delimiter"] = _parse_delimiter(self.delimiter_combo.currentText())
                kw["csv_bom"] = self.bom_check.isChecked()
            else:
                kw["xlsx_split"] = self.split_combo.currentData()
                kw["xlsx_max_rows"] = self.max_rows_spin.value()
        return ExportOptions.from_settings(fmt, self.app.settings, **kw)

    # -------------------------------------------------------------------------------- reactions
    @staticmethod
    def _page_index(fmt: str) -> int:
        return {"json": 0, "jsonl": 1, "csv": 2, "xlsx": 2, "sqlite": 3}[fmt]

    def _on_path_changed(self, text: str) -> None:
        if self._syncing:
            self._validate()
            return
        fmt = writer.format_for_path(text.strip()) if text.strip() else None
        if fmt is not None and fmt != self.format():
            index = FORMATS.index(fmt)
            item_ok = self.scope() != "node" or fmt in NODE_FORMATS
            if item_ok:
                self._syncing = True
                try:
                    self.format_combo.setCurrentIndex(index)
                finally:
                    self._syncing = False
                self._update_format_widgets()
        self._validate()

    def _on_format_changed(self, _index: int = 0) -> None:
        fmt = self.format()
        if not self._syncing:
            text = self.path_edit.text().strip()
            if text:
                stem, ext = os.path.splitext(text)
                if writer.format_for_path(text) != fmt:
                    new = (stem if writer.format_for_path(text) is not None or not ext else text) + EXTENSIONS[fmt]
                    self._syncing = True
                    try:
                        self.path_edit.setText(new)
                    finally:
                        self._syncing = False
        self._update_format_widgets()
        self._validate()

    def _update_format_widgets(self) -> None:
        fmt = self.format()
        self.pages.setCurrentIndex(self._page_index(fmt))
        csv = fmt == "csv"
        xlsx = fmt == "xlsx"
        form = self.table_form
        for widget, show in ((self.delimiter_combo, csv), (self.bom_check, csv), (self.split_combo, xlsx),
                             (self.max_rows_spin, xlsx)):
            form.setRowVisible(widget, show)
        doc = self.session.doc
        wrapped = doc is not None and doc.kind == "wrapped"
        node = self.scope() == "node"
        self.keep_wrapper_check.setEnabled(wrapped and not node and not self.as_keyed_check.isChecked())
        self.keep_wrapper_check.setToolTip(t("gui.export.keep_wrapper.tip") if wrapped
                                           else t("gui.export.keep_wrapper.only_wrapped"))
        self.as_keyed_check.setEnabled(not node)
        for combo in (self.json_sort_combo, self.jsonl_sort_combo):
            combo.setEnabled(not node)
            combo.setToolTip(t("gui.export.node_no_sort") if node else t("gui.export.sort.tip"))

    def _on_scope_toggled(self, _id: int, checked: bool) -> None:
        if not checked:
            return
        node = self.scope() == "node"
        for i, fmt in enumerate(FORMATS):
            ok = not node or fmt in NODE_FORMATS
            _set_item_enabled(self.format_combo, i, ok, None if ok else t("gui.export.node_formats"))
        if node and self.format() not in NODE_FORMATS:
            self.set_format("json")
        self._update_format_widgets()
        self._validate()

    def _on_as_keyed(self, _on: bool) -> None:
        self._update_format_widgets()
        self._validate()

    def _on_auto_columns(self, on: bool) -> None:
        self.columns_edit.setEnabled(not on)
        self._validate()

    def _update_scope_texts(self) -> None:
        session = self.session
        doc = session.doc
        count = int(doc.record_count) if doc is not None else 0
        view_len = session.view_len()
        if session.view_is_base or doc is None:
            view_text = t("gui.export.scope.view_all", count=format_count(view_len))
        else:
            view_text = t("gui.export.scope.view", label=session.view_label(), count=format_count(view_len))
        self.scope_buttons["view"].setText(copyfmt.escape_surrogates(view_text).replace("&", "&&"))
        self.scope_buttons["all"].setText(t("gui.export.scope.all", count=format_count(count)))
        ref = session.current
        node_btn = self.scope_buttons["node"]
        if ref is None:
            node_btn.setText(t("gui.export.scope.node_none"))
            node_btn.setEnabled(False)
            node_btn.setToolTip(t("gui.export.scope.node_none.tip"))
        else:
            try:
                where = session.location_text(ref)
            except Exception:  # pragma: no cover - defensive
                where = "?"
            node_btn.setText(copyfmt.escape_surrogates(t("gui.export.scope.node", path=where)).replace("&", "&&"))
            node_btn.setEnabled(True)
            node_btn.setToolTip(t("gui.export.scope.node.tip"))
        single = doc is not None and doc.kind == "single"
        if single:
            for key in ("view", "all"):
                self.scope_buttons[key].setToolTip(t("gui.export.scope.single.tip"))

    # -------------------------------------------------------------------------------- validation
    def _validate(self, *_args: Any) -> bool:
        if self._syncing:
            return self._valid
        errors: list[str] = []
        warnings: list[str] = []
        session = self.session
        doc = session.doc
        if doc is None or getattr(session, "closed", False):
            errors.append(UserInputError("gui.session.not_open", name=session.name).message)
        path = self.output_path()
        if not path:
            errors.append(t("gui.export.need_path"))
        elif doc is not None:
            target = normalize_path(path)
            for p in (doc.source_path, doc.data_path, session.path):
                if target == normalize_path(p):
                    errors.append(UserInputError("writer.error.same_file", path=path).message
                                  + " " + t("gui.export.same_file.hint"))
                    break
        scope = self.scope()
        if scope == "node":
            if session.current is None:
                errors.append(t("gui.export.scope.node_none.tip"))
            if self.format() not in NODE_FORMATS:
                errors.append(t("gui.export.node_formats"))
        try:
            self.export_options()
        except JsonReshapeError as exc:
            errors.append(exc.message)
        ext_fmt = writer.format_for_path(path) if path else None
        if ext_fmt is not None and ext_fmt != self.format():
            warnings.append(t("gui.export.ext_mismatch", ext=os.path.splitext(path)[1],
                              fmt=t(f"gui.export.format.{self.format()}")))
        if scope == "view" and getattr(session, "view_busy", False):
            # the filter / sort result is not complete: exporting it now would write a part
            errors.append(t("gui.export.view_busy"))
        if doc is not None and doc.partial:
            warnings.append(t("gui.export.partial"))
        self.error_label.setText(copyfmt.escape_surrogates("\n".join(errors)))
        self.error_label.setVisible(bool(errors))
        self.warning_label.setText("\n".join(warnings))
        self.warning_label.setVisible(bool(warnings))
        self._valid = not errors
        self.export_button.setEnabled(self._valid and not self._own_running())
        return self._valid

    def error_text(self) -> str:
        return self.error_label.text() if not self.error_label.isHidden() else ""

    @property
    def running(self) -> bool:
        """The export shown (started here, or running already when the dialog opened)
        has not ended."""
        return self.task is not None and self.task.state == "running"

    def _own_running(self) -> bool:
        """An export started with this dialog runs (one that was running when the dialog
        opened does not stop another one from being started)."""
        return self.running and not self._adopted

    # -------------------------------------------------------------------------------- file chooser
    def choose_file(self) -> None:
        """The save dialog (filters per format; the extension decides the format)."""
        fmt = self.format()
        filters = [t(f"gui.export.filter.{f}") for f in FORMATS]
        allowed = filters if self.scope() != "node" else [t(f"gui.export.filter.{f}") for f in NODE_FORMATS]
        selected = t(f"gui.export.filter.{fmt}")
        path, chosen = QFileDialog.getSaveFileName(self, t("gui.export.choose.title"), self.path_edit.text(),
                                                   ";;".join(allowed), selected)
        if not path:
            return
        if writer.format_for_path(path) is None:
            index = filters.index(chosen) if chosen in filters else FORMATS.index(fmt)
            path += EXTENSIONS[FORMATS[index]]
        self.set_output_path(path)

    # -------------------------------------------------------------------------------- export
    def start_export(self) -> Any:
        """Validate and start the export task; returns it (``None`` when invalid or the
        user declined to overwrite)."""
        if self._own_running() or not self._validate():
            return None
        session = self.session
        doc = session.doc
        out = self.output_path()
        fmt = self.format()
        scope = self.scope()
        try:
            opts = self.export_options()
        except JsonReshapeError as exc:  # pragma: no cover - validated above
            self.error_label.setText(exc.message)
            self.error_label.setVisible(True)
            return None
        try:
            # also another name of the source (junction, share, hard link): checked once here
            check_not_source(out, (doc.source_path, doc.data_path, session.path))
        except UserInputError as exc:
            self.error_label.setText(exc.message + " " + t("gui.export.same_file.hint"))
            self.error_label.setVisible(True)
            return None
        if os.path.exists(long_path(out)) and not confirm(self, t("gui.export.overwrite", path=out)):
            return None
        self._remember(opts)
        ref = session.current
        view = None
        if scope == "view" and not session.view_is_base:
            # leased now (session.keep_document_for, through the runner): the session does
            # not close this record set before the task ended -- also when the view is
            # replaced while the task waits in the heavy-task queue -- so the task reads it
            # as it is now, without a copy
            view = session.view_recordset()
        indent = opts.indent

        def work(ctx: Any) -> ExportOutcome:
            if scope == "node":
                report = export_node(session, ref, out, fmt, indent=indent, progress=ctx.progress, cancel=ctx.cancel,
                                     doc=doc)
            else:
                if view is not None and view.closed:   # pragma: no cover - the lease prevents it
                    raise UserInputError("gui.search.view_changed")
                report = writer.export(doc, out, opts, view, progress=ctx.progress, cancel=ctx.cancel)
            sizes = []
            for p in report.files:
                try:
                    sizes.append(os.stat(long_path(p)).st_size)
                except OSError:  # pragma: no cover - just written
                    sizes.append(0)
            return ExportOutcome(report=report, file_sizes=sizes, scope=scope, path=out)

        self.outcome = None
        self.error = None
        self.report_dialog = None
        self.job = self.runner.start(session, work, path=out, scope=scope,
                                     title=t("gui.export.task", name=os.path.basename(out)),
                                     parents=(self, self.parentWidget()), sets=(view,))
        self.task = self.job.task
        self._adopted = False
        self._show_running()
        self.export_started.emit(self.task)
        return self.task

    def _adopt_running_job(self) -> None:
        """Opened again while an export of this file runs (or waits in the queue): show
        its state (progress, "navbatda", the stop button); the outcome is shown here too.
        Another export can still be started (it is then the one shown)."""
        jobs = [job for job in self.runner.jobs(self.session) if job.task is not None and job.task.state == "running"]
        if not jobs:
            return
        self.job = jobs[-1]
        self.task = self.job.task
        self._adopted = True
        self._show_running()

    def _show_running(self) -> None:
        """The running (or queued) state of :attr:`task`."""
        self._queued_shown = False
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(True)
        self.cancel_task_button.setVisible(True)
        if self.job is not None:
            self.status_label.setText(t("gui.export.status.running", path=self.job.path))
        self._show_task_state()
        self._validate()

    def _show_task_state(self) -> None:
        task, job = self.task, self.job
        if task is None or job is None or task.state != "running":
            return
        if getattr(task, "queued", False):
            # waiting for a free heavy-task slot: say so (not a frozen "running" bar)
            self._queued_shown = True
            self.progress_bar.setRange(0, 0)
            self.status_label.setText(t("gui.export.status.queued", path=job.path))
            return
        info = task.info()
        if self._queued_shown:     # its turn came: running now
            self.status_label.setText(t("gui.export.status.running", path=job.path))
        self._queued_shown = False
        if info is None or info.fraction is None:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(int(max(0.0, min(1.0, info.fraction)) * 1000))
            if info.message:
                self.status_label.setText(info.message)

    @property
    def queued(self) -> bool:
        """The export shown waits for its turn in the heavy-task queue."""
        return self.task is not None and self.task.state == "running" and bool(getattr(self.task, "queued", False))

    def cancel_export(self) -> None:
        if self.task is not None and self.task.state == "running":
            self.task.cancel()

    def _remember(self, opts: ExportOptions) -> None:
        data = {
            "fmt": opts.fmt, "indent": self.indent_combo.currentData(),
            "sort_keys": (self.json_sort_combo if opts.fmt == "json" else self.jsonl_sort_combo).currentData(),
            "keep_wrapper": self.keep_wrapper_check.isChecked(),
            "array_mode": self.array_mode_combo.currentData(), "join_sep": self.join_sep_edit.text(),
            "csv_delimiter": _parse_delimiter(self.delimiter_combo.currentText()),
            "csv_bom": self.bom_check.isChecked(), "xlsx_split": self.split_combo.currentData(),
            "xlsx_max_rows": self.max_rows_spin.value(),
        }
        state = self.app.state
        try:
            state.set(STATE_KEY, data)
            state.set(DIR_KEY, os.path.dirname(self.output_path()))
        except (TypeError, ValueError):  # pragma: no cover - plain JSON values
            log.debug("could not remember the export options", exc_info=True)
            return
        # GuiState is thread-safe; its file lock may wait for another window: not here
        save_state = getattr(self.app, "save_state", None)
        if callable(save_state):
            save_state()          # the context's coalesced background save
        else:
            self.app.tasks.run(lambda _ctx: state.save(), title=t("gui.export.task.state"), kind="state", silent=True)

    def _finish_ui(self) -> None:
        self.progress_bar.setVisible(False)
        self.cancel_task_button.setVisible(False)
        self._validate()

    def _job_finished(self, job: ExportJob) -> None:
        """The runner finished a job (it already showed the report / reported the error)."""
        if job is not self.job:
            return
        if job.state == "done" and job.outcome is not None:
            self._done(job.outcome, job.report_dialog)
        elif job.state == "failed" and job.error is not None:
            self._failed(job.error)
        else:
            self._cancelled()

    def _done(self, outcome: ExportOutcome, report_dialog: Any = None) -> None:
        self.outcome = outcome
        report = outcome.report
        self._finish_ui()
        self.status_label.setText(t("gui.export.status.done", records=format_count(report.records),
                                    size=format_size(report.bytes_written)))
        self.report_dialog = report_dialog
        self.export_finished.emit(outcome)

    def show_report(self, outcome: ExportOutcome) -> ExportReportDialog:
        """Show the report window (non-modal) next to this dialog or its window."""
        return ExportRunner.show_report(outcome, self if self.isVisible() else self.parentWidget())

    def _failed(self, exc: BaseException) -> None:
        self.error = exc
        self._finish_ui()
        text = exc.message if isinstance(exc, JsonReshapeError) else t("gui.widgets.error.unexpected")
        self.status_label.setText(t("gui.export.status.failed", error=text))
        self.export_failed.emit(exc)

    def _cancelled(self) -> None:
        self._finish_ui()
        self.status_label.setText(t("gui.export.status.cancelled"))

    def _on_task_updated(self, task: Any) -> None:
        if task is not self.task:
            return
        self._show_task_state()

    def reject(self) -> None:  # noqa: D401 - Qt name
        """Close the window; a running export goes on (tasks panel) and its outcome is
        still reported (:class:`ExportRunner`)."""
        super().reject()
